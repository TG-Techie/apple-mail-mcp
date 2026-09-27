"""The connector's side of tending Mail's compose windows: every window a
composition opens is recorded in the compose ledger with how it ended,
and a tending pass reads the windows and closes the ones the ledger says
are the connector's (docs/research/compose-window-tending.md). Mail is
answered script by script, as in test_mail_connector.py."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.compose_ledger import (
    Closed,
    ComposeLedger,
    LeftOpen,
    Open,
    WindowRecord,
)
from apple_mail_mcp.compose_tending import RECORD_RETENTION_S, TEND_GRACE_S
from apple_mail_mcp.exceptions import (
    MailAppleScriptError,
    MailDraftNotSettledError,
    MailOutboundDisallowedError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector

from .test_mail_connector import _compose_outcomes, _scripted

PID = 77701
WINDOW_ID = 2900


@pytest.fixture
def ledger(tmp_path: Path) -> ComposeLedger:
    return ComposeLedger(tmp_path / "ledger")


@pytest.fixture
def connector(ledger: ComposeLedger) -> AppleMailConnector:
    return AppleMailConnector(timeout=30, compose_ledger=ledger)


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
        with pytest.raises(MailAppleScriptError, match="POSTCONDITION_TIMEOUT"):
            _send(
                connector,
                [_meta("hi")] + outcomes[1:-1] + ["POSTCONDITION_TIMEOUT:x", "NO_WINDOW"],
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


def _inventory(windows: list[dict[str, Any]], mail_windows: dict[int, str]) -> str:
    return json.dumps(
        {
            "running": True,
            "pid": PID,
            "compose": windows,
            "mail_windows": [{"id": i, "name": n} for i, n in mail_windows.items()],
        }
    )


def _window(name: str, *, empty: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "fields": ["", "", ""] if empty else ["￼", "", name],
        "body": "empty" if empty else "unread",
        "sheet": False,
        "minimized": False,
    }


def _abandoned(ledger: ComposeLedger, name: str, window_id: int = WINDOW_ID) -> WindowRecord:
    return ledger.open(
        window_name=name, window_id=window_id, mail_pid=PID, operation="send",
        seed="new", now=time.time() - TEND_GRACE_S - 5,
    )


RESTORED = [_window("New Message", empty=True) for _ in range(3)] + [_window("Re: alert")]
RESTORED_IDS = {2756: "New Message", 2757: "New Message", 2758: "New Message", 2759: "Re: alert"}


class TestATendingPass:
    def test_a_dry_run_reads_and_decides_and_touches_nothing(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        captured = _scripted(
            connector,
            [_inventory(RESTORED + [_window("Mine")], {**RESTORED_IDS, WINDOW_ID: "Mine"})],
        )
        report = connector.tend_compose_windows(dry_run=True)
        assert len(captured) == 1
        assert report.to_close == (("Mine", "salvage"),)
        assert report.closed == ()
        assert ledger.get(mine.record_id) == mine
        assert report.as_dict()["left"] == {"unowned": 1, "unowned_empty": 3}

    def test_an_abandoned_window_is_salvaged_and_recorded(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        captured = _scripted(
            connector,
            [
                _inventory(RESTORED + [_window("Mine")], {**RESTORED_IDS, WINDOW_ID: "Mine"}),
                "SALVAGED",
            ],
        )
        report = connector.tend_compose_windows()
        assert len(captured) == 2
        close = captured[1]
        assert 'set tendName to "Mine"' in close
        assert close.index(f"name of window id {WINDOW_ID}") < close.index("AXCloseButton")
        assert "set salvageOutcome" in close
        assert report.closed == (("Mine", "salvaged"),)
        state = ledger.get(mine.record_id).state  # type: ignore[union-attr]
        assert isinstance(state, Closed) and (state.how, state.by) == ("salvaged", "tending")

    def test_an_empty_one_is_discarded_only_while_it_still_reads_empty(
        self, connector: AppleMailConnector, ledger: ComposeLedger
    ) -> None:
        mine = _abandoned(ledger, "New Message")
        captured = _scripted(
            connector,
            [_inventory([_window("New Message", empty=True)], {WINDOW_ID: "New Message"}),
             "DISCARDED"],
        )
        report = connector.tend_compose_windows()
        close = captured[1]
        assert close.index("tendBodyOf(window tendName") < close.index("Don’t Save")
        assert "no longer empty; left open" in close
        assert report.closed == (("New Message", "discarded"),)
        state = ledger.get(mine.record_id).state  # type: ignore[union-attr]
        assert isinstance(state, Closed) and state.how == "discarded"

    @pytest.mark.parametrize(
        "outcome", ["SALVAGE_FAILED:window still open", "RENAMED:Mine, edited"]
    )
    def test_a_close_that_did_not_happen_leaves_the_record(
        self, connector: AppleMailConnector, ledger: ComposeLedger, outcome: str
    ) -> None:
        mine = _abandoned(ledger, "Mine")
        _scripted(connector, [_inventory([_window("Mine")], {WINDOW_ID: "Mine"}), outcome])
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
                    [_window("One"), _window("Two")], {2900: "One", 2901: "Two"}
                )
            raise MailAppleScriptError("Script execution timeout after 30s")

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        report = connector.tend_compose_windows()
        assert len(scripts) == 2
        assert len(report.failed) == 1 and len(report.not_attempted) == 1
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
        _scripted(connector, [_inventory([], {})])
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
        captured = _scripted(connector, [_inventory([_window("Mine")], {WINDOW_ID: "Mine"})])
        report = connector.tend_compose_windows()
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


class TestTheInventoryScript:
    @pytest.fixture
    def script(self, connector: AppleMailConnector) -> str:
        return connector._build_compose_inventory_script()

    def test_asks_nothing_of_a_mail_that_is_not_running(self, script: str) -> None:
        running_at = script.index('if not (application "Mail" is running) then')
        assert running_at < script.index('tell application "Mail"')
        assert running_at < script.index('tell application "System Events"')

    def test_addresses_each_window_by_index(self, script: str) -> None:
        """Through a nested ``every`` reference the body lookup failed on
        7 windows of 25; by index it read all of them (Observation 5)."""
        assert "set w to window i" in script
        assert "repeat with w in windows" not in script
        assert "scroll area 1 of group 1 of group 1 of w" in script

    def test_reads_mails_ids_in_one_event(self, script: str) -> None:
        assert "properties of every window" in script

    def test_never_clicks(self, script: str) -> None:
        assert "click" not in script
        assert "keystroke" not in script

    def test_is_parsed_as_json_with_its_handlers_after_the_code(self, script: str) -> None:
        assert script.index("NSJSONSerialization") < script.index("on tendBodyState")
