"""Tending Mail's compose windows from the resident daemon.

Mail accumulated compose windows over days — failed sends, failed
salvages, compositions whose process died — and restored all of them
at a relaunch (25 on 2026-09-27). The operator's direction that morning:
"the mail app regularly has been accumulating failed windows so the MCP
should periodically tend it reguardless". A pass
(``AppleMailConnector.tend_compose_windows``) closes, each by Mail's
window id, the windows the compose ledger says this connector opened and
abandoned, and any other compose window whose content has not changed
for ``compose_tending.STALE_S``, whoever opened it; an empty one is
discarded and any other salvaged to Drafts, so nothing typed is lost
(docs/research/compose-window-tending.md).

The daemon (``mail-serve``) runs one pass when it starts, then one every
``TEND_INTERVAL_S``, and one soon after any composition ends with its
window open, but never two within ``TEND_MIN_GAP_S``. While Mail is not
answering Apple events at all, a pass times out holding the cross-process
Mail automation lock for the full timeout; consecutive timeouts widen the
gap between passes up to ``TEND_BACKOFF_CAP_S`` instead of hammering a
Mail that cannot respond, and a request still cuts that wait short
(``ComposeTender``). The stdio server does not tend: many of them can run
at once, each for one session, and one resident process is enough; the
windows a stdio server leaves are in the same ledger, where the daemon
finds them.

Every pass is logged through ``operation_logger`` as
``tend_compose_windows``, and counts against the ``expensive_ops`` rate
tier like any other change to Mail's state.

This module imports ``server`` only when a pass runs or the tender
starts, so ``serve`` (and through it ``mail-proxy``) can take the
interval from here without building a Mail connector.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .exceptions import MailTimeoutError
from .security import check_rate_limit, operation_logger

logger = logging.getLogger(__name__)

OPERATION = "tend_compose_windows"

TEND_INTERVAL_S = 15 * 60
"""Between the daemon's passes, while Mail answers them. A pass holds
the Mail lock while it reads the windows, about 10 s for 25 of them
(2026-10-01), so it is not run often; a composition that leaves its
window open asks for one at once instead."""

TEND_MIN_GAP_S = 60.0
"""The least time between the starts of two passes, however many
compositions ask."""

TEND_BACKOFF_CAP_S = 4 * 60 * 60
"""The most a backed-off wait ever reaches (see ``ComposeTender``), so a
Mail that stays wedged for a long time still gets tried a few times a
day rather than never."""


def run_tend_pass(*, dry_run: bool = False) -> dict[str, Any]:
    """One pass over Mail's compose windows through the server's
    connector, logged. Returns the pass's report, or the rate limiter's
    refusal. A pass that fails is logged as a failure and raised."""
    from . import server

    params: dict[str, Any] = {"dry_run": dry_run}
    limited = check_rate_limit(OPERATION, params)
    if limited is not None:
        return limited
    try:
        report = server.mail.tend_compose_windows(dry_run=dry_run)
    except Exception as exc:
        operation_logger.log_operation(
            OPERATION, {**params, "error": f"{type(exc).__name__}: {exc}"}, "failure"
        )
        raise
    found = report.as_dict()
    operation_logger.log_operation(
        OPERATION,
        found,
        "failure" if report.failed or report.not_attempted else "success",
    )
    return found


class ComposeTender:
    """The daemon's tending thread: a pass at once, then one per
    ``interval_s``, and one when asked (``request_pass``), with at least
    ``min_gap_s`` between the starts of any two. A pass that raises is
    logged and the thread goes on.

    A pass that times out means Mail is not answering Apple events at
    all, and held the cross-process Mail automation lock for the whole
    timeout to find that out — retrying it at full cadence only adds
    lock contention a wedged Mail cannot relieve. So each consecutive
    timeout doubles the wait before the next scheduled pass, starting
    from ``interval_s`` and capped at ``TEND_BACKOFF_CAP_S``; a request
    (``request_pass``) still cuts that wait short, since a composition
    reaching Mail is evidence it answers again. A pass that succeeds, or
    fails some other way, resets the wait to ``interval_s`` — only a
    timeout is evidence Mail itself is unresponsive, so only a timeout
    backs off."""

    def __init__(
        self,
        *,
        interval_s: float,
        min_gap_s: float = TEND_MIN_GAP_S,
        run_pass: Callable[[], object] = run_tend_pass,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")
        self.interval_s = interval_s
        self.min_gap_s = min_gap_s
        self._run_pass = run_pass
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="compose-tender", daemon=True
        )
        self.passes = 0
        self._wait_s = interval_s

    def start(self) -> None:
        self._thread.start()

    def request_pass(self) -> None:
        """Ask for a pass soon: now, or once ``min_gap_s`` has passed
        since the last one started. Requests made during a pass are one
        request."""
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            # Cleared before the pass, so a request made during it wakes
            # the wait below at once: that pass could not have seen it.
            self._wake.clear()
            started = time.monotonic()
            try:
                self._run_pass()
            except MailTimeoutError:
                logger.exception("compose-window tending pass failed")
                self._back_off()
            except Exception:
                logger.exception("compose-window tending pass failed")
                self._reset_wait()
            else:
                self._recover()
            self.passes += 1
            self._wake.wait(self._wait_s)
            remaining = self.min_gap_s - (time.monotonic() - started)
            if remaining > 0:
                self._stopping.wait(remaining)

    def _back_off(self) -> None:
        """A pass just timed out: double the wait before the next one,
        capped at ``TEND_BACKOFF_CAP_S``. Logged once per change, so a
        Mail stuck at the cap does not repeat the warning every pass."""
        grown = min(self._wait_s * 2, TEND_BACKOFF_CAP_S)
        if grown != self._wait_s:
            self._wait_s = grown
            logger.warning(
                "Mail is not answering Apple events; tending backs off to "
                "%.0f s between passes",
                self._wait_s,
            )

    def _recover(self) -> None:
        """A pass just succeeded: resume the plain interval, and say so
        if it had been backed off."""
        if self._wait_s != self.interval_s:
            logger.info(
                "a tending pass succeeded; back-off ends and tending "
                "resumes its %.0f s interval",
                self.interval_s,
            )
        self._wait_s = self.interval_s

    def _reset_wait(self) -> None:
        """A pass just failed some other way: a timeout is the only
        failure that means Mail itself is not answering, so only a
        timeout earns back-off. Quiet, since the exception is already
        logged above."""
        self._wait_s = self.interval_s


def start_tender(interval_s: float = TEND_INTERVAL_S) -> ComposeTender:
    """Start tending for this process: the thread, and the server
    connector's call to it when a composition leaves its window open."""
    from . import server

    tender = ComposeTender(interval_s=interval_s)
    server.mail.on_window_left_open = tender.request_pass
    tender.start()
    logger.info("tending Mail's compose windows every %.0f s", interval_s)
    return tender
