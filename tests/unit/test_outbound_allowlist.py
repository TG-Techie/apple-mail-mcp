"""Tests for the centralized outbound recipient allowlist (the policy
enforcement perimeter for outbound mail).

Contract (owner directive, 2026-08-24): the comms config YAML
(APPLE_MAIL_MCP_COMMS_CONFIG → email.allowed_outbound) is the ONLY
allowlist source. There are no hardcoded policy values in code. A
missing, unreadable, or malformed config FAILS CLOSED loudly
(OutboundAllowlistUnavailableError) — except under MAIL_TEST_MODE=true,
where RFC 2606 reserved test domains remain sendable so the integration
harness works on machines with no comms.yaml.

NOTE: the autouse `_allowlist_test_domains` fixture in conftest.py writes
a comms.yaml with RFC 2606 test-domain patterns and sets
APPLE_MAIL_MCP_COMMS_CONFIG for every test. Tests that need the
missing-config path override COMMS_CONFIG_ENV to a nonexistent path.
"""

from pathlib import Path

import pytest

from apple_mail_mcp.exceptions import (
    MailOutboundDisallowedError,
    OutboundAllowlistUnavailableError,
)
from apple_mail_mcp.outbound_allowlist import (
    COMMS_CONFIG_ENV,
    all_recipients_allowed,
    allowlist_patterns,
    assert_recipients_allowed_for_send,
    disallowed_recipients,
    extract_email,
)

_NO_YAML = "/nonexistent/comms.yaml"


class TestExtractEmail:
    def test_bare_address(self) -> None:
        assert extract_email("alice@example.com") == "alice@example.com"

    def test_display_name_wrapped(self) -> None:
        assert (
            extract_email("Alice A <alice@example.com>")
            == "alice@example.com"
        )

    def test_angle_only(self) -> None:
        assert extract_email("<alice@example.com>") == "alice@example.com"

    def test_uppercase_normalized(self) -> None:
        assert extract_email("Alice@Example.COM") == "alice@example.com"

    def test_whitespace_trimmed(self) -> None:
        assert extract_email("  alice@example.com  ") == "alice@example.com"


class TestNoHardcodedPolicy:
    def test_module_exposes_no_hardcoded_allowlist(self) -> None:
        """Owner directive 2026-08-24: no hardcoded credentials or
        matching in source. The old constant must be gone."""
        import apple_mail_mcp.outbound_allowlist as mod

        assert not hasattr(mod, "USER_EXPLICIT_OUTBOUND_ALLOW_LIST")

    def test_module_source_carries_no_owner_domain(self) -> None:
        """Grep-level guard: the policy domain must not appear anywhere
        in the module source."""
        import apple_mail_mcp.outbound_allowlist as mod

        source = Path(mod.__file__).read_text()
        assert "tg-techie" not in source.lower()


class TestAllowlistPatterns:
    def test_patterns_come_from_yaml_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text(
            "email:\n"
            "  allowed_outbound:\n"
            "    - '*@partner.example'\n"
            "    - 'named@example.com'\n"
        )
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == ["*@partner.example", "named@example.com"]

    def test_missing_file_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAIL CLOSED: no config file → loud error, not a fallback."""
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        with pytest.raises(OutboundAllowlistUnavailableError):
            allowlist_patterns()

    def test_unavailable_is_a_disallowed_subclass(self) -> None:
        """Existing except-handlers for the policy gate must catch it."""
        assert issubclass(
            OutboundAllowlistUnavailableError, MailOutboundDisallowedError
        )

    def test_unparseable_yaml_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email: [unclosed")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        with pytest.raises(OutboundAllowlistUnavailableError):
            allowlist_patterns()

    def test_non_mapping_root_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("- just\n- a\n- list\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        with pytest.raises(OutboundAllowlistUnavailableError):
            allowlist_patterns()

    def test_non_mapping_email_section_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email: just-a-string\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        with pytest.raises(OutboundAllowlistUnavailableError):
            allowlist_patterns()

    def test_non_list_allowed_outbound_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound: not-a-list\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        with pytest.raises(OutboundAllowlistUnavailableError):
            allowlist_patterns()

    def test_absent_email_section_is_valid_and_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A readable config that simply grants nothing is VALID — it
        blocks all sends via the normal not-on-allowlist path, without
        the unavailable error. The line is 'can the policy be read',
        not 'what does the policy say'."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("imessage:\n  users: {}\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == []

    def test_absent_allowed_outbound_key_is_valid_and_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  known_incoming: []\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == []

    def test_patterns_lowercased(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@Partner.Example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == ["*@partner.example"]

    def test_env_var_overrides_default_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "custom_comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - 'only@example.com'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == ["only@example.com"]

    def test_read_at_call_time_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Owner edits take effect on the next call — no import-time
        caching."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - 'a@example.com'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert allowlist_patterns() == ["a@example.com"]
        cfg.write_text("email:\n  allowed_outbound:\n    - 'b@example.com'\n")
        assert allowlist_patterns() == ["b@example.com"]


class TestAllRecipientsAllowed:
    def test_allowed_when_all_match_yaml_patterns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@partner.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert all_recipients_allowed(
            ["a@partner.example", "Other <b@partner.example>"]
        )

    def test_not_allowed_when_any_off_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@partner.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        assert not all_recipients_allowed(
            ["a@partner.example", "outsider@other.com"]
        )

    def test_empty_recipients_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not all_recipients_allowed([])

    def test_unavailable_config_returns_false_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This function is only the elicitation-bypass UX check —
        fail closed means 'no bypass', never a crash. The HARD gate
        (assert_recipients_allowed_for_send) is the one that raises."""
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(["anyone@example.com"])


class TestDisallowedRecipients:
    def test_partitions_by_yaml_patterns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - 'ok@example.com'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        bad = disallowed_recipients(["ok@example.com", "bad@other.com"])
        assert bad == ["bad@other.com"]

    def test_unavailable_config_raises_outside_test_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(OutboundAllowlistUnavailableError):
            disallowed_recipients(["anyone@example.com"])


class TestMailTestModeCarveOut:
    """MAIL_TEST_MODE=true must keep the integration harness working on
    machines with no comms.yaml: reserved test domains pass, everything
    else stays blocked."""

    def test_reserved_domains_pass_without_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        assert all_recipients_allowed(["test@example.com"])
        assert all_recipients_allowed(["foo@something.test"])
        assert disallowed_recipients(["test@example.com"]) == []

    def test_non_reserved_still_blocked_without_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        bad = disallowed_recipients(["real@gmail.com"])
        assert bad == ["real@gmail.com"]

    def test_reserved_domains_blocked_outside_test_mode_without_yaml_grant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The carve-out is test-mode-only: in production, example.com
        is off-list unless comms.yaml grants it."""
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - 'x@other.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(["test@example.com"])


class TestAssertRecipientsAllowedForSend:
    @pytest.fixture
    def yaml_cfg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Path:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@partner.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        return cfg

    def test_all_allowed_passes_silently(self, yaml_cfg: Path) -> None:
        assert_recipients_allowed_for_send(
            to=["a@partner.example"], cc=None, bcc=None
        )

    def test_any_disallowed_raises(self, yaml_cfg: Path) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["a@partner.example", "outsider@other.com"],
                cc=None,
                bcc=None,
            )

    def test_disallowed_cc_raises(self, yaml_cfg: Path) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["a@partner.example"],
                cc=["outsider@other.com"],
                bcc=None,
            )

    def test_disallowed_bcc_raises(self, yaml_cfg: Path) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["a@partner.example"],
                cc=None,
                bcc=["outsider@other.com"],
            )

    def test_empty_recipients_raise(self, yaml_cfg: Path) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(to=[], cc=None, bcc=None)

    def test_reply_seed_without_recipients_raises(
        self, yaml_cfg: Path
    ) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=None, cc=None, bcc=None, seed="reply"
            )

    def test_forward_seed_without_recipients_raises(
        self, yaml_cfg: Path
    ) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=None, cc=None, bcc=None, seed="forward"
            )

    def test_display_name_form_allowed(self, yaml_cfg: Path) -> None:
        assert_recipients_allowed_for_send(
            to=["Partner P <a@partner.example>"], cc=None, bcc=None
        )

    def test_embedded_second_address_not_fooled(
        self, yaml_cfg: Path
    ) -> None:
        """'a@partner.example <evil@other.com>' extracts the BRACKETED
        address — the off-list one — and is refused."""
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["a@partner.example <evil@other.com>"],
                cc=None,
                bcc=None,
            )

    def test_unavailable_config_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The HARD gate fails closed and loudly when the policy cannot
        be read."""
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(OutboundAllowlistUnavailableError):
            assert_recipients_allowed_for_send(
                to=["anyone@example.com"], cc=None, bcc=None
            )

    def test_test_mode_reserved_recipients_pass_without_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        assert_recipients_allowed_for_send(
            to=["test@example.com"], cc=None, bcc=None
        )
