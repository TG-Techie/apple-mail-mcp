"""The tending pass's decisions, given a window inventory, the ledger and
the clock of when each window's content was first seen
(docs/research/compose-window-tending.md, "The rule")."""

from __future__ import annotations

import pytest

from apple_mail_mcp.compose_ledger import Closed, LeftOpen, Open, WindowRecord, WindowState
from apple_mail_mcp.compose_tending import (
    STALE_S,
    TEND_GRACE_S,
    ClockEntry,
    ComposeInventory,
    ComposeWindowSighting,
    LeftWindow,
    SightingClock,
    TendReport,
    advance_clock,
    content_fingerprint,
    inventory_from_report,
    plan_tending,
)

PID = 77701
NOW = 1_000_000.0
LONG_AGO = NOW - TEND_GRACE_S - 1
NO_CLOCK = SightingClock(mail_pid=None, windows={})

_FULL = content_fingerprint(fields=("￼", "", "A subject"), body="content", texts=("Hi",), attachments=0)
_EMPTY = content_fingerprint(fields=("", "", ""), body="empty", texts=(), attachments=0)


def _window(
    name: str,
    window_id: int | None,
    *,
    fields: tuple[str, ...] = ("￼", "", "A subject"),
    body: str = "content",
    sheet: bool = False,
    minimized: bool = False,
    fingerprint: str = _FULL,
) -> ComposeWindowSighting:
    return ComposeWindowSighting(
        name=name,
        window_id=window_id,
        fields=fields,
        body=body,  # type: ignore[arg-type]
        sheet=sheet,
        minimized=minimized,
        fingerprint=fingerprint,
    )


def _empty(window_id: int | None, name: str = "New Message") -> ComposeWindowSighting:
    return _window(name, window_id, fields=("", "", ""), body="empty", fingerprint=_EMPTY)


def _inventory(
    windows: list[ComposeWindowSighting], mail_windows: dict[int, str] | None = None
) -> ComposeInventory:
    if mail_windows is None:
        mail_windows = {w.window_id: w.name for w in windows if w.window_id is not None}
    return ComposeInventory(mail_pid=PID, windows=tuple(windows), mail_windows=mail_windows)


def _seen(since: float, *windows: ComposeWindowSighting, pid: int = PID) -> SightingClock:
    """A clock that first saw each window, with its present content, at
    ``since``."""
    return SightingClock(
        mail_pid=pid,
        windows={w.window_id: ClockEntry(w.fingerprint, since) for w in windows if w.window_id},
    )


_next_id = iter(range(1, 10_000))


def _record(
    name: str,
    window_id: int | None,
    *,
    state: WindowState | None = None,
    opened_at: float = LONG_AGO,
    mail_pid: int | None = PID,
) -> WindowRecord:
    return WindowRecord(
        record_id=f"{next(_next_id):032x}",
        opened_at=opened_at,
        operation="send",
        seed="new",
        window_name=name,
        window_id=window_id,
        mail_pid=mail_pid,
        state=Open() if state is None else state,
    )


def _reasons(left: tuple[LeftWindow, ...]) -> list[str]:
    return [w.reason for w in left]


class TestWhatIsProvablyEmpty:
    def test_no_recipient_no_subject_no_body_no_sheet(self) -> None:
        assert _empty(1).provably_empty

    @pytest.mark.parametrize(
        "fields",
        [("￼", "", ""), ("", "", "Hi"), ("typed@exam", "", ""), ("", "", "", "￼")],
    )
    def test_any_header_text_is_content(self, fields: tuple[str, ...]) -> None:
        assert not _window("New Message", 1, fields=fields, body="empty").provably_empty

    @pytest.mark.parametrize("body", ["content", "unreadable"])
    def test_a_body_not_read_as_empty_is_not(self, body: str) -> None:
        assert not _window("New Message", 1, fields=("", "", ""), body=body).provably_empty

    def test_a_window_with_a_sheet_is_not(self) -> None:
        window = _window("New Message", 1, fields=("", "", ""), body="empty", sheet=True)
        assert not window.provably_empty

    def test_a_window_whose_fields_were_not_read_is_not(self) -> None:
        assert not _window("New Message", 1, fields=(), body="empty").provably_empty

    def test_whitespace_is_nothing(self) -> None:
        assert _window("New Message", 1, fields=(" ", " ", "\t"), body="empty").provably_empty

    def test_an_unknown_body_state_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _window("x", 1, body="unread")


class TestTheFingerprint:
    def test_is_a_hash_and_never_the_text(self) -> None:
        fp = content_fingerprint(
            fields=("￼", "", "Secret subject"), body="content",
            texts=("a private sentence",), attachments=1,
        )
        assert len(fp) == 64 and int(fp, 16) >= 0
        assert "Secret" not in fp and "private" not in fp

    def test_is_the_same_for_the_same_content(self) -> None:
        a = content_fingerprint(fields=("x",), body="content", texts=("y",), attachments=0)
        b = content_fingerprint(fields=("x",), body="content", texts=("y",), attachments=0)
        assert a == b

    @pytest.mark.parametrize(
        "change",
        [
            {"fields": ("x", "cc")},
            {"texts": ("y", "more")},
            {"texts": ("y!",)},
            {"attachments": 1},
            {"body": "unreadable"},
        ],
    )
    def test_changes_with_any_header_body_text_or_attachment(self, change: dict) -> None:  # type: ignore[type-arg]
        base: dict = {"fields": ("x",), "body": "content", "texts": ("y",), "attachments": 0}  # type: ignore[type-arg]
        assert content_fingerprint(**base) != content_fingerprint(**{**base, **change})

    def test_does_not_run_fields_and_text_together(self) -> None:
        a = content_fingerprint(fields=("ab",), body="content", texts=("c",), attachments=0)
        b = content_fingerprint(fields=("a",), body="content", texts=("bc",), attachments=0)
        assert a != b


class TestTheClock:
    def test_a_window_first_seen_starts_now(self) -> None:
        w = _window("Hi", 10)
        clock = advance_clock(NO_CLOCK, _inventory([w]), NOW)
        assert clock == SightingClock(mail_pid=PID, windows={10: ClockEntry(_FULL, NOW)})

    def test_an_unchanged_window_keeps_its_start(self) -> None:
        w = _window("Hi", 10)
        clock = advance_clock(_seen(NOW - 50, w), _inventory([w]), NOW)
        assert clock.windows[10].since == NOW - 50

    def test_a_changed_fingerprint_restarts_it(self) -> None:
        before = _window("Hi", 10)
        edited = _window("Hi", 10, fingerprint="f" * 64)
        clock = advance_clock(_seen(NOW - 50, before), _inventory([edited]), NOW)
        assert clock.windows[10] == ClockEntry("f" * 64, NOW)

    def test_a_new_mail_process_restarts_every_clock(self) -> None:
        w = _window("Hi", 10)
        clock = advance_clock(_seen(NOW - 50, w, pid=11111), _inventory([w]), NOW)
        assert clock == SightingClock(mail_pid=PID, windows={10: ClockEntry(_FULL, NOW)})

    def test_a_window_gone_is_forgotten(self) -> None:
        w = _window("Hi", 10)
        clock = advance_clock(_seen(NOW - 50, w), _inventory([]), NOW)
        assert clock.windows == {}

    def test_a_window_without_identity_is_not_clocked(self) -> None:
        clock = advance_clock(NO_CLOCK, _inventory([_window("Hi", None)], {}), NOW)
        assert clock.windows == {}


class TestStaleWindowsAreClosedWhoeverOpenedThem:
    def test_first_seen_is_never_closed_whatever_the_period(self) -> None:
        w = _empty(10)
        plan = plan_tending(_inventory([w]), [], NO_CLOCK, now=NOW, stale_s=0)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("New Message", "not_yet_stale", remaining_s=0),)

    def test_an_empty_window_unchanged_for_the_period_is_discarded(self) -> None:
        w = _empty(10)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - STALE_S, w), now=NOW)
        assert [(a.window_id, a.action, a.rule, a.record) for a in plan.actions] == [
            (10, "discard", "stale", None)
        ]
        assert plan.left == ()

    def test_one_with_content_is_salvaged(self) -> None:
        w = _window("Re: alert", 10)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - STALE_S, w), now=NOW)
        assert [(a.window_name, a.action, a.rule) for a in plan.actions] == [
            ("Re: alert", "salvage", "stale")
        ]

    def test_one_not_yet_stale_is_left_with_the_time_remaining(self) -> None:
        w = _window("Re: alert", 10)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - 600, w), now=NOW)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Re: alert", "not_yet_stale", remaining_s=STALE_S - 600),)

    def test_an_edit_restarts_the_clock(self) -> None:
        before = _window("Hi", 10)
        edited = _window("Hi", 10, fingerprint="e" * 64)
        plan = plan_tending(_inventory([edited]), [], _seen(NOW - 2 * STALE_S, before), now=NOW)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Hi", "not_yet_stale", remaining_s=STALE_S),)
        assert plan.clock.windows[10] == ClockEntry("e" * 64, NOW)

    def test_a_relaunched_mail_restarts_the_clock(self) -> None:
        w = _empty(10)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - 2 * STALE_S, w, pid=11111), now=NOW)
        assert plan.actions == ()
        assert _reasons(plan.left) == ["not_yet_stale"]

    def test_same_named_windows_are_told_apart_by_id(self) -> None:
        """Eighteen "New Message" windows at once: the stale one is closed,
        its sibling first seen this pass is left."""
        old, new = _empty(10), _empty(11)
        plan = plan_tending(_inventory([old, new]), [], _seen(NOW - STALE_S, old), now=NOW)
        assert [a.window_id for a in plan.actions] == [10]
        assert _reasons(plan.left) == ["not_yet_stale"]

    def test_a_window_mail_cannot_identify_is_left(self) -> None:
        w = _window("Hi", None)
        plan = plan_tending(_inventory([w], {}), [], NO_CLOCK, now=NOW, stale_s=0)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Hi", "unidentified"),)

    def test_a_minimized_window_is_left(self) -> None:
        w = _window("Hi", 10, minimized=True)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - STALE_S, w), now=NOW)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Hi", "minimized"),)

    def test_the_period_is_the_callers(self) -> None:
        w = _empty(10)
        plan = plan_tending(_inventory([w]), [], _seen(NOW - 5, w), now=NOW, stale_s=5)
        assert [a.rule for a in plan.actions] == ["stale"]


class TestTheConnectorsOwnWindows:
    def test_an_abandoned_window_with_content_is_salvaged_at_once(self) -> None:
        record = _record("Hello", 2900)
        w = _window("Hello", 2900)
        plan = plan_tending(_inventory([w]), [record], NO_CLOCK, now=NOW)
        assert [(a.record, a.action, a.rule, a.window_id) for a in plan.actions] == [
            (record, "salvage", "abandoned", 2900)
        ]
        assert plan.left == ()

    def test_an_abandoned_empty_window_is_discarded(self) -> None:
        record = _record("New Message", 2900)
        plan = plan_tending(_inventory([_empty(2900)]), [record], NO_CLOCK, now=NOW)
        assert [(a.action, a.rule) for a in plan.actions] == [("discard", "abandoned")]

    def test_a_window_its_composition_left_open_is_closed_at_once(self) -> None:
        record = _record(
            "Hello", 2900, opened_at=NOW - 5, state=LeftOpen(failure="x", at=NOW - 1)
        )
        plan = plan_tending(_inventory([_window("Hello", 2900)]), [record], NO_CLOCK, now=NOW)
        assert [a.action for a in plan.actions] == ["salvage"]

    def test_a_composition_still_running_is_left_however_stale(self) -> None:
        record = _record("Hello", 2900, opened_at=NOW - TEND_GRACE_S + 1)
        w = _window("Hello", 2900)
        plan = plan_tending(_inventory([w]), [record], _seen(NOW - STALE_S, w), now=NOW)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Hello", "in_flight"),)

    def test_the_grace_is_the_callers(self) -> None:
        record = _record("Hello", 2900, opened_at=NOW - 1)
        plan = plan_tending(
            _inventory([_window("Hello", 2900)]), [record], NO_CLOCK, now=NOW, grace_s=0
        )
        assert [a.action for a in plan.actions] == ["salvage"]

    def test_a_name_shared_with_another_window_no_longer_matters(self) -> None:
        """Closes address a window by Mail's id, so its twin is no reason
        to leave it, and the twin is not touched."""
        record = _record("Re: Alert", 2900)
        plan = plan_tending(
            _inventory([_window("Re: Alert", 2900), _window("Re: Alert", 2901)]),
            [record],
            NO_CLOCK,
            now=NOW,
        )
        assert [a.window_id for a in plan.actions] == [2900]
        assert plan.left == (LeftWindow("Re: Alert", "not_yet_stale", remaining_s=STALE_S),)

    def test_a_window_renamed_since_it_opened_falls_to_the_stale_rule(self) -> None:
        """Its subject was edited after the composition ended: someone may
        be working in it, so it is closed only once it has gone untouched
        for the stale period, and then its record is ended too."""
        record = _record("Hello", 2900)
        w = _window("Hello again", 2900)
        fresh = plan_tending(_inventory([w]), [record], NO_CLOCK, now=NOW)
        assert fresh.actions == ()
        assert _reasons(fresh.left) == ["not_yet_stale"]
        stale = plan_tending(_inventory([w]), [record], _seen(NOW - STALE_S, w), now=NOW)
        assert [(a.rule, a.record) for a in stale.actions] == [("stale", record)]

    def test_a_window_mail_has_but_system_events_does_not_list_is_left(self) -> None:
        record = _record("Hello", 2900)
        plan = plan_tending(_inventory([], {2900: "Hello"}), [record], NO_CLOCK, now=NOW)
        assert plan.actions == ()
        assert plan.left == (LeftWindow("Hello", "not_listed"),)


class TestRecordsWhoseWindowIsGone:
    def test_a_window_no_longer_open_ends_its_record(self) -> None:
        record = _record("Hello", 2900)
        plan = plan_tending(_inventory([], {}), [record], NO_CLOCK, now=NOW)
        assert plan.gone == (record,)

    def test_a_relaunched_mail_ends_the_record(self) -> None:
        """Mail gives the windows it restores new ids, so a record from
        the Mail before is no window of this one, even if a window of
        its old id is there."""
        record = _record("Hello", 2900, mail_pid=11111)
        plan = plan_tending(_inventory([_window("Hello", 2900)]), [record], NO_CLOCK, now=NOW)
        assert plan.gone == (record,)
        assert plan.actions == ()
        assert _reasons(plan.left) == ["not_yet_stale"]

    def test_a_composition_still_running_keeps_its_record(self) -> None:
        record = _record("Hello", 2900, opened_at=NOW - 10)
        plan = plan_tending(_inventory([], {}), [record], NO_CLOCK, now=NOW)
        assert plan.gone == ()

    def test_a_record_without_a_window_id_is_never_acted_on(self) -> None:
        record = _record("Hello", None)
        plan = plan_tending(_inventory([_window("Hello", 2900)]), [record], NO_CLOCK, now=NOW)
        assert plan.actions == () and plan.gone == ()
        assert plan.unidentified == 1

    def test_closed_records_are_not_considered(self) -> None:
        record = _record("Hello", 2900, state=Closed(how="sent", by="composition", at=1.0))
        plan = plan_tending(_inventory([_window("Hello", 2900)]), [record], NO_CLOCK, now=NOW)
        assert plan.actions == () and plan.gone == ()
        assert _reasons(plan.left) == ["not_yet_stale"]


class TestThePileOf2026_10_01:
    """The shape read that morning: eighteen empty "New Message" windows,
    six replies, one forward, a ledger that names none of them."""

    def _pile(self) -> list[ComposeWindowSighting]:
        return (
            [_empty(3900 + i) for i in range(18)]
            + [_window("Re: alert", 3950 + i) for i in range(6)]
            + [_window("Fwd: note", 3960)]
        )

    def test_a_first_pass_closes_nothing_and_starts_every_clock(self) -> None:
        pile = self._pile()
        plan = plan_tending(_inventory(pile), [], NO_CLOCK, now=NOW)
        assert plan.actions == ()
        assert _reasons(plan.left) == ["not_yet_stale"] * 25
        assert {w.remaining_s for w in plan.left} == {STALE_S}
        assert set(plan.clock.windows) == {w.window_id for w in pile}

    def test_a_pass_an_hour_later_closes_all_of_them_losing_nothing(self) -> None:
        pile = self._pile()
        plan = plan_tending(_inventory(pile), [], _seen(NOW - STALE_S, *pile), now=NOW)
        kinds = [a.action for a in plan.actions]
        assert kinds.count("discard") == 18 and kinds.count("salvage") == 7
        assert plan.left == ()


class TestTheInventoryReport:
    def _report(self, compose: list[dict], mail: list[dict]) -> dict:  # type: ignore[type-arg]
        return {"running": True, "pid": PID, "compose": compose, "mail_windows": mail}

    def _compose(self, name: str, x: int, **over: object) -> dict:  # type: ignore[type-arg]
        entry: dict = {  # type: ignore[type-arg]
            "name": name, "position": [x, 100], "fields": ["", "", ""], "body": "empty",
            "body_roles": [[]], "body_values": [[]], "sheet": False, "minimized": False,
        }
        entry.update(over)
        return entry

    def _mail(self, wid: int, name: str, x: int, visible: bool = True) -> dict:  # type: ignore[type-arg]
        return {"id": wid, "name": name, "bounds": [x, 100, x + 800, 900], "visible": visible}

    def test_mail_not_running_is_no_inventory(self) -> None:
        assert inventory_from_report({"running": False}) is None

    def test_each_window_gets_mails_id_by_name_and_position(self) -> None:
        inventory = inventory_from_report(
            self._report(
                [self._compose("New Message", 10), self._compose("New Message", 40)],
                [self._mail(2781, "New Message", 40), self._mail(2780, "New Message", 10),
                 self._mail(2790, "Inbox", 0)],
            )
        )
        assert inventory is not None
        assert [w.window_id for w in inventory.windows] == [2780, 2781]
        assert inventory.mail_windows == {2781: "New Message", 2780: "New Message", 2790: "Inbox"}
        assert inventory.windows[0] == _empty(2780)

    def test_two_windows_mail_cannot_tell_apart_have_no_id(self) -> None:
        inventory = inventory_from_report(
            self._report(
                [self._compose("New Message", 10), self._compose("New Message", 10)],
                [self._mail(2781, "New Message", 10), self._mail(2780, "New Message", 10)],
            )
        )
        assert inventory is not None
        assert [w.window_id for w in inventory.windows] == [None, None]

    def test_a_hidden_window_of_the_same_place_is_not_a_candidate(self) -> None:
        inventory = inventory_from_report(
            self._report(
                [self._compose("New Message", 10)],
                [self._mail(2781, "New Message", 10), self._mail(2799, "New Message", 10, False)],
            )
        )
        assert inventory is not None and inventory.windows[0].window_id == 2781

    def test_one_listed_window_is_one_id(self) -> None:
        """Two listed windows that both match one Mail window cannot both
        be it."""
        inventory = inventory_from_report(
            self._report(
                [self._compose("New Message", 10), self._compose("New Message", 10)],
                [self._mail(2781, "New Message", 10)],
            )
        )
        assert inventory is not None
        assert [w.window_id for w in inventory.windows] == [None, None]

    def test_the_body_is_read_into_a_fingerprint_and_dropped(self) -> None:
        inventory = inventory_from_report(
            self._report(
                [self._compose(
                    "Re: alert", 10, fields=["￼", "", "Re: alert"], body="content",
                    body_roles=[["AXStaticText", "AXGroup", "AXButton"], [[], ["AXStaticText"], []]],
                    body_values=[["On Monday", None, "file.pdf, 3 KB"], [[], ["quoted"], []]],
                )],
                [self._mail(2781, "Re: alert", 10)],
            )
        )
        assert inventory is not None
        (w,) = inventory.windows
        assert w.fingerprint == content_fingerprint(
            fields=("￼", "", "Re: alert"), body="content",
            texts=("On Monday", "quoted"), attachments=1,
        )
        assert "On Monday" not in repr(w)

    def test_an_unknown_body_state_is_refused(self) -> None:
        with pytest.raises(ValueError):
            inventory_from_report(
                self._report([self._compose("x", 1, body="maybe")], [self._mail(1, "x", 1)])
            )


def test_the_report_counts_what_it_closed_and_left() -> None:
    report = TendReport(
        dry_run=False,
        mail_running=True,
        compose_windows=4,
        stale_s=STALE_S,
        closed=(("Mine", "salvaged", "abandoned"), ("New Message", "discarded", "stale")),
        left=(
            LeftWindow("New Message", "not_yet_stale", remaining_s=STALE_S),
            LeftWindow("Re: x", "not_yet_stale", remaining_s=61.0),
            LeftWindow("Hi", "in_flight"),
        ),
    )
    out = report.as_dict()
    assert out["closed"] == [
        {"name": "Mine", "how": "salvaged", "rule": "abandoned"},
        {"name": "New Message", "how": "discarded", "rule": "stale"},
    ]
    assert out["closed_counts"] == {"abandoned_salvaged": 1, "stale_discarded": 1}
    assert out["left"] == {"in_flight": 1, "not_yet_stale": 2}
    assert out["not_yet_stale_minutes"] == [2, 60]
    assert out["stale_after_minutes"] == 60
