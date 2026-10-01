"""Argument handling for ``mail-serve``, the resident daemon's entry point.

Nothing here starts a server: ``FastMCP.run`` is replaced with a recorder,
so what is asserted is what the entry point asks fastmcp for. That the
daemon then actually serves is tests/e2e/test_daemon_proxy.py's job.
"""

from __future__ import annotations

from typing import Any

import pytest

from apple_mail_mcp import restarter, serve, tender


@pytest.fixture(autouse=True)
def tenders_started(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A real tender would run passes against Mail; this records the
    interval each start asked for instead."""
    started: list[float] = []
    monkeypatch.setattr(serve, "start_tender", lambda interval_s: started.append(interval_s))
    return started


@pytest.fixture(autouse=True)
def restarters_started(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, int]]:
    """A real restarter would probe Mail, and might restart it; this
    records what each start asked for instead."""
    started: list[tuple[float, int]] = []
    monkeypatch.setattr(
        serve,
        "start_restarter",
        lambda interval_s, restart_hour: started.append((interval_s, restart_hour)),
    )
    return started


def test_default_port_is_the_provisional_allocation() -> None:
    assert serve.DEFAULT_PORT == 41108
    assert serve.build_parser().parse_args([]).port == 41108


def test_port_flag_is_read_as_an_integer() -> None:
    assert serve.build_parser().parse_args(["--port", "5555"]).port == 5555


def test_non_integer_port_is_refused() -> None:
    with pytest.raises(SystemExit) as exc:
        serve.build_parser().parse_args(["--port", "fast"])
    assert exc.value.code == 2


def test_unknown_argument_is_refused() -> None:
    """There is no stdio mode to ask for; an unknown flag fails loudly
    rather than being ignored."""
    with pytest.raises(SystemExit) as exc:
        serve.build_parser().parse_args(["--transport", "stdio"])
    assert exc.value.code == 2


def test_main_serves_http_on_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from apple_mail_mcp import server

    calls: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    monkeypatch.setattr(server.mcp, "run", record)
    assert serve.main(["--port", "5555"]) == 0
    assert calls == [
        {
            "args": (),
            "transport": "http",
            "host": "127.0.0.1",
            "port": 5555,
            "path": "/mcp",
        }
    ]


def test_main_defaults_to_the_provisional_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from apple_mail_mcp import server

    ports: list[int] = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: ports.append(kw["port"]))
    serve.main([])
    assert ports == [41108]


class TestTending:
    """The daemon tends Mail's compose windows (tender.py) unless told not
    to; the e2e daemon, which must not reach Mail, is told not to."""

    @pytest.fixture(autouse=True)
    def _no_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apple_mail_mcp import server

        monkeypatch.setattr(server.mcp, "run", lambda **kw: None)

    def test_starts_by_default_at_the_one_interval(
        self, tenders_started: list[float]
    ) -> None:
        serve.main([])
        assert tenders_started == [tender.TEND_INTERVAL_S]

    def test_takes_its_interval_from_the_flag(self, tenders_started: list[float]) -> None:
        serve.main(["--tend-interval", "90"])
        assert tenders_started == [90.0]

    def test_zero_turns_it_off(self, tenders_started: list[float]) -> None:
        serve.main(["--tend-interval", "0"])
        assert tenders_started == []

    def test_a_negative_interval_is_refused(self) -> None:
        with pytest.raises(SystemExit) as exc:
            serve.build_parser().parse_args(["--tend-interval", "-5"])
        assert exc.value.code == 2


class TestRestartingMail:
    """The daemon probes Mail and restarts it when it stops answering and
    once a day (restarter.py), unless told not to; the e2e daemon, which
    must not reach Mail, is told not to."""

    @pytest.fixture(autouse=True)
    def _no_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apple_mail_mcp import server

        monkeypatch.setattr(server.mcp, "run", lambda **kw: None)

    def test_starts_by_default(self, restarters_started: list[tuple[float, int]]) -> None:
        serve.main([])
        assert restarters_started == [(restarter.PROBE_INTERVAL_S, restarter.RESTART_HOUR)]

    def test_takes_its_interval_and_hour_from_the_flags(
        self, restarters_started: list[tuple[float, int]]
    ) -> None:
        serve.main(["--mail-probe-interval", "120", "--mail-restart-hour", "23"])
        assert restarters_started == [(120.0, 23)]

    def test_zero_turns_it_off(self, restarters_started: list[tuple[float, int]]) -> None:
        serve.main(["--mail-probe-interval", "0"])
        assert restarters_started == []

    def test_it_is_apart_from_tending(
        self,
        restarters_started: list[tuple[float, int]],
        tenders_started: list[float],
    ) -> None:
        serve.main(["--tend-interval", "0"])
        assert tenders_started == [] and len(restarters_started) == 1
        restarters_started.clear()
        serve.main(["--mail-probe-interval", "0"])
        assert restarters_started == [] and len(tenders_started) == 1

    @pytest.mark.parametrize(
        "argv",
        [
            ["--mail-probe-interval", "-5"],
            ["--mail-restart-hour", "24"],
            ["--mail-restart-hour", "-1"],
            ["--mail-restart-hour", "4.5"],
        ],
    )
    def test_out_of_range_values_are_refused(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            serve.build_parser().parse_args(argv)
        assert exc.value.code == 2
