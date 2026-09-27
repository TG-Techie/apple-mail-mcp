"""What a tending pass does with Mail's compose windows: the decision,
from an inventory of the windows System Events lists and the ledger of
windows the connector opened (``compose_ledger``). Pure; the connector
takes the inventory and carries the decision out
(``AppleMailConnector.tend_compose_windows``).

The rule, and the measurement it rests on, is in
docs/research/compose-window-tending.md. In short:

- A window is the connector's when a ledger record names it: the same
  Mail process, Mail's window id still present, under the name the
  record holds. Tending closes only such a window, and only when its
  composition cannot still be running: a record the composition left
  open at once, an open one after ``TEND_GRACE_S``. Its name must be the
  only window of that name, since every close addresses a window by
  name. Empty, it is discarded; anything else is salvaged to Drafts.
- Everything else is left and counted, empty or not. An empty window
  nobody recorded is counted apart from the rest, because it loses
  nothing if closed; whether tending may ever close one is the
  operator's decision, not made here.
- A record whose window is gone (closed by someone, or lost with a Mail
  relaunch, which gives restored windows new ids) is ended as ``gone``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
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

# How long the record of a closed window is kept, for anyone reading
# back what tending or a composition did.
RECORD_RETENTION_S = 7 * 24 * 60 * 60

BodyState = Literal["empty", "content", "unread", "unreadable"]
TendActionKind = Literal["salvage", "discard"]
LeftReason = Literal[
    "unowned",
    "unowned_empty",
    "in_flight",
    "name_not_unique",
    "renamed",
    "not_listed",
]


@dataclass(frozen=True)
class ComposeWindowSighting:
    """One compose window as the inventory read it through System Events.

    ``fields`` are the values of its text fields (To, Cc, Bcc and any
    other header shown, and Subject), a recipient token reading as
    U+FFFC. ``body`` is "empty" or "content" as read, "unread" when a
    header field already had text so the body did not matter, and
    "unreadable" when the body could not be found.
    """

    name: str
    fields: tuple[str, ...]
    body: BodyState
    sheet: bool
    minimized: bool

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


def inventory_from_report(report: Mapping[str, Any]) -> ComposeInventory | None:
    """The inventory script's report as a ``ComposeInventory``; None when
    Mail was not running, and the script did not start it."""
    if not report.get("running"):
        return None
    windows = tuple(
        ComposeWindowSighting(
            name=str(w["name"]),
            fields=tuple(str(v) for v in w.get("fields") or []),
            body=w["body"],
            sheet=bool(w["sheet"]),
            minimized=bool(w["minimized"]),
        )
        for w in report.get("compose") or []
    )
    mail_windows = {
        int(m["id"]): str(m["name"]) for m in report.get("mail_windows") or []
    }
    return ComposeInventory(
        mail_pid=int(report["pid"]), windows=windows, mail_windows=mail_windows
    )


@dataclass(frozen=True)
class TendAction:
    """Close the window ``record`` names: discard it when empty, salvage
    it to Drafts otherwise."""

    record: WindowRecord
    action: TendActionKind


@dataclass(frozen=True)
class TendPlan:
    actions: tuple[TendAction, ...]
    gone: tuple[WindowRecord, ...]
    left: tuple[tuple[str, LeftReason], ...]
    unidentified: int


def _in_flight(record: WindowRecord, now: float, grace_s: float) -> bool:
    """Its composition may still be running: it has not ended, and it
    began less than ``grace_s`` ago."""
    return isinstance(record.state, Open) and now - record.opened_at < grace_s


@dataclass(frozen=True)
class _Claims:
    """The unfinished records sorted: by the name Mail's window for each
    has now, those whose window is gone, and how many cannot be tied to
    a window at all."""

    by_name: Mapping[str, list[WindowRecord]]
    gone: tuple[WindowRecord, ...]
    unidentified: int


def _claims(
    inventory: ComposeInventory,
    records: Iterable[WindowRecord],
    *,
    now: float,
    grace_s: float,
) -> _Claims:
    by_name: dict[str, list[WindowRecord]] = defaultdict(list)
    gone: list[WindowRecord] = []
    unidentified = 0
    for record in records:
        if not record.unfinished:
            continue
        if record.mail_pid is not None and record.mail_pid != inventory.mail_pid:
            name_now = None  # Mail relaunched: its ids are not this Mail's
        elif record.window_id is None or record.mail_pid is None:
            unidentified += 1
            continue
        else:
            name_now = inventory.mail_windows.get(record.window_id)
        if name_now is not None:
            by_name[name_now].append(record)
        elif not _in_flight(record, now, grace_s):
            gone.append(record)
    return _Claims(by_name=by_name, gone=tuple(gone), unidentified=unidentified)


def _decide(
    record: WindowRecord,
    window: ComposeWindowSighting,
    *,
    now: float,
    grace_s: float,
) -> TendAction | LeftReason:
    """For the one window of its name, which ``record`` claims."""
    if _in_flight(record, now, grace_s):
        return "in_flight"
    if window.name != record.window_name:
        return "renamed"
    return TendAction(
        record=record, action="discard" if window.provably_empty else "salvage"
    )


def plan_tending(
    inventory: ComposeInventory,
    records: Iterable[WindowRecord],
    *,
    now: float,
    grace_s: float = TEND_GRACE_S,
) -> TendPlan:
    """Decide, window by window, what a pass does."""
    sightings_by_name: dict[str, list[ComposeWindowSighting]] = defaultdict(list)
    for window in inventory.windows:
        sightings_by_name[window.name].append(window)
    claims = _claims(inventory, records, now=now, grace_s=grace_s)

    actions: list[TendAction] = []
    left: list[tuple[str, LeftReason]] = []
    for name, sightings in sightings_by_name.items():
        claiming = claims.by_name.get(name, [])
        if not claiming:
            left.extend(
                (name, "unowned_empty" if w.provably_empty else "unowned")
                for w in sightings
            )
        elif len(sightings) > 1 or len(claiming) > 1:
            left.extend((name, "name_not_unique") for _ in sightings)
        else:
            decision = _decide(claiming[0], sightings[0], now=now, grace_s=grace_s)
            if isinstance(decision, TendAction):
                actions.append(decision)
            else:
                left.append((name, decision))
    for name, claiming in claims.by_name.items():
        if name not in sightings_by_name:
            left.extend((name, "not_listed") for _ in claiming)
    return TendPlan(
        actions=tuple(actions),
        gone=claims.gone,
        left=tuple(left),
        unidentified=claims.unidentified,
    )


@dataclass(frozen=True)
class TendReport:
    """What one pass found, closed and left. In a dry run ``to_close``
    is what it would have closed, and nothing was closed or recorded."""

    dry_run: bool
    mail_running: bool
    compose_windows: int = 0
    to_close: tuple[tuple[str, TendActionKind], ...] = ()
    closed: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    not_attempted: tuple[str, ...] = ()
    left: tuple[tuple[str, LeftReason], ...] = ()
    records_gone: int = 0
    records_unidentified: int = 0
    records_pruned: int = 0
    records_unreadable: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """Structured, for the audit log: names of the windows acted on,
        counts for those left."""
        return {
            "dry_run": self.dry_run,
            "mail_running": self.mail_running,
            "compose_windows": self.compose_windows,
            "to_close": [{"name": n, "action": a} for n, a in self.to_close],
            "closed": [{"name": n, "how": h} for n, h in self.closed],
            "failed": [{"name": n, "outcome": o} for n, o in self.failed],
            "not_attempted": list(self.not_attempted),
            "left": dict(sorted(Counter(r for _, r in self.left).items())),
            "records_gone": self.records_gone,
            "records_unidentified": self.records_unidentified,
            "records_pruned": self.records_pruned,
            "records_unreadable": list(self.records_unreadable),
        }
