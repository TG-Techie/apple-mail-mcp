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
  of its body to a thread. So every tool is a sync function. The ones
  that ask the user to confirm (delete_rule, update_rule, delete_mailbox,
  delete_template, draft_send, email_send_html) do that one thing on the
  loop (``server._confirm_from_threadpool``), and waiting for the answer
  is an ``await`` that holds no one up; ``server._in_tool_threadpool``
  makes their module-level names awaitable for callers in this process.
  tests/unit/test_tool_threadpool.py holds the tools whose work reaches
  Mail to it.
- The Mail lock (``mail_lock.py``) opens the lock
  file afresh on every call, and flock(2) refuses a second open of the
  file from another thread of the same process just as it does from
  another process (observed on this machine 2026-09-26). So the lock
  serializes the daemon's own concurrent calls, whichever thread makes
  them, and the daemon against any stdio server or test run. It depends
  on that fresh open: a second thread flocking the same handle is let
  straight through (also observed), so a handle kept and reused across
  calls would stop excluding the daemon's threads from each other.

The daemon also tends Mail's compose windows (``tender.py``): a pass at
start, one every ``--tend-interval`` seconds, and one soon after a
composition leaves its window open. ``--tend-interval 0`` turns it off,
for a daemon that must not reach Mail (the e2e tests run one on a
temporary data home, whose Mail lock no other process holds).

And it keeps Mail answering (``restarter.py``): a probe every
``--mail-probe-interval`` seconds, a quit and relaunch of Mail when it
has left several probes in a row unanswered, and one daily at
``--mail-restart-hour``. ``--mail-probe-interval 0`` turns all of that
off, for the same daemons. Only the daemon does either: many stdio
servers can run at once, one per session.
"""

from __future__ import annotations

import argparse

from .restarter import PROBE_INTERVAL_S, RESTART_HOUR, start_restarter
from .tender import TEND_INTERVAL_S, start_tender

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
    parser.add_argument(
        "--tend-interval",
        type=_seconds,
        default=float(TEND_INTERVAL_S),
        metavar="SECONDS",
        help=(
            "seconds between passes over Mail's compose windows, which close "
            "the ones this server opened and left (default "
            f"{TEND_INTERVAL_S}); 0 turns tending off."
        ),
    )
    parser.add_argument(
        "--mail-probe-interval",
        type=_seconds,
        default=float(PROBE_INTERVAL_S),
        metavar="SECONDS",
        help=(
            "seconds between probes of Mail; Mail is quit and relaunched when it "
            "stops answering them, and once a day (default "
            f"{PROBE_INTERVAL_S}); 0 turns probing and both restarts off."
        ),
    )
    parser.add_argument(
        "--mail-restart-hour",
        type=_hour,
        default=RESTART_HOUR,
        metavar="HOUR",
        help=(
            "local hour, 0 to 23, of the daily restart of Mail (default "
            f"{RESTART_HOUR})."
        ),
    )
    return parser


def _seconds(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def _hour(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole hour, got {text}") from None
    if not 0 <= value <= 23:
        raise argparse.ArgumentTypeError(f"must be 0 to 23, got {text}")
    return value


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    from .server import mcp

    if ns.tend_interval > 0:
        start_tender(interval_s=ns.tend_interval)
    if ns.mail_probe_interval > 0:
        start_restarter(interval_s=ns.mail_probe_interval, restart_hour=ns.mail_restart_hour)
    mcp.run(transport="http", host=HOST, port=int(ns.port), path=MCP_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
