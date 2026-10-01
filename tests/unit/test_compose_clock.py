"""The file tending keeps its clock in between passes: when each compose
window was first seen with the content it has now, by fingerprint only
(docs/research/compose-window-tending.md, "The rule")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_mail_mcp.compose_clock import ComposeClock, default_path
from apple_mail_mcp.compose_tending import ClockEntry, SightingClock

FP = "a" * 64


class TestThePath:
    def test_sits_next_to_the_ledger_and_is_resolved_when_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        clock = ComposeClock()
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "a"))
        assert clock.path == tmp_path / "a" / "compose_clock.json"
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "b"))
        assert clock.path == default_path() == tmp_path / "b" / "compose_clock.json"


class TestLoadAndSave:
    def test_no_file_is_an_empty_clock(self, tmp_path: Path) -> None:
        assert ComposeClock(tmp_path / "c.json").load() == SightingClock(None, {})

    def test_a_saved_clock_loads_back(self, tmp_path: Path) -> None:
        store = ComposeClock(tmp_path / "home" / "c.json")
        clock = SightingClock(mail_pid=77701, windows={2781: ClockEntry(FP, 12.5)})
        store.save(clock)
        assert store.load() == clock

    def test_the_file_holds_hashes_and_times_only(self, tmp_path: Path) -> None:
        store = ComposeClock(tmp_path / "c.json")
        store.save(SightingClock(mail_pid=77701, windows={2781: ClockEntry(FP, 12.5)}))
        assert json.loads(store.path.read_text()) == {
            "mail_pid": 77701,
            "windows": {"2781": {"fingerprint": FP, "since": 12.5}},
        }

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            '{"mail_pid": 1, "windows": {"x": {"fingerprint": "' + FP + '", "since": 1}}}',
            '{"mail_pid": 1, "windows": {"5": {"fingerprint": "short", "since": 1}}}',
            '{"mail_pid": 1}',
        ],
    )
    def test_an_unreadable_file_is_an_empty_clock_and_says_so(
        self, tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "c.json"
        path.write_text(text)
        assert ComposeClock(path).load() == SightingClock(None, {})
        assert "compose clock" in caplog.text

    def test_a_save_replaces_the_file_whole(self, tmp_path: Path) -> None:
        store = ComposeClock(tmp_path / "home" / "c.json")
        store.save(SightingClock(mail_pid=1, windows={1: ClockEntry(FP, 1.0)}))
        store.save(SightingClock(mail_pid=2, windows={}))
        assert store.load() == SightingClock(mail_pid=2, windows={})
        assert [p.name for p in (tmp_path / "home").iterdir()] == ["c.json"]
