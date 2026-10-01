"""The daemon's Mail restarter (src/apple_mail_mcp/restarter.py).

Nothing here reaches Mail or signals a process. The policy is driven
against ``FakeMail``, a simulated Mail process, lock and clock in one;
the real host's osascript, ``open``, signals and process table are
replaced where it is tested.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import restarter
from apple_mail_mcp.restarter import MailRestarter, Probe, Usage
from apple_mail_mcp.security import TIER_LIMITS, operation_logger, rate_limiter

NOON = datetime(2026, 1, 14, 12, 0, 0)


class FakeMail:
    """A Mail process, the Mail lock and the clock. Time moves only when
    something waits: a sleep, a probe Mail does not answer, a lock that
    is not free."""

    def __init__(self, *, now: datetime = NOON) -> None:
        self.mono = 1000.0
        self.wall = now
        self.pid: int | None = 4242
        self.answers_from = float("-inf")
        self.quits_on_event = True
        self.dies_on: set[int] = {signal.SIGTERM, signal.SIGKILL}
        self.relaunch_answers_after: float | None = 3.0
        self.launch_error = ""
        self.probe_error = ""
        self.lock_free = True
        self.in_lock = False
        self.composing = False
        self.events: list[str] = []
        # Mail's resource use: CPU time accrues at ``cpu_share`` of the
        # time that passes; ``footprint`` is its physical footprint.
        self.cpu_share = 0.05
        self.cpu_s = 0.0
        self.footprint = 500 * 2**20

    # The clock.
    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.cpu_s += seconds * self.cpu_share
        self.wall += timedelta(seconds=seconds)

    def at(self, when: datetime) -> None:
        self.advance((when - self.wall).total_seconds())

    # Mail's state.
    def wedge(self) -> None:
        self.answers_from = float("inf")

    def unwedge(self) -> None:
        self.answers_from = float("-inf")

    # The host.
    @contextmanager
    def mail_lock(self, wait_s: float) -> Iterator[bool]:
        if not self.lock_free:
            self.advance(wait_s)
            self.events.append("lock busy")
            yield False
            return
        self.in_lock = True
        try:
            yield True
        finally:
            self.in_lock = False

    def mail_pid(self) -> int | None:
        assert self.in_lock, "Mail's process is looked up under the lock"
        return self.pid

    def is_mail(self, pid: int) -> bool:
        return pid == self.pid

    def usage(self, pid: int) -> Usage | None:
        if pid != self.pid:
            return None
        return Usage(cpu_s=self.cpu_s, footprint_bytes=self.footprint)

    def composition_in_flight(self) -> bool:
        assert self.in_lock
        return self.composing

    def probe(self, timeout_s: float) -> Probe:
        assert self.in_lock
        self.events.append("probe")
        if self.pid is None:
            return Probe("not_running")
        if self.probe_error:
            return Probe("error", self.probe_error)
        if self.mono >= self.answers_from:
            self.advance(0.1)
            return Probe("answered")
        self.advance(timeout_s)
        return Probe("silent", "AppleEvent timed out. (-1712)")

    def ask_to_quit(self, timeout_s: float) -> Probe:
        assert self.in_lock
        self.events.append("quit event")
        if self.pid is None:
            return Probe("not_running")
        if self.quits_on_event:
            self.pid = None
            return Probe("answered")
        self.advance(timeout_s)
        return Probe("silent", "AppleEvent timed out. (-1712)")

    def signal(self, pid: int, sig: int) -> bool:
        assert self.in_lock
        self.events.append(signal.Signals(sig).name)
        if pid != self.pid:
            return False
        if sig in self.dies_on:
            self.pid = None
        return True

    def launch(self) -> str:
        assert self.in_lock
        self.events.append("launch")
        if self.launch_error:
            return self.launch_error
        self.pid = 5151
        self.answers_from = (
            float("inf")
            if self.relaunch_answers_after is None
            else self.mono + self.relaunch_answers_after
        )
        return ""


@pytest.fixture
def mail() -> FakeMail:
    return FakeMail()


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    entries: list[tuple[Any, ...]] = []
    monkeypatch.setattr(operation_logger, "log_operation", lambda *a: entries.append(a))
    return entries


def _restarter(mail: FakeMail, **kw: Any) -> MailRestarter:
    kw.setdefault("interval_s", 300)
    return MailRestarter(host=mail, clock=mail, **kw)


def _restarts(logged: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [e for e in logged if e[0] == restarter.OPERATION]


class TestProbing:
    def test_a_mail_that_answers_is_left_alone(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        for _ in range(10):
            r.check()
            mail.advance(300)
        assert mail.events == ["probe"] * 10
        assert logged == []

    def test_consecutive_silent_probes_restart_mail(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        r.check()
        assert mail.events == ["probe"]
        assert logged == []
        r.check()
        assert mail.events[:3] == ["probe", "probe", "quit event"]
        [(_, params, result)] = _restarts(logged)
        assert params["reason"] == "unresponsive"
        assert result == "success"

    def test_a_probe_that_waits_for_the_lock_is_not_a_failure(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        mail.lock_free = False
        for _ in range(5):
            r.check()
        assert "probe" not in mail.events
        mail.lock_free = True
        r.check()
        assert mail.events.count("probe") == 1
        assert logged == []

    def test_an_answer_between_silences_starts_the_count_again(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        r.check()
        mail.unwedge()
        r.check()
        mail.wedge()
        r.check()
        assert logged == []

    def test_mail_not_running_is_not_a_wedge_and_is_not_launched(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.pid = None
        for _ in range(5):
            r.check()
        assert mail.events == []
        assert logged == []

    def test_an_error_that_is_not_silence_is_not_counted(
        self, mail: FakeMail, logged: list[tuple[Any, ...]], caplog: pytest.LogCaptureFixture
    ) -> None:
        r = _restarter(mail)
        mail.probe_error = "Not authorized to send Apple events to Mail. (-1743)"
        for _ in range(3):
            r.check()
        assert logged == []
        assert "-1743" in caplog.text

    def test_a_silent_probe_is_logged_at_warning(
        self, mail: FakeMail, caplog: pytest.LogCaptureFixture
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        with caplog.at_level("WARNING", logger="apple_mail_mcp.restarter"):
            r.check()
        [record] = [x for x in caplog.records if x.name == "apple_mail_mcp.restarter"]
        assert record.levelname == "WARNING"
        assert "did not answer" in record.getMessage()


class TestTheRestart:
    """Each escalation stage, reached by a wedged Mail's second silent
    probe. Every step is taken under the Mail lock (FakeMail asserts it)."""

    @pytest.fixture
    def wedged(self, mail: FakeMail) -> MailRestarter:
        mail.wedge()
        r = _restarter(mail)
        r.check()
        mail.events.clear()
        return r

    @staticmethod
    def _stages(params: dict[str, Any]) -> dict[str, str]:
        return {s["stage"]: s["outcome"] for s in params["stages"]}

    def test_the_quit_event_is_enough(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        wedged.check()
        assert mail.events == ["probe", "quit event", "launch", "probe"]
        [(_, params, result)] = _restarts(logged)
        assert result == "success"
        assert params["ended_by"] == "quit"
        assert params["answered"] is True
        assert params["old_pid"] == 4242
        assert self._stages(params) == {
            "quit_event": "answered",
            "quit": "exited",
            "launch": "ok",
            "answer": "answered",
        }
        assert all(isinstance(s["seconds"], float) for s in params["stages"])

    def test_sigterm_when_the_quit_event_is_not_enough(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        mail.quits_on_event = False
        wedged.check()
        assert mail.events == ["probe", "quit event", "SIGTERM", "launch", "probe"]
        [(_, params, result)] = _restarts(logged)
        assert result == "success"
        assert params["ended_by"] == "sigterm"
        assert self._stages(params)["quit"] == "running"
        assert self._stages(params)["sigterm"] == "exited"

    def test_sigkill_when_sigterm_is_not_enough(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        mail.quits_on_event = False
        mail.dies_on = {signal.SIGKILL}
        wedged.check()
        assert mail.events == [
            "probe", "quit event", "SIGTERM", "SIGKILL", "launch", "probe",
        ]
        [(_, params, result)] = _restarts(logged)
        assert result == "success"
        assert params["ended_by"] == "sigkill"

    def test_each_wait_for_the_process_is_bounded(
        self,
        mail: FakeMail,
        wedged: MailRestarter,
        logged: list[tuple[Any, ...]],
    ) -> None:
        mail.quits_on_event = False
        mail.dies_on = set()
        wedged.check()
        [(_, params, _)] = _restarts(logged)
        took = {s["stage"]: s["seconds"] for s in params["stages"]}
        poll = restarter.EXIT_POLL_S
        assert took["quit_event"] <= restarter.QUIT_EVENT_TIMEOUT_S
        assert restarter.QUIT_WAIT_S <= took["quit"] <= restarter.QUIT_WAIT_S + poll
        assert restarter.TERM_WAIT_S <= took["sigterm"] <= restarter.TERM_WAIT_S + poll
        assert restarter.KILL_WAIT_S <= took["sigkill"] <= restarter.KILL_WAIT_S + poll

    def test_a_mail_that_will_not_end_is_a_failure_and_is_not_relaunched(
        self,
        mail: FakeMail,
        wedged: MailRestarter,
        logged: list[tuple[Any, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mail.quits_on_event = False
        mail.dies_on = set()
        wedged.check()
        assert "launch" not in mail.events
        [(_, params, result)] = _restarts(logged)
        assert result == "failure"
        assert params["ended_by"] is None
        assert self._stages(params)["sigkill"] == "running"
        errors = [x for x in caplog.records if x.levelname == "ERROR"]
        assert errors and "could not end" in errors[0].getMessage()

    def test_a_relaunch_that_never_answers_is_a_failure_and_is_bounded(
        self,
        mail: FakeMail,
        wedged: MailRestarter,
        logged: list[tuple[Any, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mail.relaunch_answers_after = None
        launched_at: list[float] = []
        launch = mail.launch

        def recording_launch() -> str:
            launched_at.append(mail.mono)
            return launch()

        mail.launch = recording_launch  # type: ignore[method-assign]
        wedged.check()
        [(_, params, result)] = _restarts(logged)
        assert result == "failure"
        assert params["ended_by"] == "quit"
        assert params["answered"] is False
        assert self._stages(params)["answer"] == "silent"
        waited = mail.mono - launched_at[0]
        assert restarter.RELAUNCH_ANSWER_WAIT_S <= waited
        # The last probe may start just before the deadline.
        assert waited <= (
            restarter.RELAUNCH_ANSWER_WAIT_S
            + restarter.RELAUNCH_POLL_S
            + restarter.PROBE_TIMEOUT_S
        )
        errors = [x for x in caplog.records if x.levelname == "ERROR"]
        assert errors and "did not answer" in errors[0].getMessage()

    def test_a_relaunch_that_answers_late_is_waited_for(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        mail.relaunch_answers_after = 60.0
        wedged.check()
        [(_, params, result)] = _restarts(logged)
        assert result == "success"
        assert mail.events.count("probe") > 2

    def test_an_open_that_fails_is_a_failure(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        mail.launch_error = "Unable to find application named 'Mail'"
        wedged.check()
        [(_, params, result)] = _restarts(logged)
        assert result == "failure"
        assert self._stages(params)["launch"] == "Unable to find application named 'Mail'"
        assert "answer" not in self._stages(params)

    def test_a_composition_in_flight_does_not_put_off_an_unresponsive_restart(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        mail.composing = True
        wedged.check()
        [(_, params, _)] = _restarts(logged)
        assert params["reason"] == "unresponsive"

    def test_a_successful_restart_asks_once_for_what_follows(self, mail: FakeMail) -> None:
        """The daemon hands in its tender's request_pass, so tending,
        backed off from the wedged Mail, runs again at once."""
        called: list[int] = []
        mail.wedge()
        r = _restarter(mail, on_restarted=lambda: called.append(1))
        r.check()
        assert called == []
        r.check()
        assert called == [1]
        for _ in range(3):
            mail.advance(300)
            r.check()
        assert called == [1]

    @pytest.mark.parametrize("failure", ["will_not_end", "never_answers", "open_fails"])
    def test_a_failed_restart_does_not_ask(self, mail: FakeMail, failure: str) -> None:
        if failure == "will_not_end":
            mail.quits_on_event = False
            mail.dies_on = set()
        elif failure == "never_answers":
            mail.relaunch_answers_after = None
        else:
            mail.launch_error = "LSOpenURLsWithRole() failed"
        called: list[int] = []
        mail.wedge()
        r = _restarter(mail, on_restarted=lambda: called.append(1))
        r.check()
        r.check()
        assert "quit event" in mail.events
        assert called == []

    def test_the_count_starts_again_after_a_restart(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        wedged.check()
        mail.events.clear()
        mail.advance(restarter.RESTART_MIN_GAP_S)
        mail.wedge()
        wedged.check()
        assert mail.events == ["probe"]
        assert len(_restarts(logged)) == 1

    def test_is_rate_limited_with_the_other_mutations_and_tried_again(
        self, mail: FakeMail, wedged: MailRestarter, logged: list[tuple[Any, ...]]
    ) -> None:
        limit, _ = TIER_LIMITS["expensive_ops"]
        for _ in range(limit):
            assert rate_limiter.check("expensive_ops")
        wedged.check()
        assert "quit event" not in mail.events
        assert [e[2] for e in logged] == ["rate_limited"]
        rate_limiter.reset()
        wedged.check()
        assert "quit event" in mail.events


GIB = 2**30


class TestLoad:
    """Mail's CPU share and footprint, read each tick; preventive restarts
    when either stays high. Ticks are ``interval_s`` (300 s) apart."""

    @staticmethod
    def _ticks(r: MailRestarter, mail: FakeMail, n: int) -> None:
        for _ in range(n):
            mail.advance(300)
            r.check()

    def test_a_cpu_share_high_for_the_run_restarts_mail(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        r.check()
        mail.cpu_share = 1.0
        self._ticks(r, mail, restarter.CPU_HIGH_TICKS - 1)
        assert _restarts(logged) == []
        self._ticks(r, mail, 1)
        [(_, params, result)] = _restarts(logged)
        assert params["reason"] == "cpu"
        assert result == "success"
        shares = params["measured"]["cpu_shares"]
        assert len(shares) == restarter.CPU_HIGH_TICKS
        assert all(s >= restarter.CPU_HIGH_SHARE for s in shares)

    def test_one_low_tick_breaks_the_cpu_run(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        r.check()
        mail.cpu_share = 1.0
        self._ticks(r, mail, restarter.CPU_HIGH_TICKS - 1)
        mail.cpu_share = 0.2
        self._ticks(r, mail, 1)
        mail.cpu_share = 1.0
        self._ticks(r, mail, restarter.CPU_HIGH_TICKS - 1)
        assert _restarts(logged) == []
        self._ticks(r, mail, 1)
        assert [e[1]["reason"] for e in _restarts(logged)] == ["cpu"]

    def test_a_new_mail_process_starts_the_window_again(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        r.check()
        mail.cpu_share = 1.0
        self._ticks(r, mail, restarter.CPU_HIGH_TICKS - 1)
        mail.pid = 6000
        mail.cpu_s = 0.0
        self._ticks(r, mail, restarter.CPU_HIGH_TICKS)
        assert _restarts(logged) == []
        self._ticks(r, mail, 1)
        assert [e[1]["reason"] for e in _restarts(logged)] == ["cpu"]

    def test_a_footprint_high_on_two_ticks_restarts_mail(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.footprint = restarter.MEMORY_HIGH_BYTES
        r.check()
        assert _restarts(logged) == []
        self._ticks(r, mail, 1)
        [(_, params, result)] = _restarts(logged)
        assert params["reason"] == "memory"
        assert params["measured"] == {
            "footprint_bytes": [restarter.MEMORY_HIGH_BYTES] * restarter.MEMORY_HIGH_TICKS
        }

    def test_one_low_tick_breaks_the_memory_run(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.footprint = 4 * GIB
        r.check()
        mail.footprint = GIB
        self._ticks(r, mail, 1)
        mail.footprint = 4 * GIB
        self._ticks(r, mail, 1)
        assert _restarts(logged) == []

    def test_a_restart_starts_the_window_again(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.footprint = 4 * GIB
        r.check()
        self._ticks(r, mail, 1)
        assert len(_restarts(logged)) == 1
        # Still high after the relaunch: once the gap has passed, it takes
        # a fresh run of ticks, not the one from before the restart.
        mail.advance(restarter.RESTART_MIN_GAP_S)
        r.check()
        assert len(_restarts(logged)) == 1
        self._ticks(r, mail, 1)
        assert len(_restarts(logged)) == 2

    def test_respects_the_gap_between_restarts(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        r.check()
        r.check()
        assert [e[1]["reason"] for e in _restarts(logged)] == ["unresponsive"]
        mail.footprint = 4 * GIB
        self._ticks(r, mail, int(restarter.RESTART_MIN_GAP_S // 300) - 1)
        assert len(_restarts(logged)) == 1
        self._ticks(r, mail, 2)
        assert [e[1]["reason"] for e in _restarts(logged)] == ["unresponsive", "memory"]

    def test_waits_for_a_composition_in_flight(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.footprint = 4 * GIB
        mail.composing = True
        r.check()
        self._ticks(r, mail, 3)
        assert _restarts(logged) == []
        mail.composing = False
        self._ticks(r, mail, 1)
        assert [e[1]["reason"] for e in _restarts(logged)] == ["memory"]

    def test_warns_once_when_a_threshold_is_crossed_and_not_while_under(
        self, mail: FakeMail, caplog: pytest.LogCaptureFixture
    ) -> None:
        r = _restarter(mail)
        with caplog.at_level("INFO", logger="apple_mail_mcp.restarter"):
            r.check()
            self._ticks(r, mail, 5)
            assert caplog.records == []
            mail.cpu_share = 1.0
            self._ticks(r, mail, restarter.CPU_HIGH_TICKS - 1)
        warnings = [x.getMessage() for x in caplog.records if x.levelname == "WARNING"]
        assert len(warnings) == 1 and "CPU" in warnings[0]

    def test_an_unreadable_usage_is_no_measurement(
        self, mail: FakeMail, logged: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mail, "usage", lambda pid: None)
        mail.cpu_share = 1.0
        mail.footprint = 4 * GIB
        r = _restarter(mail)
        r.check()
        self._ticks(r, mail, 5)
        assert _restarts(logged) == []


class TestBackOff:
    def test_mail_is_not_restarted_again_within_the_gap(
        self, mail: FakeMail, logged: list[tuple[Any, ...]], caplog: pytest.LogCaptureFixture
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        r.check()
        r.check()
        assert len(_restarts(logged)) == 1
        mail.wedge()
        for _ in range(int(restarter.RESTART_MIN_GAP_S // 300) - 2):
            mail.advance(300)
            r.check()
        assert len(_restarts(logged)) == 1
        assert "not restarting it again" in caplog.text
        mail.advance(restarter.RESTART_MIN_GAP_S)
        r.check()
        assert len(_restarts(logged)) == 2

    def test_a_failed_restart_does_not_loop(
        self, mail: FakeMail, logged: list[tuple[Any, ...]]
    ) -> None:
        r = _restarter(mail)
        mail.wedge()
        mail.quits_on_event = False
        mail.dies_on = set()
        for _ in range(10):
            r.check()
            mail.advance(300)
        assert len(_restarts(logged)) == 1
        assert mail.events.count("SIGKILL") == 1


class TestTheSchedule:
    def test_restarts_mail_at_the_hour_once(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4)
        r.check()
        assert logged == []
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        r.check()
        [(_, params, result)] = _restarts(logged)
        assert params["reason"] == "scheduled"
        assert result == "success"
        for _ in range(5):
            mail.advance(300)
            r.check()
        assert len(_restarts(logged)) == 1

    def test_answering_mail_is_restarted_without_waiting_for_a_probe(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4)
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        r.check()
        assert mail.events[0] == "quit event"

    def test_a_day_the_daemon_was_down_at_the_hour_is_skipped(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 4, 2))
        r = _restarter(mail, restart_hour=4)
        for _ in range(12):
            r.check()
            mail.advance(300)
        assert logged == []
        mail.at(datetime(2026, 1, 15, 4, 0, 1))
        r.check()
        assert [e[1]["reason"] for e in _restarts(logged)] == ["scheduled"]

    def test_an_hour_passed_asleep_is_not_caught_up(
        self, logged: list[tuple[Any, ...]], caplog: pytest.LogCaptureFixture
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 55))
        r = _restarter(mail, restart_hour=4)
        r.check()
        mail.at(datetime(2026, 1, 14, 9, 30))
        caplog.set_level("INFO", logger="apple_mail_mcp.restarter")
        r.check()
        assert logged == []
        assert "skipped" in caplog.text

    def test_mail_not_running_at_the_hour_is_left_not_running(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4)
        mail.pid = None
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        r.check()
        mail.pid = 6000
        mail.advance(300)
        r.check()
        assert "launch" not in mail.events
        assert "quit event" not in mail.events
        assert logged == []

    def test_a_restart_shortly_before_the_hour_stands_for_it(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 40))
        r = _restarter(mail, restart_hour=4)
        mail.wedge()
        r.check()
        mail.advance(300)
        r.check()
        assert len(_restarts(logged)) == 1
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        r.check()
        mail.advance(300)
        r.check()
        assert [e[1]["reason"] for e in _restarts(logged)] == ["unresponsive"]

    def test_the_lock_being_busy_at_the_hour_puts_it_off(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4)
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        mail.lock_free = False
        r.check()
        assert logged == []
        mail.lock_free = True
        mail.advance(300)
        r.check()
        assert [e[1]["reason"] for e in _restarts(logged)] == ["scheduled"]

    def test_a_composition_in_flight_puts_it_off(
        self, logged: list[tuple[Any, ...]]
    ) -> None:
        """A composition is several osascript calls, each taking the lock
        on its own; a restart between two of them would take its window."""
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4)
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        mail.composing = True
        r.check()
        assert "quit event" not in mail.events
        assert logged == []
        mail.composing = False
        mail.advance(300)
        r.check()
        assert [e[1]["reason"] for e in _restarts(logged)] == ["scheduled"]

    def test_the_thread_wakes_for_the_hour(self) -> None:
        mail = FakeMail(now=datetime(2026, 1, 14, 3, 58))
        r = _restarter(mail, restart_hour=4, interval_s=300)
        assert 120 <= r.seconds_to_next_check() <= 122
        mail.at(datetime(2026, 1, 14, 4, 0, 1))
        assert r.seconds_to_next_check() == 300

    def test_an_hour_out_of_range_is_refused(self, mail: FakeMail) -> None:
        with pytest.raises(ValueError):
            _restarter(mail, restart_hour=24)
        with pytest.raises(ValueError):
            _restarter(mail, restart_hour=-1)


class TestTheThread:
    def test_checks_on_its_interval_until_stopped(self, mail: FakeMail) -> None:
        r = _restarter(mail, interval_s=0.02)
        r.start()
        try:
            deadline = time.monotonic() + 5
            while mail.events.count("probe") < 3:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        finally:
            r.stop()
        assert not r._thread.is_alive()

    def test_a_check_that_raises_is_logged_and_the_thread_goes_on(
        self, mail: FakeMail, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[int] = []

        def failing(timeout_s: float) -> Probe:
            calls.append(1)
            raise OSError("osascript vanished")

        mail.probe = failing  # type: ignore[method-assign]
        r = _restarter(mail, interval_s=0.02)
        r.start()
        try:
            deadline = time.monotonic() + 5
            while len(calls) < 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        finally:
            r.stop()
        assert "Mail restarter check failed" in caplog.text

    def test_an_interval_that_is_not_positive_is_refused(self, mail: FakeMail) -> None:
        with pytest.raises(ValueError):
            _restarter(mail, interval_s=0)


class TestStartingIt:
    def test_starts_a_restarter_on_the_real_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[MailRestarter] = []
        monkeypatch.setattr(MailRestarter, "start", lambda self: started.append(self))
        r = restarter.start_restarter(interval_s=42, restart_hour=5)
        assert started == [r]
        assert r.interval_s == 42 and r.restart_hour == 5
        assert isinstance(r._host, restarter.MacMailHost)
        assert r._on_restarted is None

    def test_passes_on_what_to_call_after_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(MailRestarter, "start", lambda self: None)

        def after() -> None:
            pass

        r = restarter.start_restarter(interval_s=42, restart_hour=5, on_restarted=after)
        assert r._on_restarted is after

    def test_the_stdio_server_never_starts_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many stdio servers run at once, one per session; only the
        daemon may restart Mail."""
        from apple_mail_mcp import server

        started: list[object] = []
        monkeypatch.setattr(MailRestarter, "start", lambda self: started.append(self))
        monkeypatch.setattr(server.mcp, "run", lambda *a, **kw: None)
        assert server.main([]) == 0
        assert started == []
        assert not [t for t in threading.enumerate() if t.name == "mail-restarter"]


MAIL = restarter.MAIL_EXECUTABLE


class TestTheMacHost:
    """The real host with its edges replaced: the process table, os.kill,
    and subprocess.run for osascript and open."""

    @pytest.fixture
    def table(self, monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
        procs: dict[int, str] = {
            1: "/sbin/launchd",
            300: "/Applications/Other.app/Contents/MacOS/Mail",
            4242: MAIL,
        }
        monkeypatch.setattr(restarter, "_user_pids", lambda: list(procs))
        monkeypatch.setattr(restarter, "_executable", procs.get)
        return procs

    @pytest.fixture
    def run(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        fake = MagicMock(return_value=subprocess.CompletedProcess([], 0, "answered\n", ""))
        monkeypatch.setattr(restarter.subprocess, "run", fake)
        return fake

    def test_finds_this_users_mail_by_its_executable(self, table: dict[int, str]) -> None:
        host = restarter.MacMailHost()
        assert host.mail_pid() == 4242
        assert host.is_mail(4242)
        assert not host.is_mail(300)

    def test_no_mail_is_none(self, table: dict[int, str]) -> None:
        del table[4242]
        assert restarter.MacMailHost().mail_pid() is None

    def test_signals_only_mail(
        self, table: dict[int, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(restarter.os, "kill", lambda pid, sig: kills.append((pid, sig)))
        host = restarter.MacMailHost()
        assert host.signal(4242, signal.SIGTERM) is True
        assert host.signal(300, signal.SIGTERM) is False
        assert host.signal(1, signal.SIGKILL) is False
        assert kills == [(4242, signal.SIGTERM)]

    def test_a_process_gone_before_the_signal_is_not_signalled(
        self, table: dict[int, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def gone(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(restarter.os, "kill", gone)
        assert restarter.MacMailHost().signal(4242, signal.SIGTERM) is False

    def test_the_probe_asks_mail_one_question_and_never_launches_it(
        self, run: MagicMock
    ) -> None:
        assert restarter.MacMailHost().probe(20) == Probe("answered")
        args, kwargs = run.call_args
        assert args[0] == ["/usr/bin/osascript", "-"]
        script = kwargs["input"]
        assert 'if application "Mail" is running then' in script
        assert "with timeout of 20 seconds" in script
        assert "count of accounts" in script
        assert kwargs["timeout"] > 20

    def test_the_probe_says_when_mail_is_not_running(self, run: MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "not running\n", "")
        assert restarter.MacMailHost().probe(20).outcome == "not_running"

    @pytest.mark.parametrize(
        ("stderr", "outcome"),
        [
            ("execution error: Mail got an error: AppleEvent timed out. (-1712)", "silent"),
            ("execution error: Mail got an error: Application isn't running. (-600)",
             "not_running"),
            ("execution error: Not authorized to send Apple events to Mail. (-1743)", "error"),
        ],
    )
    def test_the_probe_reads_osascript_errors(
        self, run: MagicMock, stderr: str, outcome: str
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", stderr)
        probe = restarter.MacMailHost().probe(20)
        assert probe.outcome == outcome
        assert probe.detail == stderr

    def test_an_osascript_that_runs_past_its_time_is_silence(self, run: MagicMock) -> None:
        run.side_effect = subprocess.TimeoutExpired(["osascript"], 25)
        assert restarter.MacMailHost().probe(20).outcome == "silent"

    def test_the_quit_event(self, run: MagicMock) -> None:
        assert restarter.MacMailHost().ask_to_quit(10).outcome == "answered"
        script = run.call_args.kwargs["input"]
        assert 'if application "Mail" is running then' in script
        assert "with timeout of 10 seconds" in script
        assert 'tell application "Mail" to quit' in script
        assert "saving" not in script

    def test_launches_mail_in_the_background(self, run: MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        assert restarter.MacMailHost().launch() == ""
        assert run.call_args.args[0] == ["/usr/bin/open", "-g", "-a", restarter.MAIL_APP]

    def test_a_launch_that_fails_says_why(self, run: MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "LSOpenURLsWithRole() failed")
        assert restarter.MacMailHost().launch() == "LSOpenURLsWithRole() failed"
        run.side_effect = subprocess.TimeoutExpired(["open"], 30)
        assert "did not return" in restarter.MacMailHost().launch()

    def test_a_composition_in_flight_is_an_open_record_within_the_grace(self) -> None:
        from apple_mail_mcp.compose_ledger import Closed, ComposeLedger
        from apple_mail_mcp.compose_tending import TEND_GRACE_S

        host = restarter.MacMailHost()
        assert host.composition_in_flight() is False
        ledger = ComposeLedger()
        now = time.time()
        stale = ledger.open(
            window_name="Old", window_id=1, mail_pid=4242, operation="send",
            seed="new", now=now - TEND_GRACE_S - 1,
        )
        assert host.composition_in_flight() is False
        fresh = ledger.open(
            window_name="New", window_id=2, mail_pid=4242, operation="send",
            seed="new", now=now,
        )
        assert host.composition_in_flight() is True
        ledger.end(fresh.record_id, Closed(how="discarded", by="composition", at=now))
        assert host.composition_in_flight() is False
        assert stale.unfinished

    def test_the_lock_is_the_mail_lock(self) -> None:
        from apple_mail_mcp import mail_lock

        with restarter.MacMailHost().mail_lock(1.0) as got:
            assert got is True
            assert mail_lock.acquire(0.1) is None


class TestTheUsageRead:
    """The real host's read of a process's CPU time and footprint, on this
    test's own process: read-only, and never Mail's."""

    def test_cpu_time_is_in_seconds(self) -> None:
        import os
        import resource

        end = time.process_time() + 0.2
        while time.process_time() < end:
            pass
        usage = restarter.MacMailHost().usage(os.getpid())
        ru = resource.getrusage(resource.RUSAGE_SELF)
        assert usage is not None
        assert usage.cpu_s == pytest.approx(ru.ru_utime + ru.ru_stime, rel=0.05)
        assert usage.footprint_bytes > 0

    def test_a_pid_that_is_no_process_has_none(self) -> None:
        assert restarter.MacMailHost().usage(999_999_99) is None


class TestTheProcessTable:
    """Read-only reads of this machine's process table through libproc:
    this test's own process, never Mail's."""

    def test_lists_this_process_and_reads_its_executable(self) -> None:
        import os

        assert os.getpid() in restarter._user_pids()
        path = restarter._executable(os.getpid())
        assert path is not None and os.path.isfile(path)

    def test_a_pid_that_is_no_process_has_no_executable(self) -> None:
        assert restarter._executable(999_999_99) is None
