"""Tests for the centralized outbound recipient allowlist (the policy
enforcement perimeter for outbound mail).

NOTE: the autouse `_allowlist_test_domains` fixture in conftest.py writes
a comms.yaml with RFC 2606 test-domain patterns and sets
APPLE_MAIL_MCP_COMMS_CONFIG for every test. Tests that need the
hardcoded-defaults-only path override COMMS_CONFIG_ENV to a nonexistent
path so _load_comms_yaml_patterns() returns [].
"""

from pathlib import Path

import pytest

from apple_mail_mcp.exceptions import MailOutboundDisallowedError
from apple_mail_mcp.outbound_allowlist import (
    COMMS_CONFIG_ENV,
    USER_EXPLICIT_OUTBOUND_ALLOW_LIST,
    all_recipients_allowed,
    allowlist_patterns,
    assert_recipients_allowed_for_send,
    disallowed_recipients,
    extract_email,
)

_NO_YAML = "/nonexistent/comms.yaml"


class TestExtractEmail:
    def test_bare_address(self) -> None:
        assert extract_email("jonah@tg-techie.com") == "jonah@tg-techie.com"

    def test_display_name_wrapped(self) -> None:
        assert (
            extract_email("Jonah Y-M <jonah@tg-techie.com>")
            == "jonah@tg-techie.com"
        )

    def test_angle_only(self) -> None:
        assert extract_email("<jonah@tg-techie.com>") == "jonah@tg-techie.com"

    def test_uppercase_normalized(self) -> None:
        assert extract_email("Jonah@TG-Techie.com") == "jonah@tg-techie.com"

    def test_whitespace_trimmed(self) -> None:
        assert extract_email("  jonah@tg-techie.com  ") == "jonah@tg-techie.com"


class TestAllowlistPatterns:
    def test_hardcoded_default_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_uses_hardcoded_constant(self) -> None:
        """The exposed constant is the source of truth."""
        assert USER_EXPLICIT_OUTBOUND_ALLOW_LIST == ("*@tg-techie.com",)


class TestCommsYamlPatterns:
    """Tests for APPLE_MAIL_MCP_COMMS_CONFIG YAML integration."""

    def test_yaml_patterns_merged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Patterns from comms.yaml are merged into allowlist_patterns()."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text(
            "email:\n"
            "  allowed_outbound:\n"
            "    - extra@custom.com\n"
            "    - '*@allowed.test'\n"
        )
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns  # hardcoded still present
        assert "extra@custom.com" in patterns
        assert "*@allowed.test" in patterns

    def test_missing_file_falls_back_to_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing comms.yaml: no error, hardcoded list still works."""
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_invalid_yaml_falls_back_to_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unparseable YAML: log warning, hardcoded list still works."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("{{not: valid: yaml:::\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_wrong_type_falls_back_to_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """email.allowed_outbound not a list: log warning, hardcoded list still works."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound: not-a-list\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_email_section_wrong_type_falls_back_to_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """email section not a mapping: log warning, hardcoded list still works."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email: just-a-string\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_ignores_other_sections(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sibling sections (e.g. imessage) don't leak into the email allowlist."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text(
            "email:\n"
            "  allowed_outbound:\n"
            "    - 'ok@custom.test'\n"
            "  known_incoming:\n"
            "    - 'someone@incoming.test'\n"
            "imessage:\n"
            "  allowed_outbound:\n"
            "    - 'handle-not-an-email'\n"
        )
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "ok@custom.test" in patterns
        assert "someone@incoming.test" not in patterns
        assert "handle-not-an-email" not in patterns

    def test_env_var_overrides_default_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """APPLE_MAIL_MCP_COMMS_CONFIG overrides the default ~/iCloud path."""
        cfg = tmp_path / "custom_comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - custom@override.test\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        patterns = allowlist_patterns()
        assert "custom@override.test" in patterns


class TestAllRecipientsAllowed:
    def test_all_on_hardcoded_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert all_recipients_allowed(
            ["jonah@tg-techie.com", "Other <other@tg-techie.com>"]
        )

    def test_mixed_list_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(
            ["jonah@tg-techie.com", "outsider@other.com"]
        )

    def test_empty_list_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        assert not all_recipients_allowed([])

    def test_display_name_format_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert all_recipients_allowed(["Jonah Y-M <jonah@tg-techie.com>"])

    def test_test_mode_allows_reserved_domains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        assert all_recipients_allowed(["test@example.com"])
        assert all_recipients_allowed(["foo@something.test"])

    def test_test_mode_off_blocks_reserved_domains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(["test@example.com"])


class TestDisallowedRecipients:
    def test_returns_only_off_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        bad = disallowed_recipients(
            [
                "ok@tg-techie.com",
                "evil@other.com",
                "also-evil@another.io",
            ]
        )
        assert bad == ["evil@other.com", "also-evil@another.io"]


class TestAssertRecipientsAllowedForSend:
    def test_passes_when_all_allowlisted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert_recipients_allowed_for_send(
            to=["jonah@tg-techie.com"], cc=None, bcc=None
        )

    def test_raises_on_off_list_recipient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(MailOutboundDisallowedError) as exc:
            assert_recipients_allowed_for_send(
                to=["jonah@tg-techie.com", "outsider@other.com"],
                cc=None,
                bcc=None,
            )
        assert "outsider@other.com" in str(exc.value)

    def test_raises_when_cc_or_bcc_has_offlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["jonah@tg-techie.com"],
                cc=["evil@other.com"],
                bcc=None,
            )
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["jonah@tg-techie.com"],
                cc=None,
                bcc=["evil@other.com"],
            )

    def test_raises_when_reply_has_none_recipients(self) -> None:
        with pytest.raises(MailOutboundDisallowedError) as exc:
            assert_recipients_allowed_for_send(
                to=None, cc=None, bcc=None, seed="reply"
            )
        assert "explicit recipients" in str(exc.value).lower()

    def test_raises_when_forward_has_none_recipients(self) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=None, cc=None, bcc=None, seed="forward"
            )

    def test_new_seed_with_all_none_still_blocks_empty(self) -> None:
        with pytest.raises(MailOutboundDisallowedError) as exc:
            assert_recipients_allowed_for_send(
                to=None, cc=None, bcc=None, seed="new"
            )
        assert "no recipients" in str(exc.value).lower()

    def test_empty_lists_treated_as_empty(self) -> None:
        with pytest.raises(MailOutboundDisallowedError) as exc:
            assert_recipients_allowed_for_send(
                to=[], cc=[], bcc=[], seed="new"
            )
        assert "no recipients" in str(exc.value).lower()

    def test_test_mode_allows_reserved_test_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        assert_recipients_allowed_for_send(
            to=["test@example.com"], cc=None, bcc=None
        )

    def test_display_name_format_extracted_for_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert_recipients_allowed_for_send(
            to=["Jonah Y-M <jonah@tg-techie.com>"], cc=None, bcc=None
        )

    def test_display_name_cannot_smuggle_offlist_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A display-name string that LOOKS like an on-list pattern but
        actually wraps an off-list address must still be blocked."""
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["jonah@tg-techie.com <evil@other.com>"],
                cc=None,
                bcc=None,
            )
