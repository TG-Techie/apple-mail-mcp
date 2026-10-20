"""The connector's compositions: drafts, sends, the compose windows they open,
and the tending that closes the windows left behind."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .compose_ledger import Closed, LeftOpen, Seed, WindowOperation
from .compose_script import (
    CLOSE_FAILED,
    WINDOW_HANDLERS,
    CloseMode,
    PastePlacement,
    as_verified_send_block,
    build_attachment_ax_verify_script,
    build_close_script,
    build_open_compose_script,
    build_paste_script,
    build_readback_script,
    build_tend_close_script,
    files_paste_fill,
    paste_probe_strings,
    text_paste_fill,
)
from .compose_tending import (
    RECORD_RETENTION_S,
    STALE_S,
    TEND_GRACE_S,
    TendAction,
    TendPlan,
    TendReport,
    TendRule,
    inventory_from_report,
    plan_tending,
)
from .compose_window import (
    ComposeWindow,
    SentMessageCopy,
    SentMessageCopyUnidentified,
    WindowSnapshot,
    check_sent_attachments,
    closing_of,
    compose_window_from_report,
    composition_closing,
    existing_files,
    gate_compose_recipients,
    gate_named_recipients,
    refuse_overlong_body,
    sent_result,
    validate_compose_args,
)
from .connector_base import ConnectorBase, logger
from .drafts import _validate_draft_id
from .exceptions import (
    MailAppleScriptError,
    MailComposeLedgerError,
    MailComposeWindowError,
    MailDraftNotFoundError,
    MailDraftNotSettledError,
    MailError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
    MailTimeoutError,
)
from .outbound_allowlist import assert_recipients_allowed_for_send
from .script_blocks import wrap_as_json_script
from .utils import escape_applescript_string, parse_applescript_json, sanitize_input


class ComposeConnector(ConnectorBase):
    """Drafts, sends, compose windows and their tending."""

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

    # How long, after an opening script failed without reporting a window,
    # to look for one Mail opens late: the script already waited 5 s.
    _ADOPT_WAIT_S = 10.0

    _ADOPT_POLL_S = 0.5

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

    def find_message_by_message_id(self, rfc5322_message_id: str) -> str | None:
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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        data = parse_applescript_json(raw)
        if not isinstance(data, dict) or not data.get("found"):
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")
        # Drop the internal flag from the user-visible payload.
        data.pop("found", None)
        return cast(dict[str, Any], data)

    def _maybe_resolve_rfc_seed_id(self, seed: str, seed_id: str | None) -> str | None:
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
            raise MailMessageNotFoundError(f"no message with message-id {seed_id!r}")
        return resolved

    def _close_compose_window(
        self, window_name: str, window_id: int | None, mode: CloseMode
    ) -> str:
        """Close one compose window (``as_close_compose_block``); what the
        block read back, or "SALVAGE_FAILED:" / "DISCARD_FAILED:" with the
        error when the script itself failed."""
        try:
            return self._run_applescript(build_close_script(window_name, window_id, mode)).strip()
        except MailAppleScriptError as exc:
            return f"{CLOSE_FAILED[mode]}{exc}"

    def _salvage_compose_to_draft(self, window_name: str, window_id: int | None = None) -> str:
        """Close a compose window SAVING it as a draft (best effort): Mail's
        window ``window_id`` when given, else the one window named
        ``window_name``.

        Failure policy on a headless machine (Jonah, 2026-07-23): a
        failed send attempt must never park an open compose window —
        salvage the content to Drafts (close button → "Save" on the
        save sheet) so nothing is lost and nothing blocks later UI
        automation. Returns "SALVAGED", "NO_WINDOW", or the observed
        state on failure — callers append this to their error, never
        mask the original failure with it.
        """
        return self._close_compose_window(window_name, window_id, "save")

    def _salvage(self, window: ComposeWindow) -> str:
        """Salvage a composition's own window, by Mail's id for it when
        the opening script found one."""
        return self._salvage_compose_to_draft(window.name, window.window_id)

    def _discard_compose_window(self, window_name: str, window_id: int | None = None) -> str:
        """Close a compose window without saving it: Mail's window
        ``window_id`` when given, else the one window named
        ``window_name``. Returns "DISCARDED", "NO_WINDOW", or
        "DISCARD_FAILED:…"."""
        return self._close_compose_window(window_name, window_id, "discard")

    def _build_compose_inventory_script(self) -> str:
        """Full osascript source: every compose window System Events lists
        (a window whose toolbar has a Send button), each read by its
        index alone (Observation 7): its name and position, its header
        fields, its body read level by level (``tendBodyRead``), whether
        a sheet is on it and whether it is minimised; and Mail's own id,
        name, bounds and visibility for every window, in one event.
        Read-only. When Mail is not running it says so and starts
        nothing."""
        body = """
if not (application "Mail" is running) then
    set resultData to {|running|:false}
else
    set composeWindows to {}
    tell application "System Events"
        tell application process "Mail"
            set mailPid to unix id
            set winCount to count of windows
        end tell
    end tell
    repeat with i from 1 to winCount
        if my tendIsCompose(i) then
            tell application "System Events"
                tell application process "Mail"
                    set winName to name of window i
                    set winPos to position of window i
                    set hasSheet to ((count of sheets of window i) > 0)
                    set isMin to value of attribute "AXMinimized" of window i
                end tell
            end tell
            set fieldValues to my tendFieldValues(i)
            set bodyRead to my tendBodyRead(i)
            set end of composeWindows to {|name|:winName, |position|:winPos, |fields|:fieldValues, |body|:(bodyState of bodyRead), |body_roles|:(bodyRoles of bodyRead), |body_values|:(bodyValues of bodyRead), |sheet|:hasSheet, |minimized|:isMin}
        end if
    end repeat
    tell application "Mail"
        set mailWindows to {}
        set windowProps to properties of every window
        repeat with p in windowProps
            set props to contents of p
            set end of mailWindows to {|id|:(id of props), |name|:(name of props), |bounds|:(bounds of props), |visible|:(visible of props)}
        end repeat
        set resultData to {|running|:true, |pid|:mailPid, |compose|:composeWindows, |mail_windows|:mailWindows}
    end tell
end if
"""
        return wrap_as_json_script(body, timeout=self.timeout) + WINDOW_HANDLERS

    def tend_compose_windows(
        self,
        *,
        dry_run: bool = False,
        grace_s: float = TEND_GRACE_S,
        stale_s: float = STALE_S,
    ) -> TendReport:
        """One tending pass over Mail's compose windows.

        Reads every compose window (``_build_compose_inventory_script``),
        the compose ledger and the clock of when each window was first
        seen with its present content (``compose_clock``), decides
        (``compose_tending.plan_tending``), saves the clock, and closes
        the windows decided on, one osascript each, by Mail's id for
        each: the connector's own abandoned windows (the ledger names
        them, and their composition cannot still be running,
        ``grace_s``), and any other window whose content has not changed
        for ``stale_s``. Each closed window is discarded if empty and
        salvaged to Drafts otherwise; a ledger record is ended as closed
        by tending. Every other window is left and counted. Records whose
        window is gone are ended as such, and records of windows closed
        more than ``RECORD_RETENTION_S`` ago are pruned.

        ``dry_run`` reads and decides, and closes, records and saves
        nothing: its report's ``to_close`` is what a real pass would
        close, and the clock it reads is left as it was.

        A close Mail does not answer stops the pass: the windows after it
        are reported as not attempted rather than each waiting out the
        timeout.
        """
        raw = self._run_applescript(self._build_compose_inventory_script())
        inventory = inventory_from_report(cast(dict[str, Any], parse_applescript_json(raw)))
        if inventory is None:
            return TendReport(dry_run=dry_run, mail_running=False, stale_s=stale_s)
        contents = self.compose_ledger.read_all()
        now = time.time()
        plan = plan_tending(
            inventory,
            contents.records,
            self.compose_clock.load(),
            now=now,
            grace_s=grace_s,
            stale_s=stale_s,
        )
        found = TendReport(
            dry_run=dry_run,
            mail_running=True,
            compose_windows=len(inventory.windows),
            stale_s=stale_s,
            left=plan.left,
            records_unidentified=plan.unidentified,
            records_unreadable=contents.unreadable,
        )
        if dry_run:
            return replace(
                found,
                to_close=tuple((a.window_name, a.action, a.rule) for a in plan.actions),
                records_gone=len(plan.gone),
            )
        self._save_clock(plan)
        closed, failed, not_attempted = self._carry_out(plan.actions)
        gone = sum(
            self._end_tended_record(record.record_id, Closed(how="gone", by="tending", at=now))
            for record in plan.gone
        )
        pruned = self.compose_ledger.prune(before=now - RECORD_RETENTION_S)
        return replace(
            found,
            closed=closed,
            failed=failed,
            not_attempted=not_attempted,
            records_gone=gone,
            records_pruned=pruned,
        )

    def _save_clock(self, plan: TendPlan) -> None:
        """Keep the pass's clock for the next. One that cannot be written
        is logged: the next pass then starts its windows' clocks again,
        which delays a close and never hastens one."""
        try:
            self.compose_clock.save(plan.clock)
        except OSError as exc:
            logger.error("compose clock: could not save: %s", exc)

    def _carry_out(
        self, actions: tuple[TendAction, ...]
    ) -> tuple[
        tuple[tuple[str, str, TendRule], ...],
        tuple[tuple[str, str], ...],
        tuple[str, ...],
    ]:
        """Close each window a pass decided on, ending its ledger record
        when it has one; what closed, what failed, and what was not
        attempted after a close Mail did not answer."""
        closed: list[tuple[str, str, TendRule]] = []
        failed: list[tuple[str, str]] = []
        for index, action in enumerate(actions):
            name = action.window_name
            try:
                outcome = self._run_applescript(build_tend_close_script(action)).strip()
            except MailAppleScriptError as exc:
                failed.append((name, str(exc)))
                rest = tuple(a.window_name for a in actions[index + 1 :])
                return tuple(closed), tuple(failed), rest
            closing = closing_of(outcome, by="tending", at=time.time())
            if closing is None:
                failed.append((name, outcome))
                continue
            if action.record is not None:
                self._end_tended_record(action.record.record_id, closing)
            closed.append((name, closing.how, action.rule))
        return tuple(closed), tuple(failed), ()

    def _end_tended_record(self, record_id: str, state: Closed) -> bool:
        """End a record tending acted on; False, logged, when it had been
        ended meanwhile (its composition finished after all)."""
        try:
            self.compose_ledger.end(record_id, state)
        except MailComposeLedgerError as exc:
            logger.warning("compose ledger: %s", exc)
            return False
        return True

    def _paste_verified(
        self,
        *,
        window: ComposeWindow,
        body: str,
        placement: PastePlacement,
        plain: bool,
    ) -> None:
        """Paste ``body`` into the compose window at ``placement``
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
        snippet, raw_marker = paste_probe_strings(body, plain=plain)
        fill = text_paste_fill(body, plain)

        for attempt in (1, 2):
            paste_result = self._run_applescript(
                build_paste_script(
                    window_name=window.name,
                    fill=fill,
                    placement=placement,
                    undo_first=(attempt == 2),
                )
            ).strip()
            if paste_result != "PASTED_UNVERIFIED":
                salvage = self._salvage(window)
                raise MailComposeWindowError(
                    f"paste: {paste_result!r} (compose window: {salvage})",
                    window_outcome=salvage,
                )
            seen = self._run_applescript(build_readback_script(window.name)).strip()
            # Normalize whitespace: styled runs (<b>…) split AX static
            # texts, so the joined read-back carries doubled spaces.
            seen_norm = " ".join(seen.split())
            raw_norm = " ".join(raw_marker.split())
            arrived = (not snippet) or (snippet in seen_norm)
            degraded = bool(raw_norm) and raw_norm in seen_norm
            if arrived and not degraded:
                return
            if attempt == 2:
                salvage = self._salvage(window)
                raise MailComposeWindowError(
                    f"paste: 'PASTE_FAILED:read-back saw [{seen}] "
                    f"wanted [{snippet}] without raw [{raw_marker}]' "
                    f"(compose window: {salvage})",
                    window_outcome=salvage,
                )

    def _paste_attachments(self, window: ComposeWindow, attachment_paths: list[Path]) -> None:
        """Paste the files at the end of the compose window's body,
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
            build_paste_script(
                window_name=window.name,
                fill=files_paste_fill(attachment_paths),
                placement="end",
                undo_first=False,
            )
        ).strip()
        if outcome == "PASTED_UNVERIFIED":
            outcome = self._run_applescript(
                build_attachment_ax_verify_script(
                    window_name=window.name,
                    filenames=[Path(p).name for p in attachment_paths],
                )
            ).strip()
        if outcome != "ATTACHMENTS_VERIFIED":
            salvage = self._salvage(window)
            raise MailComposeWindowError(
                f"attachments: {outcome!r}; send NOT attempted (compose window: {salvage})",
                window_outcome=salvage,
            )

    def _send_compose_window(self, window: ComposeWindow) -> None:
        """Run the verified send on the compose window; on any outcome but
        SENT the window is salvaged to Drafts and the outcome raised."""
        win_safe = escape_applescript_string(window.name)
        send_script = (
            f'set composeName to "{win_safe}"\n' + as_verified_send_block() + "\nreturn sendOutcome"
        )
        result = self._run_applescript(send_script).strip()
        if result == "SENT":
            return
        salvage = self._salvage(window)
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
            ("reply", reply_to)
            if reply_to is not None
            else ("forward", forward_of)
            if forward_of is not None
            else ("new", None)
        )
        validate_compose_args(seed, seed_id, to, subject)
        refuse_overlong_body(body)
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

    def _sent_ending(self, window: ComposeWindow, files: list[Path]) -> dict[str, Any]:
        """What a send Mail accepted returns, by the copy it filed in
        Sent (``_find_sent_copy``): found, it must carry every file the
        composition pasted, by name, and its ids are returned; not
        identified, no id is returned, with a warning saying why
        (``sent_result``)."""
        copy = self._find_sent_copy(window.subject, window.before_ids)
        if isinstance(copy, SentMessageCopy):
            check_sent_attachments(copy, [f.name for f in files])
        else:
            logger.warning("a send's copy in Sent was not identified: %s", copy.why)
        return sent_result(copy, files_pasted=bool(files))

    def _find_sent_copy(
        self, subject: str, before_ids: list[int]
    ) -> SentMessageCopy | SentMessageCopyUnidentified:
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
                    return SentMessageCopyUnidentified(
                        f"{len(new)} messages with its subject reached Sent "
                        "while it was composed and sent, and which is its "
                        "copy cannot be told"
                    )
        except (MailError, ValueError) as exc:
            return SentMessageCopyUnidentified(f"looking for it in Sent failed: {exc}")
        waited = self._SENT_APPEAR_POLLS * self._SENT_APPEAR_INTERVAL_S
        return SentMessageCopyUnidentified(
            f"no new message with its subject appeared in Sent within {waited:g}s"
        )

    def _sent_ids_with_subject(self, subject: str) -> list[int]:
        """Mail's ids for every message in Sent (every account's) whose
        subject is ``subject``: ids only, so a look stays cheap however
        often the subject was used before."""
        subject_safe = escape_applescript_string(subject)
        raw = self._run_applescript(
            wrap_as_json_script(
                f"""
tell application "Mail"
    set resultData to (id of every message of sent mailbox whose subject is "{subject_safe}")
end tell
""",
                timeout=self.timeout,
            )
        )
        return [int(i) for i in cast(list[Any], parse_applescript_json(raw))]

    def _read_sent_copy(self, mail_id: int) -> SentMessageCopy:
        """The message in Sent with Mail's id ``mail_id``: its RFC
        Message-ID (bare) and the names of its files."""
        raw = self._run_applescript(
            wrap_as_json_script(
                f"""
tell application "Mail"
    set m to first message of sent mailbox whose id is "{mail_id}"
    set rfcId to message id of m
    if rfcId is missing value then set rfcId to ""
    set attNames to name of every mail attachment of m
    if attNames is missing value then set attNames to {{}}
    set resultData to {{|id|:(id of m as text), |message_id|:rfcId, |attachment_names|:attNames}}
end tell
""",
                timeout=self.timeout,
            )
        )
        record = cast(dict[str, Any], parse_applescript_json(raw))
        return SentMessageCopy(
            mail_id=str(record["id"]),
            rfc_message_id=str(record["message_id"]).strip().strip("<>"),
            attachment_names=tuple(str(n) for n in record["attachment_names"]),
        )

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
        validate_compose_args(seed, seed_id, to, subject)
        refuse_overlong_body(body)

        # HARD POLICY GATE — the actual-send enforcement perimeter for the
        # outbound recipient allowlist. Any code path that reaches this
        # method with send_now=True (create_draft tool, update_draft
        # delete-and-recreate, future direct callers) is checked here. If
        # any recipient is off-list, MailOutboundDisallowedError is raised
        # BEFORE any AppleScript is built or executed. See
        # outbound_allowlist.py for the policy. DO NOT add a bypass
        # without explicit human authorization.
        if send_now:
            assert_recipients_allowed_for_send(to, cc, bcc, seed=seed, reply_all=reply_all)

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
             not see (``ENDING_MAILBOX``).

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
        derives none names someone (``gate_named_recipients``,
        ``MailOutboundDisallowedError``), here as well as at every
        caller, since the connector is the hard block on a path by which
        mail leaves; every attachment exists (``FileNotFoundError``); and
        ``from_account`` names an account (``MailAccountNotFoundError``).
        The body's length was refused where it entered the connector
        (``refuse_overlong_body``). A later failure salvages the window
        to Drafts and raises ``MailAppleScriptError``, which says so when
        the message was in fact sent.

        The window is recorded in the compose ledger when it opens
        (``_open_compose``), and how it ended when the composition ends:
        sent, saved as a draft, salvaged, discarded, gone, or left open
        with the failure (``_window_lifecycle``). A window left open is
        tending's to close (``tend_compose_windows``).
        """
        if send_now:
            gate_named_recipients(seed, to, cc, bcc)
        files = existing_files(attachment_paths)
        sender = self._resolve_account_to_sender(from_account) if from_account is not None else None
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
                    gate_compose_recipients(window)
                except MailOutboundDisallowedError as exc:
                    end_window(
                        composition_closing(
                            self._discard_compose_window(window.name, window.window_id),
                            failure=f"recipients refused: {exc}",
                        )
                    )
                    raise
            self._fill_compose(window, seed=seed, body=body, plain=plain, files=files)
            if send_now:
                self._send_compose_window(window)
                end_window(Closed(how="sent", by="composition", at=time.time()))
                return self._sent_ending(window, files)
            try:
                draft_id = self._save_compose_window_as_draft(window)
            except MailDraftNotSettledError:
                # Closed with Save; the draft it became is not yet listed.
                end_window(Closed(how="salvaged", by="composition", at=time.time()))
                raise
            end_window(Closed(how="saved", by="composition", at=time.time(), draft_id=draft_id))
            return {"draft_id": draft_id, "sent_message_id": ""}

    @contextmanager
    def _window_lifecycle(
        self, window: ComposeWindow
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
                _end(composition_closing(exc.window_outcome, failure=str(exc)))
            raise
        except BaseException as exc:
            if not ended:
                _end(LeftOpen(failure=f"{type(exc).__name__}: {exc}", at=time.time()))
            raise

    def _record_window_end(self, window: ComposeWindow, state: LeftOpen | Closed) -> None:
        """Write how a composition's window ended. A window left open
        wakes tending (``on_window_left_open``). The mail has already been
        sent, saved or refused when this runs, so a ledger that cannot be
        written is reported in the log, not raised."""
        if window.record_id is not None:
            try:
                self.compose_ledger.end(window.record_id, state)
            except (OSError, MailComposeLedgerError) as exc:
                logger.error(
                    "compose ledger: could not record how window %r ended (%s): %s",
                    window.name,
                    state,
                    exc,
                )
        if isinstance(state, LeftOpen) and self.on_window_left_open is not None:
            self.on_window_left_open()

    def _fill_compose(
        self,
        window: ComposeWindow,
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
                window=window,
                body=body,
                placement="replace",
                plain=plain,
            )
        elif body:
            self._paste_verified(
                window=window,
                body=body,
                placement="above",
                plain=plain,
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
                raise MailMessageNotFoundError(f"no message with id {seed_id!r}") from e
            raise

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
    ) -> ComposeWindow:
        """Open the compose window ``build_open_compose_script``
        describes, record it in the compose ledger, and return what the
        script reported; a seed that is gone is
        ``MailMessageNotFoundError``. A failure once the window existed
        records it as left open and raises ``MailAppleScriptError``.

        A window can also open without the script reporting it: Mail
        opened it more than 5 s after it was asked (NO_COMPOSE_WINDOW),
        the script was stopped at the timeout, or its report could not be
        read. Mail's windows are read before the script for that case,
        and on any such failure every compose window that opened since is
        recorded as left open (``_adopt_unreported_windows``), so tending
        closes it on its next pass; the error raised says so."""
        snapshot = self._mail_window_snapshot()
        script = build_open_compose_script(
            seed=seed,
            seed_id=seed_id,
            reply_all=reply_all,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            sender=sender,
            operation=operation,
        )
        try:
            raw = self._run_seeded_script(
                wrap_as_json_script(script, timeout=self.timeout), seed_id
            )
            report = cast(dict[str, Any], parse_applescript_json(raw))
            window = compose_window_from_report(report)
        except MailMessageNotFoundError:
            raise  # SEED_NOT_FOUND: raised before any window is asked for
        except (MailAppleScriptError, ValueError, KeyError, TypeError) as exc:
            adopted = self._adopt_unreported_windows(
                snapshot,
                expected_name=sanitize_input(subject) if seed == "new" and subject else None,
                operation=operation,
                seed=cast(Seed, seed),
                failure=f"{type(exc).__name__}: {exc}",
            )
            note = f"{adopted} left open, recorded for tending" if adopted else "none found open"
            if type(exc) in (MailAppleScriptError, MailTimeoutError):
                raise type(exc)(f"{exc} (compose window: {note})") from exc
            raise MailAppleScriptError(
                f"compose window report unreadable: {exc} (compose window: {note})"
            ) from exc
        window = replace(
            window,
            record_id=self._record_window_open(window, operation=operation, seed=cast(Seed, seed)),
        )
        failure = str(report.get("failure") or "")
        if failure:
            self._record_window_end(window, LeftOpen(failure=failure, at=time.time()))
            raise MailAppleScriptError(f"{failure} (compose window: left open)")
        return window

    def _record_window_open(
        self, window: ComposeWindow, *, operation: WindowOperation, seed: Seed
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
            logger.error("compose ledger: could not record window %r: %s", window.name, exc)
            return None

    def _mail_window_snapshot(self) -> WindowSnapshot:
        """Mail's process id and the ids of all its windows, read before a
        composition opens its window. Opens nothing."""
        raw = self._run_applescript(
            wrap_as_json_script(
                """
tell application "System Events"
    tell application process "Mail"
        set mailPid to unix id
    end tell
end tell
tell application "Mail"
    set resultData to {|pid|:mailPid, |ids|:(id of every window)}
end tell
""",
                timeout=self.timeout,
            )
        )
        report = cast(dict[str, Any], parse_applescript_json(raw))
        return WindowSnapshot(
            mail_pid=int(report["pid"]),
            window_ids=frozenset(int(i) for i in report.get("ids") or []),
        )

    def _build_adopt_script(self, snapshot: WindowSnapshot, expected_name: str | None) -> str:
        """Full osascript source: poll, for up to ``_ADOPT_WAIT_S``, for
        compose windows of the same Mail process whose ids were not in
        ``snapshot`` (and, when ``expected_name`` is given, of that name),
        each tied to System Events' list by its name and position and
        checked for a Send button. Returns them as ``found``. Read-only."""
        ids = ", ".join(str(i) for i in sorted(snapshot.window_ids))
        expected = (
            f'"{escape_applescript_string(expected_name)}"' if expected_name is not None else '""'
        )
        polls = max(1, round(self._ADOPT_WAIT_S / self._ADOPT_POLL_S))
        body = f"""
set beforeIds to {{{ids}}}
set expectedName to {expected}
set found to {{}}
set samePid to true
repeat {polls} times
    tell application "System Events"
        tell application process "Mail"
            set pidNow to unix id
        end tell
    end tell
    if pidNow is not {snapshot.mail_pid} then
        set samePid to false
        exit repeat
    end if
    tell application "Mail"
        set allIds to id of every window
        set allNames to name of every window
        set allBounds to bounds of every window
        set allVisible to visible of every window
    end tell
    repeat with j from 1 to (count of allIds)
        set wid to item j of allIds
        set nm to item j of allNames
        if wid is not in beforeIds and (item j of allVisible) and (expectedName is "" or nm is expectedName) then
            set b to item j of allBounds
            set bPos to {{item 1 of b, item 2 of b}}
            set idx to my tendIndexOf(nm, bPos)
            if idx > 0 then
                if my tendIsCompose(idx) then set end of found to {{|id|:wid, |name|:nm}}
            end if
        end if
    end repeat
    if (count of found) > 0 then exit repeat
    delay {self._ADOPT_POLL_S}
end repeat
tell application "Mail"
    set resultData to {{|same_pid|:samePid, |found|:found}}
end tell
"""
        return wrap_as_json_script(body, timeout=self.timeout) + WINDOW_HANDLERS

    def _adopt_unreported_windows(
        self,
        snapshot: WindowSnapshot,
        *,
        expected_name: str | None,
        operation: WindowOperation,
        seed: Seed,
        failure: str,
    ) -> int:
        """Record, as left open with ``failure``, each compose window that
        opened since ``snapshot`` while an opening script failed without
        reporting one (``_build_adopt_script``), so tending closes it at
        its next pass and nothing is lost: a window with anything in it
        is salvaged. A window some unfinished ledger record already
        names is another composition's, and is left to it. Returns how
        many were recorded; a look that fails is logged and counts none,
        since the composition is failing either way."""
        try:
            raw = self._run_applescript(self._build_adopt_script(snapshot, expected_name))
            report = cast(dict[str, Any], parse_applescript_json(raw))
        except (MailAppleScriptError, ValueError) as exc:
            logger.error("could not look for a compose window left unreported: %s", exc)
            return 0
        claimed = {
            r.window_id
            for r in self.compose_ledger.read_all().records
            if r.unfinished and r.mail_pid == snapshot.mail_pid
        }
        adopted = 0
        for found in report.get("found") or []:
            window_id = int(found["id"])
            if window_id in claimed:
                continue
            window = ComposeWindow(
                name=str(found["name"]),
                subject="",
                to=[],
                cc=[],
                bcc=[],
                before_ids=[],
                window_id=window_id,
                mail_pid=snapshot.mail_pid,
            )
            window = replace(
                window,
                record_id=self._record_window_open(window, operation=operation, seed=seed),
            )
            self._record_window_end(window, LeftOpen(failure=failure, at=time.time()))
            adopted += 1
        return adopted

    def _save_compose_window_as_draft(self, window: ComposeWindow) -> str:
        """Close the compose window with Save, then find the draft it
        became: the Drafts entry with the window's subject that was not
        among the ids Drafts held before it opened, polled for and then
        given the settle time a new draft needs before Mail acts on it."""
        outcome = self._salvage(window)
        if outcome != "SALVAGED":
            raise MailComposeWindowError(f"draft save: {outcome}", window_outcome=outcome)
        ids = ", ".join(str(i) for i in window.before_ids)
        subject_safe = escape_applescript_string(window.subject)
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
            f'"{escape_applescript_string(str(p.resolve()))}"' for p in target_paths
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
