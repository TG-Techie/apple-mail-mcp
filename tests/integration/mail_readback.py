"""Read back what Mail.app holds for mail the integration tests sent.

A send call that returns success says the compose window went away; it
says nothing about what the mail carried. These helpers read the
evidence instead: the copy Mail filed in Sent, and the delivered copy
that comes back through the loopback (``MAIL_TEST_LOOPBACK``, see
conftest.py) into the test account's INBOX. Each hands back raw source
and headers, so a test asserts on what was actually on the wire.

Not a test module (no ``test_`` prefix), so pytest does not collect it.
Every value interpolated into AppleScript goes through
``escape_applescript_string``. Cleanup moves mail to the account's
Trash, which is all Mail's ``delete`` does (issue #111), and then reads
back that it is gone.
"""

from __future__ import annotations

import email
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast

import pytest

from apple_mail_mcp.exceptions import MailDraftNotFoundError
from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

SENT_COPY_TIMEOUT_S = 30.0
_SENT_COPY_POLL_S = 2.0
_GONE_TIMEOUT_S = 15.0
_GONE_POLL_S = 1.0


def _quoted(value: str) -> str:
    """``value`` as an AppleScript string literal."""
    return f'"{escape_applescript_string(value)}"'


def _inbox_of(account: str) -> str:
    return f'mailbox "INBOX" of account {_quoted(account)}'


def bare_message_id(value: str) -> str:
    """A Message-ID without surrounding space or angle brackets, the form
    Mail's ``message id`` property holds on IMAP accounts."""
    return value.strip().strip("<>")


# ---------------------------------------------------------------------------
# Sent-mailbox reads, shared with test_verified_send.py
# ---------------------------------------------------------------------------


def sent_source_for_subject(connector: AppleMailConnector, subject: str) -> str:
    return connector._run_applescript(
        f'tell application "Mail" to return source of first message of '
        f"sent mailbox whose subject is {_quoted(subject)}"
    )


def assert_html_rendered(source: str, tag_fragment: str) -> None:
    """The message's html part must contain the REAL tag, not an
    escaped one — on 2026-07-21 mail shipped with &lt;p&gt; because the
    integration suite asserted dispatch but never rendering."""
    html_part = source[source.find("text/html"):]
    assert tag_fragment in html_part, "HTML did not render as HTML"
    escaped = tag_fragment.replace("<", "&lt;").replace(">", "&gt;")
    assert escaped not in html_part, "HTML was pasted as literal text"


def sent_count_for_subject(connector: AppleMailConnector, subject: str) -> int:
    out = connector._run_applescript(
        f'tell application "Mail" to return (count of (messages of sent mailbox '
        f"whose subject is {_quoted(subject)})) as text"
    ).strip()
    return int(out)


# ---------------------------------------------------------------------------
# Whole-message reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SentCopy:
    """The copy Mail filed in Sent. ``rfc_message_id`` is bare (see
    ``bare_message_id``) and is what finds the delivered copy."""

    mail_id: str
    rfc_message_id: str
    source: str = field(repr=False)
    headers: str = field(repr=False)
    attachment_names: tuple[str, ...]


@dataclass(frozen=True)
class Arrival:
    """The delivered copy, as it landed in the test account's INBOX.
    ``content`` is Mail's plain-text rendering of the body."""

    mail_id: str
    rfc_message_id: str
    source: str = field(repr=False)
    headers: str = field(repr=False)
    content: str
    attachment_names: tuple[str, ...]
    attachment_count: int


def _read_messages(
    connector: AppleMailConnector, messages_ref: str
) -> list[dict[str, Any]]:
    """Every message ``messages_ref`` names (an AppleScript ``messages of
    ... whose ...`` reference), each read whole."""
    body = f"""
tell application "Mail"
    set resultData to {{}}
    repeat with m in ({messages_ref})
        set attNames to name of every mail attachment of m
        if attNames is missing value then set attNames to {{}}
        set msgContent to content of m
        if msgContent is missing value then set msgContent to ""
        set msgSource to source of m
        if msgSource is missing value then set msgSource to ""
        set rfcId to message id of m
        if rfcId is missing value then set rfcId to ""
        set end of resultData to {{|id|:(id of m as text), |message_id|:rfcId, |source|:msgSource, |headers|:(all headers of m), |content|:msgContent, |attachment_names|:attNames, |attachment_count|:(count of mail attachments of m)}}
    end repeat
end tell
"""
    raw = connector._run_applescript(
        _wrap_as_json_script(body, timeout=connector.timeout)
    )
    return cast(list[dict[str, Any]], parse_applescript_json(raw))


def sent_copy(
    connector: AppleMailConnector,
    subject: str,
    timeout_s: float = SENT_COPY_TIMEOUT_S,
) -> SentCopy:
    """The one copy of the message with ``subject`` that Mail filed in
    Sent. Polls the application-level ``sent mailbox`` (every account's
    Sent), since the copy can land after the send call returns. Fails
    when none appears in time, and when more than one is there: subjects
    are unique per test, so two copies mean the message went out twice.
    """
    ref = f"messages of sent mailbox whose subject is {_quoted(subject)}"
    deadline = time.monotonic() + timeout_s
    found = _read_messages(connector, ref)
    while not found:
        if time.monotonic() >= deadline:
            pytest.fail(f"No copy of {subject!r} appeared in Sent within {timeout_s:g}s.")
        time.sleep(_SENT_COPY_POLL_S)
        found = _read_messages(connector, ref)
    if len(found) > 1:
        pytest.fail(f"{len(found)} copies of {subject!r} in Sent; one send files one.")
    record = found[0]
    return SentCopy(
        mail_id=str(record["id"]),
        rfc_message_id=bare_message_id(str(record["message_id"])),
        source=str(record["source"]),
        headers=str(record["headers"]),
        attachment_names=tuple(str(n) for n in record["attachment_names"]),
    )


def wait_for_arrival(
    connector: AppleMailConnector,
    account: str,
    rfc_message_id: str,
    timeout_s: float = 120.0,
    poll_s: float = 5.0,
) -> Arrival:
    """The delivered copy of the message with ``rfc_message_id``, once it
    is in ``account``'s INBOX.

    Mail's ``message id`` property is the RFC 5322 Message-ID header's
    value; IMAP accounts store it without angle brackets, so the match is
    ``contains`` on the bare id. Each poll first asks Mail to check the
    account for new mail. A match that Mail lists before it has the
    message's source is not an arrival yet. The ``whose`` match is not
    indexed: it cost well under a second a poll on the test account it
    was written against, and grows with the INBOX. Fails naming the
    timeout and the id when nothing arrives, and when two copies do.
    """
    bare = bare_message_id(rfc_message_id)
    ref = f"messages of {_inbox_of(account)} whose message id contains {_quoted(bare)}"
    check = f'tell application "Mail" to check for new mail for account {_quoted(account)}'
    deadline = time.monotonic() + timeout_s
    while True:
        connector._run_applescript(check)
        found = [r for r in _read_messages(connector, ref) if r["source"]]
        if found:
            break
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Nothing with Message-ID {bare!r} arrived in the INBOX of "
                f"{account!r} within {timeout_s:g}s."
            )
        time.sleep(poll_s)
    if len(found) > 1:
        pytest.fail(f"{len(found)} copies of Message-ID {bare!r} arrived; one send delivers one.")
    record = found[0]
    return Arrival(
        mail_id=str(record["id"]),
        rfc_message_id=bare_message_id(str(record["message_id"])),
        source=str(record["source"]),
        headers=str(record["headers"]),
        content=str(record["content"]),
        attachment_names=tuple(str(n) for n in record["attachment_names"]),
        attachment_count=int(record["attachment_count"]),
    )


def header(headers: str, name: str) -> str | None:
    """The value of the first ``name`` header in ``headers`` (Mail's
    ``all headers`` text), matched case-insensitively, with folded
    continuation lines joined by a space; None when it is absent."""
    wanted = name.lower()
    value: str | None = None
    for line in headers.splitlines():
        folded = line[:1] in (" ", "\t")
        if value is not None:
            if not folded:
                break
            value = f"{value} {line.strip()}"
        elif not folded:
            field, colon, rest = line.partition(":")
            if colon and field.lower() == wanted:
                value = rest.strip()
    return value


def html_part(source: str) -> str:
    """The first text/html part of ``source``, transfer encoding undone
    (so quoted-printable soft breaks cannot split what a test looks
    for); "" when there is none."""
    message = email.message_from_string(source)
    for part in message.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            return cast(bytes, payload).decode(charset, errors="replace")
    return ""


# ---------------------------------------------------------------------------
# Cleanup: moved to Trash, then read back as gone
# ---------------------------------------------------------------------------


def _count(connector: AppleMailConnector, messages_ref: str) -> int:
    out = connector._run_applescript(
        f'tell application "Mail" to return (count of ({messages_ref})) as text'
    )
    return int(out.strip())


def _await_gone(connector: AppleMailConnector, messages_ref: str, what: str) -> None:
    deadline = time.monotonic() + _GONE_TIMEOUT_S
    while _count(connector, messages_ref):
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{what} was still there {_GONE_TIMEOUT_S:g}s after it was "
                "moved to Trash"
            )
        time.sleep(_GONE_POLL_S)


def trash_sent_copy(connector: AppleMailConnector, subject: str) -> None:
    """Move every Sent copy with ``subject`` to Trash. Mail's bulk
    ``delete`` of a ``whose`` reference is what moves them."""
    ref = f"messages of sent mailbox whose subject is {_quoted(subject)}"
    connector._run_applescript(f'tell application "Mail" to delete ({ref})')
    _await_gone(connector, ref, f"The Sent copy of {subject!r}")


def trash_arrival(connector: AppleMailConnector, account: str, arrival: Arrival) -> None:
    """Move a delivered copy from ``account``'s INBOX to Trash, through
    the connector's ``delete_messages`` with the Mail id.

    Ids are path-native (docs/reference/TOOLS.md): with account and
    source_mailbox given, ``delete_messages`` tries the IMAP fast path
    first, and that path reads ids as RFC Message-IDs. On an account with
    IMAP credentials in the Keychain the Mail id matches nothing there,
    the move reports 0, and this fails rather than leave the message.
    """
    moved = connector.delete_messages(
        [arrival.mail_id], account=account, source_mailbox="INBOX"
    )
    if moved != 1:
        raise AssertionError(
            f"delete_messages moved {moved} messages for Mail id "
            f"{arrival.mail_id!r}, expected 1"
        )
    ref = (
        f"messages of {_inbox_of(account)} whose message id contains "
        f"{_quoted(arrival.rfc_message_id)}"
    )
    _await_gone(connector, ref, f"The INBOX copy of {arrival.rfc_message_id!r}")


def trash_drafts(connector: AppleMailConnector, subject: str) -> None:
    """Move every draft with ``subject`` to Trash, by id through the
    connector's ``delete_draft``. By subject, not by the id a test was
    given: an iCloud draft can be re-saved under a new id, and deleting
    the old id then leaves the copy (docs/research/icloud-draft-resync.md,
    which also records a delete over a walk of drafts removing nothing).
    """
    ref = f"messages of drafts mailbox whose subject is {_quoted(subject)}"
    out = connector._run_applescript(
        f'tell application "Mail" to set idList to id of ({ref})\n'
        "set AppleScript's text item delimiters to \" \"\n"
        "return idList as text"
    )
    for draft_id in out.split():
        try:
            connector.delete_draft(draft_id)
        except MailDraftNotFoundError:
            pass  # already retired by the send, which is the ordinary case
    _await_gone(connector, ref, f"A draft of {subject!r}")


class MailTrash:
    """Everything one test sent and received, moved to Trash when it ends.

    Use as ``with MailTrash(connector, account) as trash:`` around the
    test body, which makes the moves a ``finally``. Register each piece as
    soon as it can exist: a subject before its send (a send that raises
    may still have filed a Sent copy), an arrival once found, a draft
    subject before the draft is saved. On exit every piece is attempted
    even if another fails, and the failures are raised together, so a
    test that passed but left mail behind does not pass.
    """

    def __init__(self, connector: AppleMailConnector, account: str) -> None:
        self._connector = connector
        self._account = account
        self._sent: list[str] = []
        self._arrivals: list[Arrival] = []
        self._drafts: list[str] = []

    def __enter__(self) -> MailTrash:
        return self

    def sent(self, subject: str) -> str:
        self._sent.append(subject)
        return subject

    def arrived(self, arrival: Arrival) -> Arrival:
        self._arrivals.append(arrival)
        return arrival

    def drafts(self, subject: str) -> str:
        self._drafts.append(subject)
        return subject

    def _trash_one(self, kind: str, item: Any) -> None:
        if kind == "arrival":
            trash_arrival(self._connector, self._account, item)
        elif kind == "sent":
            trash_sent_copy(self._connector, item)
        else:
            trash_drafts(self._connector, item)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pieces: list[tuple[str, Any]] = (
            [("arrival", a) for a in self._arrivals]
            + [("sent", s) for s in self._sent]
            + [("drafts", s) for s in self._drafts]
        )
        failures = []
        for kind, item in pieces:
            try:
                self._trash_one(kind, item)
            except Exception as error:  # every piece is attempted
                what = item.rfc_message_id if isinstance(item, Arrival) else item
                failures.append(f"{kind} {what!r}: {error}")
        if failures:
            raise AssertionError(
                "Not everything this test sent or received reached Trash:\n"
                + "\n".join(failures)
            )
