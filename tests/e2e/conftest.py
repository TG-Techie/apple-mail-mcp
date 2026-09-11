"""Shared fixtures for end-to-end tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point APPLE_MAIL_MCP_HOME at a per-test directory so the audit log,
    draft state and templates a test produces never land in the real
    ~/.apple_mail_mcp. Tests that need a specific home set it themselves
    on top of this."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "home"))
