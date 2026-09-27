"""Unit tests for security module."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apple_mail_mcp.security import (
    ACCOUNT_GATED_OPERATIONS,
    ACCOUNT_REQUIRED_MUTATIONS,
    OPERATION_TIERS,
    RULE_GATED_OPERATIONS,
    SEND_OPERATIONS,
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
            check_rate_limit("draft_send", {"subject": "x"})

        result = check_rate_limit("draft_send", {"subject": "x"})
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert "sends" in result["error"]

    def test_logs_rate_limited_to_operation_logger(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            check_rate_limit("draft_send", {"subject": "x"})

        check_rate_limit("draft_send", {"subject": "blocked"})

        recent = operation_logger.get_recent_operations(limit=10)
        rate_limited_entries = [
            op for op in recent if op["result"] == "rate_limited"
        ]
        assert len(rate_limited_entries) == 1
        assert rate_limited_entries[0]["operation"] == "draft_send"
        assert rate_limited_entries[0]["parameters"] == {"subject": "blocked"}

    def test_error_message_includes_limit_and_window(self) -> None:
        max_calls, window = TIER_LIMITS["sends"]
        for _ in range(max_calls):
            check_rate_limit("draft_send", {"subject": "x"})

        result = check_rate_limit("draft_send", {"subject": "x"})
        assert result is not None
        assert str(max_calls) in result["error"]
        assert str(int(window)) in result["error"]

    async def test_all_operations_have_tier_assigned(self) -> None:
        """The tiers are keyed by tool name, one per registered tool: a
        tool added without a tier fails here, and so does a tier left
        behind under a name no tool has."""
        from apple_mail_mcp import server

        tools = {t.name for t in await server.mcp.list_tools()}
        assert set(OPERATION_TIERS) == tools

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
            check_test_mode_safety("draft_send", recipients=["real@person.com"])
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

    @pytest.mark.parametrize(
        "operation",
        [
            "delete_messages", "update_message",
            # A draft id likewise names a draft in any account; the tools
            # read the draft's account back and pass it here, and one Mail
            # cannot name (a local draft) is not the test account.
            "draft_update", "draft_delete", "draft_send",
        ],
    )
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
            check_test_mode_safety("email_send_html", recipients=["a@example.com"])
            is None
        )
        assert (
            check_test_mode_safety(
                "email_send_html", recipients=["a@example.com", "b@foo.test"]
            )
            is None
        )

    def test_send_with_one_real_recipient_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety(
            "email_send_html", recipients=["a@example.com", "real@person.com"]
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "real@person.com" in result["error"]

    def test_send_blocked_when_recipients_empty_in_test_mode(
        self, monkeypatch: Any
    ) -> None:
        """#175: a send that names no recipients has Mail derive them."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = check_test_mode_safety("email_send_html", recipients=[])
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "explicit recipients" in result["error"]

    def test_send_empty_recipients_passes_outside_test_mode(
        self, monkeypatch: Any
    ) -> None:
        """#175: regression guard — the empty-recipients reject is
        scoped to test mode. Outside test mode, the gate early-returns
        None and the new branch is never reached."""
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)

        assert check_test_mode_safety("draft_send", recipients=None) is None
        assert check_test_mode_safety("draft_send", recipients=[]) is None

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
            "draft_create",
            "email_send_html",
            # The account a draft named by id sits in.
            "draft_update",
            "draft_delete",
            "draft_send",
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
            "draft_create", "draft_update", "draft_delete", "draft_send",
            "email_send_html",
        ],
    )
    def test_mutation_on_the_test_account_is_allowed(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        # A send is checked for its recipients as well; these are admitted.
        recipients = ["a@example.com"] if operation in SEND_OPERATIONS else None

        assert (
            check_test_mode_safety(
                operation, account="TestAccount", recipients=recipients,
            )
            is None
        )


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

    @pytest.mark.parametrize("operation", ["draft_send", "email_send_html"])
    def test_a_send_to_a_real_domain_is_refused(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(
            operation,
            account="TestAccount",
            recipients=["test@example.com", "real@person.com"],
        )
        assert result is not None, f"{operation} is not a gated send"
        assert result["error_type"] == "safety_violation"
        assert "real@person.com" in result["error"]

    @pytest.mark.parametrize("recipients", [[], None], ids=["empty", "none"])
    @pytest.mark.parametrize("operation", ["draft_send", "email_send_html"])
    def test_a_send_with_no_explicit_recipients_is_refused(
        self, operation: str, recipients: list[str] | None, monkeypatch: Any
    ) -> None:
        """A reply with nothing explicit lets Mail derive the recipients
        at send time, past this gate, and a call that passes none gives
        it nothing to check. Every call to a send operation sends, so
        either is refused."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        result = check_test_mode_safety(
            operation, account="TestAccount", recipients=recipients,
        )
        assert result is not None, f"{operation} is not a gated send"
        assert "explicit recipients" in result["error"]
        assert operation in result["error"]
        assert result["error_type"] == "safety_violation"


class TestEachToolIsClassifiedUnderItsOwnName:
    """A tool passes its own name to check_rate_limit,
    check_test_mode_safety and the audit log, and the tables in
    security.py say what that tool does. A name missing from the
    test-mode tables is not an error there: the gate passes it as a
    no-op. So every registered tool is accounted for below, and a tool
    added later fails here until it is placed."""

    # What each draft tool does, as the tables must say it:
    # (rate-limit tier, account-gated, account required, sends).
    DRAFT_TOOLS: dict[str, tuple[str, bool, bool, bool]] = {
        # Writes a draft into the account its named sender belongs to.
        "draft_create": ("expensive_ops", True, False, False),
        # Rebuilds a draft named by id, in its own account or under the
        # sender the caller names.
        "draft_update": ("expensive_ops", True, True, False),
        # Removes a draft named by id from its account.
        "draft_delete": ("expensive_ops", True, True, False),
        # Sends a draft named by id: the one send from a draft.
        "draft_send": ("sends", True, True, True),
    }

    # Tools test mode does not confine, and why. Placing a tool here
    # decides that an integration run may call it on any account.
    NOT_CONFINED: dict[str, str] = {
        "list_accounts": "reads the account list",
        "list_rules": "reads the rule list",
        "get_messages": "reads messages by id",
        "get_thread": "reads a thread by message id",
        "save_attachments": "reads a message; writes files to a local directory",
        "list_templates": "local template files",
        "get_template": "local template files",
        "save_template": "local template files",
        "delete_template": "local template files",
        "render_template": "a local template, and a read of the message it fills from",
    }

    @staticmethod
    async def _registered_tools() -> set[str]:
        from apple_mail_mcp import server

        return {t.name for t in await server.mcp.list_tools()}

    async def test_every_draft_tool_has_a_row(self) -> None:
        tools = await self._registered_tools()
        assert {t for t in tools if t.startswith("draft_")} == set(self.DRAFT_TOOLS)

    @pytest.mark.parametrize("tool", sorted(DRAFT_TOOLS))
    def test_the_tables_say_what_the_draft_tool_does(self, tool: str) -> None:
        tier, account_gated, account_required, sends = self.DRAFT_TOOLS[tool]
        assert OPERATION_TIERS[tool] == tier
        assert (tool in ACCOUNT_GATED_OPERATIONS) is account_gated
        assert (tool in ACCOUNT_REQUIRED_MUTATIONS) is account_required
        assert (tool in SEND_OPERATIONS) is sends

    async def test_every_tool_is_confined_by_test_mode_or_says_why_not(
        self,
    ) -> None:
        tools = await self._registered_tools()
        confined = (
            ACCOUNT_GATED_OPERATIONS | SEND_OPERATIONS | RULE_GATED_OPERATIONS
        )
        assert tools - confined == set(self.NOT_CONFINED)

    async def test_the_test_mode_tables_name_only_tools(self) -> None:
        """A name in these tables that no tool passes confines nothing."""
        tools = await self._registered_tools()
        for table in (
            ACCOUNT_GATED_OPERATIONS, ACCOUNT_REQUIRED_MUTATIONS,
            SEND_OPERATIONS, RULE_GATED_OPERATIONS,
        ):
            assert table <= tools, table - tools


class TestTheLoopbackIsAdmittedForSends:
    """Test mode confines a send to RFC 2606 reserved domains, which no
    mailbox receives, so an integration run could check what it sent
    but never what arrived. ``MAIL_TEST_LOOPBACK`` names one real
    address from which mail comes back to the test account; a send may
    reach exactly that address as well. A rule's ``forward_to`` may not:
    a rule forwards every matching message for as long as it exists,
    and the loopback belongs to a person."""

    LOOPBACK = "loopback@person.com"

    @pytest.fixture(autouse=True)
    def _test_mode(self, monkeypatch: Any) -> None:
        operation_logger.operations.clear()
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        monkeypatch.delenv("MAIL_TEST_LOOPBACK", raising=False)

    @staticmethod
    def _violations(result: dict[str, Any]) -> list[str]:
        """The recipients a safety error names as refused."""
        return result["error"].split("Violations: ", 1)[1].split(", ")

    @pytest.mark.parametrize("operation", ["draft_send", "email_send_html"])
    def test_a_send_to_exactly_the_loopback_passes(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        assert (
            check_test_mode_safety(
                operation, account="TestAccount", recipients=[self.LOOPBACK],
            )
            is None
        )

    def test_the_loopback_matches_in_any_case(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", "LoopBack@Person.com")
        assert (
            check_test_mode_safety(
                "email_send_html", recipients=["loopback@PERSON.COM"],
            )
            is None
        )

    def test_the_variable_is_read_with_surrounding_space_stripped(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", f"  {self.LOOPBACK}\n")
        assert (
            check_test_mode_safety(
                "email_send_html", recipients=[self.LOOPBACK],
            )
            is None
        )

    def test_another_real_address_is_still_refused(
        self, monkeypatch: Any
    ) -> None:
        """The match is on the whole address: another mailbox at the
        loopback's own domain is not admitted."""
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        result = check_test_mode_safety(
            "email_send_html", recipients=["real@person.com"],
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert self._violations(result) == ["real@person.com"]

    def test_a_mixed_list_names_only_the_refused_recipient(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        result = check_test_mode_safety(
            "email_send_html",
            recipients=[self.LOOPBACK, "a@example.com", "someone@partner.com"],
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert self._violations(result) == ["someone@partner.com"]

    def test_the_refusal_names_the_admitted_loopback(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        result = check_test_mode_safety(
            "email_send_html", recipients=["someone@partner.com"],
        )
        assert result is not None
        assert f"MAIL_TEST_LOOPBACK address {self.LOOPBACK}" in result["error"]

    @pytest.mark.parametrize("unset", [None, "", "   "])
    def test_without_the_variable_the_loopback_is_refused(
        self, unset: str | None, monkeypatch: Any
    ) -> None:
        if unset is not None:
            monkeypatch.setenv("MAIL_TEST_LOOPBACK", unset)
        result = check_test_mode_safety(
            "email_send_html", recipients=[self.LOOPBACK],
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert self._violations(result) == [self.LOOPBACK]
        assert (
            "set MAIL_TEST_LOOPBACK to admit one real address"
            in result["error"]
        )

    def test_reserved_domains_still_pass_with_the_variable_set(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        assert (
            check_test_mode_safety(
                "email_send_html",
                recipients=["a@example.com", "b@foo.test", self.LOOPBACK],
            )
            is None
        )

    def test_explicit_recipients_are_still_required(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        result = check_test_mode_safety("email_send_html", recipients=[])
        assert result is not None
        assert "explicit recipients" in result["error"]

    @pytest.mark.parametrize("operation", ["create_rule", "update_rule"])
    def test_a_rule_may_not_forward_to_the_loopback(
        self, operation: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        result = check_test_mode_safety(
            operation,
            rule_name="[apple-mail-mcp-test] forward",
            recipients=["test@example.com", self.LOOPBACK],
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert self._violations(result) == [self.LOOPBACK]
        assert "MAIL_TEST_LOOPBACK does not apply" in result["error"]

    def test_outside_test_mode_the_variable_changes_nothing(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        monkeypatch.setenv("MAIL_TEST_LOOPBACK", self.LOOPBACK)
        assert (
            check_test_mode_safety(
                "email_send_html", recipients=["someone@partner.com"],
            )
            is None
        )
