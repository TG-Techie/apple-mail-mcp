"""``email_send_html``, and the send gates it shares with ``draft_send``:
the outbound allowlist, the user's confirmation and the summary it is
asked on, and the checks on files to attach.
"""

import logging
from pathlib import Path
from typing import Any

from fastmcp import Context

from .. import server
from ..exceptions import OutboundAllowlistUnavailableError
from ..outbound_allowlist import all_recipients_allowed, disallowed_recipients
from ..security import (
    check_rate_limit,
    check_test_mode_safety,
    operation_logger,
    validate_attachment_size,
    validate_attachment_type,
)
from ..server import _confirm_from_threadpool, _in_tool_threadpool, envelope, mcp

logger = logging.getLogger(__name__)


def validate_attachment_files(attachment_paths: list[str]) -> None:
    """Validate files the caller asks to attach, before anything is composed.

    Checks per the security checklist: file exists, extension not in the
    executable blocklist, size within the 25MB cap. Raises
    ``FileNotFoundError`` or ``ValueError`` on the first file that fails.
    Every path that takes attachment paths from the caller runs this —
    email_send_html, draft_create and draft_update alike — so a draft
    cannot carry what a send would refuse. Attachments a rebuild carries
    over from the existing draft are Mail's state, not caller input, and
    are not re-checked here.
    """
    for raw in attachment_paths:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"attachment not found: {raw}")
        if not validate_attachment_type(path.name):
            raise ValueError(
                f"attachment type not allowed: {path.name} "
                "(executable extensions are blocked)"
            )
        if not validate_attachment_size(path.stat().st_size):
            raise ValueError(
                f"attachment too large: {path.name} exceeds the 25MB limit"
            )


def build_send_summary(
    seed_kind: str,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str | None,
    body: str,
) -> str:
    """Confirmation summary for a send (draft_send, email_send_html)."""
    verb = {"reply": "Send this reply?", "forward": "Forward this message?"}.get(
        seed_kind, "Send this email?"
    )
    lines: list[str] = []
    if to:
        lines.append(f"To: {', '.join(to)}")
    if cc:
        lines.append(f"CC: {', '.join(cc)}")
    if bcc:
        lines.append(f"BCC: {', '.join(bcc)}")
    if subject:
        lines.append(f"Subject: {subject}")
    if body:
        preview = body[:200] + "..." if len(body) > 200 else body
        lines.append(f"\n{preview}")
    return verb + "\n\n" + "\n".join(lines)


def outbound_refusal(
    operation: str, recipients: list[str]
) -> dict[str, Any] | None:
    """The outbound allowlist gate for a send, at this layer: the refusal
    for these recipients, or None to go on. draft_send and email_send_html
    both ask it before Mail is touched, so an off-list send gets its
    typed error from the tool and nothing changes, a saved draft
    included. The connector checks the final recipients again at
    dispatch (``assert_recipients_allowed_for_send``, which also sees the
    ones Mail derives for a reply); that is the backstop, not the gate.

    An allowlist that cannot be read refuses every send (fail closed) as
    ``allowlist_unavailable``, told apart from ``outbound_disallowed`` so
    "fix the comms config" is not read as "edit the recipients".
    """
    unsent = " Nothing was sent; the draft, if any, is unchanged."
    try:
        bad = disallowed_recipients(recipients)
    except OutboundAllowlistUnavailableError as e:
        logger.error("%s blocked — allowlist unavailable: %s", operation, e)
        return {
            "success": False,
            "error": f"{e}{unsent}",
            "error_type": "allowlist_unavailable",
        }
    if not bad:
        return None
    logger.warning("%s blocked — off-list recipients: %s", operation, bad)
    return {
        "success": False,
        "error": (
            "send blocked — recipients not on outbound allowlist: "
            + ", ".join(repr(b) for b in bad)
            + "."
            + unsent
        ),
        "error_type": "outbound_disallowed",
    }


def confirm_send(
    ctx: Context | None,
    operation: str,
    recipients: list[str],
    summary: str,
    elicit_extra: dict[str, Any],
) -> dict[str, Any] | None:
    """Ask the user to confirm a send, unless every recipient is on the
    outbound allowlist (see outbound_allowlist.py). The bypass lets
    clients without elicitation support (e.g. Cowork) send to
    pre-trusted addresses. It is only that: ``outbound_refusal`` has
    already refused any off-list recipient the caller named, and the
    connector checks the final recipients again at dispatch, so a send
    to an off-list address is blocked whatever happens here. What is
    left to confirm is a reply whose recipients Mail derives.

    Returns the refusal (declined, or no way to ask), or None to send.
    """
    if all_recipients_allowed(recipients):
        operation_logger.log_operation(
            operation, {**elicit_extra, "recipients": recipients}, "send_allowlisted",
        )
        return None
    return _confirm_from_threadpool(ctx, summary, operation, elicit_extra)


def sent_fields(result: dict[str, Any]) -> dict[str, Any]:
    """What a send tool returns of the connector's result for a send that
    went out: the Mail id (``sent_message_id``) and bare RFC Message-ID
    (``sent_rfc_message_id``) of the copy it filed in Sent, and, when
    that copy could not be identified, both ``""`` and the ``warnings``
    saying why. ``draft_id`` is ``""``: a send keeps no draft."""
    fields: dict[str, Any] = {
        "draft_id": result.get("draft_id", ""),
        "sent_message_id": result.get("sent_message_id", ""),
        "sent_rfc_message_id": result.get("sent_rfc_message_id", ""),
    }
    if warnings := result.get("warnings"):
        fields["warnings"] = list(warnings)
    return fields


def _html_send_seed(
    *,
    to: list[str],
    subject: str,
    reply_to: str | None,
    forward_of: str | None,
    attachment_paths: list[str],
) -> str:
    """Which message an email_send_html call composes: ``"new"``,
    ``"reply"`` or ``"forward"``, checked, before anything is composed,
    for what that message needs. A fresh message needs its recipients and
    subject. A reply has Mail derive both. A forward has Mail derive its
    subject but no recipient, so it names them. Every one takes files,
    which get the send-path file checks. Anything else raises ValueError.
    """
    if reply_to is not None and forward_of is not None:
        raise ValueError(
            "email_send_html: reply_to and forward_of are mutually exclusive"
        )
    seed = (
        "reply" if reply_to is not None
        else "forward" if forward_of is not None
        else "new"
    )
    if seed != "reply" and not to:
        raise ValueError(
            "email_send_html: 'to' is required unless reply_to is given"
        )
    if seed == "new" and not subject:
        raise ValueError(
            "email_send_html: 'subject' is required unless reply_to or "
            "forward_of is given"
        )
    if attachment_paths:
        validate_attachment_files(attachment_paths)
    return seed


@_in_tool_threadpool
@mcp.tool()
@envelope
def email_send_html(
    to: list[str] = [],  # noqa: B006 — coerced below
    subject: str = "",
    body: str = "",
    cc: list[str] = [],  # noqa: B006 — coerced below
    bcc: list[str] = [],  # noqa: B006 — coerced below
    from_account: str | None = None,
    reply_to: str | None = None,
    forward_of: str | None = None,
    attachment_paths: list[str] = [],  # noqa: B006 — coerced below
    ctx: Context | None = None,
) -> dict:
    """Send an HTML email directly. Does not save a draft first.

    This is the PREFERRED send tool — use it for fresh mail, replies and
    forwards unless a human wants to review the draft in Mail.app first
    (then use ``draft_create`` + ``draft_send``).

    Body must be an HTML string. The email is composed via clipboard injection
    into Mail.app's rich-text compose window and sent immediately, with
    mechanical verification of dispatch: a success result means it went out.

    **Fresh mail** (default): ``to`` and ``subject`` are required.

    **Reply into a thread**: pass ``reply_to=<message_id>`` (Mail internal or
    RFC 5322 id — use the LATEST message of the thread, e.g. from
    ``get_thread``). Mail carries the threading headers; ``subject`` defaults
    to the derived "Re: …" and ``to`` defaults to Mail's derived reply
    recipients. The pasted HTML lands ABOVE the auto-quoted original.

    **Forward**: pass ``forward_of=<message_id>`` (same id forms) and ``to``,
    which is required: Mail derives no recipient for a forward. ``subject``
    defaults to Mail's "Fwd: …". The HTML lands ABOVE Mail's forwarded
    message, which keeps its header block and the original's attachments.
    Never forward by pasting a message into a fresh one: that is not a
    forward.

    **Reply-all**: there is no reply_all flag. Fetch the thread participants
    (``get_thread`` / ``get_messages``) and pass them explicitly via
    ``to``/``cc`` — every recipient is validated against the outbound
    allowlist, with no exceptions; one off-list participant blocks the whole
    send (the compose window is discarded, nothing partial is sent).

    Args:
        to: Recipient email addresses. Optional when ``reply_to`` is given
            (Mail derives them; explicit values REPLACE the derived set).
        subject: Email subject line. Optional with ``reply_to`` or
            ``forward_of``.
        body: HTML string for the email body.
        cc: Optional CC recipients (replace derived CC when replying).
        bcc: Optional BCC recipients.
        attachment_paths: Optional file paths to attach, in every mode;
            on a reply or forward they go after Mail's quote or forwarded
            message. Files must exist, must not carry executable
            extensions, and must be under 25MB each. Each must be visible
            in the compose window before Send is clicked, and the copy
            this send filed in Sent is checked for every file by name.
        from_account: Mail.app account name or UUID. None uses Mail's
            default. Set as the sender of the message composed, in every
            mode.
        reply_to: Message id to reply to. Enables reply mode.
        forward_of: Message id to forward. Enables forward mode; mutually
            exclusive with ``reply_to``.

    Returns:
        ``{"success": True, "draft_id": "", "sent_message_id": <Mail id>,
        "sent_rfc_message_id": <Message-ID>}``: the copy this send filed
        in Sent, which ``get_messages`` reads by that id. When that copy
        could not be identified (not in Sent within 30 s, say), both ids
        are ``""`` and ``warnings`` says why; the message was still sent,
        so look in Sent before sending it again.
    """
    cc_list = cc or []
    bcc_list = bcc or []
    attachment_paths = attachment_paths or []
    all_recipients = list(to) + list(cc_list) + list(bcc_list)

    seed = _html_send_seed(
        to=to, subject=subject, reply_to=reply_to, forward_of=forward_of,
        attachment_paths=attachment_paths,
    )
    # A reply that names no recipients leaves the list empty here: Mail
    # derives them, and the connector checks those at dispatch.
    if refused := outbound_refusal("email_send_html", all_recipients):
        return refused
    # The account the send goes out under, as far as the caller names it.
    if refused := check_test_mode_safety(
        "email_send_html", account=from_account, recipients=all_recipients,
    ):
        return refused
    if refused := check_rate_limit("email_send_html", {"subject": subject, "to": to}):
        return refused
    summary = build_send_summary(
        seed, to, cc_list or None, bcc_list or None, subject, body,
    )
    if refused := confirm_send(
        ctx, "email_send_html", all_recipients, summary,
        {"subject": subject, "to": to},
    ):
        return refused

    result = server.mail._send_html_email(
        to=to,
        cc=cc_list or None,
        bcc=bcc_list or None,
        subject=subject,
        body=body,
        from_account=from_account,
        reply_to=reply_to,
        forward_of=forward_of,
        attachment_paths=[Path(a) for a in attachment_paths] or None,
    )
    operation_logger.log_operation(
        "email_send_html",
        {
            "to": to,
            "cc": cc_list,
            "bcc": bcc_list,
            "subject": subject,
            "from_account": from_account,
            "reply_to": reply_to,
            "forward_of": forward_of,
        },
        "success",
    )
    return {"success": True, **sent_fields(result)}
