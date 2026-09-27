"""Loopback read-back tests: send, then read what was actually delivered.

The send-path tests in test_verified_send.py stop at the Sent copy,
which is what Mail meant to send. These go one step further. They send
to the loopback, an address the tests may send to from which the mail
arrives in the test account's INBOX: the account's own address, or one
that forwards back to it. Then they read the delivered copy and assert
on its headers, HTML part, attachments and threading.

Real Mail.app; run via:
    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> MAIL_TEST_LOOPBACK=<address> \\
        uv run pytest tests/integration/test_loopback.py --run-integration -v

Without MAIL_TEST_LOOPBACK every test here skips. A fresh send cannot
name its sender on this version (Mail composes it from its default
account), so the test account must be Mail.app's default account; the
first test that finds otherwise fails, and the rest refuse to send.

Each test moves everything it sent and received to Trash when it ends,
pass or fail (see ``MailTrash``). Every subject starts with the suite's
``TEST_DRAFT_SUBJECT_PREFIX`` (conftest.py) followed by ``loopback-``.
"""

import asyncio
import uuid
from dataclasses import dataclass
from email import message_from_string
from pathlib import Path

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import (
    Arrival,
    MailTrash,
    SentCopy,
    assert_html_rendered,
    bare_message_id,
    compose_window_count,
    header,
    html_part,
    sent_copy,
    wait_for_arrival,
)

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run."
)

PREFIX = f"{TEST_DRAFT_SUBJECT_PREFIX}loopback-"
SENT = {"draft_id": "", "sent_message_id": ""}

# The From of a Sent copy that was not the test account, once one test
# has seen it. The default account is a property of the machine, not of
# a test, so the tests after it refuse to send rather than send again.
_wrong_default_sender: list[str] = []


def _hex() -> str:
    return uuid.uuid4().hex[:8]


def _two_files(tmp_path: Path, hexid: str) -> list[Path]:
    first = tmp_path / "first.txt"
    first.write_text(f"loopback attachment one {hexid}\n")
    second = tmp_path / "second.txt"
    second.write_text(f"loopback attachment two {hexid}\n")
    return [first, second]


def _attachment_bytes(source: str) -> dict[str, bytes]:
    """Each attached file in ``source`` by name, transfer encoding undone."""
    found: dict[str, bytes] = {}
    for part in message_from_string(source).walk():
        name = part.get_filename()
        if name:
            found[name] = part.get_payload(decode=True)
    return found


@dataclass(frozen=True)
class Loopback:
    connector: AppleMailConnector
    account: str
    account_address: str
    address: str

    def prepare(self, trash: MailTrash, subject: str) -> None:
        """Before a send: refuse if the default sender is known wrong, and
        register the subject so its Sent copy goes to Trash either way."""
        if _wrong_default_sender:
            pytest.fail(
                "Not sent: an earlier loopback test found Mail's default "
                f"account sending as {_wrong_default_sender[0]!r}."
            )
        trash.sent(subject)

    def receive(self, trash: MailTrash, subject: str) -> tuple[SentCopy, Arrival]:
        """The Sent copy of ``subject``, checked to be from the test
        account, and then its delivered copy, registered for Trash."""
        sent = sent_copy(self.connector, subject)
        sender = header(sent.headers, "From") or ""
        if self.account_address.lower() not in sender.lower():
            _wrong_default_sender.append(sender)
            pytest.fail(
                f"The Sent copy of {subject!r} is From {sender!r}, not the test "
                f"account's {self.account_address!r}. A fresh send cannot name "
                "its sender on this version, so Mail's default account sent "
                "it: the test account must be Mail.app's default account for "
                "the loopback tests."
            )
        arrival = wait_for_arrival(self.connector, self.account, sent.rfc_message_id)
        return sent, trash.arrived(arrival)

    def assert_delivered(self, arrival: Arrival, sent: SentCopy, subject: str) -> None:
        """What every delivered copy carries: the test account as sender,
        the loopback as recipient, the subject as sent, and the same
        Message-ID as the Sent copy."""
        assert self.account_address.lower() in (header(arrival.headers, "From") or "").lower()
        assert self.address.lower() in (header(arrival.headers, "To") or "").lower()
        assert header(arrival.headers, "Subject") == subject
        delivered_id = bare_message_id(header(arrival.headers, "Message-Id") or "")
        assert delivered_id == sent.rfc_message_id


@pytest.fixture
def loop(
    loopback_address: str, test_account: str, test_account_address: str
) -> Loopback:
    return Loopback(
        connector=AppleMailConnector(timeout=90),
        account=test_account,
        account_address=test_account_address,
        address=loopback_address,
    )


def test_fresh_html_arrives_intact(loop: Loopback) -> None:
    hexid = _hex()
    subject = f"{PREFIX}fresh-html-{hexid}"
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, subject)
        result = loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=subject,
            body=f"<p>loopback read-back <b>marker-{hexid}</b></p>",
            from_account=None,
        )
        assert result == SENT
        sent, arrival = loop.receive(trash, subject)

        loop.assert_delivered(arrival, sent, subject)
        assert_html_rendered(arrival.source, f"<b>marker-{hexid}</b>")
        assert arrival.attachment_count == 0
        assert arrival.attachment_names == ()
        assert f"marker-{hexid}" in arrival.content


def test_fresh_html_with_cc_and_attachments_arrives_intact(
    loop: Loopback, tmp_path: Path
) -> None:
    hexid = _hex()
    subject = f"{PREFIX}html-attachments-{hexid}"
    files = _two_files(tmp_path, hexid)
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, subject)
        result = loop.connector._send_html_email(
            to=[loop.address], cc=["ccprobe@example.com"], bcc=None,
            subject=subject,
            body=f"<p>loopback attachments <b>marker-{hexid}</b></p>",
            from_account=None, attachment_paths=files,
        )
        assert result == SENT
        sent, arrival = loop.receive(trash, subject)

        loop.assert_delivered(arrival, sent, subject)
        assert sorted(arrival.attachment_names) == ["first.txt", "second.txt"]
        assert arrival.attachment_count == 2
        assert _attachment_bytes(arrival.source) == {
            f.name: f.read_bytes() for f in files
        }
        assert "ccprobe@example.com" in (header(arrival.headers, "Cc") or "")
        assert_html_rendered(arrival.source, f"<b>marker-{hexid}</b>")


def test_plain_fresh_send_via_draft_path_arrives_intact(loop: Loopback) -> None:
    """``create_draft(seed="new", send_now=True)`` sends through the
    mailto: path (``_send_new_via_eml``), which exists so the body is not
    wrapped in ``<blockquote type="cite">``: iOS Mail draws that as a
    purple quoted-reply bar."""
    hexid = _hex()
    subject = f"{PREFIX}plain-eml-{hexid}"
    body = f"plain <text> & symbols marker-{hexid}"
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, subject)
        result = loop.connector.create_draft(
            seed="new", to=[loop.address], subject=subject, body=body,
            send_now=True,
        )
        assert result == SENT
        sent, arrival = loop.receive(trash, subject)

        loop.assert_delivered(arrival, sent, subject)
        assert body in arrival.content
        assert '<blockquote type="cite">' not in arrival.source
        assert 'type="cite"' not in html_part(arrival.source)


def test_html_reply_threads_at_the_receiver(loop: Loopback) -> None:
    hexid = _hex()
    seed_subject = f"{PREFIX}reply-seed-{hexid}"
    reply_subject = f"Re: {seed_subject}"
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, seed_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=seed_subject,
            body=f"<p>reply seed <b>seed-marker-{hexid}</b></p>",
            from_account=None,
        ) == SENT
        seed_sent, seed = loop.receive(trash, seed_subject)

        loop.prepare(trash, reply_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject="",
            body=f"<p><i>reply-marker-{hexid}</i></p>",
            from_account=None, reply_to=seed.mail_id,
        ) == SENT
        reply_sent, reply = loop.receive(trash, reply_subject)

        loop.assert_delivered(reply, reply_sent, reply_subject)
        in_reply_to = bare_message_id(header(reply.headers, "In-Reply-To") or "")
        assert in_reply_to == seed_sent.rfc_message_id
        assert seed_sent.rfc_message_id in (header(reply.headers, "References") or "")
        assert_html_rendered(reply.source, f"<i>reply-marker-{hexid}</i>")
        html = html_part(reply.source)
        reply_at = html.find(f"reply-marker-{hexid}")
        quoted_at = html.find(f"seed-marker-{hexid}")
        assert reply_at >= 0, "the reply's own text is not in its HTML part"
        assert quoted_at >= 0, "the quoted seed is not in the reply's HTML part"
        assert reply_at < quoted_at, "the reply is below the quote, not above it"


def test_forward_via_draft_path_carries_original_and_attachments(
    loop: Loopback, tmp_path: Path
) -> None:
    """A forward is the original message passed on: Mail's quoted header
    block and the original's attachments, below whatever the sender
    adds. A "forward" that pastes text into a fresh message was rejected
    once already; this test fails on one."""
    hexid = _hex()
    seed_subject = f"{PREFIX}forward-seed-{hexid}"
    forward_subject = f"Fwd: {seed_subject}"
    files = _two_files(tmp_path, hexid)
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, seed_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=seed_subject,
            body=f"<p>forward seed <b>seed-marker-{hexid}</b></p>",
            from_account=None, attachment_paths=files,
        ) == SENT
        _, seed = loop.receive(trash, seed_subject)
        assert seed.attachment_count == 2, "the seed itself arrived without both files"

        trash.windows(forward_subject)
        loop.prepare(trash, forward_subject)
        assert loop.connector.create_draft(
            seed="forward", seed_id=seed.mail_id, to=[loop.address],
            body=f"forward-marker-{hexid}", send_now=True,
        ) == SENT
        assert compose_window_count(loop.connector, forward_subject) == 0, (
            "the send left its compose window open"
        )
        forward_sent, forward = loop.receive(trash, forward_subject)

        loop.assert_delivered(forward, forward_sent, forward_subject)
        note_at = forward.content.find(f"forward-marker-{hexid}")
        block_at = forward.content.find("Begin forwarded message")
        assert note_at >= 0, "the forward's own text did not arrive"
        assert block_at >= 0, "Mail's forwarded-message block did not arrive"
        assert note_at < block_at, "the note is below the forwarded message"
        assert seed_subject in forward.content
        assert loop.account_address in forward.content
        assert sorted(forward.attachment_names) == sorted(seed.attachment_names)
        assert forward.attachment_count == 2


def test_reply_via_draft_path_puts_the_note_above_the_quote(loop: Loopback) -> None:
    """``create_draft(seed="reply", body=...)``: the caller's text goes
    above the quoted original, which stays."""
    hexid = _hex()
    seed_subject = f"{PREFIX}draft-reply-seed-{hexid}"
    reply_subject = f"Re: {seed_subject}"
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, seed_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=seed_subject,
            body=f"<p>draft reply seed <b>seed-marker-{hexid}</b></p>",
            from_account=None,
        ) == SENT
        seed_sent, seed = loop.receive(trash, seed_subject)

        trash.windows(reply_subject)
        loop.prepare(trash, reply_subject)
        assert loop.connector.create_draft(
            seed="reply", seed_id=seed.mail_id, to=[loop.address],
            body=f"reply-note-{hexid}", send_now=True,
        ) == SENT
        assert compose_window_count(loop.connector, reply_subject) == 0, (
            "the send left its compose window open"
        )
        reply_sent, reply = loop.receive(trash, reply_subject)

        loop.assert_delivered(reply, reply_sent, reply_subject)
        in_reply_to = bare_message_id(header(reply.headers, "In-Reply-To") or "")
        assert in_reply_to == seed_sent.rfc_message_id
        note_at = reply.content.find(f"reply-note-{hexid}")
        quoted_at = reply.content.find(f"seed-marker-{hexid}")
        assert note_at >= 0, "the reply's own text did not arrive"
        assert quoted_at >= 0, "the quoted original did not arrive"
        assert note_at < quoted_at, "the note is below the quote"


def test_a_saved_forward_note_sits_above_the_forwarded_message(
    loop: Loopback, tmp_path: Path
) -> None:
    """``create_draft(seed="forward", body=..., send_now=False)``: the saved
    draft holds the note above Mail's forwarded-message block, keeps both
    of the original's files, and leaves no compose window open. Nothing
    is sent; the seed is the only send."""
    hexid = _hex()
    seed_subject = f"{PREFIX}saved-forward-seed-{hexid}"
    forward_subject = f"Fwd: {seed_subject}"
    files = _two_files(tmp_path, hexid)
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, seed_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=seed_subject,
            body=f"<p>saved forward seed <b>seed-marker-{hexid}</b></p>",
            from_account=None, attachment_paths=files,
        ) == SENT
        _, seed = loop.receive(trash, seed_subject)
        assert seed.attachment_count == 2, "the seed itself arrived without both files"

        trash.windows(forward_subject)
        trash.drafts(forward_subject)
        created = loop.connector.create_draft(
            seed="forward", seed_id=seed.mail_id, to=[loop.address],
            body=f"saved-note-{hexid}", send_now=False,
        )
        assert created["draft_id"], created
        assert compose_window_count(loop.connector, forward_subject) == 0, (
            "the save left its compose window open"
        )
        state = loop.connector.get_draft_state(created["draft_id"])

        assert state["subject"] == forward_subject
        assert [a.lower() for a in state["to"]] == [loop.address.lower()]
        note_at = state["body"].find(f"saved-note-{hexid}")
        block_at = state["body"].find("Begin forwarded message")
        assert note_at >= 0, "the note is not in the saved draft"
        assert block_at >= 0, "Mail's forwarded-message block is not in the saved draft"
        assert note_at < block_at, "the note is below the forwarded message"
        assert state["body"].count(f"seed-marker-{hexid}") == 1
        assert sorted(state["attachment_names"]) == sorted(seed.attachment_names)


def test_forward_without_note_via_tool_lifecycle_carries_original_and_attachments(
    loop: Loopback, tmp_path: Path
) -> None:
    """``draft_create(forward_of=...)`` then ``draft_send``, no note: what
    the recipient gets is the forwarded message, whole.

    Observed 2026-09-26. Before the connector's note-above-seed fix, the
    recreate pasted the saved draft's text over Mail's forward: both
    files arrived, but the original's text arrived twice, once unquoted on
    top and once inside a cite blockquote, with Mail's forward formatting
    gone. After it, the recreate carries that text as a note, which opens
    a compose window named like the one the dictionary save left open,
    and draft_send refuses with COMPOSE_WINDOW_NOT_UNIQUE."""
    from apple_mail_mcp.tools.drafts import draft_create, draft_send

    hexid = _hex()
    seed_subject = f"{PREFIX}lifecycle-forward-seed-{hexid}"
    forward_subject = f"Fwd: {seed_subject}"
    files = _two_files(tmp_path, hexid)
    with MailTrash(loop.connector, loop.account) as trash:
        loop.prepare(trash, seed_subject)
        assert loop.connector._send_html_email(
            to=[loop.address], cc=None, bcc=None, subject=seed_subject,
            body=f"<p>lifecycle seed <b>seed-marker-{hexid}</b></p>",
            from_account=None, attachment_paths=files,
        ) == SENT
        seed_sent, seed = loop.receive(trash, seed_subject)
        assert seed.attachment_count == 2, "the seed itself arrived without both files"

        trash.windows(forward_subject)
        trash.drafts(forward_subject)
        loop.prepare(trash, forward_subject)
        created = draft_create(forward_of=seed.mail_id, to=[loop.address])
        assert created["success"] is True, created
        result = asyncio.run(draft_send(draft_id=created["draft_id"]))
        assert result["success"] is True, result
        forward_sent, forward = loop.receive(trash, forward_subject)

        loop.assert_delivered(forward, forward_sent, forward_subject)
        assert seed_sent.rfc_message_id in (header(forward.headers, "References") or "")
        assert forward.content.count(f"seed-marker-{hexid}") == 1, (
            "the original's text arrived more than once"
        )
        assert "Begin forwarded message" in forward.content
        assert seed_subject in forward.content
        assert loop.account_address in forward.content
        assert sorted(forward.attachment_names) == sorted(seed.attachment_names)
        assert forward.attachment_count == 2


def test_draft_create_then_send_arrives_intact(loop: Loopback) -> None:
    """The two-step lifecycle: save a draft, then send it by id. There is
    no single connector call that sends a saved draft; the ``draft_send``
    tool reads the draft back, gates it, sends a rebuilt copy and retires
    the draft. So this calls the tool itself, which also puts the
    test-mode gate's loopback admission on the live path."""
    from apple_mail_mcp.tools.drafts import draft_send

    hexid = _hex()
    subject = f"{PREFIX}draft-send-{hexid}"
    body = f"draft lifecycle marker-{hexid}"
    with MailTrash(loop.connector, loop.account) as trash:
        trash.drafts(subject)
        loop.prepare(trash, subject)
        created = loop.connector.create_draft(
            seed="new", to=[loop.address], subject=subject, body=body,
            send_now=False,
        )
        assert created["draft_id"]
        result = asyncio.run(draft_send(draft_id=created["draft_id"]))
        assert result["success"] is True, result
        sent, arrival = loop.receive(trash, subject)

        loop.assert_delivered(arrival, sent, subject)
        assert body in arrival.content
