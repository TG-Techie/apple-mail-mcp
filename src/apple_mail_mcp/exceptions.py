"""
Custom exceptions for Apple Mail MCP operations.
"""


class MailError(Exception):
    """Base exception for Mail operations."""

    pass


class MailOutboundDisallowedError(MailError):
    """Outbound send blocked: one or more recipients are not on the
    outbound allowlist (comms config YAML → ``email.allowed_outbound``,
    path via ``APPLE_MAIL_MCP_COMMS_CONFIG``).

    Raised by ``outbound_allowlist.assert_recipients_allowed_for_send``
    at the actual-send call site in ``mail_connector.create_draft`` —
    the policy enforcement perimeter for outbound mail.
    """

    pass


class OutboundAllowlistUnavailableError(MailOutboundDisallowedError):
    """The outbound allowlist POLICY ITSELF could not be read: the comms
    config YAML is missing, unreadable, unparseable, or malformed.

    FAIL CLOSED (owner directive, 2026-08-24): no sends while the policy
    is unreadable — there is no hardcoded fallback list. Distinct from
    the parent so callers can tell "recipient off-list" (edit recipients)
    from "config broken" (fix the comms.yaml) — but still a subclass, so
    every existing policy-gate handler catches it.

    Carve-out: under ``MAIL_TEST_MODE=true`` the recipient-checking
    functions tolerate an unavailable config and allow only RFC 2606
    reserved test domains, so the integration harness works on machines
    with no comms.yaml.
    """

    pass


class MailAccountNotFoundError(MailError):
    """Account does not exist."""

    pass


class MailMailboxNotFoundError(MailError):
    """Mailbox does not exist."""

    pass


class MailMailboxNotEmptyError(MailError):
    """Mailbox cannot be deleted because it contains messages and the
    caller did not opt in to cascade-delete via ``delete_messages=True``."""

    pass


class MailUnsupportedGmailSystemLabelError(MailError):
    """Operation targets a Gmail system label (the ``[Gmail]`` parent or
    any ``[Gmail]/...`` child path).

    Gmail's IMAP server does not support normal RENAME/DELETE semantics
    for these paths — renames may silently revert and deletes are
    refused. Tracked in #164; future Gmail-label-CRUD tools (sub-feature
    2 of #164) will provide a proper alternative.
    """

    pass


class MailImapRequiredError(MailError):
    """The requested operation requires IMAP credentials and the user
    hasn't opted in (no Keychain entry, or entry is unreachable). Surfaces
    the gap so the caller can prompt the user to set up IMAP if they want
    the operation."""

    pass


class MailImapMoveUnsupportedError(MailError):
    """The IMAP server advertises neither MOVE (RFC 6851) nor UIDPLUS
    (RFC 4315). No safe scoped move is possible; the orchestrator must
    fall back to AppleScript. A non-UIDPLUS unscoped EXPUNGE would
    remove every \\Deleted-flagged message in the mailbox, not just the
    ones we just moved."""

    pass


class MailImapTrashNotFoundError(MailError):
    """The IMAP server doesn't advertise a \\Trash SPECIAL-USE folder
    (RFC 6154) and no folder matching the conventional names (Trash,
    [Gmail]/Trash, Deleted Messages, Deleted Items) was found. Without
    a Trash folder we can't preserve the move-to-Trash semantic of
    delete_messages — fall back to AppleScript."""

    pass


class MailMessageNotFoundError(MailError):
    """Message does not exist."""

    pass


class MailAppleScriptError(MailError):
    """AppleScript execution failed."""

    pass


class MailPermissionError(MailError):
    """Permission denied for operation."""

    pass


class MailOperationCancelledError(MailError):
    """User cancelled the operation."""

    pass


class MailSafetyError(MailError):
    """Safety check failed in test mode (wrong account or non-reserved recipient)."""

    pass


class MailKeychainError(MailError):
    """Keychain operation failed."""

    pass


class MailKeychainEntryNotFoundError(MailKeychainError):
    """Requested Keychain entry does not exist.

    Expected and benign: signals the user has not opted in to IMAP
    for this account. Delegation layer (future work) treats this as
    a silent fall-back-to-AppleScript signal.
    """

    pass


class MailKeychainAccessDeniedError(MailKeychainError):
    """Keychain refused access (ACL denied or user denied prompt).

    Worth surfacing to the user on first failure per the graceful-
    degradation invariants in imap-auth-options-decision.md.
    """

    pass


class MailRuleNotFoundError(MailError):
    """Rule index is out of range — no such rule exists in Mail.app."""

    pass


class MailUnsupportedRuleActionError(MailError):
    """update_rule was called on a rule whose existing actions include
    one that's not modeled in our schema (e.g. run-AppleScript,
    redirect, reply, play sound). Read access via list_rules is
    unaffected; only mutating an existing rule with these actions is
    refused.
    """

    pass


class MailDraftError(MailError):
    """Base class for draft-lifecycle errors."""

    pass


class MailDraftInvalidIdError(MailDraftError):
    """Draft id failed validation (path traversal, invalid chars, too long,
    or empty). Ids must match ^[a-zA-Z0-9_-]{1,128}$."""

    pass


class MailDraftNotFoundError(MailDraftError):
    """No draft exists with the requested id (lookup across Drafts mailboxes
    of every account returned nothing)."""

    pass


class MailTemplateError(MailError):
    """Base class for email-template errors."""

    pass


class MailTemplateNotFoundError(MailTemplateError):
    """No template exists with the requested name."""

    pass


class MailTemplateInvalidNameError(MailTemplateError):
    """Template name fails validation (path traversal, invalid chars,
    too long, or empty). Names must match ^[a-zA-Z0-9_-]{1,64}$."""

    pass


class MailTemplateInvalidFormatError(MailTemplateError):
    """A file in the templates directory could not be parsed as a
    template (malformed header, unreadable, or empty body)."""

    pass


class MailTemplateMissingVariableError(MailTemplateError):
    """render_template encountered a {placeholder} with no matching
    auto-fill or user-supplied variable. The exception message names
    the missing placeholder(s)."""

    pass
