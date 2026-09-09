"""The MCP tool surface, as one definition shared by every e2e test.

This set used to be copy-pasted into test_mcp_tools.py and
test_stdio_transport.py. Both copies still named the pre-v2 draft tools
(``create_draft``, ``update_draft``, ``delete_draft``) months after
d36adc2 renamed them, and neither copy ever gained ``email_send_html``.
Two hand-maintained copies of the same list drift together and get
noticed separately, so there is one copy now and both tests import it.
"""

from __future__ import annotations

# Every name FastMCP registers. Asserted with strict equality, so a new
# tool fails here until it is listed — that is the point of the test.
EXPECTED_TOOLS = {
    # Discovery
    "list_accounts",
    "list_mailboxes",
    "list_rules",
    "search_messages",
    "get_messages",
    "get_thread",
    # Drafts v2 — verb-split surface (d36adc2). The undecorated
    # create_draft / update_draft / delete_draft functions still exist in
    # server.py for internal use; they are NOT tools and must not appear
    # here.
    "draft_create",
    "draft_update",
    "draft_delete",
    "draft_send",
    # Sending
    "email_send_html",
    # Mutations
    "update_message",
    "save_attachments",
    "create_mailbox",
    "update_mailbox",
    "delete_mailbox",
    "delete_messages",
    # Rule CRUD (#63)
    "create_rule",
    "update_rule",
    "delete_rule",
    # Templates (#30)
    "list_templates",
    "get_template",
    "save_template",
    "delete_template",
    "render_template",
}

# Tools with no entry in INVOCATION_CASES, and why. Some are covered by
# a dedicated test instead of the table; some are not covered yet. They
# are listed rather than left implicit so the gap is visible in the
# source instead of only in a coverage report:
# test_every_tool_has_an_invocation_case asserts that this set and
# INVOCATION_CASES together account for every registered tool.
#
# Shrinking this set is ordinary work. Growing it needs a reason.
NO_INVOCATION_CASE = {
    # Gated on user confirmation or the outbound allowlist; a bare happy
    # path would assert the gate away. Covered by their own tests below
    # instead: TestConfirmationGate and TestOutboundAllowlistGate.
    "delete_mailbox",
    "draft_send",
    "email_send_html",
    # Delete-and-recreate: needs three connector methods stubbed, so it
    # does not fit the single-method table. Covered by
    # TestDraftUpdateInvocation.
    "draft_update",
    # Rule and template tools do not go through the mail connector, so
    # the mock_mail-based table does not fit them. Not yet covered here.
    "create_rule",
    "update_rule",
    "delete_rule",
    "list_templates",
    "get_template",
    "save_template",
    "delete_template",
    "render_template",
}
