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
before executing. A dispatch Mail performs later on the caller's behalf
counts: a rule's ``forward_to`` goes through
``assert_forward_targets_allowed`` before the rule is installed.

╔══════════════════════════════════════════════════════════════════════╗
║ POLICY — set by the human repo owner, 2026-05-20; revised 2026-08-24 ║
║ (hardcoded entries stripped on owner directive).                     ║
║                                                                      ║
║ The comms config YAML is the ONLY allowlist source (key:             ║
║ email.allowed_outbound). Path is controlled by                       ║
║ APPLE_MAIL_MCP_COMMS_CONFIG                                          ║
║ (default: ~/iCloud/AgentAccessConfig/comms.yaml). Only the owner     ║
║ edits that file; agents have read-only access. There are NO          ║
║ hardcoded policy values in code, and agents MUST NOT introduce any.  ║
║                                                                      ║
║ FAIL CLOSED: if the config is missing, unreadable, or malformed,     ║
║ every send is blocked (OutboundAllowlistUnavailableError) until the  ║
║ owner fixes the file. There is no fallback list. A readable config   ║
║ that grants nothing is valid and likewise blocks everything.         ║
║                                                                      ║
║ Test-mode carve-out (MAIL_TEST_MODE=true): RFC 2606 reserved test    ║
║ domains (@example.com, .test, .invalid, .localhost) pass — with or   ║
║ without a readable config — so the integration-test harness works.   ║
║ Production never sets that env var.                                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Iterable
from email.utils import getaddresses
from pathlib import Path

import yaml

from .exceptions import (
    MailOutboundDisallowedError,
    OutboundAllowlistUnavailableError,
)

_log = logging.getLogger(__name__)

# Env var pointing to the comms config YAML (key: email.allowed_outbound).
# Defaults to ~/iCloud/AgentAccessConfig/comms.yaml.
# File is read at call time so owner edits take effect on the next send.
COMMS_CONFIG_ENV = "APPLE_MAIL_MCP_COMMS_CONFIG"
_COMMS_CONFIG_DEFAULT = "~/iCloud/AgentAccessConfig/comms.yaml"


# Mirror security.RESERVED_TEST_* here to avoid a circular import. Keep in
# sync if those change.
_RESERVED_TEST_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_TEST_TLDS = frozenset({".example", ".test", ".invalid", ".localhost"})


# One address, nothing else: no whitespace, no separators, one "@" with
# something on both sides and a dot in the domain. Stricter than RFC
# 5322 on purpose; the gate refuses what it cannot vouch for.
_ONE_ADDRESS_RE = re.compile(r"^[^\s@,;<>\"]+@[^\s@,;<>\"]+\.[^\s@,;<>\"]+$")


def single_address(recipient: str) -> str | None:
    """The one address a recipient string names, lowercased, or None.

    None when the string parses as anything other than exactly one
    well-formed address: two addresses joined by a comma or a space, a
    display name with no address, an address with a newline in it. The
    mailto: send path splits on commas (RFC 6068) and Mail's compose
    path accepts whatever text it is handed, so a string that is not one
    address is one this layer cannot say where it would go.
    """
    parsed = getaddresses([recipient])
    if len(parsed) != 1:
        return None
    addr = parsed[0][1].strip().lower()
    if not _ONE_ADDRESS_RE.fullmatch(addr):
        return None
    return addr



def allowlist_patterns() -> list[str]:
    """The outbound allowlist, read from the comms config YAML at call
    time (sole source — there is no hardcoded list).

    Path: ``APPLE_MAIL_MCP_COMMS_CONFIG`` env var, defaulting to
    ``~/iCloud/AgentAccessConfig/comms.yaml``.

    Expected schema (owner-authored; the ``email`` section is one of
    several top-level sections, e.g. ``imessage``)::

        email:
          allowed_outbound:
            - '*@owner-domain.example'
            - 'partner@example.com'

    Raises ``OutboundAllowlistUnavailableError`` (FAIL CLOSED) when the
    file is missing, unreadable, unparseable, or structurally malformed
    (non-mapping root, non-mapping ``email`` section, non-list
    ``allowed_outbound``). A structurally valid config with no ``email``
    section or no ``allowed_outbound`` key returns ``[]`` — a policy
    that grants nothing, not a broken one.
    """
    raw_path = os.environ.get(COMMS_CONFIG_ENV, _COMMS_CONFIG_DEFAULT)
    config_path = Path(raw_path).expanduser()
    try:
        with config_path.open() as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise OutboundAllowlistUnavailableError(
            f"outbound allowlist unavailable: comms config not found at "
            f"{config_path} — all sends are blocked until the owner "
            f"restores it (or fixes {COMMS_CONFIG_ENV})."
        ) from None
    except Exception as exc:
        raise OutboundAllowlistUnavailableError(
            f"outbound allowlist unavailable: comms config {config_path} "
            f"unreadable or unparseable ({exc}) — all sends are blocked "
            f"until the owner fixes it."
        ) from exc

    if not isinstance(data, dict):
        raise OutboundAllowlistUnavailableError(
            f"outbound allowlist unavailable: comms config {config_path} "
            f"root must be a YAML mapping, got {type(data).__name__} — "
            f"all sends are blocked until the owner fixes it."
        )
    email_section = data.get("email")
    if email_section is None:
        return []
    if not isinstance(email_section, dict):
        raise OutboundAllowlistUnavailableError(
            f"outbound allowlist unavailable: comms config {config_path} "
            f"'email' section must be a mapping — all sends are blocked "
            f"until the owner fixes it."
        )
    entries = email_section.get("allowed_outbound")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise OutboundAllowlistUnavailableError(
            f"outbound allowlist unavailable: comms config {config_path} "
            f"email.allowed_outbound must be a list — all sends are "
            f"blocked until the owner fixes it."
        )
    return [str(e).strip().lower() for e in entries if e]


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
    domains are considered allowed (test-mode override), and an
    unavailable config degrades to an empty pattern set (reserved
    domains only). Outside test mode an unavailable config raises
    ``OutboundAllowlistUnavailableError`` — FAIL CLOSED.
    """
    test_mode = _is_test_mode()
    try:
        patterns = allowlist_patterns()
    except OutboundAllowlistUnavailableError:
        if not test_mode:
            raise
        _log.warning(
            "comms config unavailable under MAIL_TEST_MODE — only RFC "
            "2606 reserved test domains are sendable."
        )
        patterns = []
    bad: list[str] = []
    for r in recipients:
        addr = single_address(r)
        if addr is None:
            bad.append(r)
            continue
        if test_mode and _is_reserved_test_domain(addr):
            continue
        if not _matches_any(addr, patterns):
            bad.append(r)
    return bad


def all_recipients_allowed(recipients: list[str]) -> bool:
    """True iff ``recipients`` is non-empty AND every recipient is
    allowlisted. Fail-closed on empty input AND on an unavailable
    config — this is only the elicitation-bypass UX check, so "policy
    unreadable" simply means "no bypass" here; the HARD gate
    (``assert_recipients_allowed_for_send``) is the one that raises.
    """
    if not recipients:
        return False
    try:
        return not disallowed_recipients(recipients)
    except OutboundAllowlistUnavailableError:
        return False


def assert_recipients_allowed_for_send(
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    *,
    seed: str = "new",
    reply_all: bool = False,
) -> None:
    """Hard policy gate at the actual-send call site.

    A recipient group left ``None`` on a reply is filled in by Mail.app
    from the message being replied to, and nothing at this layer ever
    sees those addresses: a plain reply derives ``to``, a reply-all
    derives ``to`` and ``cc``. Every group Mail would derive must be
    explicit (``[]`` is explicit); an on-list address in another group
    does not vouch for the derived one. A forward derives nothing.

    Raises ``MailOutboundDisallowedError`` if:
      - ``seed`` is ``"reply"`` and ``to`` is None, or additionally
        ``cc`` is None when ``reply_all``.
      - All recipient groups are empty (no one to send to).
      - Any recipient (across to/cc/bcc) is not on the allowlist.

    Raises ``OutboundAllowlistUnavailableError`` (a subclass) when the
    comms config cannot be read — FAIL CLOSED, no sends while the
    policy is unreadable (test-mode carve-out aside).

    Returns None silently when every recipient is on the allowlist.
    """
    if seed == "reply":
        derived = [
            name
            for name, group in (("to", to), ("cc", cc))
            if group is None and (name == "to" or reply_all)
        ]
        if derived:
            raise MailOutboundDisallowedError(
                f"send_now=True on a {'reply-all' if reply_all else 'reply'} "
                f"requires explicit {' and '.join(derived)} — Mail.app would "
                "fill it from the message being replied to, and that cannot "
                "be verified against the outbound allowlist at the send "
                "layer. Pass [] to send to nobody in that group."
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


def assert_forward_targets_allowed(targets: list[str]) -> None:
    """Hard policy gate for a rule's ``forward_to``.

    A rule that forwards is a standing send: every message it matches
    from then on goes to these addresses, with nobody reading each one.
    So the targets meet the outbound allowlist exactly as a send's
    recipients do, and an unreadable policy blocks the rule.

    Raises ``MailOutboundDisallowedError`` when there is no target, when
    a target is not one plain address (Mail takes the joined list as a
    single field, so a target carrying two addresses would smuggle one
    past the check), or when any target is off the allowlist; raises
    ``OutboundAllowlistUnavailableError`` (a subclass) when the policy
    cannot be read — FAIL CLOSED.
    """
    if not targets:
        raise MailOutboundDisallowedError(
            "rule blocked — forward_to names no address."
        )
    bad = disallowed_recipients(targets)
    if bad:
        raise MailOutboundDisallowedError(
            "rule blocked — forward_to addresses not on outbound allowlist: "
            + ", ".join(repr(b) for b in bad)
        )
