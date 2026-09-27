"""Live and read-only: the AppleScript search's bulk reads return what
Mail holds, and their fallbacks return the same.

The search reads a criterion's property for the whole mailbox in one
event and each row property once per run of matched positions (see
``AppleMailConnector._search_messages_applescript``). These tests hold
its result against two other readings of the same mail:

- each row against ``get_message`` for its id, a different script that
  reads one message by id;
- a filtered search's ids against the search as it was before the bulk
  reads, one message at a time, written out here as a small script of
  its own.

The fallbacks run against the real Mail by breaking one bulk read in
the emitted script (a range past the mailbox's end, which Mail refuses,
or a raised error where Mail answers any index) or one alignment check,
and must give back the same rows with a warning saying what happened.

Nothing is sent, saved, moved or deleted, and no window is touched.
Assertions compare ids and fields between two readings and never print
a subject or an address.

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
