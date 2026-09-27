"""Live and read-only: the AppleScript search's bulk reads return what
Mail holds, and their fallbacks return the same.

The search reads a criterion's property for the whole mailbox in one
event, the content a body or text filter tests once per run of the
positions the other criteria kept, and each row property once per run
of matched positions (see
``AppleMailConnector._search_messages_applescript``). These tests hold
its result against other readings of the same mail:

- each row against ``get_message`` for its id, a different script that
  reads one message by id;
- a filtered search's ids against the search as it was before the bulk
  reads, one message at a time, written out here as a small script of
  its own;
- a content search against itself with each body read in a batch of
  its own, and with a lower limit.

The fallbacks run against the real Mail by breaking one bulk read in
the emitted script (a range past the mailbox's end, which Mail refuses,
or a raised error where Mail answers any index) or one alignment check,
and must give back the same rows with a warning saying what happened.
The content searches are bounded by date to the newest messages, so
they never read every body of the INBOX.

Nothing is sent, saved, moved or deleted, and no window is touched.
Assertions compare ids and fields between two readings and never print
a subject or an address (the older tests' whole-row comparisons do
when they fail).

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> uv run pytest \\
        tests/integration/test_search_bulk.py --run-integration -v
"""

from __future__ import annotations

import functools
import os
from typing import Any, cast

import pytest

from apple_mail_mcp import mail_connector
from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

from .conftest import TEST_DRAFT_SUBJECT_PREFIX

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests disabled by default. Use --run-integration to run.",
    ),
    pytest.mark.skipif(
        os.getenv("MAIL_TEST_MODE") != "true", reason="MAIL_TEST_MODE != 'true'"
    ),
]

# The fields a search row and a get_message row share.
_ROW_FIELDS = (
    "id", "rfc_message_id", "subject", "sender", "to", "cc", "bcc",
    "date_received", "read_status", "flagged",
)

# Enough marked messages to span misses between them, few enough that
# the one-message-at-a-time reading stays quick.
_MARKED_LIMIT = 30


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=120)


@functools.cache
def _trash_name(account: str) -> str:
    """The name of ``account``'s trash mailbox, found under Mail's
    application-level trash rather than assumed."""
    body = f"""
tell application "Mail"
    set resultData to {{|name|:""}}
    repeat with box in mailboxes of trash mailbox
        if (name of account of box) is "{escape_applescript_string(account)}" then
            set resultData to {{|name|:(name of box)}}
            exit repeat
        end if
    end repeat
end tell
"""
    raw = AppleMailConnector(timeout=120)._run_applescript(
        _wrap_as_json_script(body, timeout=120)
    )
    name = str(cast(dict[str, Any], parse_applescript_json(raw))["name"])
    if not name:
        pytest.skip(f"no trash mailbox found for {account!r}")
    return name


def _ids_one_at_a_time(
    connector: AppleMailConnector, account: str, mailbox: str, limit: int
) -> list[str]:
    """The ids of the newest ``limit`` messages of ``mailbox`` whose
    subject carries the suite's prefix, found as the search found them
    before it read in bulk: each message's subject read by its own
    event, newest first, stopping at the limit, a message whose subject
    cannot be read passed over."""
    body = f"""
tell application "Mail"
    set msgs to messages of mailbox "{escape_applescript_string(mailbox)}" of account "{escape_applescript_string(account)}"
    set resultData to {{}}
    repeat with m in msgs
        if (count of resultData) >= {limit} then exit repeat
        try
            if (subject of m) contains "{escape_applescript_string(TEST_DRAFT_SUBJECT_PREFIX)}" then set end of resultData to (id of m as text)
        end try
    end repeat
end tell
"""
    raw = connector._run_applescript(_wrap_as_json_script(body, timeout=120))
    return [str(i) for i in cast(list[Any], parse_applescript_json(raw))]


def _search(
    connector: AppleMailConnector, account: str, mailbox: str, **criteria: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    rows = connector._search_messages_applescript(
        account, mailbox, on_warning=warnings.append, **criteria
    )
    return rows, warnings


def _breaking(
    connector: AppleMailConnector,
    monkeypatch: pytest.MonkeyPatch,
    old: str,
    new: str,
) -> None:
    """Make the connector's next scripts run with ``old`` replaced by
    ``new``, failing the test if the script does not contain ``old``."""
    run = connector._run_applescript

    def edited(script: str) -> str:
        assert old in script, f"the search script no longer contains {old!r}"
        return run(script.replace(old, new))

    monkeypatch.setattr(connector, "_run_applescript", edited)


# A range past the mailbox's end, which Mail refuses, in place of the
# run's range.
_PAST_THE_END = "messages runStart thru (runEnd + 1000000) of mailboxRef"

# What the content searches look for: a reserved domain, never a real
# person's name or address.
_CONTENT_TEXT = "example.com"

# The content searches start from the day this many messages back in
# the INBOX was received, so the bodies they read are that day's and
# newer rather than the whole mailbox's.
_CONTENT_SPAN = 30


def _day_of_position(
    connector: AppleMailConnector, account: str, mailbox: str, position: int
) -> str:
    """The day, YYYY-MM-DD, that the message at ``position`` of
    ``mailbox`` (the last message, if it holds fewer) was received."""
    body = f"""
tell application "Mail"
    set mb to mailbox "{escape_applescript_string(mailbox)}" of account "{escape_applescript_string(account)}"
    set n to count of messages of mb
    set resultData to {{|day|:""}}
    if n > 0 then
        set p to {position}
        if p > n then set p to n
        set d to date received of message p of mb
        set resultData to {{|day|:((year of d) as text) & "-" & (text -2 thru -1 of ("0" & ((month of d) as integer))) & "-" & (text -2 thru -1 of ("0" & (day of d)))}}
    end if
end tell
"""
    raw = connector._run_applescript(_wrap_as_json_script(body, timeout=120))
    day = str(cast(dict[str, Any], parse_applescript_json(raw))["day"])
    if not day:
        pytest.skip(f"{mailbox!r} is empty")
    return day


def _assert_same_rows(
    got: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    """The same rows in the same order, reported by id and field name
    only."""
    assert [r["id"] for r in got] == [r["id"] for r in expected]
    differing = [
        (row["id"], [f for f in row if row[f] != other.get(f)])
        for row, other in zip(got, expected, strict=True)
        if row != other
    ]
    assert differing == []


class TestTheRowsAreMailsOwn:
    def test_each_row_equals_get_message_for_its_id(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        rows, warnings = _search(connector, test_account, "INBOX", limit=5)
        assert warnings == []
        assert rows, "the test account's INBOX is empty"
        for row in rows:
            single = connector.get_message(row["id"], include_content=False)
            differing = [f for f in _ROW_FIELDS if row[f] != single[f]]
            assert differing == [], f"message {row['id']}: {differing} differ"

    def test_a_marked_search_matches_the_one_message_at_a_time_search(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        trash = _trash_name(test_account)
        expected = _ids_one_at_a_time(connector, test_account, trash, _MARKED_LIMIT)
        if not expected:
            pytest.skip(f"no message in {trash!r} carries {TEST_DRAFT_SUBJECT_PREFIX!r}")
        rows, warnings = _search(
            connector, test_account, trash,
            subject_contains=TEST_DRAFT_SUBJECT_PREFIX, limit=_MARKED_LIMIT,
        )
        assert warnings == []
        assert [r["id"] for r in rows] == expected

    def test_runs_split_at_every_miss_give_the_same_rows(
        self,
        connector: AppleMailConnector,
        test_account: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no gap allowed, each stretch of matches between two
        misses is its own run, so the rows are assembled from many runs
        rather than one."""
        trash = _trash_name(test_account)
        criteria = {"subject_contains": TEST_DRAFT_SUBJECT_PREFIX, "limit": _MARKED_LIMIT}
        merged, _ = _search(connector, test_account, trash, **criteria)
        monkeypatch.setattr(mail_connector, "_SEARCH_RUN_GAP", 0)
        split, warnings = _search(connector, test_account, trash, **criteria)
        assert warnings == []
        assert split == merged


class TestTheFallbacksGiveTheSameRows:
    """Each breaks one part of the bulk path in the script Mail runs,
    and holds the result against the unbroken search's."""

    @pytest.fixture
    def trash(self, test_account: str) -> str:
        return _trash_name(test_account)

    def _both(
        self,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
        account: str,
        mailbox: str,
        breaks: tuple[str, str],
        **criteria: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        intact, intact_warnings = _search(connector, account, mailbox, **criteria)
        assert intact_warnings == []
        _breaking(connector, monkeypatch, *breaks)
        broken, warnings = _search(connector, account, mailbox, **criteria)
        return intact, broken, warnings

    def test_a_row_property_read_one_message_at_a_time(
        self,
        connector: AppleMailConnector,
        test_account: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = "subject of messages runStart thru runEnd of mailboxRef"
        intact, broken, warnings = self._both(
            connector, monkeypatch, test_account, "INBOX",
            (old, "subject of " + _PAST_THE_END), limit=10,
        )
        assert broken == intact
        assert len(warnings) == 1
        assert warnings[0].startswith(
            "subject could not be read in bulk for mailbox positions 1-10: "
        )

    def test_a_recipient_list_read_one_message_at_a_time(
        self,
        connector: AppleMailConnector,
        test_account: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = "properties of cc recipients of messages runStart thru runEnd of mailboxRef"
        intact, broken, warnings = self._both(
            connector, monkeypatch, test_account, "INBOX",
            (old, "properties of cc recipients of " + _PAST_THE_END), limit=10,
        )
        assert broken == intact
        assert [w.partition(":")[0] for w in warnings] == [
            "cc recipients could not be read in bulk for mailbox positions 1-10"
        ]

    def test_a_criterion_read_one_message_at_a_time(
        self,
        connector: AppleMailConnector,
        test_account: str,
        trash: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        intact, broken, warnings = self._both(
            connector, monkeypatch, test_account, trash,
            (
                "set subjectAll to subject of messages of mailboxRef",
                'error "broken by the test" number -1728',
            ),
            subject_contains=TEST_DRAFT_SUBJECT_PREFIX, limit=_MARKED_LIMIT,
        )
        assert intact, "nothing in the trash carries the prefix"
        # Each message's subject is then read by its own event, and the
        # rows take theirs from the run, as with no filter.
        assert broken == intact
        assert [w.partition(":")[0] for w in warnings] == [
            "subject could not be read in bulk"
        ]

    def test_lists_out_of_line_search_one_message_at_a_time(
        self,
        connector: AppleMailConnector,
        test_account: str,
        trash: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The last check before returning says the positions moved, as
        a message arriving mid-search would: the rows the bulk path
        built are dropped and the search is done again the old way,
        with none of them left over."""
        intact, broken, warnings = self._both(
            connector, monkeypatch, test_account, trash,
            ("is not lastRowId then set aligned to false",
             "is lastRowId then set aligned to false"),
            subject_contains=TEST_DRAFT_SUBJECT_PREFIX, limit=_MARKED_LIMIT,
        )
        assert broken == intact
        assert warnings == [mail_connector._SEARCH_OUT_OF_LINE_WARNING]


class TestTheContentIsReadInBulk:
    """A body or text search reads the content over runs of the
    positions the other filters kept, in batches no larger than the
    matches still wanted. Its rows must not depend on how the bodies
    were batched, or on whether they were read in bulk at all."""

    @pytest.fixture
    def since(self, connector: AppleMailConnector, test_account: str) -> str:
        return _day_of_position(connector, test_account, "INBOX", _CONTENT_SPAN)

    @pytest.mark.parametrize("criterion", ["text_contains", "body_contains"])
    def test_the_rows_of_a_content_read_one_message_at_a_time(
        self,
        connector: AppleMailConnector,
        test_account: str,
        since: str,
        monkeypatch: pytest.MonkeyPatch,
        criterion: str,
    ) -> None:
        """Each run's content read refused, as a failed bulk read is:
        every body is then read by its own event, and the rows are the
        same."""
        criteria: dict[str, Any] = {
            criterion: _CONTENT_TEXT, "date_from": since, "limit": _MARKED_LIMIT,
        }
        intact, warnings = _search(connector, test_account, "INBOX", **criteria)
        assert warnings == []
        if not intact:
            pytest.skip(f"no INBOX message since {since} matches {criterion}")
        _breaking(
            connector, monkeypatch,
            "content of messages runStart thru runEnd of mailboxRef",
            "content of " + _PAST_THE_END,
        )
        broken, warnings = _search(connector, test_account, "INBOX", **criteria)
        _assert_same_rows(broken, intact)
        assert warnings
        assert [
            w.partition(":")[0].rpartition(" ")[0] for w in warnings
        ] == ["content could not be read in bulk for mailbox positions"] * len(warnings)

    def test_the_rows_do_not_depend_on_how_the_bodies_were_batched(
        self,
        connector: AppleMailConnector,
        test_account: str,
        since: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A batch of one reads each body by itself, and tests it before
        the next is read; a lower limit ends the reading sooner. Neither
        changes which rows come back, only how many."""
        criteria: dict[str, Any] = {
            "text_contains": _CONTENT_TEXT, "date_from": since,
            "limit": _MARKED_LIMIT,
        }
        whole, warnings = _search(connector, test_account, "INBOX", **criteria)
        assert warnings == []
        if len(whole) < 2:
            pytest.skip(f"fewer than two INBOX messages since {since} match")
        monkeypatch.setattr(mail_connector, "_SEARCH_CONTENT_BATCH", 1)
        one_by_one, warnings = _search(connector, test_account, "INBOX", **criteria)
        assert warnings == []
        _assert_same_rows(one_by_one, whole)
        monkeypatch.undo()
        fewer, warnings = _search(
            connector, test_account, "INBOX", **{**criteria, "limit": len(whole) - 1}
        )
        assert warnings == []
        _assert_same_rows(fewer, whole[:-1])
        # With no date bound the scan has nothing to test and every
        # position is a candidate. Mail lists the INBOX newest first, so
        # the matches since that day come first, and the limit ends the
        # reading at the last of them.
        undated, warnings = _search(
            connector, test_account, "INBOX",
            text_contains=_CONTENT_TEXT, limit=len(whole),
        )
        assert warnings == []
        _assert_same_rows(undated, whole)
