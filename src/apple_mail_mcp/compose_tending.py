"""What a tending pass does with Mail's compose windows: the decision,
from an inventory of the windows System Events lists, the ledger of
windows the connector opened (``compose_ledger``), and the clock of when
tending first saw each window with the content it has now. Pure; the
connector takes the inventory and carries the decision out
(``AppleMailConnector.tend_compose_windows``), and ``compose_clock``
keeps the clock between passes.

The rule, and the measurement it rests on, is in
docs/research/compose-window-tending.md. In short:

- Every window is addressed by Mail's own window ``id``, matched to the
  window System Events lists by its name and position (Mail's
  ``bounds``). Names are not identity: eighteen windows shared one.
- **Abandoned**: a window a ledger record names (same Mail process,
  same id, the name the record holds) is closed as soon as its
  composition cannot still be running: a record the composition left
  open at once, an open one after ``TEND_GRACE_S``.
- **Stale**: any other window, whoever opened it, is closed once its
  content (headers, body text, attachment count; ``content_fingerprint``)
  has stayed the same across passes for ``STALE_S``. A window first seen
  in this pass is never closed in it, whatever the period. A changed
  fingerprint, or a new Mail process, starts its clock again.
- Closing loses nothing: a provably empty window is discarded, anything
  else is salvaged to Drafts. A window whose composition may still be
  running is left, as are one that is minimized and one Mail cannot
  identify.
- A record whose window is gone (closed by someone, or lost with a Mail
  relaunch, which gives restored windows new ids) is ended as ``gone``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from .compose_ledger import Open, WindowRecord

# How long an open record is taken to be a composition still running.
# After its window is found, a composition makes at most eight more
# osascript calls (two pastes with their read-backs, the file paste and
# its check, the send and the Sent copy's check), each bounded by the
# connector's timeout (60 s) and its wait for the Mail lock (30 s):
# twelve minutes at the defaults. A composition older than this is not
# running; its process died before it could say how its window ended.
TEND_GRACE_S = 15 * 60

STALE_S = 60 * 60
"""How long a compose window nobody is known to be composing in must
stay unchanged before tending closes it. Long enough that a person
writing in a window is not closed on: an hour with no keystroke, no
recipient and no attachment added. Short enough that windows do not pile
up: a window left an hour untouched loses nothing by closing, since one
with any content is salvaged to Drafts. Measured from the first pass
that saw the window with its present content, so at the daemon's
15-minute interval a window is closed between 60 and 75 minutes after
its last change."""

# How long the record of a closed window is kept, for anyone reading
# back what tending or a composition did.
RECORD_RETENTION_S = 7 * 24 * 60 * 60

BodyState = Literal["empty", "content", "unreadable"]
TendActionKind = Literal["salvage", "discard"]
TendRule = Literal["abandoned", "stale"]
LeftReason = Literal[
    "in_flight",
    "not_yet_stale",
    "unidentified",
    "minimized",
    "not_listed",
]

# How Mail shows an attached file in a compose body: an image inline as
# an AXImage, anything else as an AXButton (``_build_attachment_ax_verify_script``).
_ATTACHMENT_ROLES = frozenset({"AXButton", "AXImage"})
_FINGERPRINT_LEN = 64


def content_fingerprint(
    *,
    fields: Sequence[str],
    body: BodyState,
    texts: Sequence[str],
    attachments: int,
) -> str:
    """A SHA-256 of what a compose window holds: its header fields, the
    state its body was read in, the body's text runs and its attachment
    count. Only this hash is ever kept; the text is not."""
    payload = json.dumps(
        {"fields": list(fields), "body": body, "texts": list(texts), "attachments": attachments},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ComposeWindowSighting:
    """One compose window as the inventory read it through System Events.

    ``window_id`` is Mail's id for it, None when its name and position
    match no single visible Mail window. ``fields`` are the values of
    its text fields (To, Cc, Bcc and any other header shown, and
    Subject), a recipient token reading as U+FFFC. ``body`` is "empty"
    or "content" as read, and "unreadable" when the body could not be
    found. ``fingerprint`` is ``content_fingerprint`` of all of it.
    """

    name: str
    window_id: int | None
    fields: tuple[str, ...]
    body: BodyState
    sheet: bool
    minimized: bool
    fingerprint: str

    def __post_init__(self) -> None:
        if self.body not in get_args(BodyState):
            raise ValueError(f"unknown body state {self.body!r}")

    @property
    def provably_empty(self) -> bool:
        """Nothing is lost if this window closes, whoever opened it: no
        recipient, no subject, nothing in the body, no attachment (an
        attachment is an element of the body), and no sheet waiting on
        it. A window whose fields or body could not be read is not."""
        return (
            bool(self.fields)
            and all(not value.strip() for value in self.fields)
            and self.body == "empty"
            and not self.sheet
        )


@dataclass(frozen=True)
class ComposeInventory:
    """Mail's compose windows at one moment: those System Events lists,
    and every window Mail's dictionary lists, by id."""

    mail_pid: int
    windows: tuple[ComposeWindowSighting, ...]
    mail_windows: Mapping[int, str]


def _leaf_pairs(roles: Any, values: Any) -> Iterator[tuple[str, Any]]:
    """The (role, value) of every element in a nested read, walking the
    roles' nesting so a value that is itself a list stays one value."""
    if isinstance(roles, list):
        vals = values if isinstance(values, list) else []
        for i, r in enumerate(roles):
            yield from _leaf_pairs(r, vals[i] if i < len(vals) else None)
    else:
        yield str(roles), values


def _body_content(levels_roles: Any, levels_values: Any) -> tuple[tuple[str, ...], int]:
    """The body's text runs, level by level in reading order, and how
    many attachments it shows."""
    texts: list[str] = []
    attachments = 0
    for role, value in _leaf_pairs(levels_roles or [], levels_values or []):
        if role == "AXStaticText":
            texts.append("" if value is None else str(value))
        elif role in _ATTACHMENT_ROLES:
            attachments += 1
    return tuple(texts), attachments


def _identify(
    compose: Sequence[Mapping[str, Any]], mail: Sequence[Mapping[str, Any]]
) -> list[int | None]:
    """Mail's id for each listed compose window: the one visible Mail
    window of its name whose ``bounds`` start where System Events puts
    it. None when there is not exactly one, or when two listed windows
    would get the same id."""
    ids: list[int | None] = []
    for w in compose:
        position = [int(v) for v in w.get("position") or []]
        candidates = [
            int(m["id"])
            for m in mail
            if m.get("visible", True)
            and str(m["name"]) == str(w["name"])
            and [int(v) for v in (m.get("bounds") or [])[:2]] == position
        ]
        ids.append(candidates[0] if len(candidates) == 1 else None)
    taken = Counter(i for i in ids if i is not None)
    return [i if i is not None and taken[i] == 1 else None for i in ids]


def _sighting(w: Mapping[str, Any], window_id: int | None) -> ComposeWindowSighting:
    fields = tuple("" if v is None else str(v) for v in w.get("fields") or [])
    body = w["body"]
    texts, attachments = _body_content(w.get("body_roles"), w.get("body_values"))
    return ComposeWindowSighting(
        name=str(w["name"]),
        window_id=window_id,
        fields=fields,
        body=body,
        sheet=bool(w["sheet"]),
        minimized=bool(w["minimized"]),
        fingerprint=content_fingerprint(
            fields=fields, body=body, texts=texts, attachments=attachments
        ),
    )


def inventory_from_report(report: Mapping[str, Any]) -> ComposeInventory | None:
    """The inventory script's report as a ``ComposeInventory``; None when
    Mail was not running, and the script did not start it. The body's
    text goes into each window's fingerprint and no further."""
    if not report.get("running"):
        return None
    compose = list(report.get("compose") or [])
    mail = list(report.get("mail_windows") or [])
    ids = _identify(compose, mail)
    return ComposeInventory(
        mail_pid=int(report["pid"]),
        windows=tuple(_sighting(w, i) for w, i in zip(compose, ids, strict=True)),
        mail_windows={int(m["id"]): str(m["name"]) for m in mail},
    )


@dataclass(frozen=True)
class ClockEntry:
    """A window's fingerprint, and when tending first saw it with it."""

    fingerprint: str
    since: float

    def __post_init__(self) -> None:
        if len(self.fingerprint) != _FINGERPRINT_LEN or any(
            c not in "0123456789abcdef" for c in self.fingerprint
        ):
            raise ValueError("a fingerprint is 64 lowercase hex characters")


@dataclass(frozen=True)
class SightingClock:
    """When tending first saw each window of one Mail process with the
    content it has now, by Mail's window id. A Mail relaunch gives its
    windows new ids, so a clock is only ever read against the process it
    was kept for."""

    mail_pid: int | None
    windows: Mapping[int, ClockEntry]


def advance_clock(
    clock: SightingClock, inventory: ComposeInventory, now: float
) -> SightingClock:
    """The clock after this inventory: a window seen before with the same
    fingerprint keeps its start, one new or changed starts now, and a
    window no longer listed is forgotten. A new Mail process starts
    every window afresh."""
    previous = clock.windows if clock.mail_pid == inventory.mail_pid else {}
    windows: dict[int, ClockEntry] = {}
    for w in inventory.windows:
        if w.window_id is None:
            continue
        prior = previous.get(w.window_id)
        windows[w.window_id] = (
            prior
            if prior is not None and prior.fingerprint == w.fingerprint
            else ClockEntry(w.fingerprint, now)
        )
    return SightingClock(mail_pid=inventory.mail_pid, windows=windows)


@dataclass(frozen=True)
class TendAction:
    """Close the window Mail knows as ``window_id``: discard it when
    empty, salvage it to Drafts otherwise. ``record`` is the ledger
    record that names it, ended once it closes."""

    window_id: int
    window_name: str
    action: TendActionKind
    rule: TendRule
    record: WindowRecord | None = None


@dataclass(frozen=True)
class LeftWindow:
    """A window, or a record's window, the pass leaves, and why;
    ``remaining_s`` is how long until a not-yet-stale one is stale."""

    name: str
    reason: LeftReason
    remaining_s: float | None = None


@dataclass(frozen=True)
class TendPlan:
    actions: tuple[TendAction, ...]
    gone: tuple[WindowRecord, ...]
    left: tuple[LeftWindow, ...]
    unidentified: int
    clock: SightingClock


def in_flight(record: WindowRecord, now: float, grace_s: float) -> bool:
    """Its composition may still be running: it has not ended, and it
    began less than ``grace_s`` ago. Tending leaves such a window, and
    the daemon's Mail restarter puts a scheduled restart off for it."""
    return isinstance(record.state, Open) and now - record.opened_at < grace_s


@dataclass(frozen=True)
class _Claims:
    """The unfinished records sorted: by the Mail window id each names,
    those whose window is gone, and how many cannot be tied to a window
    at all."""

    by_id: Mapping[int, list[WindowRecord]]
    gone: tuple[WindowRecord, ...]
    unidentified: int


def _claims(
    inventory: ComposeInventory,
    records: Iterable[WindowRecord],
    *,
    now: float,
    grace_s: float,
) -> _Claims:
    by_id: dict[int, list[WindowRecord]] = defaultdict(list)
    gone: list[WindowRecord] = []
    unidentified = 0
    for record in records:
        if not record.unfinished:
            continue
        if record.mail_pid is not None and record.mail_pid != inventory.mail_pid:
            present = False  # Mail relaunched: its ids are not this Mail's
        elif record.window_id is None or record.mail_pid is None:
            unidentified += 1
            continue
        else:
            present = record.window_id in inventory.mail_windows
        if present:
            by_id[record.window_id].append(record)  # type: ignore[index]
        elif not in_flight(record, now, grace_s):
            gone.append(record)
    return _Claims(by_id=by_id, gone=tuple(gone), unidentified=unidentified)


def _kind(window: ComposeWindowSighting) -> TendActionKind:
    return "discard" if window.provably_empty else "salvage"


def _decide(
    window: ComposeWindowSighting,
    claiming: Sequence[WindowRecord],
    previous: Mapping[int, ClockEntry],
    clock: SightingClock,
    *,
    now: float,
    grace_s: float,
    stale_s: float,
) -> TendAction | LeftWindow:
    """For one listed window."""
    if window.minimized:
        return LeftWindow(window.name, "minimized")
    if window.window_id is None:
        return LeftWindow(window.name, "unidentified")
    if any(in_flight(r, now, grace_s) for r in claiming):
        return LeftWindow(window.name, "in_flight")
    record = claiming[0] if len(claiming) == 1 else None
    if record is not None and record.window_name == window.name:
        return TendAction(window.window_id, window.name, _kind(window), "abandoned", record)
    prior = previous.get(window.window_id)
    if prior is not None and prior.fingerprint == window.fingerprint and now - prior.since >= stale_s:
        return TendAction(window.window_id, window.name, _kind(window), "stale", record)
    since = clock.windows[window.window_id].since
    return LeftWindow(window.name, "not_yet_stale", remaining_s=max(0.0, stale_s - (now - since)))


def plan_tending(
    inventory: ComposeInventory,
    records: Iterable[WindowRecord],
    clock: SightingClock,
    *,
    now: float,
    grace_s: float = TEND_GRACE_S,
    stale_s: float = STALE_S,
) -> TendPlan:
    """Decide, window by window, what a pass does, and the clock to keep
    for the next one."""
    claims = _claims(inventory, records, now=now, grace_s=grace_s)
    advanced = advance_clock(clock, inventory, now)
    previous = clock.windows if clock.mail_pid == inventory.mail_pid else {}
    actions: list[TendAction] = []
    left: list[LeftWindow] = []
    for window in inventory.windows:
        claiming = claims.by_id.get(window.window_id, []) if window.window_id else []
        decision = _decide(
            window, claiming, previous, advanced, now=now, grace_s=grace_s, stale_s=stale_s
        )
        (actions if isinstance(decision, TendAction) else left).append(decision)  # type: ignore[arg-type]
    listed = {w.window_id for w in inventory.windows}
    for window_id, claiming in claims.by_id.items():
        if window_id not in listed:
            left.extend(LeftWindow(r.window_name, "not_listed") for r in claiming)
    return TendPlan(
        actions=tuple(actions),
        gone=claims.gone,
        left=tuple(left),
        unidentified=claims.unidentified,
        clock=advanced,
    )


@dataclass(frozen=True)
class TendReport:
    """What one pass found, closed and left. In a dry run ``to_close``
    is what it would have closed, and nothing was closed or recorded."""

    dry_run: bool
    mail_running: bool
    compose_windows: int = 0
    stale_s: float = STALE_S
    to_close: tuple[tuple[str, TendActionKind, TendRule], ...] = ()
    closed: tuple[tuple[str, str, TendRule], ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    not_attempted: tuple[str, ...] = ()
    left: tuple[LeftWindow, ...] = ()
    records_gone: int = 0
    records_unidentified: int = 0
    records_pruned: int = 0
    records_unreadable: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """Structured, for the audit log: names of the windows acted on,
        counts for those left, and the minutes until each window not yet
        stale is."""
        return {
            "dry_run": self.dry_run,
            "mail_running": self.mail_running,
            "compose_windows": self.compose_windows,
            "stale_after_minutes": self.stale_s / 60,
            "to_close": [{"name": n, "action": a, "rule": r} for n, a, r in self.to_close],
            "closed": [{"name": n, "how": h, "rule": r} for n, h, r in self.closed],
            "closed_counts": dict(sorted(Counter(f"{r}_{h}" for _, h, r in self.closed).items())),
            "failed": [{"name": n, "outcome": o} for n, o in self.failed],
            "not_attempted": list(self.not_attempted),
            "left": dict(sorted(Counter(w.reason for w in self.left).items())),
            "not_yet_stale_minutes": sorted(
                math.ceil((w.remaining_s or 0.0) / 60)
                for w in self.left
                if w.reason == "not_yet_stale"
            ),
            "records_gone": self.records_gone,
            "records_unidentified": self.records_unidentified,
            "records_pruned": self.records_pruned,
            "records_unreadable": list(self.records_unreadable),
        }
