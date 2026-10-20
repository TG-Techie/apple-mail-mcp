"""Unit tests for the connector's Mail rules."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector


class TestRulesConnector:
    """Tests for RulesConnector."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @staticmethod
    def _supported_actions_clean_response() -> str:
        """Mock _check_supported_actions JSON for a rule with no
        unsupported actions set."""
        return (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"index":1,"name":"News From Apple","enabled":false},'
            '{"index":2,"name":"Junk filter","enabled":true}]'
        )
        result = connector.list_rules()
        assert result == [
            {"index": 1, "name": "News From Apple", "enabled": False},
            {"index": 2, "name": "Junk filter", "enabled": True},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_rules()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_allows_duplicate_names(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail allows multiple rules with the same name — connector returns both
        with distinct positional indices."""
        mock_run.return_value = (
            '[{"index":3,"name":"Send to OmniFocus","enabled":false},'
            '{"index":4,"name":"Send to OmniFocus","enabled":true}]'
        )
        result = connector.list_rules()
        assert len(result) == 2
        assert result[0]["name"] == result[1]["name"]
        assert result[0]["enabled"] != result[1]["enabled"]
        # The duplicate-name disambiguator: the index field.
        assert result[0]["index"] != result[1]["index"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_emits_one_based_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Per #63, list_rules' return shape must include a 1-based index
        matching Mail.app's AppleScript ``rule N`` reference."""
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        # Iterates by index, not by reference, so the loop variable is the index.
        assert "repeat with i from 1 to ruleCount" in script
        assert "|index|:i" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_quotes_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Record keys must be |quoted| per the v0.4.1 selector-collision rule."""
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of r)" in script
        assert "|enabled|:(enabled of r)" in script
        assert "|index|:i" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_returns_deleted_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"name":"Junk filter","applied":true}'
        result = connector.delete_rule(rule_index=2)
        assert result == "Junk filter"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_emits_correct_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"name":"X","applied":true}'
        connector.delete_rule(rule_index=2)
        script = mock_run.call_args[0][0]
        # Reads name before deleting (so we can echo it back).
        assert "name of rule 2" in script
        assert "delete rule 2" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=99)

    def test_delete_rule_rejects_zero_or_negative_index(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=0)
        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=-5)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_passes_for_clean_rule(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A rule with only supported actions does not raise."""
        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        # Should not raise.
        connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_rejects_run_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError, match="run script"):
            connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_lists_all_unsupported(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":true,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":true,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError) as excinfo:
            connector._check_supported_actions(rule_index=2)
        msg = str(excinfo.value)
        assert "run script" in msg
        assert "play sound" in msg
        assert "reply text" in msg

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_treats_color_message_none_as_clean(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """color_message == 'none' is the default — not a customization."""
        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        connector._check_supported_actions(rule_index=1)  # no raise

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_rejects_non_none_color_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"red"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError, match="color message"):
            connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector._check_supported_actions(rule_index=99)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_returns_new_rule_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "6"
        new_index = connector.create_rule(
            name="My Rule",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "X"}
            ],
            actions={"mark_read": True},
        )
        assert new_index == 6

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_emits_correct_field_and_operator(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "from", "operator": "contains", "value": "@apple.com"}
            ],
            actions={"delete": True},
        )
        script = mock_run.call_args[0][0]
        assert "rule type:from header" in script
        assert "qualifier:does contain value" in script
        assert 'expression:"@apple.com"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_header_name_includes_header_field(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {
                    "field": "header_name",
                    "operator": "equals",
                    "value": "yes",
                    "header_name": "X-Important",
                }
            ],
            actions={"mark_flagged": True},
        )
        script = mock_run.call_args[0][0]
        assert "rule type:header key" in script
        assert 'header:"X-Important"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_match_logic_any_emits_false(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"delete": True},
            match_logic="any",
        )
        script = mock_run.call_args[0][0]
        assert "all conditions must be met of newRule to false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_move_action_emits_mailbox_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"move_to": {"account": "Gmail", "mailbox": "Archive"}},
        )
        script = mock_run.call_args[0][0]
        assert "set should move message of newRule to true" in script
        assert (
            'set move message of newRule to mailbox "Archive" of '
            'account "Gmail"' in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_mark_flagged_with_color_sets_flag_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"mark_flagged": True, "flag_color": "yellow"},
        )
        script = mock_run.call_args[0][0]
        assert "set mark flagged of newRule to true" in script
        assert "set mark flag index of newRule to 2" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_forward_to_uses_comma_string(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"forward_to": ["a@example.com", "b@example.com"]},
        )
        script = mock_run.call_args[0][0]
        # forward_message is a string, not a list — recipients are
        # comma-separated.
        assert (
            'set forward message of newRule to "a@example.com, b@example.com"'
            in script
        )

    def test_create_rule_rejects_empty_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="name"):
            connector.create_rule(
                name="",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "X"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_empty_conditions(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="conditions"):
            connector.create_rule(
                name="X",
                conditions=[],
                actions={"delete": True},
            )

    def test_create_rule_rejects_empty_actions(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="actions"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={},
            )

    def test_create_rule_rejects_invalid_field(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="field"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "bogus", "operator": "contains", "value": "Y"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_invalid_operator(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="operator"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "BOGUS", "value": "Y"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_header_name_field_without_header_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="header_name"):
            connector.create_rule(
                name="X",
                conditions=[
                    {
                        "field": "header_name",
                        "operator": "contains",
                        "value": "v",
                    }
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_invalid_forward_to_email(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="email"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"forward_to": ["not-an-email"]},
            )

    def test_create_rule_rejects_invalid_match_logic(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="match_logic"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"delete": True},
                match_logic="bogus",
            )

    def test_create_rule_rejects_invalid_flag_color(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"mark_flagged": True, "flag_color": "neon"},
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_name_only_emits_minimal_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Two AppleScript calls happen: _check_supported_actions, then update.
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            '{"name":"X","applied":true}',  # the guarded update's outcome
        ]
        connector.update_rule(rule_index=2, name="Renamed")
        update_script = mock_run.call_args_list[1][0][0]
        assert "set newRule to rule 2" in update_script
        assert 'set name of newRule to "Renamed"' in update_script
        # Patch semantics: enabled/match_logic/conditions/actions not touched.
        assert "set enabled of newRule" not in update_script
        assert "set rule conditions of newRule" not in update_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_enabled_only_changes_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            '{"name":"X","applied":true}',
        ]
        connector.update_rule(rule_index=3, enabled=False)
        update_script = mock_run.call_args_list[1][0][0]
        assert "set enabled of newRule to false" in update_script
        assert "set name of newRule" not in update_script

    def test_update_rule_conditions_refused_due_to_mail_bug(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError
        # Mail.app on macOS Tahoe has a recursion bug in
        # removeFromCriteriaAtIndex: that crashes Mail on any AppleScript
        # path that removes a rule condition. update_rule must refuse
        # `conditions=` with a typed error instead of attempting it.
        with pytest.raises(MailUnsupportedRuleActionError, match="Tahoe"):
            connector.update_rule(
                rule_index=4,
                conditions=[
                    {"field": "from", "operator": "contains", "value": "@x.com"}
                ],
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_actions_resets_then_applies(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            '{"name":"X","applied":true}',
        ]
        connector.update_rule(
            rule_index=2,
            actions={"mark_read": True},
        )
        update_script = mock_run.call_args_list[1][0][0]
        # All action flags reset first
        assert "set mark flagged of newRule to false" in update_script
        assert "set delete message of newRule to false" in update_script
        # Then provided action applied
        assert "set mark read of newRule to true" in update_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_no_args_after_index_makes_no_changes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Calling update_rule with only rule_index does the supported-action
        check and then exits — no script for an empty update."""
        mock_run.return_value = self._supported_actions_clean_response()
        connector.update_rule(rule_index=2)
        # Only one AppleScript call: the supported-actions check.
        assert mock_run.call_count == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_refuses_unsupported_actions(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        # _check_supported_actions response indicates run-script is set.
        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError):
            connector.update_rule(rule_index=4, enabled=False)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector.update_rule(rule_index=99, enabled=False)

    def test_update_rule_rejects_invalid_match_logic(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="match_logic"):
            connector.update_rule(rule_index=2, match_logic="bogus")

    def test_update_rule_rejects_empty_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="name"):
            connector.update_rule(rule_index=2, name="")


class TestRuleMutationsActOnTheConfirmedRule:
    """A rule index is a position, and positions move.

    The server resolves the name at an index, shows it in a confirmation
    prompt, waits for a human, and then acts on the index. Rules can be
    created, deleted or reordered while the prompt is open — the window is
    as long as a person takes to read a dialog — so the confirmed name and
    the acted-on index can name different rules. Classic TOCTOU.

    The connector now takes the confirmed name and checks it against the
    rule at that index inside the same AppleScript call as the mutation,
    so there is no gap between the check and the act. A mismatch applies
    nothing and raises MailRuleChangedError.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @staticmethod
    def _clean_actions() -> str:
        return (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )

    # --- delete_rule -----------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_checks_the_name_in_the_same_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"name":"Junk filter","applied":true}'
        assert connector.delete_rule(2, expected_name="Junk filter") == "Junk filter"
        script = mock_run.call_args[0][0]
        assert 'if currentName is "Junk filter" then' in script
        assert "delete rule 2" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_of_a_moved_rule_applies_nothing_and_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleChangedError

        mock_run.return_value = '{"name":"Something else","applied":false}'
        with pytest.raises(MailRuleChangedError) as exc:
            connector.delete_rule(2, expected_name="Junk filter")
        assert exc.value.rule_index == 2
        assert exc.value.expected_name == "Junk filter"
        assert exc.value.actual_name == "Something else"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_without_an_expected_name_is_unconditional(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Callers that just listed (the integration tests' cleanup) may
        delete by index alone; the check exists for confirmations."""
        mock_run.return_value = '{"name":"X","applied":true}'
        assert connector.delete_rule(2) == "X"
        assert "if currentName is" not in mock_run.call_args[0][0]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_expected_name_is_escaped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"name":"a \\"b\\"","applied":true}'
        connector.delete_rule(1, expected_name='a "b"')
        assert 'if currentName is "a \\"b\\"" then' in mock_run.call_args[0][0]

    # --- update_rule -----------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_checks_the_name_before_any_change(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            self._clean_actions(),
            '{"name":"Junk filter","applied":true}',
        ]
        connector.update_rule(2, enabled=False, expected_name="Junk filter")
        script = mock_run.call_args_list[1][0][0]
        check_at = script.index('if currentName is "Junk filter" then')
        change_at = script.index("set enabled of newRule to false")
        assert check_at < change_at

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_of_a_moved_rule_applies_nothing_and_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleChangedError

        mock_run.side_effect = [
            self._clean_actions(),
            '{"name":"Something else","applied":false}',
        ]
        with pytest.raises(MailRuleChangedError):
            connector.update_rule(2, enabled=False, expected_name="Junk filter")


class TestAForwardingRuleIsAStandingSend:
    """``forward_to`` on a rule sends every matching message, from then
    on, to the addresses it names, with nobody reading each one. It was
    the one way mail left through the connector without meeting the
    outbound allowlist. The gate runs before any AppleScript, so a
    refused rule touches nothing in Mail."""

    _CONDITIONS = [{"field": "subject", "operator": "contains", "value": "Y"}]

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_refuses_an_off_list_target_before_touching_mail(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        with pytest.raises(MailOutboundDisallowedError) as exc:
            connector.create_rule(
                name="X",
                conditions=self._CONDITIONS,
                actions={"forward_to": ["a@example.com", "outsider@other.com"]},
            )
        assert "outsider@other.com" in str(exc.value)
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_refuses_an_off_list_target_before_touching_mail(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        with pytest.raises(MailOutboundDisallowedError):
            connector.update_rule(
                rule_index=1,
                actions={"forward_to": ["outsider@other.com"]},
                expected_name="X",
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_an_unreadable_policy_refuses_the_rule(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp.exceptions import OutboundAllowlistUnavailableError
        from apple_mail_mcp.outbound_allowlist import COMMS_CONFIG_ENV

        monkeypatch.setenv(COMMS_CONFIG_ENV, "/nonexistent/comms.yaml")
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(OutboundAllowlistUnavailableError):
            connector.create_rule(
                name="X",
                conditions=self._CONDITIONS,
                actions={"forward_to": ["a@example.com"]},
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_on_list_targets_reach_mail(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=self._CONDITIONS,
            actions={"forward_to": ["a@example.com"]},
        )
        assert 'set forward message of newRule to "a@example.com"' in (
            mock_run.call_args[0][0]
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_rule_that_does_not_forward_consults_no_policy(
        self,
        mock_run: MagicMock,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only a forwarding rule sends anything, so only it meets the
        allowlist; an unreadable policy must not block a move rule."""
        from apple_mail_mcp.outbound_allowlist import COMMS_CONFIG_ENV

        monkeypatch.setenv(COMMS_CONFIG_ENV, "/nonexistent/comms.yaml")
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        mock_run.return_value = "1"
        assert connector.create_rule(
            name="X", conditions=self._CONDITIONS, actions={"mark_read": True},
        ) == 1
