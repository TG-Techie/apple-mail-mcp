"""Shared fixtures for unit tests."""

import pytest

from apple_mail_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state between tests to prevent cross-contamination."""
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _allowlist_test_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Add RFC 2606 reserved test-domain patterns to the outbound
    allowlist env var for the duration of every unit test. Lets existing
    fixtures use ``test@example.com``-style recipients without tripping
    the connector-layer policy gate (see outbound_allowlist.py). Does NOT
    enable MAIL_TEST_MODE — that's a separate concern and toggling it
    here would activate the test-mode-safety gate in security.py.
    """
    monkeypatch.setenv(
        "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST",
        "*@example.com,*@example.net,*@example.org,"
        "*@*.example,*@*.test,*@*.invalid,*@*.localhost",
    )
