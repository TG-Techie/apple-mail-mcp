"""Fixtures shared by the integration tests.

Nothing in this directory names an account, an address, a login or a
host. Every test that touches a real account gets it from
``MAIL_TEST_ACCOUNT`` — the same variable the server's safety gate
checks — and everything about that account (its address, IMAP host,
port, login) is read from Mail.app at run time. The one real address a
test may send to, the loopback, comes from ``MAIL_TEST_LOOPBACK`` the
same way. A literal in a fixture is a second configuration path that
nobody maintains, and a default is a literal that only applies when you
forgot to set the real one.
"""

import os
import time
from collections.abc import Iterator

import pytest

from apple_mail_mcp.exceptions import MailDraftNotFoundError
from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.utils import escape_applescript_string

# Every draft an integration test saves carries this subject prefix, so
# what a run leaves behind can be found without knowing which test made
# it. The tests still delete what they created; this is for what that
# misses (see ``_test_drafts_swept``).
TEST_DRAFT_SUBJECT_PREFIX = "ZZZ-AMM-INTEG-"

# A draft saved through Mail's scripting dictionary with a named sender
# was re-saved by Mail under a new id after the test had deleted the
# original: 1–5 s after creation on the Gmail test account, 12–31 s on
# the iCloud one across the day's measurements (2026-09-11,
# docs/research/icloud-draft-resync.md, Observations 3, 4 and 8). The
# connector has saved every draft from a compose window since
# 2026-09-27, which kept its id (docs/research/draft-resave-spike.md),
# so its own drafts should no longer do this; the sweep still catches
# what a failed test leaves, and a copy from anything saved the old
# way. Nothing readable says whether a re-save is still pending, so the
# end-of-session sweep keeps sweeping until the Drafts have stayed
# clear for _RESAVE_QUIET_S, and gives up at _RESAVE_MAX_S. Both are
# bounds with margin, not signals.
_RESAVE_POLL_S = 5
_RESAVE_QUIET_S = 35
_RESAVE_MAX_S = 150


@pytest.fixture
def test_account() -> str:
    """The Mail.app account name the integration tests run against."""
    account = os.getenv("MAIL_TEST_ACCOUNT")
    if not account:
        pytest.fail(
            "MAIL_TEST_ACCOUNT is not set. Integration tests run against "
            "exactly the account it names; there is no default."
        )
    return account


@pytest.fixture
def test_account_address(test_account: str) -> str:
    """The test account's email address, as Mail.app has it configured.

    The first of the account's addresses, which is the one the server
    resolves an account to when it names a sender. Tests compare a From
    header against it rather than against a literal.
    """
    for account in AppleMailConnector().list_accounts():
        if test_account in (account.get("name"), account.get("id")):
            addresses = account.get("email_addresses") or []
            if not addresses:
                pytest.fail(
                    f"Mail.app has no email address for {test_account!r}."
                )
            return str(addresses[0])
    pytest.fail(f"MAIL_TEST_ACCOUNT={test_account!r} is not a Mail.app account.")


@pytest.fixture
def loopback_address() -> str:
    """The one real address the loopback tests send to: mail sent there
    arrives back in the test account's INBOX, where a test can read what
    was actually delivered. Skips rather than fails when unset; the rest
    of the integration suite is valid without it."""
    address = os.getenv("MAIL_TEST_LOOPBACK", "").strip()
    if not address:
        pytest.skip(
            "MAIL_TEST_LOOPBACK is not set; loopback read-back tests send "
            "to exactly the address it names"
        )
    return address


def _test_draft_ids(connector: AppleMailConnector, account: str) -> list[str]:
    """Ids of the drafts in ``account`` whose subject carries the test
    prefix, read through Mail's aggregate drafts mailbox."""
    account_safe = escape_applescript_string(account)
    prefix_safe = escape_applescript_string(TEST_DRAFT_SUBJECT_PREFIX)
    out = connector._run_applescript(f'''
    tell application "Mail"
        set out to {{}}
        repeat with d in messages of drafts mailbox
            try
                if (subject of d as text) starts with "{prefix_safe}" and (name of account of mailbox of d) is "{account_safe}" then
                    set end of out to (id of d as text)
                end if
            end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        return out as text
    end tell''').strip()
    return [line for line in out.splitlines() if line]


def _sweep_test_drafts(connector: AppleMailConnector, account: str) -> int:
    """Delete every test-prefixed draft in ``account``, each by its id:
    a ``delete`` on the item reference of a mailbox walk removes nothing
    and reports no error (Observation 8). Returns how many went."""
    swept = 0
    for draft_id in _test_draft_ids(connector, account):
        try:
            connector.delete_draft(draft_id)
        except MailDraftNotFoundError:
            continue
        swept += 1
    return swept


@pytest.fixture(scope="session", autouse=True)
def _test_drafts_swept(request: pytest.FixtureRequest) -> Iterator[None]:
    """Leave the test account's Drafts as the run found them.

    Each draft test deletes the draft it created, by the id it was
    given. A draft saved through the dictionary with a named sender was
    re-saved by Mail under a new id shortly after, so that delete
    removed the original and the copy stayed; one class run left six
    (see the comment on _RESAVE_POLL_S for why that should no longer
    happen). The sweep runs before the first integration test, for what
    an earlier run left, and after the last, repeating until nothing
    new has appeared for a quiet period — that tail is skipped when no
    draft test was collected.
    """
    if not request.config.getoption("--run-integration"):
        yield
        return
    account = os.getenv("MAIL_TEST_ACCOUNT")
    if not account:
        yield
        return
    connector = AppleMailConnector()
    _sweep_test_drafts(connector, account)
    yield
    if not any("draft" in item.nodeid.lower() for item in request.session.items):
        _sweep_test_drafts(connector, account)
        return
    started = last_found = time.monotonic()
    while True:
        if _sweep_test_drafts(connector, account):
            last_found = time.monotonic()
        now = time.monotonic()
        if now - last_found >= _RESAVE_QUIET_S or now - started >= _RESAVE_MAX_S:
            return
        time.sleep(_RESAVE_POLL_S)
