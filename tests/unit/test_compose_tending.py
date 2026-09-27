"""The tending pass's decisions, given a window inventory and the ledger
(docs/research/compose-window-tending.md, "The rule")."""

from __future__ import annotations

import pytest

from apple_mail_mcp.compose_ledger import Closed, LeftOpen, Open, WindowRecord, WindowState
from apple_mail_mcp.compose_tending import (
    TEND_GRACE_S,
    ComposeInventory,
    ComposeWindowSighting,
    TendReport,
    inventory_from_report,
    plan_tending,
)

PID = 77701
NOW = 1_000_000.0
LONG_AGO = NOW - TEND_GRACE_S - 1


def _window(
    name: str,
    *,
    fields: tuple[str, ...] = ("￼", "", "A subject"),
    body: str = "content",
    sheet: bool = False,
) -> ComposeWindowSighting:
    return ComposeWindowSighting(
        name=name, fields=fields, body=body, sheet=sheet, minimized=False  # type: ignore[arg-type]
    )


def _empty(name: str = "New Message") -> ComposeWindowSighting:
    return _window(name, fields=("", "", ""), body="empty")


def _inventory(
    windows: list[ComposeWindowSighting], mail_windows: dict[int, str]
) -> ComposeInventory:
    return ComposeInventory(mail_pid=PID, windows=tuple(windows), mail_windows=mail_windows)


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


class TestWhatIsProvablyEmpty:
    def test_no_recipient_no_subject_no_body_no_sheet(self) -> None:
        assert _empty().provably_empty

    @pytest.mark.parametrize(
        "fields",
        [("￼", "", ""), ("", "", "Hi"), ("typed@exam", "", ""), ("", "", "", "￼")],
    )
    def test_any_header_text_is_content(self, fields: tuple[str, ...]) -> None:
        assert not _window("New Message", fields=fields, body="empty").provably_empty

    @pytest.mark.parametrize("body", ["content", "unread", "unreadable"])
    def test_a_body_not_read_as_empty_is_not(self, body: str) -> None:
        assert not _window("New Message", fields=("", "", ""), body=body).provably_empty

    def test_a_window_with_a_sheet_is_not(self) -> None:
        window = _window("New Message", fields=("", "", ""), body="empty", sheet=True)
        assert not window.provably_empty

    def test_a_window_whose_fields_were_not_read_is_not(self) -> None:
        assert not _window("New Message", fields=(), body="empty").provably_empty

    def test_whitespace_is_nothing(self) -> None:
        assert _window("New Message", fields=(" ", " ", "\t"), body="empty").provably_empty


class TestTheConnectorsOwnWindows:
    def test_an_abandoned_window_with_content_is_salvaged(self) -> None:
        record = _record("Hello", 2900)
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert [(a.record, a.action) for a in plan.actions] == [(record, "salvage")]
        assert plan.left == ()

    def test_an_abandoned_empty_window_is_discarded(self) -> None:
        record = _record("New Message", 2900)
        plan = plan_tending(
            _inventory([_empty()], {2900: "New Message"}), [record], now=NOW
        )
        assert [a.action for a in plan.actions] == ["discard"]

    def test_a_window_its_composition_left_open_is_closed_at_once(self) -> None:
        record = _record(
            "Hello", 2900, opened_at=NOW - 5, state=LeftOpen(failure="x", at=NOW - 1)
        )
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert [a.action for a in plan.actions] == ["salvage"]

    def test_a_composition_still_running_is_left(self) -> None:
        record = _record("Hello", 2900, opened_at=NOW - TEND_GRACE_S + 1)
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert plan.actions == ()
        assert plan.left == (("Hello", "in_flight"),)

    def test_the_grace_is_the_callers(self) -> None:
        record = _record("Hello", 2900, opened_at=NOW - 1)
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW, grace_s=0
        )
        assert [a.action for a in plan.actions] == ["salvage"]

    def test_a_name_shared_with_another_window_is_left(self) -> None:
        """Every close addresses a window by name; with two of the name,
        which is closed cannot be told."""
        record = _record("Re: Alert", 2900)
        plan = plan_tending(
            _inventory(
                [_window("Re: Alert"), _window("Re: Alert")],
                {2900: "Re: Alert", 2901: "Re: Alert"},
            ),
            [record],
            now=NOW,
        )
        assert plan.actions == ()
        assert plan.left == (("Re: Alert", "name_not_unique"),) * 2

    def test_a_window_renamed_since_it_opened_is_left(self) -> None:
        """Its subject was edited after the composition ended: someone is
        working in it."""
        record = _record("Hello", 2900)
        plan = plan_tending(
            _inventory([_window("Hello again")], {2900: "Hello again"}),
            [record],
            now=NOW,
        )
        assert plan.actions == ()
        assert plan.left == (("Hello again", "renamed"),)

    def test_a_window_mail_has_but_system_events_does_not_list_is_left(self) -> None:
        record = _record("Hello", 2900)
        plan = plan_tending(_inventory([], {2900: "Hello"}), [record], now=NOW)
        assert plan.actions == ()
        assert plan.left == (("Hello", "not_listed"),)


class TestRecordsWhoseWindowIsGone:
    def test_a_window_no_longer_open_ends_its_record(self) -> None:
        record = _record("Hello", 2900)
        plan = plan_tending(_inventory([], {}), [record], now=NOW)
        assert plan.gone == (record,)

    def test_a_relaunched_mail_ends_the_record(self) -> None:
        """Mail gives the windows it restores new ids, so a record from
        the Mail before is no window of this one, even if a window of
        its name and old id is there."""
        record = _record("Hello", 2900, mail_pid=11111)
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert plan.gone == (record,)
        assert plan.actions == ()
        assert plan.left == (("Hello", "unowned"),)

    def test_a_composition_still_running_keeps_its_record(self) -> None:
        """Its window may just have closed, with the composition about to
        say how."""
        record = _record("Hello", 2900, opened_at=NOW - 10)
        plan = plan_tending(_inventory([], {}), [record], now=NOW)
        assert plan.gone == ()

    def test_a_record_without_a_window_id_is_never_acted_on(self) -> None:
        record = _record("Hello", None)
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert plan.actions == () and plan.gone == ()
        assert plan.unidentified == 1
        assert plan.left == (("Hello", "unowned"),)

    def test_closed_records_are_not_considered(self) -> None:
        record = _record("Hello", 2900, state=Closed(how="sent", by="composition", at=1.0))
        plan = plan_tending(
            _inventory([_window("Hello")], {2900: "Hello"}), [record], now=NOW
        )
        assert plan.actions == () and plan.gone == ()
        assert plan.left == (("Hello", "unowned"),)


class TestEverythingElseIsLeftAndCounted:
    def test_the_restored_windows_of_2026_09_27(self) -> None:
        """The shape measured that morning: eighteen empty "New Message"
        windows, two and four identical replies, one forward, and a
        ledger that names none of them. Nothing is closed."""
        windows = (
            [_empty() for _ in range(18)]
            + [_window("Re: notification") for _ in range(2)]
            + [_window("Re: alert") for _ in range(4)]
            + [_window("Fwd: note")]
        )
        mail_windows = {2756 + i: w.name for i, w in enumerate(windows)}
        plan = plan_tending(_inventory(windows, mail_windows), [], now=NOW)
        assert plan.actions == () and plan.gone == ()
        reasons = [reason for _, reason in plan.left]
        assert reasons.count("unowned_empty") == 18
        assert reasons.count("unowned") == 7

    def test_a_ledger_window_among_them_is_the_only_one_touched(self) -> None:
        windows = [_empty() for _ in range(3)] + [_window("Mine")]
        mail_windows = {1: "New Message", 2: "New Message", 3: "New Message", 4: "Mine"}
        mine = _record("Mine", 4)
        plan = plan_tending(_inventory(windows, mail_windows), [mine], now=NOW)
        assert [(a.record.window_name, a.action) for a in plan.actions] == [("Mine", "salvage")]
        assert sorted(r for _, r in plan.left) == ["unowned_empty"] * 3


class TestTheInventoryReport:
    def test_mail_not_running_is_no_inventory(self) -> None:
        assert inventory_from_report({"running": False}) is None

    def test_reads_windows_and_mails_ids(self) -> None:
        inventory = inventory_from_report(
            {
                "running": True,
                "pid": PID,
                "compose": [
                    {"name": "New Message", "fields": ["", "", ""], "body": "empty",
                     "sheet": False, "minimized": False},
                ],
                "mail_windows": [{"id": 2781, "name": "New Message"}],
            }
        )
        assert inventory == ComposeInventory(
            mail_pid=PID,
            windows=(_empty(),),
            mail_windows={2781: "New Message"},
        )

    def test_an_unknown_body_state_is_refused(self) -> None:
        with pytest.raises(ValueError):
            inventory_from_report(
                {"running": True, "pid": PID, "mail_windows": [],
                 "compose": [{"name": "x", "fields": [], "body": "maybe",
                              "sheet": False, "minimized": False}]}
            )


def test_the_report_counts_what_it_left() -> None:
    report = TendReport(
        dry_run=False,
        mail_running=True,
        compose_windows=3,
        closed=(("Mine", "salvaged"),),
        left=(("New Message", "unowned_empty"), ("New Message", "unowned_empty")),
    )
    out = report.as_dict()
    assert out["closed"] == [{"name": "Mine", "how": "salvaged"}]
    assert out["left"] == {"unowned_empty": 2}
