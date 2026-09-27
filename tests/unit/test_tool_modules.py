"""Every tool registers, whichever tool module a process imports first.

``server`` imports the tool modules at its end and each tool module
imports ``server``, so the first module imported is still half-loaded
while the rest load. A module that took another's helper by name at
import time would fail only when that other module happened to come
first, and only in a fresh process: inside one test session everything
is already imported. So each case is its own interpreter.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ..e2e.expected_tools import EXPECTED_TOOLS

TOOL_MODULES = [
    "apple_mail_mcp.tools.accounts_rules",
    "apple_mail_mcp.tools.mailboxes",
    "apple_mail_mcp.tools.messages",
    "apple_mail_mcp.tools.templates",
    "apple_mail_mcp.tools.drafts",
    "apple_mail_mcp.tools.send",
]

_LIST_AFTER_IMPORTING = """
import asyncio, importlib, sys
importlib.import_module(sys.argv[1])
from apple_mail_mcp import server
for tool in asyncio.run(server.mcp.list_tools()):
    print(tool.name)
"""


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_every_tool_registers_whichever_module_comes_first(module: str) -> None:
    run = subprocess.run(
        [sys.executable, "-c", _LIST_AFTER_IMPORTING, module],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert set(run.stdout.split()) == EXPECTED_TOOLS
