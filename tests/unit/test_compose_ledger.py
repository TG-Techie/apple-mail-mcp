"""The compose-window ledger: one record per window the connector opens,
ending in exactly one way (docs/research/compose-window-tending.md,
"The rule")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_mail_mcp.compose_ledger import (
    Closed,
    ComposeLedger,
    LeftOpen,
    Open,
    WindowRecord,
    default_root,
)
from apple_mail_mcp.exceptions import MailComposeLedgerError


def _open(ledger: ComposeLedger, **overrides: object) -> WindowRecord:
    fields: dict[str, object] = {
        "window_name": "Hello",
        "window_id": 2781,
        "mail_pid": 77701,
        "operation": "save",
        "seed": "new",
    }
    fields.update(overrides)
    return ledger.open(**fields)  # type: ignore[arg-type]


class TestTheRoot:
    def test_is_resolved_when_used_not_when_built(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ledger = ComposeLedger()
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "a"))
        assert ledger.root == tmp_path / "a" / "compose_windows"
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "b"))
        assert ledger.root == tmp_path / "b" / "compose_windows"
        record = _open(ledger)
        assert (tmp_path / "b" / "compose_windows" / f"{record.record_id}.json").is_file()

    def test_defaults_under_the_home_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APPLE_MAIL_MCP_HOME", raising=False)
        assert default_root() == Path.home() / ".apple_mail_mcp" / "compose_windows"

    def test_an_explicit_root_is_kept(self, tmp_path: Path) -> None:
        assert ComposeLedger(tmp_path / "x").root == tmp_path / "x"


class TestOpening:
    def test_a_new_record_is_open_and_reads_back_whole(self, tmp_path: Path) -> None:
        ledger = ComposeLedger(tmp_path)
        record = _open(ledger, now=1000.0)
        assert record.state == Open()
        assert record.opened_at == 1000.0
        assert ledger.get(record.record_id) == record
        assert record.unfinished

    def test_every_record_has_its_own_id(self, tmp_path: Path) -> None:
        ledger = ComposeLedger(tmp_path)
        ids = {_open(ledger).record_id for _ in range(5)}
        assert len(ids) == 5

    def test_a_window_mail_gave_no_id_is_recorded_without_one(
        self, tmp_path: Path
    ) -> None:
        ledger = ComposeLedger(tmp_path)
        record = _open(ledger, window_id=None, mail_pid=None)
        assert ledger.get(record.record_id) == record

    @pytest.mark.parametrize(
        "field, value",
        [("operation", "delete"), ("seed", "draft"), ("window_id", 0),
         ("mail_pid", -1)],
    )
    def test_a_value_outside_the_domain_is_refused(
        self, tmp_path: Path, field: str, value: object
    ) -> None:
        with pytest.raises(ValueError):
            _open(ComposeLedger(tmp_path), **{field: value})


class TestEnding:
    def test_an_open_record_ends_once(self, tmp_path: Path) -> None:
        ledger = ComposeLedger(tmp_path)
        record = _open(ledger)
        sent = Closed(how="sent", by="composition", at=5.0)
        assert ledger.end(record.record_id, sent).state == sent
        assert ledger.get(record.record_id).state == sent  # type: ignore[union-attr]
        with pytest.raises(MailComposeLedgerError):
            ledger.end(record.record_id, Closed(how="gone", by="tending", at=6.0))

    def test_a_window_left_open_is_ended_by_tending_only(
        self, tmp_path: Path
    ) -> None:
        ledger = ComposeLedger(tmp_path)
        record = _open(ledger)
        ledger.end(record.record_id, LeftOpen(failure="SALVAGE_FAILED:x", at=1.0))
        with pytest.raises(MailComposeLedgerError):
            ledger.end(
                record.record_id,
                Closed(how="salvaged", by="composition", at=2.0),
            )
        with pytest.raises(MailComposeLedgerError):
            ledger.end(record.record_id, LeftOpen(failure="again", at=2.0))
        done = ledger.end(
            record.record_id, Closed(how="salvaged", by="tending", at=3.0)
        )
        assert not done.unfinished

    def test_a_record_that_is_not_there_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(MailComposeLedgerError):
            ComposeLedger(tmp_path).end(
                "0" * 32, Closed(how="gone", by="tending", at=1.0)
            )


class TestTheStatesCannotBeIllFormed:
    def test_a_saved_window_names_its_draft(self) -> None:
        with pytest.raises(ValueError):
            Closed(how="saved", by="composition", at=1.0)
        assert Closed(how="saved", by="composition", at=1.0, draft_id="42").draft_id == "42"

    @pytest.mark.parametrize("how", ["sent", "salvaged", "discarded", "gone"])
    def test_only_a_saved_window_names_a_draft(self, how: str) -> None:
        with pytest.raises(ValueError):
            Closed(how=how, by="composition", at=1.0, draft_id="42")  # type: ignore[arg-type]

    @pytest.mark.parametrize("how", ["sent", "saved"])
    def test_tending_never_sends_or_saves(self, how: str) -> None:
        with pytest.raises(ValueError):
            Closed(how=how, by="tending", at=1.0, draft_id="42" if how == "saved" else None)  # type: ignore[arg-type]

    def test_an_unknown_ending_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Closed(how="lost", by="composition", at=1.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Closed(how="gone", by="someone", at=1.0)  # type: ignore[arg-type]

    def test_a_draft_id_is_path_safe(self) -> None:
        with pytest.raises(ValueError):
            Closed(how="saved", by="composition", at=1.0, draft_id="../x")


class TestNamesArePathSafe:
    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 33, "ABCDEF" * 6])
    def test_a_record_id_that_is_not_one_is_refused(
        self, tmp_path: Path, bad: str
    ) -> None:
        ledger = ComposeLedger(tmp_path / "ledger")
        with pytest.raises(MailComposeLedgerError):
            ledger.get(bad)
        with pytest.raises(MailComposeLedgerError):
            ledger.end(bad, Closed(how="gone", by="tending", at=1.0))
        assert not (tmp_path / "escape.json").exists()

    def test_a_window_name_never_reaches_a_path(self, tmp_path: Path) -> None:
        ledger = ComposeLedger(tmp_path / "ledger")
        record = _open(ledger, window_name="../../escaped")
        files = [p.name for p in (tmp_path / "ledger").iterdir() if p.suffix == ".json"]
        assert files == [f"{record.record_id}.json"]
        assert not (tmp_path / "escaped.json").exists()


class TestReadingAll:
    def test_lists_every_record_and_names_the_unreadable(
        self, tmp_path: Path
    ) -> None:
        ledger = ComposeLedger(tmp_path)
        a = _open(ledger)
        b = _open(ledger, window_name="Other")
        (tmp_path / ("f" * 32 + ".json")).write_text("{not json", encoding="utf-8")
        (tmp_path / ("e" * 32 + ".json")).write_text(
            json.dumps({"record_id": "e" * 32, "state": {"kind": "exploded"}}),
            encoding="utf-8",
        )
        contents = ledger.read_all()
        assert {r.record_id for r in contents.records} == {a.record_id, b.record_id}
        assert sorted(contents.unreadable) == sorted(["e" * 32 + ".json", "f" * 32 + ".json"])

    def test_an_absent_directory_holds_nothing(self, tmp_path: Path) -> None:
        contents = ComposeLedger(tmp_path / "never").read_all()
        assert contents.records == () and contents.unreadable == ()


class TestPruning:
    def test_removes_only_records_that_ended_before_the_cutoff(
        self, tmp_path: Path
    ) -> None:
        ledger = ComposeLedger(tmp_path)
        old = _open(ledger)
        ledger.end(old.record_id, Closed(how="sent", by="composition", at=100.0))
        recent = _open(ledger)
        ledger.end(recent.record_id, Closed(how="sent", by="composition", at=900.0))
        still_open = _open(ledger, now=10.0)
        left = _open(ledger, now=10.0)
        ledger.end(left.record_id, LeftOpen(failure="x", at=20.0))
        assert ledger.prune(before=500.0) == 1
        remaining = {r.record_id for r in ledger.read_all().records}
        assert remaining == {recent.record_id, still_open.record_id, left.record_id}
