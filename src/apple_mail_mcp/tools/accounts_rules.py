"""Account and rule tools.

``list_accounts``, ``list_rules`` and the rule changes: ``create_rule``,
``update_rule`` and ``delete_rule``. A rule that forwards is a standing
send, so its ``forward_to`` meets the outbound allowlist here, before
anything is asked of the user or of Mail.
"""

from typing import Any, cast

from fastmcp import Context

from .. import server
from ..exceptions import MailRuleNotFoundError
from ..outbound_allowlist import assert_forward_targets_allowed
from ..security import check_rate_limit, check_test_mode_safety, operation_logger
from ..server import _confirm_from_threadpool, _in_tool_threadpool, envelope, mcp


@mcp.tool()
@envelope
def list_accounts() -> dict[str, Any]:
    """
    List all configured email accounts in Apple Mail.

    Returns each account's id (UUID), display name, email addresses,
    account type, and enabled state. Account ids are stable across name
    changes; prefer them over names for identifying accounts.

    Returns:
        Dictionary containing the accounts list.

    Example:
        >>> list_accounts()
        {"success": True, "accounts": [
            {"id": "B21B254B-...", "name": "Gmail", "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True}, ...
        ]}
    """
    if refused := check_rate_limit("list_accounts", {}):
        return refused
    accounts = server.mail.list_accounts()
    operation_logger.log_operation("list_accounts", {}, "success")
    return {"success": True, "accounts": accounts, "count": len(accounts)}


@mcp.tool()
@envelope
def list_rules() -> dict[str, Any]:
    """
    List all Mail.app rules (read-only).

    Returns each rule's display name and enabled state. Rule names are NOT
    guaranteed unique — Mail allows duplicates — and rules have no stable
    id via AppleScript. This tool is read-only; mutation (enable/disable,
    create, delete) is tracked as a separate enhancement.

    Returns:
        Dictionary containing the rules list.

    Example:
        >>> list_rules()
        {"success": True, "rules": [
            {"name": "Junk filter", "enabled": True},
            {"name": "News From Apple", "enabled": False}, ...
        ], "count": 2}
    """
    if refused := check_rate_limit("list_rules", {}):
        return refused
    rules = server.mail.list_rules()
    operation_logger.log_operation("list_rules", {}, "success")
    return {"success": True, "rules": rules, "count": len(rules)}


def _resolve_rule_name(rule_index: int) -> str:
    """Look up a rule's name from its 1-based index via list_rules.

    Used by the rule mutation tools to feed the safety gate and the
    confirmation prompt. Raises ``MailRuleNotFoundError`` when there is
    no rule at the index.
    """
    for r in server.mail.list_rules():
        if r.get("index") == rule_index:
            return cast(str, r.get("name", ""))
    raise MailRuleNotFoundError(f"No rule at index {rule_index}")


@_in_tool_threadpool
@mcp.tool()
@envelope
def delete_rule(
    rule_index: int,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Delete a Mail.app rule by 1-based positional index.

    Destructive — requires user confirmation via MCP elicitation before
    running. Cannot be undone (Mail.app does not version rule history).

    Args:
        rule_index: 1-based positional index from list_rules.

    Returns:
        Dictionary with success status and the deleted rule's name.

    Note:
        After deletion, downstream rule indices shift down by one. Re-call
        list_rules before any further rule operations.
    """
    if refused := check_rate_limit("delete_rule", {"rule_index": rule_index}):
        return refused
    rule_name = _resolve_rule_name(rule_index)
    if refused := check_test_mode_safety("delete_rule", rule_name=rule_name):
        return refused
    summary = (
        f"Delete Mail.app rule '{rule_name}' (index {rule_index})? "
        f"This cannot be undone."
    )
    if refused := _confirm_from_threadpool(
        ctx, summary, "delete_rule", {"rule_index": rule_index}
    ):
        return refused

    # Bound to what was confirmed: the connector checks the name at
    # the index inside the same AppleScript call as the delete, so a
    # rule that moved while the prompt was open is not the one acted
    # on — nothing is, and the caller is told to re-list.
    deleted = server.mail.delete_rule(rule_index, expected_name=rule_name)
    operation_logger.log_operation(
        "delete_rule",
        {"rule_index": rule_index, "deleted_name": deleted},
        "success",
    )
    return {"success": True, "rule_index": rule_index, "deleted_name": deleted}


def _rule_policy_gate(
    operation: str,
    rule_name: str,
    actions: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The policy gates a rule mutation passes before anything is asked
    of the user or of Mail: the test-mode gate, which sees the rule's
    name and, as recipients, whatever it forwards to; then the outbound
    allowlist over those same targets. A forwarding rule is a standing
    send, so it answers with the errors a blocked send gets. The
    connector re-checks the allowlist; this is the fail-fast in front of
    the confirmation prompt. Returns the test-mode refusal or None; an
    allowlist refusal is raised, as the connector's own would be.
    """
    targets: list[str] = list((actions or {}).get("forward_to") or [])
    if refused := check_test_mode_safety(
        operation, rule_name=rule_name, recipients=targets,
    ):
        return refused
    if targets:
        assert_forward_targets_allowed(targets)
    return None


@mcp.tool()
@envelope
def create_rule(
    name: str,
    conditions: list[dict[str, Any]],
    actions: dict[str, Any],
    match_logic: str = "all",
    enabled: bool = True,
) -> dict[str, Any]:
    """
    Create a new Mail.app rule.

    Additive — no confirmation prompt. Mail.app appends new rules to the
    end of the rule list, so the returned ``rule_index`` equals the new
    total rule count.

    Args:
        name: Rule display name. Need not be unique.
        conditions: List of condition dicts (at least one required). Each:
            - field: 'from' | 'to' | 'subject' | 'body' | 'any_recipient' |
                'header_name'
            - operator: 'contains' | 'does_not_contain' | 'begins_with' |
                'ends_with' | 'equals'
            - value: substring or value to match
            - header_name: required iff field == 'header_name'
        actions: Dict with at least one truthy entry from:
            - move_to: {"account": str, "mailbox": str}
            - copy_to: {"account": str, "mailbox": str}
            - mark_read: bool
            - mark_flagged: bool (with optional flag_color enum)
            - flag_color: 'none' | 'red' | 'orange' | 'yellow' | 'green' |
                'blue' | 'purple' | 'gray'
            - delete: bool
            - forward_to: list[str] of email addresses, each on the
              outbound allowlist (a forwarding rule is a standing
              send; an off-list target is refused with
              ``outbound_disallowed`` and nothing is installed)
        match_logic: 'all' (AND across conditions) or 'any' (OR). Default 'all'.
        enabled: Whether the rule is enabled on creation. Default True.

    Returns:
        Dictionary with success status, rule_index, and name.
    """
    if refused := check_rate_limit("create_rule", {"name": name}):
        return refused
    if refused := _rule_policy_gate("create_rule", name, actions):
        return refused
    new_index = server.mail.create_rule(
        name=name,
        conditions=conditions,
        actions=actions,
        match_logic=match_logic,
        enabled=enabled,
    )
    operation_logger.log_operation(
        "create_rule",
        {
            "name": name,
            "rule_index": new_index,
            "conditions": conditions,
            "actions": actions,
            "match_logic": match_logic,
            "enabled": enabled,
        },
        "success",
    )
    return {"success": True, "rule_index": new_index, "name": name}


@_in_tool_threadpool
@mcp.tool()
@envelope
def update_rule(
    rule_index: int,
    name: str | None = None,
    enabled: bool | None = None,
    conditions: list[dict[str, Any]] | None = None,
    actions: dict[str, Any] | None = None,
    match_logic: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Update an existing Mail.app rule (patch semantics).

    Patch semantics: only fields you provide are changed. ``conditions`` and
    ``actions``, when provided, REPLACE their respective structures wholesale
    (not merged).

    Conditional confirmation: prompts the user via MCP elicitation only when
    the patch touches ``conditions``, ``actions``, or ``match_logic`` —
    those replacements are irrecoverable. Patches limited to ``enabled``
    and/or ``name`` (trivially reversible) skip the prompt. The
    enable/disable path replaces the removed ``set_rule_enabled`` tool: call
    ``update_rule(rule_index, enabled=True|False)``.

    Refuses to update any rule whose existing actions include something
    outside the supported schema (run-AppleScript, redirect, reply text,
    play sound, custom highlight color); raises
    MailUnsupportedRuleActionError. Edit such rules in Mail.app's UI.

    Args:
        rule_index: 1-based positional index from list_rules.
        name: New name (only set if not None).
        enabled: New enabled state (only set if not None).
        conditions: If provided, REPLACES all existing conditions.
        actions: If provided, REPLACES all action flags wholesale.
        match_logic: 'all' or 'any', only set if not None.

    Returns:
        Dictionary with success status.
    """
    if refused := check_rate_limit("update_rule", {"rule_index": rule_index}):
        return refused
    rule_name = _resolve_rule_name(rule_index)
    if refused := _rule_policy_gate("update_rule", rule_name, actions):
        return refused
    if conditions is not None or actions is not None or match_logic is not None:
        summary = (
            f"Update Mail.app rule '{rule_name}' (index {rule_index})? "
            f"Previous condition/action state cannot be recovered."
        )
        if refused := _confirm_from_threadpool(
            ctx, summary, "update_rule", {"rule_index": rule_index}
        ):
            return refused

    server.mail.update_rule(
        rule_index=rule_index,
        name=name,
        enabled=enabled,
        conditions=conditions,
        actions=actions,
        match_logic=match_logic,
        expected_name=rule_name,
    )
    operation_logger.log_operation(
        "update_rule",
        {
            "rule_index": rule_index,
            "previous_name": rule_name,
            "name": name,
            "enabled": enabled,
            "conditions": conditions,
            "actions": actions,
            "match_logic": match_logic,
        },
        "success",
    )
    return {"success": True, "rule_index": rule_index}
