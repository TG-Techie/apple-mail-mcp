"""Unit tests for the connector's base: the runner, the Mail lock and the account lookups."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailTimeoutError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector


class TestConnectorBase:
    """Tests for ConnectorBase."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch("subprocess.run")
    def test_run_applescript_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test successful AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="result",
            stderr=""
        )

        result = connector._run_applescript("test script")
        assert result == "result"

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/osascript", "-"]

    @patch("subprocess.run")
    def test_run_applescript_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test account not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get account \"NonExistent\""
        )

        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test mailbox not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get mailbox \"NonExistent\""
        )

        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_timeout(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A script osascript did not finish within the connector's
        timeout is its own error, told apart from a script that failed,
        and still a MailAppleScriptError to every handler of those."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        with pytest.raises(MailTimeoutError) as raised:
            connector._run_applescript("test script")

        assert str(raised.value) == (
            f"Script execution timeout after {connector.timeout}s"
        )
        assert isinstance(raised.value, MailAppleScriptError)
        assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)

    @patch("subprocess.run")
    def test_run_applescript_curly_apostrophe_still_maps_to_typed_error(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Real macOS stderr uses curly apostrophes — must still dispatch typed errors.

        Regression guard for a bug where `Can\u2019t get account "X"` (curly
        apostrophe, as emitted by Mail.app) bypassed the typed-exception
        mapping and surfaced as a generic MailAppleScriptError, defeating the
        server-layer not-found routing.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get account \"NonExistent\"",
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get mailbox \"NonExistent\"",
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","full_name":"Alice Smith",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true},'
            '{"id":"UUID-2","name":"Work","full_name":"",'
            '"email_addresses":["me@work.com","alt@work.com"],'
            '"account_type":"iCloud","enabled":false}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"id": "UUID-1", "name": "Gmail", "full_name": "Alice Smith",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            # Empty-string full_name normalized to None.
            {"id": "UUID-2", "name": "Work", "full_name": None,
             "email_addresses": ["me@work.com", "alt@work.com"],
             "account_type": "iCloud", "enabled": False},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_normalizes_whitespace_only_full_name_to_none(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Whitespace-only full_name is treated as not-configured."""
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","full_name":"   ",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result[0]["full_name"] is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_accounts()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_handles_empty_email_addresses(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """An account with no email addresses must return email_addresses as []."""
        mock_run.return_value = (
            '[{"id":"UUID-3","name":"LocalOnly","full_name":"Local User",'
            '"email_addresses":[],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result == [{
            "id": "UUID-3", "name": "LocalOnly", "full_name": "Local User",
            "email_addresses": [],
            "account_type": "imap", "enabled": True,
        }]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text), enabled,
        and the full_name (#158) used for the Display Name <email> sender."""
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script
        # #158: full_name read with missing-value coercion.
        assert "full name of acc" in script
        assert "|full_name|:accFullName" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| (quoted) so NSJSONSerialization keeps it.

        Unquoted `name:` in the record literal causes the key to be silently
        dropped during ASObjC -> NSDictionary conversion because `name` collides
        with NSObject's `name` property. Regression guard for real Mail.app bug.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of acc)" in script
        assert "{name:(name of acc)" not in script

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_with_full_name_emits_display_form(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account with full_name -> 'Display Name <email>' form."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_accepts_the_accounts_own_address(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A sender read back from a draft ("Name <email>" or a bare
        email) resolves to the account that owns the address, so a
        rebuilt draft stays in that account. Case-insensitive on the
        address, as mail addresses are."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com", "alias@icloud.com"],
            },
            {
                "id": "UUID-2",
                "name": "Gmail",
                "full_name": "",
                "email_addresses": ["alice@gmail.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("Alice Smith <alice@icloud.com>")
            == "Alice Smith <alice@icloud.com>"
        )
        assert (
            connector._resolve_account_to_sender("Alias@iCloud.com")
            == "Alice Smith <alice@icloud.com>"
        )
        assert connector._resolve_account_to_sender("alice@gmail.com") == "alice@gmail.com"
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_account_to_sender("nobody@example.com")

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_without_full_name_falls_back_to_bare_email(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account without full_name -> bare email (graceful fallback)."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": None,
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_whitespace_only_full_name_falls_back(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: whitespace-only full_name treated as not-configured."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "   ",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_lookup_by_uuid_with_full_name(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("UUID-1")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_not_found_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailAccountNotFoundError

        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_account_to_sender("Bogus")

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_no_emails_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "Empty",
                "full_name": "Nobody",
                "email_addresses": [],
            },
        ]
        with pytest.raises(ValueError, match="email addresses"):
            connector._resolve_account_to_sender("Empty")


class TestConnectorThreadsTimeoutIntoScripts:
    """Issue #227 — AppleMailConnector(timeout=N) must propagate N into the
    AppleScript `with timeout` clause, not just into subprocess.run."""

    @patch("subprocess.run")
    def test_list_accounts_emits_connector_timeout(
        self, mock_run: MagicMock
    ) -> None:
        connector = AppleMailConnector(timeout=240)
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        connector.list_accounts()
        script = mock_run.call_args.kwargs.get("input") or mock_run.call_args.args[0]
        assert "with timeout of 240 seconds" in script

    @patch("subprocess.run")
    def test_search_messages_applescript_emits_connector_timeout(
        self, mock_run: MagicMock
    ) -> None:
        connector = AppleMailConnector(timeout=300)
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        connector._search_messages_applescript(
            account="Exchange",
            mailbox="Inbox",
            sender_contains="ofca",
            limit=10,
        )
        script = mock_run.call_args.kwargs.get("input") or mock_run.call_args.args[0]
        assert "with timeout of 300 seconds" in script


class TestMailAutomationLock:
    """Cross-process serialization of Mail automation (2026-07-23).

    Multiple agent sessions each spawn their own MCP server; concurrent
    AppleScript against Mail.app collides into AppleEvent timeouts
    (-1712) and invalid connections (-609). A file lock under
    APPLE_MAIL_MCP_HOME queues callers instead, and a caller that cannot
    acquire the lock in time gets a CLEAR busy error instead of
    AppleEvent garbage.
    """

    def test_lock_file_created_under_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        connector = AppleMailConnector(timeout=5)
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "ok"
        with patch("subprocess.run", return_value=fake):
            assert connector._run_applescript("return 1") == "ok"
        assert (Path(str(tmp_path)) / "mail_automation.lock").exists()

    def test_lock_contention_raises_clear_busy_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """Another PROCESS holding the lock → MailAppleScriptError naming
        the busy condition (never reaches osascript)."""
        import subprocess as sp
        import sys
        import time as time_mod
        from pathlib import Path
        from unittest.mock import patch

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        lock_path = Path(str(tmp_path)) / "mail_automation.lock"
        holder = sp.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,sys,time\n"
                    f"fh=open({str(lock_path)!r},'w')\n"
                    "fcntl.flock(fh, fcntl.LOCK_EX)\n"
                    "print('held',flush=True)\n"
                    "time.sleep(10)\n"
                ),
            ],
            stdout=sp.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "held"
            connector = AppleMailConnector(timeout=5, lock_timeout=0.5)
            start = time_mod.monotonic()
            with patch("subprocess.run") as mock_run, pytest.raises(
                MailAppleScriptError, match="busy"
            ):
                connector._run_applescript("return 1")
            assert mock_run.call_count == 0, "osascript must not run without the lock"
            assert time_mod.monotonic() - start < 5
        finally:
            holder.kill()
            holder.wait()
