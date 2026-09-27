"""Tending Mail's compose windows, against the real Mail
(docs/research/compose-window-tending.md).

Run via:
    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> pytest tests/integration/test_compose_tending.py --run-integration -v

Nothing here sends. Each test gets a compose ledger of its own, in a
temporary directory, so a pass here sees only the windows this test
opened through the connector, never another process's; every other
window Mail has open is, to the pass, one the connector did not open,
and every test checks that the pass left it. A window a pass salvages
becomes a draft whose subject carries the suite's prefix, moved to Trash
when the test ends (and swept at the end of the session if not).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from apple_mail_mcp.compose_ledger import Closed, ComposeLedger, LeftOpen, Open
from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import MailTrash, compose_window_count, draft_ids

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run.",
)


@pytest.fixture
def ledger(tmp_path: Path) -> ComposeLedger:
    return ComposeLedger(tmp_path / "compose_windows")


@pytest.fixture
def connector(ledger: ComposeLedger) -> AppleMailConnector:
    return AppleMailConnector(timeout=90, compose_ledger=ledger)


def _subject(kind: str) -> str:
    return f"{TEST_DRAFT_SUBJECT_PREFIX}tend-{kind}-{uuid.uuid4().hex[:8]}"


def _mail_window_ids(connector: AppleMailConnector, name: str | None = None) -> set[int]:
    """Mail's ids for its windows, or for those named ``name``."""
    where = "" if name is None else f' whose name is "{name}"'
    out = connector._run_applescript(
        f'tell application "Mail" to set ids to id of every window{where}\n'
        "set AppleScript's text item delimiters to \" \"\n"
        "return ids as text"
    )
    return {int(i) for i in out.split()}


def _counts(connector: AppleMailConnector) -> dict[str, int]:
    """What the brief asks to be read before and after: the windows System
    Events lists for Mail, and Mail's own ``outgoing messages``."""
    out = connector._run_applescript(
        'tell application "System Events" to tell application process "Mail" '
        "to set w to count of windows\n"
        'tell application "Mail" to set o to count of outgoing messages\n'
        'return (w as text) & " " & (o as text)'
    ).split()
    return {"system_events_windows": int(out[0]), "outgoing_messages": int(out[1])}


def _open_plain_window(connector: AppleMailConnector, subject: str) -> None:
    """A compose window opened past the connector, so no ledger names it:
    as a person's, or another client's, would be."""
    connector._run_applescript(
        'tell application "Mail" to make new outgoing message with properties '
        f'{{subject:"{subject}", content:"typed by someone else", visible:true}}\n'
        "delay 1"
    )


def _open_through_the_connector(
    connector: AppleMailConnector, subject: str, test_account: str
) -> None:
    """Open a compose window with the connector's own primitive, as every
    composition does, and walk away from it: no paste, no send, no
    close."""
    connector._open_compose(
        seed="new", seed_id=None, reply_all=False, to=["tend@example.com"],
        cc=None, bcc=None, subject=subject,
        sender=connector._resolve_account_to_sender(test_account),
        operation="save",
    )


@pytest.fixture
def before(connector: AppleMailConnector) -> Iterator[dict[str, object]]:
    """Every window Mail had open before the test, and the counts; after
    the test, every one of those windows must still be open."""
    ids = _mail_window_ids(connector)
    counts = _counts(connector)
    yield {"ids": ids, "counts": counts}
    after_ids = _mail_window_ids(connector)
    print(f"\nbefore {counts} after {_counts(connector)}")
    assert ids <= after_ids, f"windows open before the test closed: {ids - after_ids}"


class TestAWindowTheConnectorLeft:
    def test_is_found_recorded_and_closed_and_nothing_else_is(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        subject = _subject("abandoned")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            _open_through_the_connector(connector, subject, test_account)

            (record,) = ledger.read_all().records
            assert record.window_name == subject
            assert record.state == Open()
            assert record.window_id in _mail_window_ids(connector, subject)
            assert record.window_id not in before["ids"]  # type: ignore[operator]

            # Its composition could still be running: left.
            report = connector.tend_compose_windows(dry_run=True)
            assert (subject, "in_flight") in report.left
            assert report.to_close == ()

            # Past its grace, a dry run would close it, and nothing else.
            report = connector.tend_compose_windows(dry_run=True, grace_s=0)
            print(f"\ndry run: {report.as_dict()}")
            assert report.to_close == ((subject, "salvage"),)
            assert compose_window_count(connector, subject) == 1
            assert ledger.get(record.record_id) == record

            report = connector.tend_compose_windows(grace_s=0)
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ((subject, "salvaged"),)
            assert report.failed == () and report.not_attempted == ()
            assert compose_window_count(connector, subject) == 0
            state = ledger.get(record.record_id).state  # type: ignore[union-attr]
            assert isinstance(state, Closed)
            assert (state.how, state.by) == ("salvaged", "tending")
            assert draft_ids(connector, subject), "the salvaged window's draft is not in Drafts"


class TestWindowsTheLedgerDoesNotName:
    def test_are_left_open_and_counted(
        self,
        connector: AppleMailConnector,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        subject = _subject("unowned")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            _open_plain_window(connector, subject)
            report = connector.tend_compose_windows(grace_s=0)
            print(f"\npass: {report.as_dict()}")
            assert report.closed == () and report.failed == ()
            assert (subject, "unowned") in report.left
            assert compose_window_count(connector, subject) == 1

    def test_the_windows_open_before_the_run_are_all_left(
        self, connector: AppleMailConnector, before: dict[str, object]
    ) -> None:
        """A real pass with a ledger that names none of them: every window
        Mail had open (the ones it restored at its relaunch among them) is
        left, and ``before`` checks each is still open after."""
        report = connector.tend_compose_windows(grace_s=0)
        print(f"\npass: {report.as_dict()}")
        assert report.to_close == () and report.closed == () and report.failed == ()
        assert len(report.left) == report.compose_windows


class TestTwoWindowsOfOneName:
    def test_the_salvage_closes_neither(
        self, connector: AppleMailConnector, test_account: str, before: dict[str, object]
    ) -> None:
        subject = _subject("twins")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            _open_plain_window(connector, subject)
            _open_plain_window(connector, subject)
            outcome = connector._salvage_compose_to_draft(subject)
            assert outcome.startswith("SALVAGE_FAILED:2 windows are named")
            assert compose_window_count(connector, subject) == 2

    def test_one_the_connector_opened_is_recorded_left_open_and_left(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        """COMPOSE_WINDOW_NOT_UNIQUE: the connector's window is left open
        by design, and recorded under Mail's id for it, which is not the
        other window's. Tending leaves both while they share the name."""
        subject = _subject("not-unique")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            _open_plain_window(connector, subject)
            other = _mail_window_ids(connector, subject)
            with pytest.raises(MailAppleScriptError, match="COMPOSE_WINDOW_NOT_UNIQUE"):
                _open_through_the_connector(connector, subject, test_account)
            (record,) = ledger.read_all().records
            assert isinstance(record.state, LeftOpen)
            assert record.window_id in _mail_window_ids(connector, subject) - other
            report = connector.tend_compose_windows(grace_s=0)
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ()
            assert [r for n, r in report.left if n == subject] == ["name_not_unique"] * 2
            assert compose_window_count(connector, subject) == 2
