"""End-to-end tests for MCP tool registration and invocation.

These tests exercise the full FastMCP dispatch layer in-process: they
enumerate tools via mcp.list_tools() and invoke them via mcp.call_tool().
The mail connector is mocked; no AppleScript runs.

MAIL_TEST_MODE is disabled per-test so the safety gate does not interfere
with mocked dispatch. These tests verify MCP wiring, not safety behavior
(safety is covered by tests/unit/test_security.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import server

from .expected_tools import EXPECTED_TOOLS, NO_INVOCATION_CASE

pytestmark = pytest.mark.e2e



@pytest.fixture(autouse=True)
def _disable_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable MAIL_TEST_MODE so the safety gate does not interfere.

    The connector is mocked, so destructive operations cannot reach Mail.app.
    """
    monkeypatch.setenv("MAIL_TEST_MODE", "false")


@pytest.fixture
def mock_mail(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level mail connector with a MagicMock."""
    mock = MagicMock()
    monkeypatch.setattr(server, "mail", mock)
    return mock


class TestToolRegistration:
    """Verify tools are registered with correct names and schemas."""

    async def test_expected_tool_names_registered(self) -> None:
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS

    async def test_every_tool_has_description(self) -> None:
        tools = await server.mcp.list_tools()
        missing = [t.name for t in tools if not (t.description and t.description.strip())]
        assert not missing, f"tools missing description: {missing}"

    @pytest.mark.parametrize(
        "tool_name,expected_required",
        [
            ("update_message", {"message_ids"}),
            ("draft_delete", {"draft_id"}),
        ],
    )
    async def test_tool_schema_required_fields(
        self, tool_name: str, expected_required: set[str]
    ) -> None:
        tool = await server.mcp.get_tool(tool_name)
        schema = tool.parameters
        assert schema["type"] == "object"
        required = set(schema.get("required", []))
        # Tool may have additional required fields beyond what we check; we
        # only assert the subset that must always be required.
        missing = expected_required - required
        assert not missing, (
            f"{tool_name} missing required fields {missing}; "
            f"actual required: {required}"
        )


# The one rule the mocked connector reports; the rule tools resolve a
# name from this row before they act.
_RULE_ROW = {"index": 1, "name": "Junk filter", "enabled": True}

# Sentinels replaced at test-time with values derived from tmp_path. Needed
# because parametrize is evaluated at collection time and cannot reference
# per-test fixtures directly.
_TMP_DIR = "__TMP_DIR__"
_TMP_FILE = "__TMP_FILE__"


# (tool_name, call_args, connector_method, connector_return_value)
INVOCATION_CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    (
        "list_accounts",
        {},
        "list_accounts",
        [{"id": "UUID-1", "name": "Gmail",
          "email_addresses": ["me@gmail.com"],
          "account_type": "imap", "enabled": True}],
    ),
    (
        "list_rules",
        {},
        "list_rules",
        [{"name": "Junk filter", "enabled": True}],
    ),
    (
        "list_mailboxes",
        {"account": "TestAccount"},
        "list_mailboxes",
        ["INBOX", "Sent"],
    ),
    (
        "search_messages",
        {"account": "TestAccount"},
        "search_messages",
        [],
    ),
    (
        "get_messages",
        {"message_ids": ["msg-1"]},
        "get_message",
        {"id": "msg-1", "subject": "s", "from": "a@example.com"},
    ),
    (
        "get_thread",
        {"message_id": "msg-1"},
        "get_thread",
        [{"id": "msg-1", "subject": "Q3", "sender": "a@b",
          "date_received": "Mon", "read_status": True, "flagged": False}],
    ),
    (
        "draft_create",
        {"to": ["a@example.com"], "subject": "s", "body": "b"},
        "create_draft",
        {"draft_id": "draft-1", "sent_message_id": ""},
    ),
    (
        "draft_delete",
        {"draft_id": "draft-1"},
        "delete_draft",
        True,
    ),
    (
        "update_message",
        {"message_ids": ["msg-1"], "read_status": True},
        "update_message",
        1,
    ),
    (
        "save_attachments",
        {"message_id": "msg-1", "save_directory": _TMP_DIR},
        "save_attachments",
        # The connector returns (saved_count, warnings) — a bare int here
        # made server.py raise "cannot unpack non-iterable int object",
        # so this case asserted nothing about the tool.
        (0, []),
    ),
    (
        "create_mailbox",
        {"account": "TestAccount", "name": "NewBox"},
        "create_mailbox",
        True,
    ),
    (
        "update_mailbox",
        {"account": "TestAccount", "name": "Old", "new_name": "New"},
        "update_mailbox",
        True,
    ),
    (
        "delete_messages",
        {"message_ids": ["msg-1"]},
        "delete_messages",
        1,
    ),
    (
        "create_rule",
        {
            "name": "Junk filter",
            "conditions": [{"type": "from", "operator": "contains", "value": "spam"}],
            "actions": {"move_to": "Junk"},
        },
        "create_rule",
        3,
    ),
]


class TestToolInvocation:
    """Invoke each tool via mcp.call_tool and verify structured response shape."""

    @pytest.mark.parametrize(
        "tool_name,call_args,connector_method,connector_return",
        INVOCATION_CASES,
        ids=lambda p: p if isinstance(p, str) else None,
    )
    async def test_tool_invocation_happy_path(
        self,
        mock_mail: MagicMock,
        tmp_path: Path,
        tool_name: str,
        call_args: dict[str, Any],
        connector_method: str,
        connector_return: Any,
    ) -> None:
        # Materialize tmp_path-dependent sentinels. save_attachments requires
        # the directory to exist; send_email_with_attachments requires each
        # attachment path to exist on disk.
        tmp_file = tmp_path / "attachment.txt"
        resolved_args: dict[str, Any] = {}
        for key, value in call_args.items():
            if value == _TMP_DIR:
                resolved_args[key] = str(tmp_path)
            elif isinstance(value, list) and _TMP_FILE in value:
                tmp_file.write_text("dummy")
                resolved_args[key] = [
                    str(tmp_file) if item == _TMP_FILE else item for item in value
                ]
            else:
                resolved_args[key] = value

        getattr(mock_mail, connector_method).return_value = connector_return

        from apple_mail_mcp.security import operation_logger

        audit_before = len(operation_logger.operations)
        result = await server.mcp.call_tool(tool_name, resolved_args)

        assert result.structured_content is not None
        assert result.structured_content["success"] is True
        assert "error" not in result.structured_content
        getattr(mock_mail, connector_method).assert_called_once()
        # The security checklist: every tool's success path writes an
        # audit entry. A tool that acts and leaves no record is the gap
        # the durable audit log exists to close.
        assert len(operation_logger.operations) == audit_before + 1, (
            f"{tool_name} succeeded without writing an audit entry"
        )


def _tool_names_in_invocation_cases() -> set[str]:
    return {case[0] for case in INVOCATION_CASES}


class TestInvocationCoverage:
    """Every registered tool is either invoked here or exempted by name.

    Without this, adding a tool leaves a silent hole and renaming one
    leaves a stale row. d36adc2 renamed the four draft tools and the e2e
    suite went red for months because nothing tied the two together.
    """

    async def test_every_tool_has_an_invocation_case(self) -> None:
        tools = await server.mcp.list_tools()
        registered = {t.name for t in tools}
        accounted = _tool_names_in_invocation_cases() | NO_INVOCATION_CASE
        assert not registered - accounted, (
            "registered tools with no invocation case and no exemption: "
            f"{sorted(registered - accounted)}"
        )

    async def test_no_case_names_an_unregistered_tool(self) -> None:
        tools = await server.mcp.list_tools()
        registered = {t.name for t in tools}
        named = _tool_names_in_invocation_cases() | NO_INVOCATION_CASE
        assert not named - registered, (
            "invocation cases or exemptions name tools that are not "
            f"registered: {sorted(named - registered)}"
        )


class TestConfirmationGate:
    """A confirmation-gated tool must not proceed without an accepting client.

    mcp.call_tool injects a Context whose elicitation capability is not
    backed by a real client, so the gate is exercised exactly as it is
    against a client that cannot elicit. Pre-#226 this path silently
    proceeded, which is the bypass the assertion pins down.
    """

    async def test_delete_mailbox_blocks_without_confirmation(self, mock_mail: MagicMock) -> None:
        result = await server.mcp.call_tool(
            "delete_mailbox", {"account": "TestAccount", "name": "Empty"}
        )

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "confirmation_required"
        mock_mail.delete_mailbox.assert_not_called()

    async def test_delete_rule_blocks_without_confirmation(self, mock_mail: MagicMock) -> None:
        mock_mail.list_rules.return_value = [_RULE_ROW]
        result = await server.mcp.call_tool("delete_rule", {"rule_index": 1})

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "confirmation_required"
        mock_mail.delete_rule.assert_not_called()

    async def test_update_rule_blocks_without_confirmation_when_it_rewrites_logic(
        self, mock_mail: MagicMock
    ) -> None:
        """Renaming or toggling is reversible and asks nothing; replacing
        the conditions or actions discards state Mail.app cannot give back,
        so that path asks."""
        mock_mail.list_rules.return_value = [_RULE_ROW]
        result = await server.mcp.call_tool(
            "update_rule", {"rule_index": 1, "actions": {"move_to": "Archive"}}
        )

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "confirmation_required"
        mock_mail.update_rule.assert_not_called()

    async def test_delete_template_blocks_without_confirmation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        await server.mcp.call_tool("save_template", {"name": "t", "body": "v1\n"})

        result = await server.mcp.call_tool("delete_template", {"name": "t"})

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "confirmation_required"
        still_there = await server.mcp.call_tool("get_template", {"name": "t"})
        assert still_there.structured_content is not None
        assert still_there.structured_content["success"] is True


class TestOutboundAllowlistGate:
    """The send tools refuse an off-list recipient before touching Mail.

    These run against the real outbound allowlist deliberately: the gate
    is the behaviour worth asserting at the MCP layer, and asserting it
    needs no address on the list and sends nothing. The recipient below
    is an RFC 2606 reserved domain, which is not an allowlisted
    destination.
    """

    OFF_LIST = "not-on-the-allowlist@example.invalid"

    async def test_email_send_html_blocks_off_list_recipient(self, mock_mail: MagicMock) -> None:
        result = await server.mcp.call_tool(
            "email_send_html",
            {"to": [self.OFF_LIST], "subject": "s", "body": "<p>b</p>"},
        )

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] in {
            "outbound_disallowed",
            "allowlist_unavailable",
        }
        mock_mail._send_html_email.assert_not_called()

    async def test_draft_send_blocks_off_list_recipient(self, mock_mail: MagicMock) -> None:
        mock_mail.get_draft_state.return_value = {
            "to": [self.OFF_LIST],
            "cc": [],
            "bcc": [],
            "subject": "s",
            "body": "b",
        }

        result = await server.mcp.call_tool("draft_send", {"draft_id": "draft-1"})

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] in {
            "outbound_disallowed",
            "allowlist_unavailable",
        }
        # The draft must survive a blocked send — the whole reason the
        # gate sits before the delete-and-recreate path.
        mock_mail.delete_draft.assert_not_called()


class TestDraftUpdateInvocation:
    """draft_update is delete-and-recreate, so it needs three connector
    calls stubbed and does not fit the single-method table above."""

    async def test_draft_update_recreates_and_returns_new_id(self, mock_mail: MagicMock) -> None:
        mock_mail.get_draft_state.return_value = {
            "to": ["a@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "old",
            "body": "old",
            "attachment_paths": [],
        }
        mock_mail.delete_draft.return_value = True
        mock_mail.create_draft.return_value = {
            "draft_id": "draft-2",
            "sent_message_id": "",
        }

        result = await server.mcp.call_tool(
            "draft_update", {"draft_id": "draft-1", "body": "revised"}
        )

        body = result.structured_content
        assert body is not None
        assert body["success"] is True
        assert body["draft_id"] == "draft-2"
        mock_mail.create_draft.assert_called_once()


class TestRuleMutationInvocation:
    """update_rule and delete_rule resolve the rule's name through
    list_rules before acting, so they need two connector methods stubbed
    and do not fit the single-method table. The confirmation-free path
    of update_rule is dispatched here; the gated paths are in
    TestConfirmationGate and TestConfirmationAnsweredByARealClient."""

    async def test_update_rule_rename_needs_no_confirmation(self, mock_mail: MagicMock) -> None:
        mock_mail.list_rules.return_value = [_RULE_ROW]
        mock_mail.update_rule.return_value = None

        result = await server.mcp.call_tool(
            "update_rule", {"rule_index": 1, "name": "Junk filter (old)", "enabled": False}
        )

        body = result.structured_content
        assert body is not None
        assert body == {"success": True, "rule_index": 1}
        mock_mail.update_rule.assert_called_once()
        assert mock_mail.update_rule.call_args.kwargs["expected_name"] == "Junk filter"

    async def test_unknown_index_is_a_typed_error(self, mock_mail: MagicMock) -> None:
        mock_mail.list_rules.return_value = [_RULE_ROW]

        result = await server.mcp.call_tool("delete_rule", {"rule_index": 9})

        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "rule_not_found"
        mock_mail.delete_rule.assert_not_called()


class TestTemplateInvocation:
    """The template tools read and write the on-disk store rather than the
    connector, so they do not fit the single-method table. Each is
    dispatched here against an isolated store; delete_template's gate is
    in TestConfirmationGate and TestConfirmationAnsweredByARealClient."""

    @pytest.fixture(autouse=True)
    def _isolated_templates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))

    async def test_list_is_empty_then_shows_what_was_saved(self) -> None:
        empty = await server.mcp.call_tool("list_templates", {})
        assert empty.structured_content == {"success": True, "templates": [], "count": 0}

        await server.mcp.call_tool(
            "save_template", {"name": "t", "body": "Hi {who}\n", "subject": "Re: {orig}"}
        )

        listed = await server.mcp.call_tool("list_templates", {})
        body = listed.structured_content
        assert body is not None
        assert body["count"] == 1
        assert body["templates"][0]["name"] == "t"

    async def test_get_returns_the_saved_template_and_its_placeholders(self) -> None:
        await server.mcp.call_tool(
            "save_template", {"name": "t", "body": "Hi {who}\n", "subject": "Re: {orig}"}
        )

        got = await server.mcp.call_tool("get_template", {"name": "t"})

        body = got.structured_content
        assert body is not None
        assert body["success"] is True
        assert body["subject"] == "Re: {orig}"
        assert body["body"] == "Hi {who}\n"
        assert set(body["placeholders"]) == {"who", "orig"}

    async def test_get_unknown_name_is_a_typed_error(self) -> None:
        got = await server.mcp.call_tool("get_template", {"name": "nope"})

        body = got.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "template_not_found"

    async def test_render_merges_auto_fill_under_caller_vars(self, mock_mail: MagicMock) -> None:
        mock_mail.auto_template_vars.return_value = {"today": "2026-01-01", "who": "auto"}
        await server.mcp.call_tool(
            "save_template", {"name": "t", "body": "Hi {who}, {today}\n", "subject": "s"}
        )

        rendered = await server.mcp.call_tool(
            "render_template", {"name": "t", "vars": {"who": "caller"}}
        )

        body = rendered.structured_content
        assert body is not None
        assert body["success"] is True
        assert body["body"] == "Hi caller, 2026-01-01\n"
        assert body["used_vars"] == {"today": "2026-01-01", "who": "caller"}
        mock_mail.auto_template_vars.assert_called_once_with(None)

    async def test_render_names_the_missing_placeholder(self, mock_mail: MagicMock) -> None:
        mock_mail.auto_template_vars.return_value = {"today": "2026-01-01"}
        await server.mcp.call_tool("save_template", {"name": "t", "body": "Hi {who}\n"})

        rendered = await server.mcp.call_tool("render_template", {"name": "t"})

        body = rendered.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "missing_template_variable"
        assert "who" in body["error"]

    async def test_save_refuses_a_taken_name_until_overwrite_is_named(self) -> None:
        first = await server.mcp.call_tool("save_template", {"name": "t", "body": "v1\n"})
        assert first.structured_content is not None
        assert first.structured_content["created"] is True

        refused = await server.mcp.call_tool("save_template", {"name": "t", "body": "v2\n"})
        assert refused.structured_content is not None
        assert refused.structured_content["success"] is False
        assert refused.structured_content["error_type"] == "template_exists"

        replaced = await server.mcp.call_tool(
            "save_template", {"name": "t", "body": "v2\n", "overwrite": True}
        )
        assert replaced.structured_content is not None
        assert replaced.structured_content == {"success": True, "name": "t", "created": False}

        current = await server.mcp.call_tool("get_template", {"name": "t"})
        assert current.structured_content is not None
        assert current.structured_content["body"] == "v2\n"


class TestConfirmationAnsweredByARealClient:
    """The gate, answered over the wire by a client that can elicit.

    ``TestConfirmationGate`` above only shows that a client which cannot
    elicit is refused. Every failure inside the gate collapses to the same
    ``confirmation_required`` result, so that test cannot tell "the client
    could not answer" from "the server asked a question the framework
    refuses to send". fastmcp 4 removed ``ctx.elicit(message, None)``, and
    with it every confirmation-gated tool became unconfirmable while that
    test stayed green.

    These drive the server through fastmcp's in-memory client with a
    handler that answers the elicitation, so the question actually has to
    be askable and the answer actually has to be read.
    """

    @staticmethod
    def _client(answer: Any) -> Any:
        from fastmcp import Client
        from fastmcp.client.elicitation import ElicitResult

        async def handler(message: str, response_type: Any, params: Any, ctx: Any) -> Any:
            if answer is DECLINE:
                return ElicitResult(action="decline")
            return answer

        return Client(server.mcp, elicitation_handler=handler)

    async def test_accept_true_lets_the_tool_proceed(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_mailbox.return_value = True
        async with self._client(True) as client:
            result = await client.call_tool(
                "delete_mailbox", {"account": "TestAccount", "name": "Empty"}
            )
        body = result.structured_content
        assert body is not None
        assert body["success"] is True, body
        mock_mail.delete_mailbox.assert_called_once()

    async def test_accept_true_lets_delete_rule_proceed(self, mock_mail: MagicMock) -> None:
        mock_mail.list_rules.return_value = [_RULE_ROW]
        mock_mail.delete_rule.return_value = "Junk filter"
        async with self._client(True) as client:
            result = await client.call_tool("delete_rule", {"rule_index": 1})
        body = result.structured_content
        assert body is not None
        assert body == {"success": True, "rule_index": 1, "deleted_name": "Junk filter"}
        mock_mail.delete_rule.assert_called_once_with(1, expected_name="Junk filter")

    async def test_accept_true_lets_delete_template_proceed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        async with self._client(True) as client:
            await client.call_tool("save_template", {"name": "t", "body": "v1\n"})
            result = await client.call_tool("delete_template", {"name": "t"})
            gone = await client.call_tool("get_template", {"name": "t"})
        assert result.structured_content == {"success": True, "name": "t"}
        assert gone.structured_content is not None
        assert gone.structured_content["error_type"] == "template_not_found"

    async def test_accept_false_is_a_decline(self, mock_mail: MagicMock) -> None:
        """A form answered "no" is not a yes. The bool is the answer."""
        async with self._client(False) as client:
            result = await client.call_tool(
                "delete_mailbox", {"account": "TestAccount", "name": "Empty"}
            )
        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "cancelled"
        mock_mail.delete_mailbox.assert_not_called()

    async def test_decline_is_cancelled(self, mock_mail: MagicMock) -> None:
        async with self._client(DECLINE) as client:
            result = await client.call_tool(
                "delete_mailbox", {"account": "TestAccount", "name": "Empty"}
            )
        body = result.structured_content
        assert body is not None
        assert body["success"] is False
        assert body["error_type"] == "cancelled"
        mock_mail.delete_mailbox.assert_not_called()


DECLINE = object()
