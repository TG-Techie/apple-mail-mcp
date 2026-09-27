"""
Security utilities for Apple Mail MCP.
"""

import json
import logging
import os
import subprocess
import time
from collections import deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# The audit file rolls over once it reaches this size; one previous
# generation (``audit.jsonl.1``) is kept, so the store on disk is bounded
# at about twice this. Entries run a few hundred bytes, so a cap of 5 MiB
# is on the order of ten thousand operations per generation.
AUDIT_ROTATE_BYTES = 5 * 1024 * 1024


def audit_log_path() -> Path:
    """Where the durable audit log lives: ``audit.jsonl`` under the data
    home (``APPLE_MAIL_MCP_HOME``, default ``~/.apple_mail_mcp``).
    Resolved at call time so env-var overrides and test-time
    monkeypatching are honoured.

    The file is a record of who the user corresponds with and about
    what. It is personal data at rest: it lives outside the repository
    and is never committed, never pasted into a message, and not
    something an agent reads to answer a question about someone else's
    mail."""
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = Path(home_override).expanduser() if home_override else Path.home() / ".apple_mail_mcp"
    return base / "audit.jsonl"


class OperationLogger:
    """Log operations for audit trail.

    Every entry is kept in memory for the life of the process and
    appended, as one JSON line, to ``audit_log_path()``. The file is the
    record that outlives the process: a session may run its own server or
    share the resident daemon (``mail-serve``) with every other session,
    and either way nothing else on disk says what the server did.
    """

    def __init__(self) -> None:
        self.operations: list[dict[str, Any]] = []

    def log_operation(
        self, operation: str, parameters: dict[str, Any], result: str = "success"
    ) -> None:
        """
        Log an operation with timestamp.

        Args:
            operation: Operation name
            parameters: Operation parameters
            result: Result status (success/failure/cancelled)
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "parameters": parameters,
            "result": result,
        }
        self.operations.append(entry)
        self._append_to_file(entry)
        logger.info(f"Operation logged: {operation} - {result}")

    @staticmethod
    def _append_to_file(entry: dict[str, Any]) -> None:
        """Append one line, rolling the file over at ``AUDIT_ROTATE_BYTES``.
        A log that cannot be written is reported, not raised, because the
        operation it records has already happened."""
        path = audit_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size >= AUDIT_ROTATE_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("audit log not written to %s: %s", path, e)

    def get_recent_operations(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get recent operations.

        Args:
            limit: Maximum number of operations to return

        Returns:
            List of recent operations
        """
        return self.operations[-limit:]


# Global operation logger instance
operation_logger = OperationLogger()


def validate_bulk_operation(item_count: int, max_items: int = 100) -> tuple[bool, str]:
    """
    Validate bulk operation limits.

    Args:
        item_count: Number of items in operation
        max_items: Maximum allowed items

    Returns:
        Tuple of (is_valid, error_message)
    """
    if item_count == 0:
        return False, "No items specified for operation"

    if item_count > max_items:
        return False, f"Too many items ({item_count}), maximum is {max_items}"

    return True, ""


TIER_LIMITS: dict[str, tuple[int, float]] = {
    "cheap_reads": (60, 60.0),
    "expensive_ops": (20, 60.0),
    "sends": (3, 60.0),
}

# What the server does to Mail of its own accord, not on a tool call: each
# is logged under its name and has a rate-limit tier, like a tool.
#
# tend_compose_windows (tender.py): the daemon's pass over Mail's compose
# windows, which closes those the compose ledger says this connector
# opened and nothing closed. It changes Mail's state (a window closed, a
# draft saved), so it is in the tier of the other mutations. Test mode
# does not confine it: it is no tool a test calls, it names no account,
# and what it closes loses nothing — a salvage to Drafts, or a discard of
# a window with nothing in it.
INTERNAL_OPERATIONS: frozenset[str] = frozenset({"tend_compose_windows"})

# The rate-limit tier of each operation, keyed by its name: one entry per
# registered tool and per internal operation, and no other.
OPERATION_TIERS: dict[str, str] = {
    "list_accounts": "cheap_reads",
    "list_rules": "cheap_reads",
    "list_mailboxes": "cheap_reads",
    "get_messages": "cheap_reads",
    "get_thread": "cheap_reads",
    "save_attachments": "cheap_reads",
    "search_messages": "expensive_ops",
    "update_message": "expensive_ops",
    "create_mailbox": "expensive_ops",
    "update_mailbox": "expensive_ops",
    "delete_mailbox": "expensive_ops",
    "delete_messages": "expensive_ops",
    "delete_rule": "expensive_ops",
    "create_rule": "expensive_ops",
    "update_rule": "expensive_ops",
    # Drafts. draft_create and draft_update write a draft into an account
    # through Mail, a compose window and its attachments; draft_delete
    # removes one. They change Mail's state like the mutations above.
    # draft_send is the one send from a draft.
    "draft_create": "expensive_ops",
    "draft_update": "expensive_ops",
    "draft_delete": "expensive_ops",
    "draft_send": "sends",
    "email_send_html": "sends",
    # Email templates (#30): local files under the data home, except that
    # render_template also reads the message it fills variables from.
    "list_templates": "cheap_reads",
    "get_template": "cheap_reads",
    "save_template": "cheap_reads",
    "delete_template": "cheap_reads",
    "render_template": "cheap_reads",
    # Internal operations.
    "tend_compose_windows": "expensive_ops",
}


class RateLimiter:
    """Sliding-window rate limiter with per-tier tracking."""

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {t: deque() for t in TIER_LIMITS}

    def check(self, tier: str) -> bool:
        """Return True if allowed, False if rate-limited."""
        now = time.monotonic()
        max_calls, window = TIER_LIMITS[tier]
        q = self._windows[tier]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= max_calls:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        """Clear all tier windows."""
        for q in self._windows.values():
            q.clear()


rate_limiter = RateLimiter()


def check_rate_limit(operation: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """
    Check rate limit for an operation. Returns None if allowed,
    or a structured error dict if rate-limited.
    """
    tier = OPERATION_TIERS[operation]
    if rate_limiter.check(tier):
        return None
    operation_logger.log_operation(operation, params, "rate_limited")
    max_calls, window = TIER_LIMITS[tier]
    return {
        "success": False,
        "error": f"Rate limit exceeded: {max_calls} calls per {int(window)}s for {tier} operations",
        "error_type": "rate_limited",
    }


def validate_attachment_type(filename: str, allow_executables: bool = False) -> bool:
    """
    Validate attachment file type for security.

    Args:
        filename: Name of the attachment file
        allow_executables: Whether to allow executable files (default: False)

    Returns:
        True if file type is allowed, False otherwise

    Example:
        >>> validate_attachment_type("document.pdf")
        True
        >>> validate_attachment_type("malware.exe")
        False
    """
    # Dangerous executable extensions (block by default)
    dangerous_extensions = {
        '.exe', '.bat', '.cmd', '.com', '.scr', '.pif',
        '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh',
        '.msi', '.msp', '.scf', '.lnk', '.inf', '.reg',
        '.ps1', '.psm1', '.app', '.deb', '.rpm', '.sh',
        '.bash', '.csh', '.ksh', '.zsh', '.command'
    }

    filename_lower = filename.lower()

    # Check for dangerous extensions
    for ext in dangerous_extensions:
        if filename_lower.endswith(ext):
            return allow_executables

    # All other types are allowed
    return True


def validate_attachment_size(size_bytes: int, max_size: int = 25 * 1024 * 1024) -> bool:
    """
    Validate attachment file size.

    Args:
        size_bytes: Size of file in bytes
        max_size: Maximum allowed size in bytes (default: 25MB)

    Returns:
        True if within limit, False otherwise

    Example:
        >>> validate_attachment_size(1024 * 1024)  # 1MB
        True
        >>> validate_attachment_size(30 * 1024 * 1024)  # 30MB
        False
    """
    return size_bytes <= max_size


# ---------------------------------------------------------------------------
# Test-mode safety system (MAIL_TEST_MODE)
# ---------------------------------------------------------------------------

RESERVED_TEST_DOMAINS = {"example.com", "example.net", "example.org"}
RESERVED_TEST_TLDS = {".example", ".test", ".invalid", ".localhost"}

# The loopback: one real address an integration run may send to, from
# which the mail arrives back in the test account's INBOX, so a test can
# read what was delivered instead of trusting the send. Reserved domains
# receive nothing. Named by MAIL_TEST_LOOPBACK, admitted for sends only.
TEST_LOOPBACK_ENV = "MAIL_TEST_LOOPBACK"

# Operations that take an account and, in test mode, may only target
# MAIL_TEST_ACCOUNT. Every account-scoped mutation belongs here: the gate
# is what keeps an integration run off a real account, and a mutation
# missing from this set is one the server can call the gate for and get
# a no-op back (update_mailbox and delete_mailbox were exactly that).
ACCOUNT_GATED_OPERATIONS = {
    "list_mailboxes",
    "search_messages",
    "update_message",
    "delete_messages",
    "create_mailbox",
    "update_mailbox",
    "delete_mailbox",
    # The sender the caller names (from_account): the account a draft is
    # saved into, or mail is sent under. A sender left to Mail's default
    # is not confined here — the fresh send path cannot name one.
    "draft_create",
    "email_send_html",
    # The account the draft named by id sits in, which each of these
    # reads back from Mail before acting. draft_update also passes the
    # sender it is asked to move the draft to.
    "draft_update",
    "draft_delete",
    "draft_send",
}

# Of those, the ones that change mail and can be called without naming an
# account: they take message ids, which are global across accounts, so an
# account of None reaches every account. Under test mode they must name
# the test account; the reads may search everywhere.
ACCOUNT_REQUIRED_MUTATIONS = {
    "update_message",
    "delete_messages",
    # A draft id likewise names a draft in any account. The tools read the
    # draft's account back from Mail and pass it; one Mail cannot name (a
    # local draft) is not the test account.
    "draft_update",
    "draft_delete",
    "draft_send",
}

# Every operation that delivers mail; every call to one sends. In test
# mode each is confined to RFC 2606 reserved domains and the loopback
# address, and must pass every recipient explicitly: draft_send those it
# reads back from the draft, email_send_html those it was given. A tool
# added later that sends belongs here, or test mode never sees where its
# mail goes.
SEND_OPERATIONS = {
    "draft_send",
    "email_send_html",
}

# Rule-mutation operations: in test mode, may only target rules whose
# names start with the test prefix below. Protects the user's real rules
# during integration testing.
RULE_GATED_OPERATIONS = {
    "create_rule",
    "update_rule",
    "delete_rule",
}

RULE_TEST_PREFIX = "[apple-mail-mcp-test]"


def _is_test_mode_enabled() -> bool:
    return os.environ.get("MAIL_TEST_MODE", "").lower() == "true"


def _get_test_account() -> str | None:
    return os.environ.get("MAIL_TEST_ACCOUNT")


def _get_test_loopback() -> str | None:
    """The address MAIL_TEST_LOOPBACK names, or None when it is unset or
    blank. Read at call time, like the other test-mode variables."""
    return os.environ.get(TEST_LOOPBACK_ENV, "").strip() or None


@lru_cache(maxsize=4)
def _get_test_account_identifiers(test_account_name: str) -> frozenset[str]:
    """Return the set of identifiers (name + UUID) that match the test account.

    The test account is configured by name via MAIL_TEST_ACCOUNT, but per #61
    callers may pass either the name or the UUID to account-gated tools.
    Returns both so the safety gate accepts either form.

    Cached per process, keyed by the test-account name. Tests can clear the
    cache with ``_get_test_account_identifiers.cache_clear()``. If the UUID
    lookup fails (account doesn't exist, AppleScript permission denied),
    falls back to name-only matching with a warning — degraded mode that
    still enforces the test-account boundary by name.
    """
    identifiers: set[str] = {test_account_name}
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application "Mail" to return id of account '
                f'"{test_account_name}"',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            "Test-mode safety gate: failed to resolve UUID for account %r "
            "(%s); falling back to name-only matching",
            test_account_name, exc,
        )
        return frozenset(identifiers)

    if result.returncode == 0:
        uuid = result.stdout.strip()
        if uuid:
            identifiers.add(uuid)
    else:
        logger.warning(
            "Test-mode safety gate: failed to resolve UUID for account %r "
            "(exit %d): %s; falling back to name-only matching",
            test_account_name, result.returncode, result.stderr.strip(),
        )
    return frozenset(identifiers)


def _is_reserved_test_domain(email: str) -> bool:
    """True if email's domain is an RFC 2606 reserved test domain."""
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    if domain in RESERVED_TEST_DOMAINS:
        return True
    for tld in RESERVED_TEST_TLDS:
        bare = tld.lstrip(".")
        if domain == bare or domain.endswith(tld):
            return True
    return False


def _is_admitted_send_recipient(email: str, loopback: str | None) -> bool:
    """True if test mode lets a send reach ``email``: an RFC 2606
    reserved domain, or exactly the loopback address (whole address,
    any case)."""
    if loopback is not None and email.lower() == loopback.lower():
        return True
    return _is_reserved_test_domain(email)


def _safety_error(operation: str, message: str) -> dict[str, Any]:
    operation_logger.log_operation(
        operation, {"violation": message}, "safety_violation"
    )
    return {
        "success": False,
        "error": message,
        "error_type": "safety_violation",
    }


def check_test_mode_safety(
    operation: str,
    account: str | None = None,
    recipients: list[str] | None = None,
    rule_name: str | None = None,
) -> dict[str, Any] | None:
    """
    Enforce test-mode safety checks. Returns None if allowed (or no test mode),
    or a structured error dict on safety violation.

    In test mode (MAIL_TEST_MODE=true):
    - Account-gated operations must target MAIL_TEST_ACCOUNT.
    - delete_messages and update_message must name the test account;
      with no account they would act on message ids from any account.
      draft_update, draft_delete and draft_send likewise: the tools pass
      the account the draft was found in, and a draft with none is
      refused.
    - Send operations must send only to RFC 2606 reserved domains or to
      the one address MAIL_TEST_LOOPBACK names, and must name every
      recipient in ``recipients``. Every call to one sends, so a call
      with no recipients is refused, whether it passes an empty list (a
      send whose recipients Mail would derive) or None (which leaves
      this gate nothing to check).
    - Rule-mutation operations must target rules whose names start with
      RULE_TEST_PREFIX (protects the user's real rules during integration
      testing), and a rule that forwards may forward only to RFC 2606
      reserved domains, never to the loopback: its ``forward_to`` is a
      send that repeats for every matching message, passed here as
      ``recipients``.
    """
    if not _is_test_mode_enabled():
        return None

    if operation in ACCOUNT_REQUIRED_MUTATIONS and account is None:
        test_account = _get_test_account()
        return _safety_error(
            operation,
            f"Test mode: {operation} must name account="
            f"{test_account!r} (MAIL_TEST_ACCOUNT); message ids reach every "
            "account, so without it the operation is not confined to the "
            "test account.",
        )

    # Account-gated operations: verify target account matches MAIL_TEST_ACCOUNT
    # by either name or UUID (per #61, account-gated tools accept both forms).
    if operation in ACCOUNT_GATED_OPERATIONS and account is not None:
        test_account = _get_test_account()
        if test_account is None:
            return _safety_error(
                operation,
                "MAIL_TEST_MODE is set but MAIL_TEST_ACCOUNT is not",
            )
        if account not in _get_test_account_identifiers(test_account):
            return _safety_error(
                operation,
                f"Test mode: account '{account}' does not match "
                f"MAIL_TEST_ACCOUNT='{test_account}'",
            )

    # Rule-mutation operations: verify the target rule's name starts with
    # the test prefix. The caller (server tool wrapper) is responsible for
    # resolving rule_index → rule_name before calling, since the safety gate
    # has no Mail.app access of its own.
    if operation in RULE_GATED_OPERATIONS and rule_name is not None:
        if not rule_name.startswith(RULE_TEST_PREFIX):
            return _safety_error(
                operation,
                f"Test mode: rule mutations are restricted to rules whose "
                f"name starts with {RULE_TEST_PREFIX!r}. Got rule_name="
                f"{rule_name!r}.",
            )

    # Send operations: verify every recipient is one test mode admits.
    if operation in SEND_OPERATIONS:
        # #175: a send with no explicit recipients (a reply left to Mail)
        # has them derived at send time, past this gate, so test mode
        # requires them explicit; with none passed at all there is nothing
        # here to check.
        if not recipients:
            return _safety_error(
                operation,
                f"Test mode: {operation} requires explicit recipients for "
                f"send (implicit-reply targets cannot be safety-verified "
                f"before send).",
            )
        return _send_recipient_violation(operation, recipients)

    # A rule that forwards sends to its targets on every match. Most rules
    # forward nothing, so an empty list is the ordinary case here, not the
    # derived-recipient hazard it is for a send.
    if operation in RULE_GATED_OPERATIONS and recipients:
        return _forward_to_violation(operation, recipients)

    return None


_RESERVED_DOMAINS_TEXT = (
    "RFC 2606 reserved domains (example.com/.test/.invalid/etc.)"
)


def _send_recipient_violation(
    operation: str, recipients: list[str]
) -> dict[str, Any] | None:
    """The safety error for any send recipient test mode does not admit,
    or None when it admits every one. The error names the refused
    recipients and says how the loopback stands."""
    loopback = _get_test_loopback()
    bad = [r for r in recipients if not _is_admitted_send_recipient(r, loopback)]
    if not bad:
        return None
    if loopback is None:
        admitted = f"; set {TEST_LOOPBACK_ENV} to admit one real address"
    else:
        admitted = f" or the {TEST_LOOPBACK_ENV} address {loopback}"
    return _safety_error(
        operation,
        f"Test mode: recipients must use {_RESERVED_DOMAINS_TEXT}"
        f"{admitted}. Violations: {', '.join(bad)}",
    )


def _forward_to_violation(
    operation: str, recipients: list[str]
) -> dict[str, Any] | None:
    """The safety error for any rule ``forward_to`` target off the RFC
    2606 reserved domains, or None when every one is on them.

    The loopback is deliberately not admitted here. A send reaches it
    once, when a test sends; a rule forwards every message it matches,
    unattended, for as long as it exists, and a test rule left behind
    by a failed run would keep forwarding to a person's address.
    """
    bad = [r for r in recipients if not _is_reserved_test_domain(r)]
    if not bad:
        return None
    return _safety_error(
        operation,
        f"Test mode: a rule's forward_to must use {_RESERVED_DOMAINS_TEXT}; "
        f"{TEST_LOOPBACK_ENV} does not apply to rules. "
        f"Violations: {', '.join(bad)}",
    )
