"""Live and read-only: a message row carries the recipients Mail holds.

Each test finds mail this suite left behind, whose subject carries the
suite's prefix (``TEST_DRAFT_SUBJECT_PREFIX``), reads it through one
read path, and holds the row's ``to``, ``cc`` and ``bcc`` against the
message's own headers, parsed here in Python rather than read through
the property the connector reads. Two messages: a sent copy whose To
names an RFC 2606 reserved address, and a delivered copy (one with a
Received line), which carries no Bcc.

Nothing is sent, saved, moved or deleted, and no window is touched.
Nothing prints or asserts on a real address: a mismatch reports counts,
and only reserved-domain addresses are compared by value.

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> uv run pytest \\
        tests/integration/test_message_recipients.py --run-integration -v

The IMAP tests also need the account's Keychain entry (see
test_imap_delegation.py) and skip without it.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from typing import Any, cast

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script
from apple_mail_mcp.security import _is_reserved_test_domain
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import bare_message_id, header
from .test_imap_delegation import _keychain_entry_exists

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

# How many candidates the finder hands to Python, which keeps the first
# that fits. A sent copy must name a reserved address in its To, and the
# finder can only narrow to ones whose headers mention one somewhere.
_CANDIDATES = 5

# A found message's subject may recur in its mailbox at most this often.
# get_thread's AppleScript path reads every message of the account whose
# subject contains the anchor's, and the suite reuses some subjects many
# times over.
_MAX_SAME_SUBJECT = 4


@dataclass(frozen=True)
class Marked:
    """A message this suite left behind, as Mail files it."""

    mail_id: str
    mailbox: str
    subject: str
    rfc_message_id: str
    # How many messages of its mailbox have a subject containing its own.
    same_subject: int
    headers: str = field(repr=False)


@functools.cache
def _find(account: str, *, delivered: bool) -> tuple[Marked, ...]:
    """Up to ``_CANDIDATES`` of ``account``'s messages whose subject
    carries the suite's prefix, newest first: delivered copies (a
    Received line) from its INBOX, Junk and Trash, or sent copies (no
    Received line) from its Sent and Trash whose headers mention an
    example.com address. Drafts, which Mail marks with its draft type
    header, are passed over, as are subjects that recur in their mailbox
    more than ``_MAX_SAME_SUBJECT`` times. Each mailbox is found under
    Mail's application-level special mailboxes, so no name is assumed.
    Read once per run: every test of a kind reads the same message."""
    boxes = (
        "{inbox, junk mailbox, trash mailbox}"
        if delivered
        else "{sent mailbox, trash mailbox}"
    )
    want = "true" if delivered else "false"
    also = "true" if delivered else '(hdrs contains "@example.com")'
    account_literal = escape_applescript_string(account)
    prefix_literal = escape_applescript_string(TEST_DRAFT_SUBJECT_PREFIX)
    body = f"""
tell application "Mail"
    set resultData to {{}}
    repeat with special in {boxes}
        repeat with box in mailboxes of special
            if (count of resultData) < {_CANDIDATES} and (name of account of box) is "{account_literal}" then
                repeat with m in (messages of box whose subject contains "{prefix_literal}")
                    if (count of resultData) ≥ {_CANDIDATES} then exit repeat
                    set hdrs to all headers of m
                    if hdrs does not contain "com.apple.mail-draft" then
                        set isDelivered to (hdrs starts with "Received:") or (hdrs contains (linefeed & "Received:")) or (hdrs contains (return & "Received:"))
                        if isDelivered is {want} and {also} then
                            set subj to subject of m
                            set sameSubject to count of (messages of box whose subject contains subj)
                            if sameSubject ≤ {_MAX_SAME_SUBJECT} then
                                set rfcId to message id of m
                                if rfcId is missing value then set rfcId to ""
                                set end of resultData to {{|id|:(id of m as text), |mailbox|:(name of box), |subject|:subj, |message_id|:rfcId, |same_subject|:sameSubject, |headers|:hdrs}}
                            end if
                        end if
                    end if
                end repeat
            end if
        end repeat
    end repeat
end tell
"""
    connector = AppleMailConnector(timeout=120)
    raw = connector._run_applescript(_wrap_as_json_script(body, timeout=120))
    return tuple(
        Marked(
            mail_id=str(r["id"]),
            mailbox=str(r["mailbox"]),
            subject=str(r["subject"]),
            rfc_message_id=bare_message_id(str(r["message_id"])),
            same_subject=int(r["same_subject"]),
            headers=str(r["headers"]),
        )
        for r in cast(list[dict[str, Any]], parse_applescript_json(raw))
    )


def _header_addresses(headers: str, name: str) -> list[str]:
    value = header(headers, name)
    if value is None:
        return []
    return [address.lower() for _name, address in getaddresses([value]) if address]


def _row_addresses(entries: list[str]) -> list[str]:
    return [parseaddr(entry)[1].lower() for entry in entries]


def _sent_copy(account: str) -> Marked:
    """One whose To names a reserved address, preferring one with a Cc
    too, so the Cc list is read off a real message."""
    fits = [
        marked
        for marked in _find(account, delivered=False)
        if any(
            _is_reserved_test_domain(a)
            for a in _header_addresses(marked.headers, "To")
        )
    ]
    with_cc = [m for m in fits if _header_addresses(m.headers, "Cc")]
    if with_cc or fits:
        return (with_cc or fits)[0]
    pytest.skip(
        f"no sent copy in Sent or Trash of {account!r} whose subject carries "
        f"{TEST_DRAFT_SUBJECT_PREFIX!r} and whose To names a reserved address"
    )


def _delivered_copy(account: str) -> Marked:
    found = _find(account, delivered=True)
    if not found:
        pytest.skip(
            f"no delivered copy in the INBOX, Junk or Trash of {account!r} "
            f"whose subject carries {TEST_DRAFT_SUBJECT_PREFIX!r}"
        )
    return found[0]


def _assert_row_holds_the_headers(row: dict[str, Any], marked: Marked) -> None:
    """The row's lists name exactly the addresses the message's To, Cc
    and Bcc headers name. Reports counts, and reserved addresses only."""
    for key, name in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        expected = _header_addresses(marked.headers, name)
        got = _row_addresses(row[key])
        same = sorted(got) == sorted(expected)
        assert same, (
            f"{key}: the row names {len(got)} address(es), the {name} header "
            f"{len(expected)}, and they differ"
        )


def _assert_sent_copy(row: dict[str, Any], marked: Marked) -> None:
    _assert_row_holds_the_headers(row, marked)
    reserved = [a for a in _row_addresses(row["to"]) if _is_reserved_test_domain(a)]
    assert reserved, "the sent copy's to names no reserved address"


def _assert_delivered_copy(row: dict[str, Any], marked: Marked) -> None:
    _assert_row_holds_the_headers(row, marked)
    to_count = len(row["to"])
    bcc_count = len(row["bcc"])
    assert to_count > 0, "a delivered copy's to is empty"
    assert bcc_count == 0, f"a delivered copy's bcc names {bcc_count} address(es)"


def _unreadable(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "recipients unreadable" in w]


# ---------------------------------------------------------------------------
# AppleScript path
# ---------------------------------------------------------------------------


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=120)


@pytest.mark.parametrize(
    "find,check",
    [(_sent_copy, _assert_sent_copy), (_delivered_copy, _assert_delivered_copy)],
    ids=["sent", "delivered"],
)
class TestAppleScriptRows:
    def test_search(
        self, connector: AppleMailConnector, test_account: str, find: Any, check: Any
    ) -> None:
        marked = find(test_account)
        warnings: list[str] = []
        rows = connector._search_messages_applescript(
            test_account,
            marked.mailbox,
            subject_contains=marked.subject,
            limit=marked.same_subject,
            on_warning=warnings.append,
        )
        [row] = [r for r in rows if r["id"] == marked.mail_id]
        assert _unreadable(warnings) == []
        check(row, marked)

    def test_get_message(
        self, connector: AppleMailConnector, test_account: str, find: Any, check: Any
    ) -> None:
        marked = find(test_account)
        row = connector.get_message(marked.mail_id, include_content=False)
        assert row["id"] == marked.mail_id
        assert _unreadable(row.get("warnings") or []) == []
        check(row, marked)

    def test_thread(
        self, connector: AppleMailConnector, test_account: str, find: Any, check: Any
    ) -> None:
        marked = find(test_account)
        warnings: list[str] = []
        thread = connector._get_thread_applescript(
            marked.mail_id, on_warning=warnings.append
        )
        [row] = [r for r in thread if r["id"] == marked.mail_id]
        assert _unreadable(warnings) == []
        check(row, marked)


# ---------------------------------------------------------------------------
# IMAP path
# ---------------------------------------------------------------------------


@pytest.fixture
def imap_account(connector: AppleMailConnector, test_account: str) -> str:
    """The test account, when IMAP delegation is set up for it."""
    _host, _port, email = connector._resolve_imap_config(test_account)
    if not _keychain_entry_exists(test_account, email):
        pytest.skip(
            f"No Keychain entry under apple-mail-mcp.imap.{test_account}; "
            "see test_imap_delegation.py for setup."
        )
    return test_account


@pytest.mark.parametrize(
    "find,check",
    [(_sent_copy, _assert_sent_copy), (_delivered_copy, _assert_delivered_copy)],
    ids=["sent", "delivered"],
)
class TestImapRows:
    """Each read asserts afterwards that IMAP served it: a failed IMAP
    call falls back to AppleScript and records the account."""

    def test_search(
        self, connector: AppleMailConnector, imap_account: str, find: Any, check: Any
    ) -> None:
        marked = find(imap_account)
        rows = connector.search_messages(
            account=imap_account,
            mailbox=marked.mailbox,
            subject_contains=marked.subject,
            limit=50,
        )
        assert imap_account not in connector._imap_failures
        [row] = [r for r in rows if r["rfc_message_id"] == marked.rfc_message_id]
        check(row, marked)

    def test_get_message(
        self, connector: AppleMailConnector, imap_account: str, find: Any, check: Any
    ) -> None:
        marked = find(imap_account)
        row = connector.get_message(
            marked.rfc_message_id,
            include_content=False,
            account=imap_account,
            mailbox=marked.mailbox,
        )
        assert imap_account not in connector._imap_failures
        assert row["rfc_message_id"] == marked.rfc_message_id
        check(row, marked)

    def test_thread(
        self, connector: AppleMailConnector, imap_account: str, find: Any, check: Any
    ) -> None:
        marked = find(imap_account)
        warnings: list[str] = []
        thread = connector.get_thread(marked.mail_id, on_warning=warnings.append)
        assert imap_account not in connector._imap_failures
        assert warnings == [], "the thread was not built by the IMAP path"
        [row] = [r for r in thread if r["rfc_message_id"] == marked.rfc_message_id]
        check(row, marked)
