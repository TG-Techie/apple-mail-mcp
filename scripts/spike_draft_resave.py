"""Measurement scaffolding for docs/research/draft-resave-spike.md.

Not a test and not part of the server: a manual probe of whether a
saved draft keeps its Mail id, run by hand against real Mail.app.

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<test account> \\
        uv run python scripts/spike_draft_resave.py <variant> --log <jsonl>

Variants (see the note for what each measures):
    v1-named    connector.create_draft, seed new, from_account = test account
    v1-none     connector.create_draft, seed new, from_account = None
    v2-named    make new outgoing message with the sender in the creation
                properties, "Name <addr>" form as _resolve_account_to_sender
                gives it; recipients after; save
    v2-bare     the same with the account's bare address
    v3-reply    connector.create_draft, seed reply with a note (the
                visible-window path, closed with Save); needs --seed-id
    v3-fresh    make new outgoing message visible:true with the sender in the
                properties, closed through _save_compose_window_as_draft
    v4-update   v1-named, then the draft_update tool on it, patching nothing
    v5-reveal   v1-named, then its hidden compose window is made visible
                (Mail's window `visible`) and closed through
                _salvage_compose_to_draft: an intervention on the dictionary
                path's compose session (the note's Observation 7)
    sweep       trash every draft whose subject this spike created (from the
                log) until none has appeared for a quiet period

Since 2026-09-27 connector.create_draft saves every draft from a compose
window closed with Save, as the note recommended. So v1-named, v1-none
and v4-update now measure that path, not the dictionary save the note's
Observations 2, 3 and 6 measured, and v5-reveal finds no hidden window
to reveal. v2 still saves through the dictionary, as written here.

Every run: create, then poll the Drafts entries carrying the run's subject
every 2 s until 45 s after the create returned (id, Message-ID, sender,
account), then read the windows of that name, the outgoing-message
counts, any sheet, the copies of that subject already in Trash, and
whether an integration pytest is running (its session sweep trashes
ZZZ-AMM-INTEG- drafts, these included), then move the run's drafts to
Trash (Mail's delete) and read that none is left. Every recipient is
probe@example.com; nothing is sent.

Everything that changes Mail goes through AppleMailConnector (its
cross-process lock serialises it against other callers); the polls and
counts are read-only and run as bare osascript. Nothing here names an
account or an address: the account comes from MAIL_TEST_ACCOUNT and its
addresses from Mail at run time, and what is printed and logged says
"test"/"other" rather than the sender string.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

PREFIX = "ZZZ-AMM-INTEG-spike-"
PROBE_TO = "probe@example.com"
POLL_INTERVAL_S = 2.0
POLL_FOR_S = 45.0
SWEEP_QUIET_S = 35.0
SWEEP_MAX_S = 150.0


def _q(text: str) -> str:
    return f'"{escape_applescript_string(text)}"'


def osa_read(script: str, timeout: float = 20.0) -> str:
    """Read-only probe as bare osascript; an error comes back as text."""
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script, text=True, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "ERR:timeout"
    if r.returncode != 0:
        return "ERR:" + r.stderr.strip()
    return r.stdout.strip()


class Identity:
    """The test account's sender strings, read from Mail, so a read-back
    sender can be logged as a token instead of as itself."""

    def __init__(self, connector: AppleMailConnector, account: str) -> None:
        self.account = account
        self.named = connector._resolve_account_to_sender(account)
        self.bare = ""
        self.other: list[str] = []
        for acc in connector.list_accounts():
            emails = [str(e).lower() for e in acc.get("email_addresses") or []]
            if acc.get("name") == account:
                self.bare = emails[0]
            else:
                self.other.extend(emails)

    def sender_token(self, sender: str) -> str:
        s = sender.strip()
        low = s.lower()
        if s == self.named:
            return "test:named"
        if low == self.bare:
            return "test:bare"
        if self.bare and self.bare in low:
            return "test:other-form"
        if any(o in low for o in self.other):
            return "other"
        if not s:
            return "empty"
        return "unknown"

    def account_token(self, name: str) -> str:
        return "test" if name == self.account else ("?" if not name else "other")


def drafts_with_subject(subject: str) -> list[dict[str, str]] | str:
    out = osa_read(f'''
tell application "Mail"
    set out to ""
    set ms to (messages of drafts mailbox whose subject is {_q(subject)})
    repeat with m in ms
        try
            set accName to ""
            try
                set accName to name of account of mailbox of m
            end try
            set out to out & (id of m as text) & tab & (message id of m) & tab & (sender of m) & tab & accName & linefeed
        on error errMsg
            set out to out & "ERRITEM" & tab & errMsg & linefeed
        end try
    end repeat
    return out
end tell''')
    if out.startswith("ERR:"):
        return out
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "ERRITEM":
            rows.append({"error": parts[1] if len(parts) > 1 else ""})
            continue
        while len(parts) < 4:
            parts.append("")
        rows.append({"id": parts[0], "msgid": parts[1], "sender": parts[2], "account": parts[3]})
    return rows


def outgoing_count(subject: str | None = None) -> str:
    target = "outgoing messages" if subject is None else f"(outgoing messages whose subject is {_q(subject)})"
    return osa_read(f'tell application "Mail" to return (count of {target}) as text')


def windows_named(subject: str) -> dict[str, str]:
    return {
        "mail": osa_read(f'tell application "Mail" to return (count of (windows whose name is {_q(subject)})) as text'),
        "system_events": osa_read(
            'tell application "System Events" to tell application process "Mail" '
            f'to return (count of (windows whose name is {_q(subject)})) as text'
        ),
    }


def total_windows() -> str:
    return osa_read('tell application "System Events" to tell application process "Mail" to return (count of windows) as text')


def sheets_open() -> str:
    """Any sheet on any Mail window, read-only: a dialog is recorded, never clicked."""
    return osa_read('''
tell application "System Events" to tell application process "Mail"
    set out to ""
    repeat with w in windows
        try
            if exists sheet 1 of w then set out to out & (name of w) & " | "
        end try
    end repeat
    return out
end tell''')


def trash_count(subject: str) -> str:
    """Copies of ``subject`` in Trash. Read before this run's own cleanup,
    a non-zero count means something else deleted a draft of this run:
    the integration suite's session sweep deletes every test-account
    draft whose subject starts with ZZZ-AMM-INTEG-, this spike's included."""
    return osa_read(
        f'tell application "Mail" to return (count of (messages of trash mailbox whose subject is {_q(subject)})) as text'
    )


def integration_pytest_running() -> int:
    """How many pytest processes with --run-integration are running (any
    worktree): their session sweeps can trash this run's drafts."""
    out = subprocess.run(["/bin/ps", "-Ao", "command"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if "pytest" in line and "--run-integration" in line and "uv run" not in line)


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


class Run:
    def __init__(self, ident: Identity, variant: str, subject: str) -> None:
        self.ident = ident
        self.rec: dict[str, Any] = {
            "variant": variant, "subject": subject, "started": stamp(),
        }
        self.subject = subject
        self.t0 = time.monotonic()

    def t(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def sample(self) -> dict[str, Any]:
        rows = drafts_with_subject(self.subject)
        t = self.t()
        if isinstance(rows, str):
            return {"t": t, "error": rows}
        return {
            "t": t,
            "drafts": [
                r if "error" in r else {
                    "id": r["id"],
                    "msgid": r["msgid"],
                    "sender": self.ident.sender_token(r["sender"]),
                    "account": self.ident.account_token(r["account"]),
                }
                for r in rows
            ],
        }

    def poll(self) -> None:
        samples = []
        deadline = self.t0 + self.rec.get("returned_t", 0) + POLL_FOR_S
        next_at = time.monotonic()
        while time.monotonic() <= deadline:
            samples.append(self.sample())
            next_at += POLL_INTERVAL_S
            time.sleep(max(0.0, next_at - time.monotonic()))
        self.rec["samples"] = samples

    def after(self) -> None:
        self.rec["windows_after_poll"] = windows_named(self.subject)
        self.rec["outgoing_after_poll"] = outgoing_count()
        self.rec["outgoing_with_subject_after_poll"] = outgoing_count(self.subject)
        self.rec["sheets_after_poll"] = sheets_open()
        self.rec["trash_count_after_poll"] = trash_count(self.subject)
        self.rec["integration_pytest_after_poll"] = integration_pytest_running()


def trash_subject(connector: AppleMailConnector, subject: str) -> dict[str, Any]:
    """Move every draft carrying ``subject`` to Trash through the
    connector (Mail's delete of a draft moves it to Trash), then read."""
    from apple_mail_mcp.exceptions import MailDraftNotFoundError

    trashed, missing = [], []
    rows = drafts_with_subject(subject)
    if isinstance(rows, str):
        return {"error": rows}
    for r in rows:
        if "id" not in r:
            continue
        try:
            connector.delete_draft(r["id"])
            trashed.append(r["id"])
        except MailDraftNotFoundError:
            missing.append(r["id"])
    time.sleep(2)
    left = drafts_with_subject(subject)
    return {
        "trashed": trashed,
        "not_found": missing,
        "left": left if isinstance(left, str) else [r.get("id") for r in left],
    }


def create_raw(connector: AppleMailConnector, subject: str, sender: str) -> str:
    """Variant 2: the sender inside the creation properties, recipients
    after, save, the new id found by the connector's own Drafts diff."""
    c = connector
    script = f'''
tell application "Mail"
    set beforeIds to (id of every message of drafts mailbox)
    set theMessage to make new outgoing message with properties {{subject:{_q(subject)}, content:"x", sender:{_q(sender)}, visible:false}}
    make new to recipient at end of to recipients of theMessage with properties {{address:{_q(PROBE_TO)}}}
    save theMessage
    set newDraftId to ""
    repeat with attempt from 1 to {c._DRAFT_APPEAR_POLLS}
        delay {c._DRAFT_APPEAR_INTERVAL_S}
        set afterIds to (id of every message of drafts mailbox)
        repeat with candRef in afterIds
            set candId to contents of candRef
            if candId is not in beforeIds then
                set newDraftId to (candId as text)
                exit repeat
            end if
        end repeat
        if newDraftId is not "" then exit repeat
    end repeat
    if newDraftId is not "" then delay {c._DRAFT_SETTLE_S}
    return newDraftId
end tell
'''
    return c._run_applescript(script).strip()


def open_fresh_visible(connector: AppleMailConnector, subject: str, sender: str) -> dict[str, Any]:
    """Variant 3b, first half: a visible fresh compose window with the
    sender in the creation properties, found by the connector's
    counted-name window diff, with the Drafts ids before it."""
    body = f'''
tell application "System Events"
    tell application process "Mail"
        set beforeNames to name of windows
    end tell
end tell
tell application "Mail"
    activate
    set beforeIds to (id of every message of drafts mailbox)
    set theMessage to make new outgoing message with properties {{subject:{_q(subject)}, content:"x", sender:{_q(sender)}, visible:true}}
    make new to recipient at end of to recipients of theMessage with properties {{address:{_q(PROBE_TO)}}}
end tell
{connector._as_new_compose_window_block()}
tell application "Mail"
    set resultData to {{|window|:newName, |before_ids|:beforeIds}}
end tell
'''
    raw = connector._run_applescript(_wrap_as_json_script(body, timeout=connector.timeout))
    return dict(parse_applescript_json(raw))


def run_variant(connector: AppleMailConnector, ident: Identity, variant: str, seed_id: str | None) -> dict[str, Any]:
    hexid = uuid.uuid4().hex[:8]
    subject = f"{PREFIX}{variant}-{hexid}"
    if variant == "v3-reply":
        if not seed_id:
            raise SystemExit("v3-reply needs --seed-id")
        seed_subject = osa_read(f'''
tell application "Mail"
    repeat with mb in mailboxes of account {_q(ident.account)}
        try
            return subject of (first message of mb whose id is {seed_id})
        end try
    end repeat
    return "ERR:seed not in the test account"
end tell''')
        if seed_subject.startswith("ERR:"):
            raise SystemExit(seed_subject)
        subject = f"Re: {seed_subject}"

    pre_windows = windows_named(subject)
    run = Run(ident, variant, subject)
    run.rec["outgoing_before"] = outgoing_count()
    run.rec["windows_total_before"] = total_windows()
    run.rec["windows_named_before"] = pre_windows
    run.rec["sheets_before"] = sheets_open()
    run.rec["trash_count_before"] = trash_count(subject)
    run.rec["integration_pytest_before"] = integration_pytest_running()
    run.t0 = time.monotonic()
    run.rec["create_started"] = stamp()

    extra: dict[str, Any] = {}
    try:
        if variant in ("v1-named", "v1-none", "v4-update"):
            res = connector.create_draft(
                seed="new", to=[PROBE_TO], subject=subject, body="x",
                from_account=None if variant == "v1-none" else ident.account,
            )
            created_id = res["draft_id"]
            if variant == "v4-update":
                extra["v1_returned_t"] = run.t()
                extra["v1_id"] = created_id
                extra["v1_first_sample"] = run.sample()
                from apple_mail_mcp import server
                from apple_mail_mcp.tools.drafts import draft_update

                server.mail = connector
                upd = draft_update(draft_id=created_id)
                extra["update_result"] = {k: upd.get(k) for k in ("success", "draft_id", "warning", "error", "error_type")}
                created_id = str(upd.get("draft_id") or "")
        elif variant == "v5-reveal":
            res = connector.create_draft(
                seed="new", to=[PROBE_TO], subject=subject, body="x",
                from_account=ident.account,
            )
            created_id = res["draft_id"]
            extra["v1_returned_t"] = run.t()
            extra["v1_first_sample"] = run.sample()
            extra["reveal"] = connector._run_applescript(f'''
tell application "Mail"
    set ws to (windows whose name is {_q(subject)})
    if (count of ws) is 0 then return "NO_MAIL_WINDOW"
    set visible of item 1 of ws to true
end tell
delay 0.5
tell application "System Events" to tell application process "Mail"
    return "SE_WINDOWS=" & ((count of (windows whose name is {_q(subject)})) as text)
end tell''').strip()
            extra["revealed_t"] = run.t()
            extra["sheets_after_reveal"] = sheets_open()
            if "SE_WINDOWS=1" in extra["reveal"]:
                extra["salvage"] = connector._salvage_compose_to_draft(subject)
            extra["closed_t"] = run.t()
            extra["after_close_sample"] = run.sample()
            extra["after_close_windows"] = windows_named(subject)
            extra["after_close_outgoing_with_subject"] = outgoing_count(subject)
        elif variant in ("v2-named", "v2-bare"):
            sender = ident.named if variant == "v2-named" else ident.bare
            created_id = create_raw(connector, subject, sender)
        elif variant == "v3-reply":
            res = connector.create_draft(
                seed="reply", seed_id=seed_id, to=[PROBE_TO], body="spike note",
                from_account=ident.account, send_now=False,
            )
            created_id = res["draft_id"]
        elif variant == "v3-fresh":
            opened = open_fresh_visible(connector, subject, ident.named)
            extra["window"] = "same as subject" if opened["window"] == subject else "OTHER NAME"
            extra["opened_t"] = run.t()
            before_ids = [int(i) for i in opened.get("before_ids") or []]
            created_id = connector._save_compose_window_as_draft(str(opened["window"]), subject, before_ids)
        else:
            raise SystemExit(f"unknown variant {variant}")
    except Exception as exc:  # recorded, then the run still reads and cleans up
        run.rec["create_error"] = f"{type(exc).__name__}: {exc}"
        created_id = ""
    run.rec["returned_t"] = run.t()
    run.rec["created_id"] = created_id
    run.rec.update(extra)
    run.poll()
    run.after()
    run.rec["cleanup"] = trash_subject(connector, subject)
    run.rec["windows_after_cleanup"] = windows_named(subject)
    run.rec["outgoing_after_cleanup"] = outgoing_count()
    run.rec["finished"] = stamp()
    return run.rec


def sweep(connector: AppleMailConnector, log: Path) -> dict[str, Any]:
    subjects = sorted({
        json.loads(line)["subject"] for line in log.read_text().splitlines() if line.strip()
    })
    started = last_found = time.monotonic()
    found: list[dict[str, Any]] = []
    while True:
        this_round = 0
        for s in subjects:
            res = trash_subject(connector, s)
            if res.get("trashed"):
                this_round += len(res["trashed"])
                found.append({"t": round(time.monotonic() - started, 1), "subject": s, **res})
        now = time.monotonic()
        if this_round:
            last_found = now
        if now - last_found >= SWEEP_QUIET_S or now - started >= SWEEP_MAX_S:
            break
        time.sleep(5)
    return {
        "variant": "sweep", "subject": "", "finished": stamp(),
        "subjects": len(subjects), "found": found,
        "windows": {s: windows_named(s) for s in subjects},
        "outgoing": outgoing_count(),
    }


def summarise(rec: dict[str, Any]) -> str:
    lines = [f"== {rec['variant']}  {rec.get('create_started', rec.get('finished'))}"]
    if rec["variant"] == "sweep":
        lines.append(json.dumps(rec, indent=1))
        return "\n".join(lines)
    lines.append(f"created {rec.get('created_id')!r} returned at t={rec.get('returned_t')}s"
                 + (f"  ERROR {rec['create_error']}" if rec.get("create_error") else ""))
    for k in ("v1_id", "v1_returned_t", "v1_first_sample", "update_result", "window", "opened_t",
              "reveal", "revealed_t", "sheets_after_reveal", "salvage", "closed_t", "after_close_sample", "after_close_windows",
              "after_close_outgoing_with_subject"):
        if k in rec:
            lines.append(f"  {k}: {rec[k]}")
    last = None
    for s in rec.get("samples", []):
        key = json.dumps(s.get("drafts", s.get("error")))
        if key != last:
            lines.append(f"  t={s['t']:>5}s {key}")
            last = key
    lines.append(f"  outgoing before={rec['outgoing_before']} after-poll={rec['outgoing_after_poll']} "
                 f"after-cleanup={rec['outgoing_after_cleanup']}; with this subject after-poll="
                 f"{rec['outgoing_with_subject_after_poll']}")
    lines.append(f"  windows named before={rec['windows_named_before']} after-poll={rec['windows_after_poll']} "
                 f"after-cleanup={rec['windows_after_cleanup']}")
    lines.append(f"  sheets before=[{rec['sheets_before']}] after-poll=[{rec['sheets_after_poll']}]")
    lines.append(f"  in Trash before={rec['trash_count_before']} after-poll(before own cleanup)={rec['trash_count_after_poll']}; "
                 f"integration pytest running before={rec['integration_pytest_before']} after-poll={rec['integration_pytest_after_poll']}")
    lines.append(f"  cleanup {rec['cleanup']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("variant")
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--seed-id")
    args = p.parse_args()
    if os.getenv("MAIL_TEST_MODE", "").lower() != "true" or not os.getenv("MAIL_TEST_ACCOUNT"):
        print("refusing: set MAIL_TEST_MODE=true and MAIL_TEST_ACCOUNT", file=sys.stderr)
        return 2
    connector = AppleMailConnector(timeout=90)
    if args.variant == "sweep":
        rec = sweep(connector, args.log)
        print(summarise(rec))
        return 0
    ident = Identity(connector, os.environ["MAIL_TEST_ACCOUNT"])
    rec = run_variant(connector, ident, args.variant, args.seed_id)
    with args.log.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(summarise(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
