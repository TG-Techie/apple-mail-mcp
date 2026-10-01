"""The connector's side of tending Mail's compose windows: every window a
composition opens is recorded in the compose ledger with how it ended,
and a tending pass reads the windows and closes, by Mail's id for each,
the ones the ledger says are the connector's and abandoned, and any whose
content has not changed for the stale period
(docs/research/compose-window-tending.md). Mail is answered script by
script, as in test_mail_connector.py."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.compose_clock import ComposeClock
from apple_mail_mcp.compose_ledger import (
    Closed,
    ComposeLedger,
    LeftOpen,
    Open,
    WindowRecord,
)
from apple_mail_mcp.compose_tending import RECORD_RETENTION_S, TEND_GRACE_S, SightingClock
from apple_mail_mcp.exceptions import (
    MailAppleScriptError,
    MailDraftNotSettledError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
    MailTimeoutError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector, _WindowSnapshot

from .test_mail_connector import _compose_outcomes, _scripted

PID = 77701
WINDOW_ID = 2900


@pytest.fixture
def ledger(tmp_path: Path) -> ComposeLedger:
    return ComposeLedger(tmp_path / "ledger")


@pytest.fixture
def clock(tmp_path: Path) -> ComposeClock:
    return ComposeClock(tmp_path / "clock" / "compose_clock.json")


@pytest.fixture
def connector(ledger: ComposeLedger, clock: ComposeClock) -> AppleMailConnector:
    connector = AppleMailConnector(timeout=30, compose_ledger=ledger, compose_clock=clock)
    # The read of Mail's windows a composition takes before opening its
    # own; TestAWindowTheOpenScriptDidNotReport tests what it is for.
    connector._mail_window_snapshot = lambda: _WindowSnapshot(PID, frozenset())  # type: ignore[method-assign]
    return connector


@pytest.fixture
def woken(connector: AppleMailConnector) -> list[int]:
    calls: list[int] = []
    connector.on_window_left_open = lambda: calls.append(1)
    return calls


def _meta(window: str, **extra: Any) -> str:
    report: dict[str, Any] = {
        "window": window,
        "window_id": WINDOW_ID,
        "mail_pid": PID,
        "failure": "",
        "subject": window,
        "to": ["a@example.com"],
        "cc": [],
        "bcc": [],
        "before_ids": [5, 6],
    }
    report.update(extra)
    return json.dumps(report)


def _only_record(ledger: ComposeLedger) -> WindowRecord:
    records = ledger.read_all().records
    assert len(records) == 1, records
    return records[0]


def _save(connector: AppleMailConnector, outcomes: list[str]) -> list[str]:
    captured = _scripted(connector, outcomes)
    connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="hello")
    return captured


def _send(connector: AppleMailConnector, outcomes: list[str], **kw: Any) -> list[str]:
    captured = _scripted(connector, outcomes)
    connector._send_html_email(
        to=kw.get("to", ["a@example.com"]), cc=None, bcc=None, subject="hi",
        body="<p>Hi there probe</p>", from_account=None,
    )
    return captured


class TestEveryWindowIsRecordedWithHowItEnded:
    def test_a_saved_draft(self, connector: AppleMailConnector, ledger: ComposeLedger) -> None:
        outcomes = _compose_outcomes(
            "hello", window="hi", plain=True, send=False, draft_id="161055"
        )
        _save(connector, [_meta("hi")] + outcomes[1:])
        record = _only_record(ledger)
        assert (record.window_name, record.window_id, record.mail_pid) == ("hi", WINDOW_ID, PID)
        assert (record.operation, record.seed) == ("save", "new")
        assert isinstance(record.state, Closed)
        assert (record.state.how, record.state.by, record.state.draft_id) == (
            "saved", "composition", "161055",
        )

    def test_a_sent_message(self, connector: AppleMailConnector, ledger: ComposeLedger) -> None:
        outcomes = _compose_outcomes("<p>Hi there probe</p>", window="hi")
        _send(connector, [_meta("hi")] + outcomes[1:])
        record = _only_record(ledger)
        assert record.operation == "send"
        assert isinstance(record.state, Closed) and record.state.how == "sent"

    def test_a_failure_whose_window_was_salvaged(
        self, connector: AppleMailConnector, ledger: ComposeLedger, woken: list[int]
    ) -> None:
        with pytest.raises(MailAppleScriptError, match="NO_BODY_AREA"):
            _send(connector, [_meta("hi"), "NO_BODY_AREA:x", "SALVAGED"])
        state = _only_record(ledger).state
        assert isinstance(state, Closed)
        assert (state.how, state.by) == ("salvaged", "composition")
        assert woken == []

    def test_a_failure_whose_salvage_failed_is_left_open_and_wakes_tending(
        self, connector: AppleMailConnector, ledger: ComposeLedger, woken: list[int]
    ) -> None:
        with pytest.raises(MailAppleScriptError, match="NO_BODY_AREA"):
            _send(
                connector,
                [_meta("hi"), "NO_BODY_AREA:x", "SALVAGE_FAILED:window still open"],
            )
        state = _only_record(ledger).state
        assert isinstance(state, LeftOpen)
        assert "NO_BODY_AREA" in state.failure
        assert "SALVAGE_FAILED:window still open" in state.failure
        assert woken == [1]

    def test_a_window_whose_salvage_found_it_gone(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        outcomes = _compose_outcomes("<p>Hi there probe</p>", window="hi")
        until_send = outcomes[1:outcomes.index("SENT")]
        with pytest.raises(MailAppleScriptError, match="WINDOW_STILL_OPEN"):
            _send(
                connector,
                [_meta("hi"), *until_send, "WINDOW_STILL_OPEN:x", "NO_WINDOW"],
            )
        state = _only_record(ledger).state
        assert isinstance(state, Closed) and state.how == "gone"

    @pytest.mark.parametrize(
        "discard, ending",
        [("DISCARDED", "discarded"), ("DISCARD_FAILED:Probe", "left_open")],
    )
    def test_a_window_refused_at_the_allowlist(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        discard: str,
        ending: str,
    ) -> None:
        meta = _meta("Fwd: Probe", to=["evil@other.com"], before_ids=[])
        captured = _scripted(connector, [meta, discard])
        with pytest.raises(MailOutboundDisallowedError):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"], send_now=True,
            )
        assert len(captured) == 2 and "Don’t Save" in captured[1]
        state = _only_record(ledger).state
        if ending == "discarded":
            assert isinstance(state, Closed) and state.how == "discarded"
        else:
            assert isinstance(state, LeftOpen)
            assert "evil@other.com" in state.failure

    def test_a_failure_nothing_closed(
        self, connector: AppleMailConnector, ledger: ComposeLedger, woken: list[int]
    ) -> None:
        """Mail stopped answering mid-composition: nothing tried to close
        the window, so it is recorded open with the error."""
        answers = iter([_meta("hi")])

        def fake_run(script: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise MailAppleScriptError("Script execution timeout after 30s") from None

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="timeout"):
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        state = _only_record(ledger).state
        assert isinstance(state, LeftOpen)
        assert state.failure.startswith("MailAppleScriptError: Script execution timeout")
        assert woken == [1]

    def test_a_draft_that_does_not_settle_was_closed_with_save(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        outcomes = _compose_outcomes("hello", window="hi", plain=True, send=False)
        connector._DRAFT_APPEAR_POLLS = 1  # type: ignore[misc]
        with pytest.raises(MailDraftNotSettledError):
            _save(connector, [_meta("hi")] + outcomes[1:-1] + [""])
        state = _only_record(ledger).state
        assert isinstance(state, Closed) and state.how == "salvaged"

    def test_a_window_that_opened_but_failed_there(
        self, connector: AppleMailConnector, ledger: ComposeLedger, woken: list[int]
    ) -> None:
        """COMPOSE_WINDOW_NOT_UNIQUE leaves its window open by design; the
        window is recorded, by Mail's id, as left open."""
        failure = "COMPOSE_WINDOW_NOT_UNIQUE: Mail opened a compose window named hi"
        captured = _scripted(
            connector,
            [_meta("hi", failure=failure, subject="", to=[], before_ids=[])],
        )
        with pytest.raises(MailAppleScriptError, match="COMPOSE_WINDOW_NOT_UNIQUE") as exc:
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        assert "(compose window: left open)" in str(exc.value)
        assert len(captured) == 1
        record = _only_record(ledger)
        assert record.window_id == WINDOW_ID
        assert isinstance(record.state, LeftOpen) and record.state.failure == failure
        assert woken == [1]

    def test_a_window_mail_gave_no_id(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        outcomes = _compose_outcomes("hello", window="hi", plain=True, send=False)
        _save(connector, [_meta("hi", window_id=0, mail_pid=0)] + outcomes[1:])
        record = _only_record(ledger)
        assert (record.window_id, record.mail_pid) == (None, None)

    def test_a_report_from_before_the_ledger_still_composes(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        """No window id, no pid, no failure key: the window is recorded
        without identity, and the composition goes on."""
        outcomes = _compose_outcomes("hello", window="hi", plain=True, send=False)
        _save(connector, outcomes)
        assert _only_record(ledger).window_id is None

    def test_a_ledger_that_cannot_be_written_does_not_fail_the_mail(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("x")
        connector = AppleMailConnector(
            timeout=30, compose_ledger=ComposeLedger(blocked / "ledger")
        )
        outcomes = _compose_outcomes("hello", window="hi", plain=True, send=False)
        captured = _scripted(connector, [_meta("hi")] + outcomes[1:])
        result = connector.create_draft(
            seed="new", to=["a@example.com"], subject="hi", body="hello"
        )
        assert result["draft_id"] == "7"
        assert len(captured) == len(outcomes)
        assert "could not record window 'hi'" in caplog.text


class TestTheOpenScriptIdentifiesItsWindow:
    @pytest.fixture
    def script(self, connector: AppleMailConnector) -> str:
        return connector._build_open_compose_script(
            seed="new", seed_id=None, reply_all=False, to=["a@example.com"],
            cc=None, bcc=None, subject="Hi", sender=None, operation="save",
        )

    def test_by_mails_id_for_it_and_mails_process(self, script: str) -> None:
        before_at = script.index("set beforeWindowIds to id of every window")
        assert before_at < script.index("make new outgoing message")
        assert "set mailPid to unix id" in script
        assert "id of every window whose name is newName" in script
        assert "is not in beforeWindowIds" in script
        assert "|window_id|:newWindowId" in script
        assert "|mail_pid|:mailPid" in script

    def test_a_failure_once_the_window_exists_is_reported_not_raised(
        self, script: str
    ) -> None:
        assert 'if newName is "" then error errMsg number errNum' in script
        assert "|failure|:failure" in script
        # The headers and the read-back run only when nothing failed.
        assert script.index('if failure is "" then') < script.index(
            "address of to recipients of theMessage"
        )


class TestAWindowTheOpenScriptDidNotReport:
    """Mail can open the window after the opening script stopped looking
    (NO_COMPOSE_WINDOW at 5 s), after it was stopped at the timeout, or
    with a report that cannot be read. Mail's windows are read before the
    script, and on such a failure each compose window that opened since
    is recorded as left open, for tending to close at once."""

    BEFORE = _WindowSnapshot(mail_pid=PID, window_ids=frozenset({100, 101}))

    @pytest.fixture(autouse=True)
    def snapshot(self, connector: AppleMailConnector) -> None:
        connector._mail_window_snapshot = lambda: self.BEFORE  # type: ignore[method-assign]

    def _fail_open(
        self, connector: AppleMailConnector, opening: Exception | str, found: list[dict[str, Any]]
    ) -> list[str]:
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            if len(scripts) == 1:
                if isinstance(opening, Exception):
                    raise opening
                return opening
            return json.dumps({"same_pid": True, "found": found})

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        return scripts

    @pytest.mark.parametrize(
        "opening, raised",
        [
            (MailAppleScriptError("execution error: NO_COMPOSE_WINDOW: none in 5 s (-2700)"),
             MailAppleScriptError),
            (MailTimeoutError("Script execution timeout after 30s"), MailTimeoutError),
            ("not json at all", MailAppleScriptError),
        ],
    )
    def test_is_recorded_left_open_and_wakes_tending(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        woken: list[int],
        opening: Exception | str,
        raised: type[Exception],
    ) -> None:
        scripts = self._fail_open(connector, opening, [{"id": 102, "name": "hi"}])
        with pytest.raises(raised) as exc:
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        assert type(exc.value) is raised
        assert "(compose window: 1 left open, recorded for tending)" in str(exc.value)
        assert len(scripts) == 2
        record = _only_record(ledger)
        assert (record.window_name, record.window_id, record.mail_pid) == ("hi", 102, PID)
        assert isinstance(record.state, LeftOpen)
        assert woken == [1]

    def test_the_look_is_for_new_compose_windows_of_the_subject(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = self._fail_open(connector, MailAppleScriptError("NO_COMPOSE_WINDOW"), [])
        with pytest.raises(MailAppleScriptError, match="none found open"):
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        look = scripts[1]
        assert "set beforeIds to {100, 101}" in look
        assert 'set expectedName to "hi"' in look
        assert f"if pidNow is not {PID} then" in look
        assert "my tendIsCompose(idx)" in look
        assert "click" not in look.split("on tendIsBlank")[0]

    def test_a_reply_looks_for_any_new_compose_window(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = self._fail_open(connector, MailAppleScriptError("NO_COMPOSE_WINDOW"), [])
        with pytest.raises(MailAppleScriptError):
            connector.create_draft(seed="reply", seed_id="160989", body="x")
        assert 'set expectedName to ""' in scripts[1]

    def test_a_window_another_composition_holds_is_left_to_it(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        theirs = ledger.open(
            window_name="hi", window_id=102, mail_pid=PID, operation="send", seed="new"
        )
        self._fail_open(connector, MailAppleScriptError("NO_COMPOSE_WINDOW"), [{"id": 102, "name": "hi"}])
        with pytest.raises(MailAppleScriptError, match="none found open"):
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        assert ledger.read_all().records == (theirs,)

    def test_a_seed_that_is_gone_opened_nothing(self, connector: AppleMailConnector) -> None:
        scripts = self._fail_open(connector, MailAppleScriptError("SEED_NOT_FOUND"), [])
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(seed="reply", seed_id="160989", body="x")
        assert len(scripts) == 1

    def test_a_look_that_fails_is_logged_and_the_error_stands(
        self, connector: AppleMailConnector, ledger: ComposeLedger, caplog: pytest.LogCaptureFixture
    ) -> None:
        def fake_run(script: str) -> str:
            raise MailAppleScriptError("NO_COMPOSE_WINDOW")

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="none found open"):
            connector.create_draft(seed="new", to=["a@example.com"], subject="hi", body="x")
        assert "could not look for a compose window left unreported" in caplog.text
        assert ledger.read_all().records == ()

    def test_the_snapshot_reads_mails_process_and_window_ids(self, tmp_path: Path) -> None:
        connector = AppleMailConnector(timeout=30, compose_ledger=ComposeLedger(tmp_path / "l"))
        captured = _scripted(connector, [json.dumps({"pid": PID, "ids": [5, 6]})])
        del connector._mail_window_snapshot  # the real one, not _scripted's answer
        assert connector._mail_window_snapshot() == _WindowSnapshot(PID, frozenset({5, 6}))
        assert "id of every window" in captured[0] and "unix id" in captured[0]
        assert "click" not in captured[0] and "make new" not in captured[0]


def _mail(window_id: int, name: str, x: int, *, visible: bool = True) -> dict[str, Any]:
    return {"id": window_id, "name": name, "bounds": [x, 100, x + 800, 900], "visible": visible}


def _window(name: str, x: int, *, empty: bool = False, text: str = "Hello there") -> dict[str, Any]:
    return {
        "name": name,
        "position": [x, 100],
        "fields": ["", "", ""] if empty else ["￼", "", name],
        "body": "empty" if empty else "content",
        "body_roles": [[]] if empty else [["AXStaticText"]],
        "body_values": [[]] if empty else [[text]],
        "sheet": False,
        "minimized": False,
    }


def _inventory(pairs: list[tuple[dict[str, Any], dict[str, Any]]], **extra_mail: Any) -> str:
    """An inventory report of compose windows, each with the Mail window
    it is listed as."""
    return json.dumps(
        {
            "running": True,
            "pid": PID,
            "compose": [w for w, _ in pairs],
            "mail_windows": [m for _, m in pairs] + list(extra_mail.values()),
        }
    )


def _abandoned(ledger: ComposeLedger, name: str, window_id: int = WINDOW_ID) -> WindowRecord:
    return ledger.open(
        window_name=name, window_id=window_id, mail_pid=PID, operation="send",
        seed="new", now=time.time() - TEND_GRACE_S - 5,
    )


RESTORED = [
    (_window("New Message", 10 * i, empty=True), _mail(2756 + i, "New Message", 10 * i))
    for i in range(3)
] + [(_window("Re: alert", 500), _mail(2759, "Re: alert", 500))]
MINE = (_window("Mine", 600), _mail(WINDOW_ID, "Mine", 600))


class TestATendingPass:
    def test_a_dry_run_reads_and_decides_and_touches_nothing(
        self, connector: AppleMailConnector, ledger: ComposeLedger, clock: ComposeClock
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        captured = _scripted(connector, [_inventory([*RESTORED, MINE])])
        report = connector.tend_compose_windows(dry_run=True)
        assert len(captured) == 1
        assert report.to_close == (("Mine", "salvage", "abandoned"),)
        assert report.closed == ()
        assert ledger.get(mine.record_id) == mine
        assert report.as_dict()["left"] == {"not_yet_stale": 4}
        assert not clock.path.exists()

    def test_a_first_real_pass_starts_every_clock_and_closes_no_stranger(
        self, connector: AppleMailConnector, clock: ComposeClock
    ) -> None:
        captured = _scripted(connector, [_inventory(RESTORED)])
        report = connector.tend_compose_windows()
        assert len(captured) == 1
        assert report.closed == () and report.to_close == ()
        assert set(clock.load().windows) == {2756, 2757, 2758, 2759}

    def test_a_window_unchanged_for_the_period_is_closed_by_its_id(
        self, connector: AppleMailConnector, clock: ComposeClock
    ) -> None:
        """Two windows of one name: the one whose clock ran out is closed,
        by Mail's id, and its sibling is not."""
        _scripted(connector, [_inventory(RESTORED)])
        connector.tend_compose_windows()
        saved = clock.load()
        clock.save(
            SightingClock(
                mail_pid=PID,
                windows={**saved.windows, 2757: replace(saved.windows[2757], since=0.0)},
            )
        )
        captured = _scripted(connector, [_inventory(RESTORED), "DISCARDED"])
        report = connector.tend_compose_windows()
        assert len(captured) == 2
        close = captured[1]
        assert close.startswith('set closeId to 2757\nset closeName to "New Message"\n')
        # Discarded only while it still reads empty, checked before the click.
        assert close.index("set stillEmpty") < close.index("my tendClickClose(closeIdx)")
        assert "Don’t Save" in close
        assert report.closed == (("New Message", "discarded", "stale"),)
        assert report.as_dict()["closed_counts"] == {"stale_discarded": 1}
        assert report.as_dict()["left"] == {"not_yet_stale": 3}

    def test_a_stale_window_with_content_is_salvaged(
        self, connector: AppleMailConnector, clock: ComposeClock
    ) -> None:
        _scripted(connector, [_inventory(RESTORED)])
        connector.tend_compose_windows()
        saved = clock.load()
        clock.save(
            SightingClock(
                mail_pid=PID,
                windows={**saved.windows, 2759: replace(saved.windows[2759], since=0.0)},
            )
        )
        captured = _scripted(connector, [_inventory(RESTORED), "SALVAGED"])
        report = connector.tend_compose_windows()
        assert captured[1].startswith('set closeId to 2759\nset closeName to "Re: alert"\n')
        assert 'click button "Save" of sheet 1 of window closeIdx' in captured[1]
        assert "set stillEmpty" not in captured[1]
        assert report.closed == (("Re: alert", "salvaged", "stale"),)

    def test_an_edit_between_passes_restarts_the_clock(
        self, connector: AppleMailConnector, clock: ComposeClock
    ) -> None:
        _scripted(connector, [_inventory(RESTORED)])
        connector.tend_compose_windows()
        saved = clock.load()
        clock.save(SightingClock(PID, {k: replace(v, since=0.0) for k, v in saved.windows.items()}))
        edited = [
            *RESTORED[:3],
            (_window("Re: alert", 500, text="Hello there, edited"), RESTORED[3][1]),
        ]
        captured = _scripted(connector, [_inventory(edited), "DISCARDED"])
        before = time.time()
        report = connector.tend_compose_windows()
        assert [c.split("\n")[0] for c in captured[1:]] == [
            "set closeId to 2756", "set closeId to 2757", "set closeId to 2758",
        ]
        assert report.as_dict()["left"] == {"not_yet_stale": 1}
        assert clock.load().windows[2759].since >= before

    def test_an_abandoned_window_is_salvaged_by_its_id_and_recorded(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        captured = _scripted(connector, [_inventory([*RESTORED, MINE]), "SALVAGED"])
        report = connector.tend_compose_windows()
        assert len(captured) == 2
        assert captured[1].startswith(f'set closeId to {WINDOW_ID}\nset closeName to "Mine"\n')
        assert report.closed == (("Mine", "salvaged", "abandoned"),)
        state = ledger.get(mine.record_id).state  # type: ignore[union-attr]
        assert isinstance(state, Closed) and (state.how, state.by) == ("salvaged", "tending")

    def test_an_abandoned_window_that_shares_its_name_is_still_closed(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        mine = _abandoned(ledger, "Re: alert", window_id=2800)
        twin = (_window("Re: alert", 700), _mail(2800, "Re: alert", 700))
        captured = _scripted(connector, [_inventory([*RESTORED, twin]), "SALVAGED"])
        report = connector.tend_compose_windows()
        assert captured[1].startswith('set closeId to 2800\n')
        assert report.closed == (("Re: alert", "salvaged", "abandoned"),)
        assert isinstance(ledger.get(mine.record_id).state, Closed)  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "outcome",
        ["SALVAGE_FAILED:window still open", "SALVAGE_FAILED:Mail's window 2900 is now named x"],
    )
    def test_a_close_that_did_not_happen_leaves_the_record(
        self, connector: AppleMailConnector, ledger: ComposeLedger, outcome: str
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        _scripted(connector, [_inventory([MINE]), outcome])
        report = connector.tend_compose_windows()
        assert report.failed == (("Mine", outcome),)
        assert ledger.get(mine.record_id) == mine

    def test_mail_not_answering_stops_the_pass(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        _abandoned(ledger, "One", window_id=2900)
        _abandoned(ledger, "Two", window_id=2901)
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            if len(scripts) == 1:
                return _inventory(
                    [(_window("One", 1), _mail(2900, "One", 1)),
                     (_window("Two", 2), _mail(2901, "Two", 2))]
                )
            raise MailAppleScriptError("Script execution timeout after 30s")

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        report = connector.tend_compose_windows()
        assert len(scripts) == 2
        assert len(report.failed) == 1 and report.not_attempted == ("Two",)
        assert report.closed == ()

    def test_records_whose_window_is_gone_end_and_old_ones_go(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        lost = ledger.open(
            window_name="Old", window_id=2000, mail_pid=11111, operation="save",
            seed="new", now=time.time() - TEND_GRACE_S - 5,
        )
        ancient = ledger.open(
            window_name="Ancient", window_id=1000, mail_pid=11111, operation="save",
            seed="new", now=0.0,
        )
        ledger.end(
            ancient.record_id,
            Closed(how="sent", by="composition", at=time.time() - RECORD_RETENTION_S - 5),
        )
        _scripted(connector, [_inventory([])])
        report = connector.tend_compose_windows()
        assert report.records_gone == 1 and report.records_pruned == 1
        state = ledger.get(lost.record_id).state  # type: ignore[union-attr]
        assert isinstance(state, Closed) and (state.how, state.by) == ("gone", "tending")
        assert ledger.get(ancient.record_id) is None

    def test_a_composition_in_flight_is_left_alone(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        running = ledger.open(
            window_name="Mine", window_id=WINDOW_ID, mail_pid=PID, operation="send",
            seed="new",
        )
        captured = _scripted(connector, [_inventory([MINE])])
        report = connector.tend_compose_windows(stale_s=0)
        assert len(captured) == 1
        assert report.as_dict()["left"] == {"in_flight": 1}
        assert ledger.get(running.record_id).state == Open()  # type: ignore[union-attr]

    def test_mail_not_running_is_reported_and_nothing_else_asked(
        self, connector: AppleMailConnector
    ) -> None:
        captured = _scripted(connector, [json.dumps({"running": False})])
        report = connector.tend_compose_windows()
        assert not report.mail_running
        assert len(captured) == 1

    def test_a_clock_that_cannot_be_saved_closes_nothing_new(
        self, tmp_path: Path, ledger: ComposeLedger, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("x")
        connector = AppleMailConnector(
            timeout=30, compose_ledger=ledger, compose_clock=ComposeClock(blocked / "c.json")
        )
        _scripted(connector, [_inventory(RESTORED)])
        report = connector.tend_compose_windows(stale_s=0)
        _scripted(connector, [_inventory(RESTORED)])
        again = connector.tend_compose_windows(stale_s=0)
        assert report.closed == () and again.closed == ()
        assert "compose clock: could not save" in caplog.text


class TestTheInventoryScript:
    @pytest.fixture
    def script(self, connector: AppleMailConnector) -> str:
        return connector._build_compose_inventory_script()

    def test_asks_nothing_of_a_mail_that_is_not_running(self, script: str) -> None:
        running_at = script.index('if not (application "Mail" is running) then')
        assert running_at < script.index('tell application "Mail"')
        assert running_at < script.index('tell application "System Events"')

    def test_addresses_each_window_by_its_index_alone(self, script: str) -> None:
        """Held in a variable, a System Events window becomes a reference
        by name, and reads the first window of that name: the 18 "New
        Message" windows all read as one (Observation 7)."""
        assert "set w to window" not in script
        assert "repeat with w in windows" not in script
        assert "set winPos to position of window i" in script
        assert "of scroll area 1 of group 1 of group 1 of window i" in script
        assert "my tendFieldValues(i)" in script and "my tendBodyRead(i)" in script

    def test_reads_the_whole_body_level_by_level(self, script: str) -> None:
        deepest = "UI elements of " * 8 + "UI element k of scroll area 1"
        assert f"set r to role of {deepest}" in script
        assert "UI elements of " * 9 not in script
        assert "return my tendBodyResult(levelRoles, levelValues, false)" in script

    def test_reads_mails_ids_and_places_in_one_event(self, script: str) -> None:
        assert "properties of every window" in script
        assert "|bounds|:(bounds of props)" in script
        assert "|visible|:(visible of props)" in script

    def test_never_clicks(self, script: str) -> None:
        assert "click" not in script.split("on tendClickClose")[0]
        assert "keystroke" not in script

    def test_is_parsed_as_json_with_its_handlers_after_the_code(self, script: str) -> None:
        assert script.index("NSJSONSerialization") < script.index("on tendBodyRead")
