"""Unit tests for ``include_links`` on the read paths: the AppleScript
script that reads a message's source, the IMAP fetch that carries it,
and the ``get_messages`` tool that asks for it. AppleScript and IMAP are
mocked; tests/integration/test_message_links.py reads a real message."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.imap_connector import ImapConnector
from apple_mail_mcp.links import MAX_SOURCE_BYTES
from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.tools.messages import get_messages

from .test_imap_connector import _fake_envelope

_SOURCE = (
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    '<p>Hi</p><a href="https://example.com/invite?a=1&amp;b=2">Accept</a>\r\n'
)
_LINKS = [{"url": "https://example.com/invite?a=1&b=2", "text": "Accept"}]
_SOURCE_KEYS = {"source", "source_size", "source_error"}


def _row(**extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "12345",
        "subject": "s",
        "sender": "a@example.com",
        "date_received": "Mon Jan 1 2024",
        "read_status": True,
        "flagged": False,
        "content": "Hi Accept",
    }
    row.update(extra)
    return row


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=30)


@pytest.fixture
def mock_mail() -> Iterator[MagicMock]:
    with patch("apple_mail_mcp.server.mail") as m:
        yield m


class TestAppleScriptPath:
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_default_reads_no_source(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(_row())
        row = connector.get_message("12345")
        script = mock_run.call_args[0][0]
        assert "source of" not in script
        assert "message size of" not in script
        assert "links" not in row

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reads_the_source_in_the_same_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(
            _row(source=_SOURCE, source_size=len(_SOURCE), source_error="")
        )
        row = connector.get_message("12345", include_links=True)
        assert mock_run.call_count == 1
        script = mock_run.call_args[0][0]
        assert "set msgSource to source of msg" in script
        assert "message size of msg" in script
        assert "|source|:msgSource" in script
        assert row["links"] == _LINKS
        assert _SOURCE_KEYS.isdisjoint(row)
        assert "warnings" not in row
        assert row["content"] == "Hi Accept"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_a_failed_source_read_is_a_warning(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The lookup loop's ``try`` takes any error for 'not in this
        mailbox', so the source read has its own guard, and a failure
        comes back as a warning with no links."""
        mock_run.return_value = json.dumps(
            _row(source="", source_size=0, source_error="boom (error -1728)")
        )
        row = connector.get_message("12345", include_links=True)
        script = mock_run.call_args[0][0]
        block = script[script.find('set msgSourceError to ""'):script.find("set resultData to {|id|")]
        assert "try" in block
        assert "on error errMsg number errNum" in block
        assert row["links"] == []
        assert row["warnings"] == [
            "links not read for message 12345: its source could not be read: "
            "boom (error -1728)"
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_an_oversized_source_is_not_read(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(
            _row(source="", source_size=MAX_SOURCE_BYTES + 1, source_error="")
        )
        row = connector.get_message("12345", include_links=True)
        assert f"msgSourceSize ≤ {MAX_SOURCE_BYTES}" in mock_run.call_args[0][0]
        assert row["links"] == []
        [warning] = row["warnings"]
        assert "too large" in warning and "12345" in warning

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_selected_messages_carry_links(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps(
            [_row(source=_SOURCE, source_size=100, source_error="", warnings=[])]
        )
        [row] = connector.get_selected_messages(include_links=True)
        assert "set msgSource to source of msg" in mock_run.call_args[0][0]
        assert row["links"] == _LINKS
        assert _SOURCE_KEYS.isdisjoint(row)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_selected_messages_default_reads_no_source(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = json.dumps([_row(warnings=[])])
        [row] = connector.get_selected_messages()
        assert "source of" not in mock_run.call_args[0][0]
        assert "links" not in row


class TestDispatch:
    def test_imap_path_is_asked_for_links(self, connector: AppleMailConnector) -> None:
        with patch.object(
            connector, "_imap_get_message", return_value={"id": "x", "links": []},
        ) as imap_path:
            connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX", include_links=True,
            )
        assert imap_path.call_args.kwargs["include_links"] is True

    def test_applescript_path_is_asked_for_links(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_get_message_applescript", return_value={"id": "1"},
        ) as as_path:
            connector.get_message("123", include_links=True)
        assert as_path.call_args.kwargs["include_links"] is True


class TestImapPath:
    def _client(self, mock_cls: MagicMock, raw: bytes) -> MagicMock:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = [42]
        client.fetch.return_value = {
            42: {
                b"ENVELOPE": _fake_envelope(message_id=b"<m@example.com>", subject=b"s"),
                b"FLAGS": (),
                b"BODY[TEXT]": b"Hi Accept",
                b"BODY[]": raw,
            }
        }
        return client

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_fetches_the_whole_message_in_the_same_fetch(self, mock_cls: MagicMock) -> None:
        client = self._client(mock_cls, _SOURCE.encode())
        row = ImapConnector("h", 993, "u@example.com", "pw").get_message(
            "m@example.com", mailbox="INBOX", include_links=True,
        )
        client.fetch.assert_called_once()
        assert b"BODY[]" in client.fetch.call_args[0][1]
        assert row["links"] == _LINKS
        assert "warnings" not in row
        assert row["content"] == "Hi Accept"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_default_does_not_fetch_the_whole_message(self, mock_cls: MagicMock) -> None:
        client = self._client(mock_cls, _SOURCE.encode())
        row = ImapConnector("h", 993, "u@example.com", "pw").get_message(
            "m@example.com", mailbox="INBOX",
        )
        assert b"BODY[]" not in client.fetch.call_args[0][1]
        assert "links" not in row

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_oversized_message_warns(self, mock_cls: MagicMock) -> None:
        self._client(mock_cls, b"x" * (MAX_SOURCE_BYTES + 1))
        row = ImapConnector("h", 993, "u@example.com", "pw").get_message(
            "m@example.com", mailbox="INBOX", include_links=True,
        )
        assert row["links"] == []
        [warning] = row["warnings"]
        assert "too large" in warning and "m@example.com" in warning

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_both_paths_give_the_same_links(
        self, mock_cls: MagicMock, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        self._client(mock_cls, _SOURCE.encode())
        imap_row = ImapConnector("h", 993, "u@example.com", "pw").get_message(
            "m@example.com", mailbox="INBOX", include_links=True,
        )
        mock_run.return_value = json.dumps(
            _row(source=_SOURCE, source_size=len(_SOURCE), source_error="")
        )
        as_row = connector.get_message("12345", include_links=True)
        assert imap_row["links"] == as_row["links"] == _LINKS


class TestTool:
    def test_default_does_not_ask_for_links(self, mock_mail: MagicMock) -> None:
        mock_mail.get_message.return_value = {"id": "1"}
        get_messages(["1"])
        assert mock_mail.get_message.call_args.kwargs["include_links"] is False

    def test_links_flow_through_explicit_ids_and_selection(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = [{"id": "s", "links": _LINKS}]
        mock_mail.get_message.return_value = {"id": "1", "links": _LINKS}
        result = get_messages(["SELECTED", "1"], include_links=True)
        assert result["success"] is True
        assert [m["links"] for m in result["messages"]] == [_LINKS, _LINKS]
        assert mock_mail.get_message.call_args.kwargs["include_links"] is True
        assert mock_mail.get_selected_messages.call_args.kwargs["include_links"] is True

    def test_link_warnings_reach_the_response(self, mock_mail: MagicMock) -> None:
        mock_mail.get_message.return_value = {
            "id": "1", "links": [], "warnings": ["links not read for message 1: x"],
        }
        result = get_messages(["1"], include_links=True)
        assert result["warnings"] == ["links not read for message 1: x"]
