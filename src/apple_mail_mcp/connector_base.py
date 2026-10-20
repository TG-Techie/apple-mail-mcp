"""The connector's state and its way of talking to Mail: the timeouts, the
compose ledger and clock, the IMAP pool and breaker state, the Mail lock,
the runner every script goes through, and the account lookups."""

import logging
import subprocess
from collections.abc import Callable
from email.utils import parseaddr
from typing import IO, Any, cast

from . import mail_lock
from .compose_clock import ComposeClock
from .compose_ledger import ComposeLedger
from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailTimeoutError,
)
from .imap_connector import ImapConnectionPool
from .script_blocks import wrap_as_json_script
from .utils import parse_applescript_json

# One logger for the connector however its files are divided: the name
# operators and the tests filter its records on.
logger = logging.getLogger("apple_mail_mcp.mail_connector")


class ConnectorBase:
    """What every part of the connector stands on."""

    def __init__(
        self,
        timeout: int = 60,
        *,
        imap_pool: ImapConnectionPool | None = None,
        lock_timeout: float = 30.0,
        compose_ledger: ComposeLedger | None = None,
        compose_clock: ComposeClock | None = None,
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
            compose_clock: Where tending keeps, between passes, when it
                first saw each compose window with its present content.
                Default: the file beside the compose ledger, resolved on
                each use.
        """
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.compose_ledger = compose_ledger if compose_ledger is not None else ComposeLedger()
        self.compose_clock = compose_clock if compose_clock is not None else ComposeClock()
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
            if isinstance(
                e,
                (
                    MailAccountNotFoundError,
                    MailMailboxNotFoundError,
                    MailMessageNotFoundError,
                    MailAppleScriptError,
                ),
            ):
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

        script = wrap_as_json_script(tell_body, timeout=self.timeout)
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
            owns_address = wanted_address and any(str(e).lower() == wanted_address for e in emails)
            if acc.get("id") == account or acc.get("name") == account or owns_address:
                if not emails:
                    raise ValueError(f"Account {account!r} has no email addresses configured.")
                email = cast(str, emails[0])
                full_name = (acc.get("full_name") or "").strip()
                if full_name:
                    return f"{full_name} <{email}>"
                return email
        raise MailAccountNotFoundError(
            f"Account {account!r} not found in Mail.app configured accounts."
        )
