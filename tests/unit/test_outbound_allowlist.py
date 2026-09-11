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
    assert_forward_targets_allowed,
    assert_recipients_allowed_for_send,
    disallowed_recipients,
    single_address,
)

_NO_YAML = "/nonexistent/comms.yaml"


class TestSingleAddress:
    def test_bare_address(self) -> None:
        assert single_address("alice@example.com") == "alice@example.com"

    def test_display_name_wrapped(self) -> None:
        assert (
            single_address("Alice A <alice@example.com>")
            == "alice@example.com"
        )

    def test_angle_only(self) -> None:
        assert single_address("<alice@example.com>") == "alice@example.com"

    def test_uppercase_normalized(self) -> None:
        assert single_address("Alice@Example.COM") == "alice@example.com"

    def test_whitespace_trimmed(self) -> None:
        assert single_address("  alice@example.com  ") == "alice@example.com"

    def test_two_addresses_are_none(self) -> None:
        assert single_address("a@example.com, b@example.com") is None

    def test_no_address_is_none(self) -> None:
        assert single_address("Alice") is None


class TestARecipientStringIsOneAddress:
    """The gate matched the first angle-bracketed address in a recipient
    string and let the whole string through on that. A string carrying
    two addresses ("Ok <a@allowed.example>, evil@other.com") therefore
    passed on its allowed half, and the mailto: send path splits on the
    comma (RFC 6068), so both would have been addressed. A recipient
    is now allowed only if it parses as exactly one well-formed address
    and that address is on the list; anything else is disallowed, since
    what Mail would do with it is not something this layer can vouch for."""

    @pytest.fixture
    def yaml_cfg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Path:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@partner.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        return cfg

    @pytest.mark.parametrize(
        "recipient",
        [
            "Ok <a@partner.example>, evil@other.com",
            "a@partner.example,evil@other.com",
            "a@partner.example evil@other.com",
            "a@partner.example; evil@other.com",
            "a@partner.example\nevil@other.com",
            "a@partner.example\n@partner.example",
            "not-an-address",
            "",
            "<>",
        ],
    )
    def test_anything_but_one_address_is_disallowed(
        self, yaml_cfg: Path, recipient: str
    ) -> None:
        assert disallowed_recipients([recipient]) == [recipient]

    @pytest.mark.parametrize(
        "recipient",
        [
            "a@partner.example",
            "Alice <a@partner.example>",
            "<a@partner.example>",
            "  A@Partner.Example  ",
            '"Alice, A." <a@partner.example>',
        ],
    )
    def test_one_well_formed_address_on_the_list_is_allowed(
        self, yaml_cfg: Path, recipient: str
    ) -> None:
        assert disallowed_recipients([recipient]) == []

    def test_a_display_name_that_looks_like_an_address_does_not_vouch(
        self, yaml_cfg: Path
    ) -> None:
        r = '"a@partner.example" <evil@other.com>'
        assert disallowed_recipients([r]) == [r]


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

    # A reply whose `to` is left None goes to whoever Mail derives it from,
    # which is the original sender, and nothing here ever saw that
    # address. The old rule only fired when every group was None, so an
    # on-list cc beside a None `to` sailed through with the derived `to`
    # unchecked. Every group Mail would derive must be explicit; `[]` is
    # explicit.

    def test_reply_with_an_onlist_cc_but_derived_to_is_refused(
        self, yaml_cfg: Path
    ) -> None:
        with pytest.raises(MailOutboundDisallowedError, match="to"):
            assert_recipients_allowed_for_send(
                to=None, cc=["a@partner.example"], bcc=None, seed="reply"
            )

    def test_reply_all_with_derived_cc_is_refused(
        self, yaml_cfg: Path
    ) -> None:
        with pytest.raises(MailOutboundDisallowedError, match="cc"):
            assert_recipients_allowed_for_send(
                to=["a@partner.example"], cc=None, bcc=None,
                seed="reply", reply_all=True,
            )

    def test_plain_reply_with_explicit_to_and_no_cc_passes(
        self, yaml_cfg: Path
    ) -> None:
        """A plain reply derives only `to`; cc/bcc left None stay empty."""
        assert_recipients_allowed_for_send(
            to=["a@partner.example"], cc=None, bcc=None, seed="reply"
        )

    def test_reply_all_with_both_explicit_passes(
        self, yaml_cfg: Path
    ) -> None:
        assert_recipients_allowed_for_send(
            to=["a@partner.example"], cc=[], bcc=None,
            seed="reply", reply_all=True,
        )

    def test_forward_derives_nothing_so_any_explicit_group_suffices(
        self, yaml_cfg: Path
    ) -> None:
        assert_recipients_allowed_for_send(
            to=None, cc=["a@partner.example"], bcc=None, seed="forward"
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


class TestAssertForwardTargetsAllowed:
    """A rule that forwards is a standing send: every message it matches
    from then on goes to its targets, with nobody reading each one. The
    targets are held to the outbound allowlist exactly as a send's
    recipients are."""

    @pytest.fixture
    def yaml_cfg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Path:
        cfg = tmp_path / "comms.yaml"
        cfg.write_text("email:\n  allowed_outbound:\n    - '*@partner.example'\n")
        monkeypatch.setenv(COMMS_CONFIG_ENV, str(cfg))
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        return cfg

    def test_on_list_targets_pass_silently(self, yaml_cfg: Path) -> None:
        assert_forward_targets_allowed(["a@partner.example", "b@partner.example"])

    def test_any_off_list_target_raises_and_is_named(
        self, yaml_cfg: Path
    ) -> None:
        with pytest.raises(MailOutboundDisallowedError) as exc:
            assert_forward_targets_allowed(
                ["a@partner.example", "leak@elsewhere.example"]
            )
        assert "leak@elsewhere.example" in str(exc.value)
        assert "forward_to" in str(exc.value)

    def test_a_target_carrying_two_addresses_is_refused(
        self, yaml_cfg: Path
    ) -> None:
        """The connector joins the targets with commas into one Mail
        field, so a target that is itself two addresses would smuggle a
        second recipient past the check."""
        with pytest.raises(MailOutboundDisallowedError):
            assert_forward_targets_allowed(
                ["a@partner.example, leak@elsewhere.example"]
            )

    def test_no_targets_raise(self, yaml_cfg: Path) -> None:
        with pytest.raises(MailOutboundDisallowedError):
            assert_forward_targets_allowed([])

    def test_unavailable_config_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(COMMS_CONFIG_ENV, _NO_YAML)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(OutboundAllowlistUnavailableError):
            assert_forward_targets_allowed(["a@partner.example"])
