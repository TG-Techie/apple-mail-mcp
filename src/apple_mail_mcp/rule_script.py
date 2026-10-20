"""Mail rules: the field and operator vocabularies, the checks on a rule's
conditions and actions, its action lines, and the guarded mutation.
"""

from typing import Any, cast

from .exceptions import MailRuleChangedError
from .outbound_allowlist import assert_forward_targets_allowed
from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    get_flag_index,
    sanitize_input,
    validate_email,
)

# MCP-tool field name → Mail.app AppleScript `rule type` enum identifier.
# Verified against Mail.app's running rules: 'from header', 'subject header',
# 'message content' all confirmed live. Other values follow the same naming
# convention per Mail.app's AppleScript dictionary; verified via integration
# test on live rule creation.
RULE_FIELD_MAP = {
    "from": "from header",
    "to": "to header",
    "subject": "subject header",
    "body": "message content",
    "any_recipient": "any recipient",
    "header_name": "header key",
}


# MCP-tool operator name → Mail.app AppleScript `qualifier` enum identifier.
# 'does contain value', 'equal to value', 'begins with value' verified live
# against the user's existing rules. Others follow Mail.app's documented
# naming.
RULE_OPERATOR_MAP = {
    "contains": "does contain value",
    "does_not_contain": "does not contain value",
    "begins_with": "begins with value",
    "ends_with": "ends with value",
    "equals": "equal to value",
}


def validate_rule_condition(cond: dict[str, Any]) -> None:
    """Validate a single RuleCondition dict.

    Required keys: field (in RULE_FIELD_MAP), operator (in
    RULE_OPERATOR_MAP), value (non-empty str). header_name required
    iff field == 'header_name'.
    """
    if "field" not in cond or cond["field"] not in RULE_FIELD_MAP:
        raise ValueError(
            f"condition.field must be one of {sorted(RULE_FIELD_MAP)}, got {cond.get('field')!r}"
        )
    if "operator" not in cond or cond["operator"] not in RULE_OPERATOR_MAP:
        raise ValueError(
            f"condition.operator must be one of "
            f"{sorted(RULE_OPERATOR_MAP)}, got {cond.get('operator')!r}"
        )
    if not cond.get("value") or not isinstance(cond["value"], str):
        raise ValueError("condition.value must be a non-empty string")
    if cond["field"] == "header_name":
        if not cond.get("header_name"):
            raise ValueError("condition.header_name is required when field is 'header_name'")


def validate_rule_actions(actions: dict[str, Any]) -> None:
    """Validate a RuleActions dict has at least one meaningful entry,
    flag_color (if any) is valid, and forward_to emails are valid and
    on the outbound allowlist.

    A forwarding rule is a standing send, and this is the one place
    both create_rule and update_rule pass through before any
    AppleScript runs, so the allowlist gate sits here: a refused
    rule touches nothing in Mail. Raises MailOutboundDisallowedError
    (or OutboundAllowlistUnavailableError, fail closed) for the
    forward_to that the policy does not admit."""
    meaningful_keys = {
        "move_to",
        "copy_to",
        "mark_read",
        "mark_flagged",
        "delete",
        "forward_to",
    }
    # Strip falsy bools / empty containers — they're no-ops, not actions.
    active = {k: v for k, v in actions.items() if k in meaningful_keys and v}
    if not active:
        raise ValueError(
            f"actions must include at least one of {sorted(meaningful_keys)} with a truthy value"
        )
    if "flag_color" in actions and actions["flag_color"]:
        # get_flag_index raises ValueError on bad input.
        get_flag_index(actions["flag_color"])
    if active.get("forward_to"):
        for addr in active["forward_to"]:
            if not isinstance(addr, str) or not validate_email(addr):
                raise ValueError(f"forward_to entries must be valid email addresses; got {addr!r}")
        assert_forward_targets_allowed(active["forward_to"])
    for mb_key in ("move_to", "copy_to"):
        if mb_key in active:
            ref = active[mb_key]
            if not isinstance(ref, dict) or not ref.get("account") or not ref.get("mailbox"):
                raise ValueError(
                    f"actions.{mb_key} must be a dict with "
                    f"'account' and 'mailbox' keys, got {ref!r}"
                )


def build_action_lines(actions: dict[str, Any]) -> list[str]:
    """Translate a validated RuleActions dict into AppleScript lines.

    Each line operates on a variable named ``newRule`` (or ``r`` for
    update_rule's reuse). Caller picks the target variable name and
    substitutes.
    """
    lines: list[str] = []
    if actions.get("move_to"):
        mb_safe = escape_applescript_string(sanitize_input(actions["move_to"]["mailbox"]))
        acct_clause = applescript_account_clause(actions["move_to"]["account"])
        lines.append("set should move message of newRule to true")
        lines.append(f'set move message of newRule to mailbox "{mb_safe}" of {acct_clause}')
    if actions.get("copy_to"):
        mb_safe = escape_applescript_string(sanitize_input(actions["copy_to"]["mailbox"]))
        acct_clause = applescript_account_clause(actions["copy_to"]["account"])
        lines.append("set should copy message of newRule to true")
        lines.append(f'set copy message of newRule to mailbox "{mb_safe}" of {acct_clause}')
    if actions.get("mark_read"):
        lines.append("set mark read of newRule to true")
    if actions.get("mark_flagged"):
        lines.append("set mark flagged of newRule to true")
        if actions.get("flag_color"):
            idx = get_flag_index(actions["flag_color"])
            lines.append(f"set mark flag index of newRule to {idx}")
    if actions.get("delete"):
        lines.append("set delete message of newRule to true")
    if actions.get("forward_to"):
        recipients = ", ".join(actions["forward_to"])
        recipients_safe = escape_applescript_string(recipients)
        lines.append(f'set forward message of newRule to "{recipients_safe}"')
    return lines


def guarded_rule_mutation(rule_index: int, expected_name: str | None, statements: list[str]) -> str:
    """AppleScript that applies ``statements`` to ``rule rule_index``
    only if its name is still ``expected_name``.

    The name is read and compared inside the same tell block as the
    mutation. The block sets ``resultData`` to ``{name, applied}``
    for :func:`wrap_as_json_script`; ``applied`` is false when the
    name did not match and nothing ran. With no ``expected_name``
    the statements run unconditionally.
    """
    body = "\n".join("            " + stmt for stmt in statements)
    if expected_name is None:
        return f"""
        tell application "Mail"
            set currentName to name of rule {rule_index}
{body}
            set resultData to {{|name|:currentName, |applied|:true}}
        end tell
        """
    name_safe = escape_applescript_string(expected_name)
    return f'''
        tell application "Mail"
            set currentName to name of rule {rule_index}
            if currentName is "{name_safe}" then
{body}
                set resultData to {{|name|:currentName, |applied|:true}}
            else
                set resultData to {{|name|:currentName, |applied|:false}}
            end if
        end tell
        '''


def raise_if_rule_changed(
    rule_index: int, expected_name: str | None, outcome: dict[str, Any]
) -> None:
    if outcome.get("applied") is True:
        return
    raise MailRuleChangedError(
        rule_index,
        expected_name=expected_name or "",
        actual_name=cast(str, outcome.get("name", "")),
    )
