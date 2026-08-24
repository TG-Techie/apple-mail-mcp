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

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run."
)


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=90)


def _sent_source_for_subject(connector: AppleMailConnector, subject: str) -> str:
    return connector._run_applescript(
        f'tell application "Mail" to return source of first message of '
        f'sent mailbox whose subject is "{subject}"'
    )


def _assert_html_rendered(source: str, tag_fragment: str) -> None:
    """The sent message's html part must contain the REAL tag, not an
    escaped one — on 2026-07-21 mail shipped with &lt;p&gt; because the
    integration suite asserted dispatch but never rendering."""
    html_part = source[source.find("text/html"):]
    assert tag_fragment in html_part, "HTML did not render as HTML"
    escaped = tag_fragment.replace("<", "&lt;").replace(">", "&gt;")
    assert escaped not in html_part, "HTML was pasted as literal text"


def _sent_count_for_subject(connector: AppleMailConnector, subject: str) -> int:
    out = connector._run_applescript(
        f'tell application "Mail" to return (count of (messages of sent mailbox '
        f'whose subject is "{subject}")) as text'
    ).strip()
    return int(out)


class TestVerifiedMailtoSend:
    def test_send_success_implies_sent_copy(
        self, connector: AppleMailConnector
    ) -> None:
        """A "SENT" result must mean a real Sent-mailbox copy exists —
        the exact guarantee the 2026-07-20 vanished send violated."""
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
        assert _sent_count_for_subject(connector, subject) >= 1


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
        assert _sent_count_for_subject(connector, subject) >= 1
        _assert_html_rendered(
            _sent_source_for_subject(connector, subject), "<b>phase-0</b>"
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
        assert _sent_count_for_subject(connector, f"Re: {orig_subject}") >= 1
        # The whole point of reply mode: threading headers on the wire.
        headers = connector._run_applescript(
            f'tell application "Mail" to return all headers of first message '
            f'of sent mailbox whose subject is "Re: {orig_subject}"'
        )
        assert "In-Reply-To:" in headers
        assert "References:" in headers
        _assert_html_rendered(
            _sent_source_for_subject(connector, f"Re: {orig_subject}"),
            "<i>phase-1-3</i>",
        )


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


class TestHtmlSendWithAttachments:
    def test_fresh_html_with_attachments_end_to_end(
        self, connector: AppleMailConnector, tmp_path
    ) -> None:
        """Fresh HTML send with attachments + cc, end to end against real
        Mail.app: compose via `make new outgoing message` (the mailto
        window is not scriptable for attachments), AX-verified attach,
        clipboard-injected HTML, verified send, sent-copy checks for
        attachment count / cc header / rendered HTML. Cleans up the sent
        copy."""
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
            assert _sent_count_for_subject(connector, subject) == 1
            src = _sent_source_for_subject(connector, subject)
            assert "first.txt" in src
            assert "second.txt" in src
            _assert_html_rendered(src, "<b>marker-ai</b>")
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
