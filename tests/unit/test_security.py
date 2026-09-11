"""Unit tests for security module."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apple_mail_mcp.security import (
    OPERATION_TIERS,
    TIER_LIMITS,
    OperationLogger,
    RateLimiter,
    _get_test_account_identifiers,
    _is_reserved_test_domain,
    check_rate_limit,
    check_test_mode_safety,
    operation_logger,
    rate_limiter,
    validate_bulk_operation,
    validate_send_operation,
)


class TestOperationLogger:
    """Tests for OperationLogger."""

    def test_logs_operation(self) -> None:
        logger = OperationLogger()
        logger.log_operation("test_op", {"key": "value"}, "success")

        operations = logger.get_recent_operations(limit=1)
        assert len(operations) == 1
        assert operations[0]["operation"] == "test_op"
        assert operations[0]["parameters"] == {"key": "value"}
        assert operations[0]["result"] == "success"

    def test_limits_recent_operations(self) -> None:
        logger = OperationLogger()

        for i in range(20):
            logger.log_operation(f"op_{i}", {}, "success")

        recent = logger.get_recent_operations(limit=5)
        assert len(recent) == 5
        assert recent[-1]["operation"] == "op_19"


class TestOperationLogIsDurable:
    """The in-memory record dies with the server process, and every agent
    session runs its own. Until now nothing on disk said what the server
    had done, so "did any mail go out from the wrong account" could only
    be answered from callers' transcripts. Each entry is now also
    appended, as one JSON line, to audit.jsonl under the data home."""

    def test_each_operation_is_appended_as_one_json_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from apple_mail_mcp.security import audit_log_path

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        logger = OperationLogger()
        logger.log_operation("email_send_html", {"to": ["a@example.com"]}, "success")
        logger.log_operation("delete_rule", {"rule_index": 1}, "cancelled")

        path = audit_log_path()
        assert path == tmp_path / "audit.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["operation"] == "email_send_html"
        assert first["parameters"] == {"to": ["a@example.com"]}
        assert first["result"] == "success"
        assert first["timestamp"]
        assert second["operation"] == "delete_rule"
        assert second["result"] == "cancelled"

    def test_the_path_follows_the_data_home_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apple_mail_mcp.security import audit_log_path

        monkeypatch.delenv("APPLE_MAIL_MCP_HOME", raising=False)
        assert audit_log_path() == Path.home() / ".apple_mail_mcp" / "audit.jsonl"
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "elsewhere"))
        assert audit_log_path() == tmp_path / "elsewhere" / "audit.jsonl"

    def test_non_json_parameters_are_still_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from apple_mail_mcp.security import audit_log_path

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        OperationLogger().log_operation("save_attachments", {"dir": tmp_path}, "success")
        entry = json.loads(audit_log_path().read_text(encoding="utf-8"))
        assert entry["parameters"] == {"dir": str(tmp_path)}

    def test_the_file_is_rotated_at_the_size_cap_keeping_one_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded on disk: when the current file reaches the cap it
        becomes audit.jsonl.1 (replacing the previous .1) and a new file
        starts, so the store holds at most about two caps' worth."""
        from apple_mail_mcp import security

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        monkeypatch.setattr(security, "AUDIT_ROTATE_BYTES", 300)
        logger = OperationLogger()
        for i in range(12):
            logger.log_operation(f"op_{i}", {"n": i}, "success")

        current = security.audit_log_path()
        previous = current.with_name("audit.jsonl.1")
        assert previous.is_file()
        kept = previous.read_text().splitlines() + current.read_text().splitlines()
        longest_line = max(len(line) + 1 for line in kept)
        # Rotation happens on the append that finds the file at the cap, so
        # a generation is at most the cap plus one entry.
        assert 300 <= previous.stat().st_size < 300 + longest_line
        assert current.stat().st_size < 300 + longest_line
        # The newest entry is in the current file, and no third generation exists.
        assert any('"op_11"' in line for line in current.read_text().splitlines())
        assert not current.with_name("audit.jsonl.2").exists()

    def test_an_unwritable_log_warns_and_does_not_break_the_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        blocker = tmp_path / "audit.jsonl"
        blocker.mkdir()  # a directory where the file should be: open() fails
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        logger = OperationLogger()
        with caplog.at_level(logging.WARNING, logger="apple_mail_mcp.security"):
            logger.log_operation("list_accounts", {}, "success")
        assert logger.get_recent_operations(limit=1)[0]["operation"] == "list_accounts"
        assert any("audit log" in r.getMessage() for r in caplog.records)


class TestValidateSendOperation:
    """Tests for validate_send_operation."""

    def test_valid_single_recipient(self) -> None:
        is_valid, error = validate_send_operation(["user@example.com"])
        assert is_valid is True
        assert error == ""

    def test_valid_multiple_recipients(self) -> None:
        is_valid, error = validate_send_operation(
            to=["user1@example.com"],
            cc=["user2@example.com"],
            bcc=["user3@example.com"]
        )
        assert is_valid is True
        assert error == ""

    def test_no_recipients(self) -> None:
        is_valid, error = validate_send_operation([])
        assert is_valid is False
        assert "required" in error.lower()

    def test_invalid_email(self) -> None:
        is_valid, error = validate_send_operation(["invalid-email"])
        assert is_valid is False
        assert "invalid" in error.lower()

    def test_too_many_recipients(self) -> None:
        recipients = [f"user{i}@example.com" for i in range(150)]
        is_valid, error = validate_send_operation(recipients)
        assert is_valid is False
        assert "too many" in error.lower()


class TestValidateBulkOperation:
    """Tests for validate_bulk_operation."""

    def test_valid_count(self) -> None:
        is_valid, error = validate_bulk_operation(50, max_items=100)
        assert is_valid is True
        assert error == ""

    def test_zero_items(self) -> None:
        is_valid, error = validate_bulk_operation(0)
        assert is_valid is False
        assert "no items" in error.lower()

    def test_too_many_items(self) -> None:
        is_valid, error = validate_bulk_operation(150, max_items=100)
        assert is_valid is False
        assert "too many" in error.lower()

    def test_exactly_max_items(self) -> None:
        is_valid, error = validate_bulk_operation(100, max_items=100)
        assert is_valid is True


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Tests for the sliding-window RateLimiter."""

    def setup_method(self) -> None:
        self.limiter = RateLimiter()

    def test_allows_calls_up_to_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            assert self.limiter.check("sends") is True

    def test_rejects_call_over_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False

    def test_allows_after_window_expires(self) -> None:
        max_calls, window = TIER_LIMITS["sends"]
        for _ in range(max_calls):
            self.limiter.check("sends")

        fake_time = [0.0]

        def monotonic() -> float:
            return fake_time[0]

        with patch("apple_mail_mcp.security.time") as mock_time:
            mock_time.monotonic = monotonic
            # First, fill to limit at t=0
            limiter = RateLimiter()
            for _ in range(max_calls):
                limiter.check("sends")
            assert limiter.check("sends") is False

            # Advance past window
            fake_time[0] = window + 1.0
            assert limiter.check("sends") is True

    def test_tiers_are_independent(self) -> None:
        max_sends = TIER_LIMITS["sends"][0]
        for _ in range(max_sends):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False
        assert self.limiter.check("cheap_reads") is True
        assert self.limiter.check("expensive_ops") is True

    def test_reset_clears_all_tiers(self) -> None:
        max_sends = TIER_LIMITS["sends"][0]
        for _ in range(max_sends):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False

        self.limiter.reset()
        assert self.limiter.check("sends") is True

    def test_module_level_instance_exists(self) -> None:
        assert isinstance(rate_limiter, RateLimiter)


# ---------------------------------------------------------------------------
# check_rate_limit helper
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    """Tests for the check_rate_limit helper function."""

    def setup_method(self) -> None:
        rate_limiter.reset()
        operation_logger.operations.clear()

    def test_returns_none_when_under_limit(self) -> None:
        result = check_rate_limit("list_mailboxes", {"account": "Gmail"})
        assert result is None

    def test_returns_error_dict_when_over_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            check_rate_limit("create_draft", {"subject": "x"})

        result = check_rate_limit("create_draft", {"subject": "x"})
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert "sends" in result["error"]

    def test_logs_rate_limited_to_operation_logger(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            check_rate_limit("create_draft", {"subject": "x"})

        check_rate_limit("create_draft", {"subject": "blocked"})

        recent = operation_logger.get_recent_operations(limit=10)
        rate_limited_entries = [
            op for op in recent if op["result"] == "rate_limited"
        ]
        assert len(rate_limited_entries) == 1
        assert rate_limited_entries[0]["operation"] == "create_draft"
        assert rate_limited_entries[0]["parameters"] == {"subject": "blocked"}

    def test_error_message_includes_limit_and_window(self) -> None:
        max_calls, window = TIER_LIMITS["sends"]
        for _ in range(max_calls):
            check_rate_limit("create_draft", {"subject": "x"})

        result = check_rate_limit("create_draft", {"subject": "x"})
        assert result is not None
        assert str(max_calls) in result["error"]
        assert str(int(window)) in result["error"]

    def test_all_operations_have_tier_assigned(self) -> None:
        expected_ops = {
            "list_accounts", "list_rules", "list_mailboxes", "get_messages",
            "get_thread", "save_attachments", "search_messages",
            "update_message", "create_mailbox", "update_mailbox",
            "delete_mailbox", "delete_messages",
            "create_draft", "update_draft", "delete_draft",
            "email_send_html",
            "delete_rule", "create_rule", "update_rule",
            "list_templates", "get_template", "save_template",
            "delete_template", "render_template",
        }
        assert set(OPERATION_TIERS.keys()) == expected_ops

    def test_tier_limits_config_exists_for_all_tiers(self) -> None:
        expected_tiers = {"cheap_reads", "expensive_ops", "sends"}
        assert set(TIER_LIMITS.keys()) == expected_tiers
        for _tier, (max_calls, window) in TIER_LIMITS.items():
            assert max_calls > 0
            assert window > 0


# ---------------------------------------------------------------------------
# Test-mode safety
# ---------------------------------------------------------------------------


class TestIsReservedTestDomain:
    """Tests for RFC 2606 reserved test domain detection."""

    def test_example_dot_com_is_reserved(self) -> None:
        assert _is_reserved_test_domain("a@example.com") is True

    def test_example_org_and_net_reserved(self) -> None:
        assert _is_reserved_test_domain("a@example.org") is True
        assert _is_reserved_test_domain("a@example.net") is True

    def test_dot_test_tld_reserved(self) -> None:
        assert _is_reserved_test_domain("a@foo.test") is True

    def test_dot_invalid_tld_reserved(self) -> None:
        assert _is_reserved_test_domain("a@foo.invalid") is True

    def test_dot_localhost_tld_reserved(self) -> None:
        assert _is_reserved_test_domain("a@foo.localhost") is True

    def test_dot_example_tld_reserved(self) -> None:
        assert _is_reserved_test_domain("a@foo.example") is True

    def test_real_domains_not_reserved(self) -> None:
        assert _is_reserved_test_domain("a@gmail.com") is False
        assert _is_reserved_test_domain("a@anthropic.com") is False

    def test_malformed_email_not_reserved(self) -> None:
        assert _is_reserved_test_domain("not-an-email") is False
        assert _is_reserved_test_domain("") is False

    def test_case_insensitive(self) -> None:
        assert _is_reserved_test_domain("a@EXAMPLE.COM") is True
        assert _is_reserved_test_domain("a@FOO.TEST") is True


class TestCheckTestModeSafety:
    """Tests for check_test_mode_safety helper."""

    def setup_method(self) -> None:
        operation_logger.operations.clear()
        # Clear the per-process UUID-resolution cache so tests don't see
        # cached identifiers from other tests' mocked subprocess returns.
        _get_test_account_identifiers.cache_clear()

    def test_no_test_mode_returns_none(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert check_test_mode_safety("search_messages", account="Gmail") is None
        assert (
            check_test_mode_safety("create_draft", recipients=["real@person.com"])
            is None
        )

    def test_test_mode_without_test_account_fails_account_ops(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.delenv("MAIL_TEST_ACCOUNT", raising=False)

        result = check_test_mode_safety("search_messages", account="Gmail")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "MAIL_TEST_ACCOUNT" in result["error"]

    # delete_messages and update_message take message ids, which are
    # global across accounts, so with account=None they reach any
    # account. The gate only compared an account that was given; with
    # none given it stood aside, and test mode did not confine a delete
    # or a move to the test account at all.

    @pytest.mark.parametrize("operation", ["delete_messages", "update_message"])
    def test_a_mutation_without_an_account_is_refused_in_test_mode(
        self, monkeypatch: Any, operation: str
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = check_test_mode_safety(operation, account=None)

        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "TestAccount" in result["error"]
        assert operation in result["error"]

    @pytest.mark.parametrize("operation", ["search_messages", "list_mailboxes"])
    def test_a_read_without_an_account_is_still_fine_in_test_mode(
        self, monkeypatch: Any, operation: str
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        assert check_test_mode_safety(operation, account=None) is None

    def test_account_matches_returns_none(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        assert check_test_mode_safety("search_messages", account="TestAccount") is None

    def test_account_mismatch_returns_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = check_test_mode_safety("search_messages", account="Gmail")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "Gmail" in result["error"]
        assert "TestAccount" in result["error"]

    @patch("apple_mail_mcp.security.subprocess.run")
    def test_uuid_matching_test_account_returns_none(
        self, mock_run: Any, monkeypatch: Any
    ) -> None:
        """A UUID that resolves to MAIL_TEST_ACCOUNT must be allowed."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = type(
            "Result", (), {"returncode": 0, "stdout": uuid + "\n", "stderr": ""}
        )()

        assert check_test_mode_safety("search_messages", account=uuid) is None

    @patch("apple_mail_mcp.security.subprocess.run")
    def test_unrelated_uuid_returns_error(
        self, mock_run: Any, monkeypatch: Any
    ) -> None:
        """A UUID that doesn't match the test account's UUID is rejected."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        test_uuid = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        wrong_uuid = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
        mock_run.return_value = type(
            "Result", (), {"returncode": 0, "stdout": test_uuid, "stderr": ""}
        )()

        result = check_test_mode_safety("search_messages", account=wrong_uuid)
        assert result is not None
        assert result["error_type"] == "safety_violation"

    # --- Rule-mutation prefix gate (#63) -------------------------------

    def test_rule_mutation_with_test_prefix_returns_none(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        assert (
            check_test_mode_safety(
                "update_rule",
                rule_name="[apple-mail-mcp-test] my rule",
            )
            is None
        )

    def test_rule_mutation_without_test_prefix_returns_error(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(
            "delete_rule",
            rule_name="News From Apple",
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "[apple-mail-mcp-test]" in result["error"]

    def test_rule_mutation_outside_test_mode_allowed(
        self, monkeypatch: Any
    ) -> None:
        """No prefix enforcement when MAIL_TEST_MODE is not set."""
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert (
            check_test_mode_safety(
                "delete_rule",
                rule_name="News From Apple",
            )
            is None
        )

    def test_rule_mutation_with_no_rule_name_skipped(
        self, monkeypatch: Any
    ) -> None:
        """When the caller doesn't supply rule_name, the gate has nothing
        to check (e.g. rule_index couldn't be resolved). Returns None."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        assert (
            check_test_mode_safety("delete_rule", rule_name=None)
            is None
        )

    @patch("apple_mail_mcp.security.subprocess.run")
    def test_uuid_lookup_failure_falls_back_to_name_only(
        self, mock_run: Any, monkeypatch: Any
    ) -> None:
        """When UUID lookup fails, name-only matching still enforces the gate."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        # Subprocess returns nonzero — account doesn't exist or AS denied.
        mock_run.return_value = type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "no such account"}
        )()

        # Name still allowed.
        assert check_test_mode_safety("search_messages", account="TestAccount") is None
        # A random UUID must still be rejected.
        result = check_test_mode_safety(
            "search_messages",
            account="DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5",
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"

    def test_send_all_reserved_recipients_ok(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        assert (
            check_test_mode_safety("create_draft", recipients=["a@example.com"]) is None
        )
        assert (
            check_test_mode_safety(
                "create_draft", recipients=["a@example.com", "b@foo.test"]
            )
            is None
        )

    def test_send_with_one_real_recipient_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety(
            "create_draft", recipients=["a@example.com", "real@person.com"]
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "real@person.com" in result["error"]

    def test_recipients_none_means_this_call_sends_nothing(
        self, monkeypatch: Any
    ) -> None:
        """A draft being saved reaches the gate for its account only,
        with recipients=None; nothing leaves, so there is nothing to
        confine. The send path always passes a list — an empty one is a
        send whose recipients Mail would derive, refused below (#175).
        Until 2026-09-11 None was refused too, which made the gate
        unusable for the save path."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        assert check_test_mode_safety("create_draft", recipients=None) is None

    def test_send_blocked_when_recipients_empty_in_test_mode(
        self, monkeypatch: Any
    ) -> None:
        """#175: same as above but with explicit empty list."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("create_draft", recipients=[])
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]

    def test_send_blocked_for_update_draft_empty_recipients(
        self, monkeypatch: Any
    ) -> None:
        """#175: same gap applies to update_draft's send path."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("update_draft", recipients=[])
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "update_draft" in result["error"]

    def test_send_empty_recipients_passes_outside_test_mode(
        self, monkeypatch: Any
    ) -> None:
        """#175: regression guard — the empty-recipients reject is
        scoped to test mode. Outside test mode, the gate early-returns
        None and the new branch is never reached."""
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)

        assert check_test_mode_safety("create_draft", recipients=None) is None
        assert check_test_mode_safety("create_draft", recipients=[]) is None

    def test_non_send_operation_with_empty_recipients_unchanged(
        self, monkeypatch: Any
    ) -> None:
        """#175: regression guard — the new empty-recipients reject
        only fires for operations in SEND_OPERATIONS. Other ops with
        empty recipients (which is meaningless for them anyway) are
        unaffected."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        # delete_messages isn't a send op — the new branch shouldn't fire.
        # (It names the test account, since a mutation without one is
        # refused on its own grounds.)
        assert (
            check_test_mode_safety(
                "delete_messages", account="TestAccount", recipients=None
            ) is None
        )
        assert (
            check_test_mode_safety(
                "delete_messages", account="TestAccount", recipients=[]
            ) is None
        )

    def test_non_gated_operation_returns_none(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        # get_messages is not gated (no account param, not a send)
        assert check_test_mode_safety("get_messages") is None

    def test_violation_logged_to_operation_logger(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        check_test_mode_safety("search_messages", account="Gmail")

        recent = operation_logger.get_recent_operations(limit=5)
        violations = [op for op in recent if op["result"] == "safety_violation"]
        assert len(violations) == 1
        assert violations[0]["operation"] == "search_messages"


class TestAccountGateCoversEveryAccountScopedMutation:
    """Every operation that takes an account and changes it is gated.

    The gate exists so integration runs under MAIL_TEST_MODE cannot reach a
    real account. It was keyed on a hand-kept set that named create_mailbox
    but not update_mailbox, delete_mailbox or delete_messages — so the
    server called check_test_mode_safety for the two mailbox tools and the
    call was a no-op, and delete_messages never called it at all. The
    parametrize below is the list; a mutation added later that takes an
    account belongs in it.
    """

    @pytest.mark.parametrize(
        "operation",
        [
            "update_message",
            "create_mailbox",
            "update_mailbox",
            "delete_mailbox",
            "delete_messages",
            # The sender the caller names: a draft saved into, or mail
            # sent from, that account.
            "create_draft",
            "update_draft",
            "email_send_html",
        ],
    )
    def test_mutation_on_another_account_is_refused(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = check_test_mode_safety(operation, account="Gmail")
        assert result is not None, f"{operation} is not account-gated"
        assert result["error_type"] == "safety_violation"

    @pytest.mark.parametrize(
        "operation",
        [
            "update_mailbox", "delete_mailbox", "delete_messages",
            "create_draft", "update_draft", "email_send_html",
        ],
    )
    def test_mutation_on_the_test_account_is_allowed(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        assert check_test_mode_safety(operation, account="TestAccount") is None


class TestAForwardingRuleIsConfinedLikeASend:
    """In test mode a send may only reach RFC 2606 reserved domains. A
    rule's ``forward_to`` is a send that repeats for every matching
    message, so an integration run creating one is held to the same
    domains; the rule-name prefix alone says nothing about where the
    mail goes."""

    @pytest.mark.parametrize("operation", ["create_rule", "update_rule"])
    def test_a_forward_to_a_real_domain_is_refused(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(
            operation,
            rule_name="[apple-mail-mcp-test] forward",
            recipients=["test@example.com", "real@person.com"],
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "real@person.com" in result["error"]

    @pytest.mark.parametrize("operation", ["create_rule", "update_rule"])
    def test_a_forward_to_reserved_domains_is_allowed(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        assert (
            check_test_mode_safety(
                operation,
                rule_name="[apple-mail-mcp-test] forward",
                recipients=["test@example.com", "other@sub.test"],
            )
            is None
        )

    @pytest.mark.parametrize("operation", ["create_rule", "update_rule"])
    def test_a_rule_that_does_not_forward_needs_no_recipients(
        self, operation: str, monkeypatch: Any
    ) -> None:
        """Unlike a send, a rule with nothing to forward is the normal
        case, not a derived-recipient hazard."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        assert (
            check_test_mode_safety(
                operation, rule_name="[apple-mail-mcp-test] move", recipients=None,
            )
            is None
        )
        assert (
            check_test_mode_safety(
                operation, rule_name="[apple-mail-mcp-test] move", recipients=[],
            )
            is None
        )

    def test_outside_test_mode_nothing_is_checked_here(
        self, monkeypatch: Any
    ) -> None:
        """The allowlist, not this gate, governs production forwards."""
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert (
            check_test_mode_safety(
                "create_rule", rule_name="x", recipients=["real@person.com"],
            )
            is None
        )


class TestEverySendPathIsConfinedInTestMode:
    """In test mode a send may reach only RFC 2606 reserved domains. The
    set that says which operations are sends was hand-kept and named the
    two draft tools, not email_send_html, the preferred send tool; its
    recipients passed this gate unexamined, so an integration run could
    mail anyone on the allowlist. The parametrize below is the list; a
    tool added later that sends belongs in it."""

    @pytest.mark.parametrize(
        "operation", ["create_draft", "update_draft", "email_send_html"],
    )
    def test_a_send_to_a_real_domain_is_refused(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(
            operation, recipients=["test@example.com", "real@person.com"],
        )
        assert result is not None, f"{operation} is not a gated send"
        assert result["error_type"] == "safety_violation"
        assert "real@person.com" in result["error"]

    @pytest.mark.parametrize(
        "operation", ["create_draft", "update_draft", "email_send_html"],
    )
    def test_a_send_with_no_explicit_recipients_is_refused(
        self, operation: str, monkeypatch: Any
    ) -> None:
        """A reply with nothing explicit lets Mail derive the recipients
        at send time, past this gate."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(operation, recipients=[])
        assert result is not None, f"{operation} is not a gated send"
        assert result["error_type"] == "safety_violation"
