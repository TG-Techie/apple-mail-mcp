"""The daemon's tending thread and the logged pass it runs
(src/apple_mail_mcp/tender.py). Nothing here reaches Mail: the pass is
replaced, or the connector's is."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import server, tender
from apple_mail_mcp.compose_tending import LeftWindow, TendReport
from apple_mail_mcp.exceptions import MailAppleScriptError, MailTimeoutError
from apple_mail_mcp.security import TIER_LIMITS, operation_logger


def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class TestTheTender:
    def test_runs_a_pass_at_once_then_on_its_interval(self) -> None:
        passes: list[float] = []
        t = tender.ComposeTender(
            interval_s=0.1, min_gap_s=0, run_pass=lambda: passes.append(time.monotonic())
        )
        t.start()
        try:
            _until(lambda: len(passes) >= 3)
        finally:
            t.stop()
        assert passes[1] - passes[0] >= 0.09

    def test_a_request_brings_the_next_pass_forward(self) -> None:
        passes: list[int] = []
        t = tender.ComposeTender(interval_s=3600, min_gap_s=0, run_pass=lambda: passes.append(1))
        t.start()
        try:
            _until(lambda: len(passes) == 1)
            t.request_pass()
            _until(lambda: len(passes) == 2)
        finally:
            t.stop()

    def test_requests_wait_out_the_gap_and_are_one_pass(self) -> None:
        passes: list[float] = []
        t = tender.ComposeTender(
            interval_s=3600, min_gap_s=0.3, run_pass=lambda: passes.append(time.monotonic())
        )
        t.start()
        try:
            _until(lambda: len(passes) == 1)
            for _ in range(5):
                t.request_pass()
            _until(lambda: len(passes) == 2)
            time.sleep(0.4)
        finally:
            t.stop()
        assert len(passes) == 2
        assert passes[1] - passes[0] >= 0.29

    def test_a_request_made_during_a_pass_is_not_lost(self) -> None:
        in_pass = threading.Event()
        release = threading.Event()
        passes: list[int] = []

        def slow_pass() -> None:
            passes.append(1)
            if len(passes) == 1:
                in_pass.set()
                release.wait(5)

        t = tender.ComposeTender(interval_s=3600, min_gap_s=0, run_pass=slow_pass)
        t.start()
        try:
            in_pass.wait(5)
            t.request_pass()
            release.set()
            _until(lambda: len(passes) == 2)
        finally:
            t.stop()

    def test_a_pass_that_raises_is_logged_and_the_thread_goes_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[int] = []

        def failing() -> None:
            calls.append(1)
            raise MailAppleScriptError("Mail got an error: AppleEvent timed out. (-1712)")

        t = tender.ComposeTender(interval_s=0.05, min_gap_s=0, run_pass=failing)
        t.start()
        try:
            _until(lambda: len(calls) >= 2)
        finally:
            t.stop()
        assert "compose-window tending pass failed" in caplog.text

    def test_an_interval_that_is_not_positive_is_refused(self) -> None:
        with pytest.raises(ValueError):
            tender.ComposeTender(interval_s=0)


class TestBackoffOnATimeout:
    """None of these wait on a real interval: ``request_pass`` cuts any
    wait short regardless of how long it is, so ``interval_s`` can be
    large and every pass still runs as soon as the last one is seen.
    Each fake ``run_pass`` records ``t._wait_s`` the instant it is
    called — the wait the loop just used to schedule this pass — so the
    assertions are on that exact, deterministic sequence rather than on
    wall-clock gaps between passes."""

    def test_the_wait_sequence_through_backoff_and_recovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tender, "TEND_BACKOFF_CAP_S", 350)
        samples: list[float] = []
        outcomes = iter(
            [MailTimeoutError("osascript timed out")] * 3 + [None, None]
        )

        def flaky() -> None:
            samples.append(t._wait_s)
            exc = next(outcomes)
            if exc is not None:
                raise exc

        t = tender.ComposeTender(interval_s=100, min_gap_s=0, run_pass=flaky)
        t.start()
        try:
            _until(lambda: len(samples) == 1)
            for expected in range(2, 6):
                t.request_pass()
                _until(lambda expected=expected: len(samples) == expected)
        finally:
            t.stop()
        # 100 (initial) -> 200, 350 (doubling, then capped) over the three
        # timeouts -> 350 still (the failed fourth doubling is capped) ->
        # 100 (the success resets it, held for the next pass).
        assert samples == [100, 200, 350, 350, 100]

    def test_a_non_timeout_failure_resets_the_wait(self) -> None:
        samples: list[float] = []
        outcomes = iter(
            [
                MailTimeoutError("osascript timed out"),
                MailAppleScriptError("Mail got an error: -1708"),
                None,
            ]
        )

        def flaky() -> None:
            samples.append(t._wait_s)
            exc = next(outcomes)
            if exc is not None:
                raise exc

        t = tender.ComposeTender(interval_s=100, min_gap_s=0, run_pass=flaky)
        t.start()
        try:
            _until(lambda: len(samples) == 1)
            for expected in (2, 3):
                t.request_pass()
                _until(lambda expected=expected: len(samples) == expected)
        finally:
            t.stop()
        # 100 (initial) -> 200 (backed off by the timeout) -> 100 (a
        # different failure is not more timeouts, so it resets, held for
        # the next pass).
        assert samples == [100, 200, 100]

    def test_a_request_cuts_a_backed_off_wait_short(self) -> None:
        passes: list[int] = []
        calls = {"n": 0}

        def flaky() -> None:
            calls["n"] += 1
            passes.append(1)
            if calls["n"] == 1:
                raise MailTimeoutError("osascript timed out")

        t = tender.ComposeTender(interval_s=3600, min_gap_s=0, run_pass=flaky)
        t.start()
        try:
            _until(lambda: len(passes) == 1)
            t.request_pass()
            _until(lambda: len(passes) == 2)
        finally:
            t.stop()

    def test_backoff_start_and_growth_and_recovery_are_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO", logger="apple_mail_mcp.tender")
        samples: list[int] = []
        outcomes = iter(
            [
                MailTimeoutError("osascript timed out"),
                MailTimeoutError("osascript timed out"),
                None,
            ]
        )

        def flaky() -> None:
            samples.append(1)
            exc = next(outcomes)
            if exc is not None:
                raise exc

        t = tender.ComposeTender(interval_s=100, min_gap_s=0, run_pass=flaky)
        t.start()
        try:
            _until(lambda: len(samples) == 1)
            for expected in (2, 3):
                t.request_pass()
                _until(lambda expected=expected: len(samples) == expected)
        finally:
            t.stop()
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        infos = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert len(warnings) == 2  # once when back-off starts, once when it grows
        assert any("back" in m.lower() for m in infos)  # recovery after back-off


class TestStartingIt:
    def test_wires_the_connector_to_ask_for_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = MagicMock()
        monkeypatch.setattr(server, "mail", connector)
        started: list[tender.ComposeTender] = []
        monkeypatch.setattr(tender.ComposeTender, "start", lambda self: started.append(self))
        t = tender.start_tender(interval_s=42)
        assert started == [t] and t.interval_s == 42
        assert connector.on_window_left_open == t.request_pass


class TestThePass:
    @pytest.fixture
    def connector(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        connector = MagicMock()
        monkeypatch.setattr(server, "mail", connector)
        return connector

    @pytest.fixture
    def logged(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
        entries: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            operation_logger, "log_operation", lambda *a: entries.append(a)
        )
        return entries

    def test_logs_what_it_found_closed_and_left(
        self, connector: MagicMock, logged: list[tuple[Any, ...]]
    ) -> None:
        connector.tend_compose_windows.return_value = TendReport(
            dry_run=False, mail_running=True, compose_windows=26,
            closed=(("Mine", "salvaged", "abandoned"), ("New Message", "discarded", "stale")),
            left=(LeftWindow("New Message", "not_yet_stale", remaining_s=600.0),) * 17
            + (LeftWindow("Re: x", "in_flight"),) * 7,
        )
        out = tender.run_tend_pass()
        connector.tend_compose_windows.assert_called_once_with(dry_run=False)
        assert logged == [("tend_compose_windows", out, "success")]
        assert out["closed"] == [
            {"name": "Mine", "how": "salvaged", "rule": "abandoned"},
            {"name": "New Message", "how": "discarded", "rule": "stale"},
        ]
        assert out["closed_counts"] == {"abandoned_salvaged": 1, "stale_discarded": 1}
        assert out["left"] == {"in_flight": 7, "not_yet_stale": 17}
        assert out["not_yet_stale_minutes"] == [10] * 17

    def test_a_close_that_failed_is_logged_as_a_failure(
        self, connector: MagicMock, logged: list[tuple[Any, ...]]
    ) -> None:
        connector.tend_compose_windows.return_value = TendReport(
            dry_run=False, mail_running=True, failed=(("Mine", "SALVAGE_FAILED:x"),),
        )
        tender.run_tend_pass()
        assert logged[0][2] == "failure"

    def test_a_pass_that_raises_is_logged_and_raised(
        self, connector: MagicMock, logged: list[tuple[Any, ...]]
    ) -> None:
        connector.tend_compose_windows.side_effect = MailAppleScriptError("-1712")
        with pytest.raises(MailAppleScriptError):
            tender.run_tend_pass(dry_run=True)
        assert logged == [
            (
                "tend_compose_windows",
                {"dry_run": True, "error": "MailAppleScriptError: -1712"},
                "failure",
            )
        ]

    def test_is_rate_limited_with_the_other_mutations(
        self, connector: MagicMock, logged: list[tuple[Any, ...]]
    ) -> None:
        connector.tend_compose_windows.return_value = TendReport(
            dry_run=False, mail_running=True
        )
        limit, _ = TIER_LIMITS["expensive_ops"]
        for _ in range(limit):
            tender.run_tend_pass()
        refused = tender.run_tend_pass()
        assert refused["error_type"] == "rate_limited"
        assert connector.tend_compose_windows.call_count == limit
