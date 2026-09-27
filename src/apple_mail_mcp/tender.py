"""Tending Mail's compose windows from the resident daemon.

Mail accumulated compose windows over days — failed sends, failed
salvages, compositions whose process died — and restored all of them
at a relaunch (25 on 2026-09-27). The operator's direction that morning:
"the mail app regularly has been accumulating failed windows so the MCP
should periodically tend it reguardless". A pass
(``AppleMailConnector.tend_compose_windows``) closes only the windows
the compose ledger says this connector opened and nothing closed, and
leaves and counts everything else (docs/research/compose-window-tending.md).

The daemon (``mail-serve``) runs one pass when it starts, then one every
``TEND_INTERVAL_S``, and one soon after any composition ends with its
window open, but never two within ``TEND_MIN_GAP_S``. The stdio server
does not tend: many of them can run at once, each for one session, and
one resident process is enough; the windows a stdio server leaves are in
the same ledger, where the daemon finds them.

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

from .security import check_rate_limit, operation_logger

logger = logging.getLogger(__name__)

OPERATION = "tend_compose_windows"

TEND_INTERVAL_S = 15 * 60
"""Between the daemon's passes. A pass holds the Mail lock while it
reads the windows, about 6.5 s for 25 of them (2026-09-27), so it is
not run often; a composition that leaves its window open asks for one
at once instead."""

TEND_MIN_GAP_S = 60.0
"""The least time between the starts of two passes, however many
compositions ask."""


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
    logged and the thread goes on."""

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
            except Exception:
                logger.exception("compose-window tending pass failed")
            self.passes += 1
            self._wake.wait(self.interval_s)
            remaining = self.min_gap_s - (time.monotonic() - started)
            if remaining > 0:
                self._stopping.wait(remaining)


def start_tender(interval_s: float = TEND_INTERVAL_S) -> ComposeTender:
    """Start tending for this process: the thread, and the server
    connector's call to it when a composition leaves its window open."""
    from . import server

    tender = ComposeTender(interval_s=interval_s)
    server.mail.on_window_left_open = tender.request_pass
    tender.start()
    logger.info("tending Mail's compose windows every %.0f s", interval_s)
    return tender
