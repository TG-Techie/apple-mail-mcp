"""Message tools: ``search_messages``, ``get_messages``, ``get_thread``,
``update_message``, ``save_attachments`` and ``delete_messages``.
"""

import datetime as _dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import server
from ..exceptions import MailMessageNotFoundError
from ..security import (
    check_rate_limit,
    check_test_mode_safety,
    operation_logger,
    validate_bulk_operation,
)
from ..server import envelope, mcp

_SELECTED_SENTINEL = "SELECTED"


def _resolve_id_list_to_messages(
    ids: list[str],
    include_content: bool,
    account: str | None,
    mailbox: str | None,
    headers_only: bool = False,
    include_attachments: bool = False,
    on_missing: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a mixed list of ids and ``SELECTED`` tokens to message dicts.

    ``SELECTED`` tokens expand inline to Mail.app's current UI selection
    (zero-or-more messages). Real ids are looked up via
    ``server.mail.get_message()``. An explicitly-requested id that cannot be
    located still drops from the returned list (partial-results
    convention), but the drop is no longer silent: ``on_missing`` (if
    provided) is invoked with each such id so the caller can surface the
    cardinality gap (NO SILENT ERRORS). The connector
    ``get_selected_messages`` is called at most once even if ``SELECTED``
    appears multiple times.

    Used by both ``search_messages.source`` (metadata mode,
    ``include_content=False``) and ``get_messages.message_ids`` (bodies
    mode, ``include_content=True``). The ``include_attachments`` flag
    threads through to both connector methods.
    """
    selected_resolved: list[dict[str, Any]] | None = None
    out: list[dict[str, Any]] = []
    for id_or_token in ids:
        if id_or_token == _SELECTED_SENTINEL:
            if selected_resolved is None:
                selected_resolved = server.mail.get_selected_messages(
                    include_content=include_content,
                    include_attachments=include_attachments,
                )
            out.extend(selected_resolved)
        else:
            try:
                msg = server.mail.get_message(
                    id_or_token,
                    include_content=include_content,
                    headers_only=headers_only,
                    account=account,
                    mailbox=mailbox,
                    include_attachments=include_attachments,
                )
                out.append(msg)
            except MailMessageNotFoundError:
                # Partial-results: the id drops from the list, but report
                # it so the caller never fails quiet on a cardinality gap.
                if on_missing is not None:
                    on_missing(id_or_token)
                continue
    return out


def _received_day(raw: object) -> str | None:
    """``YYYY-MM-DD`` for a Mail ``date_received`` string, or None.

    Mail renders dates as ``Monday, September 7, 2026 at 18:29:25``.
    Comparing that string against an ISO bound is not a date comparison
    at all — ``"M"`` sorts after ``"2"``, so ``date_received < "2026-09-01"``
    was false for every message and the bound never excluded anything.
    Found alongside the AppleScript ``date "..."`` defect on 2026-09-07;
    this one was read from the code, not reproduced live.
    """
    if not isinstance(raw, str) or not raw:
        return None
    for fmt in ("%A, %B %d, %Y at %H:%M:%S", "%A, %B %d, %Y at %H:%M:%S %p"):
        try:
            return _dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    # Already ISO-shaped (other producers of this field).
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return None


def _apply_search_filters(
    messages: list[dict[str, Any]],
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None,
    date_to: str | None,
    has_attachment: bool | None,
    limit: int,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Post-filter a list of message dicts in Python.

    Used by the ``source=[ids]`` dispatch path of ``search_messages``:
    after resolving the id list to message dicts (some via
    ``server.mail.get_selected_messages`` for the ``SELECTED`` sentinel, others
    via per-id ``server.mail.get_message``), apply the same predicates the
    IMAP/AppleScript search paths apply server-side, then truncate to
    ``limit``. The corpus is bounded by the caller's id list, so the
    cost is negligible.

    ``body_contains`` and ``text_contains`` (#145) match against the
    ``content`` field — the server tier forces ``include_content=True``
    on the per-id fetch when these filters are set, so ``content`` is
    populated. ``text_contains`` checks ``content + subject + sender``
    (the practical IMAP ``TEXT`` approximation; recipients omitted).
    """
    def matches(m: dict[str, Any]) -> bool:
        if sender_contains is not None and sender_contains.lower() not in str(
            m.get("sender", "")
        ).lower():
            return False
        if subject_contains is not None and subject_contains.lower() not in str(
            m.get("subject", "")
        ).lower():
            return False
        if read_status is not None and bool(m.get("read_status")) != read_status:
            return False
        if is_flagged is not None and bool(m.get("flagged")) != is_flagged:
            return False
        if date_from is not None or date_to is not None:
            day = _received_day(m.get("date_received"))
            # An unparseable date is not evidence the message is out of
            # range. Keep it and let the caller see it, rather than
            # silently dropping mail because Mail phrased a date oddly.
            if day is not None:
                if date_from is not None and day < date_from:
                    return False
                if date_to is not None and day > date_to:
                    return False
        if has_attachment is not None and bool(
            m.get("has_attachment")
        ) != has_attachment:
            return False
        if body_contains is not None and body_contains.lower() not in str(
            m.get("content", "")
        ).lower():
            return False
        if text_contains is not None:
            needle = text_contains.lower()
            haystack = (
                str(m.get("content", "")).lower()
                + " "
                + str(m.get("subject", "")).lower()
                + " "
                + str(m.get("sender", "")).lower()
            )
            if needle not in haystack:
                return False
        return True

    return [m for m in messages if matches(m)][:limit]


@mcp.tool()
@envelope
def search_messages(
    account: str | None = None,
    mailbox: str = "INBOX",
    sender_contains: str | None = None,
    subject_contains: str | None = None,
    read_status: bool | None = None,
    is_flagged: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_attachment: bool | None = None,
    limit: int = 50,
    source: list[str] | None = None,
    include_attachments: bool = False,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> dict[str, Any]:
    """
    Search for messages matching criteria. Returns metadata-only rows.

    Two corpus modes:

    - ``source=None`` (default): search the given account/mailbox using
      the IMAP/AppleScript SEARCH path. ``account`` is required.
    - ``source=[id1, id2, ...]``: scope the search to the specific
      messages identified by the given ids. ``account``/``mailbox`` are
      ignored; the connector resolves each id self-sufficiently. The
      resulting message dicts are post-filtered by the other criteria
      (``sender_contains``, ``read_status``, etc.) — full filter
      composition. The literal token ``"SELECTED"`` may appear in the
      list and is server-resolved at call time to Mail.app's current UI
      selection (zero-or-more messages). Mixed lists like
      ``["SELECTED", "12345"]`` are valid. Missing ids drop out silently
      (partial-results).

    For thread retrieval, call ``get_thread(message_id)`` to expand an
    anchor into thread member ids, then optionally pipe those ids into
    ``source=[ids]`` for filtered metadata browsing or into
    ``get_messages([ids])`` for full bodies.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts). Required when ``source is None``;
            ignored when ``source`` is a list. Names are convenient but
            unstable across renames; UUIDs are stable.
        mailbox: Mailbox name (default: "INBOX"). Ignored when ``source``
            is a list.
        sender_contains: Filter by sender email/domain substring.
        subject_contains: Filter by subject keywords substring.
        read_status: Filter by read status (true=read, false=unread).
        is_flagged: Filter by flagged status (true=flagged, false=not flagged).
        date_from: Inclusive lower bound on date received. ISO 8601 YYYY-MM-DD.
        date_to: Inclusive upper bound on date received (full day included). ISO 8601 YYYY-MM-DD.
        has_attachment: Filter messages with (true) or without (false) attachments.
        limit: Maximum results to return (default: 50).
        source: Optional list of message ids (with optional ``"SELECTED"``
            sentinel) to restrict the search to. ``None`` (default)
            searches the account/mailbox normally.
        include_attachments: When True, each row includes an ``attachments``
            field listing per-attachment metadata (name, mime_type, size,
            downloaded). Default False — opt-in because the AppleScript
            fallback path can be slow on cold caches (#142). Free on the
            IMAP fast path. To fetch attachment metadata for a known list
            of ids cheaply, prefer ``get_messages([ids])`` (default-on
            attachments, bounded cardinality).
        body_contains: Substring match against message body content. IMAP
            uses ``BODY`` predicate (sub-second); AppleScript reads
            ``content of msg`` per candidate (very slow on large mailboxes
            — measured 148s for 100 cold-cache messages). When the call
            commits to AppleScript with this filter set, a ``warnings``
            field is included in the response. Case-insensitive on both
            paths.
        text_contains: Substring match against headers + body (RFC 3501
            ``TEXT`` semantics). On AppleScript, approximated as
            ``content + subject + sender`` (recipients and other headers
            not matched). Same perf characteristics as ``body_contains``.

    Returns:
        Dictionary containing matching messages. Each message row includes
        id, subject, sender, to, cc, bcc, date_received, read_status,
        flagged. ``to``/``cc``/``bcc`` are lists of ``Name <address>`` or
        bare addresses; ``bcc`` is only ever non-empty on mail the account
        sent. Rows are metadata-only — call ``get_messages([ids])`` for
        bodies.

    Example:
        >>> search_messages("Gmail", sender_contains="john@example.com", read_status=False, limit=10)
        {"success": True, "messages": [...], "count": 5, "limit": 10, "truncated": False}
        >>> search_messages(source=["SELECTED"])
        {"success": True, "messages": [...], "count": 2}
        >>> search_messages(source=["12345", "SELECTED"], read_status=False)
        {"success": True, "messages": [...], "count": 3}
    """
    warnings: list[str] = []
    if source is not None:
        # body/text filters need bodies on the resolved messages so the
        # post-filter can match content. Force include_content=True for
        # the per-id fetch when these filters are set.
        resolved = _resolve_id_list_to_messages(
            source,
            include_content=bool(body_contains or text_contains),
            account=account,
            mailbox=mailbox,
            include_attachments=include_attachments,
        )
        messages = _apply_search_filters(
            resolved,
            sender_contains,
            subject_contains,
            read_status,
            is_flagged,
            date_from,
            date_to,
            has_attachment,
            limit,
            body_contains=body_contains,
            text_contains=text_contains,
        )
        scope: dict[str, Any] = {"source": source}
    else:
        if account is None:
            raise ValueError("account is required when source is not provided")
        if refused := check_test_mode_safety("search_messages", account=account):
            return refused
        if refused := check_rate_limit(
            "search_messages", {"account": account, "mailbox": mailbox}
        ):
            return refused
        messages = server.mail.search_messages(
            account=account,
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
            on_warning=warnings.append,
        )
        scope = {"account": account, "mailbox": mailbox}

    filters = {
        "sender": sender_contains,
        "subject": subject_contains,
        "read_status": read_status,
        "is_flagged": is_flagged,
        "date_from": date_from,
        "date_to": date_to,
        "has_attachment": has_attachment,
        "body_contains": body_contains,
        "text_contains": text_contains,
    }
    operation_logger.log_operation(
        "search_messages", {**scope, "filters": filters}, "success"
    )
    # count == limit is the one result a caller cannot read on its own:
    # everything, or the first page of much more. Say which.
    response: dict[str, Any] = {
        "success": True,
        "account": scope.get("account"),
        "mailbox": scope.get("mailbox"),
        "messages": messages,
        "count": len(messages),
        "limit": limit,
        "truncated": len(messages) >= limit,
    }
    if warnings:
        response["warnings"] = warnings
    return response


@mcp.tool()
@envelope
def get_messages(
    message_ids: list[str],
    include_content: bool = True,
    headers_only: bool = False,
    account: str | None = None,
    mailbox: str | None = None,
    include_attachments: bool = True,
) -> dict[str, Any]:
    """
    Get full details of one or more messages, with bodies.

    Returns a list of message dicts (possibly of length 0 or 1). Pair with
    ``search_messages`` (metadata-only) and ``get_thread`` (thread member
    ids) to fetch bodies for specific messages.

    Args:
        message_ids: List of message ids to fetch. May include the literal
            token ``"SELECTED"``, which the server resolves at call time
            to Mail.app's current UI selection (zero-or-more messages).
            Mixed lists like ``["SELECTED", "12345"]`` are valid. Empty
            list is a no-op (returns empty result, no error). An
            explicitly-requested id that cannot be located drops from the
            ``messages`` list (partial-results convention) but is reported
            in the response ``warnings`` field, so a count lower than the
            number of requested ids is never silent.
        include_content: Include message bodies (default: True).
        headers_only: Skip body fetch on the IMAP path for explicit ids
            (default: False). Silently ignored on the AppleScript fallback.
        account: Mail.app account name. Together with ``mailbox``, activates
            the IMAP fast path for explicit ids: one round-trip lookup
            instead of an account×mailbox AppleScript scan (issue #72).
            Ignored for the ``"SELECTED"`` sentinel (selection is global).
        mailbox: Folder to look in for the IMAP fast path (e.g. "INBOX").
        include_attachments: Include per-attachment metadata (name,
            mime_type, size, downloaded) on each message (default: True).
            Bounded cost — id-list cardinality is typically 1-10. Free on
            the IMAP fast path; cheap-enough on the AppleScript fallback
            for typical id counts.

    Returns:
        Dictionary containing the list of messages and count. Rows carry
        the ``search_messages`` fields (recipients included) plus
        ``content`` and, when requested, ``attachments``.

    Example:
        >>> get_messages(["12345"], account="iCloud", mailbox="INBOX")
        {"success": True, "messages": [...], "count": 1}
        >>> get_messages(["SELECTED"])
        {"success": True, "messages": [...], "count": 2}
        >>> get_messages(["SELECTED", "12345"])
        {"success": True, "messages": [...], "count": 3}
    """
    if refused := check_rate_limit("get_messages", {"count": len(message_ids)}):
        return refused
    missing_ids: list[str] = []
    messages = _resolve_id_list_to_messages(
        message_ids,
        include_content=include_content,
        account=account,
        mailbox=mailbox,
        headers_only=headers_only,
        include_attachments=include_attachments,
        on_missing=missing_ids.append,
    )

    # NO SILENT ERRORS: surface both kinds of non-fatal degradation at
    # the response root. (1) Ids that were requested but not located —
    # the cardinality gap is reported, never dropped quietly. (2)
    # Per-message attachment-enumeration warnings emitted by the
    # connector (e.g. inline-image -10000) are lifted to the top level,
    # mirroring search_messages' top-level ``warnings`` field, while
    # remaining attributable on each message dict.
    warnings: list[str] = []
    if missing_ids:
        warnings.append(
            "requested ids not found and dropped from results: "
            + ", ".join(missing_ids)
        )
    for m in messages:
        warnings.extend(m.get("warnings", []) or [])

    operation_logger.log_operation(
        "get_messages", {"count": len(message_ids)}, "success"
    )
    response: dict[str, Any] = {
        "success": True,
        "messages": messages,
        "count": len(messages),
    }
    if warnings:
        response["warnings"] = warnings
    return response


@mcp.tool()
@envelope
def update_message(
    message_ids: list[str],
    read_status: bool | None = None,
    flagged: bool | None = None,
    flag_color: str | None = None,
    destination_mailbox: str | None = None,
    account: str | None = None,
    source_mailbox: str | None = None,
    gmail_mode: bool = False,
) -> dict[str, Any]:
    """
    Update one or more messages: change read state, flag, and/or move,
    in one atomic call (#135).

    Patch semantics — caller specifies only the fields to change. All
    specified mutations apply in a single AppleScript pass via the
    bulk-update helper. Replaces the previous `mark_as_read`,
    `move_messages`, and `flag_message` tools.

    Order of operations (matters for IMAP): read-state and flag changes
    apply first (in source mailbox), then the move. IMAP requires the
    message to exist in the source folder for STORE before MOVE.

    Args:
        message_ids: List of message IDs to update.
        read_status: True to mark as read, False to mark as unread,
            None to leave unchanged.
        flagged: True to flag (default red if no `flag_color` set),
            False to clear the flag, None to leave unchanged.
        flag_color: Color name (orange, red, yellow, blue, green,
            purple, gray, none). Implies `flagged=True` unless "none".
            Validated against the existing flag-color schema.
        destination_mailbox: Move messages here (requires `account`).
        account: Account name or UUID hosting the destination mailbox.
            Required when `destination_mailbox` is set; also used with
            `source_mailbox` for narrow-path optimization.
        source_mailbox: Source mailbox name. With `account`, narrows the
            AppleScript scan to one mailbox (O(N) instead of cross-scan).
        gmail_mode: Use Gmail-specific copy+delete instead of MOVE.

    Returns:
        Dictionary with `updated` (int count) and `requested` (input count).

    Example:
        >>> # Mark read + move to Archive in one call:
        >>> update_message(
        ...     ["12345"], read_status=True,
        ...     destination_mailbox="Archive", account="iCloud",
        ...     source_mailbox="INBOX",
        ... )
        {"success": True, "updated": 1, "requested": 1}

        >>> # Restore from Trash:
        >>> update_message(
        ...     ["12345"], destination_mailbox="INBOX",
        ...     account="iCloud", source_mailbox="Deleted Messages",
        ... )

        >>> # Set red flag:
        >>> update_message(["12345"], flag_color="red")
    """
    # Validate at least one field is set (AC #3 from #135).
    if (
        read_status is None
        and flagged is None
        and flag_color is None
        and destination_mailbox is None
    ):
        raise ValueError("specify at least one field to update")
    # Test-mode safety: the gate compares a given account against
    # MAIL_TEST_ACCOUNT and refuses a missing one, since message ids
    # reach every account.
    if refused := check_test_mode_safety("update_message", account=account):
        return refused
    if refused := check_rate_limit("update_message", {"count": len(message_ids)}):
        return refused
    is_valid, error_msg = validate_bulk_operation(len(message_ids), max_items=100)
    if not is_valid:
        raise ValueError(error_msg)

    count = server.mail.update_message(
        message_ids,
        read_status=read_status,
        flagged=flagged,
        flag_color=flag_color,
        destination_mailbox=destination_mailbox,
        account=account,
        source_mailbox=source_mailbox,
        gmail_mode=gmail_mode,
    )
    operation_logger.log_operation(
        "update_message",
        {
            "message_ids": message_ids,
            "requested": len(message_ids),
            "updated": count,
            "read_status": read_status,
            "flagged": flagged,
            "flag_color": flag_color,
            "destination_mailbox": destination_mailbox,
            "account": account,
            "source_mailbox": source_mailbox,
            "gmail_mode": gmail_mode,
        },
        "success",
    )
    return {"success": True, "updated": count, "requested": len(message_ids)}


@mcp.tool()
@envelope
def get_thread(message_id: str) -> dict[str, Any]:
    """
    Return all messages in the thread containing the given message.

    Looks up the anchor message by its id, then reconstructs the
    conversation via the connector's tiered IMAP threading dispatch
    (Tier 1 X-GM-THRID for Gmail, Tier 3 header-search BFS fallback)
    or the AppleScript path. Result rows are sorted by ``date_received``
    ascending.

    The returned ids can be piped into ``search_messages(source=[ids])``
    for filtered metadata or ``get_messages([ids])`` for full bodies.

    Known limitation: thread members whose subject was rewritten
    mid-conversation are missed on the AppleScript fallback path
    (subject prefilter tradeoff).

    Args:
        message_id: Internal id of any message in the thread
            (from ``search_messages`` or ``get_messages`` results).

    Returns:
        Dictionary with the thread list. Rows are metadata-only —
        id, subject, sender, to, cc, bcc, date_received, read_status,
        flagged, as ``search_messages`` returns them.

    Example:
        >>> get_thread("12345")
        {"success": True, "thread": [{...}, {...}], "count": 2}
    """
    if refused := check_rate_limit("get_thread", {"message_id": message_id}):
        return refused
    warnings: list[str] = []
    thread = server.mail.get_thread(message_id, on_warning=warnings.append)
    operation_logger.log_operation("get_thread", {"message_id": message_id}, "success")
    response: dict[str, Any] = {"success": True, "thread": thread, "count": len(thread)}
    if warnings:
        response["warnings"] = warnings
    return response


@mcp.tool()
@envelope
def save_attachments(
    message_id: str,
    save_directory: str,
    attachment_indices: list[int] = [],  # noqa: B006 — coerced to None below
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Save attachments from a message to a directory.

    Args:
        message_id: Message ID from search results
        save_directory: Directory path to save attachments to
        attachment_indices: 0-based positions in the message's attachment
            list, as `get_messages` orders them; empty saves all. An index
            the message does not have is refused (`validation_error`) and
            nothing is written.
        overwrite: Replace files already in the directory. Without it, a
            name that is already taken is refused with `file_exists` and
            nothing is written. Attachments sharing a name within the
            message are always saved to distinct files (`name (2).ext`).

    Returns:
        Dictionary indicating success and number of attachments saved

    Example:
        >>> save_attachments("12345", "/Users/me/Downloads")
        {"success": True, "saved": 2, "directory": "/Users/me/Downloads"}

        >>> save_attachments("12345", "/Users/me/Downloads", [0, 2])
        {"success": True, "saved": 2, "directory": "/Users/me/Downloads"}
    """
    if refused := check_rate_limit("save_attachments", {"message_id": message_id}):
        return refused
    save_path = Path(save_directory)
    if not save_path.exists():
        return {
            "success": False,
            "error": f"Directory does not exist: {save_directory}",
            "error_type": "directory_not_found",
        }
    if not save_path.is_dir():
        return {
            "success": False,
            "error": f"Path is not a directory: {save_directory}",
            "error_type": "invalid_directory",
        }

    indices = attachment_indices or None
    count, warnings = server.mail.save_attachments(
        message_id=message_id,
        save_directory=save_path,
        attachment_indices=indices,
        overwrite=overwrite,
    )
    operation_logger.log_operation(
        "save_attachments",
        {"message_id": message_id, "directory": save_directory, "indices": indices},
        "success",
    )
    # NO SILENT ERRORS: connector returns (count, warnings); lift
    # warnings to the response when present so a 0-saved result
    # caused by Mail.app -10000 enumeration is never silent.
    response: dict[str, Any] = {
        "success": True,
        "saved": count,
        "directory": save_directory,
    }
    if warnings:
        response["warnings"] = warnings
    return response


@mcp.tool()
@envelope
def delete_messages(
    message_ids: list[str],
    permanent: bool = False,
    account: str | None = None,
    source_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Delete messages (always moves to the account's Trash mailbox).

    Args:
        message_ids: List of message IDs to delete
        permanent: Reserved; currently a no-op. Mail.app's AppleScript
            dictionary exposes no path to permanent-delete that bypasses
            Trash (issue #111). Passing True returns `permanent: false`
            and a `warning` saying so; messages still go to Trash.
            Recoverable from the account's Trash mailbox until that
            mailbox is emptied.
        account: Optional account name (or UUID) the messages live in.
            Must be provided together with `source_mailbox`. When both
            are given, the operation is much faster.
        source_mailbox: Optional source mailbox name; see `account`.

    Returns:
        Dictionary with success status and number of messages deleted

    Example:
        delete_messages(
            message_ids=["12345"],
            account="Gmail",
            source_mailbox="INBOX",
        )

    Note:
        Bulk deletions are limited to 100 messages for safety.
        All deletes are recoverable from Trash; there is currently no
        AppleScript path to bypass it. See issue #111.
    """
    if not message_ids:
        return {"success": True, "count": 0, "message": "No messages to delete"}
    if refused := check_rate_limit("delete_messages", {"count": len(message_ids)}):
        return refused
    if len(message_ids) > 100:
        raise ValueError(f"Cannot delete {len(message_ids)} messages at once (max: 100)")
    # Test-mode safety: same rule as update_message; a missing account
    # is refused under test mode, not skipped.
    if refused := check_test_mode_safety("delete_messages", account=account):
        return refused

    count = server.mail.delete_messages(
        message_ids=message_ids,
        permanent=permanent,
        skip_bulk_check=False,  # Enforce limit
        account=account,
        source_mailbox=source_mailbox,
    )
    operation_logger.log_operation(
        "delete_messages",
        {
            "message_ids": message_ids,
            "count": count,
            "permanent_requested": permanent,
            "account": account,
            "source_mailbox": source_mailbox,
        },
        "success",
    )
    # `permanent` reports what happened, not what was asked: nothing
    # bypasses Trash (issue #111), and the connector's DeprecationWarning
    # fires in this process where no MCP client can see it.
    response: dict[str, Any] = {
        "success": True,
        "count": count,
        "requested": len(message_ids),
        "permanent": False,
    }
    if permanent:
        response["warning"] = (
            "permanent=True was asked for, but Mail.app exposes no way "
            "to bypass Trash; the messages were moved to Trash and are "
            "recoverable from there until it is emptied (issue #111)."
        )
    return response
