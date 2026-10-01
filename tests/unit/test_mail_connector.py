"""Unit tests for mail connector."""

import json
import logging
import tempfile
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import LoginError

from apple_mail_mcp.compose_ledger import WindowOperation
from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftInvalidIdError,
    MailDraftNotFoundError,
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
    MailTimeoutError,
)
from apple_mail_mcp.mail_connector import (
    AppleMailConnector,
    _ComposeWindow,
    _WindowSnapshot,
    _wrap_as_json_script,
)
from apple_mail_mcp.utils import SANITIZE_MAX_LENGTH


@pytest.fixture(autouse=True)
def _windows_before_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every composition reads Mail's windows before it opens its own
    (``_mail_window_snapshot``). Answered here with none, so each test's
    scripted outcomes start at the opening script; what the read is for
    is tested in test_compose_window_tending.py."""
    monkeypatch.setattr(
        AppleMailConnector,
        "_mail_window_snapshot",
        lambda self: _WindowSnapshot(mail_pid=77701, window_ids=frozenset()),
    )


class TestAppleMailConnector:
    """Tests for AppleMailConnector."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch("subprocess.run")
    def test_run_applescript_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test successful AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="result",
            stderr=""
        )

        result = connector._run_applescript("test script")
        assert result == "result"

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/osascript", "-"]

    @patch("subprocess.run")
    def test_run_applescript_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test account not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get account \"NonExistent\""
        )

        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test mailbox not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get mailbox \"NonExistent\""
        )

        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_timeout(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A script osascript did not finish within the connector's
        timeout is its own error, told apart from a script that failed,
        and still a MailAppleScriptError to every handler of those."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        with pytest.raises(MailTimeoutError) as raised:
            connector._run_applescript("test script")

        assert str(raised.value) == (
            f"Script execution timeout after {connector.timeout}s"
        )
        assert isinstance(raised.value, MailAppleScriptError)
        assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)

    @patch("subprocess.run")
    def test_a_failed_script_is_not_a_timeout(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="execution error: boom (-2700)"
        )

        with pytest.raises(MailAppleScriptError) as raised:
            connector._run_applescript("test script")

        assert not isinstance(raised.value, MailTimeoutError)

    @patch("subprocess.run")
    def test_run_applescript_curly_apostrophe_still_maps_to_typed_error(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Real macOS stderr uses curly apostrophes — must still dispatch typed errors.

        Regression guard for a bug where `Can\u2019t get account "X"` (curly
        apostrophe, as emitted by Mail.app) bypassed the typed-exception
        mapping and surfaced as a generic MailAppleScriptError, defeating the
        server-layer not-found routing.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get account \"NonExistent\"",
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get mailbox \"NonExistent\"",
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","full_name":"Alice Smith",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true},'
            '{"id":"UUID-2","name":"Work","full_name":"",'
            '"email_addresses":["me@work.com","alt@work.com"],'
            '"account_type":"iCloud","enabled":false}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"id": "UUID-1", "name": "Gmail", "full_name": "Alice Smith",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            # Empty-string full_name normalized to None.
            {"id": "UUID-2", "name": "Work", "full_name": None,
             "email_addresses": ["me@work.com", "alt@work.com"],
             "account_type": "iCloud", "enabled": False},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_normalizes_whitespace_only_full_name_to_none(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Whitespace-only full_name is treated as not-configured."""
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","full_name":"   ",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result[0]["full_name"] is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_accounts()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_handles_empty_email_addresses(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """An account with no email addresses must return email_addresses as []."""
        mock_run.return_value = (
            '[{"id":"UUID-3","name":"LocalOnly","full_name":"Local User",'
            '"email_addresses":[],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result == [{
            "id": "UUID-3", "name": "LocalOnly", "full_name": "Local User",
            "email_addresses": [],
            "account_type": "imap", "enabled": True,
        }]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text), enabled,
        and the full_name (#158) used for the Display Name <email> sender."""
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script
        # #158: full_name read with missing-value coercion.
        assert "full name of acc" in script
        assert "|full_name|:accFullName" in script

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

    # --- delete_rule -----------------------------------------------------

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

    # --- _check_supported_actions ---------------------------------------

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

    # --- create_rule -----------------------------------------------------

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

    # --- update_rule -----------------------------------------------------

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

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| (quoted) so NSJSONSerialization keeps it.

        Unquoted `name:` in the record literal causes the key to be silently
        dropped during ASObjC -> NSDictionary conversion because `name` collides
        with NSObject's `name` property. Regression guard for real Mail.app bug.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of acc)" in script
        assert "{name:(name of acc)" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"INBOX","unread_count":5},'
            '{"name":"Sent","unread_count":0},'
            '{"name":"Projects/Client A","unread_count":3}]'
        )
        result = connector.list_mailboxes("Gmail")
        assert result == [
            {"name": "INBOX", "unread_count": 5},
            {"name": "Sent", "unread_count": 0},
            {"name": "Projects/Client A", "unread_count": 3},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector.list_mailboxes("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| so NSJSONSerialization preserves it."""
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        assert "|name|:(name of mb)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_with_name_uses_account_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        assert 'set accountRef to account "Gmail"' in script
        assert "account id" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = "[]"
        connector.list_mailboxes(uuid)
        script = mock_run.call_args[0][0]
        assert f'set accountRef to account id "{uuid}"' in script

    # --- _resolve_imap_config --------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_prefers_user_name_for_login(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Primary path: user_name (Mail.app's IMAP LOGIN credential) wins
        over email_addresses[0] (the SMTP From list). They overlap for
        most accounts but diverge for iCloud accounts on a custom-domain
        Apple ID — there email_addresses[0] is an SMTP-only From alias
        the IMAP server rejects with AUTHENTICATIONFAILED. (#201)
        """
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"user_name":"apple-id@example.com",'
            '"email_addresses":["from-alias@example.com","apple-id@example.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "apple-id@example.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_falls_back_to_email_addresses_when_user_name_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Fallback path: empty user_name → use email_addresses[0]. (#201)"""
        mock_run.return_value = (
            '{"host":"imap.gmail.com",'
            '"port":993,'
            '"user_name":"",'
            '"email_addresses":["me@gmail.com"]}'
        )
        result = connector._resolve_imap_config("Gmail")
        assert result == ("imap.gmail.com", 993, "me@gmail.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError(
            "Can't get account \"NoSuch\"."
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_imap_config("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_script_has_quoted_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """NSJSONSerialization requires |key| form for record keys."""
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config("iCloud")
        script = mock_run.call_args[0][0]
        assert "|host|:(server name of acctRef)" in script
        assert "|port|:(port of acctRef)" in script
        assert "|user_name|:(user name of acctRef)" in script
        assert "|email_addresses|:acctEmails" in script
        # Must assign to resultData for _wrap_as_json_script to serialize.
        assert "set resultData to" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_escapes_account_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config('Weird "Name" Acct')
        script = mock_run.call_args[0][0]
        # The quote must be escaped; raw quotes would break the script.
        assert 'Weird \\"Name\\" Acct' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config(uuid)
        script = mock_run.call_args[0][0]
        assert f'set acctRef to account id "{uuid}"' in script

    # --- _imap_failures state + _log_imap_fallback -----------------------

    def test_imap_failures_starts_empty(
        self, connector: AppleMailConnector
    ) -> None:
        assert connector._imap_failures == set()

    def test_log_imap_fallback_keychain_entry_not_found_is_silent(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing Keychain entry is a benign opt-out signal — DEBUG only."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainEntryNotFoundError("missing")
            )
        # Not in the failures set — benign signals don't count as failures.
        assert "iCloud" not in connector._imap_failures
        # Should log at DEBUG, never WARNING.
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1
        assert "iCloud" in debug_records[0].getMessage()

    def test_log_imap_fallback_first_failure_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("network down"))
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "iCloud" in msg
        assert "OSError" in msg

    def test_log_imap_fallback_subsequent_failure_same_account_is_debug(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Seed: first failure.
        connector._log_imap_fallback("iCloud", OSError("first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("second"))
        # Set unchanged (already contains iCloud).
        assert connector._imap_failures == {"iCloud"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1

    def test_log_imap_fallback_failure_new_account_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        connector._log_imap_fallback("iCloud", OSError("iCloud first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("Gmail", OSError("Gmail first"))
        assert connector._imap_failures == {"iCloud", "Gmail"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "Gmail" in warning_records[0].getMessage()

    def test_log_imap_fallback_access_denied_counts_as_failure(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Access denied is a misconfiguration worth surfacing, unlike missing entry."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainAccessDeniedError("ACL refused")
            )
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1

    # --- Issue #118: per-account circuit breaker --------------------------

    def test_breaker_default_ttl_is_30s(
        self, connector: AppleMailConnector
    ) -> None:
        """Class constant sanity — drift here would silently change
        offline-burst behavior."""
        assert connector._IMAP_BREAKER_TTL_S == 30.0

    def test_breaker_starts_closed(
        self, connector: AppleMailConnector
    ) -> None:
        """A fresh connector has no per-account cooldown set."""
        assert connector._imap_failure_until == {}
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_opens_after_first_non_benign_failure(
        self, connector: AppleMailConnector
    ) -> None:
        """A LoginError (or any non-benign fallback exception) sets the
        deadline ~TTL into the future."""
        before = time.monotonic()
        connector._log_imap_fallback("iCloud", LoginError("bad pw"))
        deadline = connector._imap_failure_until["iCloud"]
        # Deadline lands within the TTL window (allowing for sub-second
        # scheduling jitter from the test runner).
        assert deadline > before + connector._IMAP_BREAKER_TTL_S - 1
        assert deadline <= before + connector._IMAP_BREAKER_TTL_S + 1
        assert connector._imap_breaker_open("iCloud") is True

    def test_breaker_does_not_open_for_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        """Missing Keychain entry is the user's explicit opt-out — no
        cooldown, just silent DEBUG logging. Otherwise every call to a
        non-IMAP-configured account would consult a deadline lookup
        before falling through."""
        connector._log_imap_fallback(
            "iCloud", MailKeychainEntryNotFoundError("missing")
        )
        assert "iCloud" not in connector._imap_failure_until
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_resets_after_ttl(
        self, connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the deadline is in the past, the breaker is closed again
        and the next call attempts IMAP organically."""
        from apple_mail_mcp import mail_connector as mc_mod
        # Freeze time at a known point so we can advance deterministically.
        clock = [1000.0]
        monkeypatch.setattr(mc_mod.time, "monotonic", lambda: clock[0])

        connector._log_imap_fallback("iCloud", LoginError("bad"))
        assert connector._imap_breaker_open("iCloud") is True

        # Just before the deadline — still open.
        clock[0] = 1000.0 + connector._IMAP_BREAKER_TTL_S - 0.1
        assert connector._imap_breaker_open("iCloud") is True

        # Past the deadline — closed.
        clock[0] = 1000.0 + connector._IMAP_BREAKER_TTL_S + 0.1
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_is_per_account(
        self, connector: AppleMailConnector
    ) -> None:
        """Failure on iCloud must not skip IMAP for Gmail."""
        connector._log_imap_fallback("iCloud", LoginError("rejected"))
        assert connector._imap_breaker_open("iCloud") is True
        assert connector._imap_breaker_open("Gmail") is False

    def test_clear_breaker_removes_entry(
        self, connector: AppleMailConnector
    ) -> None:
        connector._log_imap_fallback("iCloud", LoginError("rejected"))
        assert connector._imap_breaker_open("iCloud") is True
        connector._imap_clear_breaker("iCloud")
        assert connector._imap_breaker_open("iCloud") is False
        # Idempotent — clearing an already-clear account is fine.
        connector._imap_clear_breaker("iCloud")
        connector._imap_clear_breaker("Never-Set")

    def test_search_messages_skips_imap_when_breaker_is_open(
        self, connector: AppleMailConnector
    ) -> None:
        """End-to-end: open the breaker, then call search_messages —
        the IMAP path is bypassed entirely (saving the wasted round
        trip), AppleScript runs."""
        connector._imap_failure_until["iCloud"] = (
            time.monotonic() + 60  # breaker open for the next minute
        )
        with patch.object(
            connector, "_imap_search"
        ) as imap_path, patch.object(
            connector, "_search_messages_applescript",
            return_value=[],
        ) as as_path:
            connector.search_messages("iCloud", "INBOX")
        imap_path.assert_not_called()
        as_path.assert_called_once()

    def test_successful_imap_call_clears_breaker_via_search(
        self, connector: AppleMailConnector
    ) -> None:
        """A successful IMAP call after a transient failure must clear
        the cooldown so we don't leave the breaker open longer than the
        problem persists."""
        # Pretend a previous failure opened the breaker.
        connector._imap_failure_until["iCloud"] = time.monotonic() - 1  # already expired
        # Manually re-open: a fresh failure with a future deadline.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        # Then make IMAP work normally.
        with patch.object(
            connector, "_imap_search", return_value=[{"id": "x"}]
        ):
            # We need the breaker closed to let IMAP run, then verify
            # success clears it. The clean way: clear first, then call.
            connector._imap_clear_breaker("iCloud")
            connector.search_messages("iCloud", "INBOX")
        assert "iCloud" not in connector._imap_failure_until

    def test_get_message_skips_imap_when_breaker_open(
        self, connector: AppleMailConnector
    ) -> None:
        """Same gate applies to get_message's hint-gated IMAP path."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        with patch.object(
            connector, "_imap_get_message"
        ) as imap_path, patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path:
            connector.get_message("123", account="iCloud", mailbox="INBOX")
        imap_path.assert_not_called()
        as_path.assert_called_once()

    def test_login_error_warning_includes_setup_imap_command(
        self, connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A revoked / expired app password is the most common cause of
        LoginError. The fallback works, but the user has no way to know
        IMAP is broken because results are correct via AppleScript. The
        specialized WARNING text names the exact `setup-imap` command
        so they can fix at their leisure."""
        with caplog.at_level(
            logging.DEBUG, logger="apple_mail_mcp.mail_connector"
        ):
            connector._log_imap_fallback("iCloud", LoginError("AUTHENTICATIONFAILED"))

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "iCloud" in msg
        # The actionable instruction must be present and command-perfect.
        assert "apple-mail-mcp setup-imap --account iCloud" in msg
        # Reassurance that the user isn't blocked.
        assert "AppleScript fallback" in msg

    def test_non_login_failure_uses_generic_warning(
        self, connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """OSError (offline / DNS / unreachable) gets the generic
        message — there's no setup-imap command that would help."""
        with caplog.at_level(
            logging.DEBUG, logger="apple_mail_mcp.mail_connector"
        ):
            connector._log_imap_fallback("iCloud", OSError("network unreachable"))

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        # The generic message references AppleScript fallback but does
        # NOT name a setup-imap command (would be misleading for a
        # network-level failure).
        assert "AppleScript" in msg
        assert "setup-imap" not in msg

    # --- _imap_search helper ---------------------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.return_value = [{"id": "1", "subject": "S"}]

        result = connector._imap_search("iCloud", "INBOX", limit=5)

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        # Parameters forwarded 1:1 to the IMAP connector (minus `account`).
        mock_imap.search_messages.assert_called_once_with(
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=None,
            date_from=None,
            date_to=None,
            has_attachment=None,
            include_attachments=False,
            limit=5,
            body_contains=None,
            text_contains=None,
        )
        assert result == [{"id": "1", "subject": "S"}]

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_search("iCloud", "INBOX")

    # --- _imap_get_thread helper -----------------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.find_thread_members.return_value = [
            {"id": "anchor@x", "subject": "S"},
        ]

        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": ["parent@x"],
        }
        result = connector._imap_get_thread(anchor)

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.find_thread_members.assert_called_once_with(
            anchor_rfc_message_id="anchor@x",
            anchor_references=["parent@x"],
        )
        assert result == [{"id": "anchor@x", "subject": "S"}]

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": [],
        }
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_get_thread(anchor)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.find_thread_members.side_effect = LoginError("rejected")
        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": [],
        }
        with pytest.raises(LoginError):
            connector._imap_get_thread(anchor)

    # --- get_thread delegation -------------------------------------------

    _ANCHOR = {
        "internal_id": "500",
        "account": "iCloud",
        "rfc_message_id": "anchor@x",
        "subject": "S",
        "in_reply_to": None,
        "references": [],
    }

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_the_applescript_path_tells_the_caller_what_it_can_miss(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """The fallback was logged in the server process only, so a caller
        got a thread that may lack members whose subject was rewritten
        and nothing said so. on_warning now carries which path built the
        thread and why."""
        mock_anchor.return_value = dict(self._ANCHOR)
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_collect.return_value = [{"id": "500"}]
        heard: list[str] = []
        connector.get_thread("500", on_warning=heard.append)
        assert len(heard) == 1
        assert "AppleScript" in heard[0]
        assert "subject" in heard[0]
        assert "not configured" in heard[0]

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_an_imap_failure_is_named_in_the_warning(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_anchor.return_value = dict(self._ANCHOR)
        mock_imap.side_effect = LoginError("bad password")
        mock_collect.return_value = [{"id": "500"}]
        heard: list[str] = []
        connector.get_thread("500", on_warning=heard.append)
        assert len(heard) == 1
        assert "bad password" in heard[0]

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_the_imap_path_raises_no_warning(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_anchor.return_value = dict(self._ANCHOR)
        mock_imap.return_value = [{"id": "anchor@x"}]
        heard: list[str] = []
        connector.get_thread("500", on_warning=heard.append)
        assert heard == []

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_uses_imap_on_success(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.return_value = [{"id": "anchor@x", "subject": "from imap"}]
        result = connector.get_thread("500")
        assert result == [{"id": "anchor@x", "subject": "from imap"}]
        mock_collect.assert_not_called()

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_keychain_missing(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_collect.return_value = [{"id": "500", "subject": "from applescript"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.get_thread("500")
        assert result == [{"id": "500", "subject": "from applescript"}]
        mock_collect.assert_called_once()
        # Missing-entry = silent (no WARNING).
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        assert "iCloud" not in connector._imap_failures

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_oserror_with_warning(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.side_effect = OSError("unreachable")
        mock_collect.return_value = [{"id": "500"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.get_thread("500")
        assert result == [{"id": "500"}]
        mock_collect.assert_called_once()
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "iCloud" in connector._imap_failures

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_login_error(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_anchor.return_value = {
            "internal_id": "500", "account": "iCloud",
            "rfc_message_id": "anchor@x", "subject": "S",
            "in_reply_to": None, "references": [],
        }
        mock_imap.side_effect = LoginError("rejected")
        mock_collect.return_value = [{"id": "500"}]
        result = connector.get_thread("500")
        assert result == [{"id": "500"}]
        mock_collect.assert_called_once()

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_anchor_not_found_propagates(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """MailMessageNotFoundError from anchor resolution must propagate,
        not fall back — the message just doesn't exist anywhere."""
        mock_anchor.side_effect = MailMessageNotFoundError("Can't get message")
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("nonexistent")
        mock_imap.assert_not_called()
        mock_collect.assert_not_called()

    # --- _imap_move_messages helper (#149) -------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.move_messages.return_value = 3

        result = connector._imap_move_messages(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.move_messages.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.move_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.move_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_unsupported_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.move_messages.side_effect = MailImapMoveUnsupportedError(
            "no MOVE / UIDPLUS"
        )

        with pytest.raises(MailImapMoveUnsupportedError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    # --- update_message move delegation (#149) ---------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_uses_imap_for_move_only_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move-only patch with source_mailbox + account: IMAP runs,
        AppleScript pass is skipped."""
        mock_imap.return_value = 4
        result = connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 4
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_skips_imap_for_combined_patch(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """move + read_status combined: stays on AppleScript until
        sibling issues #150 / #151 / #152 land."""
        mock_run_as.return_value = "2"
        connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move-only without source_mailbox: IMAP can't help (would
        SEARCH every mailbox per id), fall through to AppleScript."""
        mock_run_as.return_value = "2"
        connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed entirely (no wasted
        connect/login round trip)."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_unsupported_capability(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No MOVE / UIDPLUS: capability gap is permanent for the server,
        DEBUG-only log, breaker NOT opened (read paths still work)."""
        mock_imap.side_effect = MailImapMoveUnsupportedError("no caps")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry — a
        transient blip shouldn't keep the breaker open longer than
        the problem persists."""
        connector._imap_failure_until["iCloud"] = time.monotonic() - 10  # expired
        # Re-arm a future deadline so the breaker is open at call time.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        # Manually clear so IMAP is allowed; success will keep it cleared.
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_delete_messages helper (#150) -----------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.return_value = 3

        result = connector._imap_delete_messages(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.delete_messages.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_unsupported_move_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.side_effect = MailImapMoveUnsupportedError(
            "no MOVE / UIDPLUS"
        )

        with pytest.raises(MailImapMoveUnsupportedError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_trash_not_found_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.side_effect = MailImapTrashNotFoundError(
            "no Trash"
        )

        with pytest.raises(MailImapTrashNotFoundError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    # --- delete_messages delegation (#150) -------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_uses_imap_when_account_and_source_provided(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """account + source_mailbox provided: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 4
        result = connector.delete_messages(
            ["a@x", "b@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 4
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_skips_imap_without_account_and_source(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Neither hint provided: IMAP would have to SEARCH every
        mailbox per Message-ID, defeating the speed win. Stay on
        AppleScript cross-scan."""
        mock_run_as.return_value = "1"
        connector.delete_messages(["a@x"])
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed entirely; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_unsupported_capability(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No MOVE / UIDPLUS: capability gap is permanent, DEBUG-only
        log, breaker NOT opened."""
        mock_imap.side_effect = MailImapMoveUnsupportedError("no caps")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_trash_not_found(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Trash discovery failed: same benign treatment as
        unsupported-capability — DEBUG-only, no breaker."""
        mock_imap.side_effect = MailImapTrashNotFoundError("no trash")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_set_read_status helper (#151) -----------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_read_status.return_value = 3

        result = connector._imap_set_read_status(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            read=True,
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.set_read_status.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            read=True,
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_read_status.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_read_status.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    # --- update_message read-only delegation (#151) ----------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_uses_imap_for_read_only_true_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read-only patch (read_status only, no flag/move) with source
        mailbox: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 2
        result = connector.update_message(
            ["a@x", "b@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 2
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            read=True,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_uses_imap_for_read_only_false_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Unread case — same IMAP path, read=False."""
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            read_status=False,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x"],
            source_mailbox="INBOX",
            read=False,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_move(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """read + move in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_flag(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """read + flag in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read-only call without source_mailbox: IMAP can't help
        (would SEARCH every mailbox per id), fall through to AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                read_status=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                read_status=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_set_flagged_status helper (#152) --------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_flagged_status.return_value = 3

        result = connector._imap_set_flagged_status(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            flagged=True,
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.set_flagged_status.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            flagged=True,
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_flagged_status.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_flagged_status.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    # --- update_message flag-only delegation (#152) ----------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_uses_imap_for_flag_only_true_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Flag-only patch (flagged=True only, no color/read/move) with
        account + source_mailbox: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 2
        result = connector.update_message(
            ["a@x", "b@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 2
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            flagged=True,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_uses_imap_for_flag_only_false_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Clear-flag case — same IMAP path, flagged=False."""
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            flagged=False,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x"],
            source_mailbox="INBOX",
            flagged=False,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_when_flag_color_is_set(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag_color is Mail.app-specific (\\$MailFlagBit* keywords);
        IMAP can't set it. Fall through to AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flag_color="red",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_for_combined_flag_and_read(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag + read in one call: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_for_combined_flag_and_move(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag + move in one call: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Flag-only call without source_mailbox: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path bypassed; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign: silent DEBUG, no WARNING,
        breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                flagged=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                flagged=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- search_messages delegation --------------------------------------

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_uses_imap_on_success(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap_search.return_value = [{"id": "1", "subject": "from imap"}]
        result = connector.search_messages(account="iCloud", mailbox="INBOX")
        assert result == [{"id": "1", "subject": "from imap"}]
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_keychain_missing(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_imap_search.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_as_search.return_value = [{"id": "1", "subject": "from applescript"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1", "subject": "from applescript"}]
        mock_as_search.assert_called_once()
        # Missing-entry = silent (no WARNING).
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        # Account not tracked as a failure.
        assert "iCloud" not in connector._imap_failures

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_oserror_with_warning(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_imap_search.side_effect = OSError("unreachable")
        mock_as_search.return_value = [{"id": "1"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "iCloud" in connector._imap_failures

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_login_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_imap_search.side_effect = LoginError("rejected")
        mock_as_search.return_value = [{"id": "1"}]
        result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_imap_protocol_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import IMAPClientError

        mock_imap_search.side_effect = IMAPClientError("bad thing")
        mock_as_search.return_value = [{"id": "1"}]
        result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_forwards_all_parameters(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap_search.return_value = []
        connector.search_messages(
            account="iCloud",
            mailbox="Sent",
            sender_contains="alice",
            subject_contains="invoice",
            read_status=True,
            is_flagged=False,
            date_from="2026-04-01",
            date_to="2026-04-22",
            has_attachment=True,
            limit=10,
        )
        mock_imap_search.assert_called_once_with(
            "iCloud",
            "Sent",
            "alice",
            "invoice",
            True,
            False,
            "2026-04-01",
            "2026-04-22",
            True,
            10,
            False,  # include_attachments
            None,  # body_contains
            None,  # text_contains
        )

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_does_not_catch_value_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Invalid-input errors must propagate to the caller, not silently fall back."""
        mock_imap_search.side_effect = ValueError("bad date")
        with pytest.raises(ValueError, match="bad date"):
            connector.search_messages(account="iCloud", date_from="not-a-date")
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_does_not_catch_mailaccountnotfound(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """A truly-missing account must surface, not be papered over by fallback."""
        mock_imap_search.side_effect = MailAccountNotFoundError("No such account")
        with pytest.raises(MailAccountNotFoundError):
            connector.search_messages(account="Ghost")
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test basic message search."""
        mock_run.return_value = (
            '[{"id":"12345","subject":"Test Subject",'
            '"sender":"sender@example.com","date_received":"Mon Jan 1 2024",'
            '"read_status":false}]'
        )

        result = connector._search_messages_applescript("Gmail", "INBOX")

        assert len(result) == 1
        assert result[0]["id"] == "12345"
        assert result[0]["subject"] == "Test Subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[0]["read_status"] is False

    # Note: validates the Python-side JSON parse. Real end-to-end correctness
    # (AppleScript actually emitting valid JSON when the data contains '|')
    # is proven by integration tests.
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_handles_pipe_in_subject(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Subject containing '|' must not break parsing (the bug this refactor fixes)."""
        mock_run.return_value = (
            '[{"id":"abc","subject":"Q3 Report | Draft",'
            '"sender":"boss@example.com","date_received":"Wed Feb 5 2025",'
            '"read_status":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert len(result) == 1
        assert result[0]["subject"] == "Q3 Report | Draft"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """If _run_applescript raises MailAccountNotFoundError, search_messages must not swallow it.

        Regression guard: a previous version wrapped the tell-block in try/on error,
        which downgraded MailAccountNotFoundError to MailAppleScriptError.
        """
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector._search_messages_applescript("NoSuch", "INBOX")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Similar regression guard for MailMailboxNotFoundError."""
        mock_run.side_effect = MailMailboxNotFoundError("Can't get mailbox \"NoSuch\".")
        with pytest.raises(MailMailboxNotFoundError):
            connector._search_messages_applescript("Gmail", "NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_with_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test message search with filters.

        Per #32, filters are now applied as per-message IF expressions
        instead of a `whose` clause — `whose` is unusably slow against
        large IMAP mailboxes (>120s timeout on 8000+ messages). The new
        pattern iterates messages in reverse (newest-first) and checks
        each filter against the message; the script short-circuits when
        matchCount reaches the limit.
        """
        mock_run.return_value = "[]"

        connector._search_messages_applescript(
            "Gmail",
            "INBOX",
            sender_contains="john@example.com",
            subject_contains="meeting",
            read_status=False,
            limit=10
        )

        # Filter conditions appear as IF clauses, not in a `whose` clause.
        call_args = mock_run.call_args[0][0]
        assert (
            'if (sender of msg) does not contain "john@example.com" '
            'then set includeThis to false'
            in call_args
        )
        assert (
            'if (subject of msg) does not contain "meeting" '
            'then set includeThis to false'
            in call_args
        )
        assert (
            "if (read status of msg) is not false "
            "then set includeThis to false"
            in call_args
        )
        # Limit is enforced by accumulating matches and exiting the repeat
        # when matchCount reaches the bound.
        assert "if matchCount >= 10 then exit repeat" in call_args
        # Forward iteration. Mail returns `messages of mailbox`
        # newest-first, so walking it forward yields newest-first and a
        # limited search short-circuits on the NEWEST messages. The
        # previous `from total to 1 by -1` walked it backwards and made
        # limit=N return the N oldest, while the comment beside it
        # claimed newest-first.
        assert "repeat with i from 1 to total" in call_args
        assert "repeat with i from total to 1 by -1" not in call_args
        # No `whose` clause anywhere.
        assert "whose" not in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_without_filters_omits_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """AppleScript rejects `whose true` — no-filter searches must drop `whose`.

        Regression guard for a bug where `search_messages("X", "INBOX")` with no
        filters emitted `messages of mailboxRef whose true`, which Mail.app
        rejects with `Illegal comparison or logical (-1726)`.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "whose true" not in script
        # With NO filters, the generated source must reference `mailboxRef`
        # without a `whose` clause.
        assert "messages of mailboxRef\n" in script or "messages of mailboxRef " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_does_not_slice_message_reference(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail rejects `items 1 thru N of (messages ...)` with error -1728.

        The limit must be enforced via a counter-driven exit, not by slicing
        the live message collection reference.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", limit=5)
        script = mock_run.call_args[0][0]
        assert "items 1 thru" not in script
        assert "if matchCount >= 5 then exit repeat" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_is_flagged_filter(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """is_flagged is applied inside the loop via an includeThis IF expression."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=True)
        script = mock_run.call_args[0][0]
        assert (
            "if (flagged status of msg) is not true then set includeThis to false"
            in script
        )

        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=False)
        script = mock_run.call_args[0][0]
        assert (
            "if (flagged status of msg) is not false then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_date_range_filter(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """date_from/date_to are applied via inverted IF expressions inside the loop."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", date_from="2026-04-01", date_to="2026-04-15"
        )
        script = mock_run.call_args[0][0]
        # Cutoffs are built once, before the loop, by assignment. They are
        # NOT `date "YYYY-MM-DD"` literals: AppleScript reads that as the
        # year 12169 without erroring, which made date_from exclude every
        # message and date_to exclude none. See
        # applescript_iso_date_statements and docs/research/applescript-
        # date-coercion.md.
        assert 'date "' not in script, (
            "a date string literal is back in the search script; AppleScript "
            "does not parse ISO 8601 and does not fail on it"
        )
        assert "set year of dateFromCutoff to 2026" in script
        assert "set month of dateFromCutoff to 4" in script
        assert "set day of dateFromCutoff to 1" in script
        # date_to gets +1 day so the full day is inclusive.
        assert "set day of dateToCutoff to 16" in script
        # The IF expressions are the inverse of the inclusion condition: skip
        # if BEFORE date_from, skip if ON-OR-AFTER date_to+1.
        assert (
            "if (date received of msg) < dateFromCutoff "
            "then set includeThis to false" in script
        )
        assert (
            "if (date received of msg) >= dateToCutoff "
            "then set includeThis to false" in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_from(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Malformed dates must raise ValueError, not be sent to AppleScript.

        Prevents AppleScript injection via unescaped date strings.
        """
        with pytest.raises(ValueError, match="date_from"):
            connector._search_messages_applescript(
                "Gmail", "INBOX",
                date_from='2024-01-01", delete mailbox',
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_to(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="date_to"):
            connector._search_messages_applescript("Gmail", "INBOX", date_to="not-a-date")
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_true_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """has_attachment=True applied inside the loop as an includeThis IF."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", read_status=True, has_attachment=True
        )
        script = mock_run.call_args[0][0]
        # The whole script no longer uses `whose`; all filters live inside the loop.
        assert "whose" not in script
        # The attachment IF expression must appear as a post-filter inside the loop.
        assert (
            "if (count of mail attachments of msg) = 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_false_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", has_attachment=False)
        script = mock_run.call_args[0][0]
        assert (
            "if (count of mail attachments of msg) > 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_no_attachment_filter_has_no_check(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """When has_attachment is None, no attachment post-filter code appears."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "mail attachments of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_result_includes_flagged(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """New in #28: result rows include the flagged status."""
        mock_run.return_value = (
            '[{"id":"1","subject":"s","sender":"a@b.c",'
            '"date_received":"Mon","read_status":false,"flagged":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert result[0]["flagged"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Guard against NSJSONSerialization silently dropping the 'id' key.

        AppleScript record key `id:` collides with NSObject's id selector and
        gets stripped during NSDictionary conversion. Must be quoted as `|id|:`.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script
        # The bare form must not appear in the msgRecord literal — it would collide.
        assert ", id:(id of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_applescript_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = "[]"
        connector._search_messages_applescript(uuid, "INBOX")
        script = mock_run.call_args[0][0]
        assert f'set accountRef to account id "{uuid}"' in script

    def test_search_messages_logs_imap_hint_when_applescript_path_is_slow(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If AppleScript search exceeds the 5s threshold, log INFO hint to enable IMAP."""
        from apple_mail_mcp import mail_connector as mc_mod

        # perf_counter is called twice per search: start, then in finally. Side
        # effects let us simulate a 6.0s elapsed time without real sleeping.
        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          return_value=[]), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 6.0]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert len(info_records) == 1
        msg = info_records[0].getMessage()
        assert "6.0s" in msg
        assert "iCloud" in msg
        assert "INBOX" in msg
        # Hint must point users at IMAP setup.
        assert "IMAP" in msg

    def test_search_messages_does_not_log_imap_hint_when_applescript_path_is_fast(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Under the 5s threshold, no INFO hint — keeps logs quiet on small mailboxes."""
        from apple_mail_mcp import mail_connector as mc_mod

        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          return_value=[]), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 1.5]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert info_records == []

    def test_search_messages_logs_imap_hint_even_when_applescript_raises(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The threshold log fires from finally — slow failures should still log."""
        from apple_mail_mcp import mail_connector as mc_mod

        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          side_effect=MailAppleScriptError("timeout")), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 7.5]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            with pytest.raises(MailAppleScriptError):
                connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert len(info_records) == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting a message."""
        mock_run.return_value = (
            '{"id":"12345","subject":"Subject","sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false,'
            '"content":"Message body"}'
        )

        result = connector.get_message("12345", include_content=True)

        assert result["id"] == "12345"
        assert result["subject"] == "Subject"
        assert result["content"] == "Message body"
        assert result["read_status"] is True
        assert result["flagged"] is False

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_handles_pipe_in_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Body containing '|' must not break parsing."""
        mock_run.return_value = (
            '{"id":"99","subject":"x","sender":"a@b.com",'
            '"date_received":"Mon Jan 1 2024","read_status":false,"flagged":false,'
            '"content":"col1|col2|col3"}'
        )
        result = connector.get_message("99", include_content=True)
        assert result["content"] == "col1|col2|col3"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Same guard as test_search_messages_script_quotes_id_key, for get_message."""
        mock_run.return_value = '{"id":"x","subject":"","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        connector.get_message("x")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script

    # --- Issue #72 dispatcher behavior -----------------------------------

    def test_get_message_uses_imap_when_account_and_mailbox_provided(
        self, connector: AppleMailConnector
    ) -> None:
        """With both hint params, _imap_get_message runs and AppleScript
        is never called."""
        with patch.object(
            connector, "_imap_get_message",
            return_value={"id": "x", "content": "body"},
        ) as imap_path, patch.object(
            connector, "_get_message_applescript"
        ) as as_path:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "x", "content": "body"}
        imap_path.assert_called_once_with(
            account="iCloud",
            mailbox="INBOX",
            message_id="abc@x",
            include_content=True,
            headers_only=False,
            include_attachments=False,
        )
        as_path.assert_not_called()

    def test_get_message_no_hint_skips_imap_path(
        self, connector: AppleMailConnector
    ) -> None:
        """No account/mailbox → IMAP is bypassed entirely (no Keychain
        prompt, no log, no nothing)."""
        with patch.object(connector, "_imap_get_message") as imap_path, \
             patch.object(
                 connector, "_get_message_applescript",
                 return_value={"id": "1"},
             ) as as_path:
            result = connector.get_message("123")

        assert result == {"id": "1"}
        imap_path.assert_not_called()
        as_path.assert_called_once_with("123", True, False)

    def test_get_message_partial_hint_skips_imap(
        self, connector: AppleMailConnector
    ) -> None:
        """account-only or mailbox-only is not enough; both required."""
        with patch.object(connector, "_imap_get_message") as imap_path, \
             patch.object(
                 connector, "_get_message_applescript",
                 return_value={"id": "1"},
             ):
            connector.get_message("123", account="iCloud")
            connector.get_message("123", mailbox="INBOX")

        imap_path.assert_not_called()

    def test_get_message_falls_back_on_login_error(
        self, connector: AppleMailConnector
    ) -> None:
        """LoginError on IMAP path → fall through to AppleScript, log
        fallback once."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=LoginError("AUTHENTICATIONFAILED"),
        ) as imap_path, patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        imap_path.assert_called_once()
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_message_falls_back_on_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        """No Keychain entry is the benign opt-out signal — fall through
        and log at DEBUG (not WARNING)."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=MailKeychainEntryNotFoundError("none"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_message(
                "x", account="iCloud", mailbox="INBOX",
            )

        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_message_falls_back_when_network_unavailable(
        self, connector: AppleMailConnector
    ) -> None:
        """Offline / DNS-failed / host-unreachable should fall through to
        AppleScript so users running agents on a flaky network or fully
        offline still get a result. OSError covers socket.timeout,
        socket.gaierror, ConnectionRefusedError, and host-unreachable;
        all of those are in _IMAP_FALLBACK_EXCS."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=OSError("network is unreachable"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        as_path.assert_called_once()
        # The fallback log fires so the user can find the network failure
        # in DEBUG-level logs if they go looking.
        log_fb.assert_called_once()

    def test_get_message_falls_back_on_imap_protocol_error(
        self, connector: AppleMailConnector
    ) -> None:
        """IMAPClientError covers protocol-level breakage (BAD response,
        truncated session, captive-portal-style HTTP-instead-of-IMAP).
        Same fallback as network errors."""
        from imapclient.exceptions import IMAPClientError

        with patch.object(
            connector, "_imap_get_message",
            side_effect=IMAPClientError("BAD command"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        as_path.assert_called_once()

    def test_get_message_message_not_found_on_imap_does_not_fall_back(
        self, connector: AppleMailConnector
    ) -> None:
        """If IMAP found the folder but the Message-ID isn't there, that's
        a definitive answer — don't paper over it with an AppleScript scan
        that would also fail (or worse, succeed by matching a different
        message in a different folder)."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=MailMessageNotFoundError("nope"),
        ), patch.object(
            connector, "_get_message_applescript"
        ) as as_path:
            with pytest.raises(MailMessageNotFoundError):
                connector.get_message(
                    "abc@x", account="iCloud", mailbox="INBOX",
                )
        as_path.assert_not_called()

    def test_get_message_headers_only_silently_ignored_on_applescript(
        self, connector: AppleMailConnector
    ) -> None:
        """headers_only is an IMAP-only knob; passing it without a hint
        must not error and must not change the AppleScript path's behavior."""
        with patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1", "content": "body"},
        ) as as_path:
            connector.get_message("123", headers_only=True)
        # AppleScript path receives the original signature (message_id,
        # include_content, include_attachments); headers_only is silently dropped.
        as_path.assert_called_once_with("123", True, False)


    def test_get_attachments_uses_imap_when_account_and_mailbox_provided(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            return_value=[{"name": "x.pdf", "mime_type": "application/pdf",
                           "size": 100, "downloaded": False}],
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript"
        ) as as_path:
            result = connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert len(result) == 1
        imap_path.assert_called_once_with(
            account="iCloud",
            mailbox="INBOX",
            message_id="abc@x",
        )
        as_path.assert_not_called()

    def test_get_attachments_no_hint_skips_imap_path(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments"
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path:
            result = connector.get_attachments("123")

        assert result == []
        imap_path.assert_not_called()
        as_path.assert_called_once_with("123")

    def test_get_attachments_partial_hint_skips_imap(
        self, connector: AppleMailConnector
    ) -> None:
        """account-only or mailbox-only is not enough; both required."""
        with patch.object(
            connector, "_imap_get_attachments"
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript", return_value=[],
        ):
            connector.get_attachments("123", account="iCloud")
            connector.get_attachments("123", mailbox="INBOX")

        imap_path.assert_not_called()

    def test_get_attachments_falls_back_on_login_error(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=LoginError("AUTHENTICATIONFAILED"),
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        imap_path.assert_called_once()
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_attachments_falls_back_when_offline(
        self, connector: AppleMailConnector
    ) -> None:
        """OSError covers DNS, EHOSTUNREACH, connect timeout, etc.
        Same fallback behavior as get_message's offline test."""
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=OSError("network unreachable"),
        ), patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )
        as_path.assert_called_once()

    def test_get_attachments_falls_back_on_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=MailKeychainEntryNotFoundError("none"),
        ), patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_attachments_message_not_found_on_imap_does_not_fall_back(
        self, connector: AppleMailConnector
    ) -> None:
        """Same reasoning as get_message: a definitive 'not in this folder'
        from IMAP shouldn't be papered over with a cross-folder
        AppleScript scan that may match a different message."""
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=MailMessageNotFoundError("nope"),
        ), patch.object(
            connector, "_get_attachments_applescript"
        ) as as_path:
            with pytest.raises(MailMessageNotFoundError):
                connector.get_attachments(
                    "abc@x", account="iCloud", mailbox="INBOX",
                )
        as_path.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_pre_existing_positional_caller_unaffected(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Smoke test: existing callers passing only message_id still work,
        going through the AppleScript path unchanged."""
        mock_run.return_value = '{"attachments":[],"warnings":[]}'
        result = connector.get_attachments("x")
        assert result == []


    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_with_full_name_emits_display_form(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account with full_name -> 'Display Name <email>' form."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_accepts_the_accounts_own_address(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A sender read back from a draft ("Name <email>" or a bare
        email) resolves to the account that owns the address, so a
        rebuilt draft stays in that account. Case-insensitive on the
        address, as mail addresses are."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com", "alias@icloud.com"],
            },
            {
                "id": "UUID-2",
                "name": "Gmail",
                "full_name": "",
                "email_addresses": ["alice@gmail.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("Alice Smith <alice@icloud.com>")
            == "Alice Smith <alice@icloud.com>"
        )
        assert (
            connector._resolve_account_to_sender("Alias@iCloud.com")
            == "Alice Smith <alice@icloud.com>"
        )
        assert connector._resolve_account_to_sender("alice@gmail.com") == "alice@gmail.com"
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_account_to_sender("nobody@example.com")

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_without_full_name_falls_back_to_bare_email(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account without full_name -> bare email (graceful fallback)."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": None,
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_whitespace_only_full_name_falls_back(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: whitespace-only full_name treated as not-configured."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "   ",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_lookup_by_uuid_with_full_name(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("UUID-1")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_not_found_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailAccountNotFoundError

        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_account_to_sender("Bogus")

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_no_emails_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "Empty",
                "full_name": "Nobody",
                "email_addresses": [],
            },
        ]
        with pytest.raises(ValueError, match="email addresses"):
            connector._resolve_account_to_sender("Empty")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_single(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting a single selected message."""
        # Modernized: single AppleScript call returns JSON array of records.
        mock_run.return_value = (
            '[{"id":"12345","subject":"Selected Subject",'
            '"sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024",'
            '"read_status":true,"flagged":false,"content":"Body text"}]'
        )

        result = connector.get_selected_messages(include_content=True)

        assert len(result) == 1
        assert result[0]["id"] == "12345"
        assert result[0]["subject"] == "Selected Subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[0]["content"] == "Body text"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_multiple(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting multiple selected messages."""
        mock_run.return_value = (
            '[{"id":"111","subject":"Subject One","sender":"a@example.com",'
            '"date_received":"Mon Jan 1 2024","read_status":true,'
            '"flagged":false,"content":"Body one"},'
            '{"id":"222","subject":"Subject Two","sender":"b@example.com",'
            '"date_received":"Tue Jan 2 2024","read_status":false,'
            '"flagged":true,"content":"Body two"}]'
        )

        result = connector.get_selected_messages(include_content=True)

        assert len(result) == 2
        assert result[0]["id"] == "111"
        assert result[1]["id"] == "222"
        assert result[1]["flagged"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_none_selected(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test when no message is selected — script returns empty JSON array."""
        mock_run.return_value = "[]"

        result = connector.get_selected_messages()

        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_no_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that include_content=False emits the no-content branch in
        the AppleScript and the returned record has empty content."""
        mock_run.return_value = (
            '[{"id":"12345","subject":"Subject",'
            '"sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024",'
            '"read_status":false,"flagged":false,"content":""}]'
        )

        result = connector.get_selected_messages(include_content=False)

        # Verify the script took the no-content branch (no `set msgContent
        # to content of msg`).
        script = mock_run.call_args[0][0]
        assert 'set msgContent to ""' in script
        assert "set msgContent to content of msg" not in script

        assert len(result) == 1
        assert result[0]["content"] == ""

    # ---- get_thread ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_resolution_script_shape(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor-resolution AppleScript must query by internal id and quote keys."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        connector._get_thread_applescript("12345")
        anchor_script = mock_run.call_args_list[0][0][0]
        # All record keys must be |quoted| per the v0.4.1 selector-collision rule.
        assert "|rfc_message_id|:(message id of msg)" in anchor_script
        assert "|subject|:(subject of msg)" in anchor_script
        # Anchor lookup iterates by internal id; id must be wrapped in
        # AppleScript string quotes (otherwise UUID-style ids tokenize
        # as invalid syntax — see TestWhoseIdQuoting).
        assert 'whose id is "12345"' in anchor_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor lookup failure propagates MailMessageNotFoundError, and
        names the id: the script's own error says only "not found", and
        the tool passes the message through as it is."""
        mock_run.side_effect = MailMessageNotFoundError(
            "execution error: Can't get message: not found (-2700)"
        )
        with pytest.raises(MailMessageNotFoundError, match="'99999'"):
            connector._get_thread_applescript("99999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_returns_anchor_plus_replies_sorted(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor + 2 replies in candidates → all 3 sorted by date_received."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Re: Q3","in_reply_to":"","references_raw":""}',
            '['
            '{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"to":[{"name":"","address":"b@x"}],"cc":[],"bcc":[],"warnings":[],'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false},'
            '{"id":"101","rfc_message_id":"<r1@x>","in_reply_to":"<anchor@x>",'
            '"references_raw":"<anchor@x>","subject":"Re: Q3","sender":"b@x",'
            '"to":[{"name":"","address":"a@x"}],"cc":[],"bcc":[],"warnings":[],'
            '"date_received":"Tue Jan 2 2024","read_status":true,"flagged":false},'
            '{"id":"102","rfc_message_id":"<r2@x>","in_reply_to":"<r1@x>",'
            '"references_raw":"<anchor@x> <r1@x>","subject":"Re: Q3","sender":"a@x",'
            '"to":[{"name":"","address":"b@x"}],"cc":[],"bcc":[],"warnings":[],'
            '"date_received":"Wed Jan 3 2024","read_status":false,"flagged":false}'
            ']'
        ]
        result = connector._get_thread_applescript("100")
        assert len(result) == 3
        assert [m["id"] for m in result] == ["100", "101", "102"]
        # Response rows match search_messages shape (the dual-emit
        # rfc_message_id from #148, and the recipients).
        for m in result:
            assert set(m.keys()) == {
                "id", "rfc_message_id", "subject", "sender",
                "to", "cc", "bcc",
                "date_received", "read_status", "flagged",
            }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_drops_threading_internals_from_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Response rows must NOT leak in_reply_to / references_raw /
        references_parsed (threading-internal scratch fields). They
        DO carry rfc_message_id alongside id (dual-emit from #148)."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Q3","in_reply_to":"","references_raw":""}',
            '[{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector._get_thread_applescript("100")
        for m in result:
            assert "rfc_message_id" in m
            assert "in_reply_to" not in m
            assert "references_raw" not in m
            assert "references_parsed" not in m

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_orphan_anchor_returns_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor with no threading headers → thread = [anchor] only."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<orphan@x>","subject":"Standalone",'
            '"in_reply_to":"","references_raw":""}',
            '[{"id":"500","rfc_message_id":"<orphan@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Standalone","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector._get_thread_applescript("500")
        assert len(result) == 1
        assert result[0]["id"] == "500"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_candidate_script_uses_base_subject_and_account(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Candidate script must use normalized subject and scope to anchor's account."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<a@x>",'
            '"subject":"Re: Re: Q3 Report","in_reply_to":"","references_raw":""}',
            '[]',
        ]
        connector._get_thread_applescript("1")
        candidate_script = mock_run.call_args_list[1][0][0]
        assert 'account "Gmail"' in candidate_script
        # Base subject strips all Re: prefixes.
        assert 'subject contains "Q3 Report"' in candidate_script
        assert 'subject contains "Re:' not in candidate_script


# What the recipient block emits for one message, as the JSON the
# script returns: Mail's name is missing value for a recipient without
# a display name, which the script turns into "".
_RECIPIENT_RECORDS: dict[str, list[dict[str, str]]] = {
    "to": [
        {"name": "Jane Doe", "address": "jane@example.com"},
        {"name": "", "address": "ops@example.org"},
    ],
    "cc": [{"name": "", "address": "cc@example.net"}],
    "bcc": [],
}
_RECIPIENT_ROWS = {
    "to": ["Jane Doe <jane@example.com>", "ops@example.org"],
    "cc": ["cc@example.net"],
    "bcc": [],
}


def _as_record(**fields: Any) -> dict[str, Any]:
    """One message record as the AppleScript read paths emit it."""
    return {
        "id": "100", "rfc_message_id": "m-100@example.com",
        "subject": "Q3", "sender": "a@example.com",
        "date_received": "Mon", "read_status": False, "flagged": False,
        **_RECIPIENT_RECORDS, **fields,
    }


class TestRecipientFields:
    """Every AppleScript message row carries ``to``, ``cc`` and ``bcc``:
    search, get_message, the thread candidates, and the selection. The
    script reads each kind's recipients in one ``properties of`` event,
    under its own guard, and emits ``{name, address}`` records; Python
    renders them with the same ``format_address`` the IMAP rows use."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _assert_reads_recipients(self, script: str, message_var: str) -> None:
        for kind, var in (("to", "toList"), ("cc", "ccList"), ("bcc", "bccList")):
            # One event per kind, into a local list the loop walks.
            assert (
                f"set rcpts to properties of {kind} recipients of {message_var}"
                in script
            )
            # Record keys quoted; the list lands in the record.
            assert f"|{kind}|:{var}" in script
            # A failure is a warning naming the kind, never a silent [].
            assert f'"{kind} recipients unreadable for message "' in script
        assert "|name|:rcptName, |address|:rcptAddress" in script
        # missing value cannot be serialised to JSON.
        assert 'if rcptName is missing value then set rcptName to ""' in script
        assert (
            'if rcptAddress is missing value then set rcptAddress to ""' in script
        )

    def _assert_each_kind_guarded(self, script: str) -> None:
        """Each kind's read sits under its own try, so one unreadable
        list does not take the other two with it."""
        blocks = script.split("set rcpts to properties of ")[1:]
        assert len(blocks) == 3
        for block in blocks:
            guarded, _, _rest = block.partition("end try")
            assert "on error errMsg number errNum" in guarded

    # ---- search ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_script_reads_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Both of the search's paths read them: the bulk path, which
        falls back to one message's list when a run's bulk read
        failed, and the one-message-at-a-time path it gives way to."""
        mock_run.return_value = '{"messages":[],"warnings":[]}'
        connector._search_messages_applescript("Gmail", "INBOX")
        bulk, _marker, one_at_a_time = mock_run.call_args[0][0].partition(
            "set msgs to messages of mailboxRef"
        )
        for part, message_var in ((bulk, "msgRef"), (one_at_a_time, "msg")):
            self._assert_reads_recipients(part, message_var)
            self._assert_each_kind_guarded(part)
            # Failures go to the search's own warning list.
            assert "set end of warnList to" in part.split(
                "set rcpts to properties of to recipients"
            )[1]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_rows_render_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(
            {"messages": [_as_record()], "warnings": []}
        )
        [row] = connector._search_messages_applescript("Gmail", "INBOX")
        assert {k: row[k] for k in ("to", "cc", "bcc")} == _RECIPIENT_ROWS

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_with_attachments_still_reads_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"messages":[],"warnings":[]}'
        connector._search_messages_applescript(
            "Gmail", "INBOX", include_attachments=True
        )
        script = mock_run.call_args[0][0]
        self._assert_reads_recipients(script, "msg")
        assert "|bcc|:bccList, |attachments|:attList" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_passes_a_recipient_warning_on(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        warning = "cc recipients unreadable for message 100: x (error -10000)"
        mock_run.return_value = json.dumps(
            {"messages": [_as_record(cc=[])], "warnings": [warning]}
        )
        seen: list[str] = []
        [row] = connector._search_messages_applescript(
            "Gmail", "INBOX", on_warning=seen.append
        )
        assert seen == [warning]
        assert row["cc"] == []

    # ---- get_message ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_script_reads_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(_as_record(content="", warnings=[]))
        connector._get_message_applescript("100", include_content=False)
        script = mock_run.call_args[0][0]
        self._assert_reads_recipients(script, "msg")
        self._assert_each_kind_guarded(script)
        assert "|warnings|:recipWarnings" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_row_renders_recipients_and_no_empty_warnings(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(_as_record(content="", warnings=[]))
        row = connector._get_message_applescript("100", include_content=False)
        assert {k: row[k] for k in ("to", "cc", "bcc")} == _RECIPIENT_ROWS
        # The row's shape is unchanged when nothing went wrong.
        assert "warnings" not in row

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_keeps_a_recipient_warning_on_the_row(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """get_messages lifts a row's warnings to the response."""
        warning = "to recipients unreadable for message 100: x (error -1728)"
        mock_run.return_value = json.dumps(
            _as_record(content="", to=[], warnings=[warning])
        )
        row = connector._get_message_applescript("100", include_content=False)
        assert row["to"] == []
        assert row["warnings"] == [warning]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_merges_recipient_and_attachment_warnings(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        recipient_warning = "bcc recipients unreadable for message 100: x"
        attachment_warning = "attachment enumeration failed for message 100: y"
        mock_run.side_effect = [
            json.dumps(_as_record(content="", warnings=[recipient_warning])),
            json.dumps({"attachments": [], "warnings": [attachment_warning]}),
        ]
        row = connector._get_message_applescript(
            "100", include_content=False, include_attachments=True
        )
        assert row["warnings"] == [recipient_warning, attachment_warning]

    # ---- get_thread ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_thread_candidate_script_reads_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"a@x","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        connector._get_thread_applescript("100")
        script = mock_run.call_args_list[1][0][0]
        self._assert_reads_recipients(script, "m")
        self._assert_each_kind_guarded(script)
        assert "|warnings|:recipWarnings" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_thread_rows_render_recipients_and_report_member_warnings(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A warning about a thread member reaches the caller and leaves
        the row; one about a candidate that is not in the thread is not
        about anything returned, and goes with it."""
        member_warning = "cc recipients unreadable for message 100: x"
        stranger_warning = "to recipients unreadable for message 999: y"
        member = _as_record(
            rfc_message_id="a@x", in_reply_to="", references_raw="",
            warnings=[member_warning],
        )
        stranger = _as_record(
            id="999", rfc_message_id="elsewhere@x", in_reply_to="",
            references_raw="", warnings=[stranger_warning],
        )
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"a@x","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            json.dumps([member, stranger]),
        ]
        seen: list[str] = []
        [row] = connector._get_thread_applescript("100", on_warning=seen.append)
        assert {k: row[k] for k in ("to", "cc", "bcc")} == _RECIPIENT_ROWS
        assert "warnings" not in row
        assert seen == [member_warning]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_thread_anchor_missing_from_candidates_still_has_the_fields(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"a@x","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        [row] = connector._get_thread_applescript("100")
        assert (row["to"], row["cc"], row["bcc"]) == ([], [], [])

    # ---- selection ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_selection_script_reads_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.get_selected_messages(include_content=False)
        script = mock_run.call_args[0][0]
        self._assert_reads_recipients(script, "msg")
        self._assert_each_kind_guarded(script)
        assert "|warnings|:recipWarnings" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_selection_rows_render_recipients_and_merge_warnings(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        recipient_warning = "to recipients unreadable for message 100: x"
        mock_run.side_effect = [
            json.dumps([_as_record(content="", warnings=[recipient_warning])]),
            json.dumps({"attachments": [], "warnings": []}),
        ]
        [row] = connector.get_selected_messages(
            include_content=False, include_attachments=True
        )
        assert {k: row[k] for k in ("to", "cc", "bcc")} == _RECIPIENT_ROWS
        assert row["warnings"] == [recipient_warning]

    # ---- the two paths agree ----

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_applescript_row_and_the_imap_row_agree(
        self,
        mock_run: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """One message, as Mail's recipient properties report it and as
        the IMAP ENVELOPE carries it, renders the same lists."""
        from datetime import datetime

        from imapclient.response_types import Address, Envelope

        from apple_mail_mcp.imap_connector import ImapConnector

        mock_run.return_value = json.dumps(
            {"messages": [_as_record()], "warnings": []}
        )
        [applescript_row] = connector._search_messages_applescript(
            "Gmail", "INBOX"
        )

        client = MagicMock()
        mock_imap_cls.return_value = client
        client.search.return_value = [1]
        client.fetch.return_value = {1: {b"FLAGS": (), b"ENVELOPE": Envelope(
            date=datetime(2026, 1, 1), subject=b"Q3",
            from_=(Address(None, None, b"a", b"example.com"),),
            sender=None, reply_to=None,
            to=(
                Address(b"Jane Doe", None, b"jane", b"example.com"),
                Address(None, None, b"ops", b"example.org"),
            ),
            cc=(Address(None, None, b"cc", b"example.net"),),
            bcc=None,
            in_reply_to=None, message_id=b"<m-100@example.com>",
        )}}
        [imap_row] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()

        for key in ("to", "cc", "bcc"):
            assert applescript_row[key] == imap_row[key], key
        assert applescript_row["to"] == _RECIPIENT_ROWS["to"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_two_paths_agree_on_a_non_ascii_name(
        self,
        mock_run: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """The ENVELOPE carries the header's RFC 2047 encoded-words and
        the IMAP path decodes them. Mail's side here is the decoded
        text, which is what Mail is expected to hand back; it was not
        observed live, the test account holding no encoded-word name
        when this was written."""
        from datetime import datetime

        from imapclient.response_types import Address, Envelope

        from apple_mail_mcp.imap_connector import ImapConnector

        mock_run.return_value = json.dumps({"messages": [_as_record(
            to=[{"name": "Jörg Müller", "address": "jorg@example.com"}],
            cc=[], bcc=[],
        )], "warnings": []})
        [applescript_row] = connector._search_messages_applescript(
            "Gmail", "INBOX"
        )

        client = MagicMock()
        mock_imap_cls.return_value = client
        client.search.return_value = [1]
        client.fetch.return_value = {1: {b"FLAGS": (), b"ENVELOPE": Envelope(
            date=datetime(2026, 1, 1), subject=b"Q3",
            from_=(Address(None, None, b"a", b"example.com"),),
            sender=None, reply_to=None,
            to=(Address(
                b"=?UTF-8?Q?J=C3=B6rg_M=C3=BCller?=", None, b"jorg", b"example.com"
            ),),
            cc=None, bcc=None,
            in_reply_to=None, message_id=b"<m-100@example.com>",
        )}}
        [imap_row] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()

        assert applescript_row["to"] == imap_row["to"] == [
            "Jörg Müller <jorg@example.com>"
        ]


_ROW_PROPERTIES = (
    "message id", "subject", "sender", "date received", "read status",
    "flagged status",
)


class TestSearchReadsInBulk:
    """The AppleScript search reads each property once for many
    messages, not once per message: a filter's property once for the
    whole mailbox (``<prop> of messages of mailboxRef``), and each row
    property once per run of matched positions (``<prop> of messages
    runStart thru runEnd of mailboxRef``). A bulk read that fails is
    redone one message at a time and says so; lists that no longer line
    up send the whole search down the one-message-at-a-time path it had
    before. What runs against Mail is pinned by the integration tests;
    these pin the script's shape."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @staticmethod
    def _script(
        mock_run: MagicMock, connector: AppleMailConnector, **criteria: Any
    ) -> str:
        mock_run.return_value = '{"messages":[],"warnings":[]}'
        connector._search_messages_applescript("Gmail", "INBOX", **criteria)
        return str(mock_run.call_args[0][0])

    @staticmethod
    def _bulk(script: str) -> str:
        return script.partition("set msgs to messages of mailboxRef")[0]

    @staticmethod
    def _guarded_reads(script: str, marker: str) -> list[str]:
        """Each read starting with ``marker``, up to its ``end try``;
        not the line that empties the variable before the read."""
        return [
            block.partition("end try")[0]
            for block in script.split(marker)[1:]
            if not block.startswith(" {}")
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_no_filter_reads_rows_over_a_run_and_nothing_mailbox_wide(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._script(mock_run, connector, limit=50)
        bulk = self._bulk(script)
        assert "set total to count of messages of mailboxRef" in bulk
        # Nothing is read for every message of the mailbox: with no
        # filter the rows are the first `limit` positions.
        assert "of messages of mailboxRef" not in bulk.replace(
            "count of messages of mailboxRef", ""
        )
        assert (
            "set runIds to id of messages runStart thru runEnd of mailboxRef"
            in bulk
        )
        for prop in _ROW_PROPERTIES:
            assert f"{prop} of messages runStart thru runEnd of mailboxRef" in bulk
        for kind in ("to", "cc", "bcc"):
            assert (
                f"properties of {kind} recipients of messages runStart thru "
                "runEnd of mailboxRef" in bulk
            )
        assert "allIds" not in bulk

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_filter_property_is_read_once_for_the_whole_mailbox(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._script(
            mock_run, connector, subject_contains="meeting",
            date_from="2026-04-01", date_to="2026-04-15", limit=10,
        )
        bulk = self._bulk(script)
        assert bulk.count("set allIds to id of messages of mailboxRef") == 1
        assert bulk.count("set subjectAll to subject of messages of mailboxRef") == 1
        # Both date criteria test one list.
        assert bulk.count(
            "set dateReceivedAll to date received of messages of mailboxRef"
        ) == 1
        # The checks test a value, which the list supplies.
        assert "set subjectValue to item i of subjectAllRef" in bulk
        assert (
            'if subjectValue does not contain "meeting" then set includeThis to false'
            in bulk
        )
        assert "if dateReceivedValue < dateFromCutoff then set includeThis to false" in bulk
        assert "if dateReceivedValue >= dateToCutoff then set includeThis to false" in bulk
        # The rows take the filter's list rather than reading it again.
        assert "set subjectValue to item idx of subjectAllRef" in bulk
        # A property no filter read is read over the run.
        assert "sender of messages runStart thru runEnd of mailboxRef" in bulk
        assert "whose" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_attachment_filter_reads_one_message_at_a_time(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail has no cheap bulk form of it, so it is asked only of a
        message the other criteria kept, through a reference by id that
        costs no event to make."""
        script = self._script(
            mock_run, connector, subject_contains="q3", has_attachment=True,
        )
        bulk = self._bulk(script)
        assert "mail attachments of messages" not in bulk
        assert (
            "set msgRef to a reference to («class mssg» id "
            "(item i of allIdsRef) of mailboxRef)" in bulk
        )
        assert (
            "if includeThis and ((count of mail attachments of msgRef) = 0) "
            "then set includeThis to false" in bulk
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_body_filter_reads_content_over_runs_of_the_kept_positions(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The content is read for a run of the positions the other
        criteria kept, in one event, and tested in the script; never for
        the whole mailbox, and never through the scan's per-message
        reference."""
        script = self._script(
            mock_run, connector, subject_contains="q3", has_attachment=True,
            body_contains="budget",
        )
        bulk = self._bulk(script)
        assert "content of messages of mailboxRef" not in bulk
        assert "(content of msgRef) does not contain" not in bulk
        assert (
            "set contentRun to content of messages runStart thru runEnd of mailboxRef"
            in bulk
        )
        assert "set contentValue to item j of contentRunRef" in bulk
        assert (
            'if includeThis and (contentValue does not contain "budget") '
            "then set includeThis to false" in bulk
        )
        # The scan keeps a candidate; the content decides the match.
        scan, _sep, content = bulk.partition("set candsRef to a reference to cands")
        assert "set end of cands to i" in scan
        assert "set end of matched to i" not in scan
        assert "set end of matched to idx" in content

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_text_filter_reads_subject_and_sender_for_the_whole_mailbox(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """TEXT is the content, the subject or the sender. The two
        headers cost one event each for the whole mailbox, rather than
        two events for every message the content did not match; the
        rows then take them from those lists too."""
        bulk = self._bulk(self._script(mock_run, connector, text_contains="alice"))
        assert "set subjectAll to subject of messages of mailboxRef" in bulk
        assert "set senderAll to sender of messages of mailboxRef" in bulk
        assert (
            'if includeThis and (not (contentValue contains "alice" or '
            'subjectValue contains "alice" or senderValue contains "alice")) '
            "then set includeThis to false" in bulk
        )
        assert "set subjectValue to item idx of subjectAllRef" in bulk
        assert "set senderValue to item idx of senderAllRef" in bulk
        # A row reads them over its run only if the mailbox-wide read failed.
        assert "if not subjectInBulk then" in bulk
        assert "if not senderInBulk then" in bulk

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_with_only_a_content_filter_every_position_is_a_candidate(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Nothing is left for the scan to test, so it keeps each
        position without a guard around nothing."""
        bulk = self._bulk(self._script(mock_run, connector, text_contains="alice"))
        scan = bulk.partition("if candCount > 0 and")[0].rpartition(
            "repeat with i from 1 to total"
        )[2]
        assert scan.split() == (
            "if matchCount >= 999999999 then exit repeat "
            "set end of cands to i set candCount to candCount + 1"
        ).split()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bodies_are_read_no_further_than_the_limit_needs(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The kept positions are tested as soon as there are as many as
        the matches still wanted, so no body is read past the point
        where the limit is reached; and at most a batch of them at a
        time, however high the limit."""
        from apple_mail_mcp.mail_connector import _SEARCH_CONTENT_BATCH

        bulk = self._bulk(
            self._script(mock_run, connector, body_contains="budget", limit=10)
        )
        assert (
            "if candCount > 0 and (candCount >= 10 - matchCount or "
            f"candCount >= {_SEARCH_CONTENT_BATCH} or i = total) then" in bulk
        )
        assert bulk.count("if matchCount >= 10 then exit repeat") == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_content_run_takes_in_no_position_the_criteria_dropped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.mail_connector import _SEARCH_CONTENT_RUN_GAP

        assert _SEARCH_CONTENT_RUN_GAP == 0
        bulk = self._bulk(self._script(mock_run, connector, body_contains="budget"))
        assert (
            f"if (item (candLast + 1) of candsRef) - runEnd > "
            f"{_SEARCH_CONTENT_RUN_GAP + 1} then exit repeat" in bulk
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_content_read_that_fails_is_redone_one_message_at_a_time(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        bulk = self._bulk(self._script(mock_run, connector, body_contains="budget"))
        [read] = self._guarded_reads(bulk, "set contentRun to")
        assert "on error errMsg number errNum" in read
        assert (
            '("content could not be read in bulk for mailbox positions " '
            "& runStart & \"-\" & runEnd & \": \"" in read
        )
        assert (
            "set msgRef to a reference to («class mssg» id "
            "(item idx of allIdsRef) of mailboxRef)" in bulk
        )
        assert "set contentValue to (content of msgRef)" in bulk

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_content_run_must_line_up_with_the_matched_ids(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The run's ids are read after its bodies: if they are the ids
        the criteria kept at those positions, the bodies read before
        them are those messages' bodies."""
        bulk = self._bulk(self._script(mock_run, connector, body_contains="budget"))
        _before, _sep, after = bulk.partition("set contentRun to content of messages")
        assert (
            "set runIds to id of messages runStart thru runEnd of mailboxRef"
            in after.partition("repeat with k from")[0]
        )
        assert (
            "if (count of runIds) is not (runEnd - runStart + 1) or "
            "(count of contentRun) is not (count of runIds) then" in bulk
        )
        assert (
            "if (item j of runIdsRef) is not (item idx of allIdsRef) "
            "then set aligned to false" in bulk
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_one_at_a_time_search_tests_content_as_before(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._script(
            mock_run, connector, body_contains="budget", text_contains="alice"
        )
        loop = script.partition("set msgs to messages of mailboxRef")[2]
        assert (
            'if (content of msg) does not contain "budget" '
            "then set includeThis to false" in loop
        )
        assert (
            'if not ((content of msg) contains "alice" or (subject of msg) '
            'contains "alice" or (sender of msg) contains "alice") '
            "then set includeThis to false" in loop
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_every_bulk_read_falls_back_to_one_message_at_a_time(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._script(mock_run, connector, subject_contains="q3")
        bulk = self._bulk(script)
        reads = self._guarded_reads(bulk, "set subjectAll to") + [
            read
            for stem in ("messageId", "sender", "dateReceived", "readStatus",
                         "flaggedStatus", "to", "cc", "bcc")
            for read in self._guarded_reads(bulk, f"set {stem}Run to")
        ]
        assert len(reads) == 9
        for read in reads:
            assert "on error errMsg number errNum" in read
            assert "could not be read in bulk" in read
        # ...and what the fallback reads, one message at a time.
        for prop in _ROW_PROPERTIES:
            assert f"({prop} of msgRef)" in bulk
        for kind in ("to", "cc", "bcc"):
            assert f"set rcpts to properties of {kind} recipients of msgRef" in bulk

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_every_bulk_list_must_have_the_id_lists_length(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        bulk = self._bulk(self._script(mock_run, connector, subject_contains="q3"))
        assert (
            "if subjectInBulk and (count of subjectAll) is not total "
            "then set aligned to false" in bulk
        )
        assert "if (count of runIds) is not (runEnd - runStart + 1) then" in bulk
        for stem in ("messageId", "sender", "dateReceived", "readStatus",
                     "flaggedStatus", "to", "cc", "bcc"):
            assert (
                f"if {stem}RunRead and (count of {stem}Run) is not "
                "(count of runIds) then set aligned to false" in bulk
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_row_must_be_the_message_the_filter_matched(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        bulk = self._bulk(self._script(mock_run, connector, sender_contains="a"))
        assert (
            "if msgId is not (item idx of allIdsRef) then set aligned to false" in bulk
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_first_and_last_rows_are_read_again_at_the_end(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A message that arrived, moved or went while the rows were read
        shifts every position after it; the ids at the first and last
        matched positions say whether that happened."""
        bulk = self._bulk(self._script(mock_run, connector, limit=5))
        assert (
            "if (id of message (item 1 of matched) of mailboxRef) is not "
            "firstRowId then set aligned to false" in bulk
        )
        assert (
            "if (id of message (item matchCount of matched) of mailboxRef) "
            "is not lastRowId then set aligned to false" in bulk
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_lists_out_of_line_send_the_search_one_message_at_a_time(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._script(
            mock_run, connector, subject_contains="meeting", limit=10
        )
        before, _marker, fallback = script.rpartition("if not aligned then")
        assert "set aligned to true" in before
        # Whatever the bulk path gathered is discarded, with a warning
        # saying why the search went the slow way.
        head, _msgs, loop = fallback.partition("set msgs to messages of mailboxRef")
        assert "set resultData to {}" in head
        assert "the mailbox changed while" in head
        assert (
            'if (subject of msg) does not contain "meeting" '
            "then set includeThis to false" in loop
        )
        assert "|id|:(id of msg as text)" in loop
        # The limit ends both scans.
        assert script.count("if matchCount >= 10 then exit repeat") == 2

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_run_absorbs_a_short_gap(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.mail_connector import _SEARCH_RUN_GAP

        bulk = self._bulk(self._script(mock_run, connector, subject_contains="q3"))
        assert f"else if idx - runEnd > {_SEARCH_RUN_GAP + 1} then" in bulk

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_the_fallback_warnings_reach_the_caller(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """What the script reports when it fell back is passed on in
        order, and the rows it built the slow way are rendered like any
        other."""
        bulk_failure = (
            "subject could not be read in bulk for mailbox positions 1-2: "
            "x (error -1728); read one message at a time"
        )
        realigned = (
            "the mailbox changed while the search read it in bulk; "
            "searched it one message at a time instead"
        )
        mock_run.return_value = json.dumps(
            {"messages": [_as_record()], "warnings": [realigned, bulk_failure]}
        )
        seen: list[str] = []
        [row] = connector._search_messages_applescript(
            "Gmail", "INBOX", on_warning=seen.append
        )
        assert seen == [realigned, bulk_failure]
        assert {k: row[k] for k in ("to", "cc", "bcc")} == _RECIPIENT_ROWS


class TestDualEmitRfcMessageId:
    """#148: every read-tool row carries an `rfc_message_id` field
    alongside the existing `id` field. On the AppleScript path, `id`
    is Mail.app's internal numeric id and `rfc_message_id` is the
    RFC 5322 Message-ID. Missing-Message-ID cases serialize as None.

    Cross-path consumers (e.g., callers feeding an AppleScript-path
    row to one of the IMAP fast paths from #149/#150/#151/#152) can
    use `rfc_message_id` regardless of which path produced the row."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_applescript_emits_rfc_message_id_in_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The emitted AppleScript record includes the
        `|rfc_message_id|:(message id of msg)` field."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", limit=10)
        script = mock_run.call_args[0][0]
        assert "|rfc_message_id|:(message id of msg)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_applescript_includes_rfc_message_id_in_rows(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The parsed result rows carry `rfc_message_id`."""
        mock_run.return_value = (
            '[{"id": "100", "rfc_message_id": "rfc-100@example.com",'
            '"subject": "Hi", "sender": "a@x", "date_received": "Mon",'
            '"read_status": false, "flagged": false}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert result[0]["id"] == "100"
        assert result[0]["rfc_message_id"] == "rfc-100@example.com"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_applescript_emits_rfc_message_id_in_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The emitted AppleScript record for get_message also
        includes the dual-emit field."""
        mock_run.return_value = (
            '{"id": "100", "rfc_message_id": "rfc-100@example.com",'
            '"subject": "Hi", "sender": "a@x", "date_received": "Mon",'
            '"read_status": false, "flagged": false, "content": ""}'
        )
        connector._get_message_applescript("100", include_content=True)
        script = mock_run.call_args[0][0]
        assert "|rfc_message_id|:(message id of msg)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_applescript_includes_rfc_message_id_in_row(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"id": "100", "rfc_message_id": "rfc-100@example.com",'
            '"subject": "Hi", "sender": "a@x", "date_received": "Mon",'
            '"read_status": false, "flagged": false, "content": "body"}'
        )
        result = connector._get_message_applescript("100", include_content=True)
        assert result["rfc_message_id"] == "rfc-100@example.com"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_missing_message_id_serializes_as_none(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A message without a Message-ID header (drafts, malformed
        mail) yields `rfc_message_id: null` from AppleScript →
        `None` in the Python row. Mocked here at the parsed-JSON
        layer; the AppleScript-side missing-value coercion is handled
        by NSJSONSerialization in `_wrap_as_json_script`."""
        mock_run.return_value = (
            '[{"id": "200", "rfc_message_id": null,'
            '"subject": "draft", "sender": "", "date_received": "Tue",'
            '"read_status": false, "flagged": false}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert result[0]["rfc_message_id"] is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_applescript_preserves_rfc_message_id_in_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """get_thread now KEEPS rfc_message_id in output rows
        (previously stripped). Threading-internal scratch fields
        (in_reply_to / references_raw / references_parsed) are still
        dropped."""
        mock_run.side_effect = [
            '{"account": "Gmail", "rfc_message_id": "anchor@x",'
            '"subject": "Q3", "in_reply_to": "", "references_raw": ""}',
            '[{"id": "100", "rfc_message_id": "anchor@x", "in_reply_to": "",'
            '"references_raw": "", "subject": "Q3", "sender": "a@x",'
            '"date_received": "Mon", "read_status": false, "flagged": false}]'
        ]
        result = connector._get_thread_applescript("100")
        assert len(result) == 1
        assert result[0]["rfc_message_id"] == "anchor@x"
        # Threading scratch fields still stripped.
        for scratch in ("in_reply_to", "references_raw", "references_parsed"):
            assert scratch not in result[0]


class TestMessageIdAppleScriptInjection:
    """Regression guards for AppleScript-injection via message IDs.

    Two bug families this class protects against:

    1. Multi-id list methods (update_message, delete_messages) used to do
       `", ".join(message_ids)` directly into an AppleScript list literal
       — a crafted id containing a `"` could escape the list and inject
       arbitrary script.

    2. Single-id `whose id is "..."` clauses used to interpolate the raw
       message_id without escaping in reply_to_message and forward_message.

    See PR #34 (martparve) for the original report.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_quotes_and_escapes_each_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.delete_messages(['evil"', "ok"])
        script = mock_run.call_args[0][0]
        assert '"evil\\""' in script
        assert '"ok"' in script


class TestBulkOpsSourceMailbox:
    """Regression guards for #103: bulk-mutation methods accept paired
    `account` + `source_mailbox` parameters that narrow the AppleScript
    scan from O(N × accounts × mailboxes) to O(N).

    Both params must be provided together (a mailbox name without an
    account is ambiguous because the same name can exist across accounts).
    Either alone raises ValueError.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------ delete_messages ------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_narrow_path_uses_single_loop(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        connector.delete_messages(
            ["abc"], account="iCloud", source_mailbox="Trash"
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "Trash" of' in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_permanent_emits_deprecation_warning(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Issue #111: Mail.app exposes no AppleScript path to bypass Trash.
        `permanent=True` is a no-op; warn so callers don't silently rely on
        absent behavior."""
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        with pytest.warns(DeprecationWarning, match="#111"):
            connector.delete_messages(
                ["abc"],
                permanent=True,
                account="iCloud",
                source_mailbox="Junk",
            )
        # Script shape unchanged from the non-permanent path: `delete msg`
        # always moves to the account's Trash mailbox today.
        script = mock_run.call_args[0][0]
        assert 'mailbox "Junk" of' in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_default_does_not_warn(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The default path (permanent=False) must not emit DeprecationWarning."""
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            connector.delete_messages(
                ["abc"],
                account="iCloud",
                source_mailbox="Junk",
            )

    def test_delete_messages_partial_pair_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="source_mailbox"):
            connector.delete_messages(["x"], account="iCloud")
        with pytest.raises(ValueError, match="account"):
            connector.delete_messages(["x"], source_mailbox="Trash")


class TestWhoseIdQuoting:
    """Regression guards for #86: the id in a `whose ... is X` clause must
    be wrapped in quotes even when X is already escape_applescript_string'd.

    Without quotes, AppleScript chokes on UUID-style ids like
    'CF7C3761-...@icloud.com' because the dashes/dots/@ get parsed as
    syntax (dash = subtraction, @ = bare identifier, etc.).

    Which PROPERTY is matched depends on the id form: an all-digit id is
    Mail's internal integer `id`, anything else is an RFC 5322 `message id`
    and is angle-bracketed. Both forms must be quoted — that is the
    invariant these tests pin.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"id":"x","subject":"s","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        connector.get_message(uuid_id, include_content=False)
        script = mock_run.call_args[0][0]
        assert f'whose message id is "<{uuid_id}>"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_quotes_numeric_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """All-digit ids match Mail's integer `id` — still quoted."""
        mock_run.return_value = '{"id":"x","subject":"s","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        connector.get_message("12345", include_content=False)
        script = mock_run.call_args[0][0]
        assert 'whose id is "12345"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"attachments":[],"warnings":[]}'
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        connector.get_attachments(uuid_id)
        script = mock_run.call_args[0][0]
        assert f'whose message id is "<{uuid_id}>"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_attachments_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        # Pass 1 enumerates, pass 2 saves; both must quote the id.
        mock_run.side_effect = [
            '{"attachments":[{"name":"a.pdf","mime_type":"application/pdf",'
            '"size":1,"downloaded":true}],"warnings":[]}',
            '{"saved":1,"warnings":[]}',
        ]
        # save_attachments takes a Path (uses .exists()).
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            connector.save_attachments(uuid_id, Path(td))
        # Both passes re-locate the message; every one must quote the id.
        scripts = [c[0][0] for c in mock_run.call_args_list]
        assert len(scripts) == 2, f"expected 2 passes, got {len(scripts)}"
        assert all(
            f'whose message id is "<{uuid_id}>"' in s for s in scripts
        ), f"expected quoted id in every script: {scripts}"


class TestSaveAttachmentsPathTraversal:
    """Pass 2 must not compose its destination from the attachment's own
    declared name.

    That name comes from the message's MIME headers, so it is controlled
    by the sender. The old script built ``"<dir>/" & attName`` from
    ``name of att``; AppleScript's ``POSIX file`` does not normalise the
    string and the filesystem resolves it at write time, so a name
    containing ``..`` writes outside ``save_directory`` — probed
    2026-09-09, see ``safe_attachment_filename``.

    The fix is structural rather than a filter bolted onto the old shape:
    Python already has every name from pass 1, so it sanitizes them and
    hands the safe names to pass 2. The invariant pinned here is that the
    generated AppleScript never derives the path from ``name of att``.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _run_with_name(
        self, connector: AppleMailConnector, mock_run: MagicMock, name: str
    ) -> str:
        """Enumerate one attachment called ``name``; return pass 2's script."""
        import json
        import tempfile
        from pathlib import Path

        mock_run.side_effect = [
            json.dumps(
                {
                    "attachments": [
                        {
                            "name": name,
                            "mime_type": "application/pdf",
                            "size": 1,
                            "downloaded": True,
                        }
                    ],
                    "warnings": [],
                }
            ),
            '{"saved":1,"warnings":[]}',
        ]
        with tempfile.TemporaryDirectory() as td:
            connector.save_attachments("12345", Path(td))
        return mock_run.call_args_list[1][0][0]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_pass_two_does_not_build_the_path_from_name_of_att(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._run_with_name(connector, mock_run, "report.pdf")
        assert "set attName to (name of att)" not in script, (
            "pass 2 still reads the destination filename from Mail; the "
            "name is attacker-controlled and must come from Python"
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_traversing_name_is_reduced_before_it_reaches_the_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._run_with_name(connector, mock_run, "../../escaped.txt")
        assert "../../escaped.txt" not in script
        assert "escaped.txt" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_absolute_name_is_reduced_before_it_reaches_the_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._run_with_name(connector, mock_run, "/etc/passwd")
        assert "/etc/passwd" not in script
        assert "passwd" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_ordinary_name_survives_intact(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        script = self._run_with_name(connector, mock_run, "Q3 report.pdf")
        assert "Q3 report.pdf" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_quotes_in_a_name_are_escaped_not_dropped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # A name is untrusted text going into an AppleScript string
        # literal; it must be escaped like every other user input.
        script = self._run_with_name(connector, mock_run, 'say "hi".pdf')
        assert '\\"hi\\"' in script or '\\"' in script


class TestSaveAttachmentsDoesNotOverwriteUnasked:
    """Mail's ``save ... in`` replaces an existing file without a word
    (probed live 2026-09-11: a sentinel written over a saved attachment
    was back to the attachment's bytes after a second save). So a
    directory the user already had files in was silently clobbered, and
    two attachments sharing a name inside one message collapsed into one
    while ``saved_count`` said two.

    Names are made unique within the batch before the script runs, and a
    name that already exists in the directory is refused before anything
    is written unless ``overwrite=True``.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @staticmethod
    def _enumeration(*names: str) -> str:
        import json

        return json.dumps(
            {
                "attachments": [
                    {"name": n, "mime_type": "application/pdf", "size": 1, "downloaded": True}
                    for n in names
                ],
                "warnings": [],
            }
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_duplicate_names_in_one_message_get_distinct_files(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [
            self._enumeration("report.pdf", "report.pdf", "notes"),
            '{"saved":3,"warnings":[]}',
        ]
        connector.save_attachments("12345", tmp_path)
        script = mock_run.call_args_list[1][0][0]
        assert '"report.pdf"' in script
        assert '"report (2).pdf"' in script
        assert '"notes"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_an_existing_file_is_refused_before_the_save_script_runs(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        (tmp_path / "report.pdf").write_bytes(b"mine")
        mock_run.side_effect = [self._enumeration("report.pdf", "other.pdf")]
        with pytest.raises(FileExistsError, match="report.pdf") as refused:
            connector.save_attachments("12345", tmp_path)
        # The refusal says what happened and what to do; the tool passes
        # it through as it is.
        assert "nothing was written" in str(refused.value)
        assert "overwrite=True" in str(refused.value)
        assert mock_run.call_count == 1, "pass 2 must not run"
        assert (tmp_path / "report.pdf").read_bytes() == b"mine"
        assert not (tmp_path / "other.pdf").exists(), "nothing is written on a refusal"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_overwrite_true_replaces_an_existing_file(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        (tmp_path / "report.pdf").write_bytes(b"mine")
        mock_run.side_effect = [
            self._enumeration("report.pdf"),
            '{"saved":1,"warnings":[]}',
        ]
        saved, warnings = connector.save_attachments("12345", tmp_path, overwrite=True)
        assert saved == 1
        assert mock_run.call_count == 2

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_only_the_selected_indices_are_checked(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """A collision on an attachment the caller did not ask for is not
        a collision."""
        (tmp_path / "report.pdf").write_bytes(b"mine")
        mock_run.side_effect = [
            self._enumeration("report.pdf", "other.pdf"),
            '{"saved":1,"warnings":[]}',
        ]
        saved, _ = connector.save_attachments("12345", tmp_path, attachment_indices=[1])
        assert saved == 1


class TestSaveAttachmentsRefusesIndicesTheMessageDoesNotHave:
    """attachment_indices past the end were silently dropped, so asking
    for attachment 5 of a two-attachment message returned success with
    nothing saved, and [0, 5] saved one file and said nothing about the
    other. Indices are 0-based positions in the message's attachment
    list; one the message does not have is a caller error, refused by
    name before anything is written."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector()

    @staticmethod
    def _enumeration(*names: str) -> str:
        rows = ",".join(
            f'{{"name":"{n}","mime_type":"application/pdf","size":10,"downloaded":true}}'
            for n in names
        )
        return f'{{"attachments":[{rows}],"warnings":[]}}'

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_message_that_is_not_there_is_named(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """The lookup script's error says only "not found"; the id is
        named here because the tool passes the message through as it is."""
        mock_run.side_effect = MailMessageNotFoundError(
            "execution error: Can't get message: not found (-2700)"
        )
        with pytest.raises(MailMessageNotFoundError, match="'12345'"):
            connector.save_attachments("12345", tmp_path)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_an_index_past_the_end_is_refused_by_name(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [self._enumeration("a.pdf", "b.pdf")]
        with pytest.raises(ValueError, match=r"index 5 .*2 attachments.*0 to 1"):
            connector.save_attachments("12345", tmp_path, attachment_indices=[5])
        assert mock_run.call_count == 1, "pass 2 must not run"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_bad_index_beside_good_ones_writes_nothing(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [self._enumeration("a.pdf", "b.pdf")]
        with pytest.raises(ValueError, match=r"index -1"):
            connector.save_attachments("12345", tmp_path, attachment_indices=[0, -1])
        assert mock_run.call_count == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_message_with_no_attachments_still_reports_zero(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """No attachments at all is not a bad index: the existing
        (0, warnings) contract for that case stands."""
        mock_run.side_effect = [self._enumeration()]
        saved, _ = connector.save_attachments("12345", tmp_path, attachment_indices=[0])
        assert saved == 0


class TestAttachmentPropertyGuards:
    """One unreadable attachment PROPERTY must not kill the whole walk.

    Real-world case (2026-08-27, iCloud INBOX messages 1463-1466): the
    ``MIME type`` property of a PDF attachment raises errAEEventNotHandled
    (-10000) while name/file size/downloaded all read cleanly. The old
    enumeration built its record with all four property reads in ONE
    expression, so the single bad property aborted the walk, tripped the
    whole-walk -10000 guard, and search/get/save all degraded to
    "attachment enumeration failed" — blocking retrieval of perfectly
    saveable files.

    Invariant pinned here: every attachment-record property is read into
    its own variable under its own try block (record built from the
    variables), in BOTH script generators — the shared
    ``_enumerate_attachments_for_message`` helper and the
    ``_search_messages_applescript`` include_attachments clause.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _assert_per_property_guards(self, script: str) -> None:
        # Record must be built from pre-read variables...
        assert "|mime_type|:attMime" in script
        assert "|name|:attName" in script
        assert "|size|:attSize" in script
        assert "|downloaded|:attDown" in script
        # ...never from inline property reads (the one-expression form
        # that lets a single bad property abort the walk).
        assert "|mime_type|:(MIME type of att)" not in script
        assert "|name|:(name of att)" not in script
        # Each fragile read sits under its own try.
        assert "set attMime to (MIME type of att)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_enumerate_helper_guards_each_property(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"attachments":[],"warnings":[]}'
        connector._enumerate_attachments_for_message("1463")
        self._assert_per_property_guards(mock_run.call_args[0][0])

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_clause_guards_each_property(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"messages":[],"warnings":[]}'
        connector._search_messages_applescript(
            "Test Account", include_attachments=True
        )
        self._assert_per_property_guards(mock_run.call_args[0][0])

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_property_failure_surfaces_as_warning_not_empty_walk(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A degraded record (empty mime_type + warning) passes through —
        the Python side must not drop it or mistake it for a failure."""
        mock_run.return_value = (
            '{"attachments":[{"name":"3D RATIONALITY.pdf","mime_type":"",'
            '"size":4216772,"downloaded":true}],'
            '"warnings":["attachment property MIME type unreadable for '
            "message 1463 attachment '3D RATIONALITY.pdf': ... (error "
            '-10000)"]}'
        )
        attachments, warnings = connector._enumerate_attachments_for_message(
            "1463"
        )
        assert attachments == [
            {
                "name": "3D RATIONALITY.pdf",
                "mime_type": "",
                "size": 4216772,
                "downloaded": True,
            }
        ]
        assert len(warnings) == 1
        assert "MIME type" in warnings[0]

    @patch.object(AppleMailConnector, "_enumerate_attachments_for_message")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_proceeds_when_a_property_warned_but_files_exist(
        self,
        mock_run: MagicMock,
        mock_enum: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """A property-level warning must NOT block the save.

        ``save_attachments`` used to bail on ANY warning
        (``if warnings: return 0, warnings``) — correct back when the only
        possible warning was the whole-walk failure, where the list really
        is empty. A per-property warning leaves a perfectly saveable
        attachment in the list, and Mail.app saves it fine (verified live
        2026-08-27 on iCloud INBOX 1463/1465/1466 — the files came out
        byte-for-byte at their reported sizes). Bail on "no attachments",
        never on "a property was blank".
        """
        warning = (
            "attachment property MIME type unreadable for message 1463: "
            "Mail got an error: AppleEvent handler failed. (error -10000)"
        )
        mock_enum.return_value = (
            [
                {
                    "name": "3D RATIONALITY.pdf",
                    "mime_type": "",
                    "size": 4216772,
                    "downloaded": True,
                }
            ],
            [warning],
        )
        mock_run.return_value = '{"saved":1,"warnings":[]}'
        with tempfile.TemporaryDirectory() as td:
            saved, warnings = connector.save_attachments("1463", Path(td))
        assert saved == 1, "the save pass must run despite the warning"
        assert warnings == [warning], "the warning must still be surfaced"
        assert mock_run.called, "pass 2 (the actual save) never ran"

    @patch.object(AppleMailConnector, "_enumerate_attachments_for_message")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_still_bails_when_whole_walk_failed(
        self,
        mock_run: MagicMock,
        mock_enum: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Whole-walk failure => no references => nothing to save, and the
        warning is still returned to the caller."""
        warning = (
            "attachment enumeration failed for message 1463: Mail got an "
            "error: AppleEvent handler failed. (error -10000)"
        )
        mock_enum.return_value = ([], [warning])
        with tempfile.TemporaryDirectory() as td:
            saved, warnings = connector.save_attachments("1463", Path(td))
        assert saved == 0
        assert warnings == [warning]
        assert not mock_run.called, "must not attempt a save with no refs"

    @patch.object(AppleMailConnector, "_enumerate_attachments_for_message")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_pass2_uses_posix_file_reference(
        self,
        mock_run: MagicMock,
        mock_enum: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Pass 2 must build the destination with ``POSIX file``.

        AGENTS.md: "Use POSIX file references (POSIX file "/path/to/file")
        in AppleScript." The bare-string form violated that. Probed live
        2026-08-27 with the same attachment saved both ways: the bare
        string raised -10000 under /private/tmp ("To view or change
        permissions...") while POSIX file succeeded; under ~/Downloads
        BOTH succeeded. So the bare string is not universally broken —
        it is strictly more fragile, and POSIX file is the form that
        worked everywhere probed.
        """
        mock_enum.return_value = (
            [{"name": "a.pdf", "mime_type": "", "size": 1, "downloaded": True}],
            [],
        )
        mock_run.return_value = '{"saved":1,"warnings":[]}'
        with tempfile.TemporaryDirectory() as td:
            connector.save_attachments("1463", Path(td))
        script = mock_run.call_args[0][0]
        assert "POSIX file" in script, "pass 2 must use a POSIX file ref"

    @patch.object(AppleMailConnector, "_enumerate_attachments_for_message")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_surfaces_per_attachment_failure_not_a_silent_zero(
        self,
        mock_run: MagicMock,
        mock_enum: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """A failed save must produce a WARNING, never a bare 0.

        Pass 2 wrapped each save in an unqualified ``try`` with no
        on-error branch, so a failure decremented nothing and vanished:
        the caller got ``(0, [])`` — "no files, no reason". That is the
        silent-failure mode this project forbids, and it cost a real
        debugging cycle on 2026-08-27.
        """
        mock_enum.return_value = (
            [{"name": "a.pdf", "mime_type": "", "size": 1, "downloaded": True}],
            [],
        )
        mock_run.return_value = (
            '{"saved":0,"warnings":["attachment save failed for '
            "'a.pdf' of message 1463: Mail got an error: ... (error "
            '-10000)"]}'
        )
        with tempfile.TemporaryDirectory() as td:
            saved, warnings = connector.save_attachments("1463", Path(td))
        assert saved == 0
        assert len(warnings) == 1, "a failed save must explain itself"
        assert "save failed" in warnings[0]


class TestComposition:
    """The one composition behind every draft the connector saves and
    every message it sends (``_compose``): the window it opens
    (``_build_open_compose_script``), and the paste placements it
    uses. Each pin is a measurement: the loopback
    read-back of 2026-09-27 (docs/research/icloud-draft-resync.md,
    Observation 10), the re-save spike of the same day
    (docs/research/draft-resave-spike.md), or the paste-focus failure of
    2026-09-05 (docs/research/paste-focus-failed.md).
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _compose(
        self,
        connector: AppleMailConnector,
        sender: str | None = None,
        *,
        seed: str = "new",
        to: list[str] | None = None,
        operation: WindowOperation = "save",
    ) -> str:
        return connector._build_open_compose_script(
            seed=seed,
            seed_id=None if seed == "new" else "160989",
            reply_all=False,
            to=["a@example.com"] if to is None else to,
            cc=["c@example.com"],
            bcc=["b@example.com"],
            subject='Say "hi"',
            sender=sender,
            operation=operation,
        )

    def test_the_window_is_visible_and_its_body_seeded(
        self, connector: AppleMailConnector
    ) -> None:
        """An empty body's WebArea refused keyboard focus by every route
        tried on 2026-09-05; any content fixed it."""
        script = self._compose(connector)
        assert "make new outgoing message" in script
        assert 'visible:true, content:" "' in script

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_reply_or_forward_is_mails_own_window(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail's verb, opening its window: without one, every edit of
        what Mail wrote replaced it (measured 2026-09-26), and a draft
        saved without one was re-saved under a new id
        (draft-resave-spike.md)."""
        script = self._compose(connector, seed=seed)
        assert 'whose id is "160989"' in script
        assert f"{seed} origMsg opening window true" in script
        assert "make new outgoing message" not in script
        assert "opening window false" not in script

    def test_recipients_and_subject_go_through_the_model_escaped(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._compose(connector, to=['a"b\x00@example.com'])
        for kind, addr in (("to", 'a\\"b@example.com'), ("cc", "c@example.com"),
                           ("bcc", "b@example.com")):
            assert f"delete (every {kind} recipient of theMessage)" in script
            assert f'repeat with addr in {{"{addr}"}}' in script
            assert (
                f"make new {kind} recipient at end of {kind} recipients of "
                "theMessage with properties {address:addr}"
            ) in script
        assert "\x00" not in script
        assert 'subject:"Say \\"hi\\""' in script

    @pytest.mark.parametrize("seed", ["new", "reply", "forward"])
    def test_no_file_is_attached_through_the_dictionary(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """``make new attachment`` after the body was pasted sent Mail's
        empty cite blockquote again, and on a reply whose body was never
        touched it unquoted the original; files are pasted instead."""
        assert "make new attachment" not in self._compose(connector, seed=seed)

    def test_a_named_sender_is_set_last(
        self, connector: AppleMailConnector
    ) -> None:
        """Set before the recipients, the sender cost a saved draft its
        recipients (icloud-draft-resync.md, Observation 6)."""
        script = self._compose(connector, sender="Alice Smith <me@example.com>")
        sender_at = script.index(
            'set sender of theMessage to "Alice Smith <me@example.com>"'
        )
        assert sender_at > script.rindex("make new bcc recipient")

    def test_the_sender_is_sanitized_and_escaped(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._compose(connector, sender='Al"ice\x00 <me@example.com>')
        assert "\x00" not in script
        assert 'set sender of theMessage to "Al\\"ice <me@example.com>"' in script

    def test_no_sender_leaves_mails_default(
        self, connector: AppleMailConnector
    ) -> None:
        assert "set sender" not in self._compose(connector)

    def test_the_window_is_found_by_counted_names(
        self, connector: AppleMailConnector
    ) -> None:
        """Never guessed from the subject: a window of the same name
        already open (a Mail with several "New Message" windows, say) is
        refused, not pasted into."""
        script = self._compose(connector)
        assert "set beforeNames to name of windows" in script
        assert "afterCount > beforeCount" in script
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in script
        assert "|window|:newName" in script

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_window_whose_subject_is_set_is_named_again_after(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail retitles a compose window the moment its subject is set
        (measured 2026-09-27, docs/research/icloud-draft-resync.md,
        Observation 12): named only before the override, a reply or
        forward with its own subject was addressed by a name no window
        had, and the paste failed NO_BODY_AREA."""
        script = self._compose(connector, seed=seed)
        subject_at = script.index("set subject of theMessage")
        assert script.index("afterCount > beforeCount") < subject_at
        assert script.rindex("afterCount > beforeCount") > subject_at
        assert script.rindex("afterCount > beforeCount") < script.index(
            "|window|:newName"
        )

    @pytest.mark.parametrize("seed", ["new", "reply", "forward"])
    def test_a_window_whose_subject_is_not_set_is_named_once(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """A fresh message's subject is set as it is made, and a reply or
        forward without an override keeps Mail's: nothing retitles it."""
        script = connector._build_open_compose_script(
            seed=seed, seed_id=None if seed == "new" else "160989",
            reply_all=False, to=["a@example.com"], cc=None, bcc=None,
            subject="Fresh" if seed == "new" else None, sender=None,
            operation="save",
        )
        assert script.count("afterCount > beforeCount") == 1

    def test_what_mail_will_send_is_read_back_from_the_model(
        self, connector: AppleMailConnector
    ) -> None:
        """The subject and recipients the window holds, after every
        override: a send is gated on them, and a save finds its draft by
        that subject."""
        script = self._compose(connector)
        assert "|subject|:(subject of theMessage as text)" in script
        for group in ("to", "cc", "bcc"):
            assert f"address of {group} recipients of theMessage" in script
            assert f"|{group}|:" in script

    @pytest.mark.parametrize(
        ("operation", "mailbox", "other"),
        [
            ("save", "drafts mailbox", "sent mailbox"),
            ("send", "sent mailbox", "drafts mailbox"),
        ],
    )
    def test_the_mailbox_its_ending_is_found_in_is_snapshot_before_it_opens(
        self,
        connector: AppleMailConnector,
        operation: WindowOperation,
        mailbox: str,
        other: str,
    ) -> None:
        """A saved draft is the Drafts entry that was not there before,
        and a sent message's copy the Sent entry: the subject alone found
        an older message of the same subject."""
        script = self._compose(connector, operation=operation)
        snapshot_at = script.index(
            f"set beforeIds to (id of every message of {mailbox})"
        )
        assert snapshot_at < script.index("make new outgoing message")
        assert "|before_ids|:beforeIds" in script
        assert f"id of every message of {other}" not in script

    def test_the_fresh_body_paste_replaces_everything(
        self, connector: AppleMailConnector
    ) -> None:
        """Select all, delete, then paste. Pasted above the seed instead,
        the sent message carried an empty <blockquote type="cite">."""
        script = connector._build_paste_script(
            window_name="S", fill=connector._text_paste_fill("hi", plain=True),
            placement="replace", undo_first=False,
        )
        select_at = script.index('keystroke "a" using command down')
        delete_at = script.index("key code 51")
        paste_at = script.index('keystroke "v" using command down')
        assert select_at < delete_at < paste_at
        assert "public.utf8-plain-text" in script

    def test_a_paste_above_what_mail_wrote_never_selects_or_deletes(
        self, connector: AppleMailConnector
    ) -> None:
        """A reply's quote and a forward's attachments live in the body:
        a select-all or a delete there would take them."""
        script = connector._build_paste_script(
            window_name="S", fill=connector._text_paste_fill("<p>hi</p>", plain=False),
            placement="above", undo_first=False,
        )
        assert "key code 126 using command down" in script
        assert 'keystroke "a" using command down' not in script
        assert "key code 51" not in script
        assert "public.html" in script

    def test_files_are_pasted_as_file_urls_at_the_end(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        script = connector._build_paste_script(
            window_name="S", fill=connector._files_paste_fill([f]),
            placement="end", undo_first=False,
        )
        assert "writeObjects:fileURLs" in script
        assert f.resolve().as_posix() in script
        end_at = script.index("key code 125 using command down")
        assert end_at < script.index('keystroke "v" using command down')
        assert 'keystroke "a" using command down' not in script
        assert "key code 51" not in script

    def test_a_file_paste_that_fails_is_salvaged_and_nothing_sent(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        captured = _scripted(
            connector, ["PASTE_FOCUS_FAILED:body area would not take focus"]
        )
        with pytest.raises(MailAppleScriptError, match="PASTE_FOCUS_FAILED"):
            connector._paste_attachments(
                _ComposeWindow(
                    name="S", subject="S", to=[], cc=[], bcc=[], before_ids=[],
                    window_id=2781,
                ),
                [f],
            )
        # file paste, then the salvage (close, Save) of Mail's window
        # 2781; no AX verify, no send.
        assert len(captured) == 2
        assert captured[1].startswith("set closeId to 2781\n")


class TestWrapAsJsonScript:
    def test_wrapper_contains_framework_directive(self) -> None:
        script = _wrap_as_json_script(
            'tell application "Mail"\n    set resultData to {}\nend tell',
            timeout=60,
        )
        assert 'use framework "Foundation"' in script
        assert "use scripting additions" in script

    def test_wrapper_appends_json_serialization(self) -> None:
        script = _wrap_as_json_script(
            'tell application "Mail"\n    set resultData to {}\nend tell',
            timeout=60,
        )
        assert "NSJSONSerialization" in script
        assert "dataWithJSONObject:resultData" in script

    def test_wrapper_preserves_body(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body, timeout=60)
        assert body in script

    def test_wrapper_orders_framework_before_body_before_epilogue(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body, timeout=60)
        framework_idx = script.index('use framework "Foundation"')
        body_idx = script.index(body)
        epilogue_idx = script.index("NSJSONSerialization")
        assert framework_idx < body_idx < epilogue_idx

    # Regression coverage for issue #227 — Mail's default AppleEvent timeout
    # is 60 s, so without an explicit `with timeout` clause an Exchange/EWS
    # iteration that takes 70 s raises `AppleEvent timed out (-1712)` no
    # matter what subprocess timeout the connector was constructed with.

    def test_wrapper_includes_with_timeout_clause(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = _wrap_as_json_script(body, timeout=180)
        assert "with timeout of 180 seconds" in script
        assert "end timeout" in script

    def test_wrapper_timeout_brackets_the_tell_body(self) -> None:
        """The tell block must be inside the with-timeout block — putting it
        outside leaves the AppleEvent default of 60 s in force."""
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = _wrap_as_json_script(body, timeout=180)
        with_idx = script.index("with timeout of 180 seconds")
        body_idx = script.index(body)
        end_timeout_idx = script.index("end timeout")
        assert with_idx < body_idx < end_timeout_idx

    @pytest.mark.parametrize("timeout", [30, 60, 120, 300, 600])
    def test_wrapper_emits_caller_supplied_timeout(self, timeout: int) -> None:
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = _wrap_as_json_script(body, timeout=timeout)
        assert f"with timeout of {timeout} seconds" in script


class TestConnectorThreadsTimeoutIntoScripts:
    """Issue #227 — AppleMailConnector(timeout=N) must propagate N into the
    AppleScript `with timeout` clause, not just into subprocess.run."""

    @patch("subprocess.run")
    def test_list_accounts_emits_connector_timeout(
        self, mock_run: MagicMock
    ) -> None:
        connector = AppleMailConnector(timeout=240)
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        connector.list_accounts()
        script = mock_run.call_args.kwargs.get("input") or mock_run.call_args.args[0]
        assert "with timeout of 240 seconds" in script

    @patch("subprocess.run")
    def test_search_messages_applescript_emits_connector_timeout(
        self, mock_run: MagicMock
    ) -> None:
        connector = AppleMailConnector(timeout=300)
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        connector._search_messages_applescript(
            account="Exchange",
            mailbox="Inbox",
            sender_contains="ofca",
            limit=10,
        )
        script = mock_run.call_args.kwargs.get("input") or mock_run.call_args.args[0]
        assert "with timeout of 300 seconds" in script


class TestAutoTemplateVars:
    """auto_template_vars() builds the auto-fill dict for render_template."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_no_message_id_returns_only_today(
        self, connector: AppleMailConnector
    ) -> None:
        result = connector.auto_template_vars(message_id=None)
        assert set(result.keys()) == {"today"}
        # ISO date format
        assert len(result["today"]) == 10
        assert result["today"][4] == "-" and result["today"][7] == "-"

    @patch.object(AppleMailConnector, "get_message")
    def test_with_message_id_extracts_sender_fields(
        self, mock_get: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_get.return_value = {
            "id": "abc",
            "subject": "Project Q3 plan",
            "sender": "Alice Smith <alice@example.com>",
            "content": "...",
        }
        result = connector.auto_template_vars(message_id="abc")
        assert result["recipient_name"] == "Alice Smith"
        assert result["recipient_email"] == "alice@example.com"
        assert result["original_subject"] == "Project Q3 plan"
        assert "today" in result
        # Confirm we called get_message without fetching content
        mock_get.assert_called_once_with("abc", include_content=False)

    @patch.object(AppleMailConnector, "get_message")
    def test_sender_without_display_name_falls_back_to_email(
        self, mock_get: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Sender field is just an email, no display name
        mock_get.return_value = {
            "id": "x",
            "subject": "hi",
            "sender": "bob@example.com",
            "content": "",
        }
        result = connector.auto_template_vars(message_id="x")
        # When no display name, recipient_name falls back to the email
        assert result["recipient_name"] == "bob@example.com"
        assert result["recipient_email"] == "bob@example.com"


class TestDeleteDraft:
    """Tests for AppleMailConnector.delete_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        assert connector.delete_draft("160991") is True
        mock_run.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_script_embeds_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        connector.delete_draft("160991")
        script = mock_run.call_args[0][0]
        assert 'whose id is "160991"' in script
        # Scoped to Mail's own aggregate drafts mailbox, which covers every
        # account's drafts under whatever name the locale gives it; not to
        # mailboxes whose English name happens to contain "Drafts".
        assert "message of drafts mailbox" in script
        assert 'contains "Drafts"' not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.delete_draft("999999")

    def test_delete_draft_invalid_id_path_traversal(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("../etc/passwd")

    def test_delete_draft_invalid_id_with_quotes(
        self, connector: AppleMailConnector
    ) -> None:
        # Quote injection that could break out of the AppleScript string.
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft('1"; do something --')

    def test_delete_draft_empty_id(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_strips_whitespace_from_result(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # AppleScript output sometimes carries trailing newlines.
        mock_run.return_value = "OK\n"
        assert connector.delete_draft("160991") is True


class TestFindMessageByMessageId:
    """Tests for AppleMailConnector.find_message_by_message_id."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_on_match(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989"
        result = connector.find_message_by_message_id(
            "<calendar-abc123@google.com>"
        )
        assert result == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        result = connector.find_message_by_message_id(
            "<missing@example.com>"
        )
        assert result is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_empty_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        result = connector.find_message_by_message_id("")
        assert result is None
        # No need to call AppleScript for an empty input.
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_trailing_whitespace(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989\n"
        assert connector.find_message_by_message_id("<x@y>") == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_message_id_with_quotes_is_escaped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Quotes/backslashes in the Message-ID must be escaped to prevent
        AppleScript injection. Real Message-IDs almost never contain these
        but we shouldn't trust the wire."""
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id('<weird"id@host>')
        script = mock_run.call_args[0][0]
        # Escaped quote inside the AppleScript string literal.
        assert '\\"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_whose_message_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("<x@y>")
        script = mock_run.call_args[0][0]
        # Compound clause queries both bare and bracketed forms (#205 follow-up).
        assert "whose" in script
        assert "message id is" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bracketless_input_queries_both_forms(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail.app's ``message id`` storage normalization varies by
        account: IMAP-backed accounts (iCloud, Gmail) store the bare RFC
        Message-ID; some other paths may store with angle brackets. The
        resolver therefore queries both forms in a single ``whose``
        clause so a caller doesn't need to know the storage convention.
        """
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("abc@example.com")
        script = mock_run.call_args[0][0]
        assert 'message id is "abc@example.com"' in script
        assert 'message id is "<abc@example.com>"' in script
        assert "whose" in script and " or " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bracketed_input_queries_both_forms(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Existing callers (e.g. update_draft passing In-Reply-To)
        already include brackets; strip them and query both forms so
        we don't depend on Mail.app's storage convention.
        """
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("<abc@example.com>")
        script = mock_run.call_args[0][0]
        assert 'message id is "abc@example.com"' in script
        assert 'message id is "<abc@example.com>"' in script
        assert "<<" not in script and ">>" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_for_bare_rfc_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Read tools (#148) emit bare RFC ids on the IMAP path. Round-trip
        through ``create_draft(reply_to=...)`` requires this call to return
        Mail's internal id when the input is bare. Unit test asserts API
        surface; an integration test asserts the AppleScript actually
        matches against Mail.app's storage.
        """
        mock_run.return_value = "54957"
        result = connector.find_message_by_message_id(
            "1779175169746.aa805a12-74b6-4330-93ff-72a175ed8679@example.com"
        )
        assert result == "54957"


class TestGetDraftState:
    """Tests for AppleMailConnector.get_draft_state."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_full_draft_state(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"160991",'
            '"to":["a@example.com","b@example.com"],'
            '"cc":["c@example.com"],"bcc":[],'
            '"subject":"Re: hello",'
            '"body":"hi there\\n\\n-- original --","in_reply_to":"<orig@x>",'
            '"references":"<orig@x>",'
            '"attachment_names":["report.pdf"]}'
        )
        state = connector.get_draft_state("160991")
        assert state == {
            "draft_id": "160991",
            "to": ["a@example.com", "b@example.com"],
            "cc": ["c@example.com"],
            "bcc": [],
            "subject": "Re: hello",
            "body": "hi there\n\n-- original --",
            "in_reply_to": "<orig@x>",
            "references": "<orig@x>",
            "attachment_names": ["report.pdf"],
        }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reads_the_sender_back(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The draft's own sender is part of its state, so update_draft can
        rebuild it in the same account instead of Mail's default."""
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[],"sender":"Alice Smith <alice@icloud.com>"}'
        )
        state = connector.get_draft_state("x")
        assert state["sender"] == "Alice Smith <alice@icloud.com>"
        script = mock_run.call_args.args[0]
        assert "sender of foundDraft" in script
        assert "|sender|:" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reads_the_account_back(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A draft id names a draft in any account. The account the draft
        sits in is part of its state, read from the mailbox Mail resolves
        the draft to, so a tool acting by id can say which account it is
        about to touch."""
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[],"sender":"","account":"Work"}'
        )
        state = connector.get_draft_state("x")
        assert state["account"] == "Work"
        script = mock_run.call_args.args[0]
        assert "name of account of mailbox of foundDraft" in script
        assert "|account|:" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        with pytest.raises(MailDraftNotFoundError):
            connector.get_draft_state("999999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_internal_found_flag(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert "found" not in state

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_handles_empty_recipient_lists(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert state["to"] == []
        assert state["cc"] == []
        assert state["bcc"] == []
        assert state["attachment_names"] == []

    def test_invalid_id_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.get_draft_state("../escape")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_iterates_drafts_mailboxes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        # Scoped to Mail's aggregate drafts mailbox (locale-independent),
        # not to mailboxes named "Drafts" in English.
        assert "messages of drafts mailbox" in script
        assert 'contains "Drafts"' not in script
        # Should use as-text id comparison (probes showed numeric whose
        # clauses are unreliable on IMAP-backed Drafts), and read each
        # id inside a try: a draft that vanished between the listing and
        # the walk reaching it (Mail's re-save of a draft with a named
        # sender) is skipped, not a failure of the whole read.
        assert "set candId to (id of d as text)" in script
        assert "if candId is targetId then" in script
        walk = script[script.index("repeat with d in messages of drafts mailbox"):]
        assert walk.index("try") < walk.index("id of d as text")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_reads_threading_headers(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        assert '"In-Reply-To"' in script
        assert '"References"' in script


class TestCreateDraft:
    """Tests for AppleMailConnector.create_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_seed_raises(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed must be"):
            connector.create_draft(seed="bogus")

    def test_reply_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="reply")

    def test_forward_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="forward")

    def test_new_requires_to(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'to' is required"):
            connector.create_draft(seed="new", subject="hi", body="x")

    def test_new_requires_subject(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'subject' is required"):
            connector.create_draft(seed="new", to=["x@example.com"], body="x")

    # ------------------------------------------------------------------
    # Fresh seed (`seed='new'`)
    # ------------------------------------------------------------------

    def _save(
        self,
        connector: AppleMailConnector,
        *,
        seed: str = "new",
        body: str = "hello",
        outcomes: list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], dict[str, str]]:
        """Save a draft through create_draft against a well-behaved Mail
        (or ``outcomes``), returning every script run and the result."""
        if seed == "new":
            kwargs.setdefault("to", ["a@example.com"])
            kwargs.setdefault("subject", "hi")
        else:
            kwargs.setdefault("seed_id", "160989")
        files = kwargs.get("attachment_paths") or []
        captured = _scripted(
            connector,
            outcomes or _compose_outcomes(
                body, window="hi", seed=seed, plain=True, send=False,
                draft_id="161055", file_names=[Path(f).name for f in files],
            ),
        )
        result = connector.create_draft(seed=seed, body=body, **kwargs)
        return captured, result

    def test_new_save_returns_draft_id(
        self, connector: AppleMailConnector
    ) -> None:
        _, result = self._save(connector)
        assert result == {"draft_id": "161055", "sent_message_id": ""}

    def test_a_fresh_draft_is_composed_in_a_window_and_saved_from_it(
        self, connector: AppleMailConnector
    ) -> None:
        """A draft saved through the dictionary (``set content``, then
        ``save``) was re-saved by Mail under a new id when it named a
        sender (draft-resave-spike.md), held its body inside Mail's
        ``<blockquote type="cite">`` (icloud-draft-resync.md,
        Observation 10), and left a hidden outgoing message behind. A
        fresh draft is composed as a fresh send is, then closed with
        Save: open, paste over the seed, read back, close, find the id."""
        scripts, _ = self._save(
            connector, to=["a@example.com", "b@example.com"],
            cc=["c@example.com"], body="line1\nline2",
        )
        assert len(scripts) == 5
        open_s, paste_s, readback_s, close_s, find_s = scripts
        assert 'visible:true, content:" "' in open_s
        assert 'subject:"hi"' in open_s
        assert '{"a@example.com", "b@example.com"}' in open_s
        assert '{"c@example.com"}' in open_s
        assert "set beforeIds to (id of every message of drafts mailbox)" in open_s
        assert "public.utf8-plain-text" in paste_s
        assert "line1\nline2" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "my tendClickClose(closeIdx)" in close_s
        assert 'click button "Save" of sheet 1 of window closeIdx' in close_s
        assert 'messages of drafts mailbox whose subject is "hi"' in find_s
        assert "{5, 6}" in find_s

    @pytest.mark.parametrize(
        ("seed", "body"),
        [("new", "x"), ("new", ""), ("reply", "x"), ("reply", ""),
         ("forward", "x"), ("forward", "")],
    )
    def test_no_draft_is_saved_through_the_dictionary(
        self, connector: AppleMailConnector, seed: str, body: str
    ) -> None:
        """Every seed, with a body or without, is saved from a window
        closed with Save: never Mail's ``save`` verb, never ``content``,
        never a message made without a window."""
        scripts, result = self._save(connector, seed=seed, body=body)
        assert result["draft_id"] == "161055"
        joined = "\n".join(scripts)
        assert "save theMessage" not in joined
        assert "set content" not in joined
        assert "visible:false" not in joined
        assert "opening window false" not in joined
        assert "AXCloseButton" in scripts[-2]

    def test_an_empty_fresh_body_still_replaces_the_seed(
        self, connector: AppleMailConnector
    ) -> None:
        """The one-space seed sits inside Mail's cite blockquote; left in
        place it would be saved and sent quoted. An empty body is an
        empty paste over everything, which arrived empty and unquoted
        (icloud-draft-resync.md, Observation 10)."""
        scripts, _ = self._save(connector, body="")
        assert 'keystroke "a" using command down' in scripts[1]
        assert "key code 51" in scripts[1]

    def test_a_save_whose_draft_never_appears_is_an_error(
        self, connector: AppleMailConnector
    ) -> None:
        """The new draft's id is found by diffing Drafts before and after
        the save, and the draft takes a moment to appear (measured: none
        of three lookups saw it at 0.5 s, all did by 1.5 s). The script
        polls for it with a bound; if it still has not appeared, that is
        reported, not returned as success with an empty id that every
        later call rejects."""
        from apple_mail_mcp.exceptions import MailDraftNotSettledError

        outcomes = _compose_outcomes(
            "hello", window="hi", plain=True, send=False, draft_id="",
        )
        with pytest.raises(MailDraftNotSettledError, match="did not appear"):
            self._save(connector, outcomes=outcomes)

    def test_the_save_polls_for_the_new_draft(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._save(connector)
        find_s = scripts[-1]
        assert "delay 0.5\n" not in find_s, "a fixed delay is a guess"
        assert "repeat with attempt from 1 to" in find_s
        assert "messages of drafts mailbox whose subject is" in find_s

    def test_a_save_sends_nothing(self, connector: AppleMailConnector) -> None:
        scripts, _ = self._save(connector)
        assert all("click sendBtn" not in s for s in scripts)

    def test_new_send_returns_its_sent_copys_ids(
        self, connector: AppleMailConnector
    ) -> None:
        _scripted(connector, _compose_outcomes("hello", window="hi", plain=True))
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="hello",
            send_now=True,
        )
        assert result == _SENT

    def test_new_send_composes_a_window_and_pastes_the_body_as_plain_text(
        self, connector: AppleMailConnector
    ) -> None:
        """seed="new" + send_now=True is the fresh composition: a visible
        compose window, the body pasted as plain text over the seed and
        read back, then the verified send. The body never goes through
        ``content`` (it arrives inside <blockquote type="cite">, which
        iOS Mail draws as a purple bar), and there is no mailto: URL (it
        can neither name a sender nor carry a file)."""
        captured = _scripted(
            connector, _compose_outcomes("line1\nline2", window="hi", plain=True)
        )
        connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="line1\nline2",
            send_now=True,
        )
        assert len(captured) == 6
        compose_s, paste_s, readback_s, send_s, _, _ = captured
        assert "make new outgoing message" in compose_s
        assert all("open location" not in s for s in captured)
        assert all("mailto:" not in s for s in captured)
        assert all("set content" not in s for s in captured)
        assert "public.utf8-plain-text" in paste_s
        assert "line1\nline2" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "click sendBtn" in send_s

    def test_the_window_mail_opened_is_the_one_pasted_into_and_sent(
        self, connector: AppleMailConnector
    ) -> None:
        """Every later step addresses the window the compose script
        reported, not a name guessed from the subject, and the look for
        its Sent copy goes by the subject Mail holds."""
        outcomes = _compose_outcomes("x", window="the new one", plain=True)
        outcomes[0] = _compose_meta("the new one", subject="hi")
        captured = _scripted(connector, outcomes)
        connector.create_draft(
            seed="new", to=["a@example.com"], subject="hi", body="x",
            send_now=True,
        )
        for script in captured[1:4]:
            assert 'set composeName to "the new one"' in script or (
                'window "the new one"' in script
            )
        assert 'sent mailbox whose subject is "hi"' in captured[4]

    def test_new_send_no_compose_window_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """A compose window that never appears stops everything."""
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            raise MailAppleScriptError(
                "execution error: NO_COMPOSE_WINDOW: no compose window "
                "appeared within 5 s (-2700)"
            )

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="NO_COMPOSE_WINDOW") as exc:
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                send_now=True,
            )
        # The open script, then the look for a window Mail opened late.
        assert len(captured) == 2
        assert "set expectedName to \"hi\"" in captured[1]
        assert "(compose window: none found open)" in str(exc.value)

    def test_new_send_names_its_sender(
        self, connector: AppleMailConnector
    ) -> None:
        """from_account on a fresh send is honoured: the resolved sender is
        set on the composed message. Before this the mailto: path could
        not set one and refused."""
        captured = _scripted(connector, _compose_outcomes("x", window="hi", plain=True))
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                send_now=True,
                from_account="Work",
            )
        resolve.assert_called_once_with("Work")
        assert 'set sender of theMessage to "Alice Smith <me@example.com>"' in captured[0]

    def test_new_send_from_an_unknown_account_composes_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        captured = _scripted(connector, ["unused"])
        accounts = [{
            "id": "U-1", "name": "Home", "email_addresses": ["me@example.com"],
            "full_name": None,
        }]
        with patch.object(connector, "list_accounts", return_value=accounts):
            with pytest.raises(MailAccountNotFoundError):
                connector.create_draft(
                    seed="new",
                    to=["a@example.com"],
                    subject="hi",
                    body="x",
                    send_now=True,
                    from_account="Work",
                )
        assert captured == []

    def test_new_send_carries_attachments(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Attachments on a fresh send are pasted after the body, seen in
        the compose window before Send, and read on the Sent copy the
        send filed. Before this the mailto: path could not carry them and
        refused."""
        f1 = tmp_path / "report.pdf"
        f1.write_bytes(b"%PDF-fake")
        f2 = tmp_path / "data.csv"
        f2.write_text("a,b,c")
        captured = _scripted(
            connector,
            _compose_outcomes(
                "x", window="hi", plain=True, file_names=[f1.name, f2.name]
            ),
        )
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="x",
            attachment_paths=[f1, f2],
            send_now=True,
        )
        assert result == _SENT
        assert len(captured) == 8
        (compose_s, paste_s, readback_s, files_s, verify_s, send_s,
         _, copy_s) = captured
        assert "make new attachment" not in compose_s
        assert "writeObjects:fileURLs" in files_s
        assert str(f1.resolve()) in files_s
        assert str(f2.resolve()) in files_s
        assert f1.name in verify_s and f2.name in verify_s
        assert "click sendBtn" in send_s
        assert "name of every mail attachment of m" in copy_s

    def test_new_send_missing_attachment_composes_nothing(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with pytest.raises(FileNotFoundError):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                attachment_paths=[tmp_path / "ghost.pdf"],
                send_now=True,
            )
        assert captured == []

    def test_the_mailto_send_path_is_gone(self) -> None:
        """One composition for every fresh send: the mailto: path and its
        helpers were deleted, not kept beside it."""
        for name in (
            "_send_new_via_eml",
            "_send_html_new_with_attachments",
            "_build_attach_compose_script",
            "_inject_html_and_send",
            "_build_emlx_bytes",
        ):
            assert not hasattr(AppleMailConnector, name), name

    def test_one_composition_is_left(self) -> None:
        """The dictionary save and the split between a fresh send, a note
        above a seed and everything else were deleted, not kept beside
        the one composition."""
        for name in (
            "_send_fresh",
            "_build_fresh_compose_script",
            "_compose_note_above_seed",
            "_open_seeded_compose",
            "_build_attachment_block",
            "_check_sent_attachment_count",
        ):
            assert not hasattr(AppleMailConnector, name), name

    @pytest.mark.parametrize(
        ("resolved", "in_script"),
        [
            # #158: a Display Name <email> sender is embedded verbatim.
            ("Alice Smith <me@x.com>", "Alice Smith <me@x.com>"),
            # #158: an account with no display name gives the bare form.
            ("me@x.com", "me@x.com"),
            # #173: sanitize_input before escaping strips a null byte.
            ("Alice\x00Smith <me@x.com>", "AliceSmith <me@x.com>"),
        ],
    )
    def test_a_saved_draft_names_its_sender(
        self, connector: AppleMailConnector, resolved: str, in_script: str
    ) -> None:
        with patch.object(
            connector, "_resolve_account_to_sender", return_value=resolved
        ):
            scripts, _ = self._save(connector, from_account="Gmail")
        assert "\x00" not in scripts[0]
        assert f'set sender of theMessage to "{in_script}"' in scripts[0]

    def test_a_saved_draft_from_an_unknown_account_composes_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        accounts = [{
            "id": "U-1", "name": "Home", "email_addresses": ["me@example.com"],
            "full_name": None,
        }]
        captured = _scripted(connector, ["unused"])
        with patch.object(connector, "list_accounts", return_value=accounts):
            with pytest.raises(MailAccountNotFoundError):
                connector.create_draft(
                    seed="new", to=["a@example.com"], subject="hi", body="x",
                    from_account="Work",
                )
        assert captured == []

    def test_a_saved_drafts_files_are_pasted_after_the_body(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """As on a fresh send: pasted as file URLs, then seen in the
        window, then saved with it. ``make new attachment`` after the
        paste would bring the cite blockquote back."""
        f1 = tmp_path / "report.pdf"
        f1.write_bytes(b"%PDF-fake")
        f2 = tmp_path / "data.csv"
        f2.write_text("a,b,c")
        scripts, result = self._save(connector, attachment_paths=[f1, f2])
        assert result["draft_id"] == "161055"
        assert len(scripts) == 7
        _, paste_s, _, files_s, verify_s, close_s, _ = scripts
        assert 'keystroke "a" using command down' in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert str(f1.resolve()) in files_s and str(f2.resolve()) in files_s
        assert "key code 125 using command down" in files_s
        assert f1.name in verify_s and f2.name in verify_s
        assert "AXCloseButton" in close_s
        assert all("make new attachment" not in s for s in scripts)

    def test_new_attachment_missing_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                attachment_paths=[tmp_path / "does-not-exist.pdf"],
            )

    def test_recipient_none_means_no_block(
        self, connector: AppleMailConnector
    ) -> None:
        """For reply/forward, cc=None should not emit a delete-and-replace
        block (preserves Mail's auto-derived recipients)."""
        scripts, _ = self._save(connector, seed="reply", body="")
        open_s = scripts[0]
        # cc/bcc not specified → no clear-and-add block for them.
        assert "delete (every cc recipient" not in open_s
        assert "delete (every bcc recipient" not in open_s
        # to also not specified → no clear for to either.
        assert "delete (every to recipient" not in open_s

    def test_recipient_empty_list_clears(
        self, connector: AppleMailConnector
    ) -> None:
        """cc=[] explicitly clears auto-derived cc recipients."""
        scripts, _ = self._save(connector, seed="reply", body="", cc=[])
        assert "delete (every cc recipient of theMessage)" in scripts[0]

    # ------------------------------------------------------------------
    # Reply and forward seeds
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("seed", "reply_all", "verb"),
        [("reply", False, "reply"), ("reply", True, "reply to all"),
         ("forward", False, "forward")],
    )
    def test_a_seeded_draft_opens_mails_own_window(
        self, connector: AppleMailConnector, seed: str, reply_all: bool,
        verb: str,
    ) -> None:
        scripts, _ = self._save(
            connector, seed=seed, body="", reply_all=reply_all,
        )
        assert f"set theMessage to {verb} origMsg opening window true" in scripts[0]
        if not reply_all:
            assert "reply to all" not in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_body_leaves_what_mail_wrote_untouched(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """No note: nothing is pasted, and Mail's quote or forwarded
        message is saved as Mail made it. Open, close with Save, find."""
        scripts, _ = self._save(connector, seed=seed, body="")
        assert len(scripts) == 3
        assert all('keystroke "v"' not in s for s in scripts)
        assert all("set content" not in s for s in scripts)

    def test_reply_subject_override(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._save(
            connector, seed="reply", body="", subject="custom subject",
        )
        assert 'set subject of theMessage to "custom subject"' in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_bodied_seed_is_sent_through_the_verified_send(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail's dictionary ``send`` confirmed nothing. Every send is the
        window's Send button, read back: open, gate, click and verify."""
        captured = _scripted(
            connector, _compose_outcomes("", window="Re: hi", seed=seed)
        )
        result = connector.create_draft(
            seed=seed, seed_id="160989", to=["a@example.com"], body="",
            send_now=True,
        )
        assert result == _SENT
        assert len(captured) == 4  # open, verified send, the Sent look
        assert "click sendBtn" in captured[1]
        assert all("tell theMessage to send" not in s for s in captured)

    # ------------------------------------------------------------------
    # A note on a reply or forward goes above what Mail wrote
    # ------------------------------------------------------------------

    _SEEDED_META = (
        '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
        '"to": ["a@example.com"], "cc": [], "bcc": [], "before_ids": [5, 6]}'
    )
    _NOTE = "a note for you"

    def _run_seeded(
        self,
        connector: AppleMailConnector,
        *,
        seed: str,
        send_now: bool,
        outcomes: list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], dict[str, str]]:
        """Run create_draft with a note, answering each script in turn:
        open the window, paste, read back, then send and look for its
        Sent copy, or close-and-save and find the saved draft's id."""
        tail = ["SENT", *_sent_copy_outcomes()] if send_now else ["SALVAGED", "7"]
        answers = outcomes or [self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE, *tail]
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            return answers[min(len(scripts) - 1, len(answers) - 1)]

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        result = connector.create_draft(
            seed=seed, seed_id="160989", body=self._NOTE, send_now=send_now,
            **kwargs,
        )
        return scripts, result

    def test_a_forward_note_is_pasted_above_the_forwarded_message(
        self, connector: AppleMailConnector
    ) -> None:
        """Setting the content of Mail's forward replaced the forwarded
        message and dropped its attachments (read back through the
        loopback, 2026-09-26). The note is pasted above it instead, in a
        visible compose window, and the content is never set."""
        scripts, result = self._run_seeded(
            connector, seed="forward", send_now=True, to=["a@example.com"],
        )
        assert result == _SENT
        # open, paste, read-back, verified send, the Sent look (two)
        assert len(scripts) == 6
        open_s, paste_s, _, send_s, ids_s, _ = scripts
        assert 'whose id is "160989"' in open_s
        assert "forward origMsg opening window true" in open_s
        assert "beforeNames" in open_s
        assert "key code 126 using command down" in paste_s
        assert "public.utf8-plain-text" in paste_s
        assert self._NOTE in paste_s
        assert "enabled of sendBtn" in send_s
        assert 'set composeName to "Fwd: Probe"' in send_s
        assert 'sent mailbox whose subject is "Fwd: Probe"' in ids_s
        assert all("set content of theMessage" not in s for s in scripts)

    def test_a_reply_note_is_pasted_above_the_quote(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._run_seeded(
            connector, seed="reply", send_now=True, to=["a@example.com"],
            subject="custom subject",
        )
        open_s = scripts[0]
        assert "reply origMsg opening window true" in open_s
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'set subject of theMessage to "custom subject"' in open_s
        assert "key code 126 using command down" in scripts[1]
        assert all("set content of theMessage" not in s for s in scripts)

    def test_a_reply_all_note_opens_a_reply_all_window(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._run_seeded(
            connector, seed="reply", send_now=True, reply_all=True,
            to=["a@example.com"], cc=[],
        )
        assert "reply to all origMsg opening window true" in scripts[0]

    def test_a_saved_note_is_saved_from_its_window(
        self, connector: AppleMailConnector
    ) -> None:
        """Without send_now the window is closed with Save, and the new
        draft is found among the Drafts that were not there before."""
        scripts, result = self._run_seeded(
            connector, seed="reply", send_now=False,
        )
        assert result == {"draft_id": "7", "sent_message_id": ""}
        assert len(scripts) == 5  # open, paste, read-back, save, find id
        assert "id of every message of drafts mailbox" in scripts[0]
        assert "AXCloseButton" in scripts[3]
        assert "{5, 6}" in scripts[4]
        assert 'whose subject is "Fwd: Probe"' in scripts[4]
        assert all("enabled of sendBtn" not in s for s in scripts)

    @pytest.mark.parametrize("send_now", [False, True])
    @pytest.mark.parametrize("body", ["", _NOTE])
    def test_a_seeds_files_are_pasted_after_what_mail_wrote(
        self, connector: AppleMailConnector, tmp_path: Path, send_now: bool,
        body: str,
    ) -> None:
        """A caller's file on a reply or forward is pasted at the end,
        after the note (if any) and after Mail's quote or forwarded
        message. Attached through the dictionary on a window whose body
        was untouched, it unquoted the original and dropped a forward's
        own files (measured 2026-09-27)."""
        f = tmp_path / "caller.txt"
        f.write_text("mine")
        outcomes = _compose_outcomes(
            body, window="Fwd: Probe", seed="forward", plain=True,
            file_names=["caller.txt"], send=send_now,
        )
        if send_now:
            # The forward's Sent copy carries the original's files too.
            outcomes[-1] = _sent_copy_outcomes(["theirs.pdf", "caller.txt"])[1]
        captured = _scripted(connector, outcomes)
        result = connector.create_draft(
            seed="forward", seed_id="160989", to=["a@example.com"],
            body=body, attachment_paths=[f], send_now=send_now,
        )
        assert result["draft_id"] == ("" if send_now else "7")
        assert all("make new attachment" not in s for s in captured)
        files_at = 3 if body else 1
        assert "writeObjects:fileURLs" in captured[files_at]
        assert "key code 125 using command down" in captured[files_at]
        if body:
            assert "key code 126 using command down" in captured[1]
        if send_now:
            assert result == _SENT
            assert "click sendBtn" in captured[files_at + 2]
            assert "name of every mail attachment of m" in captured[-1]
        else:
            assert "AXCloseButton" in captured[files_at + 2]

    def test_an_off_list_recipient_read_back_discards_the_window(
        self, connector: AppleMailConnector
    ) -> None:
        """The window is gated on the recipients Mail will actually send
        to, read back from the model, before anything is pasted."""
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        meta = (
            '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
            '"to": ["evil@other.com"], "cc": [], "bcc": [], "before_ids": []}'
        )
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            self._run_seeded(
                connector, seed="forward", send_now=True,
                to=["a@example.com"], outcomes=[meta, "DISCARDED"],
            )

    def test_an_off_list_read_back_pastes_and_sends_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        meta = (
            '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
            '"to": ["evil@other.com"], "cc": [], "bcc": [], "before_ids": []}'
        )
        scripts: list[str] = []
        answers = [meta, "DISCARDED"]

        def fake_run(script: str) -> str:
            scripts.append(script)
            return answers[min(len(scripts) - 1, 1)]

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailOutboundDisallowedError):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE, send_now=True,
            )
        assert len(scripts) == 2
        assert "AXCloseButton" in scripts[1]
        assert all('keystroke "v"' not in s for s in scripts)

    def test_a_window_that_will_not_close_fails_the_save_loudly(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailAppleScriptError, match="window still open"):
            self._run_seeded(
                connector, seed="reply", send_now=False,
                outcomes=[
                    self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE,
                    "SALVAGE_FAILED:window still open",
                ],
            )

    def test_a_saved_note_that_never_appears_in_drafts_is_not_settled(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotSettledError

        with pytest.raises(MailDraftNotSettledError):
            self._run_seeded(
                connector, seed="reply", send_now=False,
                outcomes=[
                    self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE,
                    "SALVAGED", "",
                ],
            )

    def test_a_seed_that_is_gone_is_message_not_found_on_the_window_path(
        self, connector: AppleMailConnector
    ) -> None:
        def fake_run(script: str) -> str:
            raise MailAppleScriptError("execution error: SEED_NOT_FOUND (-2700)")

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE,
            )

    def test_the_new_window_is_found_even_when_its_name_is_already_open(
        self, connector: AppleMailConnector
    ) -> None:
        """A window of the same name can already be open: a draft saved
        through the dictionary, as this connector's once were, left one
        behind (docs/research/icloud-draft-resync.md, Obs. 5), and a
        name-set diff then saw no new window. Names are counted instead,
        and a new window whose name is not unique is refused before
        anything is pasted or sent: every later step addresses it by
        name."""
        scripts, _ = self._run_seeded(
            connector, seed="forward", send_now=True, to=["a@example.com"],
        )
        open_s = scripts[0]
        assert "afterCount > beforeCount" in open_s
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in open_s

    def test_a_new_window_that_shares_its_name_stops_everything(
        self, connector: AppleMailConnector
    ) -> None:
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            raise MailAppleScriptError(
                "execution error: COMPOSE_WINDOW_NOT_UNIQUE: Fwd: Probe (-2700)"
            )

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="COMPOSE_WINDOW_NOT_UNIQUE"):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE, send_now=True,
            )
        # The open script, then the look for a window it did not report;
        # nothing pasted, nothing sent.
        assert len(scripts) == 2
        assert "set beforeIds to {}" in scripts[1]

    def test_plain_paste_probes_look_for_the_text_itself(self) -> None:
        """A plain note is read back as the text it is. Angle brackets in
        it are text, not a sign the paste degraded to raw source."""
        snippet, raw = AppleMailConnector._paste_probe_strings(
            "<see> the  attached & more", plain=True
        )
        assert snippet == "<see> the attached & mor"
        assert raw == ""
        html_snippet, html_raw = AppleMailConnector._paste_probe_strings(
            "<p>hi <b>there</b></p>"
        )
        assert html_snippet == "hi there"
        assert html_raw == "<p>hi <b>there</"

    # ------------------------------------------------------------------
    # Seed lookup error mapping
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_seed_not_found_raises_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("SEED_NOT_FOUND")
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply", seed_id="999999", body="x"
            )

    # ------------------------------------------------------------------
    # RFC 5322 Message-ID seed_id support (#205)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    @patch.object(AppleMailConnector, "find_message_by_message_id")
    def test_an_rfc_message_id_seed_is_resolved(
        self,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
        seed: str,
    ) -> None:
        """Read tools (#148) emit RFC ids on the IMAP path. create_draft
        must resolve them to Mail's internal id before building the
        `whose id is` AppleScript clause. (#205)
        """
        mock_resolve.return_value = "160989"
        scripts, _ = self._save(
            connector, seed=seed, body="", seed_id="abc-123@example.com",
        )
        mock_resolve.assert_called_once_with("abc-123@example.com")
        # AppleScript looks up by Mail's internal id, not the RFC id.
        assert 'whose id is "160989"' in scripts[0]
        assert all("abc-123@example.com" not in s for s in scripts)

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    def test_internal_numeric_seed_id_skips_resolver(
        self,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Existing callers passing Mail's internal numeric id (no '@')
        must keep working without a resolver round-trip. (#205)
        """
        scripts, _ = self._save(connector, seed="reply", body="")
        mock_resolve.assert_not_called()
        assert 'whose id is "160989"' in scripts[0]

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_unresolvable_rfc_seed_raises_message_not_found(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """When the RFC id doesn't match any message, surface the same
        MailMessageNotFoundError the AppleScript SEED_NOT_FOUND path
        produces — caller can't tell the difference. (#205)
        """
        mock_resolve.return_value = None
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply",
                seed_id="missing@example.com",
                body="x",
            )
        # AppleScript should not run if we can't resolve the seed.
        mock_run.assert_not_called()


class TestExtractDraftAttachments:
    """Tests for AppleMailConnector.extract_draft_attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_invalid_id_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.extract_draft_attachments(
                "../escape", ["foo.pdf"], tmp_path
            )

    def test_missing_dest_dir_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.extract_draft_attachments(
                "160991", ["foo.pdf"], tmp_path / "nonexistent"
            )

    def test_no_attachments_returns_empty(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Empty attachment list short-circuits without calling AppleScript.
        result = connector.extract_draft_attachments("160991", [], tmp_path)
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_extract_creates_subdirs_and_returns_paths(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate AppleScript actually writing the files (the real
        # Mail.app would save here). We poke real bytes into the
        # expected paths so the file-existence filter at the end picks
        # them up.
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "report.pdf").write_bytes(b"%PDF-fake")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            (tmp_path / "1" / "data.csv").write_text("a,b,c")
            return "2"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["report.pdf", "data.csv"], tmp_path
        )
        assert paths == [
            tmp_path / "0" / "report.pdf",
            tmp_path / "1" / "data.csv",
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_partial_extraction_returns_only_existing(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate Mail.app writing only the first file (e.g., second
        # attachment was a Mail-internal sentinel that errored on save).
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "ok.pdf").write_bytes(b"x")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            return "1"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["ok.pdf", "missing.pdf"], tmp_path
        )
        assert paths == [tmp_path / "0" / "ok.pdf"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "ERR_NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.extract_draft_attachments(
                "999999", ["foo.pdf"], tmp_path
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_save_command(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "0"
        connector.extract_draft_attachments(
            "160991", ["a.pdf"], tmp_path
        )
        script = mock_run.call_args[0][0]
        assert "save a in (POSIX file tp)" in script
        assert "mail attachments of foundDraft" in script


class TestUpdateMailbox:
    """Tests for AppleMailConnector.update_mailbox (rename only — #102)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_rename_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "success"
        assert connector.update_mailbox(
            account="Gmail", name="Old", new_name="New"
        ) is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_set_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Old", new_name="New"
        )
        script = mock_run.call_args[0][0]
        assert 'set name of mb to "New"' in script
        assert 'mailbox "Old"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_handles_nested_path(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Slash-separated path passes through to Mail.app's
        `mailbox "Parent/Child"` form."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_name="Archive2024"
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "Archive/2024"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mailbox_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("MAILBOX_NOT_FOUND")
        from apple_mail_mcp.exceptions import MailMailboxNotFoundError
        with pytest.raises(MailMailboxNotFoundError):
            connector.update_mailbox(
                account="Gmail", name="Missing", new_name="New"
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_other_applescript_errors_propagate(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("something else")
        with pytest.raises(MailAppleScriptError):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name="New"
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_path_traversal_chars_stripped_before_applescript(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """``sanitize_mailbox_name`` strips traversal chars (``..``, ``/``,
        ``\\``) rather than rejecting them outright. Verify the AppleScript
        embeds the sanitized form, not the raw user input."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Old", new_name="../../bad-name"
        )
        script = mock_run.call_args[0][0]
        # The dots and slashes are stripped; what's left is "bad-name".
        assert '"../../bad-name"' not in script
        assert '"bad-name"' in script

    def test_new_name_that_sanitizes_to_empty_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """A new_name of just traversal chars sanitizes to empty -> reject."""
        with pytest.raises(ValueError, match="Invalid new_name"):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name="../"
            )

    def test_empty_new_name_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="Invalid new_name"):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name=""
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_account_clause_uses_uuid_when_uuid_passed(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mirror the convention from create_mailbox: account UUIDs
        produce `account id "..."` and names produce `account "..."`."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5",
            name="Old", new_name="New",
        )
        script = mock_run.call_args[0][0]
        # applescript_account_clause emits `account id "<UUID>"`.
        assert 'account id "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"' in script

    def test_requires_at_least_one_of_new_name_or_new_parent(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="at least one"):
            connector.update_mailbox(account="Gmail", name="Old")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_gmail_system_label_source_refused_before_applescript(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Pre-flight: source name like ``[Gmail]/Drafts`` raises
        ``MailUnsupportedGmailSystemLabelError`` before any AppleScript
        runs (#164). Renames of Gmail system labels don't stick anyway."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]/Drafts", new_name="MyDrafts",
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bare_gmail_parent_source_also_refused(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The bare ``[Gmail]`` parent (a \\Noselect folder) is also refused."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]", new_name="Whatever",
            )
        mock_run.assert_not_called()


class TestUpdateMailboxMove:
    """IMAP-dispatched move path of update_mailbox (#163)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_to_top_uses_imap_rename_with_leaf_only(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """new_parent="" means top-level — destination is just the leaf."""
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_parent=""
        )
        mock_imap.rename_mailbox.assert_called_once_with("Archive/2024", "2024")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_under_new_parent_keeps_leaf(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_parent="Old"
        )
        mock_imap.rename_mailbox.assert_called_once_with(
            "Archive/2024", "Old/2024"
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_with_rename_combines_in_one_rename(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Old/Sub",
            new_name="Renamed", new_parent="New",
        )
        mock_imap.rename_mailbox.assert_called_once_with(
            "Old/Sub", "New/Renamed"
        )

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_keychain_credentials_raises_imap_required(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import (
            MailImapRequiredError,
            MailKeychainEntryNotFoundError,
        )
        mock_pw.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailImapRequiredError):
            connector.update_mailbox(
                account="Gmail", name="X", new_parent="Y"
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_source_refused_before_imap_session(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move source ``[Gmail]/Sent Mail`` raises before the IMAP
        credential lookup runs (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]/Sent Mail", new_parent="Archive",
            )
        # No IMAP session opened, no credentials looked up.
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_destination_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Moving a regular folder INTO ``[Gmail]/Subfolder`` is also
        refused — the resulting destination would land in Gmail's
        system-label namespace (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="Archive",
                new_parent="[Gmail]/Subfolder",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_destination_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """``new_parent="[Gmail]"`` produces a destination of
        ``[Gmail]/<leaf>`` — also a system-label path; refused (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="Archive", new_parent="[Gmail]",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()


class TestDeleteMailbox:
    """delete_mailbox via IMAP (#162)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_delete_empty_mailbox_returns_zero(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.return_value = 0
        result = connector.delete_mailbox(account="Gmail", name="Empty")
        assert result == 0
        mock_imap.delete_mailbox.assert_called_once_with(
            "Empty", allow_non_empty=False
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_delete_messages_true_passes_through(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.return_value = 42
        result = connector.delete_mailbox(
            account="Gmail", name="Big", delete_messages=True
        )
        assert result == 42
        mock_imap.delete_mailbox.assert_called_once_with(
            "Big", allow_non_empty=True
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_non_empty_refusal_raises_typed_error(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import MailMailboxNotEmptyError
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.side_effect = ValueError(
            "mailbox 'X' is not empty (5 messages); pass allow_non_empty=True"
        )
        with pytest.raises(MailMailboxNotEmptyError):
            connector.delete_mailbox(account="Gmail", name="X")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_such_mailbox_maps_to_typed_error(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import IMAPClientError
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.side_effect = IMAPClientError(
            "DELETE: No such mailbox"
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector.delete_mailbox(account="Gmail", name="Missing")

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_keychain_raises_imap_required(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import (
            MailImapRequiredError,
            MailKeychainEntryNotFoundError,
        )
        mock_pw.side_effect = MailKeychainEntryNotFoundError("nope")
        with pytest.raises(MailImapRequiredError):
            connector.delete_mailbox(account="Gmail", name="X")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_refused_before_credential_lookup(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Pre-flight: deleting ``[Gmail]/Trash`` raises before the IMAP
        credential lookup runs (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]/Trash")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """The bare ``[Gmail]`` parent is also refused (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()


def _compose_meta(
    window: str,
    *,
    subject: str | None = None,
    to: list[str] | None = None,
    before_ids: list[int] | None = None,
) -> str:
    """What the composition's first script reports: the window it opened,
    the subject and recipients Mail holds, and the ids of the mailbox its
    ending is looked up in (Drafts for a save, Sent for a send) before."""
    return json.dumps({
        "window": window,
        "subject": window if subject is None else subject,
        "to": ["a@example.com"] if to is None else to,
        "cc": [],
        "bcc": [],
        "before_ids": [5, 6] if before_ids is None else before_ids,
    })


# What a send returns when it finds its Sent copy: Mail's id for the copy
# a well-behaved Mail files beside the two _compose_meta saw, and its
# RFC Message-ID, bare.
_SENT = {
    "draft_id": "",
    "sent_message_id": "9",
    "sent_rfc_message_id": "copy-9@example.com",
}


def _sent_copy_outcomes(
    file_names: list[str] | None = None, *, ids: list[int] | None = None
) -> list[str]:
    """What Mail answers to a send's look for its Sent copy: the ids of
    the Sent messages with its subject (the two it had before and the new
    one), then that one copy read: its id, Message-ID and files."""
    return [
        json.dumps([5, 6, 9] if ids is None else ids),
        json.dumps({
            "id": "9",
            "message_id": "<copy-9@example.com>",
            "attachment_names": file_names or [],
        }),
    ]


def _compose_outcomes(
    body: str,
    *,
    window: str,
    seed: str = "new",
    plain: bool = False,
    file_names: list[str] | None = None,
    send: bool = True,
    draft_id: str = "7",
) -> list[str]:
    """What a well-behaved Mail answers to the composition, script by
    script: open the window; paste the body and read it back (always on a
    fresh message, on a reply or forward only when there is a body); with
    files, the file paste and the AX verify; then the verified send and
    the look for its Sent copy, or the close with Save and the new draft's
    id."""
    outcomes = [_compose_meta(window)]
    if seed == "new" or body:
        snippet, _ = AppleMailConnector._paste_probe_strings(body, plain=plain)
        outcomes += ["PASTED_UNVERIFIED", f"pad {snippet} pad"]
    if file_names:
        outcomes += ["PASTED_UNVERIFIED", "ATTACHMENTS_VERIFIED"]
    if send:
        outcomes += ["SENT", *_sent_copy_outcomes(file_names)]
    else:
        outcomes += ["SALVAGED", draft_id]
    return outcomes


def _snapshot_before_compose(connector: AppleMailConnector) -> None:
    """Answer the read of Mail's windows a composition takes before it
    opens its own (``_mail_window_snapshot``) with no windows, so a test's
    scripted outcomes start at the opening script."""
    connector._mail_window_snapshot = lambda: _WindowSnapshot(  # type: ignore[method-assign]
        mail_pid=77701, window_ids=frozenset()
    )


def _scripted(connector: AppleMailConnector, outcomes: list[str]) -> list[str]:
    """Answer the connector's osascript calls with ``outcomes`` in order
    (the last repeats) and return the list each script is captured in.
    The window snapshot a composition reads first is answered apart
    (``_snapshot_before_compose``)."""
    captured: list[str] = []

    def fake_run(script: str) -> str:
        captured.append(script)
        return outcomes[min(len(captured) - 1, len(outcomes) - 1)]

    connector._run_applescript = fake_run  # type: ignore[method-assign]
    _snapshot_before_compose(connector)
    return captured


def _run_html_flow(
    connector: AppleMailConnector,
    *,
    body: str = "<p>Hi there probe</p>",
    subject: str = "Hello",
) -> list[str]:
    """Drive the fresh HTML flow with a well-behaved mock and return the
    captured scripts: [compose, paste, read-back, verified-send, the Sent
    ids with its subject, its Sent copy read]."""
    captured = _scripted(connector, _compose_outcomes(body, window=subject))
    result = connector._send_html_email(
        to=["test@example.com"],
        cc=None,
        bcc=None,
        subject=subject,
        body=body,
        from_account=None,
    )
    assert result == _SENT
    return captured


class TestSendHtmlEmail:
    """Tests for AppleMailConnector._send_html_email (fresh mode).

    A fresh HTML send is the one composition (``_compose``): SIX
    osascript invocations — compose a visible window → verified paste →
    read-back (fresh process — same-process AX reads are stale after a
    WebKit re-render) → verified send → the Sent ids with its subject →
    its Sent copy read.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_html_send_uses_clipboard_path(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = _run_html_flow(connector)
        assert len(scripts) == 6
        compose_s, paste_s, readback_s, send_s, _, _ = scripts
        assert "make new outgoing message" in compose_s
        assert all("open location" not in s for s in scripts)
        # Clipboard-inject landmarks live in the paste script, which
        # replaces the seeded body rather than pasting above it.
        assert "public.html" in paste_s
        assert "Make Rich Text" in paste_s
        assert "AXWebArea" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "click sendBtn" in send_s
        # Must NOT use the draft-save path anywhere, nor set content.
        assert all("saving yes" not in s for s in scripts)
        assert all("set content" not in s for s in scripts)

    def test_html_send_no_body_area_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """NO_BODY_AREA from the paste step → MailAppleScriptError."""
        _scripted(connector, [_compose_meta("Hi"), "NO_BODY_AREA:x"])
        with pytest.raises(MailAppleScriptError, match="NO_BODY_AREA"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="<p>x</p>",
                from_account=None,
            )

    def test_a_reply_and_a_forward_at_once_are_refused(
        self, connector: AppleMailConnector
    ) -> None:
        """One message is one seed: refused before any AppleScript."""
        captured = _scripted(connector, ["unused"])
        with pytest.raises(ValueError, match="mutually exclusive"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="",
                body="<p>x</p>",
                from_account=None,
                reply_to="12345",
                forward_of="67890",
            )
        assert captured == []

    def test_html_fresh_names_its_sender(
        self, connector: AppleMailConnector
    ) -> None:
        """A fresh HTML send sets the named account as the sender of the
        window it composes; before this it was refused, since the mailto:
        window it used could not take one."""
        captured = _scripted(connector, _compose_outcomes("<p>x</p>", window="Hi"))
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="<p>x</p>",
                from_account="Work",
            )
        resolve.assert_called_once_with("Work")
        assert 'set sender of theMessage to "Alice Smith <me@example.com>"' in captured[0]

    def test_html_send_fresh_carries_cc_bcc(
        self, connector: AppleMailConnector
    ) -> None:
        """Regression: the fresh path once accepted cc/bcc, allowlist-
        validated them, then silently dropped them — the mail went out
        WITHOUT them. They are recipients of the composed message."""
        captured = _scripted(connector, _compose_outcomes("<p>x</p>", window="Hi"))
        connector._send_html_email(
            to=["test@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Hi",
            body="<p>x</p>",
            from_account=None,
        )
        compose_s = captured[0]
        for kind, addr in (("cc", "cc1@example.com"), ("bcc", "bcc1@example.com")):
            assert f'repeat with addr in {{"{addr}"}}' in compose_s
            assert (
                f"make new {kind} recipient at end of {kind} recipients of "
                "theMessage with properties {address:addr}"
            ) in compose_s

    def test_html_send_escapes_subject(
        self, connector: AppleMailConnector
    ) -> None:
        """A subject with AppleScript-special characters is escaped in the
        compose script."""
        scripts = _run_html_flow(connector, subject='Hello "World" & <Test>')
        assert 'subject:"Hello \\"World\\" & <Test>"' in scripts[0]

    def test_html_send_escapes_body(
        self, connector: AppleMailConnector
    ) -> None:
        """Body with AppleScript-special characters (quotes, backslashes) is
        escaped before interpolation so the paste script remains valid."""
        scripts = _run_html_flow(
            connector, body='<p>He said "hello" and back\\slash probe</p>'
        )
        paste_s = scripts[1]
        assert '\\"' in paste_s or "\\\\back" in paste_s

    @pytest.mark.parametrize("group", ["to", "cc", "bcc"])
    def test_html_fresh_refuses_an_off_list_recipient_itself(
        self,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        group: str,
    ) -> None:
        """The allowlist is met at the connector as well as at the tool
        (docs/guides/SECURITY_CHECKLIST.md): a fresh send refuses an
        off-list recipient in any group itself, before any AppleScript
        runs, whatever its caller checked."""
        policy = tmp_path / "only-example-org.yaml"
        policy.write_text("email:\n  allowed_outbound:\n    - '*@example.org'\n")
        monkeypatch.setenv("APPLE_MAIL_MCP_COMMS_CONFIG", str(policy))
        recipients: dict[str, list[str] | None] = {
            "to": ["a@example.org"], "cc": None, "bcc": None,
        }
        recipients[group] = ["off@example.com"]
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="off@example.com"):
            connector._send_html_email(
                to=recipients["to"] or [],
                cc=recipients["cc"],
                bcc=recipients["bcc"],
                subject="Hi",
                body="<p>x</p>",
                from_account=None,
            )
        assert captured == []


class TestSendHtmlWithAttachments:
    """Fresh HTML send WITH attachments: the fresh composition with the
    files pasted after the body (as file URLs — attached through the
    dictionary after the paste, they brought Mail's cite blockquote
    back, measured 2026-09-27).

    Flow is EIGHT osascript invocations: compose → paste → read-back →
    file paste → AX attachment verify (the send is NOT attempted unless
    every file is visible in the compose window) → verified send → the
    Sent ids with its subject → its Sent copy read, files and all.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _drive(
        self,
        connector: AppleMailConnector,
        tmp_path: Path,
        *,
        outcomes_override: dict[int, str] | None = None,
        n_files: int = 1,
    ) -> tuple[list[str], list[Path], dict[str, Any] | None, Exception | None]:
        files = []
        for i in range(n_files):
            f = tmp_path / f"att{i}.txt"
            f.write_text(f"content {i}")
            files.append(f)
        body = "<p>with attachment probe</p>"
        outcomes = _compose_outcomes(
            body, window="Attached", file_names=[f.name for f in files]
        )
        for idx, val in (outcomes_override or {}).items():
            outcomes[idx] = val
        captured = _scripted(connector, outcomes)
        result: dict[str, Any] | None = None
        err: Exception | None = None
        try:
            result = connector._send_html_email(
                to=["test@example.com"],
                cc=["cc1@example.com"],
                bcc=None,
                subject="Attached",
                body=body,
                from_account=None,
                attachment_paths=files,
            )
        except Exception as e:  # noqa: BLE001
            err = e
        return captured, files, result, err

    def test_happy_path_eight_scripts(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        scripts, files, result, err = self._drive(connector, tmp_path)
        assert err is None
        assert result == _SENT
        assert len(scripts) == 8
        (compose_s, paste_s, readback_s, files_s, verify_s, send_s,
         ids_s, copy_s) = scripts
        # Compose: scriptable outgoing message, recipients through the
        # model, no attachment through the dictionary.
        assert "make new outgoing message" in compose_s
        assert "open location" not in compose_s
        assert "make new attachment" not in compose_s
        assert "test@example.com" in compose_s
        assert "cc1@example.com" in compose_s
        # The body replaces the seed, then the files go in after it.
        assert "public.html" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert files[0].resolve().as_posix() in files_s
        assert "key code 125 using command down" in files_s
        # AX verify names the file.
        assert files[0].name in verify_s
        assert "click sendBtn" in send_s
        # Post-send: the new Sent id with the subject, then that copy's
        # files, read by its id.
        assert 'id of every message of sent mailbox whose subject is "Attached"' in ids_s
        assert 'first message of sent mailbox whose id is "9"' in copy_s
        assert "name of every mail attachment of m" in copy_s

    def test_missing_file_raises_before_applescript(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        called: list[bool] = []
        connector._run_applescript = lambda _: (called.append(True), "X")[1]  # type: ignore[method-assign]
        with pytest.raises(FileNotFoundError):
            connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None,
                subject="s", body="<p>b</p>", from_account=None,
                attachment_paths=[tmp_path / "ghost.pdf"],
            )
        assert not called

    def test_ax_verify_failure_raises_and_no_send(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """If the attachment never appears in the compose window's AX
        tree, the send must NOT be attempted (would dispatch without the
        attachment — a silent failure)."""
        scripts, _, result, err = self._drive(
            connector, tmp_path,
            outcomes_override={4: "ATTACH_MISSING:att0.txt"},
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "att0.txt" in str(err)
        assert "NOT attempted" in str(err)
        # compose, paste, read-back, file paste, verify, salvage — no send.
        assert len(scripts) == 6
        assert "AXCloseButton" in scripts[5]
        assert all("click sendBtn" not in s for s in scripts)

    def test_a_file_missing_from_the_sent_copy_raises(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Dispatch succeeded but the Sent copy lacks a file that was
        attached → loud error naming it (the mail DID go out; the error
        must say so)."""
        scripts, _, result, err = self._drive(
            connector, tmp_path, n_files=2,
            outcomes_override={7: _sent_copy_outcomes(["att1.txt"])[1]},
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "WAS sent" in str(err)
        assert "att0.txt" in str(err)
        assert "id 9" in str(err)
        assert len(scripts) == 8

    def test_the_sent_copy_may_carry_more_than_was_pasted(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """A forward's Sent copy carries the original's files as well as
        the caller's: every file pasted must be there, not only those."""
        _, _, result, err = self._drive(
            connector, tmp_path,
            outcomes_override={7: _sent_copy_outcomes(["theirs.pdf", "att0.txt"])[1]},
        )
        assert err is None
        assert result == _SENT

    def test_ax_verify_accepts_a_file_shown_as_an_image(self) -> None:
        """A compose window shows a PNG inline, as an AXImage described
        by its file name, and a PDF or text file as an AXButton described
        "name.ext, N KB" (measured 2026-09-27,
        docs/research/icloud-draft-resync.md, Observation 10). The
        check matching only AXButton refused every image. What an image
        looks like live is covered by test_loopback.py; here, that the
        script accepts either role for a description naming the file."""
        script = AppleMailConnector._build_attachment_ax_verify_script(
            window_name="w", filenames=["probe.png"],
        )
        assert (
            '(r is "AXButton" or r is "AXImage") and '
            "((description of el) as text) contains fname"
        ) in script
        assert '"probe.png"' in script


def _sent_mailbox(
    ids: list[list[int]], names: list[str] | None = None
) -> Callable[[str], str]:
    """Mail after a send, as the look for its Sent copy sees it: the ids
    of the Sent messages with its subject, one list a look (the last
    repeats), and copy 9, read by its id, carrying ``names``."""
    looks: list[str] = []

    def answer(script: str) -> str:
        if "whose id is" in script:
            return _sent_copy_outcomes(names)[1]
        looks.append(script)
        return json.dumps(ids[min(len(looks), len(ids)) - 1])

    return answer


class TestASendFindsItsSentCopyByIdentity:
    """After the verified send, a send finds the copy it filed in Sent:
    the one message in Sent with the window's subject whose id was not
    there before the window opened (``_compose_meta`` saw 5 and 6). By
    subject alone, a send whose subject had been used before read the
    oldest copy of it, reported the file just sent missing from a
    message that carried it, and was sent again.

    A send that went out ends one of three ways: its copy found and
    carrying every file pasted (its ids returned), its copy found and
    lacking one (raised, saying the message WAS sent), or its copy not
    identified (no id, and one warning saying why)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @pytest.fixture
    def sleeps(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        from apple_mail_mcp import mail_connector as mc_mod

        slept: list[float] = []
        monkeypatch.setattr(mc_mod.time, "sleep", slept.append)
        return slept

    def _send(
        self,
        connector: AppleMailConnector,
        tmp_path: Path,
        sent_mailbox: Callable[[str], str],
        *,
        files: tuple[str, ...] = (),
        subject: str = "Same subject",
    ) -> tuple[list[str], dict[str, Any] | None, Exception | None]:
        """Send with a well-behaved Mail up to SENT, then ``sent_mailbox``
        answering the look for the copy; the scripts after SENT, and the
        result or what was raised."""
        paths = []
        for name in files:
            path = tmp_path / name
            path.write_text(name)
            paths.append(path)
        body = "<p>again probe</p>"
        until_sent = _compose_outcomes(body, window=subject, file_names=list(files))[:-2]
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            if len(captured) <= len(until_sent):
                return until_sent[len(captured) - 1]
            return sent_mailbox(script)

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        try:
            result = connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject=subject,
                body=body, from_account=None, attachment_paths=paths or None,
            )
        except Exception as e:  # noqa: BLE001
            return captured[len(until_sent):], None, e
        return captured[len(until_sent):], result, None

    def test_the_copy_is_the_one_id_the_snapshot_did_not_have(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 9, 6]])
        )
        assert err is None
        assert result == _SENT
        assert len(after) == 2
        assert (
            'id of every message of sent mailbox whose subject is "Same subject"'
        ) in after[0]
        assert 'first message of sent mailbox whose id is "9"' in after[1]
        assert sleeps == []

    def test_the_files_are_checked_on_that_copy_not_an_older_one(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Copy 5 is an earlier send of the same subject with other files:
        it is never read, so what it carries decides nothing."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 9]], ["archive.zip"]),
            files=("archive.zip",),
        )
        assert err is None
        assert result == _SENT
        assert all('whose id is "5"' not in s for s in after)
        assert all("first message of sent mailbox whose subject" not in s for s in after)

    def test_a_copy_listed_late_is_waited_for(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """A subject Sent already held satisfies the verified send before
        the new copy is listed; the look waits for it."""
        _, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6], [5, 6], [5, 6], [5, 6, 9]])
        )
        assert err is None
        assert result == _SENT
        assert sleeps == [AppleMailConnector._SENT_APPEAR_INTERVAL_S] * 3

    def test_a_copy_never_listed_is_a_warning_not_an_error(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Mail accepted the message (its window closed after Send, with no
        sheet): nothing is raised, no id is guessed, the warning says what
        was seen and what was not, and the files are said to be
        unverified, not missing."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6]], ["archive.zip"]),
            files=("archive.zip",),
        )
        assert err is None
        assert result is not None
        polls = AppleMailConnector._SENT_APPEAR_POLLS
        interval = AppleMailConnector._SENT_APPEAR_INTERVAL_S
        assert sleeps == [interval] * polls
        assert len(after) == polls + 1
        assert all("whose id is" not in s for s in after)
        assert result["sent_message_id"] == ""
        assert result["sent_rfc_message_id"] == ""
        assert result["draft_id"] == ""
        [warning] = result["warnings"]
        assert "Mail accepted the message" in warning
        assert "window closed after Send, with no sheet" in warning
        assert f"within {polls * interval:g}s" in warning
        assert "unverified" in warning
        assert "lacks" not in warning
        assert "Outbox" in warning

    def test_without_files_the_warning_says_nothing_of_files(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        _, result, _ = self._send(connector, tmp_path, _sent_mailbox([[5, 6]]))
        assert result is not None
        [warning] = result["warnings"]
        assert "file" not in warning

    def test_two_new_copies_are_not_guessed_between(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Another send of the same subject since the window opened: which
        copy is this one's cannot be told by id, and waiting will not
        tell it either."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9, 10]])
        )
        assert err is None
        assert result is not None
        assert result["sent_message_id"] == ""
        [warning] = result["warnings"]
        assert "2 messages" in warning
        assert len(after) == 1
        assert sleeps == []

    def test_a_failed_look_is_a_warning_and_the_window_stays_sent(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        ends: list[Any] = []
        connector._record_window_end = (  # type: ignore[method-assign]
            lambda window, state: ends.append(state)
        )

        def busy(script: str) -> str:
            raise MailAppleScriptError("Mail automation busy")

        _, result, err = self._send(connector, tmp_path, busy)
        assert err is None
        assert result is not None
        assert result["sent_message_id"] == ""
        [warning] = result["warnings"]
        assert "Mail automation busy" in warning
        assert [state.how for state in ends] == ["sent"]

    def test_a_file_missing_from_the_found_copy_raises_that_it_was_sent(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        _, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9]], ["other.pdf"]),
            files=("archive.zip",),
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "WAS sent" in str(err)
        assert "archive.zip" in str(err)
        assert "id 9" in str(err)

    def test_the_subject_is_escaped_in_the_look(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        after, _, _ = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9]]), subject='Say "hi"'
        )
        assert 'whose subject is "Say \\"hi\\""' in after[0]


_OVERLONG = SANITIZE_MAX_LENGTH + 1


def _overlong_fresh_html(c: AppleMailConnector) -> None:
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="s",
        body="x" * _OVERLONG, from_account=None,
    )


def _overlong_html_reply(c: AppleMailConnector) -> None:
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="",
        body="x" * _OVERLONG, from_account=None, reply_to="12345",
    )


def _overlong_html_forward(c: AppleMailConnector) -> None:
    """By RFC Message-ID, which is looked up in Mail unless refused first."""
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="",
        body="x" * _OVERLONG, from_account=None, forward_of="abc@example.com",
    )


def _overlong_fresh_plain_send(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="new", to=["a@example.com"], subject="s",
        body="x" * _OVERLONG, send_now=True,
    )


def _overlong_fresh_saved_draft(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="new", to=["a@example.com"], subject="s", body="x" * _OVERLONG,
    )


def _overlong_reply_note(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="reply", seed_id="12345", to=["a@example.com"],
        body="x" * _OVERLONG,
    )


def _overlong_forward_note_sent(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="forward", seed_id="12345", to=["a@example.com"],
        body="x" * _OVERLONG, send_now=True,
    )


class TestAnOverlongBodyIsRefusedNotCut:
    """``sanitize_input`` cuts at SANITIZE_MAX_LENGTH without a word, and
    every body a caller hands the connector passes through it on its way
    to a paste (a fresh draft or send, an HTML reply or forward, a note
    above a reply or forward). A longer body is refused, on every path,
    with one message, before anything is composed or looked up."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @pytest.mark.parametrize(
        "call",
        [
            _overlong_fresh_html,
            _overlong_html_reply,
            _overlong_html_forward,
            _overlong_fresh_plain_send,
            _overlong_fresh_saved_draft,
            _overlong_reply_note,
            _overlong_forward_note_sent,
        ],
        ids=lambda f: f.__name__.removeprefix("_overlong_"),
    )
    def test_refused_before_any_applescript(
        self, connector: AppleMailConnector, call: Any
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with pytest.raises(ValueError) as refused:
            call(connector)
        assert str(refused.value) == (
            f"body is {_OVERLONG} characters; a message body carries at "
            f"most {SANITIZE_MAX_LENGTH}. Nothing was composed, saved or "
            "sent."
        )
        assert captured == []

    def test_a_body_at_the_limit_is_carried_whole(
        self, connector: AppleMailConnector
    ) -> None:
        body = "y" * SANITIZE_MAX_LENGTH
        captured = _scripted(
            connector,
            _compose_outcomes(
                body, window="s", plain=True, send=False, draft_id="4242",
            ),
        )
        result = connector.create_draft(
            seed="new", to=["a@example.com"], subject="s", body=body,
        )
        assert result["draft_id"] == "4242"
        assert f'set theBody to "{body}"' in captured[1]


class TestVerifiedSendPrimitives:
    """Phase 0 of PLAN-html-reply-send: every UI action in the send paths
    gets a mechanical read-back (docs/reference/UI_GROUNDING_MAIL_SEND.md).

    Contract for the verified send every compose-window path ends in
    (fresh plain, fresh HTML, reply):
      - PRE: Send button resolved on a window found BY NAME (never a bare
        `window 1`), existence checked, and `enabled` checked — clicking a
        disabled button is a silent no-op (the 2026-07-20 vanished send).
      - ACT: click by AX reference.
      - POST: poll up to 15 s until the compose window is gone (SENT); a
        sheet mid-flight surfaces its static texts (SHEET:), and a window
        still open with no sheet is WINDOW_STILL_OPEN. The copy in Sent is
        not the block's: the send looks for it by identity afterwards.
      - All non-SENT sentinels raise MailAppleScriptError with the detail.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # Where the verified send sits among a fresh send's scripts: after
    # compose, paste and read-back, before the look for its Sent copy.
    _SEND_AT = 3

    def _plain_fresh_scripts(self, connector: AppleMailConnector) -> list[str]:
        """Captured scripts for a fresh plain send through create_draft:
        [compose, paste, read-back, verified-send, Sent ids, Sent copy]."""
        captured = _scripted(
            connector, _compose_outcomes("x", window="Probe", plain=True)
        )
        connector.create_draft(
            seed="new",
            to=["test@example.com"],
            subject="Probe",
            body="x",
            send_now=True,
        )
        return captured

    def _html_scripts(self, connector: AppleMailConnector) -> list[str]:
        """Captured scripts for the fresh HTML flow:
        [compose, paste, read-back, verified-send, Sent ids, Sent copy]."""
        return _run_html_flow(connector)

    # -- precondition: send-enabled check, window by name ------------------

    def test_plain_fresh_send_checks_send_enabled(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._plain_fresh_scripts(connector)[self._SEND_AT]
        assert "enabled of sendBtn" in send_s
        assert "SEND_DISABLED" in send_s

    def test_html_script_checks_send_enabled(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._html_scripts(connector)[self._SEND_AT]
        assert "enabled of sendBtn" in send_s
        assert "SEND_DISABLED" in send_s

    def test_html_script_resolves_window_by_name(
        self, connector: AppleMailConnector
    ) -> None:
        """The HTML path may not act on a bare `window 1` — the compose
        window is resolved by its name in every script."""
        for script in self._html_scripts(connector):
            assert "set w to window 1" not in script

    # -- postcondition: the window gone, sheet surfacing --------------------

    @staticmethod
    def _assert_the_postcondition_is_the_window(send_s: str) -> None:
        """Gone within 15 s is SENT; a sheet is SHEET:; still open with no
        sheet is WINDOW_STILL_OPEN. Never a Sent lookup: by subject it
        was satisfied by any earlier message of the subject, and a copy
        slower than 15 s would make a message that went read as not sent."""
        assert "repeat 15 times" in send_s
        assert 'if not winOpen then\n                    set sendOutcome to "SENT"' in send_s
        assert '"SHEET:"' in send_s
        assert '"WINDOW_STILL_OPEN:' in send_s
        assert "sent mailbox" not in send_s
        assert "composeSubject" not in send_s
        assert "POSTCONDITION_TIMEOUT" not in send_s

    def test_plain_fresh_send_verifies_dispatch(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._plain_fresh_scripts(connector)[self._SEND_AT]
        self._assert_the_postcondition_is_the_window(send_s)

    def test_html_script_verifies_dispatch(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._html_scripts(connector)[self._SEND_AT]
        self._assert_the_postcondition_is_the_window(send_s)

    def test_a_window_still_open_is_not_sent_and_is_saved_to_drafts(
        self, connector: AppleMailConnector
    ) -> None:
        """WINDOW_STILL_OPEN raises, says what was seen, salvages the
        window to Drafts, and no Sent copy is looked for."""
        outcomes = _compose_outcomes("x", window="Probe", plain=True)
        still_open = "WINDOW_STILL_OPEN:no sheet, still open 15s after Send on Probe"
        captured = _scripted(
            connector, outcomes[:self._SEND_AT] + [still_open, "SALVAGED"]
        )
        with pytest.raises(MailAppleScriptError, match="WINDOW_STILL_OPEN") as exc:
            connector.create_draft(
                seed="new", to=["test@example.com"], subject="Probe", body="x",
                send_now=True,
            )
        assert "compose window: SALVAGED" in str(exc.value)
        assert len(captured) == self._SEND_AT + 2
        assert 'click button "Save" of sheet 1 of window closeIdx' in captured[-1]
        assert all("sent mailbox whose subject" not in s for s in captured)

    def test_a_window_gone_goes_on_to_look_for_the_copy(
        self, connector: AppleMailConnector
    ) -> None:
        """SENT from the block is the window gone; whether the copy
        reached Sent is the look's to find, after it."""
        scripts = self._plain_fresh_scripts(connector)
        assert "click sendBtn" in scripts[self._SEND_AT]
        assert "sent mailbox whose subject is" in scripts[self._SEND_AT + 1]

    # -- paste read-back (2026-07-22 raw-<p> regression) -------------------

    def test_html_paste_script_verifies_focus(
        self, connector: AppleMailConnector
    ) -> None:
        """Focus is SET and VERIFIED (a click can leave focus in the To
        field, sending cmd+v to the wrong control)."""
        paste_s = self._html_scripts(connector)[1]
        assert "set focused of bodyArea to true" in paste_s
        assert "AXFocusedUIElement" in paste_s
        assert "PASTE_FOCUS_FAILED" in paste_s

    def test_html_readback_runs_in_fresh_process_and_gates_send(
        self, connector: AppleMailConnector
    ) -> None:
        """A bad read-back (raw source visible = paste degraded to
        literal text) must retry once then raise PASTE_FAILED WITHOUT
        ever running the send script — shipping without this sent
        literal <p> tags on 2026-07-21."""
        body = "<p>Hi there probe</p>"
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            n = len(captured)
            if n == 1:
                return _compose_meta("Probe", to=["test@example.com"])
            if n in (2, 4):  # paste attempts
                return "PASTED_UNVERIFIED"
            return "<p>Hi there probe</p>"  # read-back sees RAW source

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="PASTE_FAILED"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Probe",
                body=body,
                from_account=None,
            )
        # compose, paste, readback, retry-paste (with undo), retry-readback,
        # salvage-to-draft — and NO send script.
        assert len(captured) == 6
        assert 'keystroke "z" using command down' in captured[3]
        # Headless policy: the failed compose window is salvaged to
        # Drafts (close → Save), never left open.
        assert 'button "Save"' in captured[5]
        assert all("click sendBtn" not in s for s in captured)

    def test_paste_probe_strings(self) -> None:
        snippet, raw = AppleMailConnector._paste_probe_strings(
            "<p>Hello <b>world</b> of probes</p>"
        )
        assert snippet.startswith("Hello world")
        assert "<" not in snippet
        assert raw.startswith("<p>Hello ") and len(raw) == 16
        # Text-led body: no raw marker (nothing tag-like to detect).
        _, raw2 = AppleMailConnector._paste_probe_strings("plain text body")
        assert raw2 == ""

    # -- clipboard restore must survive AppleScript errors -----------------

    def test_html_paste_script_restores_clipboard_on_error(
        self, connector: AppleMailConnector
    ) -> None:
        """The paste script is wrapped so a mid-script error still restores
        the user's clipboard before the error propagates; the success path
        restores IMMEDIATELY after the paste (shortest possible hold)."""
        paste_s = self._html_scripts(connector)[1]
        assert "on error" in paste_s
        # restore loop appears at least twice: success path + error path
        assert paste_s.count("repeat with pair in savedPairs") >= 2

    # -- sentinel → exception mapping --------------------------------------

    @pytest.mark.parametrize(
        "sentinel",
        ["SEND_DISABLED", "SHEET:Save this message as a draft?",
         "WINDOW_STILL_OPEN:window still open"],
    )
    def test_plain_fresh_sentinels_raise_with_detail(
        self, connector: AppleMailConnector, sentinel: str
    ) -> None:
        outcomes = _compose_outcomes("x", window="Probe", plain=True)
        _scripted(connector, outcomes[:self._SEND_AT] + [sentinel])
        with pytest.raises(MailAppleScriptError) as exc:
            connector.create_draft(
                seed="new",
                to=["test@example.com"],
                subject="Probe",
                body="x",
                send_now=True,
            )
        assert sentinel.split(":")[0] in str(exc.value)

    @pytest.mark.parametrize(
        "sentinel",
        ["SEND_DISABLED", "SHEET:Save this message as a draft?",
         "WINDOW_STILL_OPEN:window still open"],
    )
    def test_html_sentinels_raise_with_detail(
        self, connector: AppleMailConnector, sentinel: str
    ) -> None:
        """Verified-send sentinels from the send script surface as errors."""
        body = "<p>Hi there probe</p>"
        outcomes = _compose_outcomes(body, window="Probe")
        _scripted(connector, outcomes[:self._SEND_AT] + [sentinel])
        with pytest.raises(MailAppleScriptError) as exc:
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Probe",
                body=body,
                from_account=None,
            )
        assert sentinel.split(":")[0] in str(exc.value)

    # -- closing one compose window ----------------------------------------
    #
    # System Events turns `set w to window i` into a reference by name,
    # which reads and clicks the first window of that name
    # (docs/research/compose-window-tending.md, Observation 7). A close
    # finds its window by Mail's id, ties it to System Events' list by
    # name and position, and addresses it by index alone.

    def _close_script(
        self, connector: AppleMailConnector, mode: str, window_id: int | None = 2781
    ) -> str:
        captured = _scripted(connector, ["X"])
        if mode == "save":
            connector._salvage_compose_to_draft('Probe "1"', window_id)
        else:
            connector._discard_compose_window('Probe "1"', window_id)
        assert len(captured) == 1
        return captured[0]

    def test_discard_uses_the_curly_apostrophe_and_verifies(
        self, connector: AppleMailConnector
    ) -> None:
        """`Don’t Save` carries U+2019 (a straight quote never matches) and
        the block must verify the window actually closed — both Mail-
        dictionary discard routes fail silently (grounding report)."""
        script = self._close_script(connector, "discard")
        assert 'click button "Don’t Save" of sheet 1 of window closeIdx' in script
        assert 'set closeOutcome to "DISCARDED" & sheetNote' in script
        assert 'set closeOutcome to "DISCARD_FAILED:window still open"' in script

    @pytest.mark.parametrize("mode", ["save", "discard"])
    def test_a_close_addresses_mails_window_by_id(
        self, connector: AppleMailConnector, mode: str
    ) -> None:
        script = self._close_script(connector, mode)
        assert script.startswith('set closeId to 2781\nset closeName to "Probe \\"1\\""')
        assert "set idBounds to bounds of window id closeId" in script
        # Tied to System Events' list by name and position, before any click.
        found_at = script.index("set closeIdx to my tendIndexOf(closeName, closePos)")
        assert found_at < script.index("my tendClickClose(closeIdx)")
        # Closed means Mail's window of that id is gone: not listed, or
        # listed neither visible nor minimized and gone from System Events
        # at its place (Mail keeps some closed windows listed).
        assert "if not (exists window id wid) then return true" in script
        assert "if (visible of window id wid) or (miniaturized of window id wid) then return false" in script
        assert "return (my tendIndexOf(nm, pos)) is 0" in script
        assert script.count("my tendGone(closeId, closeName, closePos)") == 2

    @pytest.mark.parametrize("mode", ["save", "discard"])
    def test_no_window_is_held_in_a_variable_or_clicked_by_name(
        self, connector: AppleMailConnector, mode: str
    ) -> None:
        """Held in a variable, a System Events window is a by-name
        reference; so is any element found in it."""
        script = self._close_script(connector, mode)
        assert "to window " not in script.replace("to window id", "")
        assert "repeat with w in windows" not in script
        assert "click button k of window i" in script
        assert "of window closeName" not in script
        assert "first button of window" not in script

    def test_a_window_mail_cannot_tell_from_another_in_its_place_is_left(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        twins_at = script.index("if twins is not 1 then")
        assert twins_at < script.index("my tendClickClose(closeIdx)")
        assert "so which to close cannot be told; none was closed" in script

    def test_a_window_renamed_since_is_left(self, connector: AppleMailConnector) -> None:
        script = self._close_script(connector, "save")
        renamed_at = script.index("if closeOutcome is \"\" and idName is not closeName")
        assert renamed_at < script.index("my tendClickClose(closeIdx)")

    def test_without_an_id_only_the_one_window_of_its_name_is_closed(
        self, connector: AppleMailConnector
    ) -> None:
        """A window the opening script got no id for is closed by name,
        and only while its name is the only one: the first window of a
        name could be someone else's."""
        script = self._close_script(connector, "save", window_id=None)
        assert script.startswith("set closeId to 0\n")
        count_at = script.index("set nameCount to count of (windows whose name is closeName)")
        assert count_at < script.index("my tendClickClose(closeIdx)")
        assert "else if nameCount > 1 then" in script
        assert "so which to close cannot be told by name; none was closed" in script

    def test_a_close_that_cannot_run_is_a_failure_of_its_kind(
        self, connector: AppleMailConnector
    ) -> None:
        def fail(script: str) -> str:
            raise MailAppleScriptError("Mail got an error")

        connector._run_applescript = fail  # type: ignore[method-assign]
        assert connector._salvage_compose_to_draft("P", 1).startswith("SALVAGE_FAILED:")
        assert connector._discard_compose_window("P", 1).startswith("DISCARD_FAILED:")

    # -- salvage, and Mail's send-error sheet --------------------------------
    #
    # When Mail cannot send through the account's server it puts a sheet
    # on the compose window, "Cannot send message using the server …",
    # with Try Later / Try With Selected Server / Connection Doctor /
    # Edit SMTP Server List / Edit Message, and no Save: pressing Save on
    # it, as the salvage did, failed and left the window open. The sheet
    # cannot be provoked on demand, so these read the script's text; no
    # live test covers it.

    def test_salvage_dismisses_the_send_error_sheet_before_closing(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        edit_at = script.index('click button "Edit Message" of sheet 1 of window closeIdx')
        close_at = script.index("my tendClickClose(closeIdx)")
        save_at = script.index('click button "Save" of sheet 1 of window closeIdx')
        assert edit_at < close_at < save_at
        # Only that sheet is dismissed that way: the button is looked for
        # before it is pressed.
        assert 'exists button "Edit Message" of sheet 1 of window closeIdx' in script

    def test_salvage_reports_the_send_error_sheet_text(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        assert "value of static texts of sheet 1 of window closeIdx" in script
        assert "Mail's send-error sheet: " in script
        # The text rides on every outcome after the sheet was read,
        # failures included.
        assert 'set closeOutcome to "SALVAGED" & sheetNote' in script
        assert 'set closeOutcome to "SALVAGE_FAILED:window still open" & sheetNote' in script
        assert 'set closeOutcome to "SALVAGE_FAILED:" & errMsg & sheetNote' in script
        assert "\nreturn closeOutcome\n" in script

    def test_a_discard_has_no_send_error_sheet_step(
        self, connector: AppleMailConnector
    ) -> None:
        assert "Edit Message" not in self._close_script(connector, "discard")

    def test_the_sheet_text_reaches_the_raised_error(
        self, connector: AppleMailConnector
    ) -> None:
        """Whatever the salvage reports is part of the send's error."""
        sheet = (
            "SALVAGED (Mail's send-error sheet: Cannot send message using "
            "the server smtp.example.com. | )"
        )
        outcomes = _compose_outcomes("<p>Hi there probe</p>", window="Probe")
        _scripted(
            connector, outcomes[:self._SEND_AT] + ["WINDOW_STILL_OPEN:x", sheet]
        )
        with pytest.raises(MailAppleScriptError) as exc:
            connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject="Probe",
                body="<p>Hi there probe</p>", from_account=None,
            )
        assert "Cannot send message using the server smtp.example.com." in str(
            exc.value
        )


class TestMailAutomationLock:
    """Cross-process serialization of Mail automation (2026-07-23).

    Multiple agent sessions each spawn their own MCP server; concurrent
    AppleScript against Mail.app collides into AppleEvent timeouts
    (-1712) and invalid connections (-609). A file lock under
    APPLE_MAIL_MCP_HOME queues callers instead, and a caller that cannot
    acquire the lock in time gets a CLEAR busy error instead of
    AppleEvent garbage.
    """

    def test_lock_file_created_under_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        connector = AppleMailConnector(timeout=5)
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "ok"
        with patch("subprocess.run", return_value=fake):
            assert connector._run_applescript("return 1") == "ok"
        assert (Path(str(tmp_path)) / "mail_automation.lock").exists()

    def test_lock_contention_raises_clear_busy_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """Another PROCESS holding the lock → MailAppleScriptError naming
        the busy condition (never reaches osascript)."""
        import subprocess as sp
        import sys
        import time as time_mod
        from pathlib import Path
        from unittest.mock import patch

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        lock_path = Path(str(tmp_path)) / "mail_automation.lock"
        holder = sp.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,sys,time\n"
                    f"fh=open({str(lock_path)!r},'w')\n"
                    "fcntl.flock(fh, fcntl.LOCK_EX)\n"
                    "print('held',flush=True)\n"
                    "time.sleep(10)\n"
                ),
            ],
            stdout=sp.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "held"
            connector = AppleMailConnector(timeout=5, lock_timeout=0.5)
            start = time_mod.monotonic()
            with patch("subprocess.run") as mock_run, pytest.raises(
                MailAppleScriptError, match="busy"
            ):
                connector._run_applescript("return 1")
            assert mock_run.call_count == 0, "osascript must not run without the lock"
            assert time_mod.monotonic() - start < 5
        finally:
            holder.kill()
            holder.wait()


class TestHtmlReplyAndForward:
    """``_send_html_email(reply_to=...)`` and ``(forward_of=...)``: the one
    composition (``_compose``) on Mail's own reply or forward window, the
    HTML pasted above what Mail wrote and the files after it. Until
    2026-09-27 the HTML reply had a composition of its own, which took no
    files, and there was no HTML forward: the "forward" of 2026-09-26 was
    a fresh message with the original pasted into it.

    The outbound allowlist is met twice: on the recipients the caller
    names, before any AppleScript; and on the recipients the window
    holds, read back from the outgoing-message model after every
    override, which include those Mail derived for a reply. Off-list at
    the second, the window is discarded, and nothing is pasted or sent.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    _BODY = "<p>html above <b>what Mail wrote</b></p>"
    _WINDOW = {"reply": "Re: Probe", "forward": "Fwd: Probe"}

    def _send(
        self,
        connector: AppleMailConnector,
        seed: str,
        *,
        to: list[str] | None = None,
        subject: str = "",
        body: str = _BODY,
        files: list[Path] | None = None,
        from_account: str | None = None,
        seed_id: str = "12345",
        outcomes: list[str] | None = None,
    ) -> list[str]:
        """Send an HTML reply or forward against a well-behaved Mail and
        return the scripts it ran: open, paste, read-back, then (with
        files) the file paste and its check, the verified send, and the
        look for its Sent copy (the ids with its subject, the copy
        read)."""
        if outcomes is None:
            outcomes = _compose_outcomes(
                body, window=self._WINDOW[seed], seed=seed,
                file_names=[f.name for f in files or []],
            )
        captured = _scripted(connector, outcomes)
        mode = {"reply_to": seed_id} if seed == "reply" else {"forward_of": seed_id}
        result = connector._send_html_email(
            to=["a@example.com"] if to is None else to,
            cc=None,
            bcc=None,
            subject=subject,
            body=body,
            from_account=from_account,
            attachment_paths=files,
            **mode,
        )
        assert result == _SENT
        return captured

    def test_a_reply_opens_mails_reply_window_and_keeps_what_mail_derived(
        self, connector: AppleMailConnector
    ) -> None:
        """No recipients and no subject given: Mail's own stay, and the
        window's recipients are read from the model, not the UI."""
        scripts = self._send(connector, "reply", to=[])
        # open, paste, read-back, verified send, the Sent look (two)
        assert len(scripts) == 6
        open_s = scripts[0]
        assert 'whose id is "12345"' in open_s
        assert "reply origMsg opening window true" in open_s
        assert "beforeNames" in open_s
        assert "afterCount > beforeCount" in open_s
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in open_s
        assert "address of to recipients of theMessage" in open_s
        assert "delete (every to recipient of theMessage)" not in open_s
        assert "set subject of theMessage" not in open_s
        # A send snapshots Sent, where its copy is looked up; never Drafts.
        assert "set beforeIds to (id of every message of sent mailbox)" in open_s
        assert "drafts mailbox" not in open_s

    def test_a_forward_opens_mails_forward_window_with_its_recipients(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = self._send(connector, "forward", to=["alice@example.com"])
        open_s = scripts[0]
        assert 'whose id is "12345"' in open_s
        assert "forward origMsg opening window true" in open_s
        assert "make new outgoing message" not in open_s
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'repeat with addr in {"alice@example.com"}' in open_s

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_the_html_goes_above_what_mail_wrote_and_is_sent_verified(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """cmd+up, then the paste: never a select-all or a delete, which
        would take Mail's quote or forwarded message with it. The content
        is never set, and the window's Send button sends."""
        scripts = self._send(connector, seed)
        _, paste_s, readback_s, send_s, ids_s, _ = scripts
        assert "key code 126 using command down" in paste_s
        assert "public.html" in paste_s
        assert 'keystroke "a" using command down' not in paste_s
        assert "key code 51" not in paste_s
        assert "AXWebArea" in readback_s
        assert "enabled of sendBtn" in send_s
        assert f'set composeName to "{self._WINDOW[seed]}"' in send_s
        assert f'sent mailbox whose subject is "{self._WINDOW[seed]}"' in ids_s
        assert all("set content" not in s for s in scripts)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_files_go_after_what_mail_wrote_and_are_checked_in_the_sent_copy(
        self, connector: AppleMailConnector, tmp_path: Path, seed: str
    ) -> None:
        """Pasted at the end, never attached through the dictionary: on a
        reply or forward whose body was untouched, ``make new attachment``
        sent the original unquoted and dropped a forward's own files
        (docs/research/icloud-draft-resync.md, Observation 11). The Sent
        copy must carry the caller's file; a forward's carries the
        original's as well."""
        mine = tmp_path / "mine.txt"
        mine.write_text("mine")
        outcomes = _compose_outcomes(
            self._BODY, window=self._WINDOW[seed], seed=seed,
            file_names=["mine.txt"],
        )
        outcomes[-1] = _sent_copy_outcomes(["theirs.pdf", "mine.txt"])[1]
        scripts = self._send(connector, seed, files=[mine], outcomes=outcomes)
        assert len(scripts) == 8
        _, paste_s, _, files_s, verify_s, send_s, ids_s, copy_s = scripts
        assert "key code 126 using command down" in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert mine.resolve().as_posix() in files_s
        assert "key code 125 using command down" in files_s
        assert '"mine.txt"' in verify_s
        assert "click sendBtn" in send_s
        assert f'whose subject is "{self._WINDOW[seed]}"' in ids_s
        assert 'whose id is "9"' in copy_s
        assert "name of every mail attachment of m" in copy_s
        assert all("make new attachment" not in s for s in scripts)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_file_missing_from_the_sent_copy_says_it_was_sent(
        self, connector: AppleMailConnector, tmp_path: Path, seed: str
    ) -> None:
        mine = tmp_path / "mine.txt"
        mine.write_text("mine")
        outcomes = _compose_outcomes(
            self._BODY, window=self._WINDOW[seed], seed=seed,
            file_names=["mine.txt"],
        )
        outcomes[-1] = _sent_copy_outcomes(["theirs.pdf"])[1]
        with pytest.raises(MailAppleScriptError, match="WAS sent"):
            self._send(connector, seed, files=[mine], outcomes=outcomes)

    def test_derived_recipients_off_the_list_discard_the_window(
        self, connector: AppleMailConnector
    ) -> None:
        """Mail derived an off-list recipient for the reply: the window is
        discarded, the HTML never pasted, nothing sent."""
        meta = _compose_meta("Re: Probe", to=["evil@other.com"])
        captured = _scripted(connector, [meta, "DISCARDED"])
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            connector._send_html_email(
                to=[], cc=None, bcc=None, subject="", body="<p>x</p>",
                from_account=None, reply_to="12345",
            )
        assert len(captured) == 2
        assert "AXCloseButton" in captured[1]
        assert all('keystroke "v"' not in s for s in captured)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_off_list_recipient_the_caller_names_never_reaches_mail(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        mode = {"reply_to": "12345"} if seed == "reply" else {"forward_of": "12345"}
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            connector._send_html_email(
                to=["a@example.com"], cc=["evil@other.com"], bcc=None,
                subject="", body="<p>x</p>", from_account=None, **mode,
            )
        assert captured == []

    def test_a_forward_to_no_one_never_reaches_mail(
        self, connector: AppleMailConnector
    ) -> None:
        """Mail derives no recipient for a forward: the connector refuses
        one that names nobody, whatever its caller checked."""
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="no recipients"):
            connector._send_html_email(
                to=[], cc=None, bcc=None, subject="", body="<p>x</p>",
                from_account=None, forward_of="12345",
            )
        assert captured == []

    def test_explicit_recipients_replace_mails_in_the_model(
        self, connector: AppleMailConnector
    ) -> None:
        """There is no reply-all: a caller wanting it names everyone."""
        outcomes = _compose_outcomes(self._BODY, window="Re: Probe", seed="reply")
        outcomes[0] = _compose_meta(
            "Re: Probe", to=["alice@example.com", "bob@example.com"]
        )
        scripts = self._send(
            connector, "reply", to=["alice@example.com", "bob@example.com"],
            outcomes=outcomes,
        )
        open_s = scripts[0]
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'repeat with addr in {"alice@example.com", "bob@example.com"}' in open_s
        assert "reply to all" not in open_s

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_subject_given_replaces_mails(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        scripts = self._send(connector, seed, subject='Say "hi"')
        assert 'set subject of theMessage to "Say \\"hi\\""' in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_named_sender_is_set_last(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            scripts = self._send(connector, seed, from_account="Work")
        resolve.assert_called_once_with("Work")
        open_s = scripts[0]
        sender_at = open_s.index(
            'set sender of theMessage to "Alice Smith <me@example.com>"'
        )
        assert sender_at > open_s.index("opening window true")
        assert sender_at > open_s.rindex("make new to recipient")

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_rfc_message_id_is_resolved_to_mails_id_first(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Read tools on the IMAP path hand out RFC 5322 Message-IDs as
        ids (#148); the seed lookup matches Mail's own id (#205)."""
        with patch.object(
            connector, "find_message_by_message_id", return_value="4242",
        ) as find:
            scripts = self._send(connector, seed, seed_id="<abc@example.com>")
        find.assert_called_once_with("<abc@example.com>")
        assert 'whose id is "4242"' in scripts[0]

    def test_an_rfc_message_id_that_matches_nothing_never_reaches_mail(
        self, connector: AppleMailConnector
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with patch.object(
            connector, "find_message_by_message_id", return_value=None,
        ), pytest.raises(MailMessageNotFoundError):
            connector._send_html_email(
                to=["a@example.com"], cc=None, bcc=None, subject="",
                body="<p>x</p>", from_account=None,
                forward_of="abc@example.com",
            )
        assert captured == []

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_body_leaves_mails_part_as_mail_made_it(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Nothing is pasted: the window is opened, gated and sent, and
        its Sent copy looked for."""
        scripts = self._send(connector, seed, body="")
        assert len(scripts) == 4
        assert "click sendBtn" in scripts[1]
        assert all('keystroke "v"' not in s for s in scripts)


class TestBulkCrossScanCountsEachIdOnce:
    """A message that appears in several mailboxes is one message.

    Gmail exposes every label as a mailbox and files a message under each
    label it carries, so the cross-scan (no ``account``/``source_mailbox``)
    finds the same message in INBOX, All Mail and every user label. Without
    leaving the scan on the first match it ran the actions and bumped the
    counter once per label — ``update_message`` on one message returned 2
    or more, and the count is the only success signal these tools have.

    The scan must leave both the mailbox loop and the account loop once an
    id has matched. AppleScript's ``exit repeat`` only leaves the innermost
    loop, so the outer exit rides on a flag. Mail's numeric ``id`` is
    unique per message, so the first match is the right one.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_cross_scan_exits_both_loops_after_a_match(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.update_message(["123"], flag_color="orange")
        script = mock_run.call_args[0][0]

        assert "repeat with acc in accounts" in script
        # The match is recorded on a flag, the mailbox loop is left at
        # once, and the account loop is left as soon as that loop ends.
        assert "set matched to false" in script
        assert "set matched to true" in script
        # The flag is set and the loop left after the counter bumps, in
        # that order, inside the try.
        counter_at = script.index("set updateCount to updateCount + 1")
        flag_at = script.index("set matched to true")
        exit_at = script.index("exit repeat", flag_at)
        assert counter_at < flag_at < exit_at
        assert "if matched then exit repeat" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_is_reset_per_id_not_per_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Two ids in one call: the second must scan even after the first
        matched, so the reset sits inside the id loop."""
        mock_run.return_value = "2"
        connector.update_message(["1", "2"], flag_color="orange")
        script = mock_run.call_args[0][0]
        id_loop = script.index("repeat with msgId in idList")
        reset = script.index("set matched to false")
        acc_loop = script.index("repeat with acc in accounts")
        assert id_loop < reset < acc_loop

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_narrow_path_is_untouched(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """One mailbox can hold an id at most once; no flag needed there."""
        mock_run.return_value = "1"
        connector.update_message(
            ["1"], flag_color="orange", account="Gmail", source_mailbox="INBOX"
        )
        script = mock_run.call_args[0][0]
        assert "matched" not in script


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
