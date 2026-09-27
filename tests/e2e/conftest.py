"""Shared fixtures for end-to-end tests."""

from pathlib import Path

import pytest

from apple_mail_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _isolated_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point APPLE_MAIL_MCP_HOME at a per-test directory so the audit log,
    draft state and templates a test produces never land in the real
    ~/.apple_mail_mcp. Tests that need a specific home set it themselves
    on top of this."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Each test starts with the rate limiter empty, as the unit tests
    do. The limiter is process-wide, so the sends of earlier tests count
    against a later one: the sends tier allows 3 in 60 s, and a fourth
    send test in the run was refused as rate_limited."""
    rate_limiter.reset()
