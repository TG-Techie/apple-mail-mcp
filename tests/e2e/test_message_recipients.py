"""Through the protocol, every message row a read tool returns carries
its recipients: ``to``, ``cc`` and ``bcc`` on the rows of
``search_messages``, ``get_messages`` and ``get_thread``, on the
AppleScript path and on the IMAP path.

Only what leaves the process is replaced: osascript (the connector's
``_run_applescript``) with the JSON Mail's scripts return, and the IMAP
socket (``IMAPClient``) with the ENVELOPE a server returns. The tools,
the connector's dispatch between the paths, and both paths' row
building run as they do in the server. Both fakes describe the same
message, so each tool must return the same lists whichever path ran.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.response_types import Address, Envelope

from apple_mail_mcp import server
from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.e2e

ACCOUNT = "Test Account"

# The message both fakes describe.
EXPECTED = {
    "to": ["Jane Doe <jane@example.com>", "ops@example.org"],
    "cc": ["cc@example.net"],
    "bcc": [],
}

# As Mail's scripts emit it: one {name, address} record per recipient,
# Mail's missing value (no display name) already turned into "".
_MAIL_RECORD: dict[str, Any] = {
    "id": "100",
    "rfc_message_id": "m-100@example.com",
    "subject": "Q3",
    "sender": "Alice <alice@example.com>",
    "date_received": "Monday, January 5, 2026 at 10:00:00",
    "read_status": False,
    "flagged": False,
    "to": [
        {"name": "Jane Doe", "address": "jane@example.com"},
        {"name": "", "address": "ops@example.org"},
    ],
    "cc": [{"name": "", "address": "cc@example.net"}],
    "bcc": [],
    "warnings": [],
}

# As an IMAP server returns it.
_ENVELOPE = Envelope(
    date=datetime(2026, 1, 5, 10, 0, 0),
    subject=b"Q3",
    from_=(Address(b"Alice", None, b"alice", b"example.com"),),
    sender=None,
    reply_to=None,
    to=(
        Address(b"Jane Doe", None, b"jane", b"example.com"),
        Address(None, None, b"ops", b"example.org"),
    ),
    cc=(Address(None, None, b"cc", b"example.net"),),
    bcc=None,
    in_reply_to=None,
    message_id=b"<m-100@example.com>",
)


def _mail_scripts(script: str) -> str:
    """What Mail returns for each read script these tools run."""
    if "set anchorResult to missing value" in script:  # thread anchor
        return json.dumps({
            "account": ACCOUNT, "rfc_message_id": "m-100@example.com",
            "subject": "Q3", "in_reply_to": "", "references_raw": "",
        })
    if "set candRecord to" in script:  # thread candidates
        return json.dumps(
            [{**_MAIL_RECORD, "in_reply_to": "", "references_raw": ""}]
        )
    if "set msgs to messages of mailboxRef" in script:  # search
        return json.dumps({"messages": [_MAIL_RECORD], "warnings": []})
    if "set foundMsg to missing value" in script:  # attachments
        return json.dumps({"attachments": [], "warnings": []})
    if "|content|:msgContent" in script:  # get_message
        return json.dumps({**_MAIL_RECORD, "content": ""})
    raise AssertionError(f"unexpected script: {script[:200]}")


@pytest.fixture(autouse=True)
def _disable_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here reaches Mail; the account gate is not under test."""
    monkeypatch.setenv("MAIL_TEST_MODE", "false")


class _Mail:
    """Answers the connector's scripts and remembers which ran, so a
    test can say which path built its rows."""

    # A fragment only that read's script contains.
    READS = {
        "search": "set msgs to messages of mailboxRef",
        "get_message": "|content|:msgContent",
        "thread": "set candRecord to",
    }

    def __init__(self) -> None:
        self.ran: list[str] = []

    def __call__(self, script: str) -> str:
        self.ran += [read for read, marker in self.READS.items() if marker in script]
        return _mail_scripts(script)


def _serve(monkeypatch: pytest.MonkeyPatch) -> _Mail:
    """A real connector behind the tools, talking to a fake Mail."""
    connector = AppleMailConnector()
    mail = _Mail()
    monkeypatch.setattr(connector, "_run_applescript", mail)
    monkeypatch.setattr(
        connector,
        "_resolve_imap_config",
        lambda _account: ("imap.example.com", 993, "me@example.com"),
    )
    monkeypatch.setattr(server, "mail", connector)
    return mail


@pytest.fixture
def applescript_path(monkeypatch: pytest.MonkeyPatch) -> _Mail:
    """IMAP is not set up for the account, so every read runs AppleScript."""
    def no_entry(_account: str, _email: str) -> str:
        raise MailKeychainEntryNotFoundError("no Keychain entry")

    monkeypatch.setattr("apple_mail_mcp.mail_connector.get_imap_password", no_entry)
    return _serve(monkeypatch)


@pytest.fixture
def imap_path(monkeypatch: pytest.MonkeyPatch) -> _Mail:
    """IMAP is set up, and the server holds the one message."""
    client = MagicMock()
    client.capabilities.return_value = ()
    client.list_folders.return_value = [((), b"/", "INBOX")]
    client.search.return_value = [1]
    client.fetch.return_value = {1: {b"ENVELOPE": _ENVELOPE, b"FLAGS": ()}}
    monkeypatch.setattr(
        "apple_mail_mcp.mail_connector.get_imap_password", lambda _a, _e: "pw"
    )
    monkeypatch.setattr(
        "apple_mail_mcp.imap_connector.IMAPClient", lambda *_a, **_k: client
    )
    return _serve(monkeypatch)


def _recipients(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("to", "cc", "bcc")}


async def _rows(tool: str, args: dict[str, Any], key: str) -> list[dict[str, Any]]:
    result = await server.mcp.call_tool(tool, args)
    body = result.structured_content
    assert body is not None
    assert body["success"] is True, body
    return list(body[key])


@pytest.mark.parametrize("path", ["applescript_path", "imap_path"])
class TestEveryReadToolRowCarriesItsRecipients:
    """Each tool on each path; the script log says which path ran."""

    async def test_search_messages(
        self, path: str, request: pytest.FixtureRequest
    ) -> None:
        mail: _Mail = request.getfixturevalue(path)
        [row] = await _rows(
            "search_messages", {"account": ACCOUNT, "mailbox": "INBOX"}, "messages"
        )
        assert _recipients(row) == EXPECTED
        assert ("search" in mail.ran) is (path == "applescript_path")

    async def test_get_messages(
        self, path: str, request: pytest.FixtureRequest
    ) -> None:
        mail: _Mail = request.getfixturevalue(path)
        # An RFC id with account and mailbox takes the IMAP path; without
        # IMAP set up the same call runs AppleScript.
        [row] = await _rows(
            "get_messages",
            {"message_ids": ["m-100@example.com"], "account": ACCOUNT,
             "mailbox": "INBOX"},
            "messages",
        )
        assert _recipients(row) == EXPECTED
        assert ("get_message" in mail.ran) is (path == "applescript_path")

    async def test_get_thread(
        self, path: str, request: pytest.FixtureRequest
    ) -> None:
        mail: _Mail = request.getfixturevalue(path)
        [row] = await _rows("get_thread", {"message_id": "100"}, "thread")
        assert _recipients(row) == EXPECTED
        assert ("thread" in mail.ran) is (path == "applescript_path")


async def test_a_recipient_warning_reaches_the_response(
    applescript_path: _Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable list comes back empty and the response says why,
    so an agent checking for a Cc cannot read the failure as "none"."""
    warning = "cc recipients unreadable for message 100: x (error -10000)"
    record = {**_MAIL_RECORD, "cc": [], "warnings": [warning]}

    def scripts(script: str) -> str:
        if "|content|:msgContent" in script:
            return json.dumps({**record, "content": ""})
        return _mail_scripts(script)

    monkeypatch.setattr(server.mail, "_run_applescript", scripts)
    result = await server.mcp.call_tool("get_messages", {"message_ids": ["100"]})
    body = result.structured_content
    assert body is not None
    assert body["success"] is True, body
    assert body["messages"][0]["cc"] == []
    assert warning in body["warnings"]
