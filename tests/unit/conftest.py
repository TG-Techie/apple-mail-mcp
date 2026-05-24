"""Shared fixtures for unit tests."""

from pathlib import Path

import pytest

from apple_mail_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state between tests to prevent cross-contamination."""
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _allowlist_test_domains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Write a comms.yaml with RFC 2606 reserved test-domain patterns and
    point APPLE_MAIL_MCP_COMMS_CONFIG at it for the duration of every unit
    test. Lets fixtures use ``test@example.com``-style recipients without
    tripping the policy gate. Does NOT enable MAIL_TEST_MODE — that's a
    separate concern.
    """
    cfg = tmp_path / "comms.yaml"
    cfg.write_text(
        "email_outbound:\n"
        "  - '*@example.com'\n"
        "  - '*@example.net'\n"
        "  - '*@example.org'\n"
        "  - '*@*.example'\n"
        "  - '*@*.test'\n"
        "  - '*@*.invalid'\n"
        "  - '*@*.localhost'\n"
    )
    monkeypatch.setenv("APPLE_MAIL_MCP_COMMS_CONFIG", str(cfg))
