"""The record of every compose window the connector opens, and how each
ended.

Mail says nothing about who opened a compose window or when: a restored
window carries no date and no origin, and one opened by a person looks
exactly like one this connector opened
(docs/research/compose-window-tending.md, Observations 2 and 3). A window
the connector opened and did not close — a composition whose salvage
failed, a process that died mid-composition — stays open until something
closes it. This ledger is how tending (``AppleMailConnector
.tend_compose_windows``) tells the connector's own windows from anyone
else's.

A window is identified by Mail's own window ``id`` within one Mail
process (``mail_pid``): names repeat (eighteen "New Message" windows at
once, 2026-09-27), and a relaunched Mail gives the windows it restores
new ids. The name is kept as ``_as_new_compose_window_block`` found it,
since every step addresses the window by name.

A record's life, one file per window at ``<root>/<record_id>.json``::

    open ──► left_open(failure) ──► closed(…, by tending)
      └────► closed(sent | saved <draft id> | salvaged | discarded | gone,
                    by the composition or by tending)

``left_open`` is how a composition ends when it could not close its
window; only tending closes such a window after. ``closed`` is final, and
a window is closed exactly once. Tending never sends or saves: ``sent``
and ``saved`` are the composition's own endings, and a window tending
salvages is ``salvaged``, whose draft id nothing reads back.

The window name is the subject of the mail being composed, so the store
is personal data at rest, like ``audit.jsonl``: it lives under the data
home, outside the repository.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

from .exceptions import MailComposeLedgerError

WindowOperation = Literal["send", "save"]
Seed = Literal["new", "reply", "forward"]
CloseHow = Literal["sent", "saved", "salvaged", "discarded", "gone"]
Closer = Literal["composition", "tending"]

_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Mail's draft ids, as drafts.py takes them.
_DRAFT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_EXT = ".json"
_LOCK_NAME = ".lock"


def default_root() -> Path:
    """``compose_windows/`` under the data home (``APPLE_MAIL_MCP_HOME``,
    default ``~/.apple_mail_mcp``), resolved when called so env-var
    overrides and test-time monkeypatching are honoured."""
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = (
        Path(home_override).expanduser()
        if home_override
        else Path.home() / ".apple_mail_mcp"
    )
    return base / "compose_windows"


@dataclass(frozen=True)
class Open:
    """The window is open and its composition has not ended: in flight,
    or its process died before it could say how the window ended."""


@dataclass(frozen=True)
class LeftOpen:
    """The composition ended without closing its window; ``failure`` says
    why, as the caller was told."""

    failure: str
    at: float


@dataclass(frozen=True)
class Closed:
    """The window is gone, and how. ``saved`` names the draft it became;
    nothing else does. Only a composition sends or saves."""

    how: CloseHow
    by: Closer
    at: float
    draft_id: str | None = None

    def __post_init__(self) -> None:
        if self.how not in get_args(CloseHow):
            raise ValueError(f"unknown window ending {self.how!r}")
        if self.by not in get_args(Closer):
            raise ValueError(f"unknown closer {self.by!r}")
        if (self.how == "saved") != (self.draft_id is not None):
            raise ValueError("a saved window names its draft, and only a saved one")
        if self.draft_id is not None and not _DRAFT_ID_RE.match(self.draft_id):
            raise ValueError(f"draft id {self.draft_id!r} is not one")
        if self.how in ("sent", "saved") and self.by != "composition":
            raise ValueError("only a composition sends or saves its window")


WindowState = Open | LeftOpen | Closed


@dataclass(frozen=True)
class WindowRecord:
    """One compose window the connector opened."""

    record_id: str
    opened_at: float
    operation: WindowOperation
    seed: Seed
    window_name: str
    window_id: int | None
    mail_pid: int | None
    state: WindowState

    def __post_init__(self) -> None:
        _validate_record_id(self.record_id)
        if self.operation not in get_args(WindowOperation):
            raise ValueError(f"unknown operation {self.operation!r}")
        if self.seed not in get_args(Seed):
            raise ValueError(f"unknown seed {self.seed!r}")
        for name, value in (("window_id", self.window_id), ("mail_pid", self.mail_pid)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    @property
    def unfinished(self) -> bool:
        """The window may still be open: nothing has closed it."""
        return not isinstance(self.state, Closed)


@dataclass(frozen=True)
class LedgerContents:
    """Every record that could be read, and the file names of those that
    could not."""

    records: tuple[WindowRecord, ...]
    unreadable: tuple[str, ...]


def _validate_record_id(record_id: str) -> None:
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        raise MailComposeLedgerError(
            f"record id {record_id!r} must match {_RECORD_ID_RE.pattern}"
        )


def _may_follow(old: WindowState, new: WindowState) -> bool:
    if isinstance(old, Open):
        return isinstance(new, (LeftOpen, Closed))
    if isinstance(old, LeftOpen):
        return isinstance(new, Closed) and new.by == "tending"
    return False


def _state_to_json(state: WindowState) -> dict[str, Any]:
    if isinstance(state, Open):
        return {"kind": "open"}
    if isinstance(state, LeftOpen):
        return {"kind": "left_open", "failure": state.failure, "at": state.at}
    out: dict[str, Any] = {"kind": "closed", "how": state.how, "by": state.by, "at": state.at}
    if state.draft_id is not None:
        out["draft_id"] = state.draft_id
    return out


def _state_from_json(data: dict[str, Any]) -> WindowState:
    kind = data.get("kind")
    if kind == "open":
        return Open()
    if kind == "left_open":
        return LeftOpen(failure=str(data["failure"]), at=float(data["at"]))
    if kind == "closed":
        return Closed(
            how=data["how"], by=data["by"], at=float(data["at"]),
            draft_id=data.get("draft_id"),
        )
    raise ValueError(f"unknown state kind {kind!r}")


def _record_to_json(record: WindowRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "opened_at": record.opened_at,
        "operation": record.operation,
        "seed": record.seed,
        "window_name": record.window_name,
        "window_id": record.window_id,
        "mail_pid": record.mail_pid,
        "state": _state_to_json(record.state),
    }


def _record_from_json(data: dict[str, Any]) -> WindowRecord:
    return WindowRecord(
        record_id=str(data["record_id"]),
        opened_at=float(data["opened_at"]),
        operation=cast(WindowOperation, data["operation"]),
        seed=cast(Seed, data["seed"]),
        window_name=str(data["window_name"]),
        window_id=data["window_id"],
        mail_pid=data["mail_pid"],
        state=_state_from_json(data["state"]),
    )


class ComposeLedger:
    """File-backed store of ``WindowRecord``s, shared by every process
    that uses the same data home: the daemon, a stdio server, a test run.

    A record is written whole and replaced atomically; a transition reads
    and rewrites it under a lock on the store, so two processes cannot
    both end the same window.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_root()

    def _path_for(self, record_id: str) -> Path:
        _validate_record_id(record_id)
        return self.root / f"{record_id}{_EXT}"

    @contextmanager
    def _locked(self) -> Iterator[Path]:
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        with open(root / _LOCK_NAME, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield root
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _write(self, record: WindowRecord) -> None:
        path = self._path_for(record.record_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(_record_to_json(record)), encoding="utf-8")
        tmp.replace(path)

    def _read(self, path: Path) -> WindowRecord:
        return _record_from_json(json.loads(path.read_text(encoding="utf-8")))

    def open(
        self,
        *,
        window_name: str,
        window_id: int | None,
        mail_pid: int | None,
        operation: WindowOperation,
        seed: Seed,
        now: float | None = None,
    ) -> WindowRecord:
        """Record a window the connector has just opened."""
        record = WindowRecord(
            record_id=uuid.uuid4().hex,
            opened_at=time.time() if now is None else now,
            operation=operation,
            seed=seed,
            window_name=window_name,
            window_id=window_id,
            mail_pid=mail_pid,
            state=Open(),
        )
        with self._locked():
            self._write(record)
        return record

    def get(self, record_id: str) -> WindowRecord | None:
        path = self._path_for(record_id)
        if not path.is_file():
            return None
        return self._read(path)

    def end(self, record_id: str, state: LeftOpen | Closed) -> WindowRecord:
        """Move a record to ``state``. ``MailComposeLedgerError`` when
        there is no such record, or its state does not lead there: a
        closed window is not closed again, and a window left open is
        closed by tending alone."""
        path = self._path_for(record_id)
        with self._locked():
            if not path.is_file():
                raise MailComposeLedgerError(f"no compose-window record {record_id}")
            current = self._read(path)
            if not _may_follow(current.state, state):
                raise MailComposeLedgerError(
                    f"compose-window record {record_id} is "
                    f"{_state_to_json(current.state)['kind']}; it cannot become "
                    f"{_state_to_json(state)}"
                )
            updated = replace(current, state=state)
            self._write(updated)
        return updated

    def read_all(self) -> LedgerContents:
        """Every record, and the names of the files that are not one."""
        root = self.root
        if not root.is_dir():
            return LedgerContents(records=(), unreadable=())
        records: list[WindowRecord] = []
        unreadable: list[str] = []
        for path in sorted(root.glob(f"*{_EXT}")):
            if path.name.startswith("."):
                continue
            try:
                record = self._read(path)
            except (OSError, ValueError, KeyError, TypeError, MailComposeLedgerError):
                unreadable.append(path.name)
                continue
            if f"{record.record_id}{_EXT}" != path.name:
                unreadable.append(path.name)
                continue
            records.append(record)
        return LedgerContents(records=tuple(records), unreadable=tuple(unreadable))

    def prune(self, *, before: float) -> int:
        """Delete the records of windows closed before ``before``; a
        record whose window may still be open is kept whatever its age.
        Returns how many went."""
        pruned = 0
        with self._locked():
            for record in self.read_all().records:
                if isinstance(record.state, Closed) and record.state.at < before:
                    self._path_for(record.record_id).unlink(missing_ok=True)
                    pruned += 1
        return pruned
