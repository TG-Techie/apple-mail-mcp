"""Console entry ``mail-proxy``: the thin per-session stdio server.

What a session's MCP client launches. It declares nothing of its own: no
tool list, no descriptions, no schemas, no configuration. Every listing
and every call is forwarded to the daemon (``mail-serve``) and answered
from the daemon's current state, because a second copy of a description
or a schema drifts from the first. Confirmation prompts travel the same
way: the daemon's elicitation reaches this session's client through
fastmcp's ``ProxyClient``, and the answer goes back to the daemon.

The one thing fastmcp's proxy does not carry is the daemon's
``instructions``, so they are fetched from the daemon when the proxy
starts and served as this proxy's own (docs/DISCIPLINE.md, "MCP context
exposure": a proxy's instructions are the daemon's, never a separate
hand-kept text).

When the daemon is not there (observed with fastmcp 3.4.7, 2026-09-26):

- A client that connects while the daemon is down fails its initialize
  handshake with "Client failed to connect", because fastmcp's proxy
  initializes the daemon inside the client's handshake. The daemon-down
  text of ``daemon_instructions`` therefore reaches a session only when
  the daemon came up between this process starting and its client
  connecting.
- If the daemon goes away mid-session, calls and listings fail naming
  the connection. Under fastmcp's default ``provider_error_strategy``,
  "warn", tools/list answered an empty list instead, and a call made
  once the proxy's 300 s tool cache had expired answered "Unknown tool":
  an outage reported as a fact about the tools. Hence "raise".
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy

from .serve import DEFAULT_PORT, HOST, MCP_PATH

URL_ENV = "APPLE_MAIL_SERVER_URL"
DEFAULT_URL = f"http://{HOST}:{DEFAULT_PORT}{MCP_PATH}"

NAME = "apple-mail"
"""The name agents already know this server by; a client shows it."""


def server_url() -> str:
    return os.environ.get(URL_ENV, DEFAULT_URL)


def daemon_instructions(target: str | FastMCP[Any]) -> str:
    """The daemon's own instructions, verbatim, to serve as this proxy's.

    Read once, when this session's proxy starts. If the daemon does not
    answer then, the text says so rather than standing in for them."""

    async def fetch() -> str | None:
        async with Client(target) as client:
            init = client.initialize_result
            return None if init is None else init.instructions

    try:
        got = asyncio.run(fetch())
    except Exception as exc:
        return (
            f"The apple-mail daemon (mail-serve) did not answer when this session "
            f"started ({type(exc).__name__}: {exc}), so its instructions are missing "
            "here. The tools work once it is up."
        )
    return got or (
        "The apple-mail daemon (mail-serve) returned no instructions when this session started."
    )


def build_proxy(url: str) -> FastMCPProxy:
    return create_proxy(
        StreamableHttpTransport(url),
        name=NAME,
        instructions=daemon_instructions(url),
        provider_error_strategy="raise",
    )


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="mail-proxy",
        description=(
            "Per-session stdio proxy for the Apple Mail MCP. Forwards every call "
            f"to the mail-serve daemon at {DEFAULT_URL}; set {URL_ENV} to point "
            "it elsewhere. Takes no arguments."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    build_proxy(server_url()).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
