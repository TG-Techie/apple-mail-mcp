"""Tests for the v2 drafts MCP surface (draft_create, draft_update,
draft_delete, draft_send) — verb-split design.

The lifecycle:
    draft_create(...) → draft_id
    [draft_update(draft_id, ...) → new draft_id]
    draft_send(draft_id) → sent_message_id

The split exists so the outbound policy gate sits on a single, obvious
tool — draft_send — and so failed sends leave the draft intact for
review.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_mail(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the AppleMailConnector singleton used by server.py."""
    from apple_mail_mcp import server as server_mod

    m = MagicMock()
    monkeypatch.setattr(server_mod, "mail", m)
    return m


@pytest.fixture
def isolated_drafts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Redirect APPLE_MAIL_MCP_HOME so draft state writes don't bleed
    between tests."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))


class TestDraftCreate:
    def test_returns_draft_id_does_not_send(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "ABCD", "sent_message_id": ""
        }
        result = draft_create(
            to=["alice@example.com"], subject="hi", body="body"
        )
        assert result["success"] is True
        assert result["draft_id"] == "ABCD"
        # Critical: saving a draft never names a send to the connector.
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert "send_now" not in kwargs

    def test_offlist_recipients_allowed_on_draft(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drafts to off-list recipients must save successfully — the
        gate only fires at send-time."""
        # Use only the hardcoded allowlist (no env additions).
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "X1", "sent_message_id": ""
        }
        # @other.com is NOT allowlisted; this should still save.
        result = draft_create(
            to=["random@other.com"], subject="hi", body="x"
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert "send_now" not in kwargs


class TestDraftCreateAttachmentsAreCheckedLikeASend:
    """A draft is a file handed to Mail as much as a send is: the same
    three checks (exists, no executable extension, under 25MB) run before
    the connector is reached. Before this the draft path only checked
    existence, and only inside the connector."""

    def test_missing_file_fails_before_connector(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        result = draft_create(
            to=["alice@example.com"], subject="x", body="b",
            attachment_paths=["/nonexistent/nope.pdf"],
        )
        assert result["success"] is False
        assert result["error_type"] == "file_not_found"
        mock_mail.create_draft.assert_not_called()

    def test_blocked_extension_fails_before_connector(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        f = tmp_path / "installer.exe"
        f.write_bytes(b"MZ")
        result = draft_create(
            to=["alice@example.com"], subject="x", body="b",
            attachment_paths=[str(f)],
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "installer.exe" in result["error"]
        mock_mail.create_draft.assert_not_called()

    def test_oversize_fails_before_connector(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any,
        monkeypatch: Any,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        f = tmp_path / "big.bin"
        f.write_bytes(b"x")
        real_stat = type(f).stat

        def fake_stat(self, **kw):  # noqa: ANN001, ANN003
            st = real_stat(self, **kw)
            if self.name == "big.bin":
                import os
                fake = list(st)
                fake[6] = 26 * 1024 * 1024  # st_size
                return os.stat_result(fake)
            return st

        monkeypatch.setattr(type(f), "stat", fake_stat)
        result = draft_create(
            to=["alice@example.com"], subject="x", body="b",
            attachment_paths=[str(f)],
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "25" in result["error"]
        mock_mail.create_draft.assert_not_called()

    def test_an_ordinary_file_still_reaches_the_connector(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from apple_mail_mcp.tools.drafts import draft_create

        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        mock_mail.create_draft.return_value = {
            "draft_id": "ABCD", "sent_message_id": ""
        }
        result = draft_create(
            to=["alice@example.com"], subject="x", body="b",
            attachment_paths=[str(f)],
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["attachment_paths"] == [Path(str(f))]


class TestDraftUpdateAttachmentsAreCheckedLikeASend:
    """Caller-supplied replacement attachments get the same checks as
    draft_create, and they run before the existing draft is deleted, so
    a refused update leaves the draft exactly as it was. Attachments
    carried over from the existing draft (attachment_paths=None) are
    Mail's state rather than caller input and are not re-checked."""

    _STATE = {
        "draft_id": "OLD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
    }

    def test_blocked_extension_is_refused_and_the_draft_is_untouched(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        f = tmp_path / "payload.sh"
        f.write_text("#!/bin/sh\n")
        result = draft_update(draft_id="OLD", attachment_paths=[str(f)])
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "payload.sh" in result["error"]
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()

    def test_missing_file_is_refused_and_the_draft_is_untouched(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        result = draft_update(
            draft_id="OLD", attachment_paths=["/nonexistent/nope.pdf"]
        )
        assert result["success"] is False
        assert result["error_type"] == "file_not_found"
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()

    def test_clearing_attachments_needs_no_files(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": ""
        }
        result = draft_update(draft_id="OLD", attachment_paths=[])
        assert result["success"] is True
        assert mock_mail.create_draft.call_args.kwargs["attachment_paths"] == []

    def test_carried_over_attachments_are_not_rechecked(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from apple_mail_mcp.tools.drafts import draft_update

        state = dict(self._STATE)
        state["attachment_names"] = ["old.exe"]
        mock_mail.get_draft_state.return_value = state
        extracted = [Path(tmp_path / "old.exe")]
        mock_mail.extract_draft_attachments.return_value = extracted
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": ""
        }
        result = draft_update(draft_id="OLD", body="revised")
        assert result["success"] is True
        assert mock_mail.create_draft.call_args.kwargs["attachment_paths"] == extracted


class TestAFreshSendCannotChooseTheSender:
    """A fresh message sent immediately goes out through Mail's mailto:
    handler, which composes from Mail's default account and offers no way
    to pick another, and carries no attachments. Until now from_account
    was accepted on those paths and silently ignored: the mail went out
    from the wrong account and the call reported success. Now
    email_send_html refuses it before anything is composed, and
    draft_send refuses a fresh draft with attachments before anything is
    deleted or put in front of the user to confirm. Replies set the
    sender on the outgoing message and keep honouring it; a saved draft
    keeps its sender for a human to send from Mail.app."""

    _FRESH_STATE = {
        "draft_id": "OLD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
    }

    @pytest.mark.asyncio
    async def test_email_send_html_fresh_is_refused_before_compose(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            to=["alice@example.com"], subject="x", body="<p>b</p>",
            from_account="Work",
        )
        assert result["success"] is False
        assert result["error_type"] == "from_account_unsupported"
        assert "from_account" in result["error"]
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_send_html_reply_still_honours_it(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        mock_mail._send_html_email.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        result = await email_send_html(
            to=["alice@example.com"], body="<p>b</p>",
            reply_to="msg-1", from_account="Work",
        )
        assert result["success"] is True
        assert mock_mail._send_html_email.call_args.kwargs["from_account"] == "Work"

    @pytest.mark.asyncio
    async def test_draft_send_fresh_with_attachments_is_refused_before_the_prompt(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other limit of the mailto: path. Before this the user
        confirmed the send and the connector then refused it."""
        from apple_mail_mcp.tools.drafts import draft_send

        # Without the allowlist bypass the send would be put to the user.
        monkeypatch.setattr(
            "apple_mail_mcp.tools.send.all_recipients_allowed", lambda r: False
        )
        state = dict(self._FRESH_STATE)
        state["attachment_names"] = ["report.pdf"]
        mock_mail.get_draft_state.return_value = state
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        result = await draft_send(draft_id="OLD", ctx=ctx)
        assert result["success"] is False
        assert result["error_type"] == "attachments_unsupported"
        ctx.elicit.assert_not_called()
        mock_mail.create_draft.assert_not_called()
        mock_mail.delete_draft.assert_not_called()

    def test_draft_create_saved_keeps_the_sender(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "ABCD", "sent_message_id": ""
        }
        result = draft_create(
            to=["alice@example.com"], subject="x", body="b",
            from_account="Work",
        )
        assert result["success"] is True
        assert mock_mail.create_draft.call_args.kwargs["from_account"] == "Work"

    def test_draft_update_saved_keeps_the_sender(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._FRESH_STATE)
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": ""
        }
        result = draft_update(draft_id="OLD", from_account="Work")
        assert result["success"] is True
        assert mock_mail.create_draft.call_args.kwargs["from_account"] == "Work"


class TestDraftUpdateKeepsTheDraftInItsAccount:
    """draft_update is delete-and-recreate. Before this the recreated draft
    was built with the caller's from_account only, so a draft saved from
    account X and then updated without naming X silently moved to Mail's
    default account. Now the draft's own sender, read back from Mail, is
    carried over unless the caller overrides it."""

    _STATE = {
        "draft_id": "OLD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
        "sender": "Agent <agent@icloud.com>",
    }

    def test_carries_the_existing_sender_over(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        result = draft_update(draft_id="OLD", body="revised")
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["from_account"] == "Agent <agent@icloud.com>"

    def test_an_explicit_override_wins(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        draft_update(draft_id="OLD", from_account="Work")
        assert mock_mail.create_draft.call_args.kwargs["from_account"] == "Work"

    def test_a_draft_with_no_sender_recorded_is_left_to_mail(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        state = dict(self._STATE)
        state["sender"] = ""
        mock_mail.get_draft_state.return_value = state
        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        draft_update(draft_id="OLD", body="revised")
        assert mock_mail.create_draft.call_args.kwargs["from_account"] is None

    @pytest.mark.asyncio
    async def test_a_reply_sent_now_keeps_its_sender(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        """Reply sends go through the AppleScript compose path, which sets
        the sender; the carried-over sender reaches it."""
        from apple_mail_mcp.drafts import SeedRecord
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_send

        _get_draft_state_store().set_seed(
            "OLD", SeedRecord(seed_kind="reply", seed_id="msg-1", body="x")
        )
        state = dict(self._STATE)
        state["in_reply_to"] = "<orig@example.com>"
        mock_mail.get_draft_state.return_value = state
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        result = await draft_send(draft_id="OLD", ctx=ctx)
        assert result["success"] is True, result
        assert mock_mail.create_draft.call_args.kwargs["from_account"] == (
            "Agent <agent@icloud.com>"
        )

    @pytest.mark.asyncio
    async def test_a_fresh_draft_sent_now_cannot_carry_it(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        """The mailto: path composes from Mail's default account and has no
        sender to set. The carried-over sender is not passed there — the
        connector would refuse it — and whether that draft's sender matches
        what mailto: will use is not knowable here (DESIGN-QUEUE)."""
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        result = await draft_send(draft_id="OLD", ctx=ctx)
        assert result["success"] is True, result
        assert mock_mail.create_draft.call_args.kwargs["from_account"] is None


class TestDraftUpdate:
    def test_returns_new_draft_id(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = {
            "draft_id": "OLD",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": ""
        }
        result = draft_update(draft_id="OLD", body="revised")
        assert result["success"] is True
        assert result["draft_id"] == "NEW"
        assert result["draft_id"] != "OLD"
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert "send_now" not in kwargs


class TestDraftDelete:
    def test_passthrough(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        result = draft_delete(draft_id="ABCD")
        assert result["success"] is True
        mock_mail.delete_draft.assert_called_once_with("ABCD")


class TestDraftSend:
    @pytest.mark.asyncio
    async def test_blocks_offlist_and_leaves_draft_intact(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The headline guarantee: off-list recipients → draft is
        untouched, no delete, no recreate, no AppleScript send."""
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["evil@other.com"], "cc": [], "bcc": [],
            "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        result = await draft_send(draft_id="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "outbound_disallowed"
        assert "evil@other.com" in result["error"]
        # CRITICAL: no destructive ops were taken.
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_mixed_recipients_intact(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One on-list + one off-list = blocked."""
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["alice@example.com"],
            "cc": ["outsider@other.com"], "bcc": [],
            "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        result = await draft_send(draft_id="ABCD")
        assert result["error_type"] == "outbound_disallowed"
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_full_onlist_send(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All recipients on-list → send proceeds (recreate-send, then
        the old draft is removed)."""
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        # Allowlist accepts → elicit is skipped (Cowork-style flow), so
        # ctx is not called at all even when provided.
        ctx.elicit.return_value = None  # not awaited, no decision
        result = await draft_send(draft_id="ABCD", ctx=ctx)
        assert result["success"] is True
        # The delete-recreate-send path was exercised.
        mock_mail.delete_draft.assert_called_once_with("ABCD")
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["send_now"] is True

    @pytest.mark.asyncio
    async def test_empty_recipients_fail_with_validation_error(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        """A draft with no recipients can't be sent — distinct from
        outbound_disallowed; this is a validation error."""
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": [], "cc": [], "bcc": [],
            "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        result = await draft_send(draft_id="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_draft_returns_error(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        """draft_send on a nonexistent id returns an error from the
        draft-state read, no destructive ops."""
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.side_effect = MailDraftNotFoundError(
            "no draft with id 'GONE'"
        )
        result = await draft_send(draft_id="GONE")
        assert result["success"] is False
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_draft_with_attachments_blocked_intact(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: fresh-seed drafts with attachments cannot be
        auto-sent (the mailto: dispatch path carries no attachments).
        Historically the delete-and-recreate ran anyway: the draft was
        deleted, then the recreate-send raised NotImplementedError —
        destroying the draft (real incident: draft ids 1390/1393,
        2026-08-24). The guard must fire BEFORE any destructive op.
        """
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "1390",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "Debrief excerpt", "body": "see attached",
            "in_reply_to": "", "references": "",
            "attachment_names": ["Debrief_verbatim.txt"],
        }
        result = await draft_send(draft_id="1390")
        assert result["success"] is False
        assert result["error_type"] == "attachments_unsupported"
        # The error must steer to paths that actually work, not to the
        # flow that just failed.
        assert "Mail.app" in result["error"]
        # CRITICAL: the draft survives.
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()
        # No pointless attachment extraction either.
        mock_mail.extract_draft_attachments.assert_not_called()


class TestAReplyOrForwardIsRebuiltFromTheCallersOwnText:
    """draft_update and draft_send rebuild a draft through the connector,
    which puts the body it is given above the original it quotes itself.
    What Mail reads back from a saved reply or forward already carries
    that quote, so handing it back sent the original twice: measured on
    a forward created without a body and then sent, which delivered the
    original's text unquoted at the top, a copy of Mail's forward block,
    and the quoted original below. The body handed back is the caller's
    own text, kept in the draft's record; only a draft with no record
    (created outside this server) falls back to the read-back content."""

    _READ_BACK = (
        "note\n\n---------- Forwarded message ----------\n"
        "From: someone@example.com\n\nthe original text"
    )

    def _state(self, **overrides: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "Fwd: hi", "body": self._READ_BACK,
            "in_reply_to": "", "references": "", "attachment_names": [],
            "account": None, "sender": "",
        }
        state.update(overrides)
        return state

    def _create_forward(self, mock_mail: MagicMock, **kwargs: Any) -> str:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {"draft_id": "D1", "sent_message_id": ""}
        created = draft_create(forward_of="msg-1", to=["alice@example.com"], **kwargs)
        assert created["success"] is True, created
        return str(created["draft_id"])

    async def test_a_forward_created_with_a_note_sends_the_note(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        draft_id = self._create_forward(mock_mail, body="note")
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        result = await draft_send(draft_id=draft_id)
        assert result["success"] is True, result
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "forward"
        assert kwargs["seed_id"] == "msg-1"
        assert kwargs["send_now"] is True
        assert kwargs["body"] == "note"

    async def test_a_forward_created_without_a_body_sends_none(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        draft_id = self._create_forward(mock_mail)
        mock_mail.get_draft_state.return_value = self._state(body="the original text")
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        result = await draft_send(draft_id=draft_id)
        assert result["success"] is True, result
        assert mock_mail.create_draft.call_args.kwargs["body"] == ""

    async def test_an_update_that_leaves_the_body_keeps_the_callers_text(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send, draft_update

        draft_id = self._create_forward(mock_mail, body="note")
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.create_draft.return_value = {"draft_id": "D2", "sent_message_id": ""}
        updated = draft_update(draft_id=draft_id, subject="Fwd: renamed")
        assert updated["success"] is True, updated
        assert mock_mail.create_draft.call_args.kwargs["body"] == "note"

        # The record moved to the new id with the text, so a send from it
        # still hands back only the caller's text.
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        sent = await draft_send(draft_id=updated["draft_id"])
        assert sent["success"] is True, sent
        assert mock_mail.create_draft.call_args.kwargs["body"] == "note"

    def test_an_update_with_a_new_body_records_it(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_update

        draft_id = self._create_forward(mock_mail, body="note")
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.create_draft.return_value = {"draft_id": "D2", "sent_message_id": ""}
        draft_update(draft_id=draft_id, body="revised note")
        assert mock_mail.create_draft.call_args.kwargs["body"] == "revised note"
        seed = _get_draft_state_store().get_seed("D2")
        assert seed is not None
        assert seed.body == "revised note"

    async def test_a_draft_with_no_record_falls_back_to_what_mail_reads_back(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        """Created outside this server: the reply is found by its
        In-Reply-To header, and the only body there is is the read-back."""
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = self._state(
            subject="Re: hi", in_reply_to="<orig@example.com>",
        )
        mock_mail.find_message_by_message_id.return_value = "msg-9"
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        result = await draft_send(draft_id="EXT1")
        assert result["success"] is True, result
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "reply"
        assert kwargs["seed_id"] == "msg-9"
        assert kwargs["body"] == self._READ_BACK

    async def test_a_record_that_predates_the_text_falls_back_too(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.drafts import SeedRecord
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_send

        _get_draft_state_store().set_seed(
            "OLD1", SeedRecord(seed_kind="forward", seed_id="msg-1")
        )
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        result = await draft_send(draft_id="OLD1")
        assert result["success"] is True, result
        assert mock_mail.create_draft.call_args.kwargs["body"] == self._READ_BACK

    async def test_a_fresh_draft_is_sent_with_what_mail_reads_back(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        """A fresh draft quotes nothing, so its read-back content is its
        whole body."""
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = self._state(
            subject="hi", body="the whole body",
        )
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        result = await draft_send(draft_id="F1")
        assert result["success"] is True, result
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "new"
        assert kwargs["body"] == "the whole body"


class TestAReplyOrForwardCarriesOnlyTheCallersAttachments:
    """Mail's forward verb carries the original's attachments itself, so a
    rebuild of a forward that re-attached everything Mail reads back would
    add them a second time. The record keeps the names of the files the
    caller attached, and a rebuild of a reply or forward re-attaches
    exactly those. A fresh draft carries nothing of Mail's, so all its
    attachments are the caller's."""

    def _state(self, **overrides: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "Fwd: hi", "body": "note\n\nforwarded",
            "in_reply_to": "", "references": "",
            "attachment_names": ["original.pdf", "mine.pdf"],
            "account": None, "sender": "",
        }
        state.update(overrides)
        return state

    @staticmethod
    def _extract_as_mail_does(
        draft_id: str, names: list[str], dest: Any
    ) -> list[Any]:
        """The connector saves by position into ``<dest>/<i>/<name>``."""
        out = []
        for i, name in enumerate(names):
            (dest / str(i)).mkdir(parents=True, exist_ok=True)
            path = dest / str(i) / name
            path.write_bytes(b"x")
            out.append(path)
        return out

    def _create_forward(
        self, mock_mail: MagicMock, attachment_paths: list[str]
    ) -> str:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {"draft_id": "D1", "sent_message_id": ""}
        created = draft_create(
            forward_of="msg-1", to=["alice@example.com"], body="note",
            attachment_paths=attachment_paths,
        )
        assert created["success"] is True, created
        return str(created["draft_id"])

    async def test_a_send_re_attaches_only_what_the_caller_added(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        mine = tmp_path / "mine.pdf"
        mine.write_bytes(b"%PDF")
        draft_id = self._create_forward(mock_mail, [str(mine)])
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.extract_draft_attachments.side_effect = self._extract_as_mail_does
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}

        result = await draft_send(draft_id=draft_id)
        assert result["success"] is True, result
        # Saved by position, so every attachment is read out; only the
        # caller's goes back.
        assert mock_mail.extract_draft_attachments.call_args.args[1] == [
            "original.pdf", "mine.pdf",
        ]
        sent_paths = mock_mail.create_draft.call_args.kwargs["attachment_paths"]
        assert [p.name for p in sent_paths] == ["mine.pdf"]

    async def test_a_forward_the_caller_attached_nothing_to_re_attaches_nothing(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        draft_id = self._create_forward(mock_mail, [])
        mock_mail.get_draft_state.return_value = self._state(
            attachment_names=["original.pdf"],
        )
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}

        result = await draft_send(draft_id=draft_id)
        assert result["success"] is True, result
        mock_mail.extract_draft_attachments.assert_not_called()
        assert mock_mail.create_draft.call_args.kwargs["attachment_paths"] is None

    def test_an_update_that_leaves_attachments_carries_only_the_callers(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_update

        mine = tmp_path / "mine.pdf"
        mine.write_bytes(b"%PDF")
        draft_id = self._create_forward(mock_mail, [str(mine)])
        mock_mail.get_draft_state.return_value = self._state()
        mock_mail.extract_draft_attachments.side_effect = self._extract_as_mail_does
        mock_mail.create_draft.return_value = {"draft_id": "D2", "sent_message_id": ""}

        updated = draft_update(draft_id=draft_id, body="revised")
        assert updated["success"] is True, updated
        sent_paths = mock_mail.create_draft.call_args.kwargs["attachment_paths"]
        assert [p.name for p in sent_paths] == ["mine.pdf"]
        seed = _get_draft_state_store().get_seed("D2")
        assert seed is not None
        assert seed.attachment_names == ("mine.pdf",)

    def test_an_update_that_replaces_attachments_records_the_new_ones(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_update

        draft_id = self._create_forward(mock_mail, [])
        other = tmp_path / "other.pdf"
        other.write_bytes(b"%PDF")
        mock_mail.get_draft_state.return_value = self._state(
            attachment_names=["original.pdf"],
        )
        mock_mail.create_draft.return_value = {"draft_id": "D2", "sent_message_id": ""}

        draft_update(draft_id=draft_id, attachment_paths=[str(other)])
        mock_mail.extract_draft_attachments.assert_not_called()
        seed = _get_draft_state_store().get_seed("D2")
        assert seed is not None
        assert seed.attachment_names == ("other.pdf",)

    async def test_an_attachment_that_cannot_be_carried_over_refuses_the_send(
        self, isolated_drafts: None, mock_mail: MagicMock, tmp_path: Any
    ) -> None:
        """Mail no longer lists a file the caller attached under the name
        it was recorded with, or saving it out failed: sending without it
        would drop it quietly, so nothing is sent and the draft stays."""
        from apple_mail_mcp.tools.drafts import draft_send

        mine = tmp_path / "mine.pdf"
        mine.write_bytes(b"%PDF")
        draft_id = self._create_forward(mock_mail, [str(mine)])
        mock_mail.get_draft_state.return_value = self._state(
            attachment_names=["original.pdf", "renamed.pdf"],
        )
        mock_mail.extract_draft_attachments.side_effect = self._extract_as_mail_does

        result = await draft_send(draft_id=draft_id)
        assert result["success"] is False
        assert result["error_type"] == "draft_error"
        assert "mine.pdf" in result["error"]
        mock_mail.create_draft.assert_called_once()  # the draft_create above
        mock_mail.delete_draft.assert_not_called()


class TestAFailureLeavesTheDraftWhereItWas:
    """draft_update and draft_send are delete-and-recreate. Before this the
    old draft was deleted first, so a failure in the recreate or the send
    (Mail timing out, a reply's seed message gone, the sender account no
    longer matching) returned an error while the draft whose id the
    caller still held was sitting in Trash, against what the tool doc
    promised. Now the new message is created (or sent) first and the old
    draft is removed only after that succeeded; a failure leaves it
    untouched, and a removal that fails after the success is reported
    beside the success rather than turning it into an error."""

    _STATE = {
        "draft_id": "ABCD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
    }

    @pytest.fixture(autouse=True)
    def _no_elicitation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )

    @pytest.mark.asyncio
    async def test_a_send_that_fails_leaves_the_draft_in_drafts(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.drafts import SeedRecord
        from apple_mail_mcp.exceptions import MailAppleScriptError
        from apple_mail_mcp.tools.drafts import _get_draft_state_store, draft_send

        _get_draft_state_store().set_seed(
            "ABCD", SeedRecord(seed_kind="reply", seed_id="msg-1")
        )
        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.side_effect = MailAppleScriptError("Mail timed out")
        result = await draft_send(draft_id="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "applescript_error"
        mock_mail.delete_draft.assert_not_called()
        assert _get_draft_state_store().get_seed("ABCD") is not None

    def test_an_update_that_fails_leaves_the_draft_in_drafts(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailMessageNotFoundError
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.side_effect = MailMessageNotFoundError(
            "no message with id 'msg-1'"
        )
        result = draft_update(draft_id="ABCD", body="revised")
        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        mock_mail.delete_draft.assert_not_called()

    def test_the_old_draft_goes_only_after_the_new_one_exists(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "EFGH", "sent_message_id": ""}
        result = draft_update(draft_id="ABCD", body="revised")
        assert result["success"] is True
        assert result["draft_id"] == "EFGH"
        names = [c[0] for c in mock_mail.mock_calls]
        assert names.index("create_draft") < names.index("delete_draft")
        mock_mail.delete_draft.assert_called_once_with("ABCD")
        assert "warning" not in result

    @pytest.mark.asyncio
    async def test_a_removal_that_fails_after_the_send_is_reported(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailAppleScriptError
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        mock_mail.delete_draft.side_effect = MailAppleScriptError("Mail busy")
        result = await draft_send(draft_id="ABCD")
        assert result["success"] is True
        assert "ABCD" in result["warning"]
        assert "Drafts" in result["warning"]
        assert "Mail busy" in result["warning"]

    def test_an_old_draft_already_gone_is_reported_not_failed(
        self, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {"draft_id": "EFGH", "sent_message_id": ""}
        mock_mail.delete_draft.side_effect = MailDraftNotFoundError("gone")
        result = draft_update(draft_id="ABCD", body="revised")
        assert result["success"] is True
        assert result["draft_id"] == "EFGH"
        assert "ABCD" in result["warning"]


class TestAllowlistUnavailableFailClosed:
    """FAIL CLOSED (owner directive 2026-08-24): with no readable comms
    config there is NO fallback list — sends are blocked with the
    distinct allowlist_unavailable error_type, and nothing destructive
    happens."""

    @pytest.mark.asyncio
    async def test_draft_send_blocked_draft_intact(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        monkeypatch.setenv(
            "APPLE_MAIL_MCP_COMMS_CONFIG", "/nonexistent/comms.yaml"
        )
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        result = await draft_send(draft_id="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "allowlist_unavailable"
        assert "comms config" in result["error"]
        mock_mail.delete_draft.assert_not_called()
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_send_html_blocked_nothing_sent(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        monkeypatch.setenv(
            "APPLE_MAIL_MCP_COMMS_CONFIG", "/nonexistent/comms.yaml"
        )
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        result = await email_send_html(
            to=["alice@example.com"], subject="s", body="<p>b</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "allowlist_unavailable"
        mock_mail._send_html_email.assert_not_called()


class TestBothSendsMeetOneOutboundGate:
    """draft_send and email_send_html refuse through the one outbound
    gate at the server layer, so the same recipients get the same answer
    from either, and the answer says nothing went out."""

    UNSENT = " Nothing was sent; the draft, if any, is unchanged."

    async def _send(self, tool: str, to: list[str], mock_mail: MagicMock) -> Any:
        from apple_mail_mcp.tools.drafts import draft_send
        from apple_mail_mcp.tools.send import email_send_html

        if tool == "draft_send":
            mock_mail.get_draft_state.return_value = {
                "draft_id": "ABCD",
                "to": to, "cc": [], "bcc": [],
                "subject": "s", "body": "b",
                "in_reply_to": "", "references": "", "attachment_names": [],
            }
            return await draft_send(draft_id="ABCD")
        return await email_send_html(to=to, subject="s", body="<p>b</p>")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ["draft_send", "email_send_html"])
    async def test_an_off_list_recipient_gets_one_refusal(
        self, tool: str, isolated_drafts: None, mock_mail: MagicMock
    ) -> None:
        result = await self._send(
            tool, ["alice@example.com", "outsider@other.com"], mock_mail
        )
        assert result == {
            "success": False,
            "error": (
                "send blocked — recipients not on outbound allowlist: "
                "'outsider@other.com'." + self.UNSENT
            ),
            "error_type": "outbound_disallowed",
        }
        mock_mail.create_draft.assert_not_called()
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ["draft_send", "email_send_html"])
    async def test_an_unreadable_allowlist_gets_one_refusal(
        self,
        tool: str,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "APPLE_MAIL_MCP_COMMS_CONFIG", "/nonexistent/comms.yaml"
        )
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        result = await self._send(tool, ["alice@example.com"], mock_mail)
        assert result["success"] is False
        assert result["error_type"] == "allowlist_unavailable"
        assert result["error"].startswith("outbound allowlist unavailable:")
        assert result["error"].endswith(self.UNSENT)
        mock_mail.create_draft.assert_not_called()
        mock_mail._send_html_email.assert_not_called()


class TestDraftSendHtml:
    """Tests for the email_send_html MCP tool."""

    @pytest.mark.asyncio
    async def test_attachment_paths_passed_to_connector(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        tmp_path: Any,
    ) -> None:
        """attachment_paths flow through to the connector as Paths."""
        from pathlib import Path

        from apple_mail_mcp.tools.send import email_send_html

        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        mock_mail._send_html_email.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        result = await email_send_html(
            to=["alice@example.com"],
            subject="With attachment",
            body="<p>see attached</p>",
            attachment_paths=[str(f)],
        )
        assert result["success"] is True
        kwargs = mock_mail._send_html_email.call_args.kwargs
        assert kwargs["attachment_paths"] == [Path(str(f))]

    @pytest.mark.asyncio
    async def test_attachment_missing_file_fails_before_connector(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            to=["alice@example.com"],
            subject="x", body="<p>b</p>",
            attachment_paths=["/nonexistent/nope.pdf"],
        )
        assert result["success"] is False
        assert result["error_type"] == "file_not_found"
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_attachment_blocked_extension_fails(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Executable attachments are refused (security checklist)."""
        from apple_mail_mcp.tools.send import email_send_html

        f = tmp_path / "installer.exe"
        f.write_bytes(b"MZ")
        result = await email_send_html(
            to=["alice@example.com"],
            subject="x", body="<p>b</p>",
            attachment_paths=[str(f)],
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "installer.exe" in result["error"]
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_attachment_oversize_fails(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        tmp_path: Any,
        monkeypatch: Any,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        f = tmp_path / "big.bin"
        f.write_bytes(b"x")
        # Don't actually write 25MB — patch the size check's view of it.
        real_stat = type(f).stat

        def fake_stat(self, **kw):  # noqa: ANN001, ANN003
            st = real_stat(self, **kw)
            if self.name == "big.bin":
                import os
                fake = list(st)
                fake[6] = 26 * 1024 * 1024  # st_size
                return os.stat_result(fake)
            return st

        monkeypatch.setattr(type(f), "stat", fake_stat)
        result = await email_send_html(
            to=["alice@example.com"],
            subject="x", body="<p>b</p>",
            attachment_paths=[str(f)],
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "25" in result["error"]
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_attachments_with_reply_to_unsupported(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        tmp_path: Any,
    ) -> None:
        """reply_to + attachments is explicitly unsupported for now —
        clear validation error, no connector call, nothing sent."""
        from apple_mail_mcp.tools.send import email_send_html

        f = tmp_path / "a.txt"
        f.write_text("x")
        result = await email_send_html(
            to=["alice@example.com"],
            body="<p>b</p>",
            reply_to="12345",
            attachment_paths=[str(f)],
        )
        assert result["success"] is False
        assert result["error_type"] == "attachments_unsupported"
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_send_html_calls_connector(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        """Happy path: allowlisted recipient → _send_html_email is called."""
        from apple_mail_mcp.tools.send import email_send_html

        mock_mail._send_html_email.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        result = await email_send_html(
            to=["alice@example.com"],
            subject="Test HTML",
            body="<p>Hello</p>",
        )
        assert result["success"] is True
        assert result["draft_id"] == ""
        assert result["sent_message_id"] == ""
        mock_mail._send_html_email.assert_called_once()
        call_kwargs = mock_mail._send_html_email.call_args.kwargs
        assert call_kwargs["to"] == ["alice@example.com"]
        assert call_kwargs["subject"] == "Test HTML"
        assert call_kwargs["body"] == "<p>Hello</p>"

    @pytest.mark.asyncio
    async def test_email_send_html_blocks_off_allowlist(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: Any,
    ) -> None:
        """Off-list recipient → outbound_disallowed error, connector not called."""
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            to=["random@other.com"],
            subject="Blocked",
            body="<p>x</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "outbound_disallowed"
        mock_mail._send_html_email.assert_not_called()


class TestEmailSendHtmlIsConfinedInTestMode:
    """Under MAIL_TEST_MODE the preferred send tool is held to RFC 2606
    reserved domains like the draft tools, before the connector is
    reached. An allowlisted address on a real domain is the case that
    used to pass: the allowlist admitted it and the test-mode gate never
    looked."""

    @pytest.fixture
    def allowlisted_real_domain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text(
            "email:\n  allowed_outbound:\n"
            "    - '*@example.com'\n"
            "    - '*@partner.com'\n"
        )
        monkeypatch.setenv("APPLE_MAIL_MCP_COMMS_CONFIG", str(cfg))
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        monkeypatch.delenv("MAIL_TEST_LOOPBACK", raising=False)

    @pytest.mark.asyncio
    async def test_an_allowlisted_real_domain_is_refused(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        allowlisted_real_domain: None,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            to=["someone@partner.com"], subject="s", body="<p>b</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        assert "someone@partner.com" in result["error"]
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reply_with_derived_recipients_is_refused(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        allowlisted_real_domain: None,
    ) -> None:
        """Outside test mode the connector reads the derived set back
        from Mail and gates it; in test mode nothing can vouch for it
        before the send, so it must be explicit."""
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            reply_to="12345", body="<p>b</p>",
        )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail._send_html_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reserved_domain_still_sends(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        allowlisted_real_domain: None,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        mock_mail._send_html_email.return_value = {
            "draft_id": "", "sent_message_id": "",
        }
        result = await email_send_html(
            to=["someone@example.com"], subject="s", body="<p>b</p>",
        )
        assert result["success"] is True
        mock_mail._send_html_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_loopback_address_sends(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        allowlisted_real_domain: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MAIL_TEST_LOOPBACK admits one real address, so a read-back
        test can send to a mailbox that delivers back to the test
        account. The allowlist still applies on top: this one is on it."""
        from apple_mail_mcp.tools.send import email_send_html

        monkeypatch.setenv("MAIL_TEST_LOOPBACK", "someone@partner.com")
        mock_mail._send_html_email.return_value = {
            "draft_id": "", "sent_message_id": "",
        }
        result = await email_send_html(
            to=["someone@partner.com"], subject="s", body="<p>b</p>",
        )
        assert result["success"] is True
        mock_mail._send_html_email.assert_called_once()


class TestANamedSenderIsConfinedInTestMode:
    """A draft saved from a named account lands in that account's Drafts,
    and mail sent from one goes out under it. In test mode a caller's
    ``from_account`` must therefore be the test account, as ``account``
    must be for the mailbox and message tools. A sender left to Mail's
    default is not confined here: the fresh send path cannot name one."""

    _STATE = {
        "draft_id": "OLD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
        "sender": "Agent <agent@example.com>",
        "account": "TestAccount",
    }

    @pytest.fixture
    def test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apple_mail_mcp.security import _get_test_account_identifiers

        _get_test_account_identifiers.cache_clear()
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

    def test_draft_create_from_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        result = draft_create(
            to=["alice@example.com"], subject="s", body="b",
            from_account="Other",
        )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()

    def test_draft_create_from_the_test_account_proceeds(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": "",
        }
        result = draft_create(
            to=["alice@example.com"], subject="s", body="b",
            from_account="TestAccount",
        )
        assert result["success"] is True

    def test_draft_update_to_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        result = draft_update(draft_id="OLD", from_account="Other")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()
        mock_mail.delete_draft.assert_not_called()

    def test_draft_update_carrying_its_sender_over_is_not_a_reach(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        """The sender read back from Mail is an address, not an account
        the caller named; the draft is recreated where it already is."""
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = dict(self._STATE)
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": "",
        }
        result = draft_update(draft_id="OLD", body="revised")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_email_send_html_from_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.send import email_send_html

        result = await email_send_html(
            reply_to="12345", to=["alice@example.com"], body="<p>b</p>",
            from_account="Other",
        )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail._send_html_email.assert_not_called()


class TestADraftIdReachesEveryAccountInTestMode:
    """A draft id names a draft in any account, and draft_delete,
    draft_update and draft_send take nothing else. Under MAIL_TEST_MODE
    each now reads the draft's account back from Mail and acts only if
    it is the test account; a draft whose account Mail cannot name is
    not in the test account either. Before, the ids an integration run
    held were the only thing keeping it off a real account's drafts."""

    @staticmethod
    def _state(account: str) -> dict[str, Any]:
        return {
            "draft_id": "OLD",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "", "attachment_names": [],
            "sender": "", "account": account,
        }

    @pytest.fixture
    def test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apple_mail_mcp.security import _get_test_account_identifiers

        _get_test_account_identifiers.cache_clear()
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

    def test_delete_of_a_draft_in_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.return_value = self._state("Work")
        result = draft_delete(draft_id="OLD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.delete_draft.assert_not_called()

    def test_delete_of_a_draft_with_no_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.return_value = self._state("")
        result = draft_delete(draft_id="OLD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.delete_draft.assert_not_called()

    def test_delete_of_a_draft_in_the_test_account_proceeds(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        result = draft_delete(draft_id="OLD")
        assert result["success"] is True
        mock_mail.delete_draft.assert_called_once_with("OLD")

    def test_delete_of_a_missing_draft_is_still_not_found(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.side_effect = MailDraftNotFoundError("no")
        result = draft_delete(draft_id="OLD")
        assert result["success"] is False
        assert result["error_type"] == "draft_not_found"

    def test_outside_test_mode_delete_does_not_care_where_the_draft_is(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        mock_mail.get_draft_state.return_value = self._state("")
        assert draft_delete(draft_id="OLD")["success"] is True

    def test_update_of_a_draft_in_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = self._state("Work")
        result = draft_update(draft_id="OLD", body="revised")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_of_a_draft_in_another_account_is_refused(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = self._state("Work")
        result = await draft_send(draft_id="OLD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()
        mock_mail.delete_draft.assert_not_called()


class TestEachDraftToolAnswersToItsOwnName:
    """Each draft tool reaches the test-mode gate, the rate limiter and
    the audit log under its own name, so an audit entry's ``operation``
    is the tool that was called. What security.py's tables say about
    each name is pinned in test_security.py."""

    _STATE = {
        "draft_id": "OLD",
        "to": ["alice@example.com"], "cc": [], "bcc": [],
        "subject": "hi", "body": "x",
        "in_reply_to": "", "references": "", "attachment_names": [],
        "sender": "", "account": "TestAccount",
    }

    @pytest.fixture(autouse=True)
    def _fresh_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apple_mail_mcp.security import operation_logger

        operation_logger.operations.clear()
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )

    @pytest.fixture
    def test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        # The test account's identifiers are its name alone: no osascript.
        monkeypatch.setattr(
            "apple_mail_mcp.security._get_test_account_identifiers",
            lambda name: frozenset({name}),
        )

    @staticmethod
    def _logged() -> list[tuple[str, str]]:
        from apple_mail_mcp.security import operation_logger

        return [(op["operation"], op["result"]) for op in operation_logger.operations]

    def _state(self, account: str) -> dict[str, Any]:
        return {**self._STATE, "account": account}

    def test_draft_create(self, isolated_drafts: None, mock_mail: MagicMock) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        assert draft_create(to=["alice@example.com"], subject="s", body="b")["success"]
        assert self._logged() == [("draft_create", "success")]

    def test_draft_update(self, isolated_drafts: None, mock_mail: MagicMock) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        assert draft_update(draft_id="OLD", body="revised")["success"]
        assert self._logged() == [("draft_update", "success")]

    def test_draft_delete(self, isolated_drafts: None, mock_mail: MagicMock) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        assert draft_delete(draft_id="OLD")["success"]
        assert self._logged() == [("draft_delete", "success")]

    async def test_draft_send(self, isolated_drafts: None, mock_mail: MagicMock) -> None:
        """The allowlisted send is recorded, then the send itself."""
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        mock_mail.create_draft.return_value = {"draft_id": "", "sent_message_id": ""}
        assert (await draft_send(draft_id="OLD"))["success"]
        assert self._logged() == [
            ("draft_send", "send_allowlisted"), ("draft_send", "success"),
        ]

    def test_draft_create_refused_in_test_mode(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_create

        result = draft_create(
            to=["alice@example.com"], subject="s", body="b", from_account="Other",
        )
        assert result["error_type"] == "safety_violation"
        assert self._logged() == [("draft_create", "safety_violation")]

    def test_draft_update_refused_in_test_mode(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        mock_mail.get_draft_state.return_value = self._state("")
        result = draft_update(draft_id="OLD", body="revised")
        assert "draft_update" in result["error"]
        assert self._logged() == [("draft_update", "safety_violation")]

    def test_draft_delete_refused_in_test_mode(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        mock_mail.get_draft_state.return_value = self._state("")
        result = draft_delete(draft_id="OLD")
        assert "draft_delete" in result["error"]
        assert self._logged() == [("draft_delete", "safety_violation")]

    async def test_draft_send_refused_in_test_mode(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_send

        mock_mail.get_draft_state.return_value = self._state("")
        result = await draft_send(draft_id="OLD")
        assert "draft_send" in result["error"]
        assert self._logged() == [("draft_send", "safety_violation")]

    async def test_draft_send_is_rate_limited_as_a_send(
        self, isolated_drafts: None, mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.security import TIER_LIMITS, rate_limiter
        from apple_mail_mcp.tools.drafts import draft_send

        for _ in range(TIER_LIMITS["sends"][0]):
            assert rate_limiter.check("sends")
        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        result = await draft_send(draft_id="OLD")
        assert result["error_type"] == "rate_limited"
        assert "sends" in result["error"]
        assert self._logged() == [("draft_send", "rate_limited")]
        mock_mail.create_draft.assert_not_called()

    @staticmethod
    def _fill_expensive_ops(leave: int = 0) -> None:
        from apple_mail_mcp.security import TIER_LIMITS, rate_limiter

        for _ in range(TIER_LIMITS["expensive_ops"][0] - leave):
            assert rate_limiter.check("expensive_ops")

    def test_draft_create_is_rate_limited_before_the_test_mode_gate(
        self, isolated_drafts: None, mock_mail: MagicMock, test_mode: None,
    ) -> None:
        """The rate limit is the first gate: with the tier full, a call
        test mode would also refuse is refused as rate-limited."""
        from apple_mail_mcp.tools.drafts import draft_create

        self._fill_expensive_ops()
        result = draft_create(
            to=["alice@example.com"], subject="s", body="b", from_account="Other",
        )
        assert result["error_type"] == "rate_limited"
        assert "expensive_ops" in result["error"]
        assert self._logged() == [("draft_create", "rate_limited")]
        mock_mail.create_draft.assert_not_called()

    def test_draft_update_is_rate_limited_before_the_draft_is_read(
        self, isolated_drafts: None, mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_update

        self._fill_expensive_ops()
        result = draft_update(draft_id="OLD", body="revised")
        assert result["error_type"] == "rate_limited"
        assert "expensive_ops" in result["error"]
        assert self._logged() == [("draft_update", "rate_limited")]
        mock_mail.get_draft_state.assert_not_called()
        mock_mail.create_draft.assert_not_called()

    def test_draft_delete_is_rate_limited_before_the_draft_is_read(
        self, isolated_drafts: None, mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.tools.drafts import draft_delete

        self._fill_expensive_ops()
        result = draft_delete(draft_id="OLD")
        assert result["error_type"] == "rate_limited"
        assert "expensive_ops" in result["error"]
        assert self._logged() == [("draft_delete", "rate_limited")]
        mock_mail.get_draft_state.assert_not_called()
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.parametrize("tool", ["draft_create", "draft_update", "draft_delete"])
    def test_a_call_takes_one_expensive_ops_slot(
        self, tool: str, isolated_drafts: None, mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.security import rate_limiter
        from apple_mail_mcp.tools import drafts

        mock_mail.get_draft_state.return_value = self._state("TestAccount")
        mock_mail.create_draft.return_value = {"draft_id": "NEW", "sent_message_id": ""}
        calls = {
            "draft_create": lambda: drafts.draft_create(
                to=["alice@example.com"], subject="s", body="b",
            ),
            "draft_update": lambda: drafts.draft_update(draft_id="OLD", body="v2"),
            "draft_delete": lambda: drafts.draft_delete(draft_id="OLD"),
        }
        self._fill_expensive_ops(leave=1)
        assert calls[tool]()["success"] is True
        assert rate_limiter.check("expensive_ops") is False
