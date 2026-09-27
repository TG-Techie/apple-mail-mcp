"""The fleet shape end to end: ``mail-serve`` as a real process, a proxy in front.

The daemon runs as a subprocess from its installed console script, on an
ephemeral loopback port (never the provisional 41108, which a live daemon
may hold). The proxy is built in-process by the same ``build_proxy`` that
``mail-proxy`` runs, and a fastmcp ``Client`` talks to it, so every call
crosses the real HTTP transport between proxy and daemon. One test also
launches the ``mail-proxy`` console script over real stdio, since that is
what a session starts.

Nothing here reaches Mail.app. The tools exercised are the template tools,
which read and write only the daemon's ``APPLE_MAIL_MCP_HOME``, set to a
temporary directory.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client

from apple_mail_mcp import proxy, serve, server

from .expected_tools import EXPECTED_TOOLS

pytestmark = pytest.mark.e2e

DAEMON_START_SECONDS = 30.0
CALL_SECONDS = 30.0
BIN = Path(sys.executable).parent


@dataclass(frozen=True)
class Daemon:
    url: str
    home: Path
    process: subprocess.Popen[bytes]


def _free_port() -> int:
    """A loopback port nothing holds right now. Another process could
    take it before the daemon binds; the daemon then exits and the
    fixture fails with its log, rather than a test passing wrongly."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    assert port != serve.DEFAULT_PORT
    return port


def _start_daemon(home: Path) -> Daemon:
    script = BIN / "mail-serve"
    assert script.is_file(), f"{script} is not installed; run `uv sync`"
    port = _free_port()
    log_path = home.parent / f"mail-serve-{port}.log"
    env = {
        **os.environ,
        "APPLE_MAIL_MCP_HOME": str(home),
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(script), "--port", str(port)], env=env, stdout=log, stderr=log
        )
    deadline = time.monotonic() + DAEMON_START_SECONDS
    while True:
        if process.poll() is not None:
            pytest.fail(f"mail-serve exited {process.returncode}:\n{log_path.read_text()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            if time.monotonic() > deadline:
                process.kill()
                pytest.fail(
                    f"mail-serve did not listen within {DAEMON_START_SECONDS}s:\n"
                    f"{log_path.read_text()}"
                )
            time.sleep(0.1)
            continue
        return Daemon(url=f"http://127.0.0.1:{port}/mcp", home=home, process=process)


def _stop(daemon: Daemon) -> None:
    daemon.process.terminate()
    try:
        daemon.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.process.kill()
        daemon.process.wait(timeout=10)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Daemon]:
    running = _start_daemon(tmp_path_factory.mktemp("daemon") / "home")
    yield running
    _stop(running)


@pytest.fixture
def own_daemon(tmp_path: Path) -> Iterator[Daemon]:
    """A daemon for one test, for the test that stops it."""
    running = _start_daemon(tmp_path / "daemon-home")
    yield running
    _stop(running)


def _client(built: Any, answer: Any, asked: list[str]) -> Client[Any]:
    """A client that answers every confirmation with ``answer`` and
    records each question it was asked."""

    async def handler(message: str, response_type: Any, params: Any, ctx: Any) -> Any:
        asked.append(message)
        return answer

    return Client(built, elicitation_handler=handler)


async def _build_proxy(daemon: Daemon) -> Any:
    # build_proxy fetches the daemon's instructions with asyncio.run, as
    # it does at a session's start; a worker thread has no running loop.
    return await anyio.to_thread.run_sync(proxy.build_proxy, daemon.url)


async def test_tool_list_through_the_proxy_is_the_daemons(daemon: Daemon) -> None:
    with anyio.fail_after(CALL_SECONDS):
        async with Client(await _build_proxy(daemon)) as client:
            names = {t.name for t in await client.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_instructions_the_client_sees_are_the_daemons(daemon: Daemon) -> None:
    with anyio.fail_after(CALL_SECONDS):
        async with Client(await _build_proxy(daemon)) as client:
            init = client.initialize_result
    assert server.mcp.instructions
    assert init is not None
    assert init.instructions == server.mcp.instructions


async def test_confirmation_is_asked_and_answered_through_the_proxy(daemon: Daemon) -> None:
    """The daemon's elicitation reaches the client behind the proxy, and
    the client's yes reaches the daemon: without both the gate answers
    confirmation_required or cancelled and the file stays."""
    template = daemon.home / "templates" / "through-proxy.md"
    asked: list[str] = []
    with anyio.fail_after(CALL_SECONDS):
        built = await _build_proxy(daemon)
        async with _client(built, True, asked) as client:
            saved = await client.call_tool(
                "save_template", {"name": "through-proxy", "body": "v1\n"}
            )
            assert saved.structured_content is not None
            assert saved.structured_content["success"] is True
            assert template.is_file()

            result = await client.call_tool("delete_template", {"name": "through-proxy"})

    assert len(asked) == 1
    assert "Delete email template 'through-proxy'?" in asked[0]
    assert result.structured_content == {"success": True, "name": "through-proxy"}
    assert not template.exists()


async def test_a_decline_through_the_proxy_keeps_the_template(daemon: Daemon) -> None:
    template = daemon.home / "templates" / "kept.md"
    asked: list[str] = []
    with anyio.fail_after(CALL_SECONDS):
        built = await _build_proxy(daemon)
        async with _client(built, ElicitResult(action="decline"), asked) as client:
            await client.call_tool("save_template", {"name": "kept", "body": "v1\n"})
            result = await client.call_tool("delete_template", {"name": "kept"})

    assert len(asked) == 1
    body = result.structured_content
    assert body is not None
    assert body["success"] is False
    assert body["error_type"] == "cancelled"
    assert template.is_file()


async def test_mail_proxy_console_script_over_stdio(daemon: Daemon) -> None:
    """What a session launches: the installed script, over real pipes.
    Catches what the in-process proxy cannot: anything written to stdout
    that is not JSON-RPC, and the script entry point itself."""
    script = BIN / "mail-proxy"
    assert script.is_file(), f"{script} is not installed; run `uv sync`"
    params = StdioServerParameters(
        command=str(script),
        args=[],
        env={"APPLE_MAIL_SERVER_URL": daemon.url, "FASTMCP_CHECK_FOR_UPDATES": "off"},
    )
    with anyio.fail_after(CALL_SECONDS):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
    assert init.serverInfo.name == "apple-mail"
    assert init.instructions == server.mcp.instructions
    assert {t.name for t in listed.tools} == EXPECTED_TOOLS


async def test_daemon_gone_mid_session_is_a_connection_failure(own_daemon: Daemon) -> None:
    """Not an empty tool list: fastmcp's default for a proxy would have
    answered one, reporting an outage as a fact about the tools."""
    with anyio.fail_after(CALL_SECONDS):
        async with Client(await _build_proxy(own_daemon)) as client:
            assert {t.name for t in await client.list_tools()} == EXPECTED_TOOLS
            await anyio.to_thread.run_sync(_stop, own_daemon)
            with pytest.raises(McpError, match="failed to connect"):
                await client.list_tools()
