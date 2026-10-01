"""Tending Mail's compose windows, against the real Mail
(docs/research/compose-window-tending.md).

Run via:
    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> pytest tests/integration/test_compose_tending.py --run-integration -v

Nothing here sends. Each test gets a compose ledger and a tending clock
of its own, in a temporary directory, so a pass here sees only the
windows this test opened through the connector as the connector's, and
starts every other window's clock at its first pass: no window the test
did not open can be stale to it. A test that needs one of its own
windows stale backdates that window's clock entry alone, and asks a
dry run, by Mail's id, that nothing else would close before the real
pass. Every test checks that each window open before it is still open
after. A window a pass salvages becomes a draft whose subject carries the
suite's prefix, moved to Trash when the test ends (and swept at the end
of the session if not).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from apple_mail_mcp.compose_clock import ComposeClock
from apple_mail_mcp.compose_ledger import Closed, ComposeLedger, LeftOpen, Open
from apple_mail_mcp.compose_tending import (
    STALE_S,
    LeftWindow,
    SightingClock,
    inventory_from_report,
    plan_tending,
)
from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.mail_connector import AppleMailConnector, _ComposeWindow
from apple_mail_mcp.utils import parse_applescript_json

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import MailTrash, compose_window_count, draft_ids, mail_window_ids

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run.",
)


@pytest.fixture
def ledger(tmp_path: Path) -> ComposeLedger:
    return ComposeLedger(tmp_path / "compose_windows")


@pytest.fixture
def clock(tmp_path: Path) -> ComposeClock:
    return ComposeClock(tmp_path / "compose_clock.json")


@pytest.fixture
def connector(ledger: ComposeLedger, clock: ComposeClock) -> AppleMailConnector:
    return AppleMailConnector(timeout=90, compose_ledger=ledger, compose_clock=clock)


def _subject(kind: str) -> str:
    return f"{TEST_DRAFT_SUBJECT_PREFIX}tend-{kind}-{uuid.uuid4().hex[:8]}"


def _counts(connector: AppleMailConnector) -> dict[str, int]:
    """The windows System Events lists for Mail, and Mail's own
    ``outgoing messages``, read before and after each test."""
    out = connector._run_applescript(
        'tell application "System Events" to tell application process "Mail" '
        "to set w to count of windows\n"
        'tell application "Mail" to set o to count of outgoing messages\n'
        'return (w as text) & " " & (o as text)'
    ).split()
    return {"system_events_windows": int(out[0]), "outgoing_messages": int(out[1])}


def _open_plain_window(
    connector: AppleMailConnector, subject: str | None, content: str | None = "typed by someone else"
) -> int:
    """A compose window opened past the connector, so no ledger names it,
    as a person's or another client's would be; Mail's id for it. With
    no subject and no content it is an empty "New Message" window."""
    before = mail_window_ids(connector)
    props = ["visible:true"]
    if subject is not None:
        props.append(f'subject:"{subject}"')
    if content is not None:
        props.append(f'content:"{content}"')
    connector._run_applescript(
        f'tell application "Mail" to make new outgoing message with properties {{{", ".join(props)}}}'
    )
    for _ in range(20):
        new = mail_window_ids(connector) - before
        if new:
            assert len(new) == 1, new
            return new.pop()
        time.sleep(0.25)
    raise AssertionError("no window opened")


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


def _planned_ids(connector: AppleMailConnector, ledger: ComposeLedger, clock: ComposeClock) -> list[int]:
    """Mail's ids for the windows a pass would close now: the plan, read
    and decided as a pass would, and nothing done."""
    raw = connector._run_applescript(connector._build_compose_inventory_script())
    inventory = inventory_from_report(cast(dict, parse_applescript_json(raw)))  # type: ignore[type-arg]
    assert inventory is not None
    plan = plan_tending(inventory, ledger.read_all().records, clock.load(), now=time.time())
    return [a.window_id for a in plan.actions]


def _backdate(clock: ComposeClock, *window_ids: int) -> None:
    """Make these windows' clocks, and no other's, older than the stale
    period, as if a pass had first seen them over an hour ago."""
    saved = clock.load()
    for window_id in window_ids:
        assert window_id in saved.windows, f"window {window_id} was not clocked"
    clock.save(
        SightingClock(
            mail_pid=saved.mail_pid,
            windows={
                k: replace(v, since=v.since - STALE_S - 60) if k in window_ids else v
                for k, v in saved.windows.items()
            },
        )
    )


@pytest.fixture
def before(connector: AppleMailConnector) -> Iterator[dict[str, object]]:
    """Every window Mail had open before the test, and the counts; after
    the test, every one of those windows must still be open."""
    ids = mail_window_ids(connector)
    counts = _counts(connector)
    yield {"ids": ids, "counts": counts}
    after_ids = mail_window_ids(connector)
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
            assert record.window_id in mail_window_ids(connector, subject)
            assert record.window_id not in before["ids"]  # type: ignore[operator]

            # Its composition could still be running: left.
            report = connector.tend_compose_windows(dry_run=True)
            assert LeftWindow(subject, "in_flight") in report.left
            assert report.to_close == ()

            # Past its grace, a dry run would close it, and nothing else.
            report = connector.tend_compose_windows(dry_run=True, grace_s=0)
            print(f"\ndry run: {report.as_dict()}")
            assert report.to_close == ((subject, "salvage", "abandoned"),)
            assert compose_window_count(connector, subject) == 1
            assert ledger.get(record.record_id) == record

            report = connector.tend_compose_windows(grace_s=0)
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ((subject, "salvaged", "abandoned"),)
            assert report.failed == () and report.not_attempted == ()
            assert compose_window_count(connector, subject) == 0
            state = ledger.get(record.record_id).state  # type: ignore[union-attr]
            assert isinstance(state, Closed)
            assert (state.how, state.by) == ("salvaged", "tending")
            assert draft_ids(connector, subject), "the salvaged window's draft is not in Drafts"


class TestWindowsNobodyRecorded:
    def test_a_first_pass_leaves_every_window_and_starts_its_clock(
        self,
        connector: AppleMailConnector,
        clock: ComposeClock,
        before: dict[str, object],
    ) -> None:
        """A real pass with a fresh clock and an empty ledger: every window
        Mail had open (the ones it restored at its relaunch among them)
        is first seen, so left, and clocked by Mail's id."""
        report = connector.tend_compose_windows()
        print(f"\npass: {report.as_dict()}")
        assert report.to_close == () and report.closed == () and report.failed == ()
        assert [w.reason for w in report.left] == ["not_yet_stale"] * report.compose_windows
        assert len(clock.load().windows) == report.compose_windows

    def test_an_empty_one_sharing_its_name_is_discarded_by_id_and_its_sibling_stays(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        clock: ComposeClock,
        before: dict[str, object],
    ) -> None:
        """Two empty "New Message" windows of the test's own, among every
        other "New Message" window Mail has open. Only the one whose
        clock ran out is closed, by Mail's id, and without Save."""
        stale = _open_plain_window(connector, None, None)
        sibling = _open_plain_window(connector, None, None)
        try:
            first = connector.tend_compose_windows()
            assert first.closed == ()
            _backdate(clock, stale)
            assert _planned_ids(connector, ledger, clock) == [stale]

            report = connector.tend_compose_windows()
            print(f"\npass: {report.as_dict()}")
            assert report.closed == (("New Message", "discarded", "stale"),)
            assert report.failed == ()
            assert stale not in mail_window_ids(connector)
            assert sibling in mail_window_ids(connector, "New Message")
        finally:
            for window_id in (stale, sibling):
                if window_id in mail_window_ids(connector):
                    connector._discard_compose_window("New Message", window_id)

    def test_one_with_content_is_salvaged_to_drafts(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        clock: ComposeClock,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        subject = _subject("stale")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            mine = _open_plain_window(connector, subject)
            connector.tend_compose_windows()
            _backdate(clock, mine)
            assert _planned_ids(connector, ledger, clock) == [mine]

            report = connector.tend_compose_windows()
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ((subject, "salvaged", "stale"),)
            assert compose_window_count(connector, subject) == 0
            saved: list[str] = []
            for _ in range(20):
                saved = draft_ids(connector, subject)
                if saved:
                    break
                time.sleep(0.5)
            assert saved, "the salvaged window's draft never reached Drafts"

    def test_an_edit_restarts_its_clock(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        clock: ComposeClock,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        subject = _subject("edited")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            mine = _open_plain_window(connector, subject, "first words")
            connector.tend_compose_windows()
            _backdate(clock, mine)
            assert _planned_ids(connector, ledger, clock) == [mine]

            edited_at = time.time()
            connector._paste_verified(
                window=_ComposeWindow(
                    name=subject, subject=subject, to=[], cc=[], bcc=[],
                    before_ids=[], window_id=mine,
                ),
                body="more words, typed later",
                placement="end",
                plain=True,
            )
            report = connector.tend_compose_windows()
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ()
            assert compose_window_count(connector, subject) == 1
            assert clock.load().windows[mine].since >= edited_at
            (left,) = [w for w in report.left if w.name == subject]
            assert left.reason == "not_yet_stale"
            assert left.remaining_s is not None and left.remaining_s > STALE_S - 120


class TestTwoWindowsOfOneName:
    def test_a_close_by_name_closes_neither(
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

    def test_a_close_by_id_closes_exactly_that_one(
        self, connector: AppleMailConnector, test_account: str, before: dict[str, object]
    ) -> None:
        """The newer of two same-named windows, then the older behind a
        third: each time the one Mail's id names, and only it."""
        subject = _subject("twin-by-id")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            older = _open_plain_window(connector, subject)
            newer = _open_plain_window(connector, subject)
            assert connector._discard_compose_window(subject, newer) == "DISCARDED"
            assert mail_window_ids(connector, subject) == {older}
            third = _open_plain_window(connector, subject)
            assert connector._discard_compose_window(subject, older) == "DISCARDED"
            assert mail_window_ids(connector, subject) == {third}

    def test_one_the_connector_opened_is_recorded_left_open_and_closed_by_id(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        clock: ComposeClock,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        """COMPOSE_WINDOW_NOT_UNIQUE: the connector's window is left open
        by design, and recorded under Mail's id for it, which is not the
        other window's. Tending closes it by that id, at once since its
        composition ended, and leaves the other."""
        subject = _subject("not-unique")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            other = _open_plain_window(connector, subject)
            with pytest.raises(MailAppleScriptError, match="COMPOSE_WINDOW_NOT_UNIQUE"):
                _open_through_the_connector(connector, subject, test_account)
            (record,) = ledger.read_all().records
            assert isinstance(record.state, LeftOpen)
            assert record.window_id in mail_window_ids(connector, subject) - {other}
            assert _planned_ids(connector, ledger, clock) == [record.window_id]
            report = connector.tend_compose_windows()
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ((subject, "salvaged", "abandoned"),)
            assert mail_window_ids(connector, subject) == {other}
            state = ledger.get(record.record_id).state  # type: ignore[union-attr]
            assert isinstance(state, Closed) and state.by == "tending"


class TestAWindowNoOpeningScriptReported:
    def test_is_found_recorded_and_closed_at_the_next_pass(
        self,
        connector: AppleMailConnector,
        ledger: ComposeLedger,
        clock: ComposeClock,
        test_account: str,
        before: dict[str, object],
    ) -> None:
        """As after NO_COMPOSE_WINDOW: a compose window that opened since
        the snapshot, which no script reported, is recorded as left open
        and closed by the next pass."""
        subject = _subject("late")
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            snapshot = connector._mail_window_snapshot()
            late = _open_plain_window(connector, subject)
            adopted = connector._adopt_unreported_windows(
                snapshot, expected_name=subject, operation="save", seed="new",
                failure="NO_COMPOSE_WINDOW (test)",
            )
            assert adopted == 1
            (record,) = ledger.read_all().records
            assert (record.window_id, record.window_name) == (late, subject)
            assert isinstance(record.state, LeftOpen)
            assert _planned_ids(connector, ledger, clock) == [late]
            report = connector.tend_compose_windows()
            print(f"\npass: {report.as_dict()}")
            assert report.closed == ((subject, "salvaged", "abandoned"),)
            assert compose_window_count(connector, subject) == 0
