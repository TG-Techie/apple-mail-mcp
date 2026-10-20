"""AppleScript fragments the connector's scripts share: the JSON wrapper, the
bulk repeat, the attachment walk, and the recipient and source reads.
"""

from typing import Any

from .links import (
    MAX_SOURCE_BYTES,
    links_from_source,
    source_too_large_warning,
    source_unreadable_warning,
)
from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    format_recipients,
    sanitize_input,
)


def wrap_as_json_script(body: str, *, timeout: int) -> str:
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


def bulk_repeat_block(
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
        raise ValueError(f"account and source_mailbox must be provided together; missing {missing}")

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
# Each is read under its OWN try block -- see attachment_walk_block.
_ATTACHMENT_PROPS: tuple[tuple[str, str, str, str], ...] = (
    ("name of att", "name", "attName", '""'),
    ("MIME type of att", "mime_type", "attMime", '""'),
    ("file size of att", "size", "attSize", "0"),
    ("downloaded of att", "downloaded", "attDown", "false"),
)


def attachment_walk_block(
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
            f'(id of {message_var} as text) & ": " & errMsg & '
            f'" (error " & errNum & ")")'
        )
        lines.append(f"{pad}        end try")
    record_fields = ", ".join(f"|{key}|:{var}" for _e, key, var, _d in _ATTACHMENT_PROPS)
    lines.append(f"{pad}        set attRecord to {{{record_fields}}}")
    lines.append(f"{pad}        set end of {list_var} to attRecord")
    lines.append(f"{pad}    end repeat")
    lines.append(f"{pad}on error errMsg number errNum")
    lines.append(f"{pad}    set {list_var} to {{}}")
    lines.append(
        f"{pad}    set end of {warnings_var} to "
        f'("attachment enumeration failed for message " & '
        f'(id of {message_var} as text) & ": " & errMsg & '
        f'" (error " & errNum & ")")'
    )
    lines.append(f"{pad}end try")
    return "\n".join(lines)


# A message's recipient elements, in emitted order:
#   (Mail element, JSON key, AppleScript list variable)
RECIPIENT_KINDS: tuple[tuple[str, str, str], ...] = (
    ("to recipients", "to", "toList"),
    ("cc recipients", "cc", "ccList"),
    ("bcc recipients", "bcc", "bccList"),
)

# The fields a message record takes from recipient_read_block's lists.
RECIPIENT_FIELDS = ", ".join(f"|{key}|:{var}" for _element, key, var in RECIPIENT_KINDS)


def recipient_read_block(
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
    with ``format_address`` (``render_recipients``) exactly as the
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
        takes the lists with ``RECIPIENT_FIELDS``.
    """
    pad = " " * indent
    id_text = id_expr or f"(id of {message_var} as text)"
    lines: list[str] = []
    for element, key, var in RECIPIENT_KINDS:
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


def render_recipients(record: dict[str, Any]) -> None:
    """Replace the recipient records a script emitted with the row's
    strings, in place: each ``{name, address}`` through
    ``format_address``, the renderer the IMAP rows use.

    A record without one of the keys is left without it. Every script
    that reads recipients emits all three, so there is nothing to
    render, and an empty list here would say the message has no
    recipients when nothing was read.
    """
    for _element, key, _var in RECIPIENT_KINDS:
        if key in record:
            record[key] = format_recipients(
                (str(r.get("name") or ""), str(r.get("address") or "")) for r in record[key] or []
            )


# The record fields ``source_read_block`` fills, which ``take_links``
# turns into the row's ``links``.
SOURCE_FIELDS = "|source|:msgSource, |source_size|:msgSourceSize, |source_error|:msgSourceError"


def source_read_block(*, message_var: str, indent: int) -> str:
    """Emit the AppleScript that reads ``<message_var>``'s raw source into
    ``msgSource``, for ``include_links``.

    Two events: ``message size``, then ``source`` only when the size is
    within ``MAX_SOURCE_BYTES``, so an oversized message is never
    carried through JSON. Both under their own ``try``: the lookup loops
    that call this treat any error as "not in this mailbox", so an
    unreadable source would otherwise become "message not found". The
    error goes to ``msgSourceError`` for ``take_links`` to report.
    """
    pad = " " * indent
    lines = [
        'set msgSource to ""',
        "set msgSourceSize to 0",
        'set msgSourceError to ""',
        "try",
        f"    set msgSourceSize to message size of {message_var}",
        "    if msgSourceSize is missing value then set msgSourceSize to 0",
        f"    if msgSourceSize ≤ {MAX_SOURCE_BYTES} then",
        f"        set msgSource to source of {message_var}",
        '        if msgSource is missing value then set msgSource to ""',
        "    end if",
        "on error errMsg number errNum",
        '    set msgSource to ""',
        '    set msgSourceError to (errMsg & " (error " & errNum & ")")',
        "end try",
    ]
    return "\n".join(pad + line for line in lines)


def take_links(record: dict[str, Any]) -> list[str]:
    """Replace the source fields a script emitted (``SOURCE_FIELDS``)
    with the row's ``links``, in place, and return any warnings: the
    source could not be read, was over ``MAX_SOURCE_BYTES``, or had
    more links than are returned."""
    source = str(record.pop("source", "") or "")
    size = int(record.pop("source_size", 0) or 0)
    error = str(record.pop("source_error", "") or "")
    message_id = str(record.get("id", ""))
    if error:
        record["links"] = []
        return [source_unreadable_warning(message_id, error)]
    if size > MAX_SOURCE_BYTES:
        record["links"] = []
        return [source_too_large_warning(message_id, size)]
    links, warnings = links_from_source(source, message_id=message_id)
    record["links"] = links
    return warnings


def build_flag_actions(
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
