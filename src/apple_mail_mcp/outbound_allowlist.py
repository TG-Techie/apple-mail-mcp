"""Centralized outbound recipient allowlist — the single point of truth
for which recipient addresses may receive outbound mail.

Imported by BOTH:
  - ``server.py``      — to short-circuit MCP elicitation when all
                          recipients are pre-trusted.
  - ``mail_connector.py`` — to HARD-BLOCK any attempt to actually dispatch
                            mail (``tell theMessage to send``) when any
                            recipient is off-list. This is the policy
                            enforcement perimeter.

Both layers MUST consult this module. Bypassing it on either side defeats
the policy. Any new tool, helper, or AppleScript that could cause an
outbound mail dispatch must call ``assert_recipients_allowed_for_send``
before executing.

╔══════════════════════════════════════════════════════════════════════╗
║ POLICY — set by the human repo owner, 2026-05-20.                    ║
║                                                                      ║
║ Entries in USER_EXPLICIT_OUTBOUND_ALLOW_LIST below are the always-on ║
║ default. ONLY the human repo owner may add, remove, or alter         ║
║ entries. Agents (including the agent that authored this file) MUST   ║
║ NOT modify USER_EXPLICIT_OUTBOUND_ALLOW_LIST without explicit        ║
║ per-change user authorization quoted in the conversation. The same   ║
║ rule applies to the env-var augmentation path:                       ║
║ APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST — agents may not set/unset ║
║ it on the user's behalf without authorization.                       ║
║                                                                      ║
║ Test-mode override (MAIL_TEST_MODE=true) lets RFC 2606 reserved test ║
║ domains (@example.com, .test, .invalid, .localhost) through so the   ║
║ integration-test harness still functions. Production never sets that ║
║ env var.                                                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Iterable

from .exceptions import MailOutboundDisallowedError

# ──────────────────────────────────────────────────────────────────────────
# POLICY VALUE — see policy block above before touching.
# ──────────────────────────────────────────────────────────────────────────
USER_EXPLICIT_OUTBOUND_ALLOW_LIST: tuple[str, ...] = (
    "*@tg-techie.com",
)

# Env var that ADDS patterns to the hardcoded list. Cannot remove from it.
SEND_ELICITATION_ALLOWLIST_ENV = "APPLE_MAIL_MCP_SEND_ELICITATION_ALLOWLIST"

_EMAIL_EXTRACT_RE = re.compile(r"<([^>]+)>")

# Mirror security.RESERVED_TEST_* here to avoid a circular import. Keep in
# sync if those change — there's a ruff/parity test we can add later.
_RESERVED_TEST_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_TEST_TLDS = frozenset({".example", ".test", ".invalid", ".localhost"})


def extract_email(recipient: str) -> str:
    """Pull the bare ``addr@host`` out of a recipient string.

    Handles:
      - ``"jonah@tg-techie.com"`` → ``"jonah@tg-techie.com"``
      - ``"Jonah Y-M <jonah@tg-techie.com>"`` → ``"jonah@tg-techie.com"``
      - ``"<jonah@tg-techie.com>"`` → ``"jonah@tg-techie.com"``
    Strings without ``<...>`` are returned trimmed/lowercased as-is.
    """
    m = _EMAIL_EXTRACT_RE.search(recipient)
    if m:
        return m.group(1).strip().lower()
    return recipient.strip().lower()


def allowlist_patterns() -> list[str]:
    """Hardcoded defaults + env-var additions. Env var ADDS only — it
    cannot remove from the hardcoded defaults. Resolved at call time so
    env changes between calls are honored.
    """
    patterns = [p.lower() for p in USER_EXPLICIT_OUTBOUND_ALLOW_LIST]
    raw = os.environ.get(SEND_ELICITATION_ALLOWLIST_ENV, "")
    patterns.extend(p.strip().lower() for p in raw.split(",") if p.strip())
    return patterns


def _is_test_mode() -> bool:
    return os.environ.get("MAIL_TEST_MODE", "").lower() == "true"


def _is_reserved_test_domain(addr: str) -> bool:
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1].lower()
    if domain in _RESERVED_TEST_DOMAINS:
        return True
    for tld in _RESERVED_TEST_TLDS:
        bare = tld.lstrip(".")
        if domain == bare or domain.endswith(tld):
            return True
    return False


def _matches_any(addr: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(addr, p) for p in patterns)


def disallowed_recipients(recipients: list[str]) -> list[str]:
    """Return the subset of ``recipients`` whose extracted email is NOT on
    the allowlist. Under MAIL_TEST_MODE=true, RFC 2606 reserved test
    domains are considered allowed (test-mode override).
    """
    patterns = allowlist_patterns()
    test_mode = _is_test_mode()
    bad: list[str] = []
    for r in recipients:
        addr = extract_email(r)
        if test_mode and _is_reserved_test_domain(addr):
            continue
        if not _matches_any(addr, patterns):
            bad.append(r)
    return bad


def all_recipients_allowed(recipients: list[str]) -> bool:
    """True iff ``recipients`` is non-empty AND every recipient is allowlisted.
    Fail-closed on empty input.
    """
    if not recipients:
        return False
    return not disallowed_recipients(recipients)


def assert_recipients_allowed_for_send(
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    *,
    seed: str = "new",
) -> None:
    """Hard policy gate at the actual-send call site.

    Raises ``MailOutboundDisallowedError`` if:
      - ``seed`` is ``"reply"`` or ``"forward"`` AND all of (to, cc, bcc)
        are None — Mail.app would auto-derive recipients we cannot
        validate at this layer. Caller must pass explicit recipients.
      - All recipient groups are empty (no one to send to).
      - Any recipient (across to/cc/bcc) is not on the allowlist.

    Returns None silently when every recipient is on the allowlist.
    """
    if (
        seed in ("reply", "forward")
        and to is None
        and cc is None
        and bcc is None
    ):
        raise MailOutboundDisallowedError(
            f"send_now=True on a {seed} requires explicit recipients "
            "(to/cc/bcc) — Mail.app's auto-derived recipients cannot be "
            "verified against the outbound allowlist at the send layer."
        )

    all_r: list[str] = []
    for group in (to, cc, bcc):
        if group:
            all_r.extend(group)

    if not all_r:
        raise MailOutboundDisallowedError(
            "send_now=True with no recipients (to/cc/bcc all empty)."
        )

    bad = disallowed_recipients(all_r)
    if bad:
        raise MailOutboundDisallowedError(
            "send blocked — recipients not on outbound allowlist: "
            + ", ".join(repr(b) for b in bad)
        )
