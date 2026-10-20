"""What the connector knows of a compose window and of a sent copy, and the
checks made on a composition's arguments, recipients and sent copy.
"""

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compose_ledger import Closed, Closer, LeftOpen, WindowOperation
from .exceptions import MailAppleScriptError
from .outbound_allowlist import assert_recipients_allowed_for_send
from .utils import SANITIZE_MAX_LENGTH


@dataclass(frozen=True)
class WindowSnapshot:
    """Mail's process and the ids of its windows, read before a compose
    window is opened, so a window that opens without its opening script
    reporting it can still be told apart (``_adopt_unreported_windows``)."""

    mail_pid: int
    window_ids: frozenset[int]


def refuse_overlong_body(body: str) -> None:
    """Refuse a caller's message body longer than ``sanitize_input``
    carries. Every body reaches Mail's pasteboard through it, and it cuts
    at SANITIZE_MAX_LENGTH without a word, so a longer body would go out
    or be saved shortened. Called where a body enters the connector
    (``create_draft``, ``_send_html_email``), before anything is looked
    up or composed."""
    if len(body) > SANITIZE_MAX_LENGTH:
        raise ValueError(
            f"body is {len(body)} characters; a message body carries at "
            f"most {SANITIZE_MAX_LENGTH}. Nothing was composed, saved or "
            "sent."
        )


def existing_files(attachment_paths: list[Path] | None) -> list[Path]:
    """The files a caller asked to attach, each checked to exist before
    anything is composed; ``FileNotFoundError`` names the first that
    does not."""
    files = [Path(p) for p in attachment_paths or []]
    for f in files:
        if not f.is_file():
            raise FileNotFoundError(f"attachment not found: {f}")
    return files


# The mailbox a composition's ending is looked up in: the draft a save
# made, or the copy a send filed. Its ids are taken before the window
# opens, and the ending is the entry with the window's subject whose id
# was not among them. The subject alone matched an older message of the
# same subject: a send's files were checked on a Sent copy sent hours
# before, and reported missing from a message that carried them.
ENDING_MAILBOX: dict[WindowOperation, str] = {
    "save": "drafts mailbox",
    "send": "sent mailbox",
}


@dataclass(frozen=True)
class ComposeWindow:
    """An open compose window, as the composition's opening script
    reports it: the window's name, the subject and recipients Mail holds
    for its message after every override, and the ids of the mailbox its
    ending is looked up in as they were before it opened (Drafts for a
    save, Sent for a send; ``ENDING_MAILBOX``). Then what identifies it
    to the compose ledger: Mail's id for the window and Mail's process
    id (None when Mail did not say), and the ledger record it was entered
    under (None when the ledger could not be written)."""

    name: str
    subject: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    before_ids: list[int]
    window_id: int | None = None
    mail_pid: int | None = None
    record_id: str | None = None


def compose_window_from_report(report: dict[str, Any]) -> ComposeWindow:
    """The opening script's ``resultData`` as a ``ComposeWindow``; Mail's
    ``missing value`` groups arrive as empty lists, and an id of 0 is no
    id."""
    groups = {group: [str(a) for a in report.get(group) or []] for group in ("to", "cc", "bcc")}
    return ComposeWindow(
        name=str(report["window"]),
        subject=str(report["subject"]),
        to=groups["to"],
        cc=groups["cc"],
        bcc=groups["bcc"],
        before_ids=[int(i) for i in report.get("before_ids") or []],
        window_id=int(report.get("window_id") or 0) or None,
        mail_pid=int(report.get("mail_pid") or 0) or None,
    )


# What a send Mail accepted finds of the copy it filed in Sent. Exactly
# one of the two: the copy, found by identity, or why none was. With the
# files pasted, that makes three endings and no others (``_sent_ending``):
# the copy carries every file, and the send returns its ids; it lacks
# one, and the send raises that the message WAS sent; it was not
# identified, and the send returns no id and a warning. A copy not found
# has no files to be missing, and an id is never returned unverified.
@dataclass(frozen=True)
class SentMessageCopy:
    """The copy a send filed in Sent: the one message in Sent with the
    window's subject whose id was not there before the window opened.
    ``rfc_message_id`` is bare, as the read tools emit it."""

    mail_id: str
    rfc_message_id: str
    attachment_names: tuple[str, ...]


@dataclass(frozen=True)
class SentMessageCopyUnidentified:
    """A send whose copy in Sent could not be told: none appeared in time,
    more than one did, or looking failed; ``why`` says which. Mail accepted
    the message either way: the verified send saw its window close after
    Send with no sheet on it. Nothing after that has been seen of it."""

    why: str


def sent_result(
    copy: SentMessageCopy | SentMessageCopyUnidentified, *, files_pasted: bool
) -> dict[str, Any]:
    """What a send Mail accepted returns: the copy's Mail id, which
    ``get_messages`` takes, and its RFC Message-ID; or, for a copy not
    identified, no id and one warning saying why, and that the files,
    when there were any, are unverified rather than missing."""
    if isinstance(copy, SentMessageCopy):
        return {
            "draft_id": "",
            "sent_message_id": copy.mail_id,
            "sent_rfc_message_id": copy.rfc_message_id,
        }
    unverified = ", and the files it was sent with are unverified" if files_pasted else ""
    return {
        "draft_id": "",
        "sent_message_id": "",
        "sent_rfc_message_id": "",
        "warnings": [
            "Mail accepted the message (its compose window closed after "
            "Send, with no sheet), but its copy in Sent could not be "
            f"identified: {copy.why}. No id is returned{unverified}; look "
            "in Sent and in Mail's Outbox before sending it again."
        ],
    }


def closing_of(raw: str, *, by: Closer, at: float) -> Closed | None:
    """How a salvage's or a discard's outcome ended its window: "SALVAGED"
    (with any note after it), "DISCARDED", or "NO_WINDOW" (it was gone
    when the close came). None for anything else: the close failed, and
    the window may still be open."""
    if raw.startswith("SALVAGED"):
        return Closed(how="salvaged", by=by, at=at)
    if raw == "DISCARDED":
        return Closed(how="discarded", by=by, at=at)
    if raw == "NO_WINDOW":
        return Closed(how="gone", by=by, at=at)
    return None


def composition_closing(raw: str, *, failure: str) -> LeftOpen | Closed:
    """How a composition's window ended, by what its own salvage or
    discard reported; a close that failed leaves it open, with
    ``failure``."""
    now = time.time()
    closed = closing_of(raw, by="composition", at=now)
    return closed if closed is not None else LeftOpen(failure=failure, at=now)


def validate_compose_args(
    seed: str,
    seed_id: str | None,
    to: list[str] | None,
    subject: str | None,
) -> None:
    """Validate the per-seed argument requirements of a composition,
    where a caller's arguments enter the connector (``create_draft``,
    ``_send_html_email``). Raises ValueError with a specific message
    on the first violation. (#193)
    """
    if seed not in ("new", "reply", "forward"):
        raise ValueError(f"seed must be 'new', 'reply', or 'forward'; got {seed!r}")
    if seed in ("reply", "forward"):
        if not seed_id:
            raise ValueError(f"seed_id is required for seed={seed!r}")
    else:  # seed == "new"
        if not to:
            raise ValueError("'to' is required when seed='new'")
        if not subject:
            raise ValueError("'subject' is required when seed='new'")


def gate_named_recipients(
    seed: str,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
) -> None:
    """The outbound allowlist on a send's recipients as the caller
    named them, before Mail is touched; ``MailOutboundDisallowedError``
    on failure. A reply's group left None is Mail's to fill from the
    message replied to, which nothing here can see: it is judged with
    every other recipient once the window holds it
    (``gate_compose_recipients``). No other seed derives a recipient,
    so what it names is everyone it sends to, and it must name
    someone."""
    if seed != "reply":
        assert_recipients_allowed_for_send(to, cc, bcc, seed=seed)
        return
    named = [addr for group in (to, cc, bcc) for addr in group or []]
    if named:
        assert_recipients_allowed_for_send(named, None, None)


def gate_compose_recipients(window: ComposeWindow) -> None:
    """HARD POLICY GATE on the recipients an open compose window will
    send to, as read back from the outgoing-message model after every
    override: those the caller named and those Mail derived. No
    exceptions. ``MailOutboundDisallowedError`` on failure, before
    anything is pasted or sent; the composition then discards the
    window (``_compose``)."""
    assert_recipients_allowed_for_send(
        window.to or None, window.cc or None, window.bcc or None, seed="new"
    )


def check_sent_attachments(copy: SentMessageCopy, names: list[str]) -> None:
    """The copy a send filed must carry every file the composition
    pasted, by name. It may carry more: a forward's has the
    original's files as well. A file missing raises, saying the
    message WAS sent, so the caller inspects that copy before sending
    again."""
    carried = list(copy.attachment_names)
    missing = list((Counter(names) - Counter(carried)).elements())
    if missing:
        raise MailAppleScriptError(
            f"send with attachments: message WAS sent, but the sent "
            f"copy lacks {missing} of the files attached (it carries "
            f"{carried}) — inspect that copy in Sent (id "
            f"{copy.mail_id}) before resending."
        )
