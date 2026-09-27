"""Draft tools: ``draft_create``, ``draft_update``, ``draft_delete`` and
``draft_send``. (``apple_mail_mcp.drafts`` is the store of what this
server records about each draft; these are the tools.)

Correct create → send pattern (agents: follow this exactly).

Minimum lifecycle — 2 calls::

    draft_create(...)              → {"draft_id": "ABCD"}
    draft_send(draft_id="ABCD")    → {"sent_message_id": "WXYZ"}

With optional refinement (revise the draft before sending)::

    draft_create(...)              → {"draft_id": "ABCD"}
    draft_update(draft_id="ABCD",  → {"draft_id": "EFGH"}   # id CHANGES
                 body="revised")
    draft_send(draft_id="EFGH")    → {"sent_message_id": "WXYZ"}

``sent_message_id`` is the Mail id of the copy the send filed in Sent,
beside its ``sent_rfc_message_id``. Both are ``""``, with a
``warnings`` entry saying why, when that copy could not be identified;
Mail accepted the message either way.

Sending is ALWAYS a separate call. There is no auto-send. The split
exists so the policy gate (outbound recipient allowlist) sits at a
single, obvious tool — draft_send — making it auditable and reviewable.

Off-allowlist recipients are fine on saved drafts (steps 1–2). They are
only blocked at step 3. A blocked draft_send leaves the draft intact so
the human can review and either edit the recipients or send manually
from Mail.app.

IMPORTANT: draft_update is implemented as recreate-then-delete, so the
returned draft_id is a NEW id. Always use the returned id for the next
step in the lifecycle. Treat the id you held before draft_update as
stale. The old draft is removed only after the new one exists (or the
send went out), so a failed draft_update or draft_send leaves it in
Drafts under the id you already hold.
"""

import dataclasses
import logging
import tempfile
from pathlib import Path
from typing import Any, cast

from fastmcp import Context

from .. import server
from ..drafts import DraftStateStore, SeedRecord
from ..exceptions import MailAppleScriptError, MailDraftError, MailDraftNotFoundError
from ..security import check_rate_limit, check_test_mode_safety, operation_logger
from ..server import _in_tool_threadpool, envelope, mcp
from . import send, templates

logger = logging.getLogger(__name__)


def _get_draft_state_store() -> DraftStateStore:
    """Return the active DraftStateStore. Re-resolved per call so the
    APPLE_MAIL_MCP_HOME env var (and test-time monkeypatching) take
    effect at use time, not import time, as ``templates.get_template_store``
    does."""
    return DraftStateStore()


def _draft_account(state: dict[str, Any]) -> str | None:
    """The account a draft sits in, as Mail reads it back; None when Mail
    cannot name one (a local draft)."""
    return cast(str, state.get("account") or "") or None


def _draft_sender(state: dict[str, Any]) -> str | None:
    """The sender a draft was saved with, as Mail reads it back, or None.

    A rebuilt draft is built with it unless the caller names another, so
    an update or a send does not silently move the draft to Mail's
    default account (the connector resolves the address to its account).
    """
    return cast(str, state.get("sender") or "") or None


@dataclasses.dataclass(frozen=True)
class _DraftSource:
    """What a saved draft is built again from: the seed it was made from,
    and the caller's own part of it — its text, and the names of the
    files it attached — which is all a rebuild hands back to Mail."""

    seed_kind: str
    seed_id: str | None
    reply_all: bool
    body: str
    attachment_names: list[str]


def _resolve_draft_source(
    draft_id: str,
    state: dict[str, Any],
    store: DraftStateStore,
) -> _DraftSource:
    """How to rebuild a saved draft.

    Lookup order: persisted disk state first (fast); In-Reply-To header
    fallback for externally-created reply drafts (slow); fresh seed
    if neither yields anything.

    The connector's reply and forward verbs write the quoted original,
    and a forward carries the original's attachments, so a reply or
    forward is rebuilt from the caller's own part only, which the record
    keeps. What Mail reads back already has Mail's part: handing its
    body back sent the original twice, once unquoted (measured on a
    forward created without a body and then sent), and its attachments
    include the forwarded ones. A draft with no record of the caller's
    part (created outside this server, or recorded before it was kept)
    falls back to everything Mail reads back, with a warning. A fresh
    draft has nothing of Mail's in it, so what Mail reads back is all
    the caller's.
    """
    read_back_body = cast(str, state.get("body") or "")
    read_back_names = list(state.get("attachment_names") or [])
    record = store.get_seed(draft_id)
    if record:
        if record.body is not None and record.attachment_names is not None:
            return _DraftSource(
                record.seed_kind, record.seed_id, record.reply_all,
                record.body, list(record.attachment_names),
            )
        logger.warning(
            "draft %s has no record of what its caller wrote and attached; "
            "rebuilding it from what Mail reads back, which repeats the "
            "quoted original and a forward's attachments",
            draft_id,
        )
        return _DraftSource(
            record.seed_kind, record.seed_id, record.reply_all,
            read_back_body, read_back_names,
        )

    in_reply_to = state.get("in_reply_to") or ""
    if in_reply_to:
        logger.warning(
            "falling back to In-Reply-To lookup for externally-created "
            "draft %s — this may take 30s+ on large mailboxes",
            draft_id,
        )
        resolved = server.mail.find_message_by_message_id(in_reply_to)
        if resolved:
            return _DraftSource("reply", resolved, False, read_back_body, read_back_names)

    return _DraftSource("new", None, False, read_back_body, read_back_names)


def _persist_draft_source(draft_id: str, source: _DraftSource) -> None:
    """Record a reply or forward draft's seed, and the caller's own part
    of it, under its id, so draft_update and draft_send can rebuild it
    without an O(N) header lookup and without handing Mail back what it
    wrote itself (see ``_resolve_draft_source``). Called by draft_create,
    and by draft_update under the new id. No-op for a failed create
    (empty draft_id) or a seed kind without an anchor message.
    (#191/#192)
    """
    if (
        not draft_id
        or source.seed_kind not in ("reply", "forward")
        or not source.seed_id
    ):
        return
    _get_draft_state_store().set_seed(
        draft_id,
        SeedRecord(
            seed_kind=cast(Any, source.seed_kind),
            seed_id=source.seed_id,
            reply_all=source.reply_all,
            body=source.body,
            attachment_names=tuple(source.attachment_names),
        ),
    )


def _retire_old_draft(
    draft_id: str,
    store: DraftStateStore,
    *,
    new_draft_id: str,
    sent: bool,
) -> str | None:
    """Remove the draft that draft_update has just replaced or draft_send
    has just sent.

    Runs only once the new message exists, so nothing here can lose the
    caller's draft. A removal that fails is reported, not raised: the
    outcome the caller asked for holds, and the old id stays usable
    until they deal with it. Returns the text to surface as a warning,
    or None when the old draft is gone as intended.

    A draft saved through Mail's scripting dictionary with a named
    sender left its compose session open and was re-saved from it
    seconds later, so retiring it early left a copy in Drafts
    (docs/research/draft-resave-spike.md, Observation 6). The
    connector saves every draft from a window closed with Save, which
    leaves no session behind, and two updates of a named-sender draft
    saved that way left no copy (the same note, Observation 11). The
    copy can still come from a draft saved the old way, by this server
    before 2026-09-27 or by another client, retired within its re-save
    window.
    """
    try:
        server.mail.delete_draft(draft_id)
    except MailDraftNotFoundError:
        store.delete(draft_id)
        return (
            f"the old draft {draft_id!r} was already gone when its removal "
            "was attempted; the new state is as requested."
        )
    except MailAppleScriptError as e:
        logger.error("old draft %r not removed after update: %s", draft_id, e)
        outcome = (
            "the message was sent"
            if sent
            else f"the new draft {new_draft_id!r} was saved"
        )
        return (
            f"{outcome}, but the old draft {draft_id!r} could not be removed "
            f"and is still in Drafts: {e}. Delete it with draft_delete."
        )
    store.delete(draft_id)
    return None


def _resolve_draft_attachments(
    draft_id: str,
    attachment_paths: list[str] | None,
    listed: list[str],
    carried: list[str],
) -> tuple[list[Path] | None, "tempfile.TemporaryDirectory[str] | None"]:
    """Compute final attachment paths for a rebuilt draft.

    Semantics:
        - ``attachment_paths == [...]`` → replace with caller-supplied list.
        - ``attachment_paths == []`` → explicitly clear.
        - ``attachment_paths is None`` → carry over the draft's own
          attachments named in ``carried`` (the caller's; see
          ``_resolve_draft_source``), extracted to a temp dir the caller
          must clean up; None when there are none.

    The connector saves a draft's attachments by position, not by name,
    so every attachment Mail ``listed`` is saved out and only the carried
    ones are kept. One that cannot be (Mail no longer lists it under that
    name, or saving it failed) raises ``MailDraftError`` rather than
    rebuilding the draft without it.

    Returns ``(final_paths, tempdir_to_clean_up)``.
    """
    if attachment_paths is not None:
        return [Path(p) for p in attachment_paths], None
    if not carried:
        return None, None

    tempdir = tempfile.TemporaryDirectory(prefix="amm-update-attach-")
    extracted = server.mail.extract_draft_attachments(draft_id, listed, Path(tempdir.name))
    missing = list(carried)
    kept: list[Path] = []
    for path in extracted:
        if path.name in missing:
            missing.remove(path.name)
            kept.append(path)
    if missing:
        tempdir.cleanup()
        raise MailDraftError(
            f"could not carry {missing} over from draft {draft_id!r}: Mail "
            f"lists its attachments as {listed}. Nothing was changed."
        )
    return kept, tempdir


def _rebuild_draft(
    draft_id: str,
    attachment_paths: list[str] | None,
    listed: list[str],
    carried: list[str],
    **compose: Any,
) -> dict[str, Any]:
    """Build a saved draft again through the connector's create_draft:
    saved, for draft_update, or sent, for draft_send. Mail forbids
    changing a saved draft, so this is how both act on one. ``compose``
    is the rest of create_draft's arguments.

    The old draft is left where it is: the caller holds its id, so it
    goes (``_retire_old_draft``) only once this has succeeded, and a
    failure here leaves it in Drafts. Attachments carried over from it
    are extracted to a temporary directory for the rebuild, which is
    removed whatever happens.
    """
    attachments, tempdir = _resolve_draft_attachments(
        draft_id, attachment_paths, listed, carried
    )
    try:
        return server.mail.create_draft(attachment_paths=attachments, **compose)
    finally:
        if tempdir is not None:
            tempdir.cleanup()


def _resolve_draft_create_seed(
    reply_to: str | None,
    forward_of: str | None,
) -> tuple[str, str | None]:
    """Resolve (seed_kind, seed_id) from draft_create's reply_to/forward_of
    params. Param-shape validation (reply_to AND forward_of both set) is
    caller's responsibility; this helper assumes valid input. (#191)
    """
    if reply_to:
        return "reply", reply_to
    if forward_of:
        return "forward", forward_of
    return "new", None


def _maybe_apply_template(
    template_name: str | None,
    template_vars: dict[str, str] | None,
    seed_id: str | None,
    subject: str | None,
    body: str,
) -> tuple[str | None, str]:
    """If template_name is set, load it and merge into (subject, body).
    Caller-supplied values override the rendered output. Pass-through
    when template_name is None. Raises MailTemplateError on bad
    templates. (#191)
    """
    if not template_name:
        return subject, body
    template = templates.get_template_store().get(template_name)
    auto_vars = server.mail.auto_template_vars(seed_id)
    merged_vars: dict[str, str] = {**auto_vars, **(template_vars or {})}
    rendered = template.render(merged_vars)
    if subject is None:
        subject = rendered["subject"]
    if not body:
        body = rendered["body"] or ""
    return subject, body


def _validate_fresh_seed_fields(
    seed_kind: str,
    to: list[str],
    subject: str | None,
) -> None:
    """For seed_kind=='new', require both `to` and `subject` (post-template
    rendering). Raises ValueError for the first one missing. (#191)
    """
    if seed_kind != "new":
        return
    if not to:
        raise ValueError("'to' is required when not replying or forwarding")
    if not subject:
        raise ValueError("'subject' is required when not replying or forwarding")


def _resolve_update_subject_body(
    subject: str | None,
    body: str | None,
    template_name: str | None,
    template_vars: dict[str, str] | None,
    seed_id: str | None,
    current_subject: str | None,
    current_body: str,
) -> tuple[str | None, str]:
    """Three-tier resolution for draft_update's subject + body:
    caller-supplied > template-rendered > the draft's current values (its
    subject as Mail reads it back; its body as ``_resolve_draft_source``
    resolved it).

    Differs from draft_create's `_maybe_apply_template`: update treats
    `body=""` as a deliberate clear (preserved through the chain), while
    create treats `not body` as "fall through to template". Raises
    MailTemplateError on bad templates. (#192)
    """
    merged_subject = subject
    merged_body = body
    if template_name:
        template = templates.get_template_store().get(template_name)
        auto_vars = server.mail.auto_template_vars(seed_id)
        merged_vars: dict[str, str] = {**auto_vars, **(template_vars or {})}
        rendered = template.render(merged_vars)
        if merged_subject is None:
            merged_subject = rendered["subject"]
        if merged_body is None:
            merged_body = rendered["body"] or ""
    final_subject = merged_subject if merged_subject is not None else current_subject
    final_body = merged_body if merged_body is not None else current_body
    return final_subject, final_body


def _merge_draft_recipients(
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    state: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Merge caller-supplied recipient lists with existing draft state.
    None = keep existing state value; [] = clear; list = replace. (#192)
    """
    return (
        to if to is not None else state.get("to", []),
        cc if cc is not None else state.get("cc", []),
        bcc if bcc is not None else state.get("bcc", []),
    )


def _gate_draft_update_accounts(
    draft_account: str | None, from_account: str | None
) -> dict[str, Any] | None:
    """The test-mode account gate for draft_update: the draft's own
    account, which the update deletes from and, absent an override,
    recreates in, and the override when the caller gave one. A draft
    whose account Mail could not name is passed as None and refused in
    test mode. Returns the first gate's error, or None.
    """
    touched = [draft_account]
    if from_account is not None:
        touched.append(from_account)
    for account in touched:
        safety_err = check_test_mode_safety("draft_update", account=account)
        if safety_err:
            return safety_err
    return None


@mcp.tool()
@envelope
def draft_create(
    reply_to: str | None = None,
    forward_of: str | None = None,
    to: list[str] = [],  # noqa: B006 — coerced to None below
    cc: list[str] = [],  # noqa: B006 — coerced to None below
    bcc: list[str] = [],  # noqa: B006 — coerced to None below
    subject: str | None = None,
    body: str = "",
    attachment_paths: list[str] = [],  # noqa: B006 — coerced to None below
    reply_all: bool = False,
    template_name: str | None = None,
    template_vars: dict[str, str] | None = None,
    from_account: str | None = None,
) -> dict[str, Any]:
    """Create a draft (fresh, reply, or forward). DOES NOT SEND.

    To actually send, call ``draft_send(draft_id)`` as a separate step
    after reviewing/editing the draft. The split is intentional —
    sending requires its own explicit action, and the outbound allowlist
    policy is enforced there.

    Modes (driven by ``reply_to`` / ``forward_of``):
      - Neither: fresh draft (``to`` and ``subject`` required).
      - ``reply_to=<message_id>``: reply draft. Mail.app auto-derives
        recipients and subject prefix unless overridden.
      - ``forward_of=<message_id>``: forward draft. Recipients default
        to empty (user must specify); subject prefixed with ``Fwd:``.

    Args:
        reply_to: Message id to reply to: Mail's internal id or an RFC
            5322 Message-ID, as any ``search_messages`` / ``get_messages``
            row gives it. Mutually exclusive with ``forward_of``.
        forward_of: Message id to forward, in the same forms. Mutually
            exclusive with ``reply_to``.
        to/cc/bcc: Recipient lists. For reply/forward, empty list keeps
            Mail's auto-derived recipients; populated list replaces.
        subject: Subject line. Required for fresh drafts; optional for
            reply/forward (None keeps Mail's auto-derived prefix).
        body: Body text. For reply/forward, a non-empty body goes above
            Mail's quoted original or forwarded message, which stays with
            its header block and attachments.
        attachment_paths: File paths to attach. Each must exist, must
            not carry an executable extension, and must be under 25MB.
        reply_all: For ``reply_to`` only — use Mail's reply-all logic.
        template_name / template_vars: Optional template render.
            Caller-supplied ``subject``/``body`` override the rendered
            output; ``template_vars`` override auto-fills.
        from_account: Mail.app account name or UUID. None uses Mail's
            default sender for the seed message. A saved draft keeps it.

    Returns:
        ``{"success": True, "draft_id": "<id>"}`` on success.

    Example (full lifecycle):
        >>> r = draft_create(to=["alice@example.com"],
        ...                  subject="hi", body="hello")
        >>> r["draft_id"]
        'ABCD'
        >>> draft_send(draft_id="ABCD")
        {"success": True, "sent_message_id": "161300", ...}
    """
    if refused := check_rate_limit("draft_create", {"subject": subject, "to": to}):
        return refused
    if reply_to and forward_of:
        raise ValueError("reply_to and forward_of are mutually exclusive")
    if template_vars and not template_name:
        raise ValueError("template_vars requires template_name")
    # A named sender is an account this call writes into: the draft lands
    # in its Drafts, so in test mode it must be the test account.
    if refused := check_test_mode_safety("draft_create", account=from_account):
        return refused

    seed_kind, seed_id = _resolve_draft_create_seed(reply_to, forward_of)
    subject, body = _maybe_apply_template(
        template_name, template_vars, seed_id, subject, body,
    )
    # After rendering, so a template can supply the subject.
    _validate_fresh_seed_fields(seed_kind, to, subject)
    if attachment_paths:
        send.validate_attachment_files(attachment_paths)

    result = server.mail.create_draft(
        seed=seed_kind,
        seed_id=seed_id,
        to=to or None,
        cc=cc or None,
        bcc=bcc or None,
        subject=subject,
        body=body,
        attachment_paths=[Path(p) for p in attachment_paths] or None,
        reply_all=reply_all,
        from_account=from_account,
    )
    draft_id = result.get("draft_id", "")
    _persist_draft_source(
        draft_id,
        _DraftSource(
            seed_kind, seed_id, reply_all, body,
            [Path(p).name for p in attachment_paths],
        ),
    )
    operation_logger.log_operation(
        "draft_create",
        {
            "seed_kind": seed_kind,
            "seed_id": seed_id,
            "draft_id": draft_id,
            "to": to or None,
            "cc": cc or None,
            "bcc": bcc or None,
            "subject": subject,
            "from_account": from_account,
        },
        "success",
    )
    return {
        "success": True,
        "draft_id": draft_id,
        "sent_message_id": result.get("sent_message_id", ""),
        "details": {"seed_kind": seed_kind, "send_now": False},
    }


@mcp.tool()
@envelope
def draft_update(
    draft_id: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    attachment_paths: list[str] | None = None,
    template_name: str | None = None,
    template_vars: dict[str, str] | None = None,
    from_account: str | None = None,
) -> dict[str, Any]:
    """Update an existing draft. DOES NOT SEND.

    Patch semantics: only fields you pass change. ``None`` means "keep
    existing"; empty list/string means "clear". To actually send, call
    ``draft_send(draft_id)`` afterwards.

    IMPORTANT: Mail.app forbids mutating saved drafts, so this is
    implemented as recreate-then-delete. The returned ``draft_id`` is a
    NEW id — use it for any subsequent ``draft_update`` or ``draft_send``
    call. The id you passed in is stale after this call returns. The old
    draft is removed only after the new one exists, so a failure leaves
    it in Drafts under the id you passed; a removal that fails after
    that is reported as a ``warning`` beside the success.

    Args:
        draft_id: Existing draft to update.
        to/cc/bcc: Recipient overrides (None=keep, []=clear, list=replace).
        subject: Subject override. None=keep.
        body: Body override. None=keep; empty string=clear. On a reply or
            forward the text kept is your own, never the original Mail
            quotes below it.
        attachment_paths: Attachment override (None=keep, []=clear,
            list=replace). On a reply or forward, None keeps the files
            you attached; a forward's own come with Mail's forward. A
            replacement list is checked like a send (exists, no
            executable extension, under 25MB) before the existing draft
            is touched.
        template_name / template_vars: Optional template render.
        from_account: Sender override. None keeps the draft's own sender.

    Returns:
        ``{"success": True, "draft_id": "<NEW_ID>"}``. The id is new.

    Example:
        >>> r = draft_update(draft_id="ABCD", body="revised text")
        >>> r["draft_id"]   # different from "ABCD"!
        'EFGH'
    """
    if refused := check_rate_limit("draft_update", {"draft_id": draft_id}):
        return refused
    if template_vars and not template_name:
        raise ValueError("template_vars requires template_name")
    state = server.mail.get_draft_state(draft_id)
    # A draft id names a draft in any account; the state read says
    # which, and the update must stay in the test account.
    if refused := _gate_draft_update_accounts(_draft_account(state), from_account):
        return refused

    store = _get_draft_state_store()
    source = _resolve_draft_source(draft_id, state, store)
    # A replacement list is caller input, checked before anything is
    # touched; None (carry over) and [] (clear) hand in no files.
    if attachment_paths:
        send.validate_attachment_files(attachment_paths)
    final_subject, final_body = _resolve_update_subject_body(
        subject, body, template_name, template_vars, source.seed_id,
        state.get("subject"), source.body,
    )
    final_to, final_cc, final_bcc = _merge_draft_recipients(to, cc, bcc, state)
    final_from = from_account if from_account is not None else _draft_sender(state)

    result = _rebuild_draft(
        draft_id, attachment_paths,
        list(state.get("attachment_names") or []), source.attachment_names,
        seed=source.seed_kind,
        seed_id=source.seed_id,
        reply_all=source.reply_all,
        to=final_to,
        cc=final_cc,
        bcc=final_bcc,
        subject=final_subject,
        body=final_body,
        from_account=final_from,
    )
    new_draft_id = result.get("draft_id", "")
    _persist_draft_source(
        new_draft_id,
        dataclasses.replace(
            source,
            body=final_body,
            attachment_names=(
                source.attachment_names
                if attachment_paths is None
                else [Path(p).name for p in attachment_paths]
            ),
        ),
    )
    warning = _retire_old_draft(
        draft_id, store, new_draft_id=new_draft_id, sent=False,
    )
    operation_logger.log_operation(
        "draft_update",
        {
            "old_draft_id": draft_id,
            "new_draft_id": new_draft_id,
            "old_draft_removed": warning is None,
            "to": final_to,
            "cc": final_cc,
            "bcc": final_bcc,
            "subject": final_subject,
            "from_account": final_from,
        },
        "success",
    )
    response: dict[str, Any] = {
        "success": True,
        "draft_id": new_draft_id,
        "sent_message_id": result.get("sent_message_id", ""),
        "details": {"seed_kind": source.seed_kind, "send_now": False},
    }
    if warning is not None:
        response["warning"] = warning
    return response


@mcp.tool()
@envelope
def draft_delete(draft_id: str) -> dict[str, Any]:
    """Delete (move to Trash) an existing draft. No send, no recovery
    expected — Mail.app moves the draft to Deleted Messages and no longer
    treats it as editable. No confirmation (recoverable from Trash). It
    drives Mail, so it is rate-limited with the other mutations.

    Args:
        draft_id: Existing draft to delete.

    Returns:
        ``{"success": True, "draft_id": "<id>"}`` on a clean delete; an
        error response if the draft does not exist.
    """
    if refused := check_rate_limit("draft_delete", {"draft_id": draft_id}):
        return refused
    # A draft id names a draft in any account; read which before
    # acting, so test mode can keep the delete in the test account.
    state = server.mail.get_draft_state(draft_id)
    if refused := check_test_mode_safety("draft_delete", account=_draft_account(state)):
        return refused
    server.mail.delete_draft(draft_id)
    _get_draft_state_store().delete(draft_id)
    operation_logger.log_operation(
        "draft_delete", {"draft_id": draft_id, "account": state.get("account")},
        "success",
    )
    return {"success": True, "draft_id": draft_id}


@_in_tool_threadpool
@mcp.tool()
@envelope
def draft_send(
    draft_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Send an existing draft. The one send from a draft.

    Hard policy gate: every recipient (to/cc/bcc) on the draft must
    match the outbound allowlist (see outbound_allowlist.py). If any
    recipient is off-list, the send is blocked and the draft is left
    INTACT for human review — you can edit it via ``draft_update`` or
    open Mail.app and send/discard manually.

    Off-list recipients are detected BEFORE any destructive operation,
    so a blocked ``draft_send`` is a pure no-op on Mail.app state. The
    draft is rebuilt and sent, then removed; on a reply or forward only
    the caller's own text and attachments are handed back to Mail, as
    ``draft_update`` keeps them.

    Args:
        draft_id: Id of the saved draft to send.
        ctx: MCP context, supplied by the server; used to ask the user
            to confirm a send the allowlist does not already cover.

    Returns:
        On success: ``{"success": True, "draft_id": "", "sent_message_id":
        <Mail id>, "sent_rfc_message_id": <Message-ID>}``, the copy the
        send filed in Sent, which ``get_messages`` reads by that id. When
        that copy could not be identified (not in Sent within 30 s, say),
        both ids are ``""`` and ``warnings`` says why; Mail still accepted
        the message, so look in Sent and in Mail's Outbox before sending
        it again. An old draft that could not be removed is named in
        ``warning``.

        On policy block:
        ``{"success": False, "error": "...", "error_type":
        "outbound_disallowed"}`` — draft is unchanged.

    Example:
        >>> draft_send(draft_id="EFGH")
        {"success": True, "draft_id": "", "sent_message_id": "161300", ...}
    """
    state = server.mail.get_draft_state(draft_id)
    to, cc, bcc = (list(state.get(group) or []) for group in ("to", "cc", "bcc"))
    recipients = to + cc + bcc
    if not recipients:
        raise ValueError(
            "draft_send: draft has no recipients. Add recipients via "
            "draft_update before sending."
        )
    subject: str | None = state.get("subject")
    if refused := check_test_mode_safety(
        "draft_send", account=_draft_account(state), recipients=recipients,
    ):
        return refused
    if refused := check_rate_limit(
        "draft_send", {"draft_id": draft_id, "subject": subject}
    ):
        return refused
    if refused := send.outbound_refusal("draft_send", recipients):
        return refused

    store = _get_draft_state_store()
    source = _resolve_draft_source(draft_id, state, store)
    if refused := send.confirm_send(
        ctx, "draft_send", recipients,
        send.build_send_summary(source.seed_kind, to, cc, bcc, subject, source.body),
        {"draft_id": draft_id},
    ):
        return refused

    result = _rebuild_draft(
        draft_id, None,
        list(state.get("attachment_names") or []), source.attachment_names,
        seed=source.seed_kind,
        seed_id=source.seed_id,
        reply_all=source.reply_all,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=source.body,
        from_account=_draft_sender(state),
        send_now=True,
    )
    warning = _retire_old_draft(draft_id, store, new_draft_id="", sent=True)
    operation_logger.log_operation(
        "draft_send",
        {
            "old_draft_id": draft_id,
            "old_draft_removed": warning is None,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
        },
        "success",
    )
    response: dict[str, Any] = {
        "success": True,
        **send.sent_fields(result),
        "details": {"seed_kind": source.seed_kind, "send_now": True},
    }
    if warning is not None:
        response["warning"] = warning
    return response
