"""Tests for the centralized outbound recipient allowlist (the policy
enforcement perimeter for outbound mail).

NOTE: the autouse `_allowlist_test_domains` fixture in conftest.py adds
RFC 2606 test-domain patterns to APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST
for every test. These tests use ``monkeypatch.delenv`` where they need to
exercise the hardcoded-defaults-only path.
"""

import pytest

from apple_mail_mcp.exceptions import MailOutboundDisallowedError
from apple_mail_mcp.outbound_allowlist import (
    SEND_ELICITATION_ALLOWLIST_ENV,
    USER_EXPLICIT_OUTBOUND_ALLOW_LIST,
    all_recipients_allowed,
    allowlist_patterns,
    assert_recipients_allowed_for_send,
    disallowed_recipients,
    extract_email,
)


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
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_env_var_additive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            SEND_ELICITATION_ALLOWLIST_ENV, "extra@here.com,*@allowed.io"
        )
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns  # hardcoded default still present
        assert "extra@here.com" in patterns
        assert "*@allowed.io" in patterns

    def test_env_var_cannot_remove_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if the env var is set to a single odd pattern, the
        hardcoded default still appears."""
        monkeypatch.setenv(SEND_ELICITATION_ALLOWLIST_ENV, "nothing@here")
        patterns = allowlist_patterns()
        assert "*@tg-techie.com" in patterns

    def test_uses_hardcoded_constant(self) -> None:
        """The exposed constant is the source of truth."""
        assert USER_EXPLICIT_OUTBOUND_ALLOW_LIST == ("*@tg-techie.com",)


class TestAllRecipientsAllowed:
    def test_all_on_hardcoded_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert all_recipients_allowed(
            ["jonah@tg-techie.com", "Other <other@tg-techie.com>"]
        )

    def test_mixed_list_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(
            ["jonah@tg-techie.com", "outsider@other.com"]
        )

    def test_empty_list_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        assert not all_recipients_allowed([])

    def test_display_name_format_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert all_recipients_allowed(["Jonah Y-M <jonah@tg-techie.com>"])

    def test_test_mode_allows_reserved_domains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        assert all_recipients_allowed(["test@example.com"])
        assert all_recipients_allowed(["foo@something.test"])

    def test_test_mode_off_blocks_reserved_domains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        assert not all_recipients_allowed(["test@example.com"])


class TestDisallowedRecipients:
    def test_returns_only_off_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
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
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        # Should not raise.
        assert_recipients_allowed_for_send(
            to=["jonah@tg-techie.com"], cc=None, bcc=None
        )

    def test_raises_on_off_list_recipient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
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
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
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
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        # Should not raise — @example.com is reserved.
        assert_recipients_allowed_for_send(
            to=["test@example.com"], cc=None, bcc=None
        )

    def test_display_name_format_extracted_for_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        # Should not raise — extraction finds the on-list address.
        assert_recipients_allowed_for_send(
            to=["Jonah Y-M <jonah@tg-techie.com>"], cc=None, bcc=None
        )

    def test_display_name_cannot_smuggle_offlist_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A display-name string that LOOKS like an on-list pattern but
        actually wraps an off-list address must still be blocked."""
        monkeypatch.delenv(SEND_ELICITATION_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        with pytest.raises(MailOutboundDisallowedError):
            assert_recipients_allowed_for_send(
                to=["jonah@tg-techie.com <evil@other.com>"],
                cc=None,
                bcc=None,
            )
