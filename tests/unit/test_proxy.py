"""Argument handling and instruction forwarding for ``mail-proxy``.

The proxy's only content of its own is where the daemon is and what to
say when the daemon does not answer; these pin both. Forwarding through
a real daemon is tests/e2e/test_daemon_proxy.py's job.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.server.providers.proxy import FastMCPProxy

from apple_mail_mcp import proxy


def _closed_port_url() -> str:
    """A loopback URL nothing is listening on, found by binding port 0
    and letting it go. Another process could take the port in between;
    that would make the test fail, not pass wrongly."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


class TestServerUrl:
    def test_default_is_the_daemon_on_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APPLE_MAIL_SERVER_URL", raising=False)
        assert proxy.server_url() == "http://127.0.0.1:41108/mcp"

    def test_environment_overrides_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLE_MAIL_SERVER_URL", "http://127.0.0.1:5555/mcp")
        assert proxy.server_url() == "http://127.0.0.1:5555/mcp"


class TestArguments:
    def test_takes_no_arguments(self) -> None:
        """A flag copied from a sibling's registration (the calendar proxy
        takes a grant) fails loudly instead of being ignored."""
        with pytest.raises(SystemExit) as exc:
            proxy.main(["--allow-all"])
        assert exc.value.code == 2

    def test_main_runs_a_stdio_proxy_named_apple_mail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLE_MAIL_SERVER_URL", "http://127.0.0.1:5555/mcp")
        fetched_from: list[Any] = []

        def instructions(target: Any) -> str:
            fetched_from.append(target)
            return "the daemon's words"

        runs: list[tuple[FastMCPProxy, dict[str, Any]]] = []

        def record_run(self: FastMCPProxy, *args: Any, **kwargs: Any) -> None:
            runs.append((self, {"args": args, **kwargs}))

        monkeypatch.setattr(proxy, "daemon_instructions", instructions)
        monkeypatch.setattr(FastMCPProxy, "run", record_run)

        assert proxy.main([]) == 0

        assert fetched_from == ["http://127.0.0.1:5555/mcp"]
        [(server, kwargs)] = runs
        assert server.name == "apple-mail"
        assert server.instructions == "the daemon's words"
        assert kwargs == {"args": (), "transport": "stdio"}


class TestDaemonInstructions:
    def test_returns_the_daemons_own_text_verbatim(self) -> None:
        daemon = FastMCP("daemon", instructions="Prefer email_send_html.\nSecond line.")
        assert proxy.daemon_instructions(daemon) == "Prefer email_send_html.\nSecond line."

    def test_says_so_when_the_daemon_has_none(self) -> None:
        assert proxy.daemon_instructions(FastMCP("daemon")) == (
            "The apple-mail daemon (mail-serve) returned no instructions when this session started."
        )

    def test_says_so_when_the_daemon_does_not_answer(self) -> None:
        text = proxy.daemon_instructions(_closed_port_url())
        assert text.startswith(
            "The apple-mail daemon (mail-serve) did not answer when this session started ("
        )
        assert text.endswith(
            "), so its instructions are missing here. The tools work once it is up."
        )


def test_building_the_proxy_imports_no_server_and_warns_nothing() -> None:
    """The proxy holds no configuration and touches nothing. Importing
    ``server`` would construct the Mail connector and configure logging,
    so the proxy must not import it, directly or through ``serve``. And
    it uses fastmcp's current proxy API: ``fastmcp.server.proxy`` is the
    deprecated path on 3.4.7, and importing it warns. Checked in a fresh
    interpreter, with deprecation warnings as errors, because this test
    process has imported ``server`` already."""
    probe = (
        "import sys; from apple_mail_mcp import proxy; "
        f"proxy.build_proxy({_closed_port_url()!r}); "
        "print('apple_mail_mcp.server' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", probe],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
