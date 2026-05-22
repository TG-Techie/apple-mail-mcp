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
    @pytest.mark.asyncio
    async def test_returns_draft_id_does_not_send(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "ABCD", "sent_message_id": ""
        }
        result = await draft_create(
            to=["jonah@tg-techie.com"], subject="hi", body="body"
        )
        assert result["success"] is True
        assert result["draft_id"] == "ABCD"
        # Critical: send_now=False was passed to the underlying impl.
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["send_now"] is False

    @pytest.mark.asyncio
    async def test_offlist_recipients_allowed_on_draft(
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
        from apple_mail_mcp.server import draft_create

        mock_mail.create_draft.return_value = {
            "draft_id": "X1", "sent_message_id": ""
        }
        # @other.com is NOT allowlisted; this should still save.
        result = await draft_create(
            to=["random@other.com"], subject="hi", body="x"
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["send_now"] is False


class TestDraftUpdate:
    @pytest.mark.asyncio
    async def test_returns_new_draft_id(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import draft_update

        mock_mail.get_draft_state.return_value = {
            "draft_id": "OLD",
            "to": ["jonah@tg-techie.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "NEW", "sent_message_id": ""
        }
        result = await draft_update(draft_id="OLD", body="revised")
        assert result["success"] is True
        assert result["draft_id"] == "NEW"
        assert result["draft_id"] != "OLD"
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["send_now"] is False


class TestDraftDelete:
    def test_passthrough(
        self,
        isolated_drafts: None,
        mock_mail: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import draft_delete

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
        from apple_mail_mcp.server import draft_send

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
        from apple_mail_mcp.server import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["jonah@tg-techie.com"],
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
        """All recipients on-list → send proceeds (delete-recreate-send
        via update_draft path)."""
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST", raising=False
        )
        from apple_mail_mcp.server import draft_send

        mock_mail.get_draft_state.return_value = {
            "draft_id": "ABCD",
            "to": ["jonah@tg-techie.com"], "cc": [], "bcc": [],
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
        from apple_mail_mcp.server import draft_send

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
        from apple_mail_mcp.server import draft_send

        mock_mail.get_draft_state.side_effect = MailDraftNotFoundError(
            "no draft with id 'GONE'"
        )
        result = await draft_send(draft_id="GONE")
        assert result["success"] is False
        mock_mail.delete_draft.assert_not_called()
