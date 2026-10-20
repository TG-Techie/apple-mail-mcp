"""The AppleScript search_messages runs, built from its filters."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta

from .script_blocks import (
    RECIPIENT_FIELDS,
    RECIPIENT_KINDS,
    attachment_walk_block,
    recipient_read_block,
)
from .utils import (
    ISO_DATE_RE,
    applescript_iso_date_statements,
    escape_applescript_string,
    sanitize_input,
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


def _condition(criterion: _SearchCriterion, value: Callable[[str], str], message: str) -> str:
    """``criterion.excludes`` rendered with ``value(p)`` as each
    property's value, or with ``message`` when the test asks the
    message itself."""
    if criterion.prop is None:
        return criterion.excludes(message)
    return criterion.excludes(value(criterion.prop), *(value(p) for p in criterion.also))


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


def search_criteria(
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
        criteria.append(_SearchCriterion("subject", _does_not_contain(subject_contains)))
    if read_status is not None:
        criteria.append(_SearchCriterion("read status", _is_not(read_status)))
    if is_flagged is not None:
        criteria.append(_SearchCriterion("flagged status", _is_not(is_flagged)))

    if date_from is not None:
        if not ISO_DATE_RE.match(date_from):
            raise ValueError(f"date_from must be ISO 8601 YYYY-MM-DD, got: {date_from!r}")
        date_setup.append(applescript_iso_date_statements("dateFromCutoff", date_from))
        criteria.append(_SearchCriterion("date received", lambda v: f"{v} < dateFromCutoff"))

    if date_to is not None:
        if not ISO_DATE_RE.match(date_to):
            raise ValueError(f"date_to must be ISO 8601 YYYY-MM-DD, got: {date_to!r}")
        # Upper bound is exclusive of the day AFTER date_to, so the full
        # day of date_to is included.
        next_day = (_date.fromisoformat(date_to) + _timedelta(days=1)).isoformat()
        date_setup.append(applescript_iso_date_statements("dateToCutoff", next_day))
        criteria.append(_SearchCriterion("date received", lambda v: f"{v} >= dateToCutoff"))

    if has_attachment is not None:
        compare = "= 0" if has_attachment else "> 0"
        criteria.append(
            _SearchCriterion(None, lambda m: f"(count of mail attachments of {m}) {compare}")
        )

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
        criteria.append(
            _SearchCriterion(
                _CONTENT,
                lambda content, subject, sender: (
                    f'not ({content} contains "{text_safe}" or '
                    f'{subject} contains "{text_safe}" or '
                    f'{sender} contains "{text_safe}")'
                ),
                also=("subject", "sender"),
            )
        )
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
SEARCH_RUN_GAP = 8

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
SEARCH_CONTENT_RUN_GAP = 0

# The kept positions wait for their bodies until there are as many as
# matches still wanted (so no body is read past the limit), or this
# many (so no one read holds more bodies than this, whatever the
# limit), or the scan reaches the mailbox's end. Beside 50 bodies, the
# one more event a batch costs (about 11 ms) is small.
SEARCH_CONTENT_BATCH = 50

SEARCH_OUT_OF_LINE_WARNING = (
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
    fields.append(RECIPIENT_FIELDS)
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
        row += _block_lines(
            attachment_walk_block(message_var="msg", warnings_var="warnList", indent=0)
        )
    row += _block_lines(recipient_read_block(message_var="msg", warnings_var="warnList", indent=0))
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
        f"set msgRef to a reference to («class mssg» id (item {index} of allIdsRef) of mailboxRef)"
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
            target=f"{stem}All",
            expr=f"{prop} of messages of mailboxRef",
            what=prop,
            flag=f"{stem}InBulk",
            where="",
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
            for c in criteria
            if c.prop == prop
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
    ``SEARCH_CONTENT_RUN_GAP``; read the run's content in one event,
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
        f"    if (item (candLast + 1) of candsRef) - runEnd > {SEARCH_CONTENT_RUN_GAP + 1} then exit repeat",
        "    set candLast to candLast + 1",
        "    set runEnd to item candLast of candsRef",
        "end repeat",
        *_bulk_read_lines(
            target="contentRun",
            expr="content of messages runStart thru runEnd of mailboxRef",
            what="content",
            flag="contentRunRead",
            where=where,
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


def _bulk_match_lines(criteria: list[_SearchCriterion], *, limit: str) -> list[str]:
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
    scan = (
        [
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
        ]
        if checks
        else kept
    )
    flush = (
        [
            f"if candCount > 0 and (candCount >= {limit} - matchCount or candCount >= {SEARCH_CONTENT_BATCH} or i = total) then",
            *_indented(_content_test_lines(content), 4),
            "    if not aligned then exit repeat",
            "end if",
        ]
        if content
        else []
    )
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
        for element, key, _var in RECIPIENT_KINDS
    ]
    for stem, expr, what in reads:
        read = _bulk_read_lines(
            target=f"{stem}Run",
            expr=expr,
            what=what,
            flag=f"{stem}RunRead",
            where=where,
        ) + [
            f"set {stem}RunRef to a reference to {stem}Run",
            f"if {stem}RunRead and (count of {stem}Run) is not (count of runIds) then set aligned to false",
        ]
        if what in filter_props:
            read = [
                f"set {stem}RunRead to false",
                f"if not {stem}InBulk then",
                *_indented(read, 4),
                "end if",
            ]
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
        *_block_lines(
            recipient_read_block(
                message_var="msgRef",
                warnings_var="warnList",
                indent=0,
                id_expr="(msgId as text)",
                from_runs=True,
            )
        ),
    ]
    if include_attachments:
        row += _block_lines(
            attachment_walk_block(message_var="msgRef", warnings_var="warnList", indent=0)
        )
    record = _search_record(
        id_expr="(msgId as text)",
        value=lambda _prop, stem, as_text: f"({stem}Value as text)" if as_text else f"{stem}Value",
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
        f"        else if idx - runEnd > {SEARCH_RUN_GAP + 1} then",
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


def search_script_body(
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
        f'    set warnList to {{"{SEARCH_OUT_OF_LINE_WARNING}"}}',
        *_indented(
            _one_at_a_time_search_lines(
                criteria, limit=limit, include_attachments=include_attachments
            ),
            4,
        ),
        "end if",
        "set resultData to {|messages|:resultData, |warnings|:warnList}",
    ]
    return 'tell application "Mail"\n' + "\n".join(_indented(body, 4)) + "\nend tell"
