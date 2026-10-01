"""Quitting and relaunching Mail from the resident daemon.

Mail can stop answering Apple events while its process stays up, its
main thread busy and never returning to the event loop. Every call that
reaches Mail then times out, each holding the Mail lock while it does,
and nothing recovers until Mail is ended. The operator has granted that
the server may quit and relaunch Mail periodically, so that a wedged
Mail recovers without an agent or a person.

The daemon (``mail-serve``) runs a ``MailRestarter`` thread. Every
``PROBE_INTERVAL_S`` it asks Mail one cheap question under the Mail lock
(``MacMailHost.probe``). After ``SILENT_PROBES_TO_RESTART`` probes in a
row that Mail did not answer within ``PROBE_TIMEOUT_S``, it restarts
Mail with reason ``unresponsive``. Once a day, at a local hour
(``RESTART_HOUR`` by default), it restarts Mail with reason
``scheduled``; a day whose hour passed while the daemon was not running
(or the machine was asleep) is skipped, not caught up, and the restart
waits while a composition is in flight, since a composition is several
osascript calls that each take the lock on their own. Only Mail's
silence counts: a probe that could not have the lock, or that Mail
answered with an error, is not a failure, and a Mail that is not
running is not a wedge. Nothing here launches a Mail that was not
running.

A restart holds the Mail lock throughout, so no osascript is mid-flight
against Mail: ask Mail to quit; if its process has not exited within a
bound, SIGTERM; then SIGKILL; then ``open -g`` it and wait, bounded,
for a probe it answers. The process signalled is only this user's whose
executable is ``MAIL_EXECUTABLE``. Every restart is logged through
``operation_logger`` as ``restart_mail`` with its reason, each stage's
outcome and how long it took, and counts against the ``expensive_ops``
rate tier. One that could not end Mail, or whose relaunch never
answered, is logged as a failure and at ERROR. Whatever its outcome, no
other restart follows within ``RESTART_MIN_GAP_S``, so a Mail that will
not come back is not killed over and over. After one that succeeded,
the daemon's tender is asked for a pass (``on_restarted``), so tending
does not stay backed off from the Mail that was wedged.

The stdio server never restarts Mail: many of them can run at once, one
per session, for the same reason none of them tends compose windows
(``tender.py``). Compose windows Mail restores at a relaunch get new
ids, and tending ends their ledger records as ``gone``
(``compose_tending``).

This module imports nothing that builds a Mail connector, so ``serve``
(and through it ``mail-proxy``) can take its defaults from here.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol

from . import mail_lock
from .compose_ledger import ComposeLedger
from .compose_tending import TEND_GRACE_S, in_flight
from .security import check_rate_limit, operation_logger

logger = logging.getLogger(__name__)

OPERATION = "restart_mail"

MAIL_APP = "/System/Applications/Mail.app"
MAIL_EXECUTABLE = f"{MAIL_APP}/Contents/MacOS/Mail"

PROBE_INTERVAL_S = 5 * 60
"""Between the daemon's probes of Mail."""

PROBE_TIMEOUT_S = 20
"""How long Mail has to answer a probe. A probe is one event Mail
answers at once when its main thread is free."""

SILENT_PROBES_TO_RESTART = 2
"""Probes in a row Mail must leave unanswered before it is restarted:
one may land in a long but finite stall."""

RESTART_HOUR = 4
"""The local hour of the daily restart."""

SCHEDULE_LATE_LIMIT_S = 60 * 60
"""How late the daily restart may still run, when the lock was busy at
the hour or the daemon could not run then (the machine asleep). Later
than this, that day's is skipped."""

RESTART_MIN_GAP_S = 60 * 60
"""The least time between the starts of two restarts, whatever their
outcome or reason. A Mail rebuilding its index after a SIGKILL can be
slow to answer for a while; it is not killed again for that."""

LOCK_WAIT_S = 90.0
"""How long a probe or a restart waits for the Mail lock. Longer than
the connector's 60 s osascript timeout, so a call stuck on a wedged Mail
times out and lets go within it."""

QUIT_EVENT_TIMEOUT_S = 10
"""How long Mail has to answer the quit event."""

QUIT_WAIT_S = 30.0
"""How long Mail's process has to exit after the quit event, before
SIGTERM."""

TERM_WAIT_S = 15.0
"""How long it has to exit after SIGTERM, before SIGKILL."""

KILL_WAIT_S = 5.0
"""How long it has to exit after SIGKILL, before the restart fails."""

EXIT_POLL_S = 0.5

LAUNCH_TIMEOUT_S = 30
"""How long ``open`` has to return."""

RELAUNCH_ANSWER_WAIT_S = 180.0
"""How long the relaunched Mail has to answer a probe."""

RELAUNCH_POLL_S = 5.0

_OSASCRIPT_GRACE_S = 5
"""osascript's own timeout runs this much past the script's, as a
backstop: the script's ``with timeout`` is meant to end it first."""

Reason = Literal["unresponsive", "scheduled"]
ProbeOutcome = Literal["answered", "silent", "not_running", "error"]
EndedBy = Literal["quit", "sigterm", "sigkill"]


@dataclass(frozen=True)
class Probe:
    """What came of one question to Mail: it answered, it was silent
    (the event timed out), it was not running, or the event failed
    some other way (``detail`` says how)."""

    outcome: ProbeOutcome
    detail: str = ""


@dataclass(frozen=True)
class Stage:
    name: str
    outcome: str
    seconds: float


@dataclass(frozen=True)
class RestartReport:
    reason: Reason
    old_pid: int
    stages: tuple[Stage, ...]
    ended_by: EndedBy | None
    answered: bool
    new_pid: int | None
    seconds: float

    @property
    def succeeded(self) -> bool:
        return self.ended_by is not None and self.answered

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "old_pid": self.old_pid,
            "ended_by": self.ended_by,
            "answered": self.answered,
            "new_pid": self.new_pid,
            "seconds": round(self.seconds, 1),
            "stages": [
                {"stage": s.name, "outcome": s.outcome, "seconds": round(s.seconds, 1)}
                for s in self.stages
            ],
        }


class MailHost(Protocol):
    """Mail's process, the questions put to it, and the Mail lock: what
    the restarter acts through."""

    def mail_lock(self, wait_s: float) -> AbstractContextManager[bool]: ...

    def mail_pid(self) -> int | None: ...

    def is_mail(self, pid: int) -> bool: ...

    def composition_in_flight(self) -> bool: ...

    def probe(self, timeout_s: float) -> Probe: ...

    def ask_to_quit(self, timeout_s: float) -> Probe: ...

    def signal(self, pid: int, sig: int) -> bool: ...

    def launch(self) -> str: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# This user's processes and their executables, from libproc.
_PROC_UID_ONLY = 4
_PROC_PIDPATHINFO_MAXSIZE = 4096


def _libproc() -> Any:
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _user_pids() -> list[int]:
    """The pids of this user's processes."""
    lib = _libproc()
    needed = lib.proc_listpids(_PROC_UID_ONLY, os.getuid(), None, 0)
    if needed <= 0:
        return []
    # Room for processes started between the two calls.
    buf = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 64))()
    got = lib.proc_listpids(_PROC_UID_ONLY, os.getuid(), buf, ctypes.sizeof(buf))
    return [pid for pid in buf[: max(got, 0) // ctypes.sizeof(ctypes.c_int)] if pid > 0]


def _executable(pid: int) -> str | None:
    """The path of the executable ``pid`` runs, from the kernel; None
    when there is no such process (or it has exited)."""
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    if _libproc().proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE) <= 0:
        return None
    return buf.value.decode("utf-8", "replace")


# Both scripts check that Mail is running first, so that neither starts
# it. Their only interpolations are this module's integer timeouts.
_PROBE_SCRIPT = """
if application "Mail" is running then
    with timeout of {timeout} seconds
        tell application "Mail" to count of accounts
    end timeout
    return "answered"
end if
return "not running"
"""

_QUIT_SCRIPT = """
if application "Mail" is running then
    with timeout of {timeout} seconds
        tell application "Mail" to quit
    end timeout
    return "answered"
end if
return "not running"
"""


class MacMailHost:
    """The real host. Mail is this user's process whose executable is
    ``MAIL_EXECUTABLE``, found through libproc; nothing else is ever
    signalled. The probe is ``count of accounts``: one event that Mail
    must answer on its main thread, and the one seen to go unanswered
    when Mail wedged."""

    def mail_lock(self, wait_s: float) -> AbstractContextManager[bool]:
        return mail_lock.held(wait_s)

    def mail_pid(self) -> int | None:
        for pid in _user_pids():
            if _executable(pid) == MAIL_EXECUTABLE:
                return pid
        return None

    def is_mail(self, pid: int) -> bool:
        return pid in _user_pids() and _executable(pid) == MAIL_EXECUTABLE

    def composition_in_flight(self) -> bool:
        """Some composition, in this process or another, may be between
        two of its osascript calls: the compose ledger has a window
        record for it that nothing has ended, opened within the grace
        tending gives one (``compose_tending.in_flight``)."""
        now = time.time()
        return any(
            in_flight(record, now, TEND_GRACE_S) for record in ComposeLedger().read_all().records
        )

    def probe(self, timeout_s: float) -> Probe:
        return self._ask(_PROBE_SCRIPT, timeout_s)

    def ask_to_quit(self, timeout_s: float) -> Probe:
        return self._ask(_QUIT_SCRIPT, timeout_s)

    def signal(self, pid: int, sig: int) -> bool:
        """Send ``sig`` to ``pid`` if it is still Mail. False when it is
        not, or exited before the signal."""
        if not self.is_mail(pid):
            return False
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def launch(self) -> str:
        """``open -g`` Mail: launched in the background, without taking
        focus. Returns "" when ``open`` succeeded, else why not."""
        try:
            result = subprocess.run(
                ["/usr/bin/open", "-g", "-a", MAIL_APP],
                capture_output=True,
                text=True,
                timeout=LAUNCH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"open did not return within {LAUNCH_TIMEOUT_S} s"
        if result.returncode != 0:
            return result.stderr.strip() or f"open exited {result.returncode}"
        return ""

    @staticmethod
    def _ask(template: str, timeout_s: float) -> Probe:
        script = template.format(timeout=int(timeout_s))
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-"],
                input=script,
                capture_output=True,
                text=True,
                timeout=int(timeout_s) + _OSASCRIPT_GRACE_S,
            )
        except subprocess.TimeoutExpired:
            return Probe("silent", f"osascript ran past {int(timeout_s) + _OSASCRIPT_GRACE_S} s")
        if result.returncode == 0:
            if result.stdout.strip() == "not running":
                return Probe("not_running")
            return Probe("answered")
        err = result.stderr.strip()
        if "(-1712)" in err:
            return Probe("silent", err)
        if "(-600)" in err:
            return Probe("not_running", err)
        return Probe("error", err)


class MailRestarter:
    """The daemon's Mail restarter: ``start`` its thread, ``stop`` it.
    ``check`` is one turn of it, which the thread runs every
    ``interval_s`` and at the scheduled hour."""

    def __init__(
        self,
        *,
        interval_s: float,
        restart_hour: int = RESTART_HOUR,
        host: MailHost | None = None,
        clock: Clock | None = None,
        on_restarted: Callable[[], None] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")
        if not 0 <= restart_hour <= 23:
            raise ValueError(f"restart_hour must be 0 to 23, got {restart_hour}")
        self.interval_s = interval_s
        self.restart_hour = restart_hour
        self._host: MailHost = host if host is not None else MacMailHost()
        self._clock: Clock = clock if clock is not None else SystemClock()
        # Called after a restart whose relaunched Mail answered. The
        # daemon passes its tender's request_pass, so tending, backed
        # off from the wedged Mail, runs again at once.
        self._on_restarted = on_restarted
        self._silent_probes = 0
        self._last_restart: float | None = None
        self._last_check = self._clock.now()
        self._scheduled_for: datetime | None = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="mail-restarter", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._thread.join(timeout)

    def check(self) -> None:
        """Note whether the scheduled hour has come; then, under the Mail
        lock, restart Mail if it is due, else probe it and restart it if
        it has stopped answering."""
        self._note_schedule(self._clock.now())
        with self._host.mail_lock(LOCK_WAIT_S) as held:
            if not held:
                logger.info(
                    "Mail restarter: the Mail lock was busy for %.0f s; "
                    "Mail was not probed, and that is not counted against it",
                    LOCK_WAIT_S,
                )
                return
            pid = self._host.mail_pid()
            if pid is None:
                self._silent_probes = 0
                if self._scheduled_for is not None:
                    logger.info("scheduled restart of Mail skipped: Mail is not running")
                    self._scheduled_for = None
                return
            reason = self._due_reason()
            if reason is not None:
                self._restart(reason, pid)

    def seconds_to_next_check(self) -> float:
        """Until the next probe, or the scheduled hour if that is sooner
        (and a second past it, so the check that wakes finds it come)."""
        now = self._clock.now()
        at = self._occurrence(now) + timedelta(days=1)
        return min(self.interval_s, (at - now).total_seconds() + 1)

    def _loop(self) -> None:
        while not self._stopping.wait(self.seconds_to_next_check()):
            try:
                self.check()
            except Exception:
                logger.exception("Mail restarter check failed")

    def _occurrence(self, now: datetime) -> datetime:
        """The scheduled hour's latest occurrence at or before ``now``."""
        at = now.replace(hour=self.restart_hour, minute=0, second=0, microsecond=0)
        return at if at <= now else at - timedelta(days=1)

    def _note_schedule(self, now: datetime) -> None:
        at = self._occurrence(now)
        if self._last_check < at <= now:
            self._scheduled_for = at
        self._last_check = now
        if (
            self._scheduled_for is not None
            and (now - self._scheduled_for).total_seconds() > SCHEDULE_LATE_LIMIT_S
        ):
            logger.info(
                "scheduled restart of Mail for %s skipped: the daemon could not run it "
                "within %.0f min of the hour",
                self._scheduled_for.isoformat(timespec="minutes"),
                SCHEDULE_LATE_LIMIT_S / 60,
            )
            self._scheduled_for = None

    def _backing_off(self) -> bool:
        return (
            self._last_restart is not None
            and self._clock.monotonic() - self._last_restart < RESTART_MIN_GAP_S
        )

    def _due_reason(self) -> Reason | None:
        """Under the lock, with Mail running: why Mail is to be restarted
        now, if it is."""
        if self._scheduled_for is not None:
            if self._backing_off():
                logger.info("scheduled restart of Mail skipped: Mail was restarted recently")
                self._scheduled_for = None
            elif self._host.composition_in_flight():
                logger.info("scheduled restart of Mail put off: a composition is in flight")
            else:
                return "scheduled"
        probe = self._host.probe(PROBE_TIMEOUT_S)
        if probe.outcome in ("answered", "not_running"):
            self._silent_probes = 0
            return None
        if probe.outcome == "error":
            logger.warning("Mail probe failed, not counted as silence: %s", probe.detail)
            return None
        self._silent_probes += 1
        logger.warning(
            "Mail did not answer a probe within %d s (%d in a row): %s",
            PROBE_TIMEOUT_S,
            self._silent_probes,
            probe.detail,
        )
        if self._silent_probes < SILENT_PROBES_TO_RESTART:
            return None
        if self._backing_off():
            logger.warning(
                "Mail is not answering, but it was restarted less than %.0f min ago; "
                "not restarting it again until then",
                RESTART_MIN_GAP_S / 60,
            )
            return None
        return "unresponsive"

    def _restart(self, reason: Reason, pid: int) -> None:
        """Under the lock: restart Mail, log it, and start the back-off.
        A restart the rate limiter refuses is left for the next check."""
        if check_rate_limit(OPERATION, {"reason": reason, "old_pid": pid}) is not None:
            return
        self._last_restart = self._clock.monotonic()
        self._silent_probes = 0
        self._scheduled_for = None
        report = _Restart(self._host, self._clock, reason, pid).run()
        operation_logger.log_operation(
            OPERATION, report.as_dict(), "success" if report.succeeded else "failure"
        )
        _log_restart(report)
        if report.succeeded and self._on_restarted is not None:
            self._on_restarted()


def _log_restart(report: RestartReport) -> None:
    stages = {s.name: s.outcome for s in report.stages}
    gap_min = RESTART_MIN_GAP_S / 60
    if report.succeeded:
        logger.log(
            logging.WARNING if report.reason == "unresponsive" else logging.INFO,
            "restarted Mail (%s): ended by %s, answering again after %.0f s",
            report.reason, report.ended_by, report.seconds,
        )
    elif report.ended_by is None:
        logger.error(
            "restart of Mail (%s) failed: could not end process %d, even with SIGKILL; "
            "not trying again for %.0f min",
            report.reason, report.old_pid, gap_min,
        )
    elif stages.get("launch") != "ok":
        logger.error(
            "restart of Mail (%s) failed: ended by %s, but could not relaunch it (%s); "
            "not trying again for %.0f min",
            report.reason, report.ended_by, stages.get("launch"), gap_min,
        )
    else:
        logger.error(
            "restart of Mail (%s) failed: relaunched Mail did not answer within %.0f s "
            "(last probe: %s); not restarting it again for %.0f min",
            report.reason, RELAUNCH_ANSWER_WAIT_S, stages.get("answer"), gap_min,
        )


class _Restart:
    """One restart, its stages timed as they go. Run under the lock."""

    def __init__(self, host: MailHost, clock: Clock, reason: Reason, pid: int) -> None:
        self._host = host
        self._clock = clock
        self._reason = reason
        self._pid = pid
        self._stages: list[Stage] = []

    def run(self) -> RestartReport:
        started = self._clock.monotonic()
        ended_by = self._end()
        answered = False
        new_pid: int | None = None
        if ended_by is not None and self._timed("launch", self._launch) == "ok":
            answered = self._timed("answer", self._await_answer) == "answered"
            new_pid = self._host.mail_pid() if answered else None
        return RestartReport(
            reason=self._reason,
            old_pid=self._pid,
            stages=tuple(self._stages),
            ended_by=ended_by,
            answered=answered,
            new_pid=new_pid,
            seconds=self._clock.monotonic() - started,
        )

    def _timed(self, name: str, step: Callable[[], str]) -> str:
        began = self._clock.monotonic()
        outcome = step()
        self._stages.append(Stage(name, outcome, self._clock.monotonic() - began))
        return outcome

    def _end(self) -> EndedBy | None:
        """The quit event, then each signal in turn, until the process
        has exited; which of them ended it, or None."""
        self._timed("quit_event", self._ask_to_quit)
        steps: tuple[tuple[EndedBy, int | None, float], ...] = (
            ("quit", None, QUIT_WAIT_S),
            ("sigterm", signal.SIGTERM, TERM_WAIT_S),
            ("sigkill", signal.SIGKILL, KILL_WAIT_S),
        )
        for name, sig, wait_s in steps:
            if self._timed(name, partial(self._signal_and_wait, sig, wait_s)) == "exited":
                return name
        return None

    def _ask_to_quit(self) -> str:
        """Mail's answer to the quit event. Its process exiting, not the
        answer, is what the next stage waits on."""
        asked = self._host.ask_to_quit(QUIT_EVENT_TIMEOUT_S)
        return f"{asked.outcome}: {asked.detail}" if asked.detail else asked.outcome

    def _signal_and_wait(self, sig: int | None, wait_s: float) -> str:
        if sig is not None:
            self._host.signal(self._pid, sig)
        deadline = self._clock.monotonic() + wait_s
        while self._host.is_mail(self._pid):
            if self._clock.monotonic() >= deadline:
                return "running"
            self._clock.sleep(EXIT_POLL_S)
        return "exited"

    def _launch(self) -> str:
        return self._host.launch() or "ok"

    def _await_answer(self) -> str:
        deadline = self._clock.monotonic() + RELAUNCH_ANSWER_WAIT_S
        outcome = "not_running"
        while self._clock.monotonic() < deadline:
            self._clock.sleep(RELAUNCH_POLL_S)
            outcome = self._host.probe(PROBE_TIMEOUT_S).outcome
            if outcome == "answered":
                break
        return outcome


def start_restarter(
    interval_s: float = PROBE_INTERVAL_S,
    restart_hour: int = RESTART_HOUR,
    on_restarted: Callable[[], None] | None = None,
) -> MailRestarter:
    """Start this process's Mail restarter. ``on_restarted`` is called
    after each restart whose relaunched Mail answered."""
    restarter = MailRestarter(
        interval_s=interval_s, restart_hour=restart_hour, on_restarted=on_restarted
    )
    restarter.start()
    logger.info(
        "probing Mail every %.0f s, restarting it when it stops answering and daily at %02d:00",
        interval_s,
        restart_hour,
    )
    return restarter
