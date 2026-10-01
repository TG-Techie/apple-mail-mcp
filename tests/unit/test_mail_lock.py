"""The cross-process Mail automation lock (src/apple_mail_mcp/mail_lock.py).

Real flock(2) on a file under the per-test data home; nothing here
reaches Mail."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from apple_mail_mcp import mail_lock


def test_lives_under_the_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "elsewhere"))
    assert mail_lock.lock_path() == tmp_path / "elsewhere" / "mail_automation.lock"


def test_held_yields_true_and_creates_the_file() -> None:
    with mail_lock.held(1.0) as got:
        assert got is True
        assert mail_lock.lock_path().exists()


def test_is_free_again_after_the_block() -> None:
    with mail_lock.held(1.0):
        pass
    fh = mail_lock.acquire(0.5)
    assert fh is not None
    mail_lock.release(fh)


def test_another_thread_of_this_process_is_kept_out() -> None:
    """The fresh open on every call is what makes flock exclude threads
    of one process from each other, not only processes."""
    holding = threading.Event()
    done = threading.Event()

    def hold() -> None:
        with mail_lock.held(1.0):
            holding.set()
            done.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(5)
        start = time.monotonic()
        with mail_lock.held(0.3) as got:
            assert got is False
        assert time.monotonic() - start >= 0.29
    finally:
        done.set()
        t.join(5)


def test_another_process_holding_it_is_waited_out_then_refused() -> None:
    path = mail_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,time\n"
                f"fh=open({str(path)!r},'w')\n"
                "fcntl.flock(fh, fcntl.LOCK_EX)\n"
                "print('held',flush=True)\n"
                "time.sleep(10)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert mail_lock.acquire(0.3) is None
    finally:
        holder.kill()
        holder.wait()
    fh = mail_lock.acquire(1.0)
    assert fh is not None
    mail_lock.release(fh)
