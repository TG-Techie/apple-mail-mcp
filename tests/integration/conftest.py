"""Fixtures shared by the integration tests.

Nothing in this directory names an account, a login or a host. Every
test that touches a real account gets it from ``MAIL_TEST_ACCOUNT`` —
the same variable the server's safety gate checks — and everything
about that account (IMAP host, port, login) is read from Mail.app at
run time. A literal in a fixture is a second configuration path that
nobody maintains, and a default is a literal that only applies when
you forgot to set the real one.
"""

import os

import pytest


@pytest.fixture
def test_account() -> str:
    """The Mail.app account name the integration tests run against."""
    account = os.getenv("MAIL_TEST_ACCOUNT")
    if not account:
        pytest.fail(
            "MAIL_TEST_ACCOUNT is not set. Integration tests run against "
            "exactly the account it names; there is no default."
        )
    return account
