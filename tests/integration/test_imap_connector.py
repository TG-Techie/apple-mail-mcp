"""Integration tests for ImapConnector against a real IMAP server.

Guarded by ``MAIL_TEST_MODE=true``. The account comes from
``MAIL_TEST_ACCOUNT``; its host, port and login are read from Mail.app.
The app password comes from a Keychain entry keyed to that login::

    security add-generic-password \\
        -s "apple-mail-mcp.imap.<account name>" \\
        -a "<login Mail.app uses for the account>" \\
        -w "<APP_PASSWORD>" -T "" -U

Run::

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account name> \\
        uv run pytest tests/integration/test_imap_connector.py -v
"""

from __future__ import annotations

import os

import pytest

from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.imap_connector import ImapConnector
from apple_mail_mcp.keychain import get_imap_password
from apple_mail_mcp.mail_connector import AppleMailConnector


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestEndToEnd:
    def test_end_to_end_search_returns_list(self, test_account: str):
        host, port, email = AppleMailConnector()._resolve_imap_config(test_account)
        password = get_imap_password(test_account, email)
        connector = ImapConnector(host, port, email, password)
        result = connector.search_messages(limit=5)
        assert isinstance(result, list)
        # May be empty (per PR #70 spike finding — merged-away Apple ID's
        # residual mailbox). Any non-empty result must have the standard
        # keys matching mail_connector.search_messages output shape.
        expected_keys = {
            "id",
            "subject",
            "sender",
            "date_received",
            "read_status",
            "flagged",
        }
        for msg in result:
            assert set(msg.keys()) == expected_keys
            assert isinstance(msg["read_status"], bool)
            assert isinstance(msg["flagged"], bool)

    def test_keychain_entry_missing_raises_entry_not_found(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("DoesNotExistAccount", "nobody@example.com")
