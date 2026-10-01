"""The cross-process Mail automation lock.

Several callers can drive Mail at once (the resident daemon on behalf of
many sessions, any stdio server beside it, a test run); without
serialization, concurrent AppleScript against Mail.app collides into
AppleEvent timeouts (-1712) and invalid connections (-609) that surface
as inscrutable failures for the other caller. A flock(2) on
``mail_automation.lock`` under the data home (``APPLE_MAIL_MCP_HOME``,
default ``~/.apple_mail_mcp``) queues them instead.

Every acquisition opens the file afresh. flock refuses a second open of
the file from another thread of the same process just as it does from
another process, but lets a second thread flocking the same handle
straight through (both observed on macOS, 2026-09-26). So a handle kept
and reused across calls would stop excluding the daemon's own threads
from each other.

Two holders take it: ``AppleMailConnector._run_applescript``, for each
osascript it runs, and the daemon's Mail restarter (``restarter.py``),
for a probe and for the whole of a restart.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

LOCK_FILENAME = "mail_automation.lock"

_POLL_S = 0.25


def lock_path() -> Path:
    """Where the lock file is, resolved at call time so an env-var
    override or a test's data home is honoured."""
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = Path(home_override).expanduser() if home_override else Path.home() / ".apple_mail_mcp"
    return base / LOCK_FILENAME


def acquire(wait_s: float) -> IO[str] | None:
    """Take the lock, waiting up to ``wait_s`` for it. Returns the open
    handle that holds it, for ``release``; None, holding nothing, when it
    was not free in time."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")  # noqa: SIM115 — held past scope, closed by release()
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(_POLL_S)


def release(fh: IO[str]) -> None:
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


@contextmanager
def held(wait_s: float) -> Iterator[bool]:
    """Hold the lock for the block. Yields False, holding nothing, when
    it was not free within ``wait_s``."""
    fh = acquire(wait_s)
    if fh is None:
        yield False
        return
    try:
        yield True
    finally:
        release(fh)
