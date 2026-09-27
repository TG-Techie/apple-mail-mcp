"""Console entry ``mail-serve``: the resident daemon.

One process, supervised by pm2, serving this server's tools over HTTP on
loopback. Sessions reach it through ``mail-proxy`` and never talk to it
directly. The ``apple-mail-mcp`` stdio entry is unchanged and still serves
clients that run a server of their own, such as Claude Desktop.

Not a stdio server, and it has no stdio mode.

This module does not import ``server`` until ``main`` runs, so
``mail-proxy`` can take the port from here without building a Mail
connector of its own.

Concurrency, as read from the installed fastmcp 3.4.7 on 2026-09-26:

- A sync ``@mcp.tool`` function is registered with ``run_in_thread=True``
  (the default of ``FastMCP.tool``). ``without_injected_parameters`` in
  ``fastmcp/server/dependencies.py`` then calls it through
  ``call_sync_fn_in_threadpool`` in ``fastmcp/utilities/async_utils.py``,
  which is ``anyio.to_thread.run_sync``. One session's 60 s AppleScript
  call in such a tool holds a worker thread, not the event loop. anyio's
  default limiter admits 40 worker threads at once; later calls queue.
- An ``async def`` tool runs on the event loop, and fastmcp moves none
  of its body to a thread. So the tools whose work reaches Mail keep it
  off the loop themselves: delete_rule, update_rule, delete_mailbox and
  email_send_html are sync tools, and draft_create, draft_update and
  draft_send await their Mail work through ``server._in_tool_threadpool``.
  Asking the user to confirm is the one thing those bodies do on the
  loop (``server._confirm_from_threadpool``), and waiting for the answer
  is an ``await`` that holds no one up. tests/unit/test_tool_threadpool.py
  holds each of the seven to it.
- The Mail lock (``AppleMailConnector._acquire_mail_lock``) opens the lock
  file afresh on every call, and flock(2) refuses a second open of the
  file from another thread of the same process just as it does from
  another process (observed on this machine 2026-09-26). So the lock
  serializes the daemon's own concurrent calls, whichever thread makes
  them, and the daemon against any stdio server or test run. It depends
  on that fresh open: a second thread flocking the same handle is let
  straight through (also observed), so a handle kept and reused across
  calls would stop excluding the daemon's threads from each other.
"""

from __future__ import annotations

import argparse

DEFAULT_PORT = 41108
"""Provisional until the operator allocates it. imsg-serve holds 41100,
apw-serve 41102, reminders-serve 41104, calendar-serve 41106."""

HOST = "127.0.0.1"
"""Loopback only. The daemon has no authentication of its own; what
stands between it and the network is that it is not on the network."""

MCP_PATH = "/mcp"
"""Pinned rather than left to fastmcp's settings, because ``mail-proxy``
builds its URL from it."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-serve",
        description=(
            "The resident Apple Mail MCP daemon. Serves the tools over HTTP on "
            "loopback for mail-proxy to forward to. A session talks to the "
            "proxy, never to this."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"loopback port (default {DEFAULT_PORT}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    from .server import mcp

    mcp.run(transport="http", host=HOST, port=int(ns.port), path=MCP_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
