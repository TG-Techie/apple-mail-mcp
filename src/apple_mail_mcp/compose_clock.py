"""Where tending keeps its clock between passes: when it first saw each
compose window, by Mail's window id and process, with the fingerprint of
the content the window had then (``compose_tending.SightingClock``).

A window whose fingerprint stays the same across passes for
``compose_tending.STALE_S`` is closed, whoever opened it. The file holds
hashes and times only, never a window's name or text. It lives next to
``compose_windows/`` under the data home, resolved when used, so env-var
overrides and test-time monkeypatching are honoured.

Only the daemon's tender writes it in normal running. A save replaces
the file whole, so a reader never sees half of one; two writers would
lose one pass's starts, which only makes a clock later, never earlier.
A file that cannot be read is an empty clock: every window starts again,
which delays a close and never hastens one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .compose_ledger import default_root
from .compose_tending import ClockEntry, SightingClock

logger = logging.getLogger(__name__)

_FILE_NAME = "compose_clock.json"


def default_path() -> Path:
    """``compose_clock.json`` beside the compose ledger, under the data
    home."""
    return default_root().parent / _FILE_NAME


def _clock_from_json(data: dict[str, Any]) -> SightingClock:
    pid = data["mail_pid"]
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool)):
        raise ValueError(f"mail_pid {pid!r}")
    windows: dict[int, ClockEntry] = {}
    for key, entry in data["windows"].items():
        window_id = int(key)
        if window_id <= 0:
            raise ValueError(f"window id {key!r}")
        windows[window_id] = ClockEntry(str(entry["fingerprint"]), float(entry["since"]))
    return SightingClock(mail_pid=pid, windows=windows)


class ComposeClock:
    """The clock file. ``path`` None means ``default_path()``, resolved
    on each use."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    def load(self) -> SightingClock:
        """The clock as last saved; empty when there is none, or when it
        cannot be read (logged)."""
        path = self.path
        if not path.is_file():
            return SightingClock(mail_pid=None, windows={})
        try:
            return _clock_from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("compose clock %s unreadable, starting afresh: %s", path, exc)
            return SightingClock(mail_pid=None, windows={})

    def save(self, clock: SightingClock) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "mail_pid": clock.mail_pid,
            "windows": {
                str(window_id): {"fingerprint": e.fingerprint, "since": e.since}
                for window_id, e in sorted(clock.windows.items())
            },
        }
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
