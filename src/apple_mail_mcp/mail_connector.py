"""
AppleScript-based connector for Apple Mail.
"""

import logging
import time
import warnings
from collections.abc import Callable
from datetime import date as _date
from pathlib import Path
from typing import Any, cast

from imapclient.exceptions import IMAPClientError, LoginError

from .connector_compose import ComposeConnector
from .connector_rules import RulesConnector
from .exceptions import (
    MailAppleScriptError,
    MailImapMoveUnsupportedError,
    MailImapRequiredError,
    MailImapTrashNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotEmptyError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailUnsupportedGmailSystemLabelError,
)
from .imap_connector import ImapConnector
from .keychain import get_imap_password
from .script_blocks import (
    RECIPIENT_FIELDS,
    SOURCE_FIELDS,
    attachment_walk_block,
    build_flag_actions,
    bulk_repeat_block,
    recipient_read_block,
    render_recipients,
    source_read_block,
    take_links,
    wrap_as_json_script,
)
from .search_script import (
    search_criteria,
    search_script_body,
)
from .utils import (
    applescript_account_clause,
    destination_names,
    escape_applescript_string,
    parse_applescript_json,
    sanitize_input,
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

# Threshold (seconds) above which the AppleScript search path emits an
# INFO-level recommendation to enable IMAP delegation. Calibrated against
# the post-#32 baseline: a 50-message search on a 200+ msg mailbox runs
# in well under a second; sustained >5s suggests the user is hitting the
# AppleScript fallback against a mailbox where IMAP would help.
_SLOW_SEARCH_THRESHOLD_SEC = 5.0


class AppleMailConnector(ComposeConnector, RulesConnector):
    """Interface to Apple Mail via AppleScript."""

    _IMAP_BREAKER_TTL_S: float = 30.0
    """How long to skip IMAP for an account after a fallback-triggering
    failure. 30s is long enough to skip a tight burst of calls (typical
    agent workloads do many calls in succession), short enough that a
    refreshed Keychain entry or recovered network is picked up within a
    minute. Class constant — no public knob; tune by subclassing if
    really needed. See issue #118."""

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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
        # (see SEARCH_CONTENT_RUN_GAP). A text criterion's subject and
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
        criteria, date_setup = search_criteria(
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
        tell_body = search_script_body(
            account_clause=account_clause,
            mailbox_safe=mailbox_safe,
            criteria=criteria,
            date_setup=date_setup,
            limit=str(limit) if limit else "999999999",
            include_attachments=include_attachments,
        )

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
            render_recipients(message)
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
        include_links: bool = False,
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
            include_links: Add ``links``, read from the message's raw
                source (``links.links_from_source``): ``source of`` in the
                same script on the AppleScript path, ``BODY[]`` in the
                same FETCH on the IMAP path.

        Returns:
            Message dictionary with keys: id, rfc_message_id, subject,
            sender, to, cc, bcc, date_received, read_status, flagged,
            content; ``warnings`` when there are any, and always with
            ``attachments``; ``links`` with ``include_links``.

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
                    include_links=include_links,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        return self._get_message_applescript(
            message_id, include_content, include_attachments,
            include_links=include_links,
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
        include_links: bool = False,
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
            include_links=include_links,
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

        att_walk = attachment_walk_block(
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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
        *,
        include_links: bool = False,
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

        ``include_links`` reads the source in the same script
        (``source_read_block``), so it costs two Apple events and no
        further ``osascript`` call.
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
        recipients_clause = recipient_read_block(
            message_var="msg", warnings_var="recipWarnings", indent=24
        )
        source_clause = (
            source_read_block(message_var="msg", indent=24) if include_links else ""
        )
        source_fields = f", {SOURCE_FIELDS}" if include_links else ""

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
{source_clause}
                        set resultData to {{|id|:(id of msg as text), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent, {RECIPIENT_FIELDS}, |warnings|:recipWarnings{source_fields}}}
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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        msg = cast(dict[str, Any], parse_applescript_json(result))
        render_recipients(msg)
        warnings = cast(list[str], msg.pop("warnings", None) or [])
        if include_links:
            warnings += take_links(msg)

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

        anchor_script = wrap_as_json_script(anchor_body, timeout=self.timeout)
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
        recipients_clause = recipient_read_block(
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
                        set candRecord to {{|id|:(id of m as text), |rfc_message_id|:(message id of m), |in_reply_to|:inReplyTo, |references_raw|:refs, |subject|:(subject of m), |sender|:(sender of m), |date_received|:(date received of m as text), |read_status|:(read status of m), |flagged|:(flagged status of m), {RECIPIENT_FIELDS}, |warnings|:recipWarnings}}
                        set end of resultData to candRecord
                    end repeat
                on error
                    -- Some mailboxes (e.g. Gmail smart labels) reject whose clauses; skip
                end try
            end repeat
        end tell
        '''

        candidates_script = wrap_as_json_script(candidates_body, timeout=self.timeout)
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
            render_recipients(m)
            for warning in m.pop("warnings", None) or []:
                if on_warning is not None:
                    on_warning(warning)

        return thread

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
        safe_names = destination_names(
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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
        all of them in a single AppleScript pass via ``bulk_repeat_block``.

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

        actions.extend(build_flag_actions(flagged, flag_color))

        # Move (always last — IMAP STORE requires source folder).
        if destination_mailbox is not None:
            if gmail_mode:
                actions.append("duplicate msg to destMailbox")
                actions.append("delete msg")
            else:
                actions.append("set mailbox of msg to destMailbox")

        repeat_block = bulk_repeat_block(
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

        repeat_block = bulk_repeat_block(
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
        include_links: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get messages currently selected in Apple Mail.

        Args:
            include_content: Include message body (default: True)
            include_attachments: Include per-message attachment metadata
                list (name, mime_type, size, downloaded). On AppleScript,
                this can be expensive on cold caches — see #142.
            include_links: Add ``links``, read from each message's source
                in the same script, as ``get_message`` does.

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
        recipients_clause = recipient_read_block(
            message_var="msg", warnings_var="recipWarnings", indent=16
        )
        source_clause = (
            source_read_block(message_var="msg", indent=16) if include_links else ""
        )
        source_fields = f", {SOURCE_FIELDS}" if include_links else ""

        tell_body = f"""
        tell application "Mail"
            set resultData to {{}}
            set sel to selection
            repeat with msg in sel
                {content_clause}
                set recipWarnings to {{}}
{recipients_clause}
{source_clause}
                set msgRecord to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent, {RECIPIENT_FIELDS}, |warnings|:recipWarnings{source_fields}}}
                set end of resultData to msgRecord
            end repeat
        end tell
        """

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        messages = cast(list[dict[str, Any]], parse_applescript_json(result))

        for m in messages:
            render_recipients(m)
            warnings = cast(list[str], m.pop("warnings", None) or [])
            if include_links:
                warnings += take_links(m)
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

    # -- tending Mail's compose windows ------------------------------------
    #
    # docs/research/compose-window-tending.md: what was measured, and the
    # rule. The decision is compose_tending.plan_tending; the AppleScript
    # that reads the windows and closes the ones decided on is here.

