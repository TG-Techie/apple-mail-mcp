"""
AppleScript-based connector for Apple Mail.
"""

import logging
import re
import subprocess
import time
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date as _date
from datetime import timedelta as _timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import IO, Any, Literal, cast

from imapclient.exceptions import IMAPClientError, LoginError

from . import mail_lock
from .compose_ledger import (
    Closed,
    Closer,
    ComposeLedger,
    LeftOpen,
    Seed,
    WindowOperation,
)
from .compose_tending import (
    RECORD_RETENTION_S,
    TEND_GRACE_S,
    TendAction,
    TendReport,
    inventory_from_report,
    plan_tending,
)
from .drafts import _validate_draft_id
from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailComposeLedgerError,
    MailComposeWindowError,
    MailDraftNotFoundError,
    MailDraftNotSettledError,
    MailError,
    MailImapMoveUnsupportedError,
    MailImapRequiredError,
    MailImapTrashNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotEmptyError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
    MailRuleChangedError,
    MailRuleNotFoundError,
    MailTimeoutError,
    MailUnsupportedGmailSystemLabelError,
    MailUnsupportedRuleActionError,
)
from .imap_connector import ImapConnectionPool, ImapConnector
from .keychain import get_imap_password
from .outbound_allowlist import (
    assert_forward_targets_allowed,
    assert_recipients_allowed_for_send,
)
from .utils import (
    SANITIZE_MAX_LENGTH,
    applescript_account_clause,
    applescript_iso_date_statements,
    distinct_filenames,
    escape_applescript_string,
    format_recipients,
    get_flag_index,
    parse_applescript_json,
    safe_attachment_filename,
    sanitize_input,
    validate_email,
)

# Exception classes that trigger AppleScript fallback per the graceful-
# degradation invariants (docs/research/imap-auth-options-decision.md).
# OSError covers socket.timeout too. ValueError and MailAccountNotFoundError
# are deliberately NOT in this tuple — they indicate caller/config errors
# and must surface, not be papered over by fallback.
_IMAP_FALLBACK_EXCS: tuple[type[Exception], ...] = (
    MailKeychainEntryNotFoundError,
    MailKeychainAccessDeniedError,
    OSError,
    LoginError,
    IMAPClientError,
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
)

logger = logging.getLogger(__name__)

# Strict ISO 8601 YYYY-MM-DD — search_messages's date_from/date_to filters
# reject anything else to prevent AppleScript injection via the date clause.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Threshold (seconds) above which the AppleScript search path emits an
# INFO-level recommendation to enable IMAP delegation. Calibrated against
# the post-#32 baseline: a 50-message search on a 200+ msg mailbox runs
# in well under a second; sustained >5s suggests the user is hitting the
# AppleScript fallback against a mailbox where IMAP would help.
_SLOW_SEARCH_THRESHOLD_SEC = 5.0

# One-character body seed for a compose window made by `make new
# outgoing message`. Such a window with an EMPTY body had a WebArea that
# refused keyboard focus by every route tried (observed 2026-09-05; not
# reproduced since, see docs/research/paste-focus-failed.md, so it
# stays). The seed reaches neither a sent message nor a saved draft: the
# composition selects the whole body of a fresh message and deletes it
# before pasting (``_PASTE_CARET_KEYS["replace"]``), empty body or not,
# because Mail keeps anything set through ``content`` inside a
# <blockquote type="cite">.
_BODY_SEED = " "

# Where a clipboard paste lands in a compose window's body, as the keys
# pressed just before cmd+v. Measured through the loopback read-back on
# 2026-09-27 (docs/research/icloud-draft-resync.md, Observation 10):
#   above   - cmd+up: above what Mail wrote (a reply's quote, a forward's
#             header block and attachments), which stays as Mail made it.
#   replace - cmd+a, delete: the whole body. On a fresh message that is
#             the seed inside Mail's URLShare wrapper; pasting above it
#             instead sent an empty <blockquote type="cite">, which iOS
#             Mail draws as a purple quoted-reply bar.
#   end     - cmd+down: after everything, where the composition pastes
#             files once the body is in.
_PastePlacement = Literal["above", "replace", "end"]
_PASTE_CARET_KEYS: dict[_PastePlacement, str] = {
    "above": "key code 126 using command down\n            delay 0.2",
    "replace": (
        'keystroke "a" using command down\n            delay 0.3\n'
        "            key code 51\n            delay 0.3"
    ),
    "end": "key code 125 using command down\n            delay 0.2",
}


def _refuse_overlong_body(body: str) -> None:
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


def _existing_files(attachment_paths: list[Path] | None) -> list[Path]:
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
_ENDING_MAILBOX: dict[WindowOperation, str] = {
    "save": "drafts mailbox",
    "send": "sent mailbox",
}


@dataclass(frozen=True)
class _ComposeWindow:
    """An open compose window, as the composition's opening script
    reports it: the window's name, the subject and recipients Mail holds
    for its message after every override, and the ids of the mailbox its
    ending is looked up in as they were before it opened (Drafts for a
    save, Sent for a send; ``_ENDING_MAILBOX``). Then what identifies it
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


def _compose_window_from_report(report: dict[str, Any]) -> _ComposeWindow:
    """The opening script's ``resultData`` as a ``_ComposeWindow``; Mail's
    ``missing value`` groups arrive as empty lists, and an id of 0 is no
    id."""
    groups = {
        group: [str(a) for a in report.get(group) or []]
        for group in ("to", "cc", "bcc")
    }
    return _ComposeWindow(
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
class _SentCopy:
    """The copy a send filed in Sent: the one message in Sent with the
    window's subject whose id was not there before the window opened.
    ``rfc_message_id`` is bare, as the read tools emit it."""

    mail_id: str
    rfc_message_id: str
    attachment_names: tuple[str, ...]


@dataclass(frozen=True)
class _SentCopyUnidentified:
    """A send whose copy in Sent could not be told: none appeared in time,
    more than one did, or looking failed; ``why`` says which. Mail accepted
    the message either way: the verified send saw its window close after
    Send with no sheet on it. Nothing after that has been seen of it."""

    why: str


def _sent_result(
    copy: _SentCopy | _SentCopyUnidentified, *, files_pasted: bool
) -> dict[str, Any]:
    """What a send Mail accepted returns: the copy's Mail id, which
    ``get_messages`` takes, and its RFC Message-ID; or, for a copy not
    identified, no id and one warning saying why, and that the files,
    when there were any, are unverified rather than missing."""
    if isinstance(copy, _SentCopy):
        return {
            "draft_id": "",
            "sent_message_id": copy.mail_id,
            "sent_rfc_message_id": copy.rfc_message_id,
        }
    unverified = (
        ", and the files it was sent with are unverified" if files_pasted else ""
    )
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


def _closing_of(raw: str, *, by: Closer, at: float) -> Closed | None:
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


def _composition_closing(raw: str, *, failure: str) -> LeftOpen | Closed:
    """How a composition's window ended, by what its own salvage or
    discard reported; a close that failed leaves it open, with
    ``failure``."""
    now = time.time()
    closed = _closing_of(raw, by="composition", at=now)
    return closed if closed is not None else LeftOpen(failure=failure, at=now)


# MCP-tool field name → Mail.app AppleScript `rule type` enum identifier.
# Verified against Mail.app's running rules: 'from header', 'subject header',
# 'message content' all confirmed live. Other values follow the same naming
# convention per Mail.app's AppleScript dictionary; verified via integration
# test on live rule creation.
_RULE_FIELD_MAP = {
    "from": "from header",
    "to": "to header",
    "subject": "subject header",
    "body": "message content",
    "any_recipient": "any recipient",
    "header_name": "header key",
}

# MCP-tool operator name → Mail.app AppleScript `qualifier` enum identifier.
# 'does contain value', 'equal to value', 'begins with value' verified live
# against the user's existing rules. Others follow Mail.app's documented
# naming.
_RULE_OPERATOR_MAP = {
    "contains": "does contain value",
    "does_not_contain": "does not contain value",
    "begins_with": "begins with value",
    "ends_with": "ends with value",
    "equals": "equal to value",
}


def _wrap_as_json_script(body: str, *, timeout: int) -> str:
    """Wrap a tell-block body with ASObjC imports and an NSJSONSerialization return.

    The `body` must:
      - Contain a `tell application "Mail" ... end tell` block.
      - Assign the final result to an AppleScript variable named `resultData`
        inside that tell block.
      - Handle failures EITHER by letting AppleScript errors propagate via
        stderr (preserves _run_applescript's typed exception mapping, e.g.,
        MailAccountNotFoundError) OR by catching them in a try block and
        returning "ERROR: <message>" (surfaces as MailAppleScriptError on
        the Python side). Use the stderr path when the caller relies on
        typed exceptions; use the "ERROR:" path otherwise.

    The wrapper:
      - Prepends `use framework "Foundation"` and `use scripting additions`.
      - Wraps the body in `with timeout of {timeout} seconds ... end timeout`
        so Mail's default 60 s AppleEvent timeout does not fire before the
        connector's subprocess timeout — see issue #227. Without this,
        per-message property fetches on Exchange/EWS mailboxes (server-
        bound, not local) trip `AppleEvent timed out (-1712)` and leave the
        scripting bridge in `Connection is invalid (-609)` for ~30 s.
      - After the tell block, serializes `resultData` via NSJSONSerialization
        and returns the resulting NSString as text. The serializer runs in
        the ASObjC bridge (no AppleEvent) so it is intentionally OUTSIDE the
        timeout block — but `resultData` is still visible because AppleScript
        `with timeout` is a control construct, not a scope.

    Args:
        body: AppleScript tell-block source setting `resultData`.
        timeout: AppleEvent timeout in seconds for the wrapped tell block.
            Callers should pass ``self.timeout`` so the in-script timeout
            matches the subprocess-level kill timer.

    Returns:
        Full AppleScript source ready for osascript.
    """
    return (
        'use framework "Foundation"\n'
        "use scripting additions\n"
        "\n"
        f"with timeout of {timeout} seconds\n"
        f"{body}\n"
        "end timeout\n"
        "\n"
        "set jsonData to (current application's NSJSONSerialization's "
        "dataWithJSONObject:resultData options:0 |error|:(missing value))\n"
        "return (current application's NSString's alloc()'s "
        "initWithData:jsonData encoding:4) as text\n"
    )


def _bulk_repeat_block(
    *,
    account: str | None,
    source_mailbox: str | None,
    actions: list[str],
    counter_var: str,
) -> str:
    """Emit the AppleScript repeat block for a bulk-mutation operation.

    When `account` and `source_mailbox` are both provided, emits a narrow
    O(N) loop scoped to a single mailbox. When both are None, falls back
    to the legacy O(N × accounts × mailboxes) cross-scan for backwards
    compatibility; that scan stops at the first mailbox that holds an id,
    so a message filed under several Gmail labels is acted on and counted
    once. Any partial-pair raises ValueError — a mailbox name without an
    account is ambiguous (the same name can exist across multiple
    accounts).

    Args:
        account: Account name or UUID, or None.
        source_mailbox: Source mailbox name, or None.
        actions: One or more AppleScript statements to run inside the
            loop AFTER `set msg to first message ... whose id is msgId`.
            The counter increment is appended automatically.
        counter_var: Name of the AppleScript counter variable (e.g.
            "updateCount", "moveCount") that gets incremented per success.

    Returns:
        AppleScript fragment ready to interpolate into a `tell application
        "Mail"` block.

    Raises:
        ValueError: If exactly one of `account`/`source_mailbox` is given.
    """
    if (account is None) != (source_mailbox is None):
        missing = "source_mailbox" if account is not None else "account"
        raise ValueError(
            f"account and source_mailbox must be provided together; "
            f"missing {missing}"
        )

    if account is not None and source_mailbox is not None:
        # Narrow path: single mailbox, single loop. O(N).
        action_indent = " " * 20
        action_lines = "\n".join(action_indent + a for a in actions)
        account_clause = applescript_account_clause(account)
        mb_safe = escape_applescript_string(sanitize_input(source_mailbox))
        return (
            f'            set sourceMb to mailbox "{mb_safe}" of {account_clause}\n'
            f"            repeat with msgId in idList\n"
            f"                try\n"
            f"                    set msg to first message of sourceMb whose id is msgId\n"
            f"{action_lines}\n"
            f"                    set {counter_var} to {counter_var} + 1\n"
            f"                end try\n"
            f"            end repeat"
        )

    # Cross-scan path (legacy / backwards compat). O(N × M × K).
    #
    # Each id contributes at most once. Gmail exposes every label as a
    # mailbox and files one message under each label it carries, so the
    # same message matches in INBOX, All Mail and every user label. Without
    # leaving the scan on the first match, the actions ran and the counter
    # bumped once per label: update_message on one message returned 2, and
    # the count is the only success signal these tools have. Mail's numeric
    # `id` is unique per message, so the first match is the right one.
    #
    # `exit repeat` leaves only the innermost loop, hence the flag: it ends
    # the mailbox loop directly and the account loop on the next check.
    action_indent = " " * 28
    action_lines = "\n".join(action_indent + a for a in actions)
    return (
        f"            repeat with msgId in idList\n"
        f"                set matched to false\n"
        f"                repeat with acc in accounts\n"
        f"                    repeat with mb in mailboxes of acc\n"
        f"                        try\n"
        f"                            set msg to first message of mb whose id is msgId\n"
        f"{action_lines}\n"
        f"                            set {counter_var} to {counter_var} + 1\n"
        f"                            set matched to true\n"
        f"                            exit repeat\n"
        f"                        end try\n"
        f"                    end repeat\n"
        f"                    if matched then exit repeat\n"
        f"                end repeat\n"
        f"            end repeat"
    )


# Attachment-record properties, in emitted order:
#   (AppleScript property expression, JSON key, AppleScript variable,
#    AppleScript literal used as the on-failure default)
# Each is read under its OWN try block -- see _attachment_walk_block.
_ATTACHMENT_PROPS: tuple[tuple[str, str, str, str], ...] = (
    ("name of att", "name", "attName", '""'),
    ("MIME type of att", "mime_type", "attMime", '""'),
    ("file size of att", "size", "attSize", "0"),
    ("downloaded of att", "downloaded", "attDown", "false"),
)


def _attachment_walk_block(
    *,
    message_var: str,
    warnings_var: str,
    indent: int,
    list_var: str = "attList",
) -> str:
    """Emit the AppleScript that walks ``mail attachments of <message_var>``
    and builds one record per attachment.

    TWO layers of guard, for two DIFFERENT observed failures:

    1. **Per-property guard (inner).** Every property is read into its own
       variable under its own ``try``; the record is assembled from those
       variables. Observed 2026-08-27 against the iCloud test account's INBOX
       messages 1463-1466: ``MIME type of att`` raises
       errAEEventNotHandled (-10000) while ``name`` / ``file size`` /
       ``downloaded`` of the SAME attachment read cleanly. Because the
       record used to be built from four inline property reads in one
       expression, that single bad property aborted the whole walk and a
       perfectly saveable PDF surfaced as "no attachments". Probe output
       and re-check commands: docs/research/attachment-property-10000.md.

    2. **Whole-walk guard (outer).** ``mail attachments of <msg>`` can
       itself raise -10000 on some inline-image multipart layouts. That
       guard predates this one and is preserved, including its exact
       warning wording.

    On-failure defaults are empty/zero -- never a guess. A MIME type is
    deliberately NOT inferred from the filename extension: the record
    reports what Mail.app actually returned, and the warning says why it
    is blank.

    Args:
        message_var: AppleScript variable holding the message.
        warnings_var: AppleScript variable holding the warnings list.
            Must already be initialized by the caller.
        indent: Leading spaces for the emitted block.
        list_var: AppleScript variable to build; initialized here.

    Returns:
        AppleScript fragment ready to interpolate.
    """
    pad = " " * indent
    lines: list[str] = []
    lines.append(f"{pad}set {list_var} to {{}}")
    lines.append(f"{pad}try")
    lines.append(f"{pad}    repeat with att in mail attachments of {message_var}")
    for prop_expr, _key, var, default in _ATTACHMENT_PROPS:
        prop_label = prop_expr.replace(" of att", "")
        lines.append(f"{pad}        set {var} to {default}")
        lines.append(f"{pad}        try")
        lines.append(f"{pad}            set {var} to ({prop_expr})")
        lines.append(f"{pad}        on error errMsg number errNum")
        lines.append(
            f"{pad}            set end of {warnings_var} to "
            f'("attachment property {prop_label} unreadable for message " & '
            f"(id of {message_var} as text) & \": \" & errMsg & "
            f'" (error " & errNum & ")")'
        )
        lines.append(f"{pad}        end try")
    record_fields = ", ".join(
        f"|{key}|:{var}" for _e, key, var, _d in _ATTACHMENT_PROPS
    )
    lines.append(f"{pad}        set attRecord to {{{record_fields}}}")
    lines.append(f"{pad}        set end of {list_var} to attRecord")
    lines.append(f"{pad}    end repeat")
    lines.append(f"{pad}on error errMsg number errNum")
    lines.append(f"{pad}    set {list_var} to {{}}")
    lines.append(
        f"{pad}    set end of {warnings_var} to "
        f'("attachment enumeration failed for message " & '
        f"(id of {message_var} as text) & \": \" & errMsg & "
        f'" (error " & errNum & ")")'
    )
    lines.append(f"{pad}end try")
    return "\n".join(lines)


# A message's recipient elements, in emitted order:
#   (Mail element, JSON key, AppleScript list variable)
_RECIPIENT_KINDS: tuple[tuple[str, str, str], ...] = (
    ("to recipients", "to", "toList"),
    ("cc recipients", "cc", "ccList"),
    ("bcc recipients", "bcc", "bccList"),
)

# The fields a message record takes from _recipient_read_block's lists.
_RECIPIENT_FIELDS = ", ".join(
    f"|{key}|:{var}" for _element, key, var in _RECIPIENT_KINDS
)


def _recipient_read_block(
    *,
    message_var: str,
    warnings_var: str,
    indent: int,
    id_expr: str | None = None,
    from_runs: bool = False,
) -> str:
    """Emit the AppleScript that reads ``<message_var>``'s to, cc and bcc
    recipients into ``toList``, ``ccList`` and ``bccList``: one
    ``{|name|, |address|}`` record per recipient, which Python renders
    with ``format_address`` (``_render_recipients``) exactly as the
    IMAP path renders an ENVELOPE address.

    One Apple event per kind. ``properties of <kind> of <msg>`` returns
    every recipient of that kind at once, and name and address are then
    read from the local records. Measured on the test account: about
    16 ms per event, where the six ``name of`` / ``address of`` reads of
    the same three lists took about 100 ms. ``recipients of <msg>``
    would be one event for all three, but it does not tell them apart:
    read that way, a Cc recipient reported its class as ``to
    recipient``.

    Each kind is read under its own ``try``. A failure leaves that list
    empty and appends a warning naming the kind and the message to
    ``warnings_var`` (which the caller initialises), so an unreadable
    list is reported rather than passed off as an empty one, and one
    bad kind does not take the others with it. A recipient without a
    display name has Mail's ``missing value`` as its name, which JSON
    cannot carry; it becomes ``""``, as does a missing address.

    With ``from_runs``, the search's bulk path is the caller: each kind
    was read for a run of messages at once into ``<key>Run`` (reached
    through ``<key>RunRef``), and ``<key>RunRead`` says whether that
    read worked. The message's list is then item ``j`` of the run's,
    and only when the run's read failed is it read for the one
    message, under the same guard.

    Args:
        message_var: AppleScript variable holding the message.
        warnings_var: AppleScript list variable collecting warnings.
        indent: Leading spaces for the emitted block.
        id_expr: AppleScript text expression for the message's id in a
            warning; by default it is read from the message.
        from_runs: Take each list from the search's run reads.

    Returns:
        AppleScript fragment ready to interpolate. The message record
        takes the lists with ``_RECIPIENT_FIELDS``.
    """
    pad = " " * indent
    id_text = id_expr or f"(id of {message_var} as text)"
    lines: list[str] = []
    for element, key, var in _RECIPIENT_KINDS:
        one_message = f"set rcpts to properties of {element} of {message_var}"
        source = (
            [
                f"{pad}    if {key}RunRead then",
                f"{pad}        set rcpts to item j of {key}RunRef",
                f"{pad}    else",
                f"{pad}        {one_message}",
                f"{pad}    end if",
            ]
            if from_runs
            else [f"{pad}    {one_message}"]
        )
        lines += [
            f"{pad}set {var} to {{}}",
            f"{pad}try",
            *source,
            f"{pad}    repeat with rcpt in rcpts",
            f"{pad}        set rcptName to name of rcpt",
            f'{pad}        if rcptName is missing value then set rcptName to ""',
            f"{pad}        set rcptAddress to address of rcpt",
            f'{pad}        if rcptAddress is missing value then set rcptAddress to ""',
            f"{pad}        set end of {var} to {{|name|:rcptName, |address|:rcptAddress}}",
            f"{pad}    end repeat",
            f"{pad}on error errMsg number errNum",
            f"{pad}    set {var} to {{}}",
            f"{pad}    set end of {warnings_var} to "
            f'("{key} recipients unreadable for message " & '
            f'{id_text} & ": " & errMsg & '
            f'" (error " & errNum & ")")',
            f"{pad}end try",
        ]
    return "\n".join(lines)


def _render_recipients(record: dict[str, Any]) -> None:
    """Replace the recipient records a script emitted with the row's
    strings, in place: each ``{name, address}`` through
    ``format_address``, the renderer the IMAP rows use.

    A record without one of the keys is left without it. Every script
    that reads recipients emits all three, so there is nothing to
    render, and an empty list here would say the message has no
    recipients when nothing was read.
    """
    for _element, key, _var in _RECIPIENT_KINDS:
        if key in record:
            record[key] = format_recipients(
                (str(r.get("name") or ""), str(r.get("address") or ""))
                for r in record[key] or []
            )


# The property a body or text criterion reads.
_CONTENT = "content"


@dataclass(frozen=True)
class _SearchCriterion:
    """One search predicate, as the script tests a message against it.

    ``prop`` is the message property the test reads, and says where the
    bulk path reads it:

    - a property Mail answers for every message at once (``subject``,
      ``date received``): read for the whole mailbox in one event;
    - ``content`` (``_CONTENT``): read for a run of the positions the
      other criteria kept, in one event, and only for those;
    - None, when the test needs the message itself (its attachments):
      asked one message at a time, of a message the others kept.

    ``also`` names further properties the test reads, each read for the
    whole mailbox like a ``prop`` (a text search tests the subject and
    the sender beside the content). ``excludes`` renders the AppleScript
    condition that drops a message, given the expression for the value
    of ``prop`` and then one for each of ``also``, or the expression for
    the message itself when ``prop`` is None.
    """

    prop: str | None
    excludes: Callable[..., str]
    also: tuple[str, ...] = ()


def _condition(
    criterion: _SearchCriterion, value: Callable[[str], str], message: str
) -> str:
    """``criterion.excludes`` rendered with ``value(p)`` as each
    property's value, or with ``message`` when the test asks the
    message itself."""
    if criterion.prop is None:
        return criterion.excludes(message)
    return criterion.excludes(
        value(criterion.prop), *(value(p) for p in criterion.also)
    )


def _mailbox_wide_props(criteria: list[_SearchCriterion]) -> list[str]:
    """The properties the criteria read for the whole mailbox, each
    once, in the order the criteria name them."""
    props: list[str] = []
    for c in criteria:
        if c.prop is not None and c.prop != _CONTENT:
            props.append(c.prop)
        props += c.also
    return list(dict.fromkeys(props))


def _does_not_contain(text: str) -> Callable[[str], str]:
    safe = escape_applescript_string(sanitize_input(text))
    return lambda value: f'{value} does not contain "{safe}"'


def _is_not(flag: bool) -> Callable[[str], str]:
    target = "true" if flag else "false"
    return lambda value: f"{value} is not {target}"


def _search_criteria(
    *,
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None,
    date_to: str | None,
    has_attachment: bool | None,
    body_contains: str | None,
    text_contains: str | None,
) -> tuple[list[_SearchCriterion], list[str]]:
    """Translate search predicates into the criteria the script tests.

    Returns ``(criteria, date_setup)``: one criterion per active
    predicate, and the date-cutoff statements that must run once before
    any message is tested. Pure: no Mail access, so the translation is
    testable on its own.

    Raises:
        ValueError: If date_from or date_to is not ISO 8601 YYYY-MM-DD.
    """
    criteria: list[_SearchCriterion] = []
    date_setup: list[str] = []

    if sender_contains:
        criteria.append(_SearchCriterion("sender", _does_not_contain(sender_contains)))
    if subject_contains:
        criteria.append(
            _SearchCriterion("subject", _does_not_contain(subject_contains))
        )
    if read_status is not None:
        criteria.append(_SearchCriterion("read status", _is_not(read_status)))
    if is_flagged is not None:
        criteria.append(_SearchCriterion("flagged status", _is_not(is_flagged)))

    if date_from is not None:
        if not _ISO_DATE_RE.match(date_from):
            raise ValueError(
                f"date_from must be ISO 8601 YYYY-MM-DD, got: {date_from!r}"
            )
        date_setup.append(
            applescript_iso_date_statements("dateFromCutoff", date_from)
        )
        criteria.append(
            _SearchCriterion("date received", lambda v: f"{v} < dateFromCutoff")
        )

    if date_to is not None:
        if not _ISO_DATE_RE.match(date_to):
            raise ValueError(
                f"date_to must be ISO 8601 YYYY-MM-DD, got: {date_to!r}"
            )
        # Upper bound is exclusive of the day AFTER date_to, so the full
        # day of date_to is included.
        next_day = (
            _date.fromisoformat(date_to) + _timedelta(days=1)
        ).isoformat()
        date_setup.append(
            applescript_iso_date_statements("dateToCutoff", next_day)
        )
        criteria.append(
            _SearchCriterion("date received", lambda v: f"{v} >= dateToCutoff")
        )

    if has_attachment is not None:
        compare = "= 0" if has_attachment else "> 0"
        criteria.append(_SearchCriterion(
            None, lambda m: f"(count of mail attachments of {m}) {compare}"
        ))

    # Body / text filters (#145). AppleScript `contains` is
    # case-insensitive by default, matching IMAP `SEARCH BODY`/`TEXT`
    # semantics. Reading the content is the expensive part — see #146
    # for the proactive warning surfaced before this script runs.
    if body_contains:
        criteria.append(_SearchCriterion(_CONTENT, _does_not_contain(body_contains)))

    if text_contains:
        # `text_contains` is the IMAP `TEXT` predicate — substring match
        # against headers + body. AppleScript can't easily address all
        # headers in a per-msg property; we approximate with content +
        # subject + sender (the practical cases). Recipients omitted —
        # callers who need recipient matching should use `sender_contains`
        # or future params. Documented in TOOLS.md.
        text_safe = escape_applescript_string(sanitize_input(text_contains))
        criteria.append(_SearchCriterion(
            _CONTENT,
            lambda content, subject, sender: (
                f'not ({content} contains "{text_safe}" or '
                f'{subject} contains "{text_safe}" or '
                f'{sender} contains "{text_safe}")'
            ),
            also=("subject", "sender"),
        ))
    return criteria, date_setup


# What a search row reads from Mail besides its id and recipients, in
# the order its record lists them: (Mail property, JSON key, whether the
# value is coerced to text).
_SEARCH_ROW_PROPERTIES: tuple[tuple[str, str, bool], ...] = (
    ("message id", "rfc_message_id", False),
    ("subject", "subject", False),
    ("sender", "sender", False),
    ("date received", "date_received", True),
    ("read status", "read_status", False),
    ("flagged status", "flagged", False),
)

# The bulk path reads a row property once per run of matched mailbox
# positions, and a run takes in up to this many unmatched positions
# between two matches rather than ending. Reading a property over a
# range cost about 10 ms per event plus 1.3 ms per message on the test
# account (2026-09-27), so a gap this wide costs about what one more
# event would.
_SEARCH_RUN_GAP = 8

# The content, unlike a row property, is read only for the positions
# the other criteria kept: a run of body reads takes in no position they
# dropped. What a body costs is its message's, not the event's. Measured
# on the test account's INBOX (2026-09-27), read-only: over 20 short
# bodies Mail had read lately, one range read cost 116-127 ms (about
# 6 ms a message) and a range of one per message 330-342 ms, so each
# further event cost about 11 ms; over 20 older ones, 5-6 s whichever
# way they were read (215-315 ms a message); five others cost 0.85-6.4 s
# each on the one read of them measured; and one, whose body came back
# empty, cost from 17 ms to 10.7 s a read, alone or in a range, between
# reads seconds apart. So the cheapest body a gap would take in costs
# about the event it saves, and a dear one costs seconds for a message
# no criterion wanted.
_SEARCH_CONTENT_RUN_GAP = 0

# The kept positions wait for their bodies until there are as many as
# matches still wanted (so no body is read past the limit), or this
# many (so no one read holds more bodies than this, whatever the
# limit), or the scan reaches the mailbox's end. Beside 50 bodies, the
# one more event a batch costs (about 11 ms) is small.
_SEARCH_CONTENT_BATCH = 50

_SEARCH_OUT_OF_LINE_WARNING = (
    "the search's bulk reads did not line up (the mailbox changed while "
    "they ran, or its ids could not be read in bulk); searched it one "
    "message at a time instead"
)


def _as_var(prop: str) -> str:
    """``date received`` → ``dateReceived``: a variable stem for a property."""
    first, *rest = prop.split()
    return first + "".join(word.capitalize() for word in rest)


def _indented(lines: list[str], by: int) -> list[str]:
    pad = " " * by
    return [pad + line if line else line for line in lines]


def _block_lines(block: str) -> list[str]:
    """A block emitted at indent 0 by one of the helpers above, as lines."""
    return block.split("\n")


def _search_record(
    *,
    id_expr: str,
    value: Callable[[str, str, bool], str],
    include_attachments: bool,
) -> str:
    """The AppleScript record literal for one search row.

    ``value(prop, stem, as_text)`` is the expression for a row
    property's field, coerced to text when ``as_text``.
    """
    fields = [f"|id|:{id_expr}"] + [
        f"|{key}|:{value(prop, _as_var(prop), as_text)}"
        for prop, key, as_text in _SEARCH_ROW_PROPERTIES
    ]
    fields.append(_RECIPIENT_FIELDS)
    if include_attachments:
        fields.append("|attachments|:attList")
    return "{" + ", ".join(fields) + "}"


def _one_at_a_time_search_lines(
    criteria: list[_SearchCriterion], *, limit: str, include_attachments: bool
) -> list[str]:
    """The search one message at a time: every message's criteria and
    row read with an event per property. What the search did before it
    read in bulk, and what the bulk path gives way to when its lists do
    not line up.

    Iterate forward: Mail returns ``messages of mailbox`` newest-first,
    so a limited search short-circuits on the newest messages. (It once
    ran backwards, returning the oldest, while its comment claimed
    newest-first; measuring caught it, reading did not.)

    A message whose criteria cannot be read is left out with a warning
    rather than failing the search; an unreadable recipient list or
    attachment walk leaves the row with that list empty and a warning.
    """
    checks = [
        f"if {_condition(c, lambda p: f'({p} of msg)', 'msg')} then set includeThis to false"
        for c in criteria
    ]
    row: list[str] = []
    if include_attachments:
        row += _block_lines(_attachment_walk_block(
            message_var="msg", warnings_var="warnList", indent=0
        ))
    row += _block_lines(_recipient_read_block(
        message_var="msg", warnings_var="warnList", indent=0
    ))
    record = _search_record(
        id_expr="(id of msg as text)",
        value=lambda prop, _stem, as_text: (
            f"({prop} of msg as text)" if as_text else f"({prop} of msg)"
        ),
        include_attachments=include_attachments,
    )
    return [
        "set msgs to messages of mailboxRef",
        "set total to count of msgs",
        "set matchCount to 0",
        "repeat with i from 1 to total",
        f"    if matchCount >= {limit} then exit repeat",
        "    set msg to item i of msgs",
        "    set includeThis to true",
        "    try",
        *_indented(checks, 8),
        "    on error errMsg number errNum",
        "        set includeThis to false",
        "        try",
        '            set end of warnList to ("filter check failed for message " & (id of msg as text) & ": " & errMsg & " (error " & errNum & ")")',
        "        on error",
        '            set end of warnList to ("filter check failed for message at index " & i & ": " & errMsg & " (error " & errNum & ")")',
        "        end try",
        "    end try",
        "    if includeThis then",
        *_indented(row, 8),
        f"        set end of resultData to {record}",
        "        set matchCount to matchCount + 1",
        "    end if",
        "end repeat",
    ]


def _bulk_read_lines(*, target: str, expr: str, what: str, flag: str, where: str) -> list[str]:
    """Read ``expr`` into ``target`` under a guard: ``flag`` says
    whether it worked, and a failure is a warning naming ``what`` and
    ``where``, after which the caller reads one message at a time.
    ``target`` starts empty, so a reference to it is always valid."""
    return [
        f"set {target} to {{}}",
        f"set {flag} to false",
        "try",
        f"    set {target} to {expr}",
        f"    set {flag} to true",
        "on error errMsg number errNum",
        f'    set end of warnList to ("{what} could not be read in bulk{where}: " & errMsg & " (error " & errNum & "); read one message at a time")',
        "end try",
    ]


def _first_positions_lines(*, limit: str) -> list[str]:
    """With no criteria the matches are the first ``limit`` positions,
    and nothing is read for the whole mailbox but its count."""
    return [
        "set total to count of messages of mailboxRef",
        "set matched to {}",
        "set matchCount to 0",
        "repeat with i from 1 to total",
        f"    if matchCount >= {limit} then exit repeat",
        "    set end of matched to i",
        "    set matchCount to matchCount + 1",
        "end repeat",
    ]


def _one_message_line(index: str) -> str:
    """Make ``msgRef`` a reference to the message at ``index`` of the id
    list, by its id: making it costs no event, reading through it does."""
    return (
        "set msgRef to a reference to («class mssg» id "
        f"(item {index} of allIdsRef) of mailboxRef)"
    )


def _value_expr(prop: str) -> str:
    """The variable ``_value_lines`` sets for ``prop``."""
    return _as_var(prop) + "Value"


def _value_lines(prop: str, index: str) -> list[str]:
    """Set ``prop``'s value for the message at ``index``: from its list
    for the whole mailbox, or from the message when that read failed."""
    return [
        f"if {_as_var(prop)}InBulk then",
        f"    set {_value_expr(prop)} to item {index} of {_as_var(prop)}AllRef",
        "else",
        f"    {_one_message_line(index)}",
        f"    set {_value_expr(prop)} to ({prop} of msgRef)",
        "end if",
    ]


def _mailbox_read_lines(props: list[str]) -> list[str]:
    """Read the ids, and each of ``props``, for the whole mailbox, each
    in one event. Every list must be as long as the id list; one that
    is not means the mailbox changed between the reads."""
    lines = [
        "set allIds to id of messages of mailboxRef",
        "set allIdsRef to a reference to allIds",
        "set total to count of allIds",
    ]
    for prop in props:
        stem = _as_var(prop)
        lines += _bulk_read_lines(
            target=f"{stem}All", expr=f"{prop} of messages of mailboxRef",
            what=prop, flag=f"{stem}InBulk", where="",
        ) + [
            f"set {stem}AllRef to a reference to {stem}All",
            f"if {stem}InBulk and (count of {stem}All) is not total then set aligned to false",
        ]
    return lines


def _scan_check_lines(criteria: list[_SearchCriterion]) -> list[str]:
    """Test the message at position ``i`` against the criteria the scan
    decides: each on a property read for the whole mailbox against its
    list, then each that asks the message itself, only while the others
    keep it."""
    checks: list[str] = []
    for prop in dict.fromkeys(c.prop for c in criteria if c.prop):
        checks += _value_lines(prop, "i") + [
            f"if {_condition(c, _value_expr, 'msgRef')} then set includeThis to false"
            for c in criteria if c.prop == prop
        ]
    by_message = [c for c in criteria if c.prop is None]
    if by_message:
        checks.append(f"if includeThis then {_one_message_line('i')}")
    checks += [
        f"if includeThis and ({_condition(c, _value_expr, 'msgRef')}) then set includeThis to false"
        for c in by_message
    ]
    return checks


def _content_run_lines() -> list[str]:
    """From the kept position at ``candFirst`` of ``cands``, extend a run
    to ``candLast`` over the kept positions that follow within
    ``_SEARCH_CONTENT_RUN_GAP``; read the run's content in one event,
    and then its ids, which must be as many and, message by message, the
    ids the criteria kept, for the content read before them to be those
    messages'. A content read that fails is warned about, and each
    message's is read on its own; ids that do not line up leave
    ``aligned`` false and end the tests."""
    where = ' for mailbox positions " & runStart & "-" & runEnd & "'
    return [
        "set runStart to item candFirst of candsRef",
        "set runEnd to runStart",
        "set candLast to candFirst",
        "repeat while candLast < candCount",
        f"    if (item (candLast + 1) of candsRef) - runEnd > {_SEARCH_CONTENT_RUN_GAP + 1} then exit repeat",
        "    set candLast to candLast + 1",
        "    set runEnd to item candLast of candsRef",
        "end repeat",
        *_bulk_read_lines(
            target="contentRun",
            expr="content of messages runStart thru runEnd of mailboxRef",
            what="content", flag="contentRunRead", where=where,
        ),
        "set contentRunRef to a reference to contentRun",
        "if contentRunRead then",
        "    try",
        "        set runIds to id of messages runStart thru runEnd of mailboxRef",
        "    on error",
        "        set aligned to false",
        "        exit repeat",
        "    end try",
        "    set runIdsRef to a reference to runIds",
        "    if (count of runIds) is not (runEnd - runStart + 1) or (count of contentRun) is not (count of runIds) then",
        "        set aligned to false",
        "        exit repeat",
        "    end if",
        "end if",
    ]


def _content_test_lines(criteria: list[_SearchCriterion]) -> list[str]:
    """Test the content criteria on the ``candCount`` kept positions in
    ``cands``, a run of them at a time (``_content_run_lines``), and add
    each position they keep to ``matched``. A message whose content, or
    a property beside it, cannot be read is left out with a warning, as
    in the scan. ``cands`` is emptied for the scan to fill again."""
    also = list(dict.fromkeys(p for c in criteria for p in c.also))
    candidate = [
        "set idx to item k of candsRef",
        "set includeThis to true",
        "try",
        "    if contentRunRead then",
        "        set j to idx - runStart + 1",
        "        if (item j of runIdsRef) is not (item idx of allIdsRef) then set aligned to false",
        f"        set {_value_expr(_CONTENT)} to item j of contentRunRef",
        "    else",
        f"        {_one_message_line('idx')}",
        f"        set {_value_expr(_CONTENT)} to ({_CONTENT} of msgRef)",
        "    end if",
        *_indented([line for p in also for line in _value_lines(p, "idx")], 4),
        *[
            f"    if includeThis and ({_condition(c, _value_expr, 'msgRef')}) then set includeThis to false"
            for c in criteria
        ],
        "on error errMsg number errNum",
        "    set includeThis to false",
        '    set end of warnList to ("filter check failed for message " & ((item idx of allIdsRef) as text) & ": " & errMsg & " (error " & errNum & ")")',
        "end try",
        "if includeThis then",
        "    set end of matched to idx",
        "    set matchCount to matchCount + 1",
        "end if",
    ]
    return [
        "set candsRef to a reference to cands",
        "set candFirst to 1",
        "repeat while candFirst <= candCount",
        *_indented(_content_run_lines(), 4),
        "    repeat with k from candFirst to candLast",
        *_indented(candidate, 8),
        "    end repeat",
        "    if not aligned then exit repeat",
        "    set candFirst to candLast + 1",
        "end repeat",
        "set cands to {}",
        "set candCount to 0",
    ]


def _bulk_match_lines(
    criteria: list[_SearchCriterion], *, limit: str
) -> list[str]:
    """Find the matched positions. The scan reads each criterion's
    property for the whole mailbox in one event and tests the values in
    the script, and asks a message itself only for what has no bulk
    form, only when the other criteria kept it. With a content
    criterion, a position the scan keeps is a candidate: the candidates
    wait in ``cands`` until they are as many as the matches still
    wanted, or fill a batch, or the scan ends, and then their content is
    read a run at a time and tested (``_content_test_lines``)."""
    content = [c for c in criteria if c.prop == _CONTENT]
    keep, count = ("cands", "candCount") if content else ("matched", "matchCount")
    checks = _scan_check_lines([c for c in criteria if c.prop != _CONTENT])
    kept = [f"set end of {keep} to i", f"set {count} to {count} + 1"]
    scan = [
        "set includeThis to true",
        "try",
        *_indented(checks, 4),
        "on error errMsg number errNum",
        "    set includeThis to false",
        '    set end of warnList to ("filter check failed for message " & ((item i of allIdsRef) as text) & ": " & errMsg & " (error " & errNum & ")")',
        "end try",
        "if includeThis then",
        *_indented(kept, 4),
        "end if",
    ] if checks else kept
    flush = [
        f"if candCount > 0 and (candCount >= {limit} - matchCount or candCount >= {_SEARCH_CONTENT_BATCH} or i = total) then",
        *_indented(_content_test_lines(content), 4),
        "    if not aligned then exit repeat",
        "end if",
    ] if content else []
    return _mailbox_read_lines(_mailbox_wide_props(criteria)) + [
        "set matched to {}",
        "set matchCount to 0",
        *(["set cands to {}", "set candCount to 0"] if content else []),
        "if aligned then",
        "    repeat with i from 1 to total",
        f"        if matchCount >= {limit} then exit repeat",
        *_indented(scan, 8),
        *_indented(flush, 8),
        "    end repeat",
        "end if",
    ]


def _run_reads_lines(filter_props: list[str]) -> list[str]:
    """Read each row property, and each kind of recipient, once for the
    run ``runStart thru runEnd``. A property a criterion already read
    for the whole mailbox is not read again. Each read's list must be as
    long as the run's ids."""
    lines: list[str] = []
    where = ' for mailbox positions " & runStart & "-" & runEnd & "'
    reads = [
        (_as_var(prop), f"{prop} of messages runStart thru runEnd of mailboxRef", prop)
        for prop, _key, _text in _SEARCH_ROW_PROPERTIES
    ] + [
        (key, f"properties of {element} of messages runStart thru runEnd of mailboxRef", element)
        for element, key, _var in _RECIPIENT_KINDS
    ]
    for stem, expr, what in reads:
        read = _bulk_read_lines(
            target=f"{stem}Run", expr=expr, what=what, flag=f"{stem}RunRead", where=where,
        ) + [
            f"set {stem}RunRef to a reference to {stem}Run",
            f"if {stem}RunRead and (count of {stem}Run) is not (count of runIds) then set aligned to false",
        ]
        if what in filter_props:
            read = [f"set {stem}RunRead to false", f"if not {stem}InBulk then", *_indented(read, 4), "end if"]
        lines += read
    return lines


def _row_value_lines(filter_props: list[str]) -> list[str]:
    """Each row property's value for the message at position ``idx``
    (item ``j`` of its run): from the criterion's list for the whole
    mailbox, else from the run's list, else read from the message."""
    lines: list[str] = []
    for prop, _key, _text in _SEARCH_ROW_PROPERTIES:
        stem = _as_var(prop)
        sources = [(f"{stem}RunRead", f"item j of {stem}RunRef")]
        if prop in filter_props:
            sources.insert(0, (f"{stem}InBulk", f"item idx of {stem}AllRef"))
        for n, (flag, expr) in enumerate(sources):
            lines += [f"{'else if' if n else 'if'} {flag} then", f"    set {stem}Value to {expr}"]
        lines += ["else", f"    set {stem}Value to ({prop} of msgRef)", "end if"]
    return lines


def _bulk_rows_lines(
    *, filter_props: list[str], check_ids: bool, include_attachments: bool
) -> list[str]:
    """Build a row for each matched position, reading each property
    once per run of positions rather than once per message.

    Positions shift when a message arrives, moves or goes, and a list
    read after the shift no longer lines up with one read before it.
    So each row's id must be the one the criteria matched at its
    position (``check_ids``), and once the rows are built the ids at
    the first and last matched positions are read again; a mismatch
    leaves ``aligned`` false and the caller searches one message at a
    time instead.
    """
    row: list[str] = [
        "set j to idx - runStart + 1",
        "set msgId to item j of runIdsRef",
    ]
    if check_ids:
        row.append("if msgId is not (item idx of allIdsRef) then set aligned to false")
    row += [
        "if k is 1 then set firstRowId to msgId",
        "set lastRowId to msgId",
        "set msgRef to a reference to («class mssg» id msgId of mailboxRef)",
        *_row_value_lines(filter_props),
        *_block_lines(_recipient_read_block(
            message_var="msgRef", warnings_var="warnList", indent=0,
            id_expr="(msgId as text)", from_runs=True,
        )),
    ]
    if include_attachments:
        row += _block_lines(_attachment_walk_block(
            message_var="msgRef", warnings_var="warnList", indent=0
        ))
    record = _search_record(
        id_expr="(msgId as text)",
        value=lambda _prop, stem, as_text: (
            f"({stem}Value as text)" if as_text else f"{stem}Value"
        ),
        include_attachments=include_attachments,
    )
    row += [f"set end of resultData to {record}", "set k to k + 1"]
    return [
        "if aligned and matchCount > 0 then",
        "    set matchedRef to a reference to matched",
        "    set runs to {}",
        "    set runStart to 0",
        "    set runEnd to 0",
        "    repeat with k from 1 to matchCount",
        "        set idx to item k of matchedRef",
        "        if runStart is 0 then",
        "            set runStart to idx",
        f"        else if idx - runEnd > {_SEARCH_RUN_GAP + 1} then",
        "            set end of runs to {runStart, runEnd}",
        "            set runStart to idx",
        "        end if",
        "        set runEnd to idx",
        "    end repeat",
        "    set end of runs to {runStart, runEnd}",
        "    set k to 1",
        "    repeat with runBounds in runs",
        "        set runStart to item 1 of runBounds",
        "        set runEnd to item 2 of runBounds",
        "        try",
        "            set runIds to id of messages runStart thru runEnd of mailboxRef",
        "        on error",
        "            set aligned to false",
        "            exit repeat",
        "        end try",
        "        set runIdsRef to a reference to runIds",
        "        if (count of runIds) is not (runEnd - runStart + 1) then",
        "            set aligned to false",
        "            exit repeat",
        "        end if",
        *_indented(_run_reads_lines(filter_props), 8),
        "        repeat while k <= matchCount",
        "            set idx to item k of matchedRef",
        "            if idx > runEnd then exit repeat",
        *_indented(row, 12),
        "        end repeat",
        "        if not aligned then exit repeat",
        "    end repeat",
        "    if aligned then",
        "        try",
        "            if (id of message (item 1 of matched) of mailboxRef) is not firstRowId then set aligned to false",
        "            if (id of message (item matchCount of matched) of mailboxRef) is not lastRowId then set aligned to false",
        "        on error",
        "            set aligned to false",
        "        end try",
        "    end if",
        "end if",
    ]


def _search_script_body(
    *,
    account_clause: str,
    mailbox_safe: str,
    criteria: list[_SearchCriterion],
    date_setup: list[str],
    limit: str,
    include_attachments: bool,
) -> str:
    """The tell block of the AppleScript search; see
    ``AppleMailConnector._search_messages_applescript`` for the design
    and the measurements behind it."""
    filter_props = _mailbox_wide_props(criteria)
    match = (
        _bulk_match_lines(criteria, limit=limit)
        if criteria
        else _first_positions_lines(limit=limit)
    )
    body = [
        f"set accountRef to {account_clause}",
        f'set mailboxRef to mailbox "{mailbox_safe}" of accountRef',
        *"\n".join(date_setup).splitlines(),
        "set resultData to {}",
        "set warnList to {}",
        "set aligned to true",
        *match,
        *_bulk_rows_lines(
            filter_props=filter_props,
            check_ids=bool(criteria),
            include_attachments=include_attachments,
        ),
        "if not aligned then",
        "    set resultData to {}",
        f'    set warnList to {{"{_SEARCH_OUT_OF_LINE_WARNING}"}}',
        *_indented(_one_at_a_time_search_lines(
            criteria, limit=limit, include_attachments=include_attachments
        ), 4),
        "end if",
        "set resultData to {|messages|:resultData, |warnings|:warnList}",
    ]
    return 'tell application "Mail"\n' + "\n".join(_indented(body, 4)) + "\nend tell"


class AppleMailConnector:
    """Interface to Apple Mail via AppleScript."""

    # How long create_draft waits for a saved draft to show up in Drafts
    # before giving up. 40 polls at 0.25 s is 10 s; measured appearance
    # is ~1-1.5 s (2026-09-11), the bound is for a Mail that is busy.
    _DRAFT_APPEAR_POLLS = 40
    _DRAFT_APPEAR_INTERVAL_S = 0.25
    # Extra wait once the draft is listed, before its id is handed out.
    # Measured 2026-09-11: a delete issued at 0 s after listing did not
    # take, at 1 s and 3 s it did; nothing readable on the draft marks
    # the difference, so this is a measured bound, not a signal.
    _DRAFT_SETTLE_S = 1.0
    # How long a send looks for the copy it filed in Sent once the
    # verified send has seen its window go: one look at once, then 30
    # more 1 s apart. The verified send has seen no copy, only the window
    # closing, so the copy may not be filed yet on any look but the last,
    # whatever the subject. 30 s is what the integration suite allows a
    # Sent copy after a send returns (SENT_COPY_TIMEOUT_S,
    # tests/integration/mail_readback.py), and every copy it has read
    # arrived within it, a forward with three files among them; the
    # send's grounding saw copies appear "within seconds"
    # (docs/reference/UI_GROUNDING_MAIL_SEND.md). How long the copy of a
    # large attachment takes is unmeasured. The looks are separate
    # scripts, so the Mail lock is free between them, and a copy not seen
    # in time is a warning, never an error: Mail accepted the message when
    # it closed the window.
    _SENT_APPEAR_POLLS = 30
    _SENT_APPEAR_INTERVAL_S = 1.0

    _IMAP_BREAKER_TTL_S: float = 30.0
    """How long to skip IMAP for an account after a fallback-triggering
    failure. 30s is long enough to skip a tight burst of calls (typical
    agent workloads do many calls in succession), short enough that a
    refreshed Keychain entry or recovered network is picked up within a
    minute. Class constant — no public knob; tune by subclassing if
    really needed. See issue #118."""

    def __init__(
        self,
        timeout: int = 60,
        *,
        imap_pool: ImapConnectionPool | None = None,
        lock_timeout: float = 30.0,
        compose_ledger: ComposeLedger | None = None,
    ) -> None:
        """
        Initialize the Mail connector.

        Args:
            timeout: Timeout in seconds for AppleScript operations.
            imap_pool: Optional ImapConnectionPool. When provided, every
                IMAP-delegated call (search_messages, get_message,
                get_attachments, get_thread) reuses cached connections
                across calls, amortizing the ~400 ms TCP+TLS+LOGIN
                overhead per call. Default None (per-call lifecycle —
                the v0.5.0 behavior). See issue #75.
            lock_timeout: Seconds to wait for the cross-process Mail
                automation lock before failing with a clear busy error.
                Several callers can drive Mail at once (the resident
                daemon on behalf of many sessions, any stdio server
                beside it, a test run); unserialized concurrent
                AppleScript collides into AppleEvent timeouts (-1712) /
                invalid connections (-609).
            compose_ledger: Where every compose window the connector
                opens is recorded, and how it ended. Default: the store
                under the data home, its root resolved on each use.
        """
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.compose_ledger = compose_ledger if compose_ledger is not None else ComposeLedger()
        # Called when a composition ends with its window still open, so
        # whoever tends Mail's windows can do so now rather than at its
        # next turn. The daemon's tender sets it (tender.py); unset, the
        # window waits in the ledger.
        self.on_window_left_open: Callable[[], None] | None = None
        self._imap_pool = imap_pool
        # Accounts for which we've already logged a WARNING about IMAP failure.
        # Subsequent failures for the same account are demoted to DEBUG per
        # invariant 5 in docs/research/imap-auth-options-decision.md.
        self._imap_failures: set[str] = set()
        # Issue #118: per-account circuit breaker state. Maps account name
        # to the monotonic deadline before which IMAP is skipped entirely
        # (the orchestrator goes straight to the AppleScript path without
        # paying the connect/login round trip).
        self._imap_failure_until: dict[str, float] = {}

    def _imap_breaker_open(self, account: str) -> bool:
        """True if a recent IMAP failure on this account is still cooling
        down. Callers consult this *before* attempting IMAP — when True,
        skip IMAP entirely for this call (issue #118)."""
        deadline = self._imap_failure_until.get(account)
        return deadline is not None and time.monotonic() < deadline

    def _imap_clear_breaker(self, account: str) -> None:
        """Reset the cooldown for an account. Called after every
        successful IMAP call so a transient blip doesn't leave the
        breaker open longer than necessary."""
        self._imap_failure_until.pop(account, None)

    def _log_imap_fallback(self, account: str, exc: Exception) -> None:
        """Log an IMAP fallback event AND open the circuit breaker.

        MailKeychainEntryNotFoundError is a benign opt-out signal — always
        DEBUG, never tracked, never opens the breaker (the user explicitly
        chose not to configure IMAP for this account; cooling down would
        do nothing but cost a deadline lookup on every subsequent call).

        For any other failure: the first per-account occurrence logs
        WARNING; subsequent occurrences log DEBUG. LoginError gets a
        specialized message that names the exact `setup-imap` command —
        a stale/revoked Keychain password is the most common cause and
        the AppleScript fallback would otherwise hide the breakage from
        the user indefinitely (issue #118). For all non-benign failures,
        the breaker opens for ``_IMAP_BREAKER_TTL_S`` seconds.
        """
        if isinstance(exc, MailKeychainEntryNotFoundError):
            logger.debug(
                "IMAP not configured for %s (no Keychain entry); using AppleScript",
                account,
            )
            return

        if isinstance(exc, MailImapMoveUnsupportedError):
            # Capability gap is permanent for that server; opening the
            # 30s breaker would skip IMAP for read paths that work fine.
            logger.debug(
                "IMAP server for %s lacks MOVE/UIDPLUS; using AppleScript "
                "for the move-only patch",
                account,
            )
            return

        if isinstance(exc, MailImapTrashNotFoundError):
            # Same reasoning as above — Trash discovery failing once
            # means it'll fail every time for this server, so opening
            # the breaker would only hurt unrelated read paths.
            logger.debug(
                "IMAP server for %s has no discoverable Trash folder; "
                "using AppleScript for delete_messages",
                account,
            )
            return

        # Non-benign failure: open the breaker.
        self._imap_failure_until[account] = (
            time.monotonic() + self._IMAP_BREAKER_TTL_S
        )

        if account not in self._imap_failures:
            self._imap_failures.add(account)
            if isinstance(exc, LoginError):
                logger.warning(
                    "IMAP login rejected for %r — likely an expired or "
                    "revoked app password. To refresh: "
                    "`apple-mail-mcp setup-imap --account %s`. The "
                    "AppleScript fallback is being used in the meantime; "
                    "results will be correct but slower.",
                    account, account,
                )
            else:
                logger.warning(
                    "IMAP failed for %s (%s: %s), falling back to AppleScript; "
                    "subsequent failures for this account will log at DEBUG",
                    account,
                    type(exc).__name__,
                    exc,
                )
        else:
            logger.debug(
                "IMAP retry failed for %s: %s: %s",
                account,
                type(exc).__name__,
                exc,
            )

    def _run_applescript(self, script: str) -> str:
        """
        Execute AppleScript and return output.

        Args:
            script: AppleScript code to execute

        Returns:
            Script output as string

        Raises:
            MailAppleScriptError: If script execution fails
            MailTimeoutError: If osascript ran past ``self.timeout`` and
                was killed (a ``MailAppleScriptError`` too)
            MailAccountNotFoundError: If account not found
            MailMailboxNotFoundError: If mailbox not found
            MailMessageNotFoundError: If message not found
        """
        lock_fh = self._acquire_mail_lock()
        try:
            logger.debug(f"Executing AppleScript: {script[:200]}...")

            result = subprocess.run(
                ["/usr/bin/osascript", "-"],
                input=script,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                logger.error(f"AppleScript error: {error_msg}")

                # macOS stderr uses curly apostrophes (Can't) that won't match a
                # straight-apostrophe substring. Normalize before dispatching.
                normalized = error_msg.replace("\u2019", "'")

                # Parse error and raise appropriate exception
                if "Can't get account" in normalized:
                    raise MailAccountNotFoundError(error_msg)
                elif "Can't get mailbox" in normalized:
                    raise MailMailboxNotFoundError(error_msg)
                elif "Can't get message" in normalized:
                    raise MailMessageNotFoundError(error_msg)
                elif "Can't get rule" in normalized:
                    raise MailRuleNotFoundError(error_msg)
                else:
                    raise MailAppleScriptError(error_msg)

            output = result.stdout.strip()
            logger.debug(f"AppleScript output: {output[:200]}...")
            return output

        except subprocess.TimeoutExpired as e:
            raise MailTimeoutError(f"Script execution timeout after {self.timeout}s") from e
        except Exception as e:
            if isinstance(e, (MailAccountNotFoundError, MailMailboxNotFoundError,
                            MailMessageNotFoundError, MailAppleScriptError)):
                raise
            raise MailAppleScriptError(f"Unexpected error: {str(e)}") from e
        finally:
            mail_lock.release(lock_fh)

    def _acquire_mail_lock(self) -> IO[str]:
        """Take the cross-process Mail automation lock (``mail_lock``),
        waiting up to ``lock_timeout``. A caller that cannot have it in
        time gets a clear "busy" error naming the condition, before any
        osascript runs."""
        fh = mail_lock.acquire(self.lock_timeout)
        if fh is None:
            raise MailAppleScriptError(
                f"Mail automation busy: another process held the "
                f"Mail lock for over {self.lock_timeout:.0f}s "
                f"({mail_lock.lock_path()}). Retry "
                f"shortly; concurrent Mail automation is "
                f"serialized to prevent AppleEvent collisions."
            )
        return fh

    def list_accounts(self) -> list[dict[str, Any]]:
        """List all mail accounts.

        Returns:
            List of account dicts with keys:
              - id: account UUID (stable across name changes)
              - name: account preferences-sidebar label (e.g. "Gmail")
              - full_name: per-message display name used in outgoing
                "From" headers (e.g. "Alice Smith"), or None if no
                full name is configured for the account
              - email_addresses: list of associated email addresses
              - account_type: lowercase Mail type (e.g., "imap", "pop", "iCloud")
              - enabled: whether the account is currently enabled in Mail.app
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            repeat with acc in accounts
                set accEmails to email addresses of acc
                if accEmails is missing value then set accEmails to {}
                set accFullName to full name of acc
                if accFullName is missing value then set accFullName to ""
                set accRecord to {|id|:(id of acc as text), |name|:(name of acc), |full_name|:accFullName, |email_addresses|:accEmails, |account_type|:((account type of acc) as text), |enabled|:(enabled of acc)}
                set end of resultData to accRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        accounts = cast(list[dict[str, Any]], parse_applescript_json(result))
        # Normalize empty-string full_name to None so callers don't have
        # to distinguish two "no display name" representations.
        for acc in accounts:
            if not (acc.get("full_name") or "").strip():
                acc["full_name"] = None
        return accounts

    def _resolve_account_to_sender(self, account: str) -> str:
        """Resolve an account name, UUID, or one of the account's own
        addresses to a sender string for the AppleScript ``sender``
        property.

        Returns ``"Display Name <email>"`` when the account has a
        ``full_name`` configured (#158), or bare ``email`` as a graceful
        fallback when no full name is set. The Display-Name form is what
        recipients see in their inbox's From column.

        Used by the draft lifecycle (``create_draft`` / ``update_draft``)
        per #155. Accepts a name or UUID, matching the convention on
        ``list_mailboxes``, ``search_messages``, etc., and also an
        address in ``email`` or ``Name <email>`` form — the form
        ``get_draft_state`` reads back — matched case-insensitively
        against the account's addresses, so a rebuilt draft resolves to
        the account it was saved from.

        Raises:
            MailAccountNotFoundError: No account matches the given name/UUID.
            ValueError: Account exists but has no email addresses configured.
        """
        wanted_address = parseaddr(account)[1].lower() if "@" in account else ""
        for acc in self.list_accounts():
            emails = acc.get("email_addresses") or []
            owns_address = wanted_address and any(
                str(e).lower() == wanted_address for e in emails
            )
            if acc.get("id") == account or acc.get("name") == account or owns_address:
                if not emails:
                    raise ValueError(
                        f"Account {account!r} has no email addresses "
                        f"configured."
                    )
                email = cast(str, emails[0])
                full_name = (acc.get("full_name") or "").strip()
                if full_name:
                    return f"{full_name} <{email}>"
                return email
        raise MailAccountNotFoundError(
            f"Account {account!r} not found in Mail.app configured accounts."
        )

    def list_rules(self) -> list[dict[str, Any]]:
        """List all Mail.app rules.

        Returns:
            List of rule dicts with keys:
              - index: 1-based positional index, matching Mail.app's
                AppleScript ``rule N`` reference. Stable within a single
                snapshot; can change if the user reorders rules.
              - name: rule display name (NOT guaranteed unique — Mail
                allows duplicates).
              - enabled: whether the rule is currently enabled.

        Note:
            Mail.app does not expose a stable rule id via AppleScript;
            ``index`` is the canonical handle for downstream mutation tools
            (delete_rule / update_rule). Callers that care about
            reorder-stability should call ``list_rules`` again immediately
            before each mutation.
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            set ruleCount to count of rules
            repeat with i from 1 to ruleCount
                set r to rule i
                set ruleRecord to {|index|:i, |name|:(name of r), |enabled|:(enabled of r)}
                set end of resultData to ruleRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def _validate_rule_condition(self, cond: dict[str, Any]) -> None:
        """Validate a single RuleCondition dict.

        Required keys: field (in _RULE_FIELD_MAP), operator (in
        _RULE_OPERATOR_MAP), value (non-empty str). header_name required
        iff field == 'header_name'.
        """
        if "field" not in cond or cond["field"] not in _RULE_FIELD_MAP:
            raise ValueError(
                f"condition.field must be one of {sorted(_RULE_FIELD_MAP)}, "
                f"got {cond.get('field')!r}"
            )
        if (
            "operator" not in cond
            or cond["operator"] not in _RULE_OPERATOR_MAP
        ):
            raise ValueError(
                f"condition.operator must be one of "
                f"{sorted(_RULE_OPERATOR_MAP)}, got {cond.get('operator')!r}"
            )
        if not cond.get("value") or not isinstance(cond["value"], str):
            raise ValueError("condition.value must be a non-empty string")
        if cond["field"] == "header_name":
            if not cond.get("header_name"):
                raise ValueError(
                    "condition.header_name is required when field is "
                    "'header_name'"
                )

    def _validate_rule_actions(self, actions: dict[str, Any]) -> None:
        """Validate a RuleActions dict has at least one meaningful entry,
        flag_color (if any) is valid, and forward_to emails are valid and
        on the outbound allowlist.

        A forwarding rule is a standing send, and this is the one place
        both create_rule and update_rule pass through before any
        AppleScript runs, so the allowlist gate sits here: a refused
        rule touches nothing in Mail. Raises MailOutboundDisallowedError
        (or OutboundAllowlistUnavailableError, fail closed) for the
        forward_to that the policy does not admit."""
        meaningful_keys = {
            "move_to", "copy_to", "mark_read", "mark_flagged",
            "delete", "forward_to",
        }
        # Strip falsy bools / empty containers — they're no-ops, not actions.
        active = {
            k: v for k, v in actions.items()
            if k in meaningful_keys and v
        }
        if not active:
            raise ValueError(
                "actions must include at least one of "
                f"{sorted(meaningful_keys)} with a truthy value"
            )
        if "flag_color" in actions and actions["flag_color"]:
            # get_flag_index raises ValueError on bad input.
            get_flag_index(actions["flag_color"])
        if active.get("forward_to"):
            for addr in active["forward_to"]:
                if not isinstance(addr, str) or not validate_email(addr):
                    raise ValueError(
                        f"forward_to entries must be valid email "
                        f"addresses; got {addr!r}"
                    )
            assert_forward_targets_allowed(active["forward_to"])
        for mb_key in ("move_to", "copy_to"):
            if mb_key in active:
                ref = active[mb_key]
                if (
                    not isinstance(ref, dict)
                    or not ref.get("account")
                    or not ref.get("mailbox")
                ):
                    raise ValueError(
                        f"actions.{mb_key} must be a dict with "
                        f"'account' and 'mailbox' keys, got {ref!r}"
                    )

    def _build_action_lines(self, actions: dict[str, Any]) -> list[str]:
        """Translate a validated RuleActions dict into AppleScript lines.

        Each line operates on a variable named ``newRule`` (or ``r`` for
        update_rule's reuse). Caller picks the target variable name and
        substitutes.
        """
        lines: list[str] = []
        if actions.get("move_to"):
            mb_safe = escape_applescript_string(
                sanitize_input(actions["move_to"]["mailbox"])
            )
            acct_clause = applescript_account_clause(
                actions["move_to"]["account"]
            )
            lines.append("set should move message of newRule to true")
            lines.append(
                f'set move message of newRule to mailbox "{mb_safe}" '
                f"of {acct_clause}"
            )
        if actions.get("copy_to"):
            mb_safe = escape_applescript_string(
                sanitize_input(actions["copy_to"]["mailbox"])
            )
            acct_clause = applescript_account_clause(
                actions["copy_to"]["account"]
            )
            lines.append("set should copy message of newRule to true")
            lines.append(
                f'set copy message of newRule to mailbox "{mb_safe}" '
                f"of {acct_clause}"
            )
        if actions.get("mark_read"):
            lines.append("set mark read of newRule to true")
        if actions.get("mark_flagged"):
            lines.append("set mark flagged of newRule to true")
            if actions.get("flag_color"):
                idx = get_flag_index(actions["flag_color"])
                lines.append(
                    f"set mark flag index of newRule to {idx}"
                )
        if actions.get("delete"):
            lines.append("set delete message of newRule to true")
        if actions.get("forward_to"):
            recipients = ", ".join(actions["forward_to"])
            recipients_safe = escape_applescript_string(recipients)
            lines.append(
                f'set forward message of newRule to "{recipients_safe}"'
            )
        return lines

    def create_rule(
        self,
        name: str,
        conditions: list[dict[str, Any]],
        actions: dict[str, Any],
        match_logic: str = "all",
        enabled: bool = True,
    ) -> int:
        """Create a new Mail.app rule. Returns the new rule's 1-based index.

        Args:
            name: Rule display name.
            conditions: List of RuleCondition dicts. At least one required.
            actions: RuleActions dict. At least one action must be set.
            match_logic: 'all' (AND) or 'any' (OR) across conditions.
            enabled: Whether the rule is enabled on creation.

        Returns:
            1-based positional index of the newly-created rule (Mail.app
            appends new rules to the end, so this equals the new total
            count of rules).

        Raises:
            ValueError: If any input fails schema validation.
            MailOutboundDisallowedError: If ``actions.forward_to`` names
                an address off the outbound allowlist, or the allowlist
                cannot be read. Nothing is installed.
        """
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if not conditions:
            raise ValueError("conditions must have at least one entry")
        if match_logic not in ("all", "any"):
            raise ValueError(
                f"match_logic must be 'all' or 'any', got {match_logic!r}"
            )
        for cond in conditions:
            self._validate_rule_condition(cond)
        self._validate_rule_actions(actions)

        name_safe = escape_applescript_string(sanitize_input(name))
        all_conditions = "true" if match_logic == "all" else "false"
        enabled_str = "true" if enabled else "false"

        condition_lines: list[str] = []
        for cond in conditions:
            rule_type = _RULE_FIELD_MAP[cond["field"]]
            qualifier = _RULE_OPERATOR_MAP[cond["operator"]]
            expr_safe = escape_applescript_string(
                sanitize_input(cond["value"])
            )
            if cond["field"] == "header_name":
                header_safe = escape_applescript_string(
                    sanitize_input(cond["header_name"])
                )
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}", header:"{header_safe}"}} '
                    f"at end of rule conditions of newRule"
                )
            else:
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}"}} '
                    f"at end of rule conditions of newRule"
                )

        action_lines = self._build_action_lines(actions)

        body = (
            f'set newRule to make new rule with properties '
            f'{{name:"{name_safe}"}}\n'
            f"set all conditions must be met of newRule to {all_conditions}\n"
            + "\n".join(condition_lines) + "\n"
            + "\n".join(action_lines) + "\n"
            f"set enabled of newRule to {enabled_str}\n"
            f"return (count of rules) as text"
        )
        script = f'tell application "Mail"\n{body}\nend tell'
        return int(self._run_applescript(script))

    def update_rule(
        self,
        rule_index: int,
        name: str | None = None,
        enabled: bool | None = None,
        conditions: list[dict[str, Any]] | None = None,
        actions: dict[str, Any] | None = None,
        match_logic: str | None = None,
        expected_name: str | None = None,
    ) -> None:
        """Update an existing Mail.app rule (patch-style for top-level fields,
        full replacement for conditions/actions when provided).

        Calls ``_check_supported_actions`` first; refuses to update any rule
        whose existing action set includes something outside our schema
        (run-AppleScript, redirect, reply text, etc.) — we cannot safely
        partial-update because the unsupported actions would be silently
        dropped or misrepresented.

        Args:
            rule_index: 1-based positional index from ``list_rules``.
            name: New name (only set if not None).
            enabled: New enabled state (only set if not None).
            conditions: If provided, REPLACES all existing conditions wholesale.
            actions: If provided, REPLACES all action flags wholesale —
                unprovided actions are reset to off.
            match_logic: 'all' | 'any', only set if not None.
            expected_name: The name the caller confirmed for this index.
                When given, the update applies only if the rule at the
                index still carries that name, checked inside the same
                AppleScript call as the changes. See ``delete_rule``.

        Raises:
            ValueError: If any provided input fails schema validation.
            MailOutboundDisallowedError: If ``actions.forward_to`` names
                an address off the outbound allowlist, or the allowlist
                cannot be read. Nothing is changed.
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If the rule currently has an
                action outside the supported schema.
            MailRuleChangedError: If ``expected_name`` was given and the
                rule at the index is now a different one. Nothing was
                changed.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        if match_logic is not None and match_logic not in ("all", "any"):
            raise ValueError(
                f"match_logic must be 'all' or 'any', got {match_logic!r}"
            )
        if conditions is not None:
            # Mail.app on macOS Tahoe (16.0 / macOS 26) has a recursion bug
            # in -[MFMessageRule(Applescript) removeFromCriteriaAtIndex:].
            # ANY AppleScript path that removes a rule condition (delete by
            # index, delete every, or assigning a new list to `rule
            # conditions`) hits the same broken accessor and crashes Mail.
            # Verified with a one-line minimal repro:
            #     tell application "Mail" to delete rule condition 1 of rule "X"
            # Until Apple fixes this, replacing conditions in place is not
            # implementable; users must delete and recreate the rule.
            raise MailUnsupportedRuleActionError(
                "Replacing rule conditions is not supported: Mail.app on "
                "macOS Tahoe has a recursion bug in its AppleScript handler "
                "for rule-condition deletion (-[MFMessageRule(Applescript) "
                "removeFromCriteriaAtIndex:]) that crashes Mail. To change "
                "conditions, delete the rule and recreate it with create_rule."
            )
        if actions is not None:
            self._validate_rule_actions(actions)
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("name, if provided, must be a non-empty string")

        # Refuse to patch rules whose existing actions we don't fully model.
        self._check_supported_actions(rule_index)

        # Renaming a rule invalidates the local AppleScript variable
        # bound to it (Mail.app tries to resolve the variable by the old
        # name on subsequent property accesses, which now fails). Defer
        # any rename to the very end so all other property changes
        # operate on a stable reference.
        body_parts: list[str] = [
            f"set newRule to rule {rule_index}",
        ]

        if match_logic is not None:
            body_parts.append(
                f"set all conditions must be met of newRule to "
                f"{'true' if match_logic == 'all' else 'false'}"
            )
        if actions is not None:
            # Reset all supported action flags first; then apply provided ones.
            # `set forward message ... to ""` raises -10000 when the value is
            # already empty (Tahoe quirk), so gate the reset on a length check.
            body_parts.extend([
                "set should move message of newRule to false",
                "set should copy message of newRule to false",
                "set mark read of newRule to false",
                "set mark flagged of newRule to false",
                "set mark flag index of newRule to -1",
                "set delete message of newRule to false",
                'if forward message of newRule is not "" then '
                'set forward message of newRule to ""',
            ])
            body_parts.extend(self._build_action_lines(actions))
        # `enabled` must come AFTER the action-reset block: setting enabled
        # before resets causes the reset to silently revert it (Tahoe quirk).
        if enabled is not None:
            body_parts.append(
                f"set enabled of newRule to "
                f"{'true' if enabled else 'false'}"
            )
        # Rename last — see comment above.
        if name is not None:
            name_safe = escape_applescript_string(sanitize_input(name))
            body_parts.append(f'set name of newRule to "{name_safe}"')

        if len(body_parts) == 1:
            # Only the rule lookup, no actual updates — caller passed nothing.
            return
        tell_body = self._guarded_rule_mutation(rule_index, expected_name, body_parts)
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        outcome = cast(
            dict[str, Any], parse_applescript_json(self._run_applescript(script))
        )
        self._raise_if_rule_changed(rule_index, expected_name, outcome)

    def _check_supported_actions(self, rule_index: int) -> None:
        """Verify a rule's existing actions are all in our schema.

        Used by ``update_rule`` before applying changes — if the rule
        currently has any action set that we don't model (run-AppleScript,
        redirect, reply text, play sound, highlight color, forward text),
        we can't safely partial-update because we'd silently drop or
        misrepresent that action. Read access via ``list_rules`` is
        unaffected.

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If any action outside the
                medium-tier schema is currently set on the rule.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        tell_body = f'''
        tell application "Mail"
            set r to rule {rule_index}
            set resultData to {{|run_script_set|:(run script of r is not missing value), |play_sound_set|:(play sound of r is not missing value), |redirect_set|:((redirect message of r) is not ""), |forward_text_set|:((forward text of r) is not ""), |reply_text_set|:((reply text of r) is not ""), |highlight_text|:(highlight text using color of r), |color_message|:((color message of r) as text)}}
        end tell
        '''
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))

        unsupported: list[str] = []
        if parsed.get("run_script_set"):
            unsupported.append("run script")
        if parsed.get("play_sound_set"):
            unsupported.append("play sound")
        if parsed.get("redirect_set"):
            unsupported.append("redirect message")
        if parsed.get("forward_text_set"):
            unsupported.append("forward text")
        if parsed.get("reply_text_set"):
            unsupported.append("reply text")
        if parsed.get("highlight_text"):
            unsupported.append("highlight text using color")
        if parsed.get("color_message", "none") != "none":
            unsupported.append("color message")

        if unsupported:
            raise MailUnsupportedRuleActionError(
                f"rule {rule_index} uses actions outside the supported "
                f"schema: {', '.join(unsupported)}. Edit this rule in "
                f"Mail.app's Rules pane instead."
            )

    def delete_rule(self, rule_index: int, expected_name: str | None = None) -> str:
        """Delete a rule by 1-based index.

        Reads the rule's name in the same AppleScript call so callers
        (typically the server layer's elicitation summary) can echo the
        deleted name. After deletion, downstream rule indices shift down
        by one — callers should re-call ``list_rules`` before any further
        rule operations.

        Args:
            rule_index: 1-based positional index, as returned by ``list_rules``.
            expected_name: The name the caller confirmed for this index.
                When given, the delete applies only if the rule at the
                index still carries that name — checked inside the same
                AppleScript call, so there is no gap between the check
                and the act. Indices are positions, and positions move
                while a confirmation prompt is open.

        Returns:
            The name of the deleted rule (for confirmation / logging).

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
            MailRuleChangedError: If ``expected_name`` was given and the
                rule at the index is now a different one. Nothing was
                deleted.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        tell_body = self._guarded_rule_mutation(
            rule_index, expected_name, [f"delete rule {rule_index}"]
        )
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        outcome = cast(
            dict[str, Any], parse_applescript_json(self._run_applescript(script))
        )
        self._raise_if_rule_changed(rule_index, expected_name, outcome)
        return cast(str, outcome["name"])

    @staticmethod
    def _guarded_rule_mutation(
        rule_index: int, expected_name: str | None, statements: list[str]
    ) -> str:
        """AppleScript that applies ``statements`` to ``rule rule_index``
        only if its name is still ``expected_name``.

        The name is read and compared inside the same tell block as the
        mutation. The block sets ``resultData`` to ``{name, applied}``
        for :func:`_wrap_as_json_script`; ``applied`` is false when the
        name did not match and nothing ran. With no ``expected_name``
        the statements run unconditionally.
        """
        body = "\n".join("            " + stmt for stmt in statements)
        if expected_name is None:
            return f'''
        tell application "Mail"
            set currentName to name of rule {rule_index}
{body}
            set resultData to {{|name|:currentName, |applied|:true}}
        end tell
        '''
        name_safe = escape_applescript_string(expected_name)
        return f'''
        tell application "Mail"
            set currentName to name of rule {rule_index}
            if currentName is "{name_safe}" then
{body}
                set resultData to {{|name|:currentName, |applied|:true}}
            else
                set resultData to {{|name|:currentName, |applied|:false}}
            end if
        end tell
        '''

    @staticmethod
    def _raise_if_rule_changed(
        rule_index: int, expected_name: str | None, outcome: dict[str, Any]
    ) -> None:
        if outcome.get("applied") is True:
            return
        raise MailRuleChangedError(
            rule_index,
            expected_name=expected_name or "",
            actual_name=cast(str, outcome.get("name", "")),
        )

    def list_mailboxes(self, account: str) -> list[dict[str, Any]]:
        """List all mailboxes for an account.

        Args:
            account: Account name.

        Returns:
            List of dicts with keys: name, unread_count.

        Raises:
            MailAccountNotFoundError: If account doesn't exist.
        """
        account_clause = applescript_account_clause(account)

        tell_body = f'''
        tell application "Mail"
            set accountRef to {account_clause}
            set resultData to {{}}

            repeat with mb in mailboxes of accountRef
                set mbUnread to unread count of mb
                if mbUnread is missing value then set mbUnread to 0
                set mbRecord to {{|name|:(name of mb), |unread_count|:mbUnread}}
                set end of resultData to mbRecord
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def _resolve_imap_config(self, account: str) -> tuple[str, int, str]:
        """Query Mail.app for the IMAP connection details of an account.

        Args:
            account: Mail.app account name (e.g. "iCloud", "Gmail").

        Returns:
            Tuple of (host, port, email). `email` is Mail.app's `user name`
            property if non-empty, else falls back to the first entry of
            `email addresses`.

            `user name` is the credential Mail.app itself sends as the IMAP
            LOGIN — it's the source of truth. `email addresses` is the SMTP
            From list, which usually overlaps with `user name` for Gmail /
            Yahoo / @icloud.com-primary accounts but diverges for iCloud
            accounts whose Apple ID is on a custom domain (Apple's "Custom
            Email Domain" setup): there `email_addresses[0]` is an SMTP-
            only From alias that the IMAP server rejects with
            AUTHENTICATIONFAILED, while `user name` (the Apple ID itself)
            is what the server actually accepts. Preferring `user name`
            matches Mail.app's own behavior in every configuration we've
            seen. (#201)

        Raises:
            MailAccountNotFoundError: If the account doesn't exist.
        """
        account_clause = applescript_account_clause(account)
        tell_body = f'''
        tell application "Mail"
            set acctRef to {account_clause}
            set acctEmails to email addresses of acctRef
            if acctEmails is missing value then set acctEmails to {{}}
            set resultData to {{|host|:(server name of acctRef), |port|:(port of acctRef), |user_name|:(user name of acctRef), |email_addresses|:acctEmails}}
        end tell
        '''
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))
        email_addresses = cast(list[str], parsed.get("email_addresses") or [])
        user_name = cast(str, parsed.get("user_name") or "")
        email = user_name or (email_addresses[0] if email_addresses else "")
        return (
            cast(str, parsed["host"]),
            cast(int, parsed["port"]),
            email,
        )

    def _imap_search(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run search_messages through the IMAP path.

        Resolves host/port/email via AppleScript, fetches the password from
        Keychain, and delegates to ImapConnector. Propagates all fallback-
        triggering exceptions unchanged — the caller (search_messages) is
        responsible for catching and falling back.

        Raises:
            MailKeychainEntryNotFoundError: No opt-in (benign).
            MailKeychainAccessDeniedError: Keychain ACL refused.
            OSError (incl. socket.timeout): Network / connection failure.
            imapclient.exceptions.LoginError: Credentials rejected.
            imapclient.exceptions.IMAPClientError: Protocol or session error.
            MailAccountNotFoundError: Mail.app doesn't know this account.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.search_messages(
            mailbox=mailbox,
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            read_status=read_status,
            is_flagged=is_flagged,
            date_from=date_from,
            date_to=date_to,
            has_attachment=has_attachment,
            limit=limit,
            include_attachments=include_attachments,
            body_contains=body_contains,
            text_contains=text_contains,
        )

    def search_messages(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for messages matching criteria.

        Tries the IMAP path first (fast, server-side SEARCH). Falls back to
        AppleScript on any IMAP failure per the graceful-degradation invariants
        in docs/research/imap-auth-options-decision.md — so a user with no
        Keychain entry, a revoked password, or a dropped network still gets
        working search via AppleScript.

        When ``include_attachments=True``, every row includes an
        ``attachments`` field with the same shape as ``mail.get_attachments``
        rows (``name``, ``mime_type``, ``size``, ``downloaded``). On the IMAP
        path this is essentially free (BODYSTRUCTURE bundles into the same
        FETCH); on the AppleScript fallback, per-row attachment enumeration
        can be expensive on cold caches — see the ``include_attachments``
        notes in TOOLS.md and #142.

        ``body_contains`` and ``text_contains`` filter by message content
        (RFC 3501 ``BODY`` / ``TEXT`` semantics on IMAP; ``content of msg``
        on AppleScript). On AppleScript these can be very slow — measured
        148s for 100 cold-cache messages on a 47k-message INBOX. When the
        call commits to AppleScript and a body/text filter is set,
        ``on_warning`` (if provided) is invoked with a human-readable string
        describing the cost. See #145 / #146.
        """
        body_search = bool(body_contains or text_contains)

        if not self._imap_breaker_open(account):
            try:
                result = self._imap_search(
                    account,
                    mailbox,
                    sender_contains,
                    subject_contains,
                    read_status,
                    is_flagged,
                    date_from,
                    date_to,
                    has_attachment,
                    limit,
                    include_attachments,
                    body_contains,
                    text_contains,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        # We're committed to the AppleScript path. Warn proactively if a
        # body/text search is set — that's the multi-order-of-magnitude
        # slow case (#146).
        if on_warning is not None and body_search:
            on_warning(
                f"AppleScript body search can take minutes on large "
                f"mailboxes (measured 148s for 100 cold-cache messages on "
                f"a 47k-message Gmail INBOX). Run "
                f"`apple-mail-mcp setup-imap --account {account!r}` for "
                f"sub-second IMAP body search."
            )

        _search_args = (
            account, mailbox, sender_contains, subject_contains,
            read_status, is_flagged, date_from, date_to,
            has_attachment, limit, include_attachments, body_contains,
            text_contains,
        )
        start = time.perf_counter()
        try:
            try:
                return self._search_messages_applescript(
                    *_search_args, on_warning=on_warning
                )
            except MailAppleScriptError as exc:
                # Mail.app busy / handler temporarily unavailable; retry once.
                # With the new per-message try/on-error guards inside the
                # AppleScript, -10000 should now only escape on whole-
                # script failures (e.g. Mail.app crashed mid-call); the
                # retry is a residual safety net.
                if "(-10000)" in str(exc):
                    logger.warning(
                        "AppleScript search got -10000 on account=%r; "
                        "retrying once after 1s delay.",
                        account,
                    )
                    time.sleep(1.0)
                    return self._search_messages_applescript(
                        *_search_args, on_warning=on_warning
                    )
                raise
        finally:
            elapsed = time.perf_counter() - start
            if elapsed > _SLOW_SEARCH_THRESHOLD_SEC:
                logger.info(
                    "AppleScript search took %.1fs on account=%r mailbox=%r. "
                    "For large mailboxes, enabling IMAP delegation is "
                    "substantially faster — see the 'Optional: faster "
                    "search via IMAP' section in the project README.",
                    elapsed, account, mailbox,
                )

    def _search_messages_applescript(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """AppleScript path for search_messages (the universal baseline).

        Called directly when IMAP is not configured for the account, or as a
        fallback when the IMAP path fails for any reason (see the graceful-
        degradation invariants in docs/research/imap-auth-options-decision.md).

        Args:
            account: Account name.
            mailbox: Mailbox name.
            sender_contains: Substring match on sender.
            subject_contains: Substring match on subject.
            read_status: Filter by read status (True=read, False=unread).
            is_flagged: Filter by flagged status (True=flagged, False=not).
            date_from: Inclusive lower bound on date received. ISO 8601 YYYY-MM-DD.
            date_to: Inclusive upper bound on date received (full day included).
                ISO 8601 YYYY-MM-DD.
            has_attachment: Filter messages with/without attachments. Asked
                of each message the other criteria kept.
            limit: Maximum results.

        Returns:
            List of message dictionaries, newest first.

        Raises:
            ValueError: If date_from or date_to is not ISO 8601 YYYY-MM-DD.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If mailbox doesn't exist.
        """
        account_clause = applescript_account_clause(account)
        mailbox_safe = escape_applescript_string(sanitize_input(mailbox))

        # No `whose` clause. `messages of mb whose <filter>` makes Mail
        # evaluate the predicate across the whole mailbox before
        # returning anything: over 120 s for permissive filters on an
        # 8443-message MobileMe Sent folder, where testing each message
        # in the script took about a second.
        #
        # What costs is the Apple event, about 15 ms each, and the old
        # loop spent one per message per property it tested or returned:
        # 8.6 s for a 50-row page of the test account's INBOX
        # (2026-09-27). The script now reads a property for many
        # messages in one event. Measured there, the same day:
        #
        #   - `subject of messages of mb`, every message at once, cost
        #     about 20-30 ms; sender, date received, read and flagged
        #     status the same; `message id` about 0.13 ms a message and
        #     `properties of to recipients` about 0.3-0.4 ms a message.
        #   - `subject of messages 1 thru N of mb`, a range, cost about
        #     10 ms plus 1.3 ms a message: cheaper than an event per
        #     message, far dearer than the whole mailbox at once.
        #   - `subject of <a list of message references>` is refused
        #     (-1728): AppleScript does not distribute a property over a
        #     list, so the matched messages cannot be read as a set.
        #
        # So each criterion's property is read for the whole mailbox
        # (a cost in the mailbox's size, as the old loop's `messages of
        # mb` already was) and tested in the script, and each row
        # property is read once per run of matched positions (a cost in
        # the rows returned). Attachments have no cheap bulk form and
        # are asked one message at a time, of messages the other
        # criteria kept. The content a body or text criterion tests is
        # read over runs of the positions the other criteria kept, and
        # only those, in batches no larger than the matches still
        # wanted, so no body is read that the limit does not need; what
        # a body costs is its message's, from milliseconds to seconds
        # (see _SEARCH_CONTENT_RUN_GAP). A text criterion's subject and
        # sender are read for the whole mailbox like a criterion's
        # property. Items of those lists are
        # reached through `a reference to` the list: at the script's
        # top level `item i of aList` slows with the list's length (26 s
        # over 50000 items, 83 ms through a reference, measured without
        # Mail).
        #
        # Every bulk read sits under its own guard: one that fails is
        # warned about and redone one message at a time, with the
        # guards the old loop had (a criterion that cannot be read
        # leaves the message out with a warning; an unreadable
        # recipient list is empty with a warning). And the lists must
        # line up by position: each is checked against the id list's
        # length, a content run's ids, read after its bodies, against
        # the ones the criteria kept, each row's id against the one the
        # criteria matched, and the first and last matched positions'
        # ids are read again at the end. A mailbox that changed under
        # the reads fails one of those, and the search starts over one
        # message at a time, the old loop, over references Mail hands
        # out by id, saying so in a warning.
        #
        # The script returns {messages, warnings}; the Python side hands
        # the warnings to ``on_warning``, which ``search_messages`` lifts
        # into the response. NO SILENT ERRORS.
        criteria, date_setup = _search_criteria(
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            read_status=read_status,
            is_flagged=is_flagged,
            date_from=date_from,
            date_to=date_to,
            has_attachment=has_attachment,
            body_contains=body_contains,
            text_contains=text_contains,
        )
        tell_body = _search_script_body(
            account_clause=account_clause,
            mailbox_safe=mailbox_safe,
            criteria=criteria,
            date_setup=date_setup,
            limit=str(limit) if limit else "999999999",
            include_attachments=include_attachments,
        )

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        parsed = parse_applescript_json(result)
        # Result shape: {messages: [...], warnings: [...]}. Older
        # in-flight callers that previously expected a bare list are
        # served by the same wrapper — there are none in-tree, but
        # the dict form is the new contract.
        if isinstance(parsed, dict):
            messages = cast(
                list[dict[str, Any]], parsed.get("messages") or []
            )
            warns = cast(list[str], parsed.get("warnings") or [])
            if on_warning is not None:
                for w in warns:
                    on_warning(w)
        else:
            # Defensive fallback for any pre-existing test fixtures that
            # might emit the bare-list form.
            messages = cast(list[dict[str, Any]], parsed)
        for message in messages:
            _render_recipients(message)
        return messages

    def get_message(
        self,
        message_id: str,
        include_content: bool = True,
        *,
        headers_only: bool = False,
        account: str | None = None,
        mailbox: str | None = None,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """
        Get full message details.

        Tries the IMAP path first when both ``account`` and ``mailbox``
        are supplied AND the account has a Keychain entry. Falls back to
        AppleScript on any IMAP failure per the graceful-degradation
        invariants in docs/research/imap-auth-options-decision.md, and
        also when no account/mailbox hint is given.

        Note on identifier semantics: the IMAP path matches against the
        RFC 5322 ``Message-ID`` header (the same form ``search_messages``
        returns when delegated through IMAP). The AppleScript path
        matches Mail.app's internal numeric message id. Callers that
        obtained ``message_id`` from a `search_messages` call should
        forward the same ``account`` + ``mailbox`` to keep the paths
        consistent.

        Args:
            message_id: Message ID. RFC 5322 form for the IMAP path,
                Mail.app internal id for the AppleScript path.
            include_content: When False, ``content`` is the empty string.
            headers_only: IMAP-only optimization — fetches ``BODY[HEADER]``
                instead of the body. Silently ignored on the AppleScript
                fallback path (AppleScript always returns body content
                when ``include_content`` is True).
            account: Mail.app account name. Optional; required (with
                ``mailbox``) to enable the IMAP fast path.
            mailbox: Folder to look in for the IMAP path. Optional.

        Returns:
            Message dictionary with keys: id, rfc_message_id, subject,
            sender, to, cc, bcc, date_received, read_status, flagged,
            content; ``warnings`` when there are any, and always with
            ``attachments``.

        Raises:
            MailMessageNotFoundError: Message not found via either path.
        """
        # Numeric ids are Mail.app internal ids — IMAP only understands RFC
        # 5322 Message-IDs, so route numeric ids straight to AppleScript.
        if not message_id.strip().isdigit() and (
            account is not None
            and mailbox is not None
            and not self._imap_breaker_open(account)
        ):
            try:
                result = self._imap_get_message(
                    account=account,
                    mailbox=mailbox,
                    message_id=message_id,
                    include_content=include_content,
                    headers_only=headers_only,
                    include_attachments=include_attachments,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        return self._get_message_applescript(
            message_id, include_content, include_attachments
        )

    def _imap_get_message(
        self,
        *,
        account: str,
        mailbox: str,
        message_id: str,
        include_content: bool,
        headers_only: bool,
        include_attachments: bool,
    ) -> dict[str, Any]:
        """Run get_message through the IMAP path. Mirrors _imap_search.

        Propagates all fallback-triggering exceptions unchanged — the
        caller (get_message) catches and falls back.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.get_message(
            message_id,
            mailbox=mailbox,
            include_content=include_content,
            headers_only=headers_only,
            include_attachments=include_attachments,
        )

    def _enumerate_attachments_for_message(
        self, message_id: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Single owner of Mail.app attachment enumeration + -10000 guard.

        Locates the message by id (numeric Mail.app id or RFC 5322
        Message-ID), then walks ``mail attachments of msg`` inside a
        try/on-error block. Inline-image multipart layouts (e.g.
        Greenhouse "Security code" mails) make that walk raise
        ``errAEEventNotHandled`` (-10000); the guard catches it and
        degrades to ``([], [warning_string])`` so a found message is
        never silently turned into "not found".

        Every site that enumerates Mail.app attachments calls this
        helper. The previous policy of duplicating the inline try
        block at each callsite caused the same bug to recur and meant
        any future enumerator had to remember the guard — moving the
        guard into one function makes it impossible to forget.

        Args:
            message_id: Mail.app internal numeric id, or RFC 5322
                Message-ID (with or without angle brackets).

        Returns:
            ``(attachments, warnings)`` where ``attachments`` is a
            list of dicts (``name``, ``mime_type``, ``size``,
            ``downloaded``) and ``warnings`` is a list of strings
            describing enumeration failures. ``warnings`` is empty on
            the success path.

        Raises:
            MailMessageNotFoundError: id resolves to no message.
            MailAppleScriptError: any other AppleScript failure.
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        if message_id.strip().isdigit():
            id_where = f'whose id is "{message_id_safe}"'
        else:
            raw_bracketed = (
                message_id if message_id.startswith('<') else f'<{message_id}>'
            )
            bracketed_safe = escape_applescript_string(
                sanitize_input(raw_bracketed)
            )
            id_where = f'whose message id is "{bracketed_safe}"'

        att_walk = _attachment_walk_block(
            message_var="foundMsg", warnings_var="attWarnings", indent=12
        )

        tell_body = f'''
        tell application "Mail"
            set foundMsg to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set foundMsg to first message of mb {id_where}
                        exit repeat
                    end try
                end repeat
                if foundMsg is not missing value then exit repeat
            end repeat

            if foundMsg is missing value then
                error "Can't get message: not found"
            end if

            set attWarnings to {{}}
{att_walk}

            set resultData to {{|attachments|:attList, |warnings|:attWarnings}}
        end tell
        '''

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        try:
            result = self._run_applescript(script)
        except MailMessageNotFoundError as e:
            # The script's error says only "not found"; say which.
            raise MailMessageNotFoundError(f"Message {message_id!r} not found") from e
        parsed = cast(dict[str, Any], parse_applescript_json(result))
        attachments = cast(
            list[dict[str, Any]], parsed.get("attachments") or []
        )
        warnings = cast(list[str], parsed.get("warnings") or [])
        return attachments, warnings

    def _get_message_applescript(
        self,
        message_id: str,
        include_content: bool,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """AppleScript fallback for get_message — iterates account × mailbox.

        Slow on accounts with many mailboxes (see issue #72). Callers
        with a known account+mailbox should provide them to take the
        IMAP path instead.

        When ``include_attachments`` is True, attachment enumeration is
        delegated to :meth:`_enumerate_attachments_for_message` — the
        single owner of the inline-image -10000 guard. That adds one
        extra ``osascript`` round-trip per call (~100-300ms); the cost
        is the price of having one source of truth for the guard.
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        # Numeric ids use Mail.app's internal `id` (integer).
        # RFC 5322 ids use the `message id` (string) property with angle brackets.
        if message_id.strip().isdigit():
            id_where = f'whose id is "{message_id_safe}"'
        else:
            raw_bracketed = message_id if message_id.startswith('<') else f'<{message_id}>'
            bracketed_safe = escape_applescript_string(sanitize_input(raw_bracketed))
            id_where = f'whose message id is "{bracketed_safe}"'

        content_clause = (
            'set msgContent to content of msg'
            if include_content
            else 'set msgContent to ""'
        )
        # Guarded: the lookup's own try below would otherwise turn an
        # unreadable recipient list into "message not found".
        recipients_clause = _recipient_read_block(
            message_var="msg", warnings_var="recipWarnings", indent=24
        )

        tell_body = f'''
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb {id_where}
                        {content_clause}
                        set recipWarnings to {{}}
{recipients_clause}
                        set resultData to {{|id|:(id of msg as text), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent, {_RECIPIENT_FIELDS}, |warnings|:recipWarnings}}
                        exit repeat
                    end try
                end repeat
                if resultData is not missing value then exit repeat
            end repeat

            if resultData is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        msg = cast(dict[str, Any], parse_applescript_json(result))
        _render_recipients(msg)
        warnings = cast(list[str], msg.pop("warnings", None) or [])

        if include_attachments:
            attachments, attachment_warnings = (
                self._enumerate_attachments_for_message(message_id)
            )
            msg["attachments"] = attachments
            warnings += attachment_warnings
        # With attachments the row always says whether enumeration
        # warned; without, it carries warnings only when there are some.
        if include_attachments or warnings:
            msg["warnings"] = warnings

        return msg

    def auto_template_vars(self, message_id: str | None) -> dict[str, str]:
        """Build the auto-fill variable dict for render_template.

        With ``message_id``, calls :meth:`get_message` (without content)
        and extracts ``recipient_name``, ``recipient_email``, and
        ``original_subject`` from the original sender. Always includes
        ``today`` (ISO date). User-supplied vars are layered on top of
        this dict at the call site, so user values win on conflict.
        """
        from email.utils import parseaddr

        out: dict[str, str] = {"today": _date.today().isoformat()}
        if message_id is None:
            return out
        msg = self.get_message(message_id, include_content=False)
        sender_field = str(msg.get("sender") or "")
        display_name, email_addr = parseaddr(sender_field)
        out["recipient_email"] = email_addr or sender_field
        out["recipient_name"] = display_name or out["recipient_email"]
        out["original_subject"] = str(msg.get("subject") or "")
        return out

    def get_attachments(
        self,
        message_id: str,
        *,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get list of attachments from a message.

        Tries the IMAP path first when both ``account`` and ``mailbox``
        are supplied AND the account has a Keychain entry — one
        BODYSTRUCTURE FETCH instead of an account×mailbox AppleScript
        scan plus per-attachment property reads. Falls back to AppleScript
        on any IMAP failure per the graceful-degradation invariants in
        docs/research/imap-auth-options-decision.md.

        The IMAP path also surfaces attachment cases Mail.app's
        AppleScript layer drops silently — forwarded message/rfc822
        parts, multipart/related inline images with filenames, and
        attachments with Unicode filenames (issue #73).

        Note on identifier semantics: same as ``get_message`` — the IMAP
        path matches against the RFC 5322 ``Message-ID`` header (the
        form ``search_messages`` returns when delegated through IMAP).
        The AppleScript path matches Mail.app's internal numeric id.
        Forward the same ``account`` + ``mailbox`` you used for
        ``search_messages`` to keep the paths consistent.

        Note on ``downloaded``: on the IMAP path, ``downloaded`` is
        always ``False`` — BODYSTRUCTURE returns metadata only, and
        Mail.app's local-cache state isn't observable from the IMAP
        protocol. On the AppleScript path it reflects Mail.app's cache.
        Treat ``False`` as "may need a network fetch on save".

        Args:
            message_id: Message ID (RFC 5322 form for IMAP path,
                Mail.app internal id for AppleScript path).
            account: Mail.app account name. Optional; required (with
                ``mailbox``) to enable the IMAP fast path.
            mailbox: Folder to look in for the IMAP path. Optional.

        Returns:
            List of attachment dicts with keys ``name`` (str),
            ``mime_type`` (str), ``size`` (int), ``downloaded`` (bool).

        Raises:
            MailMessageNotFoundError: Message not found via either path.
        """
        # Numeric ids are Mail.app internal ids — bypass IMAP (same reasoning
        # as get_message).
        if not message_id.strip().isdigit() and (
            account is not None
            and mailbox is not None
            and not self._imap_breaker_open(account)
        ):
            try:
                result = self._imap_get_attachments(
                    account=account,
                    mailbox=mailbox,
                    message_id=message_id,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        return self._get_attachments_applescript(message_id)

    def _imap_get_attachments(
        self,
        *,
        account: str,
        mailbox: str,
        message_id: str,
    ) -> list[dict[str, Any]]:
        """Run get_attachments through the IMAP path. Mirrors _imap_search
        and _imap_get_message.

        Propagates all fallback-triggering exceptions unchanged — the
        caller (get_attachments) catches and falls back.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.get_attachments(message_id, mailbox=mailbox)

    def _get_attachments_applescript(
        self, message_id: str
    ) -> list[dict[str, Any]]:
        """AppleScript fallback for get_attachments — delegates to the
        shared :meth:`_enumerate_attachments_for_message` helper, which
        owns the inline-image -10000 guard.

        The public ``get_attachments`` return type is a flat
        ``list[dict]``. Warnings produced by the helper would otherwise
        disappear here, so they are logged at ``WARNING`` level — the
        operation log keeps the audit trail (NO SILENT ERRORS).
        Callers that need warnings in-band (e.g. ``get_messages``) go
        through ``_get_message_applescript``, which forwards the
        helper's warnings into the message dict.
        """
        attachments, warnings = self._enumerate_attachments_for_message(
            message_id
        )
        for w in warnings:
            logger.warning("get_attachments: %s", w)
        return attachments

    def get_thread(
        self,
        message_id: str,
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all messages in the thread containing ``message_id``.

        Tries the IMAP path first (server-side header search, no subject-
        prefilter dependency). Falls back to AppleScript on any IMAP
        failure per the graceful-degradation invariants in
        docs/research/imap-auth-options-decision.md — so a user with no
        Keychain entry, a revoked password, or a dropped network still
        gets working threading via AppleScript.

        The AppleScript path prefilters on subject, so it misses members
        whose subject was rewritten mid-thread. Whenever it is the path
        that built the result, ``on_warning`` (if given) is told so and
        why IMAP was not used, since the log line that records the
        fallback is written in this process and no caller can see it.

        Args:
            message_id: Internal Mail.app id of any message in the thread
                (the anchor). Typically obtained from search_messages or
                get_message results.
            on_warning: Receives one human-readable string when the
                result came from the AppleScript path, and on that path
                one for each recipient list of a thread row that could
                not be read.

        Returns:
            List of message dicts sorted by date_received ascending. Each
            dict has the search_messages shape: id, rfc_message_id,
            subject, sender, to, cc, bcc, date_received, read_status,
            flagged. A thread of 1 is valid (anchor with no threading
            headers).

        Raises:
            MailMessageNotFoundError: If no message with the given id exists.
        """
        anchor = self._resolve_thread_anchor_applescript(message_id)
        anchor_account = cast(str, anchor["account"])
        why = (
            f"IMAP is cooling down for account {anchor_account!r} after an "
            "earlier failure"
        )
        if not self._imap_breaker_open(anchor_account):
            try:
                result = self._imap_get_thread(anchor)
                self._imap_clear_breaker(anchor_account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(anchor_account, exc)
                if isinstance(exc, MailKeychainEntryNotFoundError):
                    why = f"IMAP is not configured for account {anchor_account!r}"
                else:
                    why = f"IMAP failed for account {anchor_account!r}: {exc}"
        if on_warning is not None:
            on_warning(
                "thread built by the AppleScript path, which prefilters on "
                "subject and misses members whose subject was rewritten "
                f"mid-thread ({why})."
            )
        return self._collect_thread_applescript(anchor, on_warning=on_warning)

    def _imap_get_thread(
        self, anchor: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """IMAP path for get_thread.

        Takes the anchor dict produced by _resolve_thread_anchor_applescript
        and delegates thread-member collection to ImapConnector. Propagates
        all fallback-triggering exceptions unchanged — the caller
        (get_thread) is responsible for catching and falling back.

        Raises:
            MailKeychainEntryNotFoundError: No opt-in (benign).
            MailKeychainAccessDeniedError: Keychain ACL refused.
            OSError (incl. socket.timeout): Network / connection failure.
            imapclient.exceptions.LoginError: Credentials rejected.
            imapclient.exceptions.IMAPClientError: Protocol or session error.
            MailAccountNotFoundError: Mail.app doesn't know this account.
        """
        account = cast(str, anchor["account"])
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.find_thread_members(
            anchor_rfc_message_id=cast(str, anchor["rfc_message_id"]),
            anchor_references=cast(list[str], anchor.get("references") or []),
        )

    def _imap_move_messages(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int:
        """IMAP path for the move-only branch of update_message (#149).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.move_messages. Propagates all fallback-triggering
        exceptions unchanged — the caller (_try_imap_move_only) catches
        and falls back via _IMAP_FALLBACK_EXCS.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.move_messages(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            destination_mailbox=destination_mailbox,
        )

    def _try_imap_move_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int | None:
        """Attempt the move-only IMAP fast path. Returns the move count
        on success, or None to signal the caller should fall through to
        the AppleScript pass.

        Caller must already have verified this is a move-only patch
        (read_status / flagged / flag_color all None) and that account +
        source_mailbox + destination_mailbox are all provided.
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_move_messages(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                destination_mailbox=destination_mailbox,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _imap_delete_messages(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
    ) -> int:
        """IMAP path for delete_messages (#150).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.delete_messages (which discovers the Trash folder
        via SPECIAL-USE \\Trash with conventional-name fallback, then
        does UID MOVE / UID COPY+STORE+EXPUNGE). Propagates all
        fallback-triggering exceptions unchanged — the caller
        (_try_imap_delete) catches and falls back via _IMAP_FALLBACK_EXCS.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.delete_messages(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
        )

    def _try_imap_delete(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
    ) -> int | None:
        """Attempt the IMAP fast path for delete_messages. Returns the
        moved-to-Trash count on success, or None when the caller should
        fall through to the AppleScript pass.

        Caller must already have verified that account + source_mailbox
        are both provided (without source_mailbox, IMAP would have to
        SEARCH every mailbox per Message-ID).
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_delete_messages(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _imap_set_read_status(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        read: bool,
    ) -> int:
        """IMAP path for the read-status-only branch of update_message (#151).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.set_read_status (\\Seen STORE — base IMAP, no
        capability check). Propagates all fallback-triggering exceptions
        unchanged.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.set_read_status(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            read=read,
        )

    def _try_imap_read_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        read: bool,
    ) -> int | None:
        """Attempt the IMAP fast path for the read-status-only branch.
        Returns updated count on success, or None to signal the caller
        should fall through to the AppleScript pass.

        Caller must already have verified this is a read-only patch
        and that account + source_mailbox are both provided.
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_set_read_status(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                read=read,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _maybe_imap_move_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#149) when this update_message
        call is a move-only patch with a source_mailbox hint. Returns the
        moved-count on success, or None when the caller should fall
        through to the AppleScript pass.

        Combined patches (move + read/flag) and patches without
        source_mailbox return None unconditionally — those stay on
        AppleScript pending #150 / #151 / #152.
        """
        move_only = (
            destination_mailbox is not None
            and read_status is None
            and flagged is None
            and flag_color is None
        )
        if not move_only or source_mailbox is None:
            return None
        return self._try_imap_move_only(
            message_ids,
            account=cast(str, account),
            source_mailbox=source_mailbox,
            destination_mailbox=cast(str, destination_mailbox),
        )

    def _maybe_imap_read_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#151) when this update_message
        call is a read-status-only patch with account + source_mailbox.
        Returns the updated count on success, or None when the caller
        should fall through to the AppleScript pass.

        Combined patches (read + move / read + flag) and patches without
        source_mailbox or account return None unconditionally — those
        stay on AppleScript pending #152.
        """
        read_only = (
            read_status is not None
            and destination_mailbox is None
            and flagged is None
            and flag_color is None
        )
        if not read_only or source_mailbox is None or account is None:
            return None
        return self._try_imap_read_only(
            message_ids,
            account=account,
            source_mailbox=source_mailbox,
            read=cast(bool, read_status),
        )

    def _imap_set_flagged_status(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        flagged: bool,
    ) -> int:
        """IMAP path for the flag-only branch of update_message (#152).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.set_flagged_status (\\Flagged STORE — base IMAP,
        no capability check). Propagates all fallback-triggering
        exceptions unchanged.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.set_flagged_status(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            flagged=flagged,
        )

    def _try_imap_flag_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        flagged: bool,
    ) -> int | None:
        """Attempt IMAP fast path for flag-only patch. Returns count
        on success, or None to fall through to AppleScript."""
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_set_flagged_status(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                flagged=flagged,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _maybe_imap_flag_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#152) when this
        update_message call is a flag-only patch (flagged set, no
        flag_color, no other fields) with account + source_mailbox.

        flag_color requires Mail.app's $MailFlagBit* keywords which
        IMAP can't set; combined patches need multiple actions in one
        AppleScript pass. Both fall through to AppleScript.
        """
        flag_only = (
            flagged is not None
            and flag_color is None
            and read_status is None
            and destination_mailbox is None
        )
        if not flag_only or source_mailbox is None or account is None:
            return None
        return self._try_imap_flag_only(
            message_ids,
            account=account,
            source_mailbox=source_mailbox,
            flagged=cast(bool, flagged),
        )

    @staticmethod
    def _build_flag_actions(
        flagged: bool | None,
        flag_color: str | None,
    ) -> list[str]:
        """Translate the (flagged, flag_color) patch into AppleScript
        action strings. Pulled out of update_message in #174 to keep
        that method below the CC ≤ 20 threshold.

        Order of precedence: ``flagged=False`` always clears regardless
        of color; ``flag_color`` set wins over bare ``flagged=True``;
        bare ``flagged=True`` defaults to red (#185 fix).
        """
        from .utils import get_flag_index, validate_flag_color

        if flagged is False:
            return [
                "set flag index of msg to -1",
                "set flagged status of msg to false",
            ]
        if flag_color is not None:
            if not validate_flag_color(flag_color):
                raise ValueError(f"Invalid flag color: {flag_color}")
            flag_index = get_flag_index(flag_color)
            flagged_status = "true" if flag_color != "none" else "false"
            return [
                f"set flag index of msg to {flag_index}",
                f"set flagged status of msg to {flagged_status}",
            ]
        if flagged is True:
            # No color → default red. flag index 0 (red) sets bare \\Flagged
            # on the IMAP server with no $MailFlagBit* keyword — same state
            # the #152 IMAP fast path produces, ensuring path-independent
            # rendering in Mail.app.
            return [
                f"set flag index of msg to {get_flag_index('red')}",
                "set flagged status of msg to true",
            ]
        return []

    def _try_imap_fast_paths(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Try each per-mutation IMAP fast path in turn (#149/#151/#152).
        Returns the first non-None result, or None if no fast path
        applies (caller falls through to the AppleScript pass).

        The three fast paths' branch conditions are mutually exclusive
        (each requires a single-field patch in its specific field), so
        order doesn't matter functionally — but the historical order
        is preserved for grep-ability against the issue numbers.

        Pulled out of update_message in #174 to keep that function
        below the CC ≤ 20 threshold; #149/#151/#152 each added a
        _maybe_imap_* call + if-check, drifting it from 21 to 24.
        Net effect of this helper: 1 call + 1 if-check at the call
        site instead of 3 of each.
        """
        for fast_path in (
            self._maybe_imap_move_only,
            self._maybe_imap_read_only,
            self._maybe_imap_flag_only,
        ):
            result = fast_path(
                message_ids,
                read_status=read_status,
                flagged=flagged,
                flag_color=flag_color,
                destination_mailbox=destination_mailbox,
                source_mailbox=source_mailbox,
                account=account,
            )
            if result is not None:
                return result
        return None

    def _get_thread_applescript(
        self,
        message_id: str,
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """AppleScript path for get_thread (the universal baseline).

        Composes _resolve_thread_anchor_applescript (call 1) and
        _collect_thread_applescript (call 2 + Python graph walk). Called
        directly when IMAP is not configured for the account, or as a
        fallback when the IMAP path fails for any reason.

        Uses Mail.app's indexed ``whose subject contains "..."`` filter as
        a pre-filter, then reconstructs the thread by walking RFC 5322
        Message-ID / In-Reply-To / References headers across the candidate
        set. Members whose subject was rewritten mid-thread are not found
        (documented limitation of this path; fixed by the IMAP path).
        ``on_warning`` receives what ``_collect_thread_applescript``
        reports.
        """
        anchor = self._resolve_thread_anchor_applescript(message_id)
        return self._collect_thread_applescript(anchor, on_warning=on_warning)

    def _resolve_thread_anchor_applescript(
        self, message_id: str,
    ) -> dict[str, Any]:
        """AppleScript call 1: resolve Mail.app internal ID to thread anchor.

        Returns a dict with keys:
            internal_id: str — the Mail.app internal id the caller passed in
                (echoed back so downstream code can use it without threading
                it separately).
            account: str — Mail.app account name the message lives in.
            rfc_message_id: str — RFC 5322 Message-ID (no angle brackets).
            subject: str — message subject.
            in_reply_to: str | None — parent's Message-ID if present.
            references: list[str] — parsed References header (bracketless,
                order preserved, duplicates removed).

        Raises:
            MailMessageNotFoundError: If no message with the given id exists.
        """
        from .utils import parse_rfc822_ids

        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        anchor_body = f'''
        tell application "Mail"
            set anchorResult to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is "{message_id_safe}"
                        set anchorInReplyTo to ""
                        set anchorRefs to ""
                        try
                            repeat with h in headers of msg
                                set hname to name of h
                                if hname is "in-reply-to" then set anchorInReplyTo to (content of h)
                                if hname is "references" then set anchorRefs to (content of h)
                            end repeat
                        end try
                        set resultData to {{|account|:(name of acc), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |in_reply_to|:anchorInReplyTo, |references_raw|:anchorRefs}}
                        set anchorResult to resultData
                        exit repeat
                    end try
                end repeat
                if anchorResult is not missing value then exit repeat
            end repeat

            if anchorResult is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        anchor_script = _wrap_as_json_script(anchor_body, timeout=self.timeout)
        try:
            anchor_raw = self._run_applescript(anchor_script)
        except MailMessageNotFoundError as e:
            # The script's error says only "not found"; say which.
            raise MailMessageNotFoundError(f"Message {message_id!r} not found") from e
        raw = cast(dict[str, Any], parse_applescript_json(anchor_raw))

        in_reply_to_raw = raw.get("in_reply_to") or ""
        references_raw = raw.get("references_raw") or ""
        return {
            "internal_id": message_id,
            "account": cast(str, raw["account"]),
            "rfc_message_id": cast(str, raw["rfc_message_id"]),
            "subject": cast(str, raw["subject"]),
            "in_reply_to": in_reply_to_raw or None,
            "references": parse_rfc822_ids(references_raw),
        }

    def _collect_thread_applescript(
        self,
        anchor: dict[str, Any],
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """AppleScript call 2 + Python graph walk.

        Takes the anchor dict produced by _resolve_thread_anchor_applescript,
        fetches subject-prefiltered candidates across all mailboxes of the
        anchor's account, and walks the reference graph to assemble the
        thread. Returns the final sorted search-shape list.

        ``on_warning`` (if given) receives each warning about a row in the
        result: a recipient list that could not be read. One about a
        candidate the walk left out is about nothing returned, and is
        dropped with it.
        """
        from .utils import normalize_subject, parse_rfc822_ids, walk_thread_graph

        account_name = cast(str, anchor["account"])
        base_subject = normalize_subject(cast(str, anchor["subject"]))
        account_safe = escape_applescript_string(sanitize_input(account_name))
        subject_safe = escape_applescript_string(sanitize_input(base_subject))
        # Guarded: the mailbox's try below would otherwise drop every
        # candidate in the mailbox over one unreadable recipient list.
        recipients_clause = _recipient_read_block(
            message_var="m", warnings_var="recipWarnings", indent=24
        )

        candidates_body = f'''
        tell application "Mail"
            set acctRef to account "{account_safe}"
            set resultData to {{}}
            repeat with mbRef in mailboxes of acctRef
                try
                    set hits to (messages of mbRef whose subject contains "{subject_safe}")
                    repeat with m in hits
                        set inReplyTo to ""
                        set refs to ""
                        try
                            repeat with h in headers of m
                                set hname to name of h
                                if hname is "in-reply-to" then set inReplyTo to (content of h)
                                if hname is "references" then set refs to (content of h)
                            end repeat
                        end try
                        set recipWarnings to {{}}
{recipients_clause}
                        set candRecord to {{|id|:(id of m as text), |rfc_message_id|:(message id of m), |in_reply_to|:inReplyTo, |references_raw|:refs, |subject|:(subject of m), |sender|:(sender of m), |date_received|:(date received of m as text), |read_status|:(read status of m), |flagged|:(flagged status of m), {_RECIPIENT_FIELDS}, |warnings|:recipWarnings}}
                        set end of resultData to candRecord
                    end repeat
                on error
                    -- Some mailboxes (e.g. Gmail smart labels) reject whose clauses; skip
                end try
            end repeat
        end tell
        '''

        candidates_script = _wrap_as_json_script(candidates_body, timeout=self.timeout)
        candidates_raw = self._run_applescript(candidates_script)
        candidates = cast(
            list[dict[str, Any]],
            parse_applescript_json(candidates_raw),
        )

        # Enrich candidates with parsed references (Python-side).
        for cand in candidates:
            cand["references_parsed"] = parse_rfc822_ids(
                cand.get("references_raw", "")
            )

        # Seed the known-id frontier: anchor + its own references.
        anchor_rfc = cast(str, anchor["rfc_message_id"])
        known_ids: set[str] = {anchor_rfc}
        in_reply_to = cast("str | None", anchor.get("in_reply_to"))
        if in_reply_to:
            known_ids.add(in_reply_to)
        known_ids.update(cast(list[str], anchor.get("references") or []))

        # Separate the anchor's own candidate row (when present) from the
        # rest. The graph walk operates on the non-anchor candidates; the
        # anchor itself always belongs in the result.
        anchor_candidate: dict[str, Any] | None = None
        non_anchor_candidates: list[dict[str, Any]] = []
        for cand in candidates:
            if cand["rfc_message_id"] == anchor_rfc and anchor_candidate is None:
                anchor_candidate = cand
            else:
                non_anchor_candidates.append(cand)

        accepted = walk_thread_graph(
            known_ids=known_ids,
            candidates=non_anchor_candidates,
        )

        # Assemble final thread: anchor (from candidates or a minimal row
        # if the anchor's own row didn't surface in the candidate set).
        thread: list[dict[str, Any]] = []
        if anchor_candidate is not None:
            thread.append(anchor_candidate)
        else:
            logger.warning(
                "get_thread: anchor (rfc=%s) not in candidate set; "
                "result row will be incomplete",
                anchor_rfc,
            )
            thread.append({
                "id": cast(str, anchor.get("internal_id") or ""),
                "rfc_message_id": cast(
                    "str | None", anchor.get("rfc_message_id")
                ),
                "subject": anchor["subject"],
                "sender": "",
                "to": [],
                "cc": [],
                "bcc": [],
                "date_received": "",
                "read_status": False,
                "flagged": False,
            })
        thread.extend(accepted)

        # Sort by date_received ascending. AppleScript emits locale-formatted
        # strings; lexicographic sort is a close-enough proxy within a thread.
        thread.sort(key=lambda m: m.get("date_received") or "")

        # Drop threading-internal scratch fields from output rows. Per
        # #148 we KEEP rfc_message_id alongside id (dual-emit), so
        # callers can hand it to the IMAP fast paths from #149/#150/
        # #151/#152 even when get_thread fell back to AppleScript. A
        # row's recipient warnings leave it for the caller.
        for m in thread:
            m.pop("in_reply_to", None)
            m.pop("references_raw", None)
            m.pop("references_parsed", None)
            _render_recipients(m)
            for warning in m.pop("warnings", None) or []:
                if on_warning is not None:
                    on_warning(warning)

        return thread

    @staticmethod
    def _destination_names(
        attachments: list[dict[str, Any]],
        selected_zero_based: list[int],
        save_directory: Path,
        overwrite: bool,
    ) -> list[str]:
        """What each selected attachment will be called on disk, and that
        those names are free to take.

        Each declared name is reduced to a safe filename (sender-controlled
        text never reaches a path expression), the batch is made distinct
        so two attachments sharing a name land in two files, and unless
        ``overwrite`` is set every name is checked against the directory
        before anything is written. Raises ``FileExistsError`` naming the
        taken files and saying how to replace them.
        """
        names = distinct_filenames([
            safe_attachment_filename(
                attachments[i].get("name"), f"attachment-{i + 1}"
            )
            for i in selected_zero_based
        ])
        if overwrite:
            return names
        taken = [n for n in names if (save_directory / n).exists()]
        if taken:
            raise FileExistsError(
                "already in the directory: " + ", ".join(taken)
                + "; nothing was written. Pass overwrite=True to replace it."
            )
        return names

    def save_attachments(
        self,
        message_id: str,
        save_directory: Path,
        attachment_indices: list[int] | None = None,
        *,
        overwrite: bool = False,
    ) -> tuple[int, list[str]]:
        """
        Save attachments from a message to a directory.

        Two-pass implementation. Pass 1 delegates metadata enumeration
        to :meth:`_enumerate_attachments_for_message` — the single
        owner of the -10000 guards. If that returns no attachments the
        call returns ``(0, warnings)`` immediately; without references
        there is nothing to save. Warnings alone do NOT stop the save:
        a per-property warning still leaves a saveable attachment. Pass
        2 runs a separate AppleScript that re-locates the message and
        saves the requested attachments by 1-based index, using the
        helper's count to bound ``attachment_indices``.

        Args:
            message_id: Mail.app internal numeric id, or RFC 5322
                Message-ID (with or without angle brackets).
            save_directory: Directory to save attachments to.
            attachment_indices: 0-based positions in the message's
                attachment list (the order ``get_messages`` reports).
                ``None`` saves all. An index the message does not have
                is refused with ``ValueError`` before anything is
                written; a message with no attachments at all returns
                ``(0, warnings)`` whatever was asked for.
            overwrite: Replace files already in ``save_directory``.
                Without it, a name that is already taken is refused
                with ``FileExistsError`` before anything is written.
                Mail's ``save`` replaces an existing file without a word
                (probed live 2026-09-11), so the check is made here, in
                Python, before pass 2 runs; a file created by someone
                else in the moment between that check and Mail's save
                is the one window this does not cover. Attachments that
                share a name within the message are always written to
                distinct files (``name (2).ext``).

        Returns:
            ``(saved_count, warnings)``.

            ``saved_count`` is the number of files written.

            ``warnings`` is a list of strings describing degradation
            (empty on the success path). It can be non-empty WITH a
            non-zero ``saved_count``: a property-level warning degrades
            the metadata, not the file. ``saved_count`` is 0 with
            warnings only when enumeration yielded no references.

        Raises:
            FileNotFoundError: save_directory doesn't exist.
            FileExistsError: a destination name is already taken and
                ``overwrite`` is False. Nothing was written.
            ValueError: save_directory path validation failed.
            MailMessageNotFoundError: id resolves to no message.
        """
        # Validate save directory
        if not save_directory.exists():
            raise FileNotFoundError(f"Save directory does not exist: {save_directory}")

        if not save_directory.is_dir():
            raise ValueError(f"Save path is not a directory: {save_directory}")

        # Prevent path traversal
        try:
            save_directory = save_directory.resolve()
            # Check for suspicious paths
            if ".." in str(save_directory):
                raise ValueError("Path traversal detected")
        except (RuntimeError, OSError) as e:
            raise ValueError(f"Invalid save directory: {e}") from e

        # Pass 1: helper owns both -10000 guards. Bail only when the walk
        # produced NO references — that is the whole-walk failure, where
        # there is genuinely nothing to save. A per-property warning (e.g.
        # an unreadable MIME type) still yields a saveable attachment, so
        # it must NOT block the save; it rides along in the return value.
        # Verified live 2026-08-27 against iCloud INBOX 1463/1465/1466:
        # MIME type raised -10000 while the files saved intact at their
        # reported sizes. See docs/research/attachment-property-10000.md.
        attachments, warnings = self._enumerate_attachments_for_message(
            message_id
        )
        if not attachments:
            return 0, warnings

        # Resolve which indices to save. Filter to the actual range so
        # the second AppleScript never references items past the end.
        n = len(attachments)
        if attachment_indices is None:
            selected_zero_based = list(range(n))
        else:
            missing = [i for i in attachment_indices if not 0 <= i < n]
            if missing:
                raise ValueError(
                    "attachment index "
                    + ", ".join(str(i) for i in missing)
                    + f" is out of range: message {message_id!r} has {n} "
                    f"attachments (indices 0 to {n - 1}); nothing was saved"
                )
            selected_zero_based = list(attachment_indices)
        if not selected_zero_based:
            return 0, warnings

        # Pass 2: save by 1-based index in a fresh AppleScript. The
        # helper already proved `mail attachments of msg` doesn't
        # raise here, so this second pass is the safe code path.
        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        dir_safe = escape_applescript_string(str(save_directory))

        if message_id.strip().isdigit():
            id_where = f'whose id is "{message_id_safe}"'
        else:
            raw_bracketed = message_id if message_id.startswith('<') else f'<{message_id}>'
            bracketed_safe = escape_applescript_string(sanitize_input(raw_bracketed))
            id_where = f'whose message id is "{bracketed_safe}"'

        indices_str = ", ".join(str(i + 1) for i in selected_zero_based)

        # The destination filename is decided HERE, in Python, and passed
        # into the script as data. It is never derived inside AppleScript
        # from `name of att`.
        #
        # An attachment's name comes from the message's own MIME headers,
        # so the sender controls it. The previous script built
        # `"<dir>/" & name of att`; `POSIX file` does not normalise the
        # string and the filesystem resolves it at write time, so a name
        # containing `..` wrote outside `save_directory`. Probed
        # 2026-09-09 — see `safe_attachment_filename` for the transcript.
        #
        # Pass 1 already returned every name, so nothing extra is read
        # from Mail to do this. It also subsumes the old in-script
        # `attachment-N` fallback: an unreadable name arrives here as a
        # non-string and takes the same fallback, and pass 1 has already
        # emitted its own warning about it.
        safe_names = self._destination_names(
            attachments, selected_zero_based, save_directory, overwrite
        )
        safe_names_literal = ", ".join(
            f'"{escape_applescript_string(n)}"' for n in safe_names
        )

        # Pass 2 emits JSON {saved, warnings} rather than a bare count.
        # Every failure gets an on-error branch: the previous unqualified
        # `try` swallowed save errors whole, so a total failure returned
        # (0, []) — "no files, no reason". Destination is built with
        # `POSIX file` per the project's AppleScript convention; a bare
        # string path raised -10000 under /private/tmp when probed
        # 2026-08-27 (see docs/research/attachment-property-10000.md).
        tell_body = f'''
        tell application "Mail"
            set foundMsg to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set foundMsg to first message of mb {id_where}
                        exit repeat
                    end try
                end repeat
                if foundMsg is not missing value then exit repeat
            end repeat

            if foundMsg is missing value then
                error "Can't get message: not found"
            end if

            set saveWarnings to {{}}
            set safeNames to {{{safe_names_literal}}}
            set attRefs to items {{{indices_str}}} of mail attachments of foundMsg
            set saveCount to 0
            set attIdx to 0
            repeat with att in attRefs
                set attIdx to attIdx + 1
                set attName to item attIdx of safeNames
                try
                    save att in (POSIX file ("{dir_safe}/" & attName))
                    set saveCount to saveCount + 1
                on error errMsg number errNum
                    set end of saveWarnings to ("attachment save failed for '" & attName & "' of message " & (id of foundMsg as text) & ": " & errMsg & " (error " & errNum & ")")
                end try
            end repeat

            set resultData to {{|saved|:saveCount, |warnings|:saveWarnings}}
        end tell
        '''

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(result))
        saved = int(parsed.get("saved") or 0)
        save_warnings = cast(list[str], parsed.get("warnings") or [])
        return saved, warnings + save_warnings

    def update_message(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None = None,
        flagged: bool | None = None,
        flag_color: str | None = None,
        destination_mailbox: str | None = None,
        account: str | None = None,
        source_mailbox: str | None = None,
        gmail_mode: bool = False,
    ) -> int:
        """
        Patch one or more messages in a single AppleScript pass.

        Consolidates ``mark_as_read`` + ``flag_message`` + ``move_messages``
        (#135). Caller sets only the fields they want changed; tool applies
        all of them in a single AppleScript pass via ``_bulk_repeat_block``.

        Order of operations: read-state and flag changes are applied first
        (in the source mailbox), then the move. IMAP requires the message
        to exist in the source folder for STORE before MOVE.

        Args:
            message_ids: List of message ids to update.
            read_status: When set, mark as read (True) / unread (False).
            flagged: When set, set flag presence. ``True`` without
                ``flag_color`` defaults to red (Mail.app's default flag
                color, matching the IMAP fast path's bare ``\\Flagged``
                rendering). ``False`` clears the flag.
            flag_color: Color name (orange, red, yellow, blue, green,
                purple, gray, none). Implies ``flagged=True`` unless
                "none". Validated.
            destination_mailbox: When set, move messages to this mailbox.
                Requires ``account`` (the destination's account).
            account: Account hosting destination (required when
                ``destination_mailbox`` is set). Also used with
                ``source_mailbox`` for narrow-path optimization.
            source_mailbox: Optional source mailbox. With ``account``,
                narrows the AppleScript scan to one mailbox. Required to
                unlock the IMAP fast path on move-only patches (#149) —
                without it, the move runs via AppleScript even when
                IMAP is configured.
            gmail_mode: Use Gmail-specific copy+delete for the move.

        IMAP fast path (#149): when the patch is move-only
        (``destination_mailbox`` is the only field set) and
        ``source_mailbox`` is provided, the move runs server-side via
        IMAP ``UID MOVE`` (RFC 6851), avoiding the AppleScript ``whose
        message id is`` linear scan that costs ~57s on a 47k-message
        mailbox. Combined patches (move + read/flag in one call) stay on
        AppleScript until siblings #150 / #151 / #152 land.

        Returns:
            Number of messages updated.

        Raises:
            ValueError: If no fields set; if exactly one of
                account/source_mailbox is given without a destination
                requirement; if flag_color invalid; if account is missing
                when destination_mailbox is set.
            MailAccountNotFoundError: account doesn't exist.
            MailMailboxNotFoundError: destination mailbox doesn't exist.
        """
        if not message_ids:
            return 0

        # At least one mutation field must be set (server tier also
        # validates; defense-in-depth here).
        if (
            read_status is None
            and flagged is None
            and flag_color is None
            and destination_mailbox is None
        ):
            raise ValueError("update_message: specify at least one field to update")

        if destination_mailbox is not None and account is None:
            raise ValueError(
                "update_message: account is required when "
                "destination_mailbox is set"
            )

        imap_count = self._try_imap_fast_paths(
            message_ids,
            read_status=read_status,
            flagged=flagged,
            flag_color=flag_color,
            destination_mailbox=destination_mailbox,
            source_mailbox=source_mailbox,
            account=account,
        )
        if imap_count is not None:
            return imap_count

        actions: list[str] = []

        if read_status is not None:
            target = "true" if read_status else "false"
            actions.append(f"set read status of msg to {target}")

        actions.extend(self._build_flag_actions(flagged, flag_color))

        # Move (always last — IMAP STORE requires source folder).
        if destination_mailbox is not None:
            if gmail_mode:
                actions.append("duplicate msg to destMailbox")
                actions.append("delete msg")
            else:
                actions.append("set mailbox of msg to destMailbox")

        repeat_block = _bulk_repeat_block(
            account=account if source_mailbox is not None else None,
            source_mailbox=source_mailbox,
            actions=actions,
            counter_var="updateCount",
        )

        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        # Set up destMailbox at script level when moving; the actions list
        # references it inside the loop.
        dest_setup = ""
        if destination_mailbox is not None:
            account_clause = applescript_account_clause(cast(str, account))
            mb_safe = escape_applescript_string(sanitize_input(destination_mailbox))
            dest_setup = (
                f'set accountRef to {account_clause}\n'
                f'            set destMailbox to mailbox "{mb_safe}" of accountRef'
            )

        script = f"""
        tell application "Mail"
            {dest_setup}
            set idList to {{{id_list}}}
            set updateCount to 0

{repeat_block}

            return updateCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def update_mailbox(
        self,
        account: str,
        name: str,
        new_name: str | None = None,
        new_parent: str | None = None,
    ) -> bool:
        """Rename and/or re-parent an existing mailbox.

        - **Rename only** (``new_name`` set, ``new_parent`` is ``None``):
          AppleScript's ``set name of mailbox X to "Y"``. Fast, no IMAP
          credentials needed.
        - **Move** (``new_parent`` set): IMAP RENAME with the destination
          path computed from ``new_parent`` + the leaf of ``name``.
          ``new_parent=""`` means move to top-level.
          Requires IMAP credentials in Keychain (#73 opt-in flow).

        At least one of ``new_name`` / ``new_parent`` must be provided.
        Combined ("move and rename") works in one IMAP RENAME.

        Args:
            account: Account name or UUID.
            name: Current mailbox name. Slash-separated for nested
                mailboxes (e.g. ``"Archive/2024"``).
            new_name: Replacement leaf name. ``None`` to keep the current
                leaf. Sanitized via ``sanitize_mailbox_name``.
            new_parent: Destination parent path. ``None`` means keep
                current parent (rename only). ``""`` (empty string) means
                move to top-level. Non-empty string means move under that
                path.

        Returns:
            True on success.

        Raises:
            ValueError: If neither ``new_name`` nor ``new_parent`` was
                provided, or ``new_name`` sanitizes to empty.
            MailUnsupportedGmailSystemLabelError: If the source ``name``
                or the resulting destination is a Gmail system label
                (``[Gmail]`` / ``[Gmail]/...``). Pre-flight refusal —
                no AppleScript or IMAP traffic. See #164.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If the source mailbox doesn't exist.
            MailImapRequiredError: If a move was requested but no IMAP
                credentials are configured for ``account``.
            MailAppleScriptError: If a rename-only path otherwise fails.
            imapclient.exceptions.IMAPClientError: If a move otherwise
                fails on the IMAP server.
        """
        from .utils import is_gmail_system_label, sanitize_mailbox_name

        if new_name is None and new_parent is None:
            raise ValueError(
                "update_mailbox requires at least one of new_name or new_parent"
            )

        if is_gmail_system_label(name):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot update Gmail system label {name!r}; Gmail's IMAP "
                f"server does not support normal RENAME for these paths "
                f"(see #164)"
            )

        sanitized_new_name: str | None = None
        if new_name is not None:
            sanitized_new_name = sanitize_mailbox_name(new_name)
            if not sanitized_new_name:
                raise ValueError(f"Invalid new_name: {new_name}")

        # ------------------------------------------------------------------
        # Rename-only path (no parent change) -> AppleScript
        # ------------------------------------------------------------------
        if new_parent is None:
            assert sanitized_new_name is not None  # narrowed by the guard above
            account_clause = applescript_account_clause(account)
            name_safe = escape_applescript_string(sanitize_input(name))
            new_name_safe = escape_applescript_string(sanitized_new_name)

            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
                try
                    set mb to mailbox "{name_safe}" of accountRef
                on error
                    error "MAILBOX_NOT_FOUND"
                end try
                set name of mb to "{new_name_safe}"
                return "success"
            end tell
            """

            try:
                result = self._run_applescript(script)
            except MailAppleScriptError as e:
                if "MAILBOX_NOT_FOUND" in str(e):
                    raise MailMailboxNotFoundError(
                        f"mailbox {name!r} not found in account {account!r}"
                    ) from e
                raise
            return result == "success"

        # ------------------------------------------------------------------
        # Move (with optional rename) -> IMAP RENAME
        # ------------------------------------------------------------------
        # Destination path = new_parent + "/" + (new_name or current leaf).
        leaf = sanitized_new_name if sanitized_new_name else name.rsplit("/", 1)[-1]
        if new_parent == "":
            destination = leaf
        else:
            destination = f"{new_parent}/{leaf}"

        if is_gmail_system_label(destination):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot move mailbox {name!r} to {destination!r}; the "
                f"destination would land in Gmail's system-label namespace "
                f"(see #164)"
            )

        try:
            host, port, email = self._resolve_imap_config(account)
            password = get_imap_password(account, email)
        except (
            MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError
        ) as e:
            raise MailImapRequiredError(
                f"moving a mailbox requires IMAP credentials for account "
                f"{account!r}; configure them via the Keychain opt-in flow"
            ) from e

        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        try:
            imap.rename_mailbox(name, destination)
        except IMAPClientError as e:
            # Map "mailbox doesn't exist" to the typed error; let other
            # IMAP errors propagate for the server layer to translate.
            msg = str(e).lower()
            if "no such" in msg or "doesn't exist" in msg or "doesn't exist" in msg:
                raise MailMailboxNotFoundError(
                    f"mailbox {name!r} not found in account {account!r}"
                ) from e
            raise
        return True

    def delete_mailbox(
        self,
        account: str,
        name: str,
        delete_messages: bool = False,
    ) -> int:
        """Delete a mailbox via IMAP DELETE.

        Mail.app's AppleScript ``delete`` command's handler refuses
        mailbox specifiers (verified by probe), so this operation is
        IMAP-only. Requires Keychain credentials per the #73 opt-in
        flow; raises ``MailImapRequiredError`` otherwise.

        Args:
            account: Account name or UUID.
            name: Mailbox name. Slash-separated for nested mailboxes.
            delete_messages: When False (default), refuse if the mailbox
                contains messages. When True, cascade-delete the mailbox
                and its contents.

        Returns:
            Number of messages that existed at delete time (0 for empty
            mailbox; positive when ``delete_messages=True`` cascaded).

        Raises:
            MailUnsupportedGmailSystemLabelError: If ``name`` is a Gmail
                system label (``[Gmail]`` / ``[Gmail]/...``). Pre-flight
                refusal — no IMAP traffic. See #164.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If the mailbox doesn't exist on
                the IMAP server.
            MailMailboxNotEmptyError: If ``delete_messages=False`` and
                the mailbox is non-empty.
            MailImapRequiredError: If no Keychain credentials.
            imapclient.exceptions.IMAPClientError: Other server-side
                error.
        """
        from .utils import is_gmail_system_label

        if is_gmail_system_label(name):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot delete Gmail system label {name!r}; Gmail's IMAP "
                f"server does not support DELETE for these paths (see #164)"
            )

        try:
            host, port, email = self._resolve_imap_config(account)
            password = get_imap_password(account, email)
        except (
            MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError
        ) as e:
            raise MailImapRequiredError(
                f"deleting a mailbox requires IMAP credentials for account "
                f"{account!r}; configure them via the Keychain opt-in flow"
            ) from e

        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        try:
            return imap.delete_mailbox(name, allow_non_empty=delete_messages)
        except ValueError as e:
            # ImapConnector raises ValueError on the non-empty refusal.
            raise MailMailboxNotEmptyError(str(e)) from e
        except IMAPClientError as e:
            msg = str(e).lower()
            if "no such" in msg or "doesn't exist" in msg or "nonexistent" in msg:
                raise MailMailboxNotFoundError(
                    f"mailbox {name!r} not found in account {account!r}"
                ) from e
            raise

    def create_mailbox(
        self,
        account: str,
        name: str,
        parent_mailbox: str | None = None,
    ) -> bool:
        """
        Create a new mailbox/folder.

        Args:
            account: Account name
            name: Name for new mailbox
            parent_mailbox: Parent mailbox for nested creation (optional)

        Returns:
            True if created successfully

        Raises:
            ValueError: If name is invalid
            MailAccountNotFoundError: If account doesn't exist
            MailAppleScriptError: If mailbox already exists
        """
        from .utils import sanitize_mailbox_name

        # Validate and sanitize name
        sanitized_name = sanitize_mailbox_name(name)
        if not sanitized_name:
            raise ValueError(f"Invalid mailbox name: {name}")

        account_clause = applescript_account_clause(account)
        name_safe = escape_applescript_string(sanitized_name)

        if parent_mailbox:
            parent_safe = escape_applescript_string(sanitize_input(parent_mailbox))
            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
                set parentMailbox to mailbox "{parent_safe}" of accountRef
                make new mailbox at parentMailbox with properties {{name:"{name_safe}"}}
                return "success"
            end tell
            """
        else:
            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
                make new mailbox at accountRef with properties {{name:"{name_safe}"}}
                return "success"
            end tell
            """

        result = self._run_applescript(script)
        return result == "success"

    def delete_messages(
        self,
        message_ids: list[str],
        permanent: bool = False,
        skip_bulk_check: bool = True,
        *,
        account: str | None = None,
        source_mailbox: str | None = None,
    ) -> int:
        """
        Delete messages (always moves to the account's Trash mailbox).

        Args:
            message_ids: List of message IDs to delete
            permanent: Reserved; currently a no-op. Mail.app's AppleScript
                dictionary exposes no path to permanent-delete that bypasses
                Trash — see issue #111. Passing True emits a
                DeprecationWarning so callers see the discrepancy clearly
                rather than silently relying on absent behavior.
            skip_bulk_check: If False, enforce bulk operation limits
            account: Optional account name (or UUID); see `source_mailbox`.
            source_mailbox: Optional source mailbox name. When provided
                together with `account`, the AppleScript narrows the scan
                to that single mailbox — O(N) instead of O(N × M × K).
                Either alone raises ValueError.

        Returns:
            Number of messages deleted (moved to Trash)

        Raises:
            ValueError: If bulk check fails, or if exactly one of
                `account`/`source_mailbox` is given.
        """
        if not message_ids:
            return 0

        # `permanent` was originally meant to bypass Trash, but empirical
        # probing of Mail.app's AppleScript surface (issue #111) found no
        # primitive that can permanently-delete:
        #   - `delete msg` always moves to the account's Trash
        #   - A second `delete` on a trashed message is a no-op
        #   - There is no `empty trash` command in the dictionary
        # Until / unless that changes, the parameter is reserved. Surface
        # the gap loudly so MCP clients don't quietly trust a ghost knob.
        if permanent:
            warnings.warn(
                "delete_messages(permanent=True) currently behaves "
                "identically to permanent=False; Mail.app's AppleScript "
                "dictionary does not expose a way to bypass Trash. "
                "Messages are moved to the account's Trash mailbox in "
                "both cases. See issue #111.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Safety check for bulk operations
        if not skip_bulk_check and len(message_ids) > 100:
            raise ValueError(
                f"Too many messages for bulk delete ({len(message_ids)}). "
                "Maximum is 100 without skip_bulk_check=True"
            )

        # IMAP fast path (#150). Requires account + source_mailbox —
        # without source_mailbox, IMAP would have to SEARCH every
        # mailbox per Message-ID, defeating the speed win. Falls
        # through to the AppleScript pass on any _IMAP_FALLBACK_EXCS
        # exception (incl. capability gaps and trash-not-found).
        if account is not None and source_mailbox is not None:
            imap_count = self._try_imap_delete(
                message_ids,
                account=account,
                source_mailbox=source_mailbox,
            )
            if imap_count is not None:
                return imap_count

        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        repeat_block = _bulk_repeat_block(
            account=account,
            source_mailbox=source_mailbox,
            actions=["delete msg"],
            counter_var="deleteCount",
        )

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set deleteCount to 0

{repeat_block}

            return deleteCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def get_selected_messages(
        self,
        include_content: bool = True,
        include_attachments: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get messages currently selected in Apple Mail.

        Args:
            include_content: Include message body (default: True)
            include_attachments: Include per-message attachment metadata
                list (name, mime_type, size, downloaded). On AppleScript,
                this can be expensive on cold caches — see #142.

        Returns:
            List of message dicts (same structure as get_message). Empty list if
            no messages are selected.

        Raises:
            MailAppleScriptError: If AppleScript execution fails
        """
        # First pass: one osascript call enumerates `selection` and
        # builds records WITHOUT attachments — body + headers only.
        # Attachment enumeration is then delegated per-message to
        # :meth:`_enumerate_attachments_for_message`, the single owner
        # of the inline-image -10000 guard. That trades the previous
        # one-shot AppleScript for one extra round-trip per selected
        # message; selections are typically 1-3, so the cost stays
        # bounded, and the guard now lives in one place.
        content_clause = (
            "set msgContent to content of msg"
            if include_content
            else 'set msgContent to ""'
        )
        recipients_clause = _recipient_read_block(
            message_var="msg", warnings_var="recipWarnings", indent=16
        )

        tell_body = f"""
        tell application "Mail"
            set resultData to {{}}
            set sel to selection
            repeat with msg in sel
                {content_clause}
                set recipWarnings to {{}}
{recipients_clause}
                set msgRecord to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent, {_RECIPIENT_FIELDS}, |warnings|:recipWarnings}}
                set end of resultData to msgRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        messages = cast(list[dict[str, Any]], parse_applescript_json(result))

        for m in messages:
            _render_recipients(m)
            warnings = cast(list[str], m.pop("warnings", None) or [])
            if include_attachments:
                attachments, attachment_warnings = (
                    self._selected_message_attachments(cast(str, m.get("id", "")))
                )
                m["attachments"] = attachments
                warnings += attachment_warnings
            # As in _get_message_applescript: with attachments the row
            # always carries warnings; without, only when there are some.
            if include_attachments or warnings:
                m["warnings"] = warnings

        return messages

    def _selected_message_attachments(
        self, msg_id: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Attachments and warnings of one message ``selection`` listed."""
        try:
            return self._enumerate_attachments_for_message(msg_id)
        except MailMessageNotFoundError:
            # The id came directly from `selection` so a "not
            # found" here means Mail.app lost the reference
            # between the two AppleScript calls (e.g. user
            # deleted or moved the message in the gap). Surface
            # as a warning rather than dropping the row.
            return [], [
                "attachment enumeration could not relocate "
                f"selected message {msg_id}"
            ]

    def delete_draft(self, draft_id: str) -> bool:
        """Move a draft to Trash (lifecycle endpoint for cancellation).

        Mail.app's ``delete`` moves the message to the Deleted Messages
        mailbox. Recovery from Trash is technically possible but Mail.app
        no longer treats a trashed draft as editable, so this is
        effectively a one-way discard.

        Args:
            draft_id: Mail.app internal draft id (from ``create_draft``).

        Returns:
            True if a draft with that id was found and trashed.

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
        """
        _validate_draft_id(draft_id)

        # `drafts mailbox` is Mail's own aggregate of every account's drafts
        # mailbox, under whatever name the locale gives it. Matching the
        # English name "Drafts" found nothing on a localised Mail and also
        # matched any user folder with "Drafts" in its name.
        script = f"""
        tell application "Mail"
            set m to missing value
            try
                set m to first message of drafts mailbox whose id is "{draft_id}"
            end try
            if m is missing value then return "NOT_FOUND"
            delete m
            return "OK"
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "OK":
            return True
        raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

    def find_message_by_message_id(
        self, rfc5322_message_id: str
    ) -> str | None:
        """Resolve an RFC 5322 Message-ID header to Mail's internal id.

        Used by ``update_draft`` to recover a reply seed from a saved
        draft's ``In-Reply-To`` header, and by ``create_draft`` to accept
        the bracketless RFC ids that read tools emit on the IMAP path
        (#148 / #205).

        Args:
            rfc5322_message_id: e.g. ``<calendar-abc123@google.com>`` or
                ``calendar-abc123@google.com``. Brackets are stripped
                from the input; the AppleScript ``whose`` clause then
                queries for both the bare and bracketed forms in one
                pass. Mail.app's ``message id`` property storage
                normalization is not uniform — IMAP-backed accounts
                (iCloud, Gmail) store the value bare, while other paths
                may store with angle brackets per RFC 5322. Querying
                both forms in a single clause is robust to either
                convention and matches in one round-trip.

        Returns:
            Mail's internal numeric id (as a string) of the first
            matching message found, or None if no message with that
            Message-ID exists in any mailbox.
        """
        if not rfc5322_message_id:
            return None
        bare = rfc5322_message_id
        if bare.startswith("<") and bare.endswith(">"):
            bare = bare[1:-1]
        bracketed = f"<{bare}>"
        safe_bare = escape_applescript_string(sanitize_input(bare))
        safe_bracketed = escape_applescript_string(sanitize_input(bracketed))

        script = f"""
        tell application "Mail"
            set foundId to ""
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        try
                            set m to first message of mb whose (message id is "{safe_bare}" or message id is "{safe_bracketed}")
                            set foundId to (id of m as text)
                            exit repeat
                        end try
                    end repeat
                end try
                if foundId is not "" then exit repeat
            end repeat
            if foundId is "" then
                return "NOT_FOUND"
            else
                return foundId
            end if
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "NOT_FOUND" or not result:
            return None
        return result

    def get_draft_state(self, draft_id: str) -> dict[str, Any]:
        """Read recipients, subject, body, sender, threading headers, and
        attachment names from a saved draft.

        Used by ``update_draft`` to merge the caller's overrides with
        the draft's current state before recreate-and-delete. The sender
        is read back so the recreated draft stays in the account the
        draft was saved from rather than moving to Mail's default. The
        account is the one Mail resolves the draft's mailbox to (the
        aggregate ``drafts mailbox`` yields the concrete per-account
        message), so a tool acting on a draft by id can say which
        account it is about to touch; it is ``""`` for a draft whose
        mailbox has no account (a local one).

        Iterates Mail's aggregate ``drafts mailbox`` manually (rather than
        `whose id is`) because newly-created drafts can take a moment to
        be queryable via whose-clause; iteration is reliable and Drafts
        mailboxes are typically small. The aggregate covers every
        account's drafts mailbox whatever the locale names it. A draft
        that vanishes between the listing and the walk reaching it (Mail
        re-saved a draft saved through its scripting dictionary with a
        named sender under a new id 8–31 s after the save,
        docs/research/icloud-draft-resync.md; this connector no longer
        saves that way, but another client may, and a draft can be
        deleted mid-walk) is skipped rather than failing the walk: it is
        not the draft asked for, and if it was, not-found is the truth.

        Returns:
            ``{
                "draft_id": "...",
                "to":  [...email...],
                "cc":  [...email...],
                "bcc": [...email...],
                "subject": "...",
                "body": "...",
                "in_reply_to": "<msg-id>" | "",
                "references": "<msg-id> ..." | "",
                "attachment_names": ["foo.pdf", ...],
                "sender": "Name <email>" | "email" | "",
                "account": "<account name>" | "",
            }``

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
        """
        _validate_draft_id(draft_id)

        tell_body = f"""
        tell application "Mail"
            set targetId to "{draft_id}"
            set foundDraft to missing value
            repeat with d in messages of drafts mailbox
                set candId to ""
                try
                    set candId to (id of d as text)
                end try
                if candId is targetId then
                    set foundDraft to d
                    exit repeat
                end if
            end repeat

            if foundDraft is missing value then
                set resultData to {{|found|:false}}
            else
                set toList to {{}}
                try
                    repeat with r in to recipients of foundDraft
                        set end of toList to (address of r)
                    end repeat
                end try
                set ccList to {{}}
                try
                    repeat with r in cc recipients of foundDraft
                        set end of ccList to (address of r)
                    end repeat
                end try
                set bccList to {{}}
                try
                    repeat with r in bcc recipients of foundDraft
                        set end of bccList to (address of r)
                    end repeat
                end try

                set inReplyTo to ""
                set refs to ""
                try
                    repeat with h in headers of foundDraft
                        set hname to (name of h)
                        if hname is "In-Reply-To" then set inReplyTo to (content of h)
                        if hname is "References" then set refs to (content of h)
                    end repeat
                end try

                set attNames to {{}}
                try
                    repeat with a in mail attachments of foundDraft
                        try
                            set end of attNames to (name of a)
                        end try
                    end repeat
                end try

                set draftSubject to ""
                try
                    set draftSubject to (subject of foundDraft)
                end try
                set draftBody to ""
                try
                    set draftBody to (content of foundDraft)
                end try
                set draftSender to ""
                try
                    set draftSender to (sender of foundDraft)
                end try
                set draftAccount to ""
                try
                    set draftAccount to (name of account of mailbox of foundDraft)
                end try

                set resultData to {{|found|:true, |draft_id|:targetId, |to|:toList, |cc|:ccList, |bcc|:bccList, |subject|:draftSubject, |body|:draftBody, |in_reply_to|:inReplyTo, |references|:refs, |attachment_names|:attNames, |sender|:draftSender, |account|:draftAccount}}
            end if
        end tell
        """

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        data = parse_applescript_json(raw)
        if not isinstance(data, dict) or not data.get("found"):
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")
        # Drop the internal flag from the user-visible payload.
        data.pop("found", None)
        return cast(dict[str, Any], data)

    def _maybe_resolve_rfc_seed_id(
        self, seed: str, seed_id: str | None
    ) -> str | None:
        """Translate an RFC 5322 Message-ID seed into Mail's internal id.

        Read tools (#148) emit the bracketless RFC id as ``id`` on the
        IMAP path; passing that value to ``create_draft(reply_to=...)``
        used to fail because the AppleScript ``whose id is`` clause
        matches Mail's internal numeric id only. Mail's internal id is a
        stringified long integer with no ``@``, so ``'@' in seed_id`` is
        an unambiguous discriminator. (#205)

        Returns ``seed_id`` unchanged for the ``new`` seed, for empty
        seeds, or for non-RFC ids. Raises ``MailMessageNotFoundError``
        when an RFC id is passed but doesn't match any message.
        """
        if seed not in ("reply", "forward") or not seed_id or "@" not in seed_id:
            return seed_id
        resolved = self.find_message_by_message_id(seed_id)
        if resolved is None:
            raise MailMessageNotFoundError(
                f"no message with message-id {seed_id!r}"
            )
        return resolved

    @staticmethod
    def _validate_compose_args(
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
            raise ValueError(
                f"seed must be 'new', 'reply', or 'forward'; got {seed!r}"
            )
        if seed in ("reply", "forward"):
            if not seed_id:
                raise ValueError(f"seed_id is required for seed={seed!r}")
        else:  # seed == "new"
            if not to:
                raise ValueError("'to' is required when seed='new'")
            if not subject:
                raise ValueError("'subject' is required when seed='new'")

    @staticmethod
    def _build_creation_block(
        seed: str,
        seed_id_safe: str | None,
        reply_all: bool,
        subject_safe: str | None,
    ) -> str:
        """Per-seed AppleScript fragment that produces ``theMessage`` in a
        visible compose window, which is where every message the
        connector composes is written (``_compose``).

        A fresh message is ``make new outgoing message`` with
        ``visible:true``, its subject, and the one-space body seed
        (``_BODY_SEED``), which the composition pastes over.

        A reply or forward is Mail's own verb (``reply`` / ``reply to
        all`` / ``forward``) with ``opening window true``, after the
        cross-account ``whose id is "{seed_id_safe}"`` lookup.
        ``seed_id_safe`` is expected to be Mail's internal id — callers
        route RFC 5322 ids through ``_maybe_resolve_rfc_seed_id`` first
        (#205). Opened without a window, what Mail wrote is not readable
        before send, and any edit of it through the dictionary replaced
        the quote or forwarded message and dropped a forward's
        attachments (measured 2026-09-26, see ``_compose``).
        """
        if seed == "new":
            return (
                f'set theMessage to make new outgoing message with properties '
                f'{{subject:"{subject_safe}", visible:true, content:"{_BODY_SEED}"}}'
            )
        if seed == "reply":
            verb = "reply to all" if reply_all else "reply"
        else:  # forward
            verb = "forward"
        return f"""
            set origMsg to missing value
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        try
                            set origMsg to first message of mb whose id is "{seed_id_safe}"
                            exit repeat
                        end try
                    end repeat
                end try
                if origMsg is not missing value then exit repeat
            end repeat
            if origMsg is missing value then error "SEED_NOT_FOUND"
            set theMessage to {verb} origMsg opening window true
        """

    @staticmethod
    def _as_verified_send_block() -> str:
        """AppleScript fragment: verified Send click with mechanical
        read-back (Phase 0 of PLAN-html-reply-send, grounded in
        docs/reference/UI_GROUNDING_MAIL_SEND.md).

        Caller must set ``composeName``, the compose window's exact AX
        name, beforehand.

        The fragment sets ``sendOutcome`` to one of:
          "SENT" | "WINDOW_NOT_FOUND:…" | "NO_SEND_BUTTON:…" |
          "SEND_DISABLED:…" | "SHEET:…" | "WINDOW_STILL_OPEN:…"
        After the click it polls the window, once a second for 15 s: gone
        is SENT, since Mail closes the window when it takes the message;
        a sheet on it is SHEET:… with the sheet's text; still open with no
        sheet at the end is WINDOW_STILL_OPEN:…, not sent, which the
        caller's salvage saves to Drafts. It never returns early, so
        callers can run cleanup (e.g. clipboard restore) before returning
        ``sendOutcome``.

        Why each check exists (observed live, 2026-07-20, unless noted):
          - window resolved BY NAME — ``window 1`` may be the viewer;
          - ``enabled`` gate — clicking a disabled Send button is a silent
            no-op (the vanished-send mechanism);
          - sheet surfacing — a mid-send sheet means NOT dispatched; its
            static texts go into the outcome instead of a blind Cancel;
          - the window gone, and nothing about Sent. This block once also
            waited for a message with the subject in Sent. For a subject
            Sent already held, any earlier message satisfied that; for a
            new one, a copy slower than 15 s would make a message that
            went read as not sent, inviting a second send. That is read
            from the code (2026-09-27), not seen happen. Which copy the
            send filed is looked for afterwards, by identity
            (``_find_sent_copy``).
        """
        return """
        set sendOutcome to missing value
        set sendClicked to false
        set readyState to ""
        -- Precondition with bounded wait: after a reopen, Mail populates
        -- recipients asynchronously and Send stays disabled until then —
        -- clicking early is a silent no-op (the vanished-send mechanism).
        repeat 10 times
            set readyState to ""
            tell application "System Events"
                tell application process "Mail"
                    if not (exists window composeName) then
                        set readyState to "WINDOW_NOT_FOUND:" & ((name of windows) as text)
                    else
                        set tbBtns to (buttons of (first toolbar of window composeName) whose description is "Send")
                        if (count of tbBtns) is 0 then
                            set readyState to "NO_SEND_BUTTON:" & composeName
                        else
                            set sendBtn to item 1 of tbBtns
                            if not (enabled of sendBtn) then
                                set readyState to "SEND_DISABLED"
                            else
                                click sendBtn
                                set sendClicked to true
                            end if
                        end if
                    end if
                end tell
            end tell
            if sendClicked then exit repeat
            delay 1
        end repeat
        if not sendClicked then
            if readyState is "SEND_DISABLED" then
                set sendOutcome to "SEND_DISABLED:Send never became enabled within 10s on " & composeName & " (recipients missing or not yet populated)"
            else
                set sendOutcome to readyState
            end if
        end if
        if sendOutcome is missing value then
            repeat 15 times
                delay 1
                set winOpen to false
                set sheetInfo to ""
                tell application "System Events"
                    tell application process "Mail"
                        if exists window composeName then
                            set winOpen to true
                            if exists (first sheet of window composeName) then
                                try
                                    -- NB: "st" is a reserved AppleScript token; do not shorten this name.
                                    repeat with sheetTextEl in static texts of (first sheet of window composeName)
                                        set sheetInfo to sheetInfo & (value of sheetTextEl) & " | "
                                    end repeat
                                end try
                                set sheetInfo to "SHEET:" & sheetInfo
                            end if
                        end if
                    end tell
                end tell
                if sheetInfo is not "" then
                    set sendOutcome to sheetInfo
                    exit repeat
                end if
                if not winOpen then
                    set sendOutcome to "SENT"
                    exit repeat
                end if
            end repeat
            if sendOutcome is missing value then
                set sendOutcome to "WINDOW_STILL_OPEN:no sheet, and still open 15s after Send was clicked on " & composeName
            end if
        end if
        """

    @staticmethod
    def _as_new_compose_window_block() -> str:
        """AppleScript fragment: name the compose window just opened (by
        a reply or forward verb, or by ``make new outgoing message``),
        by comparing Mail's window names before and after, counting each
        name. A name already open does not hide a new window of the same
        name: a draft saved through the dictionary, as this connector's
        once were, leaves its window behind
        (docs/research/icloud-draft-resync.md, Observation 5), and a
        plain set difference then saw no new window at all (2026-09-26).

        Needs ``beforeNames`` (System Events' ``name of windows`` of Mail)
        set before the window is opened; sets ``newName``. Errors NO_COMPOSE_WINDOW
        when no window appeared within 5 s, and
        COMPOSE_WINDOW_NOT_UNIQUE when the new window shares its name
        with one already open: every later step (paste, read-back,
        verified send, discard, save) addresses the window by name, so it
        could act on the other one. That window is left open, not closed,
        since which of the two is the new one cannot be told by name.
        """
        return """
-- Identify the new compose window by comparing counted names (bounded poll).
set newName to ""
set sameNamed to 0
repeat 10 times
    delay 0.5
    tell application "System Events"
        tell application process "Mail"
            set afterNames to name of windows
        end tell
    end tell
    repeat with n in afterNames
        set nm to (n as text)
        set afterCount to 0
        repeat with m in afterNames
            if (m as text) is nm then set afterCount to afterCount + 1
        end repeat
        set beforeCount to 0
        repeat with m in beforeNames
            if (m as text) is nm then set beforeCount to beforeCount + 1
        end repeat
        if afterCount > beforeCount then
            set newName to nm
            set sameNamed to afterCount
            exit repeat
        end if
    end repeat
    if newName is not "" then exit repeat
end repeat
if newName is "" then error "NO_COMPOSE_WINDOW: no compose window appeared within 5 s"
if sameNamed > 1 then error "COMPOSE_WINDOW_NOT_UNIQUE: Mail opened a compose window named " & newName & " while another window of that name was open, so it cannot be addressed safely and was left open; close the other window of that name and retry"
"""

    @staticmethod
    def _as_discard_compose_block(win_name_var: str) -> str:
        """AppleScript fragment: discard a compose window with read-back.

        ``win_name_var`` is the AppleScript variable holding the window
        name. Both Mail-dictionary discards (``close … saving no``,
        ``delete outgoing message``) fail silently (observed live) — the
        only working route is the close button + the "Save this message as
        a draft?" sheet. The Don't Save button's real name carries a curly
        apostrophe (U+2019); a straight quote never matches. Sets
        ``discardOutcome`` to "DISCARDED" or "DISCARD_FAILED:<name>".
        """
        return f"""
        set discardOutcome to "DISCARDED"
        tell application "System Events"
            tell application process "Mail"
                if exists window {win_name_var} then
                    -- Close button by subrole — `button 1` is "add contacts"
                    -- on compose windows (observed live 2026-07-20).
                    click (first button of window {win_name_var} whose subrole is "AXCloseButton")
                    delay 0.8
                    if exists window {win_name_var} then
                        if exists (first sheet of window {win_name_var}) then
                            click button "Don’t Save" of first sheet of window {win_name_var}
                            delay 0.8
                        end if
                    end if
                    if exists window {win_name_var} then
                        set discardOutcome to "DISCARD_FAILED:" & {win_name_var}
                    end if
                end if
            end tell
        end tell
        """

    @staticmethod
    def _paste_probe_strings(body: str, plain: bool = False) -> tuple[str, str]:
        """Compute the paste read-back probes for a body.

        Returns ``(snippet, raw_marker)``:
          - ``snippet``: first chunk of the TAG-STRIPPED text content —
            after a successful paste this text must be readable in the
            WebArea (arrival check). Empty when the HTML has no text
            content (checks degrade gracefully).
          - ``raw_marker``: first chunk of the raw source when it starts
            with a tag — if this appears LITERALLY in the WebArea, the
            paste degraded to plain text (the 2026-07-21 raw-``<p>``
            regression; see docs/reference/UI_GROUNDING_MAIL_SEND.md).

        A ``plain`` body is its own text: the snippet is the text itself
        and there is no raw marker, since angle brackets in it are text.
        """
        if plain:
            return " ".join(body.split())[:24].strip(), ""
        text = re.sub(r"<[^>]+>", " ", body)
        text = " ".join(text.split())
        snippet = text[:24].strip()
        raw_marker = body.strip()[:16] if body.lstrip().startswith("<") else ""
        return snippet, raw_marker

    @staticmethod
    def _text_paste_fill(body: str, plain: bool) -> str:
        """AppleScript that puts ``body`` on the pasteboard ``pb``: as
        HTML, or as plain text when ``plain``."""
        body_safe = escape_applescript_string(sanitize_input(body))
        flavor = "public.utf8-plain-text" if plain else "public.html"
        return (
            f'set theBody to "{body_safe}"\n'
            "set bodyNSString to current application's NSString's "
            "stringWithString:theBody\n"
            "set bodyData to bodyNSString's dataUsingEncoding:"
            "(current application's NSUTF8StringEncoding)\n"
            "pb's clearContents()\n"
            f'pb\'s setData:bodyData forType:"{flavor}"'
        )

    @staticmethod
    def _files_paste_fill(attachment_paths: list[Path]) -> str:
        """AppleScript that puts the files on the pasteboard ``pb`` as
        file URLs, which Mail pastes into a compose body as attachments,
        as it does a drag from Finder."""
        paths_safe = ", ".join(
            f'"{escape_applescript_string(str(Path(p).resolve()))}"'
            for p in attachment_paths
        )
        return (
            "set fileURLs to current application's NSMutableArray's array()\n"
            f"repeat with apath in {{{paths_safe}}}\n"
            "    (fileURLs's addObject:(current application's NSURL's "
            "fileURLWithPath:(apath as text)))\n"
            "end repeat\n"
            "pb's clearContents()\n"
            "pb's writeObjects:fileURLs"
        )

    @staticmethod
    def _build_paste_script(
        *,
        window_name: str,
        fill: str,
        placement: _PastePlacement,
        undo_first: bool,
    ) -> str:
        """Full osascript source: paste into the named compose window's
        body whatever ``fill`` puts on the pasteboard (``_text_paste_fill``
        or ``_files_paste_fill``), at ``placement`` (``_PASTE_CARET_KEYS``):

          1. readiness — poll until the body WebArea EXISTS (window-name
             alone races WebKit initialization);
          2. focus — ``set focused`` then VERIFY AXFocusedUIElement is the
             WebArea (a click can leave focus in the To field, sending
             cmd+v to the wrong control);
          3. caret, paste, then restore the clipboard IMMEDIATELY
             (shortest possible hold; restore also runs on every error
             path).

        Returns "PASTED_UNVERIFIED" — what the paste did is read back in
        a SEPARATE osascript run (``_build_readback_script``,
        ``_build_attachment_ax_verify_script``): within one process
        System Events serves a stale AX subtree after WebKit re-renders,
        so an in-script read-back sees nothing (observed live).
        ``undo_first`` prepends cmd+z for the retry attempt.
        """
        win_safe = escape_applescript_string(window_name)
        undo_block = (
            'keystroke "z" using command down\n            delay 0.5'
            if undo_first
            else ""
        )
        caret_keys = _PASTE_CARET_KEYS[placement]
        return f"""
use framework "AppKit"
use framework "Foundation"
use scripting additions

set composeName to "{win_safe}"

set pb to current application's NSPasteboard's generalPasteboard()
set savedTypes to (pb's types()) as list
set savedPairs to {{}}
repeat with t in savedTypes
    set theData to (pb's dataForType:(t as text))
    if theData is not missing value then
        set end of savedPairs to {{pbType:(t as text), pbData:theData}}
    end if
end repeat

{fill}

try
    tell application "Mail" to activate
    set bodyArea to missing value
    repeat 15 times
        tell application "System Events"
            tell application process "Mail"
                if exists window composeName then
                    set w to window composeName
                    repeat with g in groups of w
                        try
                            set sa to scroll area 1 of group 1 of g
                            set waList to (UI elements of sa whose role is "AXWebArea")
                            if (count of waList) > 0 then
                                set bodyArea to item 1 of waList
                                exit repeat
                            end if
                        end try
                    end repeat
                end if
            end tell
        end tell
        if bodyArea is not missing value then exit repeat
        delay 0.5
    end repeat
    if bodyArea is missing value then error "NO_BODY_AREA:webarea never appeared in " & composeName
    set focusOK to false
    tell application "System Events"
        tell application process "Mail"
            if exists menu item "Make Rich Text" of menu "Format" of menu bar 1 then
                click menu item "Make Rich Text" of menu "Format" of menu bar 1
                delay 0.3
            end if
            repeat 5 times
                set focused of bodyArea to true
                delay 0.3
                try
                    if role of (value of attribute "AXFocusedUIElement" of it) is "AXWebArea" then
                        set focusOK to true
                        exit repeat
                    end if
                end try
                click bodyArea
                delay 0.3
            end repeat
        end tell
    end tell
    if not focusOK then error "PASTE_FOCUS_FAILED:body area would not take keyboard focus in " & composeName
    tell application "System Events"
        tell application process "Mail"
            {undo_block}
            {caret_keys}
            keystroke "v" using command down
            delay 0.8
        end tell
    end tell
on error errText
    pb's clearContents()
    repeat with pair in savedPairs
        pb's setData:(pbData of pair) forType:(pbType of pair)
    end repeat
    return errText
end try

pb's clearContents()
repeat with pair in savedPairs
    pb's setData:(pbData of pair) forType:(pbType of pair)
end repeat

return "PASTED_UNVERIFIED"
"""

    @staticmethod
    def _as_salvage_compose_block(name_expr: str) -> str:
        """AppleScript fragment: close the compose window named
        ``name_expr`` (an AppleScript string literal or variable) SAVING
        it as a draft, and read back that it closed. Sets
        ``salvageOutcome`` to "SALVAGED" (with a note on Mail's send-error
        sheet when there was one), "NO_WINDOW", or "SALVAGE_FAILED:…".

        Only when that name is the only window of it: a close addresses
        the window by name, and with two of the name it may close the
        other, which could be a person's (the rule
        ``_as_new_compose_window_block`` holds when a window opens).
        With more than one, none is closed.

        A send Mail could not make through the account's server leaves a
        sheet on the window, "Cannot send message using the server …",
        whose buttons are Try Later, Try With Selected Server, Connection
        Doctor, Edit SMTP Server List and Edit Message (seen 2026-09-27),
        and no Save. Found (by its Edit Message button), its text is
        read, Edit Message pressed, and the window then closed with Save
        as any other; the outcome carries the text, as "(Mail's
        send-error sheet: …)", so the caller's error says why Mail did
        not send. It cannot be provoked on demand, and no live test has
        met it.
        """
        win = f"window {name_expr}"
        return f"""
set salvageOutcome to ""
tell application "System Events"
    tell application process "Mail"
        set sameNamed to count of (windows whose name is {name_expr})
        if sameNamed is 0 then
            set salvageOutcome to "NO_WINDOW"
        else if sameNamed > 1 then
            set salvageOutcome to "SALVAGE_FAILED:" & sameNamed & " windows are named " & {name_expr} & ", so which to close cannot be told by name; none was closed"
        else
            set sheetNote to ""
            try
                if exists (first sheet of {win}) then
                    if exists button "Edit Message" of first sheet of {win} then
                        set sheetText to ""
                        try
                            -- NB: "st" is a reserved AppleScript token; do not shorten this name.
                            repeat with sheetTextEl in static texts of first sheet of {win}
                                set sheetText to sheetText & (value of sheetTextEl) & " | "
                            end repeat
                        end try
                        set sheetNote to " (Mail's send-error sheet: " & sheetText & ")"
                        click button "Edit Message" of first sheet of {win}
                        repeat 10 times
                            if not (exists (first sheet of {win})) then exit repeat
                            delay 0.3
                        end repeat
                    end if
                end if
                click (first button of {win} whose subrole is "AXCloseButton")
                delay 0.8
                if exists {win} then
                    if exists (first sheet of {win}) then
                        click button "Save" of first sheet of {win}
                        delay 0.8
                    end if
                end if
                if exists {win} then
                    set salvageOutcome to "SALVAGE_FAILED:window still open" & sheetNote
                else
                    set salvageOutcome to "SALVAGED" & sheetNote
                end if
            on error errMsg
                set salvageOutcome to "SALVAGE_FAILED:" & errMsg & sheetNote
            end try
        end if
    end tell
end tell
"""

    def _salvage_compose_to_draft(self, window_name: str) -> str:
        """Close a compose window SAVING it as a draft (best effort),
        through ``_as_salvage_compose_block``.

        Failure policy on a headless machine (Jonah, 2026-07-23): a
        failed send attempt must never park an open compose window —
        salvage the content to Drafts (close button → "Save" on the
        save sheet) so nothing is lost and nothing blocks later UI
        automation. Returns "SALVAGED", "NO_WINDOW", or the observed
        state on failure — callers append this to their error, never
        mask the original failure with it.
        """
        win_safe = escape_applescript_string(window_name)
        try:
            return self._run_applescript(
                self._as_salvage_compose_block(f'"{win_safe}"')
                + "\nreturn salvageOutcome"
            ).strip()
        except MailAppleScriptError as exc:
            return f"SALVAGE_FAILED:{exc}"

    def _discard_compose_window(self, window_name: str) -> str:
        """Close a compose window without saving it
        (``_as_discard_compose_block``); returns what the block read back,
        "DISCARDED" or "DISCARD_FAILED:…", or "DISCARD_FAILED:<error>"
        when the script itself failed."""
        win_safe = escape_applescript_string(window_name)
        try:
            return self._run_applescript(
                f'set discardName to "{win_safe}"\n'
                + self._as_discard_compose_block("discardName")
                + "\nreturn discardOutcome"
            ).strip()
        except MailAppleScriptError as exc:
            return f"DISCARD_FAILED:{exc}"

    # -- tending Mail's compose windows ------------------------------------
    #
    # docs/research/compose-window-tending.md: what was measured, and the
    # rule. The decision is compose_tending.plan_tending; the AppleScript
    # that reads the windows and closes the ones decided on is here.

    # Handlers the tending scripts share. Top-level AppleScript, so each
    # script carries them after its own code.
    _TEND_HANDLERS = """
on tendIsBlank(t)
    repeat with ch in (characters of t)
        if (id of ch) is not in {32, 9, 10, 13, 160} then return false
    end repeat
    return true
end tendIsBlank

-- "empty" when the body holds nothing but whitespace text; "content" at
-- the first anything else: text, an attachment's button or image, or a
-- group too deep to look into.
on tendBodyState(el, depthLeft)
    tell application "System Events"
        set kids to UI elements of el
        if (count of kids) is 0 then return "empty"
        repeat with k in kids
            set r to role of k
            if r is "AXStaticText" then
                set v to value of k
                if v is missing value then set v to ""
                if not my tendIsBlank(v as text) then return "content"
            else if r is "AXGroup" and depthLeft > 0 then
                if my tendBodyState(k, depthLeft - 1) is "content" then return "content"
            else
                return "content"
            end if
        end repeat
    end tell
    return "empty"
end tendBodyState

-- A compose window's text-field values (To, Cc, any other header shown,
-- Subject), each read as text.
on tendFieldValues(w)
    tell application "System Events"
        set roles to role of UI elements of w
        set vals to value of UI elements of w
    end tell
    set fieldValues to {}
    repeat with j from 1 to (count of roles)
        if item j of roles is "AXTextField" then
            set v to item j of vals
            if v is missing value then set v to ""
            set end of fieldValues to (v as text)
        end if
    end repeat
    return fieldValues
end tendFieldValues

-- The body's state when every header field is blank; "unread" when one
-- is not (the window is not empty whatever the body holds), and
-- "unreadable" when the body cannot be found.
on tendBodyOf(w, fieldValues)
    repeat with v in fieldValues
        if not my tendIsBlank(contents of v) then return "unread"
    end repeat
    try
        tell application "System Events"
            set wa to first UI element of scroll area 1 of group 1 of group 1 of w whose role is "AXWebArea"
        end tell
        return my tendBodyState(wa, 6)
    on error
        return "unreadable"
    end try
end tendBodyOf
"""

    def _build_compose_inventory_script(self) -> str:
        """Full osascript source: every compose window System Events lists
        (a window whose toolbar has a Send button), each addressed as
        ``window i`` — through a nested ``every`` reference the body
        lookup failed on 7 windows of 25 (Observation 5) — with its
        header fields, its body's state, whether a sheet is on it and
        whether it is minimised; and Mail's own id and name for every
        window, in one event. Read-only. When Mail is not running it says
        so and starts nothing."""
        body = """
if not (application "Mail" is running) then
    set resultData to {|running|:false}
else
    set composeWindows to {}
    tell application "System Events"
        tell application process "Mail"
            set mailPid to unix id
            repeat with i from 1 to (count of windows)
                set w to window i
                set isCompose to false
                try
                    set isCompose to exists (first button of (first toolbar of w) whose description is "Send")
                end try
                if isCompose then
                    set fieldValues to my tendFieldValues(w)
                    set bodyState to my tendBodyOf(w, fieldValues)
                    set end of composeWindows to {|name|:(name of w), |fields|:fieldValues, |body|:bodyState, |sheet|:((count of sheets of w) > 0), |minimized|:(value of attribute "AXMinimized" of w)}
                end if
            end repeat
        end tell
    end tell
    tell application "Mail"
        set mailWindows to {}
        set windowProps to properties of every window
        repeat with p in windowProps
            set props to contents of p
            set end of mailWindows to {|id|:(id of props), |name|:(name of props)}
        end repeat
        set resultData to {|running|:true, |pid|:mailPid, |compose|:composeWindows, |mail_windows|:mailWindows}
    end tell
end if
"""
        return _wrap_as_json_script(body, timeout=self.timeout) + self._TEND_HANDLERS

    def _build_tend_close_script(self, action: TendAction) -> str:
        """Full osascript source: close the window a tending pass decided
        on, checking first, in the same script, that it is still that
        window — Mail's id for it still names it, and no other window
        has its name. Salvaged to Drafts through
        ``_as_salvage_compose_block``; discarded through
        ``_as_discard_compose_block`` only when it still reads empty, so
        nothing typed into it since the inventory is thrown away.
        Returns the block's outcome, or "NO_WINDOW", or "RENAMED:<name>"
        when the window now carries another name (someone edited its
        subject; it is left)."""
        record = action.record
        if record.window_id is None:
            raise ValueError("tending acts only on a window Mail gave an id")
        name_safe = escape_applescript_string(record.window_name)
        if action.action == "salvage":
            close = (
                self._as_salvage_compose_block("tendName")
                + "\nset tendOutcome to salvageOutcome"
            )
        else:
            close = f"""
tell application "System Events"
    tell application process "Mail"
        set sameNamed to count of (windows whose name is tendName)
        set stillEmpty to false
        if sameNamed is 1 then
            set fieldValues to my tendFieldValues(window tendName)
            set stillEmpty to (my tendBodyOf(window tendName, fieldValues) is "empty")
        end if
    end tell
end tell
if sameNamed is 0 then
    set tendOutcome to "NO_WINDOW"
else if sameNamed > 1 then
    set tendOutcome to "DISCARD_FAILED:" & sameNamed & " windows are named " & tendName & "; none was closed"
else if not stillEmpty then
    set tendOutcome to "DISCARD_FAILED:no longer empty; left open"
else
{self._as_discard_compose_block("tendName")}
    set tendOutcome to discardOutcome
end if
"""
        return f"""
set tendName to "{name_safe}"
set tendOutcome to ""
set idName to ""
tell application "Mail"
    try
        set idName to name of window id {int(record.window_id)}
    on error
        set tendOutcome to "NO_WINDOW"
    end try
end tell
if tendOutcome is "" and idName is not tendName then set tendOutcome to "RENAMED:" & idName
if tendOutcome is "" then
{close}
end if
return tendOutcome
""" + self._TEND_HANDLERS

    def tend_compose_windows(
        self, *, dry_run: bool = False, grace_s: float = TEND_GRACE_S
    ) -> TendReport:
        """One tending pass over Mail's compose windows.

        Reads every compose window (``_build_compose_inventory_script``)
        and the compose ledger, decides (``compose_tending.plan_tending``),
        and closes the windows decided on, one osascript each, recording
        each in the ledger as closed by tending: those the ledger says
        this connector opened and nothing closed, whose composition
        cannot still be running (``grace_s``), each the only window of
        its name. Every other window is left and counted. Records whose
        window is gone are ended as such, and records of windows closed
        more than ``RECORD_RETENTION_S`` ago are pruned.

        ``dry_run`` reads and decides, and closes and records nothing: its
        report's ``to_close`` is what a real pass would close.

        A close Mail does not answer stops the pass: the windows after it
        are reported as not attempted rather than each waiting out the
        timeout.
        """
        raw = self._run_applescript(self._build_compose_inventory_script())
        inventory = inventory_from_report(
            cast(dict[str, Any], parse_applescript_json(raw))
        )
        if inventory is None:
            return TendReport(dry_run=dry_run, mail_running=False)
        contents = self.compose_ledger.read_all()
        now = time.time()
        plan = plan_tending(inventory, contents.records, now=now, grace_s=grace_s)
        found = TendReport(
            dry_run=dry_run,
            mail_running=True,
            compose_windows=len(inventory.windows),
            left=plan.left,
            records_unidentified=plan.unidentified,
            records_unreadable=contents.unreadable,
        )
        if dry_run:
            return replace(
                found,
                to_close=tuple((a.record.window_name, a.action) for a in plan.actions),
                records_gone=len(plan.gone),
            )
        closed: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        not_attempted: list[str] = []
        for index, action in enumerate(plan.actions):
            name = action.record.window_name
            try:
                outcome = self._run_applescript(
                    self._build_tend_close_script(action)
                ).strip()
            except MailAppleScriptError as exc:
                failed.append((name, str(exc)))
                not_attempted.extend(
                    a.record.window_name for a in plan.actions[index + 1:]
                )
                break
            closing = _closing_of(outcome, by="tending", at=time.time())
            if closing is None:
                failed.append((name, outcome))
                continue
            self._end_tended_record(action.record.record_id, closing)
            closed.append((name, closing.how))
        gone = sum(
            self._end_tended_record(
                record.record_id, Closed(how="gone", by="tending", at=now)
            )
            for record in plan.gone
        )
        pruned = self.compose_ledger.prune(before=now - RECORD_RETENTION_S)
        return replace(
            found,
            closed=tuple(closed),
            failed=tuple(failed),
            not_attempted=tuple(not_attempted),
            records_gone=gone,
            records_pruned=pruned,
        )

    def _end_tended_record(self, record_id: str, state: Closed) -> bool:
        """End a record tending acted on; False, logged, when it had been
        ended meanwhile (its composition finished after all)."""
        try:
            self.compose_ledger.end(record_id, state)
        except MailComposeLedgerError as exc:
            logger.warning("compose ledger: %s", exc)
            return False
        return True

    @staticmethod
    def _build_readback_script(window_name: str) -> str:
        """Full osascript source: read the compose body's text content.

        Runs as its OWN osascript process — a fresh process gets a fresh
        AX snapshot; the pasting process reads a stale (empty) subtree
        after WebKit re-renders on paste (observed live 2026-07-22).
        Returns the concatenated static-text content, or
        "READBACK_NO_AREA" when the window/WebArea can't be resolved.
        """
        win_safe = escape_applescript_string(window_name)
        return f"""
tell application "System Events"
    tell application process "Mail"
        if not (exists window "{win_safe}") then return "READBACK_NO_AREA"
        set bodyArea to missing value
        repeat with g in groups of window "{win_safe}"
            try
                set sa to scroll area 1 of group 1 of g
                set waList to (UI elements of sa whose role is "AXWebArea")
                if (count of waList) > 0 then
                    set bodyArea to item 1 of waList
                    exit repeat
                end if
            end try
        end repeat
        if bodyArea is missing value then return "READBACK_NO_AREA"
        -- Pasted HTML nests as AXGroup > AXStaticText (typed text sits
        -- directly under the WebArea) — descend three levels.
        set seenText to ""
        try
            repeat with l1 in UI elements of bodyArea
                if role of l1 is "AXStaticText" then
                    set seenText to seenText & (value of l1) & " "
                else
                    try
                        repeat with l2 in UI elements of l1
                            if role of l2 is "AXStaticText" then
                                set seenText to seenText & (value of l2) & " "
                            else
                                try
                                    repeat with l3 in UI elements of l2
                                        if role of l3 is "AXStaticText" then set seenText to seenText & (value of l3) & " "
                                    end repeat
                                end try
                            end if
                        end repeat
                    end try
                end if
            end repeat
        end try
        return seenText
    end tell
end tell
"""

    def _paste_verified(
        self,
        *,
        window_name: str,
        body: str,
        placement: _PastePlacement,
        plain: bool,
    ) -> None:
        """Paste ``body`` into the named compose window at ``placement``
        and read it back: plain text when ``plain``, HTML otherwise.

        The read-back is its own osascript process, since a fresh process
        is the only reliable way to read the post-paste AX tree (within
        one, System Events serves a stale subtree after WebKit
        re-renders; observed live 2026-07-22). It must see the
        tag-stripped text and must NOT see the raw source (the 2026-07-21
        literal-``<p>`` regression). One undo-and-retry; on a second
        failure the window is salvaged to Drafts and
        ``MailAppleScriptError`` names what the read-back saw.
        """
        snippet, raw_marker = self._paste_probe_strings(body, plain=plain)
        fill = self._text_paste_fill(body, plain)

        for attempt in (1, 2):
            paste_result = self._run_applescript(
                self._build_paste_script(
                    window_name=window_name,
                    fill=fill,
                    placement=placement,
                    undo_first=(attempt == 2),
                )
            ).strip()
            if paste_result != "PASTED_UNVERIFIED":
                salvage = self._salvage_compose_to_draft(window_name)
                raise MailComposeWindowError(
                    f"paste: {paste_result!r} (compose window: {salvage})",
                    window_outcome=salvage,
                )
            seen = self._run_applescript(
                self._build_readback_script(window_name)
            ).strip()
            # Normalize whitespace: styled runs (<b>…) split AX static
            # texts, so the joined read-back carries doubled spaces.
            seen_norm = " ".join(seen.split())
            raw_norm = " ".join(raw_marker.split())
            arrived = (not snippet) or (snippet in seen_norm)
            degraded = bool(raw_norm) and raw_norm in seen_norm
            if arrived and not degraded:
                return
            if attempt == 2:
                salvage = self._salvage_compose_to_draft(window_name)
                raise MailComposeWindowError(
                    f"paste: 'PASTE_FAILED:read-back saw [{seen}] "
                    f"wanted [{snippet}] without raw [{raw_marker}]' "
                    f"(compose window: {salvage})",
                    window_outcome=salvage,
                )

    def _paste_attachments(
        self, window_name: str, attachment_paths: list[Path]
    ) -> None:
        """Paste the files at the end of the named compose window's body,
        then wait for each to show in the window's AX tree. On any
        failure the window is salvaged to Drafts and
        ``MailAppleScriptError`` raised; nothing is sent.

        Pasted, not attached through the dictionary: ``make new
        attachment`` on a window whose body had been pasted put Mail's
        URLShare wrapper and an empty ``<blockquote type="cite">`` back
        into the sent message (measured 2026-09-27,
        docs/research/icloud-draft-resync.md, Observation 10). A pasted
        file shows in the AX tree exactly as a dictionary-attached one
        does, so the same verification reads both.
        """
        outcome = self._run_applescript(
            self._build_paste_script(
                window_name=window_name,
                fill=self._files_paste_fill(attachment_paths),
                placement="end",
                undo_first=False,
            )
        ).strip()
        if outcome == "PASTED_UNVERIFIED":
            outcome = self._run_applescript(
                self._build_attachment_ax_verify_script(
                    window_name=window_name,
                    filenames=[Path(p).name for p in attachment_paths],
                )
            ).strip()
        if outcome != "ATTACHMENTS_VERIFIED":
            salvage = self._salvage_compose_to_draft(window_name)
            raise MailComposeWindowError(
                f"attachments: {outcome!r}; send NOT attempted "
                f"(compose window: {salvage})",
                window_outcome=salvage,
            )

    @staticmethod
    def _gate_named_recipients(
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
        (``_gate_compose_recipients``). No other seed derives a recipient,
        so what it names is everyone it sends to, and it must name
        someone."""
        if seed != "reply":
            assert_recipients_allowed_for_send(to, cc, bcc, seed=seed)
            return
        named = [addr for group in (to, cc, bcc) for addr in group or []]
        if named:
            assert_recipients_allowed_for_send(named, None, None)

    @staticmethod
    def _gate_compose_recipients(window: _ComposeWindow) -> None:
        """HARD POLICY GATE on the recipients an open compose window will
        send to, as read back from the outgoing-message model after every
        override: those the caller named and those Mail derived. No
        exceptions. ``MailOutboundDisallowedError`` on failure, before
        anything is pasted or sent; the composition then discards the
        window (``_compose``)."""
        assert_recipients_allowed_for_send(
            window.to or None, window.cc or None, window.bcc or None, seed="new"
        )

    def _send_compose_window(self, window_name: str) -> None:
        """Run the verified send on the named compose window; on any
        outcome but SENT the window is salvaged to Drafts and the outcome
        raised."""
        win_safe = escape_applescript_string(window_name)
        send_script = (
            f'set composeName to "{win_safe}"\n'
            + self._as_verified_send_block()
            + "\nreturn sendOutcome"
        )
        result = self._run_applescript(send_script).strip()
        if result == "SENT":
            return
        salvage = self._salvage_compose_to_draft(window_name)
        raise MailComposeWindowError(
            f"verified send: {result!r} (compose window: {salvage})",
            window_outcome=salvage,
        )

    def _send_html_email(
        self,
        *,
        to: list[str],
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,  # HTML string
        from_account: str | None,
        attachment_paths: list[Path] | None = None,
        reply_to: str | None = None,
        forward_of: str | None = None,
    ) -> dict[str, Any]:
        """Send an HTML email at once; no draft is saved first. A fresh
        message, a reply (``reply_to``) or a forward (``forward_of``),
        each through the one composition (``_compose``), the HTML pasted
        into the compose window, never set through ``content``. On a
        reply or forward it goes above what Mail wrote, which stays as
        Mail made it: the quoted original, or the forwarded message with
        its header block and the original's files.

        Args:
            to: Recipient addresses. Required on a fresh message and a
                forward. On a reply, an empty list keeps the recipients
                Mail derives from the message replied to; a list replaces
                them.
            cc: CC recipients; on a reply, ``None`` keeps Mail's.
            bcc: BCC recipients.
            subject: Required on a fresh message. On a reply or forward,
                ``""`` keeps Mail's "Re: …" or "Fwd: …".
            body: HTML string for the email body; on a reply or forward
                an empty one leaves Mail's part as it is.
            from_account: Account name, UUID or one of its addresses to
                send from; set as the message's sender. ``None`` leaves
                Mail's default.
            attachment_paths: Files pasted after everything else, on
                every seed.
            reply_to: Id of the message to reply to, Mail's own or an RFC
                5322 Message-ID.
            forward_of: Id of the message to forward, in the same forms.
                At most one of ``reply_to`` and ``forward_of``.

        Returns:
            What a send returns (``_sent_ending``): ``{"draft_id": "",
            "sent_message_id": <Mail id>, "sent_rfc_message_id":
            <Message-ID>}`` for the copy it filed in Sent, or, when that
            copy could not be identified, both ids ``""`` and a
            ``warnings`` list saying why. Either way the message was sent.

        Raises:
            ValueError: both ``reply_to`` and ``forward_of``; a fresh
                message without ``to`` or ``subject``; a body longer than
                a message body carries.
            MailMessageNotFoundError: no message has that id.
            MailAccountNotFoundError: ``from_account`` matches no account.
            FileNotFoundError: a listed attachment does not exist.
            MailOutboundDisallowedError: a recipient is not on the
                outbound allowlist, or a fresh message or forward names
                none.
            MailAppleScriptError: a mechanical read-back failed. Nothing
                was sent, except when the error says the message WAS sent
                and its Sent copy lacks a file it was sent with.
        """
        if reply_to is not None and forward_of is not None:
            raise ValueError("reply_to and forward_of are mutually exclusive")
        seed, seed_id = (
            ("reply", reply_to) if reply_to is not None
            else ("forward", forward_of) if forward_of is not None
            else ("new", None)
        )
        self._validate_compose_args(seed, seed_id, to, subject)
        _refuse_overlong_body(body)
        return self._compose(
            seed=seed,
            seed_id=self._maybe_resolve_rfc_seed_id(seed, seed_id),
            reply_all=False,
            to=to or None,
            cc=cc,
            bcc=bcc,
            subject=subject if seed == "new" else subject or None,
            body=body,
            plain=False,
            attachment_paths=attachment_paths,
            from_account=from_account,
            send_now=True,
        )

    def _sent_ending(
        self, window: _ComposeWindow, files: list[Path]
    ) -> dict[str, Any]:
        """What a send Mail accepted returns, by the copy it filed in
        Sent (``_find_sent_copy``): found, it must carry every file the
        composition pasted, by name, and its ids are returned; not
        identified, no id is returned, with a warning saying why
        (``_sent_result``)."""
        copy = self._find_sent_copy(window.subject, window.before_ids)
        if isinstance(copy, _SentCopy):
            self._check_sent_attachments(copy, [f.name for f in files])
        else:
            logger.warning("a send's copy in Sent was not identified: %s", copy.why)
        return _sent_result(copy, files_pasted=bool(files))

    @staticmethod
    def _check_sent_attachments(copy: _SentCopy, names: list[str]) -> None:
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

    def _find_sent_copy(
        self, subject: str, before_ids: list[int]
    ) -> _SentCopy | _SentCopyUnidentified:
        """The copy a send filed in Sent: the one message in Sent with
        ``subject`` whose id is not among ``before_ids``, taken before its
        window opened. Looked for at once, then every
        ``_SENT_APPEAR_INTERVAL_S`` for ``_SENT_APPEAR_POLLS`` more looks,
        each its own script. More than one new message with the subject
        (another send of it since the window opened) is not guessed
        between. Never raises: Mail accepted the message, and what fails
        here is only knowing which copy is its."""
        before = set(before_ids)
        try:
            for look in range(self._SENT_APPEAR_POLLS + 1):
                if look:
                    time.sleep(self._SENT_APPEAR_INTERVAL_S)
                new = [i for i in self._sent_ids_with_subject(subject) if i not in before]
                if len(new) == 1:
                    return self._read_sent_copy(new[0])
                if new:
                    return _SentCopyUnidentified(
                        f"{len(new)} messages with its subject reached Sent "
                        "while it was composed and sent, and which is its "
                        "copy cannot be told"
                    )
        except (MailError, ValueError) as exc:
            return _SentCopyUnidentified(f"looking for it in Sent failed: {exc}")
        waited = self._SENT_APPEAR_POLLS * self._SENT_APPEAR_INTERVAL_S
        return _SentCopyUnidentified(
            f"no new message with its subject appeared in Sent within {waited:g}s"
        )

    def _sent_ids_with_subject(self, subject: str) -> list[int]:
        """Mail's ids for every message in Sent (every account's) whose
        subject is ``subject``: ids only, so a look stays cheap however
        often the subject was used before."""
        subject_safe = escape_applescript_string(subject)
        raw = self._run_applescript(_wrap_as_json_script(f"""
tell application "Mail"
    set resultData to (id of every message of sent mailbox whose subject is "{subject_safe}")
end tell
""", timeout=self.timeout))
        return [int(i) for i in cast(list[Any], parse_applescript_json(raw))]

    def _read_sent_copy(self, mail_id: int) -> _SentCopy:
        """The message in Sent with Mail's id ``mail_id``: its RFC
        Message-ID (bare) and the names of its files."""
        raw = self._run_applescript(_wrap_as_json_script(f"""
tell application "Mail"
    set m to first message of sent mailbox whose id is "{mail_id}"
    set rfcId to message id of m
    if rfcId is missing value then set rfcId to ""
    set attNames to name of every mail attachment of m
    if attNames is missing value then set attNames to {{}}
    set resultData to {{|id|:(id of m as text), |message_id|:rfcId, |attachment_names|:attNames}}
end tell
""", timeout=self.timeout))
        record = cast(dict[str, Any], parse_applescript_json(raw))
        return _SentCopy(
            mail_id=str(record["id"]),
            rfc_message_id=str(record["message_id"]).strip().strip("<>"),
            attachment_names=tuple(str(n) for n in record["attachment_names"]),
        )

    @staticmethod
    def _build_attachment_ax_verify_script(
        *,
        window_name: str,
        filenames: list[str],
    ) -> str:
        """AppleScript: poll the compose window until every attachment
        shows up in the body WebArea as an element whose description
        carries the filename, or time out. Returns "ATTACHMENTS_VERIFIED"
        or "ATTACH_MISSING:<filename>".

        Mail shows a file one of two ways, whether it was attached
        through the dictionary or pasted: an image inline, as an AXImage
        described by its file name, and anything else as an AXButton
        described "name.ext, N KB" (both measured 2026-09-27,
        docs/research/icloud-draft-resync.md, Observation 10; the button
        form first live 2026-08-24). Either role is accepted.

        Walks the same groups → scroll area → AXWebArea path as the
        paste script and descends recursively from there. NEVER uses
        ``entire contents`` — it silently returns an empty list on
        compose windows more often than not (observed live 2026-08-24;
        one lucky success during exploration, empty on every retry).
        The WebArea is re-located on every poll: WebKit re-renders
        invalidate stale element references."""
        win_safe = escape_applescript_string(window_name)
        names_safe = ", ".join(
            f'"{escape_applescript_string(n)}"' for n in filenames
        )
        return f"""
on searchAttachment(el, fname, depthLeft)
    tell application "System Events"
        try
            set r to role of el
            if (r is "AXButton" or r is "AXImage") and ((description of el) as text) contains fname then return true
        end try
        if depthLeft > 0 then
            try
                repeat with c in UI elements of el
                    if my searchAttachment(c, fname, depthLeft - 1) then return true
                end repeat
            end try
        end if
        return false
    end tell
end searchAttachment

tell application "Mail" to activate
delay 0.3
set missingName to ""
repeat with fname in {{{names_safe}}}
    set foundIt to false
    repeat 10 times
        tell application "System Events"
            tell application process "Mail"
                if exists window "{win_safe}" then
                    set bodyArea to missing value
                    repeat with g in groups of window "{win_safe}"
                        try
                            set sa to scroll area 1 of group 1 of g
                            set waList to (UI elements of sa whose role is "AXWebArea")
                            if (count of waList) > 0 then
                                set bodyArea to item 1 of waList
                                exit repeat
                            end if
                        end try
                    end repeat
                    if bodyArea is not missing value then
                        set foundIt to my searchAttachment(bodyArea, fname as text, 6)
                    end if
                end if
            end tell
        end tell
        if foundIt then exit repeat
        delay 0.5
    end repeat
    if not foundIt then
        set missingName to fname as text
        exit repeat
    end if
end repeat
if missingName is "" then
    return "ATTACHMENTS_VERIFIED"
else
    return "ATTACH_MISSING:" & missingName
end if
"""

    def create_draft(
        self,
        *,
        seed: str = "new",
        seed_id: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        body: str = "",
        attachment_paths: list[Path] | None = None,
        reply_all: bool = False,
        from_account: str | None = None,
        send_now: bool = False,
    ) -> dict[str, Any]:
        """Create a draft (fresh, reply, or forward). Optionally send.

        Every seed, saved or sent, is composed the one way
        (``_compose``): in a visible compose window, the body pasted as
        plain text, so Mail comes to the front for a few seconds. A
        saved draft is that window closed with Save.

        Args:
            seed: ``"new"``, ``"reply"``, or ``"forward"``.
            seed_id: Identifier of the message to reply/forward. Accepts
                either Mail's internal numeric id OR an RFC 5322
                Message-ID (with or without angle brackets). The latter
                is what read tools (``search_messages`` / ``get_messages``)
                emit as ``id`` on the IMAP path (#148), so callers can
                forward those ids verbatim. Required when ``seed != "new"``.
            to/cc/bcc: Recipient lists. For reply/forward, ``None`` keeps
                Mail's auto-derived recipients; an empty list explicitly
                clears that group; a populated list replaces.
            subject: Subject. For ``seed="new"`` this is required by the
                caller. For reply/forward, ``None`` keeps Mail's
                auto-derived ``Re:``/``Fwd:`` prefix; non-None overrides.
            body: Body text. On a fresh message it is the whole body. For
                reply/forward, a non-empty body goes above what Mail
                wrote, which stays: the quoted original, or the forwarded
                message with its header block and every attachment Mail
                carried; an empty body leaves Mail's quote or forward
                exactly as Mail made it.
            attachment_paths: List of file paths, pasted into the body
                after everything else. Each must exist.
            reply_all: For ``seed="reply"`` only — use ``reply to all``.
            from_account: Mail.app account name or UUID; ``None`` uses
                Mail's default sender for the seed message.
            send_now: ``False`` saves as draft. ``True`` sends, and no
                draft is kept.

        Returns:
            A save: ``{"draft_id": <the draft's id>, "sent_message_id":
            ""}``. A send: what ``_sent_ending`` returns, the ids of the
            copy it filed in Sent, or ``""`` for both and a ``warnings``
            list when that copy could not be identified.

        Raises:
            ValueError: invalid seed, missing required fields, or a body
                longer than a message body carries.
            FileNotFoundError: a listed attachment does not exist.
            MailAccountNotFoundError: ``from_account`` doesn't match.
            MailMessageNotFoundError: ``seed_id`` not found in any mailbox.
            MailOutboundDisallowedError: on a send, a recipient is not on
                the outbound allowlist.
            MailDraftNotSettledError: the saved draft did not appear in
                Drafts, so there is no id to return.
            MailAppleScriptError: AppleScript failure, or a mechanical
                read-back of the compose window failed; on a send, the
                message WAS sent when the error says so (its Sent copy
                lacks a file it was sent with).
        """
        self._validate_compose_args(seed, seed_id, to, subject)
        _refuse_overlong_body(body)

        # HARD POLICY GATE — the actual-send enforcement perimeter for the
        # outbound recipient allowlist. Any code path that reaches this
        # method with send_now=True (create_draft tool, update_draft
        # delete-and-recreate, future direct callers) is checked here. If
        # any recipient is off-list, MailOutboundDisallowedError is raised
        # BEFORE any AppleScript is built or executed. See
        # outbound_allowlist.py for the policy. DO NOT add a bypass
        # without explicit human authorization.
        if send_now:
            assert_recipients_allowed_for_send(
                to, cc, bcc, seed=seed, reply_all=reply_all
            )

        # If the caller handed us an RFC 5322 Message-ID (the form read
        # tools emit on the IMAP path per #148), resolve to Mail's
        # internal id before the `whose id is` lookup. (#205)
        seed_id = self._maybe_resolve_rfc_seed_id(seed, seed_id)
        return self._compose(
            seed=seed,
            seed_id=seed_id,
            reply_all=reply_all,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            plain=True,
            attachment_paths=attachment_paths,
            from_account=from_account,
            send_now=send_now,
        )

    def _compose(
        self,
        *,
        seed: str,
        seed_id: str | None,
        reply_all: bool,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        body: str,
        plain: bool,
        attachment_paths: list[Path] | None,
        from_account: str | None,
        send_now: bool,
    ) -> dict[str, Any]:
        """The one composition behind every draft the connector saves
        and every message it sends: ``create_draft`` for every seed and
        both outcomes (``plain``), and ``_send_html_email`` for every
        seed (HTML).

          1. Open a visible compose window (``_open_compose``): a fresh
             message, or Mail's own reply or forward of the seed. Its
             headers go through the dictionary: a reply's or forward's
             subject override, each recipient group, and the sender, set
             last. The window is found by comparing Mail's window names
             before and after, never guessed from the subject, and the
             subject and recipients it holds are read back.
          2. On a send, those recipients pass the outbound allowlist, or
             the window is discarded and nothing is pasted. They include
             any Mail derived for a reply, which this is the first point
             that can see.
          3. Paste the body and read it back (``_fill_compose``), then the
             files, each seen in the window's AX tree.
          4. Send: the verified send, then find the copy it filed in Sent
             by identity, which must carry every file by name, and return
             its ids (``_sent_ending``). Or save: close the window with
             Save and find the draft it became. Either ending is the entry
             with the window's subject whose id the opening script did
             not see (``_ENDING_MAILBOX``).

        Why every message goes this way, all measured on 2026-09-26 and
        -27. Opened without a window, Mail's reply or forward exposes no
        content, and every edit of it through the dictionary replaced
        what Mail wrote. A body set through ``content`` arrives inside
        Mail's ``<blockquote type="cite">``, so a human sending such a
        saved draft from Mail.app sent it quoted; a body pasted above the
        fresh seed leaves an empty one below it; files attached through
        the dictionary after the paste bring it back
        (docs/research/icloud-draft-resync.md, Observation 10). A draft
        saved through the dictionary with a named sender was re-saved by
        Mail under a new id and Message-ID 0.2–28 s after the save, and
        every dictionary save left a hidden outgoing message behind; a
        window closed with Save kept its id for 45 s in 6 runs of 6 and
        left none (docs/research/draft-resave-spike.md).

        Checked before anything is composed: on a send, every recipient
        the caller named passes the outbound allowlist, and a seed that
        derives none names someone (``_gate_named_recipients``,
        ``MailOutboundDisallowedError``), here as well as at every
        caller, since the connector is the hard block on a path by which
        mail leaves; every attachment exists (``FileNotFoundError``); and
        ``from_account`` names an account (``MailAccountNotFoundError``).
        The body's length was refused where it entered the connector
        (``_refuse_overlong_body``). A later failure salvages the window
        to Drafts and raises ``MailAppleScriptError``, which says so when
        the message was in fact sent.

        The window is recorded in the compose ledger when it opens
        (``_open_compose``), and how it ended when the composition ends:
        sent, saved as a draft, salvaged, discarded, gone, or left open
        with the failure (``_window_lifecycle``). A window left open is
        tending's to close (``tend_compose_windows``).
        """
        if send_now:
            self._gate_named_recipients(seed, to, cc, bcc)
        files = _existing_files(attachment_paths)
        sender = (
            self._resolve_account_to_sender(from_account)
            if from_account is not None
            else None
        )
        window = self._open_compose(
            seed=seed,
            seed_id=seed_id,
            reply_all=reply_all,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            sender=sender,
            operation="send" if send_now else "save",
        )
        with self._window_lifecycle(window) as end_window:
            if send_now:
                try:
                    self._gate_compose_recipients(window)
                except MailOutboundDisallowedError as exc:
                    end_window(
                        _composition_closing(
                            self._discard_compose_window(window.name),
                            failure=f"recipients refused: {exc}",
                        )
                    )
                    raise
            self._fill_compose(
                window.name, seed=seed, body=body, plain=plain, files=files
            )
            if send_now:
                self._send_compose_window(window.name)
                end_window(Closed(how="sent", by="composition", at=time.time()))
                return self._sent_ending(window, files)
            try:
                draft_id = self._save_compose_window_as_draft(
                    window.name, window.subject, window.before_ids
                )
            except MailDraftNotSettledError:
                # Closed with Save; the draft it became is not yet listed.
                end_window(Closed(how="salvaged", by="composition", at=time.time()))
                raise
            end_window(
                Closed(
                    how="saved", by="composition", at=time.time(), draft_id=draft_id
                )
            )
            return {"draft_id": draft_id, "sent_message_id": ""}

    @contextmanager
    def _window_lifecycle(
        self, window: _ComposeWindow
    ) -> Iterator[Callable[[LeftOpen | Closed], None]]:
        """Record how the composition's window ends, exactly once, through
        the function it yields. What the body does not record itself is
        recorded as it leaves: from the outcome a
        ``MailComposeWindowError`` carries, or, for anything else raised,
        as left open with that error."""
        ended = False

        def _end(state: LeftOpen | Closed) -> None:
            nonlocal ended
            self._record_window_end(window, state)
            ended = True

        try:
            yield _end
        except MailComposeWindowError as exc:
            if not ended:
                _end(_composition_closing(exc.window_outcome, failure=str(exc)))
            raise
        except BaseException as exc:
            if not ended:
                _end(LeftOpen(failure=f"{type(exc).__name__}: {exc}", at=time.time()))
            raise

    def _record_window_end(
        self, window: _ComposeWindow, state: LeftOpen | Closed
    ) -> None:
        """Write how a composition's window ended. A window left open
        wakes tending (``on_window_left_open``). The mail has already been
        sent, saved or refused when this runs, so a ledger that cannot be
        written is reported in the log, not raised."""
        if window.record_id is not None:
            try:
                self.compose_ledger.end(window.record_id, state)
            except (OSError, MailComposeLedgerError) as exc:
                logger.error(
                    "compose ledger: could not record how window %r ended "
                    "(%s): %s", window.name, state, exc,
                )
        if isinstance(state, LeftOpen) and self.on_window_left_open is not None:
            self.on_window_left_open()

    def _fill_compose(
        self,
        window: str,
        *,
        seed: str,
        body: str,
        plain: bool,
        files: list[Path],
    ) -> None:
        """Paste the caller's part into the open compose window.

        The body is read back from a fresh process after the paste. A
        fresh message's replaces everything the window holds (select all,
        delete, paste), the one-space seed included, so an empty body is
        an empty paste and leaves the body empty. A reply's or forward's
        goes above what Mail wrote, and only when there is one: with no
        body, Mail's quote or forward is left exactly as Mail made it.

        The files go in last, at the end of the body, pasted as file URLs
        (``_paste_attachments``), on every seed. Measured 2026-09-27 on
        a reply and a forward whose body was not touched, each sent with
        one file to the loopback and read back from its Sent copy (no
        delivered copy arrived within 120 s;
        docs/research/icloud-draft-resync.md, Observation 11): attached
        through the dictionary (``make new attachment``), the file went
        but Mail's part did not — the original went out unquoted, with
        no "On … wrote:" or "Begin forwarded message:", above an empty
        cite blockquote, and the forward lost both of the original's
        files. Pasted at the end, the quote and the forwarded message
        stayed as Mail made them, the forward kept both files, and the
        caller's file sat after Mail's part, outside its blockquote.
        """
        if seed == "new":
            self._paste_verified(
                window_name=window, body=body, placement="replace", plain=plain,
            )
        elif body:
            self._paste_verified(
                window_name=window, body=body, placement="above", plain=plain,
            )
        if files:
            self._paste_attachments(window, files)

    def _draft_not_settled(self) -> MailDraftNotSettledError:
        return MailDraftNotSettledError(
            "Mail accepted the save but the new draft did not appear "
            "in Drafts within "
            f"{self._DRAFT_APPEAR_POLLS * self._DRAFT_APPEAR_INTERVAL_S:g}s, "
            "so there is no id to return; look for it in Mail.app "
            "before saving again. Nothing was sent."
        )

    def _run_seeded_script(self, script: str, seed_id: str | None) -> str:
        """Run a create_draft script, mapping its SEED_NOT_FOUND to
        ``MailMessageNotFoundError``."""
        try:
            return self._run_applescript(script).strip()
        except MailAppleScriptError as e:
            if "SEED_NOT_FOUND" in str(e):
                raise MailMessageNotFoundError(
                    f"no message with id {seed_id!r}"
                ) from e
            raise

    @staticmethod
    def _recipient_override_block(kind: str, addrs: list[str] | None) -> str:
        """AppleScript that clears and re-populates one recipient group of
        ``theMessage``: None keeps what Mail derived (``""``), ``[]``
        clears it, a list replaces it."""
        if addrs is None:
            return ""
        list_str = ", ".join(
            f'"{escape_applescript_string(sanitize_input(a))}"' for a in addrs
        )
        return f"""
                delete (every {kind} recipient of theMessage)
                repeat with addr in {{{list_str}}}
                    make new {kind} recipient at end of {kind} recipients of theMessage with properties {{address:addr}}
                end repeat
            """

    def _draft_headers_block(
        self,
        *,
        seed: str,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        sender: str | None,
    ) -> str:
        """AppleScript for every header the composition sets on
        ``theMessage`` through the dictionary: the subject override
        (reply and forward; a fresh message gets its subject at
        creation), each recipient group, and ``sender`` (already
        resolved), set LAST — set before the content and recipients, the
        first saved copy of a draft carried no recipients
        (docs/research/icloud-draft-resync.md, Observation 6)."""
        parts = []
        if seed != "new" and subject is not None:
            subject_safe = escape_applescript_string(sanitize_input(subject))
            parts.append(f'set subject of theMessage to "{subject_safe}"')
        for kind, addrs in (("to", to), ("cc", cc), ("bcc", bcc)):
            parts.append(self._recipient_override_block(kind, addrs))
        # The SECURITY_CHECKLIST two-step idiom (sanitize_input then
        # escape_applescript_string) applies even though the resolver
        # pulls from Mail.app's own account list — the convention exists
        # so we don't have to risk-assess each site individually, and the
        # Display-Name <email> form from #158 broadened what characters
        # can appear here. (#173)
        if sender is not None:
            sender_safe = escape_applescript_string(sanitize_input(sender))
            parts.append(f'set sender of theMessage to "{sender_safe}"')
        return "\n".join(part for part in parts if part)

    def _build_open_compose_script(
        self,
        *,
        seed: str,
        seed_id: str | None,
        reply_all: bool,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        sender: str | None,
        operation: WindowOperation,
    ) -> str:
        """AppleScript body for ``_wrap_as_json_script``: open the compose
        window (``_build_creation_block``), name it by window-set diff,
        apply the headers (``_draft_headers_block``), and set
        ``resultData`` to what the window holds: its name, subject and
        recipients, and the ids of the mailbox the ``operation``'s ending
        is looked up in, taken before it opened (``_ENDING_MAILBOX``), so
        a save finds the draft it made and a send the copy it filed.

        Mail retitles a compose window the moment its subject is set
        (measured 2026-09-27, docs/research/icloud-draft-resync.md,
        Observation 12), and every later step addresses the window by
        name. So a reply or forward whose subject is overridden is named
        again, by the same diff, once the headers are on it; named only
        before, it was addressed by a name no window had any more.

        The report also carries what identifies the window to the compose
        ledger: Mail's own id for it (the one window of its name whose id
        was not there before it opened; 0 when that is not exactly one)
        and Mail's process id. Once the window exists, a failure does not
        leave the script: it is reported as ``failure``, with the window,
        so the window is recorded as left open rather than lost from
        view. That covers COMPOSE_WINDOW_NOT_UNIQUE, whose window stays
        open by design, and any header or read-back that fails. Before a
        window is found (SEED_NOT_FOUND, NO_COMPOSE_WINDOW) the script
        still raises."""
        seed_id_safe = (
            escape_applescript_string(sanitize_input(seed_id))
            if seed_id is not None
            else None
        )
        subject_safe = (
            escape_applescript_string(sanitize_input(subject))
            if subject is not None
            else None
        )
        creation_block = self._build_creation_block(
            seed, seed_id_safe, reply_all, subject_safe,
        )
        headers_block = self._draft_headers_block(
            seed=seed, to=to, cc=cc, bcc=bcc, subject=subject, sender=sender,
        )
        snapshot = (
            f"set beforeIds to (id of every message of {_ENDING_MAILBOX[operation]})"
        )
        renamed = (
            self._as_new_compose_window_block()
            if seed != "new" and subject is not None
            else ""
        )
        return f"""
tell application "System Events"
    tell application process "Mail"
        set beforeNames to name of windows
        set mailPid to unix id
    end tell
end tell
tell application "Mail"
    set beforeWindowIds to id of every window
    activate
    {snapshot}
    {creation_block}
end tell
set newName to ""
set failure to ""
try
{self._as_new_compose_window_block()}
on error errMsg number errNum
    if newName is "" then error errMsg number errNum
    set failure to errMsg
end try
-- Mail's own id for the window: the one of its name that is new.
set newWindowId to 0
repeat 10 times
    tell application "Mail"
        set candidateIds to id of every window whose name is newName
    end tell
    set newIds to {{}}
    repeat with cid in candidateIds
        if (contents of cid) is not in beforeWindowIds then set end of newIds to (contents of cid)
    end repeat
    if (count of newIds) is 1 then set newWindowId to item 1 of newIds
    if (count of newIds) is not 0 then exit repeat
    delay 0.2
end repeat
if failure is "" then
    try
        tell application "Mail"
            {headers_block}
        end tell
{renamed}
        tell application "Mail"
            set toAddrs to address of to recipients of theMessage
            if toAddrs is missing value then set toAddrs to {{}}
            set ccAddrs to address of cc recipients of theMessage
            if ccAddrs is missing value then set ccAddrs to {{}}
            set bccAddrs to address of bcc recipients of theMessage
            if bccAddrs is missing value then set bccAddrs to {{}}
            set resultData to {{|window|:newName, |window_id|:newWindowId, |mail_pid|:mailPid, |failure|:"", |subject|:(subject of theMessage as text), |to|:toAddrs, |cc|:ccAddrs, |bcc|:bccAddrs, |before_ids|:beforeIds}}
        end tell
    on error errMsg
        set failure to errMsg
    end try
end if
if failure is not "" then
    if newName is "" and newWindowId is not 0 then
        try
            tell application "Mail" to set newName to name of window id newWindowId
        end try
    end if
    set resultData to {{|window|:newName, |window_id|:newWindowId, |mail_pid|:mailPid, |failure|:failure, |subject|:"", |to|:{{}}, |cc|:{{}}, |bcc|:{{}}, |before_ids|:{{}}}}
end if
"""

    def _open_compose(
        self,
        *,
        seed: str,
        seed_id: str | None,
        reply_all: bool,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        sender: str | None,
        operation: WindowOperation,
    ) -> _ComposeWindow:
        """Open the compose window ``_build_open_compose_script``
        describes, record it in the compose ledger, and return what the
        script reported; a seed that is gone is
        ``MailMessageNotFoundError``. A failure once the window existed
        records it as left open and raises ``MailAppleScriptError``."""
        script = self._build_open_compose_script(
            seed=seed, seed_id=seed_id, reply_all=reply_all, to=to, cc=cc,
            bcc=bcc, subject=subject, sender=sender, operation=operation,
        )
        raw = self._run_seeded_script(
            _wrap_as_json_script(script, timeout=self.timeout), seed_id
        )
        report = cast(dict[str, Any], parse_applescript_json(raw))
        window = _compose_window_from_report(report)
        window = replace(
            window,
            record_id=self._record_window_open(
                window, operation=operation, seed=cast(Seed, seed)
            ),
        )
        failure = str(report.get("failure") or "")
        if failure:
            self._record_window_end(window, LeftOpen(failure=failure, at=time.time()))
            raise MailAppleScriptError(f"{failure} (compose window: left open)")
        return window

    def _record_window_open(
        self, window: _ComposeWindow, *, operation: WindowOperation, seed: Seed
    ) -> str | None:
        """Record a window the connector just opened; its record id, or
        None when the ledger could not be written (logged: the window is
        open either way, and the composition goes on)."""
        try:
            return self.compose_ledger.open(
                window_name=window.name,
                window_id=window.window_id,
                mail_pid=window.mail_pid,
                operation=operation,
                seed=seed,
            ).record_id
        except OSError as exc:
            logger.error(
                "compose ledger: could not record window %r: %s", window.name, exc
            )
            return None

    def _save_compose_window_as_draft(
        self, window: str, subject: str, before_ids: list[int]
    ) -> str:
        """Close the compose window with Save, then find the draft it
        became: the Drafts entry with ``subject`` that was not among
        ``before_ids``, polled for and then given the settle time a new
        draft needs before Mail acts on it."""
        outcome = self._salvage_compose_to_draft(window)
        if outcome != "SALVAGED":
            raise MailComposeWindowError(
                f"draft save: {outcome}", window_outcome=outcome
            )
        ids = ", ".join(str(i) for i in before_ids)
        subject_safe = escape_applescript_string(subject)
        result = self._run_applescript(f"""
tell application "Mail"
    set beforeIds to {{{ids}}}
    set newDraftId to ""
    repeat with attempt from 1 to {self._DRAFT_APPEAR_POLLS}
        delay {self._DRAFT_APPEAR_INTERVAL_S}
        repeat with m in (messages of drafts mailbox whose subject is "{subject_safe}")
            set candId to id of m
            if candId is not in beforeIds then set newDraftId to (candId as text)
        end repeat
        if newDraftId is not "" then exit repeat
    end repeat
    if newDraftId is not "" then delay {self._DRAFT_SETTLE_S}
    return newDraftId
end tell
""").strip()
        if not result:
            raise self._draft_not_settled()
        return result

    def extract_draft_attachments(
        self,
        draft_id: str,
        attachment_names: list[str],
        dest_dir: Path,
    ) -> list[Path]:
        """Save each attachment of a draft to disk.

        Used by ``update_draft`` to preserve attachments through the
        delete-and-recreate cycle. Mail.app doesn't expose attachment
        file paths on saved drafts (`file of att` returns an opaque
        reference), so we extract via the ``save`` AppleScript command.

        Each attachment lands in its own ``<dest_dir>/<i>/`` subdirectory
        so filename collisions between attachments don't lose data.
        Original filenames are preserved.

        Args:
            draft_id: Draft to read attachments from.
            attachment_names: Filenames (index-aligned with the draft's
                ``mail attachments`` collection). Caller typically sources
                these from ``get_draft_state(draft_id)["attachment_names"]``.
            dest_dir: Existing directory under which subdirectories are
                created. Caller owns the lifecycle (e.g. tempdir cleanup).

        Returns:
            Paths of the extracted files, in the same order as
            ``attachment_names``. Length equals number of attachments
            actually written; missing entries indicate per-attachment
            extraction failures.

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
            FileNotFoundError: ``dest_dir`` does not exist.
        """
        _validate_draft_id(draft_id)
        dest_dir = Path(dest_dir)
        if not dest_dir.is_dir():
            raise FileNotFoundError(f"dest_dir does not exist: {dest_dir}")
        if not attachment_names:
            return []

        # Pre-create per-attachment subdirectories on the Python side so
        # the AppleScript only has to do `save att in (POSIX file <path>)`.
        target_paths: list[Path] = []
        for i, name in enumerate(attachment_names):
            subdir = dest_dir / str(i)
            subdir.mkdir(parents=True, exist_ok=True)
            target_paths.append(subdir / name)

        targets_safe = ", ".join(
            f'"{escape_applescript_string(str(p.resolve()))}"'
            for p in target_paths
        )

        script = f"""
        tell application "Mail"
            set targetId to "{draft_id}"
            set foundDraft to missing value
            repeat with d in messages of drafts mailbox
                set candId to ""
                try
                    set candId to (id of d as text)
                end try
                if candId is targetId then
                    set foundDraft to d
                    exit repeat
                end if
            end repeat
            if foundDraft is missing value then return "ERR_NOT_FOUND"

            set targetPaths to {{{targets_safe}}}
            set atts to mail attachments of foundDraft
            set saved to 0
            set total to count of atts
            if total > (count of targetPaths) then set total to (count of targetPaths)
            repeat with i from 1 to total
                set a to item i of atts
                set tp to item i of targetPaths
                try
                    save a in (POSIX file tp)
                    set saved to saved + 1
                end try
            end repeat
            return saved as text
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "ERR_NOT_FOUND":
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

        # Return only the paths that actually got files written.
        return [p for p in target_paths if p.is_file()]
