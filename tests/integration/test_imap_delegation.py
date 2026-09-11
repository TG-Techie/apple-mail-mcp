"""Integration tests for search_messages IMAP delegation against a real account.

Guarded by ``MAIL_TEST_MODE=true``. The account comes from
``MAIL_TEST_ACCOUNT``. The positive tests require a Keychain entry keyed
to the login Mail.app returns for `user name` (for iCloud that is the
Apple ID email, not an @icloud.com alias)::

    security add-generic-password \\
        -s "apple-mail-mcp.imap.<account name>" \\
        -a "<login Mail.app uses for the account>" \\
        -w "<APP_PASSWORD>" \\
        -T "" -U

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account name> \\
        uv run pytest tests/integration/test_imap_delegation.py -v
"""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.utils import escape_applescript_string


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


def _keychain_entry_exists(account_name: str, email: str) -> bool:
    service = f"apple-mail-mcp.imap.{account_name}"
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", email],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _any_message_id(account: str) -> str | None:
    """Return the id of some message in ``account``, or None if it has none.

    Resolved through AppleScript directly so that anchor discovery never
    touches the IMAP delegation path these tests exercise. Walks the
    account's mailboxes rather than naming one: which mailbox has mail is
    a property of the account, not of the test.
    """
    account_literal = escape_applescript_string(account)
    finder = subprocess.run(
        [
            "/usr/bin/osascript", "-e",
            'tell application "Mail"\n'
            f'  repeat with mb in mailboxes of account "{account_literal}"\n'
            '    if (count of messages of mb) > 0 then return id of first message of mb\n'
            '  end repeat\n'
            '  return ""\n'
            'end tell',
        ],
        capture_output=True, text=True, check=False,
    )
    if finder.returncode != 0:
        return None
    return finder.stdout.strip() or None


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector()


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestIMAPDelegation:
    def test_search_messages_uses_imap_when_keychain_entry_present(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        test_account: str,
    ) -> None:
        """Positive path: real account, Keychain entry present, search goes via IMAP.

        Skipped if the user hasn't set up the Apple-ID-keyed Keychain entry
        — this test requires an entry keyed to Mail.app's 'user name'
        property (the Apple ID), not an alias.
        """
        host, port, email = connector._resolve_imap_config(test_account)
        if not _keychain_entry_exists(test_account, email):
            pytest.skip(
                f"No Keychain entry under "
                f"apple-mail-mcp.imap.{test_account} for {email}. "
                f"See the test file's module docstring for setup."
            )

        # If IMAP succeeds, _imap_failures stays empty. If anything falls back,
        # the set will contain the account. We assert the IMAP path executed.
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.search_messages(
                account=test_account, limit=5
            )

        assert isinstance(result, list)
        # Search may be empty (the account's inbox may be), but the
        # IMAP path must have been used — which means the failures set is empty
        # AND we did not emit any WARNING about falling back.
        assert test_account not in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"Expected IMAP path to succeed silently, but got warnings: "
            f"{[r.getMessage() for r in warnings]}"
        )

        # Any messages returned must have the standard keys. (The server may
        # legitimately return [] — that's a successful IMAP search.)
        expected_keys = {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged",
        }
        for msg in result:
            assert set(msg.keys()) == expected_keys

    def test_search_messages_falls_back_when_imap_host_unroutable(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        test_account: str,
    ) -> None:
        """Negative path: IMAP connect times out, AppleScript path runs.

        Monkey-patches _resolve_imap_config to return an unroutable host
        (10.255.255.1:993 — TEST-NET-1-adjacent; guaranteed to not route).
        Also stubs get_imap_password so the Keychain lookup doesn't short-
        circuit the test with a benign MailKeychainEntryNotFoundError —
        we want the 3s IMAP connect timeout to fire, OSError to propagate,
        and the first-failure WARNING path to execute.
        """
        def fake_config(_account: str) -> tuple[str, int, str]:
            return ("10.255.255.1", 993, "fake@example.com")

        monkeypatch.setattr(connector, "_resolve_imap_config", fake_config)
        monkeypatch.setattr(
            "apple_mail_mcp.mail_connector.get_imap_password",
            lambda _account, _email: "fake-password",
        )

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.search_messages(
                account=test_account, limit=5
            )

        # AppleScript path succeeded despite IMAP failure. The account may
        # have an empty inbox; the key assertion is that we got a list back at all
        # (i.e. search_messages didn't raise).
        assert isinstance(result, list)

        # The failures set must contain the account.
        assert test_account in connector._imap_failures

        # First failure should log at WARNING.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly one WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}"
        )
        msg = warnings[0].getMessage()
        assert test_account in msg

    def test_get_thread_uses_imap_when_keychain_entry_present(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        test_account: str,
    ) -> None:
        """Positive path: real account, IMAP path resolves a thread.

        Discovers a real message ID at runtime via Mail.app, from whichever
        mailbox of the account has one. Skips if no messages anywhere.
        """
        host, port, email = connector._resolve_imap_config(test_account)
        if not _keychain_entry_exists(test_account, email):
            pytest.skip(
                f"No Keychain entry under "
                f"apple-mail-mcp.imap.{test_account} for {email}."
            )

        anchor_id = _any_message_id(test_account)
        if anchor_id is None:
            pytest.skip(f"No anchor message available in account {test_account!r}")

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.get_thread(message_id=anchor_id)

        assert isinstance(result, list)
        # IMAP path must have succeeded silently — no fallback WARNING for
        # this account.
        assert test_account not in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"Expected IMAP path to succeed silently, but got warnings: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_get_thread_falls_back_when_imap_host_unroutable(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        test_account: str,
    ) -> None:
        """Negative path for get_thread: IMAP fails, AppleScript path runs.

        Same monkey-patch trick as the search_messages negative test —
        force the IMAP connect to fail by pointing at an unroutable host,
        and stub the Keychain lookup so we don't short-circuit on the
        benign not-found path.
        """
        # Find an anchor first (anchor resolution stays AppleScript).
        anchor_id = _any_message_id(test_account)
        if anchor_id is None:
            pytest.skip(f"No anchor message available in account {test_account!r}")

        def fake_config(_account: str) -> tuple[str, int, str]:
            return ("10.255.255.1", 993, "fake@example.com")

        monkeypatch.setattr(connector, "_resolve_imap_config", fake_config)
        monkeypatch.setattr(
            "apple_mail_mcp.mail_connector.get_imap_password",
            lambda _account, _email: "fake-password",
        )

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.get_thread(message_id=anchor_id)

        # AppleScript fallback ran and returned the thread (at least the
        # anchor itself).
        assert isinstance(result, list)
        assert len(result) >= 1
        assert test_account in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert test_account in warnings[0].getMessage()
