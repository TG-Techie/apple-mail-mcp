"""The connector's Mail rules: list, create, update and delete."""

from typing import Any, cast

from .connector_base import ConnectorBase
from .exceptions import MailRuleNotFoundError, MailUnsupportedRuleActionError
from .rule_script import (
    RULE_FIELD_MAP,
    RULE_OPERATOR_MAP,
    build_action_lines,
    guarded_rule_mutation,
    raise_if_rule_changed,
    validate_rule_actions,
    validate_rule_condition,
)
from .script_blocks import wrap_as_json_script
from .utils import escape_applescript_string, parse_applescript_json, sanitize_input


class RulesConnector(ConnectorBase):
    """The connector's Mail rules."""

    def list_rules(self) -> list[dict[str, Any]]:
        """List all Mail.app rules.

        Returns:
            List of rule dicts with keys:
              - index: 1-based positional index, matching Mail.app's
                AppleScript ``rule N`` reference. Stable within a single
                snapshot; can change if the user reorders rules.
              - name: rule display name (NOT guaranteed unique — Mail
                allows duplicates).
              - enabled: whether the rule is currently enabled.

        Note:
            Mail.app does not expose a stable rule id via AppleScript;
            ``index`` is the canonical handle for downstream mutation tools
            (delete_rule / update_rule). Callers that care about
            reorder-stability should call ``list_rules`` again immediately
            before each mutation.
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            set ruleCount to count of rules
            repeat with i from 1 to ruleCount
                set r to rule i
                set ruleRecord to {|index|:i, |name|:(name of r), |enabled|:(enabled of r)}
                set end of resultData to ruleRecord
            end repeat
        end tell
        """

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def create_rule(
        self,
        name: str,
        conditions: list[dict[str, Any]],
        actions: dict[str, Any],
        match_logic: str = "all",
        enabled: bool = True,
    ) -> int:
        """Create a new Mail.app rule. Returns the new rule's 1-based index.

        Args:
            name: Rule display name.
            conditions: List of RuleCondition dicts. At least one required.
            actions: RuleActions dict. At least one action must be set.
            match_logic: 'all' (AND) or 'any' (OR) across conditions.
            enabled: Whether the rule is enabled on creation.

        Returns:
            1-based positional index of the newly-created rule (Mail.app
            appends new rules to the end, so this equals the new total
            count of rules).

        Raises:
            ValueError: If any input fails schema validation.
            MailOutboundDisallowedError: If ``actions.forward_to`` names
                an address off the outbound allowlist, or the allowlist
                cannot be read. Nothing is installed.
        """
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if not conditions:
            raise ValueError("conditions must have at least one entry")
        if match_logic not in ("all", "any"):
            raise ValueError(f"match_logic must be 'all' or 'any', got {match_logic!r}")
        for cond in conditions:
            validate_rule_condition(cond)
        validate_rule_actions(actions)

        name_safe = escape_applescript_string(sanitize_input(name))
        all_conditions = "true" if match_logic == "all" else "false"
        enabled_str = "true" if enabled else "false"

        condition_lines: list[str] = []
        for cond in conditions:
            rule_type = RULE_FIELD_MAP[cond["field"]]
            qualifier = RULE_OPERATOR_MAP[cond["operator"]]
            expr_safe = escape_applescript_string(sanitize_input(cond["value"]))
            if cond["field"] == "header_name":
                header_safe = escape_applescript_string(sanitize_input(cond["header_name"]))
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}", header:"{header_safe}"}} '
                    f"at end of rule conditions of newRule"
                )
            else:
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}"}} '
                    f"at end of rule conditions of newRule"
                )

        action_lines = build_action_lines(actions)

        body = (
            f"set newRule to make new rule with properties "
            f'{{name:"{name_safe}"}}\n'
            f"set all conditions must be met of newRule to {all_conditions}\n"
            + "\n".join(condition_lines)
            + "\n"
            + "\n".join(action_lines)
            + "\n"
            f"set enabled of newRule to {enabled_str}\n"
            f"return (count of rules) as text"
        )
        script = f'tell application "Mail"\n{body}\nend tell'
        return int(self._run_applescript(script))

    def update_rule(
        self,
        rule_index: int,
        name: str | None = None,
        enabled: bool | None = None,
        conditions: list[dict[str, Any]] | None = None,
        actions: dict[str, Any] | None = None,
        match_logic: str | None = None,
        expected_name: str | None = None,
    ) -> None:
        """Update an existing Mail.app rule (patch-style for top-level fields,
        full replacement for conditions/actions when provided).

        Calls ``_check_supported_actions`` first; refuses to update any rule
        whose existing action set includes something outside our schema
        (run-AppleScript, redirect, reply text, etc.) — we cannot safely
        partial-update because the unsupported actions would be silently
        dropped or misrepresented.

        Args:
            rule_index: 1-based positional index from ``list_rules``.
            name: New name (only set if not None).
            enabled: New enabled state (only set if not None).
            conditions: If provided, REPLACES all existing conditions wholesale.
            actions: If provided, REPLACES all action flags wholesale —
                unprovided actions are reset to off.
            match_logic: 'all' | 'any', only set if not None.
            expected_name: The name the caller confirmed for this index.
                When given, the update applies only if the rule at the
                index still carries that name, checked inside the same
                AppleScript call as the changes. See ``delete_rule``.

        Raises:
            ValueError: If any provided input fails schema validation.
            MailOutboundDisallowedError: If ``actions.forward_to`` names
                an address off the outbound allowlist, or the allowlist
                cannot be read. Nothing is changed.
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If the rule currently has an
                action outside the supported schema.
            MailRuleChangedError: If ``expected_name`` was given and the
                rule at the index is now a different one. Nothing was
                changed.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        if match_logic is not None and match_logic not in ("all", "any"):
            raise ValueError(f"match_logic must be 'all' or 'any', got {match_logic!r}")
        if conditions is not None:
            # Mail.app on macOS Tahoe (16.0 / macOS 26) has a recursion bug
            # in -[MFMessageRule(Applescript) removeFromCriteriaAtIndex:].
            # ANY AppleScript path that removes a rule condition (delete by
            # index, delete every, or assigning a new list to `rule
            # conditions`) hits the same broken accessor and crashes Mail.
            # Verified with a one-line minimal repro:
            #     tell application "Mail" to delete rule condition 1 of rule "X"
            # Until Apple fixes this, replacing conditions in place is not
            # implementable; users must delete and recreate the rule.
            raise MailUnsupportedRuleActionError(
                "Replacing rule conditions is not supported: Mail.app on "
                "macOS Tahoe has a recursion bug in its AppleScript handler "
                "for rule-condition deletion (-[MFMessageRule(Applescript) "
                "removeFromCriteriaAtIndex:]) that crashes Mail. To change "
                "conditions, delete the rule and recreate it with create_rule."
            )
        if actions is not None:
            validate_rule_actions(actions)
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("name, if provided, must be a non-empty string")

        # Refuse to patch rules whose existing actions we don't fully model.
        self._check_supported_actions(rule_index)

        # Renaming a rule invalidates the local AppleScript variable
        # bound to it (Mail.app tries to resolve the variable by the old
        # name on subsequent property accesses, which now fails). Defer
        # any rename to the very end so all other property changes
        # operate on a stable reference.
        body_parts: list[str] = [
            f"set newRule to rule {rule_index}",
        ]

        if match_logic is not None:
            body_parts.append(
                f"set all conditions must be met of newRule to "
                f"{'true' if match_logic == 'all' else 'false'}"
            )
        if actions is not None:
            # Reset all supported action flags first; then apply provided ones.
            # `set forward message ... to ""` raises -10000 when the value is
            # already empty (Tahoe quirk), so gate the reset on a length check.
            body_parts.extend(
                [
                    "set should move message of newRule to false",
                    "set should copy message of newRule to false",
                    "set mark read of newRule to false",
                    "set mark flagged of newRule to false",
                    "set mark flag index of newRule to -1",
                    "set delete message of newRule to false",
                    'if forward message of newRule is not "" then '
                    'set forward message of newRule to ""',
                ]
            )
            body_parts.extend(build_action_lines(actions))
        # `enabled` must come AFTER the action-reset block: setting enabled
        # before resets causes the reset to silently revert it (Tahoe quirk).
        if enabled is not None:
            body_parts.append(f"set enabled of newRule to {'true' if enabled else 'false'}")
        # Rename last — see comment above.
        if name is not None:
            name_safe = escape_applescript_string(sanitize_input(name))
            body_parts.append(f'set name of newRule to "{name_safe}"')

        if len(body_parts) == 1:
            # Only the rule lookup, no actual updates — caller passed nothing.
            return
        tell_body = guarded_rule_mutation(rule_index, expected_name, body_parts)
        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        outcome = cast(dict[str, Any], parse_applescript_json(self._run_applescript(script)))
        raise_if_rule_changed(rule_index, expected_name, outcome)

    def _check_supported_actions(self, rule_index: int) -> None:
        """Verify a rule's existing actions are all in our schema.

        Used by ``update_rule`` before applying changes — if the rule
        currently has any action set that we don't model (run-AppleScript,
        redirect, reply text, play sound, highlight color, forward text),
        we can't safely partial-update because we'd silently drop or
        misrepresent that action. Read access via ``list_rules`` is
        unaffected.

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If any action outside the
                medium-tier schema is currently set on the rule.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        tell_body = f"""
        tell application "Mail"
            set r to rule {rule_index}
            set resultData to {{|run_script_set|:(run script of r is not missing value), |play_sound_set|:(play sound of r is not missing value), |redirect_set|:((redirect message of r) is not ""), |forward_text_set|:((forward text of r) is not ""), |reply_text_set|:((reply text of r) is not ""), |highlight_text|:(highlight text using color of r), |color_message|:((color message of r) as text)}}
        end tell
        """
        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))

        unsupported: list[str] = []
        if parsed.get("run_script_set"):
            unsupported.append("run script")
        if parsed.get("play_sound_set"):
            unsupported.append("play sound")
        if parsed.get("redirect_set"):
            unsupported.append("redirect message")
        if parsed.get("forward_text_set"):
            unsupported.append("forward text")
        if parsed.get("reply_text_set"):
            unsupported.append("reply text")
        if parsed.get("highlight_text"):
            unsupported.append("highlight text using color")
        if parsed.get("color_message", "none") != "none":
            unsupported.append("color message")

        if unsupported:
            raise MailUnsupportedRuleActionError(
                f"rule {rule_index} uses actions outside the supported "
                f"schema: {', '.join(unsupported)}. Edit this rule in "
                f"Mail.app's Rules pane instead."
            )

    def delete_rule(self, rule_index: int, expected_name: str | None = None) -> str:
        """Delete a rule by 1-based index.

        Reads the rule's name in the same AppleScript call so callers
        (typically the server layer's elicitation summary) can echo the
        deleted name. After deletion, downstream rule indices shift down
        by one — callers should re-call ``list_rules`` before any further
        rule operations.

        Args:
            rule_index: 1-based positional index, as returned by ``list_rules``.
            expected_name: The name the caller confirmed for this index.
                When given, the delete applies only if the rule at the
                index still carries that name — checked inside the same
                AppleScript call, so there is no gap between the check
                and the act. Indices are positions, and positions move
                while a confirmation prompt is open.

        Returns:
            The name of the deleted rule (for confirmation / logging).

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
            MailRuleChangedError: If ``expected_name`` was given and the
                rule at the index is now a different one. Nothing was
                deleted.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        tell_body = guarded_rule_mutation(rule_index, expected_name, [f"delete rule {rule_index}"])
        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        outcome = cast(dict[str, Any], parse_applescript_json(self._run_applescript(script)))
        raise_if_rule_changed(rule_index, expected_name, outcome)
        return cast(str, outcome["name"])
