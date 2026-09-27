"""Integration tests for the Phase-0 verified send primitives.

Real Mail.app; run via:
    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> pytest tests/integration/test_verified_send.py --run-integration -v

These exist because the failure modes Phase 0 fixes (silent no-op click on
a disabled Send button, false "SENT" without dispatch, clipboard leaks,
silent discard failures) are ALL invisible to mocked unit tests — see
docs/reference/UI_GROUNDING_MAIL_SEND.md for the live observations.

Sends go to RFC 2606 reserved domains (allowed under MAIL_TEST_MODE).
"""

import time
import uuid
from pathlib import Path

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import (
    MailTrash,
    Seed,
    assert_html_rendered,
    bare_message_id,
    compose_window_count,
    earlier_seed,
    header,
    html_part,
    plain_part,
    sent_copy,
    sent_count_for_subject,
    sent_source_for_subject,
)

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run."
)

SENT = {"draft_id": "", "sent_message_id": ""}


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=90)


class TestVerifiedFreshSend:
    def test_send_success_implies_sent_copy(
        self, connector: AppleMailConnector
    ) -> None:
        """A "SENT" result from a fresh plain send must mean a real
        Sent-mailbox copy exists — the exact guarantee the 2026-07-20
        vanished send violated."""
        subject = f"verified-send-int-{uuid.uuid4().hex[:8]}"
        result = connector.create_draft(
            seed="new",
            to=["test@example.com"],
            subject=subject,
            body="phase-0 integration probe",
            send_now=True,
        )
        assert result == {"draft_id": "", "sent_message_id": ""}
        # The verified-send block already polled for the Sent copy before
        # returning SENT; re-read it here independently.
        assert sent_count_for_subject(connector, subject) >= 1


class TestVerifiedHtmlSend:
    def test_html_send_success_implies_sent_copy_and_clipboard_restored(
        self, connector: AppleMailConnector
    ) -> None:
        subject = f"verified-html-int-{uuid.uuid4().hex[:8]}"
        clipboard_sentinel = f"clipboard-sentinel-{uuid.uuid4().hex[:8]}"
        connector._run_applescript(
            f'set the clipboard to "{clipboard_sentinel}"'
        )
        result = connector._send_html_email(
            to=["test@example.com"],
            cc=None,
            bcc=None,
            subject=subject,
            body="<p><b>phase-0</b> html integration probe</p>",
            from_account=None,
        )
        assert result == {"draft_id": "", "sent_message_id": ""}
        assert sent_count_for_subject(connector, subject) >= 1
        assert_html_rendered(
            sent_source_for_subject(connector, subject), "<b>phase-0</b>"
        )
        restored = connector._run_applescript(
            "return (the clipboard as text)"
        ).strip()
        assert restored == clipboard_sentinel


class TestVerifiedHtmlReply:
    def test_html_reply_threads_and_dispatches(
        self, connector: AppleMailConnector
    ) -> None:
        """Reply to a real message: derived recipients pass the gate,
        HTML lands above the quote, dispatch is verified (Re: subject in
        Sent). Uses one of this suite's own earlier sends as the target."""
        target = connector._run_applescript(
            'tell application "Mail" to return (id of first message of '
            'sent mailbox whose subject begins with "verified-send-int-") '
            "as text"
        ).strip()
        assert target, "no verified-send-int-* message found in Sent to reply to"
        orig_subject = connector._run_applescript(
            f'tell application "Mail" to return subject of first message of '
            f'sent mailbox whose id is "{target}"'
        ).strip()
        result = connector._send_html_email(
            to=[],
            cc=None,
            bcc=None,
            subject="",
            body="<p><i>phase-1-3</i> html reply probe</p>",
            from_account=None,
            reply_to=target,
        )
        assert result == {"draft_id": "", "sent_message_id": ""}
        assert sent_count_for_subject(connector, f"Re: {orig_subject}") >= 1
        # The whole point of reply mode: threading headers on the wire.
        headers = connector._run_applescript(
            f'tell application "Mail" to return all headers of first message '
            f'of sent mailbox whose subject is "Re: {orig_subject}"'
        )
        assert "In-Reply-To:" in headers
        assert "References:" in headers
        assert_html_rendered(
            sent_source_for_subject(connector, f"Re: {orig_subject}"),
            "<i>phase-1-3</i>",
        )


class TestVerifiedHtmlReplyAndForwardWithAFile:
    """``_send_html_email(forward_of=...)``, and ``reply_to`` with a file:
    the Sent copy carries the HTML above what Mail wrote, which stays,
    and the caller's file after it.

    Each test sends one message, to test@example.com (example.com takes
    no mail), from the test account. What it answers is a message this
    suite sent earlier (``earlier_seed``), not one it sends first, so a
    run is one send per test and nothing waits on a delivery. What the
    Sent copy holds is printed before it is asserted on, so a miss still
    reports all of it.

    NOT YET RUN."""

    @pytest.fixture
    def seed(self, connector: AppleMailConnector, test_account: str) -> Seed:
        return earlier_seed(connector, test_account, TEST_DRAFT_SUBJECT_PREFIX)

    def test_html_forward_carries_the_original_and_a_file(
        self,
        connector: AppleMailConnector,
        test_account: str,
        seed: Seed,
        tmp_path: Path,
    ) -> None:
        hexid = uuid.uuid4().hex[:8]
        caller = tmp_path / "caller.txt"
        caller.write_text(f"caller {hexid}\n")
        with MailTrash(connector, test_account) as trash:
            subject = trash.sent(f"{TEST_DRAFT_SUBJECT_PREFIX}verified-forward-{hexid}")
            trash.windows(subject)
            result = connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject=subject,
                body=f"<p>forward <b>forward-marker-{hexid}</b></p>",
                from_account=test_account, forward_of=seed.mail_id,
                attachment_paths=[caller],
            )
            print(f"sent {subject!r} at {time.strftime('%H:%M:%S')}: {result}")
            assert result == SENT
            assert compose_window_count(connector, subject) == 0
            forward = sent_copy(connector, subject)
            html = html_part(forward.source)
            marker_at = html.find(f"forward-marker-{hexid}")
            block_at = html.find("Begin forwarded message")
            print(
                f"Sent copy {forward.mail_id}: files {forward.attachment_names}; "
                f"References {header(forward.headers, 'References')!r}; "
                f"note at {marker_at}, forwarded block at {block_at}, "
                f"seed marker after the block: {seed.marker in html[max(block_at, 0):]}"
            )

            assert_html_rendered(forward.source, f"<b>forward-marker-{hexid}</b>")
            assert 0 <= marker_at < block_at, "the HTML is not above the forward"
            assert seed.marker in html[block_at:]
            assert sorted(forward.attachment_names) == sorted(
                [*seed.attachment_names, caller.name]
            )
            assert seed.rfc_message_id in (header(forward.headers, "References") or "")

    def test_html_reply_with_a_file_keeps_the_quote(
        self,
        connector: AppleMailConnector,
        test_account: str,
        seed: Seed,
        tmp_path: Path,
    ) -> None:
        hexid = uuid.uuid4().hex[:8]
        caller = tmp_path / "caller.txt"
        caller.write_text(f"caller {hexid}\n")
        with MailTrash(connector, test_account) as trash:
            subject = trash.sent(f"{TEST_DRAFT_SUBJECT_PREFIX}verified-reply-{hexid}")
            trash.windows(subject)
            result = connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject=subject,
                body=f"<p>reply <b>reply-marker-{hexid}</b></p>",
                from_account=test_account, reply_to=seed.mail_id,
                attachment_paths=[caller],
            )
            print(f"sent {subject!r} at {time.strftime('%H:%M:%S')}: {result}")
            assert result == SENT
            assert compose_window_count(connector, subject) == 0
            reply = sent_copy(connector, subject)
            html = html_part(reply.source)
            marker_at = html.find(f"reply-marker-{hexid}")
            cite_at = html.find('type="cite"')
            quoted = [
                line for line in plain_part(reply.source).splitlines()
                if line.startswith(">")
            ]
            print(
                f"Sent copy {reply.mail_id}: files {reply.attachment_names}; "
                f"In-Reply-To {header(reply.headers, 'In-Reply-To')!r}; "
                f"note at {marker_at}, quote at {cite_at}, "
                f"seed marker in the quote: {seed.marker in html[max(cite_at, 0):]}; "
                f"quoted plain lines: {quoted[:5]}"
            )

            assert_html_rendered(reply.source, f"<b>reply-marker-{hexid}</b>")
            assert 0 <= marker_at < cite_at, "the HTML is not above the quote"
            assert seed.marker in html[cite_at:]
            assert any(seed.marker in line for line in quoted), (
                "the original went out unquoted"
            )
            assert reply.attachment_names == (caller.name,)
            in_reply_to = bare_message_id(header(reply.headers, "In-Reply-To") or "")
            assert in_reply_to == seed.rfc_message_id


class TestDiscardCompose:
    def test_discard_block_closes_compose_window(
        self, connector: AppleMailConnector
    ) -> None:
        """The discard primitive must actually close the window and read
        that fact back (both Mail-dictionary discards fail silently)."""
        subject = f"discard-int-{uuid.uuid4().hex[:8]}"
        connector._run_applescript(
            f'tell application "Mail" to make new outgoing message '
            f'with properties {{subject:"{subject}", content:"x", visible:true}}'
        )
        time.sleep(1)
        block = connector._as_discard_compose_block("discardName")
        out = connector._run_applescript(
            f'set discardName to "{subject}"\n{block}\nreturn discardOutcome'
        ).strip()
        assert out == "DISCARDED"
        still_open = connector._run_applescript(
            f'tell application "System Events" to tell application process "Mail" '
            f'to return (exists window "{subject}") as text'
        ).strip()
        assert still_open == "false"


class TestSalvageCompose:
    def test_salvage_saves_the_window_to_drafts(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """The salvage every failed send ends in, on a window with no
        sheet: close, Save, the window gone and its draft in Drafts.
        Nothing is sent. The branch for Mail's send-error sheet cannot be
        provoked on demand; only the unit tests on the script's text
        cover it."""
        subject = f"{TEST_DRAFT_SUBJECT_PREFIX}salvage-{uuid.uuid4().hex[:8]}"
        with MailTrash(connector, test_account) as trash:
            trash.windows(subject)
            trash.drafts(subject)
            connector._run_applescript(
                f'tell application "Mail" to make new outgoing message '
                f'with properties {{subject:"{subject}", content:"x", visible:true}}'
            )
            time.sleep(1)
            assert connector._salvage_compose_to_draft(subject) == "SALVAGED"
            assert compose_window_count(connector, subject) == 0
            saved = "0"
            for _ in range(20):
                saved = connector._run_applescript(
                    'tell application "Mail" to return (count of (messages of '
                    f'drafts mailbox whose subject is "{subject}")) as text'
                ).strip()
                if saved != "0":
                    break
                time.sleep(0.5)
            assert saved != "0", "the salvaged window's draft never reached Drafts"


class TestHtmlSendWithAttachments:
    def test_fresh_html_with_attachments_end_to_end(
        self, connector: AppleMailConnector, tmp_path
    ) -> None:
        """Fresh HTML send with attachments + cc, end to end against real
        Mail.app: compose via `make new outgoing message`, clipboard-
        injected HTML over the seeded body, the files pasted after it and
        AX-verified, verified send, sent-copy checks for attachment count
        / cc header / rendered HTML. Cleans up the sent copy."""
        subject = f"attach-int-{uuid.uuid4().hex[:8]}"
        f1 = tmp_path / "first.txt"
        f1.write_text("integration attachment one")
        f2 = tmp_path / "second.txt"
        f2.write_text("integration attachment two")
        try:
            result = connector._send_html_email(
                to=["probe@example.com"],
                cc=["ccprobe@example.com"],
                bcc=None,
                subject=subject,
                body="<p>attachment integration <b>marker-ai</b></p>",
                from_account=None,
                attachment_paths=[f1, f2],
            )
            assert result == {"draft_id": "", "sent_message_id": ""}
            assert sent_count_for_subject(connector, subject) == 1
            src = sent_source_for_subject(connector, subject)
            assert "first.txt" in src
            assert "second.txt" in src
            assert_html_rendered(src, "<b>marker-ai</b>")
            # cc must be on the wire (it used to be silently dropped).
            assert "ccprobe@example.com" in src
            count = connector._run_applescript(
                f'tell application "Mail" to return (count of mail attachments '
                f'of (first message of sent mailbox whose subject is "{subject}")) as text'
            ).strip()
            assert count == "2"
        finally:
            connector._run_applescript(
                f'tell application "Mail" to delete (every message of sent '
                f'mailbox whose subject is "{subject}")'
            )
