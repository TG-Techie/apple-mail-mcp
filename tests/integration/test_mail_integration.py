"""
Integration tests for Apple Mail MCP.

These tests require:
1. Apple Mail.app installed and running
2. At least one configured mail account
3. Permission granted for automation
4. Environment variables for safety gate (when running tools via server.py):
   - MAIL_TEST_MODE=true
   - MAIL_TEST_ACCOUNT=<test account name>

Run with: MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<test account name> pytest --run-integration
"""

import datetime as _dt
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from apple_mail_mcp.mail_connector import AppleMailConnector

# Skip all integration tests by default
# Run with: pytest --run-integration
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run."
)


@pytest.fixture
def connector() -> AppleMailConnector:
    """Create a real connector instance."""
    return AppleMailConnector()


class TestMailIntegration:
    """Integration tests with real Apple Mail."""

    def test_list_mailboxes(self, connector: AppleMailConnector, test_account: str) -> None:
        """Test listing mailboxes from real account."""
        result = connector.list_mailboxes(test_account)
        assert isinstance(result, list)
        # Should have at least INBOX
        assert len(result) > 0

    def test_list_mailboxes_by_uuid(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """#61: account-gated tools also accept the account UUID.

        Discovers the test account's UUID at runtime via list_accounts,
        then calls list_mailboxes with the UUID. Results must match
        calling with the name.
        """
        accounts = connector.list_accounts()
        match = next((a for a in accounts if a["name"] == test_account), None)
        assert match is not None, f"Test account {test_account!r} not found"
        uuid = match["id"]

        # Sanity check: it really is a UUID-shaped string.
        from apple_mail_mcp.utils import is_account_uuid
        assert is_account_uuid(uuid), f"Expected UUID, got {uuid!r}"

        by_uuid = connector.list_mailboxes(uuid)
        by_name = connector.list_mailboxes(test_account)

        assert isinstance(by_uuid, list)
        # Results may not match in order, but both lists should have the same
        # set of mailbox names.
        assert {m["name"] for m in by_uuid} == {m["name"] for m in by_name}

    def test_search_messages(self, connector: AppleMailConnector, test_account: str) -> None:
        """Test searching messages in real mailbox."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            limit=5
        )
        assert isinstance(result, list)
        # Mailbox might be empty, so just check type

    def test_search_unread_messages(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Test searching for unread messages."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            read_status=False,
            limit=10
        )
        assert isinstance(result, list)

        # Verify all returned messages are unread
        for msg in result:
            assert msg["read_status"] is False

    def test_search_flagged_messages(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """New in #28: is_flagged pushes a flagged-status whose clause."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            is_flagged=True,
            limit=5,
        )
        assert isinstance(result, list)
        for msg in result:
            assert msg["flagged"] is True

    def test_search_with_date_range(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """New in #28: date_from + date_to stack in the whose clause.

        Uses a wide range so the query is guaranteed to return something on
        any realistic test mailbox with recent activity.
        """
        from datetime import date, timedelta

        today = date.today()
        range_start = (today - timedelta(days=365)).isoformat()
        range_end = today.isoformat()

        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            date_from=range_start,
            date_to=range_end,
            limit=5,
        )
        assert isinstance(result, list)
        # Non-empty only validates that a stacked date whose clause survives
        # round-trip to Mail. Empty inbox or no recent messages is a valid pass.

    def test_search_rejects_malformed_date(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Malformed date raises ValueError before any AppleScript runs."""
        with pytest.raises(ValueError):
            connector.search_messages(
                account=test_account,
                mailbox="INBOX",
                date_from="not-a-date",
            )

    def test_list_accounts(self, connector: AppleMailConnector) -> None:
        """Real list_accounts returns structured account records.

        Guards against the pre-0.4.0 `[{"raw": str}]` placeholder shape and
        the NSJSONSerialization `|name|` selector-collision bug fixed in #23.
        Exercises the v0.5.0 fields added in #26: id, account_type, enabled.
        """
        result = connector.list_accounts()
        assert isinstance(result, list)
        assert len(result) >= 1
        for acct in result:
            assert set(acct.keys()) >= {
                "id", "name", "email_addresses", "account_type", "enabled",
            }
            assert isinstance(acct["id"], str) and acct["id"]
            assert isinstance(acct["name"], str) and acct["name"]
            assert isinstance(acct["email_addresses"], list)
            assert isinstance(acct["account_type"], str) and acct["account_type"]
            assert isinstance(acct["enabled"], bool)
            # No "raw" key left over from the old placeholder
            assert "raw" not in acct

    def test_list_rules(self, connector: AppleMailConnector) -> None:
        """Real list_rules returns structured rule records.

        Rules list may be empty for a user who has never configured any. Empty
        is a valid pass. Non-empty entries must have name + enabled with the
        right types.
        """
        result = connector.list_rules()
        assert isinstance(result, list)
        for rule in result:
            assert set(rule.keys()) >= {"name", "enabled"}
            assert isinstance(rule["name"], str) and rule["name"]
            assert isinstance(rule["enabled"], bool)

    def test_get_thread_orphan_anchor(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """For any message, get_thread must at minimum return the anchor itself.

        This exercises the anchor-resolution + candidate-collection path
        end-to-end without needing a known-threaded message. Skips if the
        inbox is empty.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        thread = connector.get_thread(matches[0]["id"])
        assert isinstance(thread, list)
        assert len(thread) >= 1
        for m in thread:
            assert set(m.keys()) >= {
                "id", "subject", "sender", "date_received",
                "read_status", "flagged",
            }
        # Anchor must be in the result.
        assert any(m["id"] == matches[0]["id"] for m in thread)

    def test_get_thread_rejects_nonexistent_anchor(
        self, connector: AppleMailConnector
    ) -> None:
        """Nonexistent anchor raises MailMessageNotFoundError."""
        from apple_mail_mcp.exceptions import MailMessageNotFoundError
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("99999999999")

    def test_get_message(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Real get_message returns a full structured message.

        Chains off search_messages for a real ID. Guards against the
        NSJSONSerialization `|id|` selector-collision bug fixed in #23.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        target_id = matches[0]["id"]
        result = connector.get_message(target_id)

        assert set(result.keys()) >= {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged", "content",
        }
        assert result["id"] == target_id
        assert isinstance(result["subject"], str)
        assert isinstance(result["sender"], str)
        assert isinstance(result["date_received"], str)
        assert isinstance(result["read_status"], bool)
        assert isinstance(result["flagged"], bool)
        assert isinstance(result["content"], str)

    def test_get_message_via_imap(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Issue #72: when account+mailbox are provided AND the account
        has a Keychain entry, get_message uses the IMAP fast path.

        Skips if the test account doesn't have IMAP configured — the
        fallback would still work but we'd be testing the AppleScript
        path again, which test_get_message above already covers.
        """
        from apple_mail_mcp.exceptions import (
            MailKeychainAccessDeniedError,
            MailKeychainEntryNotFoundError,
        )
        from apple_mail_mcp.keychain import get_imap_password

        # Resolve email + skip-if-no-keychain via the same path the
        # connector itself uses for IMAP delegation. Match the skip
        # pattern from test_imap_connector integration tests.
        try:
            _, _, email = connector._resolve_imap_config(test_account)
            get_imap_password(test_account, email)
        except (
            MailKeychainEntryNotFoundError,
            MailKeychainAccessDeniedError,
        ):
            pytest.skip(
                f"No Keychain entry for {test_account!r} — IMAP path "
                f"can't be exercised. Run `apple-mail-mcp setup-imap` first."
            )

        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")
        target_id = matches[0]["id"]

        # Same shape as the AppleScript path — callers don't have to
        # special-case which dispatch fired.
        result = connector.get_message(
            target_id, account=test_account, mailbox="INBOX",
        )
        assert set(result.keys()) >= {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged", "content",
        }
        assert isinstance(result["content"], str)

        # headers_only=True: same shape, content empty. Useful for
        # preview-style callers.
        head_only = connector.get_message(
            target_id, account=test_account, mailbox="INBOX",
            headers_only=True,
        )
        assert set(head_only.keys()) >= {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged", "content",
        }
        assert head_only["content"] == ""

    def test_get_attachments(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Real get_attachments returns a structured list (possibly empty).

        Chains off search_messages for a real ID. Guards against the
        NSJSONSerialization `|size|` selector-collision bug fixed in #23.
        Empty list is a valid pass — most messages have no attachments.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        result = connector.get_attachments(matches[0]["id"])
        assert isinstance(result, list)
        for att in result:
            assert set(att.keys()) >= {"name", "mime_type", "size", "downloaded"}
            assert isinstance(att["name"], str)
            assert isinstance(att["mime_type"], str)
            assert isinstance(att["size"], int)
            assert isinstance(att["downloaded"], bool)

    def test_get_attachments_via_imap(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Issue #73: when account+mailbox are provided AND the account
        has a Keychain entry, get_attachments uses BODYSTRUCTURE.

        Skips if the test account doesn't have IMAP configured — the
        fallback would still work but we'd be testing the AppleScript
        path again, which test_get_attachments above already covers.
        """
        from apple_mail_mcp.exceptions import (
            MailKeychainAccessDeniedError,
            MailKeychainEntryNotFoundError,
        )
        from apple_mail_mcp.keychain import get_imap_password

        try:
            _, _, email = connector._resolve_imap_config(test_account)
            get_imap_password(test_account, email)
        except (
            MailKeychainEntryNotFoundError,
            MailKeychainAccessDeniedError,
        ):
            pytest.skip(
                f"No Keychain entry for {test_account!r} — IMAP path "
                f"can't be exercised. Run `apple-mail-mcp setup-imap` first."
            )

        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")
        target_id = matches[0]["id"]

        # Same shape as the AppleScript path, modulo `downloaded` which
        # is always False on the IMAP path (BODYSTRUCTURE doesn't expose
        # Mail.app's local cache state).
        result = connector.get_attachments(
            target_id, account=test_account, mailbox="INBOX",
        )
        assert isinstance(result, list)
        for att in result:
            assert set(att.keys()) >= {"name", "mime_type", "size", "downloaded"}
            assert isinstance(att["name"], str)
            assert isinstance(att["mime_type"], str)
            assert isinstance(att["size"], int)
            # Documented divergence — IMAP path always reports False.
            assert att["downloaded"] is False


def _first_address_of(connector: AppleMailConnector, account: str) -> str:
    """The test account's first address, lower-cased, or skip."""
    match = next(
        (a for a in connector.list_accounts() if a["name"] == account), None
    )
    if not match:
        pytest.skip(f"test account {account!r} not found")
    emails = match.get("email_addresses") or []
    if not emails:
        pytest.skip(f"test account {account!r} has no email addresses")
    return str(emails[0]).lower()


class TestDraftsLifecycleIntegration:
    """Integration tests for the drafts lifecycle (#134).

    These exercise the connector primitives against real Mail.app —
    create_draft / get_draft_state / extract_draft_attachments /
    delete_draft. update_draft is server-layer orchestration (delete +
    recreate) so it is covered there; here we verify the AppleScript
    primitives that update_draft composes.

    Each test cleans up its own drafts.
    """

    @pytest.fixture
    def anchor_message_id(
        self, connector: AppleMailConnector, test_account: str
    ) -> str:
        """Return Mail.app's internal id of the newest INBOX message —
        used as a seed for reply / forward tests.

        Note: search_messages returns the RFC 5322 Message-ID, but
        create_draft's seed lookup uses Mail's internal numeric id
        (`whose id is`). Fetch via osascript directly to keep the
        integration test self-contained.
        """
        import subprocess
        script = f'''
        tell application "Mail"
            set acc to first account whose name is "{test_account}"
            set mb to first mailbox of acc whose name is "INBOX"
            if (count of messages of mb) is 0 then return ""
            return id of (item 1 of messages of mb) as text
        end tell
        '''
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
        seed_id = result.stdout.strip()
        if not seed_id:
            pytest.skip("test account has no INBOX messages to anchor on")
        return seed_id

    def test_fresh_save_then_read_state_then_delete(
        self, connector: AppleMailConnector
    ) -> None:
        result = connector.create_draft(
            seed="new",
            to=["test1@example.com"],
            cc=["test2@example.com"],
            subject="ZZZ-AMM-INTEG-FRESH",
            body="integration fresh body",
        )
        draft_id = result["draft_id"]
        assert draft_id, "create_draft should return a non-empty draft_id"

        try:
            state = connector.get_draft_state(draft_id)
            assert state["to"] == ["test1@example.com"]
            assert state["cc"] == ["test2@example.com"]
            assert state["subject"] == "ZZZ-AMM-INTEG-FRESH"
            assert "integration fresh body" in state["body"]
            # Fresh draft has no threading headers.
            assert state["in_reply_to"] == ""
        finally:
            connector.delete_draft(draft_id)

    def test_reply_save_preserves_threading_headers(
        self,
        connector: AppleMailConnector,
        anchor_message_id: str,
    ) -> None:
        result = connector.create_draft(
            seed="reply",
            seed_id=anchor_message_id,
            body="ZZZ-AMM-INTEG-REPLY-BODY",
        )
        draft_id = result["draft_id"]
        assert draft_id

        try:
            state = connector.get_draft_state(draft_id)
            # Threading header populated by Mail.app's reply primitive.
            assert state["in_reply_to"], "reply must have In-Reply-To"
            # Subject auto-prefixed by Mail.
            assert state["subject"].startswith("Re:"), \
                f"expected Re: prefix; got {state['subject']!r}"
            # User body replaces Mail's auto-quote (per design tradeoff:
            # auto-quote isn't readable from outgoing-msg-ref before save).
            assert "ZZZ-AMM-INTEG-REPLY-BODY" in state["body"]
        finally:
            connector.delete_draft(draft_id)

    def test_attachment_extraction_round_trip(
        self,
        connector: AppleMailConnector,
        tmp_path: Path,
    ) -> None:
        """Verify the preserve-on-None pipeline works: attach a file,
        save as draft, extract via the connector, content matches."""
        original = tmp_path / "src" / "report.pdf"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"%PDF-FAKE-INTEG-CONTENT")

        result = connector.create_draft(
            seed="new",
            to=["target@example.com"],
            subject="ZZZ-AMM-INTEG-ATTACH",
            body="see attached",
            attachment_paths=[original],
        )
        draft_id = result["draft_id"]

        try:
            state = connector.get_draft_state(draft_id)
            assert "report.pdf" in state["attachment_names"]

            extract_dir = tmp_path / "extract"
            extract_dir.mkdir()
            extracted = connector.extract_draft_attachments(
                draft_id, state["attachment_names"], extract_dir
            )
            assert len(extracted) == 1
            assert extracted[0].is_file()
            # Content must match the original byte-for-byte.
            assert extracted[0].read_bytes() == b"%PDF-FAKE-INTEG-CONTENT"
        finally:
            connector.delete_draft(draft_id)

    def test_state_reads_the_sender_back(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """A draft saved from the test account reports that account's
        address as its sender. This is what update_draft carries over."""
        address = _first_address_of(connector, test_account)
        result = connector.create_draft(
            seed="new",
            to=["target@example.com"],
            subject="ZZZ-AMM-INTEG-SENDER",
            body="whose draft is this",
            from_account=test_account,
        )
        draft_id = result["draft_id"]
        try:
            state = connector.get_draft_state(draft_id)
            assert address in state["sender"].lower(), state["sender"]
        finally:
            connector.delete_draft(draft_id)

    def test_state_reads_the_account_back(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """A draft saved from the test account reports that account as
        the one it sits in, resolved through Mail's aggregate drafts
        mailbox. This is what test mode confines draft_delete,
        draft_update and draft_send with."""
        result = connector.create_draft(
            seed="new",
            to=["target@example.com"],
            subject="ZZZ-AMM-INTEG-ACCOUNT",
            body="whose draft is this",
            from_account=test_account,
        )
        draft_id = result["draft_id"]
        try:
            state = connector.get_draft_state(draft_id)
            assert state["account"] == test_account, state["account"]
        finally:
            connector.delete_draft(draft_id)

    def test_update_keeps_the_draft_in_its_account(
        self,
        connector: AppleMailConnector,
        test_account: str,
        monkeypatch: MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Server-layer update_draft, against real Mail: a draft saved from
        the test account and then updated without naming an account is
        rebuilt in that account, not in Mail's default. Run with
        MAIL_TEST_ACCOUNT set to an account that is not Mail's default
        sender for this to prove anything beyond the read-back.

        A draft saved with a sender that is not Mail's default is re-saved
        by Mail under a new id some 10-20 s later (measured on both test
        accounts, docs/research/icloud-draft-resync.md); this test runs
        inside that window and one class run on the iCloud account failed
        with the error text not captured. Passes 3/3 alone on iCloud and
        8/8 for the class on both accounts otherwise."""
        import asyncio

        from apple_mail_mcp import server
        from apple_mail_mcp.exceptions import MailDraftNotFoundError

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        monkeypatch.setattr(server, "mail", connector)
        address = _first_address_of(connector, test_account)

        created = asyncio.run(server.create_draft(
            to=["target@example.com"],
            subject="ZZZ-AMM-INTEG-UPDATE-SENDER",
            body="v1",
            from_account=test_account,
        ))
        assert created["success"] is True, created
        draft_id = created["draft_id"]
        new_draft_id = ""
        try:
            updated = asyncio.run(server.update_draft(draft_id=draft_id, body="v2"))
            assert updated["success"] is True, updated
            new_draft_id = updated["draft_id"]
            state = connector.get_draft_state(new_draft_id)
            assert "v2" in state["body"]
            assert address in state["sender"].lower(), state["sender"]
            # The new draft was found by diffing Drafts ids while the old
            # one still existed; the old one is then gone, cleanly.
            assert "warning" not in updated, updated
            with pytest.raises(MailDraftNotFoundError):
                connector.get_draft_state(draft_id)
        finally:
            for did in (new_draft_id, draft_id):
                if did:
                    try:
                        connector.delete_draft(did)
                    except Exception:
                        pass

    def test_delete_draft_removes_from_drafts_mailbox(
        self,
        connector: AppleMailConnector,
    ) -> None:
        import time

        from apple_mail_mcp.exceptions import MailDraftNotFoundError

        result = connector.create_draft(
            seed="new",
            to=["x@example.com"],
            subject="ZZZ-AMM-INTEG-DELETE",
            body="delete me",
        )
        draft_id = result["draft_id"]
        assert connector.delete_draft(draft_id) is True

        # IMAP sync lag: the delete returns synchronously but the
        # Drafts mailbox enumeration can take several seconds to
        # reflect the move. Poll briefly before asserting.
        for _ in range(20):
            try:
                connector.get_draft_state(draft_id)
                time.sleep(0.5)
            except MailDraftNotFoundError:
                return  # success
        pytest.fail("draft still queryable 10s after delete")

    def test_delete_mailbox_via_imap_round_trip(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#162: create -> delete via IMAP -> verify gone via direct IMAP query.

        Uses direct IMAP listing for verification because Mail.app's local
        mailbox-list cache lags IMAP server changes by minutes; the truth
        is on the server."""
        import uuid as _uuid

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        fixture = f"ZZZ-AMM-DEL-INT-{_uuid.uuid4().hex[:8]}"
        assert connector.create_mailbox(account=test_account, name=fixture)

        try:
            count = connector.delete_mailbox(
                account=test_account, name=fixture
            )
            assert count == 0  # empty fixture
        except Exception:
            # Best-effort cleanup if delete itself fails so we don't orphan.
            try:
                connector.delete_mailbox(
                    account=test_account, name=fixture, delete_messages=True
                )
            except Exception:
                pass
            raise

        # Verify via direct IMAP — Mail.app's view lags
        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)
        client = IMAPClient(host, port=port, ssl=True, timeout=30)
        client.login(email, pw)
        try:
            folders = {f[2] for f in client.list_folders()}
        finally:
            client.logout()
        assert fixture not in folders, f"{fixture} still on IMAP server after delete"

    def test_update_message_move_via_imap_round_trip(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#149: move-only update_message → IMAP MOVE → verify via direct
        IMAP. Mail.app's mailbox view lags IMAP server changes; the
        truth lives on the server."""
        import uuid as _uuid
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        suffix = _uuid.uuid4().hex[:8]
        src = f"ZZZ-AMM-MV-SRC-{suffix}"
        dst = f"ZZZ-AMM-MV-DST-{suffix}"
        msg_id_local = f"{_uuid.uuid4().hex}@apple-mail-mcp-test.invalid"
        bracketed = f"<{msg_id_local}>"

        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)

        assert connector.create_mailbox(account=test_account, name=src)
        assert connector.create_mailbox(account=test_account, name=dst)

        try:
            # APPEND a synthetic message into source via direct IMAP — this
            # gives us a known Message-ID without going through Mail.app.
            now = format_datetime(datetime.now(tz=timezone.utc))
            raw = (
                f"From: sender@apple-mail-mcp-test.invalid\r\n"
                f"To: rcpt@apple-mail-mcp-test.invalid\r\n"
                f"Subject: AMM #149 IMAP move test\r\n"
                f"Date: {now}\r\n"
                f"Message-ID: {bracketed}\r\n"
                f"\r\n"
                f"body\r\n"
            ).encode()

            append_client = IMAPClient(host, port=port, ssl=True, timeout=30)
            append_client.login(email, pw)
            try:
                append_client.append(src, raw)
            finally:
                append_client.logout()

            # Drive the IMAP move via update_message's move-only branch.
            moved = connector.update_message(
                [msg_id_local],
                destination_mailbox=dst,
                account=test_account,
                source_mailbox=src,
            )
            assert moved == 1

            # Verify via direct IMAP — Mail.app's local view lags.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                verify.select_folder(src, readonly=True)
                src_uids = verify.search(["HEADER", "Message-ID", bracketed])
                verify.select_folder(dst, readonly=True)
                dst_uids = verify.search(["HEADER", "Message-ID", bracketed])
            finally:
                verify.logout()

            assert src_uids == [], f"message still in source after MOVE: {src_uids}"
            assert len(dst_uids) == 1, f"message not in dest after MOVE: {dst_uids}"
        finally:
            # Best-effort cleanup of both fixture mailboxes.
            for name in (src, dst):
                try:
                    connector.delete_mailbox(
                        account=test_account, name=name, delete_messages=True
                    )
                except Exception:
                    pass

    def test_delete_messages_via_imap_round_trip(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#150: delete_messages with account+source_mailbox → IMAP MOVE
        to Trash → verify via direct IMAP that the source is empty and
        the message landed in the account's Trash folder.

        Mail.app's mailbox view lags IMAP server changes; the truth
        lives on the server, so verification uses a direct IMAPClient."""
        import uuid as _uuid
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        suffix = _uuid.uuid4().hex[:8]
        src = f"ZZZ-AMM-DEL-SRC-{suffix}"
        msg_id_local = f"{_uuid.uuid4().hex}@apple-mail-mcp-test.invalid"
        bracketed = f"<{msg_id_local}>"

        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)

        assert connector.create_mailbox(account=test_account, name=src)

        try:
            # APPEND a synthetic message into source via direct IMAP so
            # we have a known Message-ID without going through Mail.app.
            now = format_datetime(datetime.now(tz=timezone.utc))
            raw = (
                f"From: sender@apple-mail-mcp-test.invalid\r\n"
                f"To: rcpt@apple-mail-mcp-test.invalid\r\n"
                f"Subject: AMM #150 IMAP delete test\r\n"
                f"Date: {now}\r\n"
                f"Message-ID: {bracketed}\r\n"
                f"\r\n"
                f"body\r\n"
            ).encode()

            append_client = IMAPClient(host, port=port, ssl=True, timeout=30)
            append_client.login(email, pw)
            try:
                append_client.append(src, raw)
            finally:
                append_client.logout()

            # Drive the IMAP delete via delete_messages.
            deleted = connector.delete_messages(
                [msg_id_local],
                account=test_account,
                source_mailbox=src,
            )
            assert deleted == 1

            # Verify via direct IMAP. Discover the Trash folder the same
            # way the connector did (SPECIAL-USE first, conventional
            # fallback) so this test works on Gmail / iCloud / Fastmail.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                trash_name = None
                conventional = (
                    "Trash", "[Gmail]/Trash",
                    "Deleted Messages", "Deleted Items",
                )
                listing = verify.list_folders()
                for flags, _delim, name in listing:
                    if b"\\Trash" in flags:
                        trash_name = (
                            name.decode("utf-8", errors="replace")
                            if isinstance(name, (bytes, bytearray))
                            else name
                        )
                        break
                if trash_name is None:
                    present = {
                        n.decode("utf-8", errors="replace")
                        if isinstance(n, (bytes, bytearray)) else n
                        for _f, _d, n in listing
                    }
                    for candidate in conventional:
                        if candidate in present:
                            trash_name = candidate
                            break
                assert trash_name is not None, (
                    "Test account has no discoverable Trash folder"
                )

                verify.select_folder(src, readonly=True)
                src_uids = verify.search(["HEADER", "Message-ID", bracketed])
                verify.select_folder(trash_name, readonly=True)
                trash_uids = verify.search(["HEADER", "Message-ID", bracketed])
            finally:
                verify.logout()

            assert src_uids == [], (
                f"message still in source after delete: {src_uids}"
            )
            assert len(trash_uids) >= 1, (
                f"message not in Trash after delete: {trash_uids}"
            )
        finally:
            # Best-effort cleanup of the source fixture mailbox. Don't
            # touch Trash — that's the user's domain.
            try:
                connector.delete_mailbox(
                    account=test_account, name=src, delete_messages=True
                )
            except Exception:
                pass

    def test_update_message_read_status_via_imap_round_trip(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#151: read-only update_message (read_status, no flag/move)
        with account+source_mailbox → IMAP STORE \\Seen → verify via
        direct IMAP that the flag was set, then flip to unread and
        verify it was cleared.

        Mail.app's mailbox view lags IMAP server changes; verification
        uses a direct IMAPClient against the source mailbox."""
        import uuid as _uuid
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        suffix = _uuid.uuid4().hex[:8]
        src = f"ZZZ-AMM-READ-SRC-{suffix}"
        msg_id_local = f"{_uuid.uuid4().hex}@apple-mail-mcp-test.invalid"
        bracketed = f"<{msg_id_local}>"

        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)

        assert connector.create_mailbox(account=test_account, name=src)

        try:
            # APPEND a synthetic message into source via direct IMAP
            # explicitly without \Seen.
            now = format_datetime(datetime.now(tz=timezone.utc))
            raw = (
                f"From: sender@apple-mail-mcp-test.invalid\r\n"
                f"To: rcpt@apple-mail-mcp-test.invalid\r\n"
                f"Subject: AMM #151 IMAP read-status test\r\n"
                f"Date: {now}\r\n"
                f"Message-ID: {bracketed}\r\n"
                f"\r\n"
                f"body\r\n"
            ).encode()

            append_client = IMAPClient(host, port=port, ssl=True, timeout=30)
            append_client.login(email, pw)
            try:
                # flags=[] explicitly: ensure no \Seen at start.
                append_client.append(src, raw, flags=[])
            finally:
                append_client.logout()

            # Mark read via IMAP fast path.
            marked = connector.update_message(
                [msg_id_local],
                read_status=True,
                account=test_account,
                source_mailbox=src,
            )
            assert marked == 1

            # Verify \Seen is now present.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                verify.select_folder(src, readonly=True)
                uids = verify.search(["HEADER", "Message-ID", bracketed])
                assert len(uids) == 1, f"message missing after mark-read: {uids}"
                flags_after_read = verify.get_flags(uids)
                assert b"\\Seen" in flags_after_read[uids[0]], (
                    f"\\Seen not set after mark-read: {flags_after_read}"
                )
            finally:
                verify.logout()

            # Flip to unread.
            unmarked = connector.update_message(
                [msg_id_local],
                read_status=False,
                account=test_account,
                source_mailbox=src,
            )
            assert unmarked == 1

            # Verify \Seen is now absent.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                verify.select_folder(src, readonly=True)
                uids = verify.search(["HEADER", "Message-ID", bracketed])
                flags_after_unread = verify.get_flags(uids)
                assert b"\\Seen" not in flags_after_unread[uids[0]], (
                    f"\\Seen not cleared after mark-unread: {flags_after_unread}"
                )
            finally:
                verify.logout()
        finally:
            try:
                connector.delete_mailbox(
                    account=test_account, name=src, delete_messages=True
                )
            except Exception:
                pass

    def test_update_message_flagged_status_via_imap_round_trip(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#152: flag-only update_message (flagged, no flag_color/read/move)
        with account+source_mailbox → IMAP STORE \\Flagged → verify via
        direct IMAP that the flag was set, then flip to unflagged and
        verify it was cleared."""
        import uuid as _uuid
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        suffix = _uuid.uuid4().hex[:8]
        src = f"ZZZ-AMM-FLAG-SRC-{suffix}"
        msg_id_local = f"{_uuid.uuid4().hex}@apple-mail-mcp-test.invalid"
        bracketed = f"<{msg_id_local}>"

        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)

        assert connector.create_mailbox(account=test_account, name=src)

        try:
            now = format_datetime(datetime.now(tz=timezone.utc))
            raw = (
                f"From: sender@apple-mail-mcp-test.invalid\r\n"
                f"To: rcpt@apple-mail-mcp-test.invalid\r\n"
                f"Subject: AMM #152 IMAP flag round-trip test\r\n"
                f"Date: {now}\r\n"
                f"Message-ID: {bracketed}\r\n"
                f"\r\n"
                f"body\r\n"
            ).encode()

            append_client = IMAPClient(host, port=port, ssl=True, timeout=30)
            append_client.login(email, pw)
            try:
                # Explicitly NO flags at start.
                append_client.append(src, raw, flags=[])
            finally:
                append_client.logout()

            # Set the flag via IMAP fast path.
            marked = connector.update_message(
                [msg_id_local],
                flagged=True,
                account=test_account,
                source_mailbox=src,
            )
            assert marked == 1

            # Verify \Flagged is now present.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                verify.select_folder(src, readonly=True)
                uids = verify.search(["HEADER", "Message-ID", bracketed])
                assert len(uids) == 1
                flags_after_set = verify.get_flags(uids)
                assert b"\\Flagged" in flags_after_set[uids[0]], (
                    f"\\Flagged not set after flagged=True: {flags_after_set}"
                )
            finally:
                verify.logout()

            # Clear the flag.
            unmarked = connector.update_message(
                [msg_id_local],
                flagged=False,
                account=test_account,
                source_mailbox=src,
            )
            assert unmarked == 1

            # Verify \Flagged is now absent.
            verify = IMAPClient(host, port=port, ssl=True, timeout=30)
            verify.login(email, pw)
            try:
                verify.select_folder(src, readonly=True)
                uids = verify.search(["HEADER", "Message-ID", bracketed])
                flags_after_clear = verify.get_flags(uids)
                assert b"\\Flagged" not in flags_after_clear[uids[0]], (
                    f"\\Flagged not cleared after flagged=False: {flags_after_clear}"
                )
            finally:
                verify.logout()
        finally:
            try:
                connector.delete_mailbox(
                    account=test_account, name=src, delete_messages=True
                )
            except Exception:
                pass

    def test_search_messages_returns_rfc_message_id_via_applescript(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#148: AppleScript search rows carry both `id` (Mail.app
        internal numeric) and `rfc_message_id` (RFC 5322, bracketless)
        — verified end-to-end against real Mail.app.

        Cost should be sub-second per mailbox per #147's probe (the
        `message id of msg` direct-property read is cheap)."""
        import uuid as _uuid
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from imapclient import IMAPClient

        from apple_mail_mcp.keychain import get_imap_password

        suffix = _uuid.uuid4().hex[:8]
        src = f"ZZZ-AMM-DUAL-EMIT-{suffix}"
        msg_id_local = f"{_uuid.uuid4().hex}@apple-mail-mcp-test.invalid"
        bracketed = f"<{msg_id_local}>"

        host, port, email = connector._resolve_imap_config(test_account)
        pw = get_imap_password(test_account, email)

        assert connector.create_mailbox(account=test_account, name=src)

        try:
            now = format_datetime(datetime.now(tz=timezone.utc))
            raw = (
                f"From: sender@apple-mail-mcp-test.invalid\r\n"
                f"To: rcpt@apple-mail-mcp-test.invalid\r\n"
                f"Subject: AMM #148 dual-emit test\r\n"
                f"Date: {now}\r\n"
                f"Message-ID: {bracketed}\r\n"
                f"\r\n"
                f"body\r\n"
            ).encode()

            append_client = IMAPClient(host, port=port, ssl=True, timeout=30)
            append_client.login(email, pw)
            try:
                append_client.append(src, raw, flags=[])
            finally:
                append_client.logout()

            # Force the AppleScript path so we exercise the dual-emit
            # we just added (IMAP path's dual-emit is identical-id and
            # already covered by unit tests).
            connector._imap_failure_until[test_account] = (
                __import__("time").monotonic() + 60
            )

            # Mail.app's IMAP sync may lag the APPEND. Poll up to ~30s.
            import time as _time
            for _ in range(10):
                rows = connector.search_messages(
                    account=test_account, mailbox=src, limit=10,
                )
                match = [
                    r for r in rows
                    if r.get("rfc_message_id") == msg_id_local
                ]
                if match:
                    break
                _time.sleep(3)
            else:
                raise AssertionError(
                    "Mail.app never surfaced the APPENDed message via "
                    "AppleScript search within 30s"
                )

            row = match[0]
            assert "id" in row, "missing path-native `id`"
            assert "rfc_message_id" in row, "missing dual-emit `rfc_message_id`"
            assert row["rfc_message_id"] == msg_id_local, (
                f"rfc_message_id wrong: {row['rfc_message_id']!r}"
            )
            # AppleScript path: `id` is Mail.app's internal numeric id,
            # NOT equal to the RFC id.
            assert row["id"] != row["rfc_message_id"], (
                "AppleScript path should yield divergent id and "
                "rfc_message_id; got equal values"
            )
        finally:
            try:
                connector.delete_mailbox(
                    account=test_account, name=src, delete_messages=True
                )
            except Exception:
                pass

    def test_update_mailbox_renames_in_place(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#102: full create -> rename via update_mailbox -> verify cycle.

        Doesn't test delete (Mail.app's AppleScript dictionary doesn't
        expose a working delete primitive — tracked as #162). This means
        the test leaves the renamed fixture mailbox behind for cleanup
        via Mail.app's GUI."""
        import uuid as _uuid
        fixture = f"ZZZ-AMM-RENAME-INT-{_uuid.uuid4().hex[:8]}"
        new_name = f"{fixture}-renamed"

        # Create the fixture.
        assert connector.create_mailbox(account=test_account, name=fixture)

        # Rename via update_mailbox.
        assert connector.update_mailbox(
            account=test_account, name=fixture, new_name=new_name
        )

        # Verify via list_mailboxes — old name gone, new name present.
        names = {m["name"] for m in connector.list_mailboxes(test_account)}
        assert new_name in names, (
            f"renamed mailbox {new_name!r} not in listing"
        )
        assert fixture not in names, (
            f"old name {fixture!r} still in listing"
        )

    def test_from_account_emits_display_name_sender(
        self,
        connector: AppleMailConnector,
        test_account: str,
    ) -> None:
        """#158: when the test account has a full_name configured, a draft
        created with from_account=<account> should land with a 'From'
        header in `Display Name <email>` form.

        Skips when the account has no full_name set (graceful fallback to
        bare email is exercised by unit tests; integration covers the
        happy path)."""
        accounts = connector.list_accounts()
        match = next(
            (a for a in accounts if a["name"] == test_account), None
        )
        if not match:
            pytest.skip(f"test account {test_account!r} not found")
        full_name = (match.get("full_name") or "").strip()
        if not full_name:
            pytest.skip(
                f"test account {test_account!r} has no full_name "
                f"configured — display-name path is unverifiable here"
            )
        emails = match.get("email_addresses") or []
        if not emails:
            pytest.skip(f"test account {test_account!r} has no email addresses")

        result = connector.create_draft(
            seed="new",
            to=["target@example.com"],
            subject="ZZZ-AMM-INTEG-DISPLAY-NAME",
            body="checking display-name sender",
            from_account=test_account,
        )
        draft_id = result["draft_id"]

        try:
            # Read the draft's headers via osascript and confirm the
            # From header includes both the display name and email.
            import subprocess as _subprocess
            script = f'''
            tell application "Mail"
                repeat with acc in accounts
                    try
                        repeat with mb in mailboxes of acc
                            if name of mb contains "Drafts" then
                                repeat with d in messages of mb
                                    if (id of d as text) is "{draft_id}" then
                                        return sender of d
                                    end if
                                end repeat
                            end if
                        end repeat
                    end try
                end repeat
                return ""
            end tell
            '''
            r = _subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True, text=True, timeout=30,
            )
            sender_value = r.stdout.strip()
            assert full_name in sender_value, (
                f"sender {sender_value!r} should contain full_name "
                f"{full_name!r}"
            )
            assert emails[0] in sender_value, (
                f"sender {sender_value!r} should contain email "
                f"{emails[0]!r}"
            )
        finally:
            connector.delete_draft(draft_id)


class TestErrorHandling:
    """Test error handling with real Mail.app."""

    def test_nonexistent_account(self, connector: AppleMailConnector) -> None:
        """Test error when account doesn't exist."""
        from apple_mail_mcp.exceptions import MailAccountNotFoundError

        with pytest.raises(MailAccountNotFoundError):
            connector.list_mailboxes("NonExistentAccount12345")

    def test_nonexistent_mailbox(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Test error when mailbox doesn't exist."""
        from apple_mail_mcp.exceptions import MailMailboxNotFoundError

        with pytest.raises(MailMailboxNotFoundError):
            connector.search_messages(
                account=test_account,
                mailbox="NonExistentMailbox12345"
            )


class TestRuleCRUDIntegration:
    """End-to-end CRUD on a test-prefixed Mail.app rule.

    Self-cleaning: always deletes the test rule at the end via try/finally,
    even if intermediate assertions fail. Idempotent: a leftover from a
    previous failed run is detected and removed at the start.

    Refers to a rule whose name starts with '[apple-mail-mcp-test]' —
    this is the test prefix the safety gate uses, but the connector
    itself doesn't enforce it. We use a recognizable name so manual
    cleanup is easy if all else fails.
    """

    TEST_RULE_NAME = "[apple-mail-mcp-test] integration test rule"

    def _delete_test_rule_if_present(
        self, connector: AppleMailConnector
    ) -> None:
        """Find and delete any rule with TEST_RULE_NAME, regardless of state."""
        for r in connector.list_rules():
            if r["name"] == self.TEST_RULE_NAME:
                connector.delete_rule(r["index"])

    def test_full_crud_cycle(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Create → list → enable-toggle → update → delete a test rule."""
        # Pre-clean: in case a previous run left a leftover.
        self._delete_test_rule_if_present(connector)

        try:
            # 1. CREATE
            new_index = connector.create_rule(
                name=self.TEST_RULE_NAME,
                conditions=[
                    {
                        "field": "subject",
                        "operator": "contains",
                        "value": "this-string-will-not-match-anything-zzz",
                    }
                ],
                actions={"mark_read": True},
                match_logic="all",
                enabled=True,
            )
            assert new_index >= 1

            # 2. LIST: verify it's there with expected index, name, enabled.
            rules = connector.list_rules()
            test_rule = next(
                (r for r in rules if r["name"] == self.TEST_RULE_NAME),
                None,
            )
            assert test_rule is not None, (
                f"Created rule not found in list_rules output. "
                f"Saw: {[r['name'] for r in rules]}"
            )
            assert test_rule["index"] == new_index
            assert test_rule["enabled"] is True

            # 3. SET_RULE_ENABLED: toggle off.
            connector.set_rule_enabled(new_index, enabled=False)
            rules = connector.list_rules()
            test_rule = next(
                r for r in rules if r["name"] == self.TEST_RULE_NAME
            )
            assert test_rule["enabled"] is False

            # 4. UPDATE: rename + re-enable + change actions + match_logic.
            # NOTE: `conditions=` deliberately not exercised here — Mail.app
            # on macOS Tahoe has a recursion bug in
            # removeFromCriteriaAtIndex: that crashes Mail on any path that
            # removes a rule condition. The connector refuses `conditions=`
            # with MailUnsupportedRuleActionError; see test_mail_connector
            # for unit coverage of the refusal.
            renamed = self.TEST_RULE_NAME + " v2"
            connector.update_rule(
                rule_index=new_index,
                name=renamed,
                enabled=True,
                match_logic="any",
                actions={"mark_flagged": True, "flag_color": "red"},
            )
            rules = connector.list_rules()
            updated_rule = next(
                (r for r in rules if r["name"] == renamed), None
            )
            assert updated_rule is not None, (
                f"Updated rule with new name not found. "
                f"Saw: {[r['name'] for r in rules]}"
            )
            assert updated_rule["enabled"] is True

            # Restore the original name so cleanup finds it.
            connector.update_rule(rule_index=updated_rule["index"], name=self.TEST_RULE_NAME)

            # 5. DELETE: remove it. delete_rule returns the rule's name.
            test_rule = next(
                r for r in connector.list_rules()
                if r["name"] == self.TEST_RULE_NAME
            )
            deleted_name = connector.delete_rule(test_rule["index"])
            assert deleted_name == self.TEST_RULE_NAME

            # 6. VERIFY GONE
            rules_after = connector.list_rules()
            names_after = [r["name"] for r in rules_after]
            assert self.TEST_RULE_NAME not in names_after, (
                f"Test rule still in list after delete: {names_after}"
            )
        finally:
            # Defensive cleanup if anything above raised.
            self._delete_test_rule_if_present(connector)


class TestTemplateIntegration:
    """End-to-end: save a template referencing reply-context placeholders,
    render it against a real message from the test inbox, verify the
    auto-fills came through.

    Storage isolation: redirects APPLE_MAIL_MCP_HOME at tmp_path to avoid
    touching the real templates directory.
    """

    def test_round_trip_with_real_message_data(
        self,
        connector: AppleMailConnector,
        test_account: str,
        tmp_path: Path,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Save → reload from disk → render against real message data.

        Pulls subject and sender from search_messages (which already
        returns those fields, so we don't depend on get_message — that
        path has a pre-existing AppleScript-quoting bug on UUID-style
        IDs that's unrelated to this feature). Auto-fill behavior is
        unit-tested with mocked get_message in test_mail_connector.
        """
        from email.utils import parseaddr

        from apple_mail_mcp.templates import Template, TemplateStore

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        store = TemplateStore()

        # Try a few likely mailboxes — the test account may have an
        # empty INBOX but messages elsewhere.
        msg: dict | None = None
        for mb in ("INBOX", "Archive", "Sent Messages"):
            try:
                matches = connector.search_messages(
                    account=test_account, mailbox=mb, limit=1
                )
            except Exception:
                continue
            if matches:
                msg = matches[0]
                break
        if msg is None:
            pytest.skip("no messages found in test account")

        # Save a template that exercises every reply-context placeholder.
        store.save(
            Template(
                name="integration-reply",
                subject="Re: {original_subject}",
                body=(
                    "Hi {recipient_name},\n\n"
                    "Thanks for reaching out (writing on {today}).\n"
                ),
            )
        )

        # Build the var dict the same way auto_template_vars would,
        # but from search_messages data so we sidestep the get_message
        # quoting bug.
        from datetime import date

        sender_field = str(msg.get("sender") or "")
        display_name, email_addr = parseaddr(sender_field)
        recipient_email = email_addr or sender_field
        recipient_name = display_name or recipient_email
        original_subject = str(msg.get("subject") or "")
        today = date.today().isoformat()

        loaded = store.get("integration-reply")
        rendered = loaded.render(
            {
                "recipient_name": recipient_name,
                "recipient_email": recipient_email,
                "original_subject": original_subject,
                "today": today,
            }
        )
        assert rendered["subject"] == f"Re: {original_subject}"
        assert recipient_name in rendered["body"]
        assert today in rendered["body"]


class TestFindMessageByMessageIdIntegration:
    """Real-Mail.app round-trip for ``find_message_by_message_id``.

    Unit tests mock ``_run_applescript`` and so cannot catch mismatches
    between the form the AppleScript ``whose`` clause sends and the form
    Mail.app's ``message id`` property actually stores. This integration
    test asserts the round-trip works against real storage, with a
    message picked from the test account's INBOX at runtime so the test
    survives any specific Message-ID being deleted.
    """

    def test_find_by_bare_rfc_id_from_search_messages(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """``search_messages`` on the IMAP path emits the bare RFC
        Message-ID in the ``id`` field (per #148). Passing that value
        back through ``find_message_by_message_id`` must resolve to
        Mail's internal id — the contract ``create_draft(reply_to=...)``
        and ``create_draft(forward_of=...)`` rely on for #205.
        """
        rows = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not rows:
            pytest.skip(f"{test_account} INBOX has no messages to test against")

        rfc_id = rows[0].get("rfc_message_id") or rows[0].get("id")
        assert rfc_id, "search_messages row missing rfc_message_id/id"
        # Search results from the IMAP path are bare per #148; this test
        # is specifically about that form.
        if rfc_id.startswith("<") and rfc_id.endswith(">"):
            pytest.skip(
                "search_messages returned a bracketed id — "
                "test_account is not on the IMAP path"
            )

        internal_id = connector.find_message_by_message_id(rfc_id)
        assert internal_id is not None, (
            f"find_message_by_message_id returned None for {rfc_id!r} "
            f"despite the message being present in {test_account}/INBOX"
        )
        assert "@" not in internal_id, (
            "expected Mail's internal numeric id, got something RFC-shaped"
        )

    def test_find_by_bracketed_rfc_id_matches_bare(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Both bracketed and bracketless forms of the same RFC id must
        resolve to the same internal id, so callers (e.g. update_draft
        passing In-Reply-To with brackets, create_draft passing a bare
        seed_id from search_messages) can hand us either form.
        """
        rows = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not rows:
            pytest.skip(f"{test_account} INBOX has no messages to test against")

        rfc_id = rows[0].get("rfc_message_id") or rows[0].get("id")
        assert rfc_id, "search_messages row missing rfc_message_id/id"
        bare = rfc_id[1:-1] if rfc_id.startswith("<") and rfc_id.endswith(">") else rfc_id
        bracketed = f"<{bare}>"

        by_bare = connector.find_message_by_message_id(bare)
        by_bracketed = connector.find_message_by_message_id(bracketed)

        assert by_bare is not None, f"bare {bare!r} did not match"
        assert by_bracketed is not None, f"bracketed {bracketed!r} did not match"
        assert by_bare == by_bracketed, (
            f"bare resolved to {by_bare!r}, bracketed to {by_bracketed!r} — "
            "compound clause should yield the same internal id"
        )

    def test_create_draft_reply_round_trip_with_bare_rfc_id(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """End-to-end #205 contract: an id from search_messages round-trips
        through create_draft(seed='reply') without MailMessageNotFoundError.
        Cleans up the created draft.
        """
        rows = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not rows:
            pytest.skip(f"{test_account} INBOX has no messages to test against")

        rfc_id = rows[0].get("rfc_message_id") or rows[0].get("id")
        assert rfc_id
        if rfc_id.startswith("<") and rfc_id.endswith(">"):
            rfc_id = rfc_id[1:-1]

        result = connector.create_draft(
            seed="reply",
            seed_id=rfc_id,
            body="Integration-test draft — safe to discard.",
        )
        draft_id = result.get("draft_id") if isinstance(result, dict) else None
        assert draft_id, f"no draft_id in create_draft result: {result!r}"
        try:
            # Lightweight assertion — the draft exists; we don't introspect
            # its In-Reply-To header here because update_draft / get_draft
            # paths have their own coverage. We only assert the seed
            # resolution worked.
            pass
        finally:
            connector.delete_draft(draft_id)


class TestAttachmentPropertyGuardIntegration:
    """Real-Mail.app coverage for the per-property attachment guard.

    Unit tests mock ``_run_applescript`` and therefore cannot catch an
    AppleScript bug — that is exactly how the -10000 MIME type failure
    survived. These assert machine-INDEPENDENT invariants: we do not
    assume any particular property fails here (on the machine where this
    was written, 140/140 attachments had an unreadable MIME type; another
    machine may have none). What must hold everywhere is that an
    unreadable property degrades ONE FIELD and is reported, never costing
    us the attachment.

    Evidence and probes: docs/research/attachment-property-10000.md
    """

    def _first_message_with_attachments(
        self, connector: AppleMailConnector, test_account: str
    ) -> str | None:
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", has_attachment=True, limit=1
        )
        return str(matches[0]["id"]) if matches else None

    def test_attachments_survive_an_unreadable_property(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """A message Mail reports as having attachments must enumerate to a
        non-empty list, with a readable name per record."""
        msg_id = self._first_message_with_attachments(connector, test_account)
        if msg_id is None:
            pytest.skip("test inbox has no messages with attachments")

        attachments, warnings = connector._enumerate_attachments_for_message(
            msg_id
        )

        assert attachments, (
            f"message {msg_id} is attachment-bearing per Mail's own filter "
            f"but enumerated to an empty list; warnings={warnings}"
        )
        for att in attachments:
            assert set(att.keys()) >= {
                "name", "mime_type", "size", "downloaded"
            }
            assert att["name"], "attachment name must not be blank"

        # Any degradation must name the property it lost — never a bare
        # 'enumeration failed' that discards the whole walk.
        for w in warnings:
            assert "attachment property" in w, (
                f"whole-walk failure resurfaced on {msg_id}: {w}"
            )

    def test_save_refuses_to_replace_what_is_already_there(
        self, connector: AppleMailConnector, test_account: str, tmp_path: Path
    ) -> None:
        """Mail's ``save`` replaces an existing file silently (probed live
        2026-09-11), so the refusal has to come from us: a second save
        into the same directory is refused with nothing written, and
        overwrite=True is the one way through."""
        msg_id = self._first_message_with_attachments(connector, test_account)
        if msg_id is None:
            pytest.skip("test inbox has no messages with attachments")

        saved, _ = connector.save_attachments(msg_id, tmp_path)
        assert saved >= 1
        first = sorted(p for p in tmp_path.iterdir() if p.is_file())[0]
        first.write_bytes(b"SENTINEL")

        with pytest.raises(FileExistsError, match=first.name):
            connector.save_attachments(msg_id, tmp_path)
        assert first.read_bytes() == b"SENTINEL", "a refused save must write nothing"

        saved_again, _ = connector.save_attachments(msg_id, tmp_path, overwrite=True)
        assert saved_again == saved
        assert first.read_bytes() != b"SENTINEL", "overwrite=True must replace the file"

    def test_save_succeeds_despite_property_warnings(
        self, connector: AppleMailConnector, test_account: str, tmp_path: Path
    ) -> None:
        """Files must land on disk even when a property read failed, and the
        reported count must match what is actually written."""
        msg_id = self._first_message_with_attachments(connector, test_account)
        if msg_id is None:
            pytest.skip("test inbox has no messages with attachments")

        attachments, _ = connector._enumerate_attachments_for_message(msg_id)
        # Without this the whole test is vacuous: a broken enumeration
        # returns [] and every count assertion below becomes 0 == 0.
        assert attachments, "enumeration returned nothing to save"
        saved, warnings = connector.save_attachments(msg_id, tmp_path)

        on_disk = [p for p in tmp_path.iterdir() if p.is_file()]
        assert saved == len(attachments), (
            f"expected to save all {len(attachments)} attachments of "
            f"{msg_id}, saved {saved}; warnings={warnings}"
        )
        assert len(on_disk) == saved, (
            f"reported {saved} saved but {len(on_disk)} files on disk — the "
            f"count must reflect reality"
        )
        # Sizes must match the enumerated metadata (proves we saved the
        # real payload, not an empty placeholder).
        by_name = {p.name: p.stat().st_size for p in on_disk}
        for att in attachments:
            if att["name"] in by_name and att["size"]:
                assert by_name[att["name"]] == att["size"], (
                    f"{att['name']}: on-disk {by_name[att['name']]} != "
                    f"reported {att['size']}"
                )

    def test_saved_files_stay_inside_the_target_directory(
        self, connector: AppleMailConnector, test_account: str, tmp_path: Path
    ) -> None:
        """Nothing may be written outside ``save_directory``.

        Pass 2 used to build its destination inside AppleScript as
        ``"<dir>/" & name of att``. That name comes from the message's own
        MIME headers, so the sender controls it, and ``POSIX file`` does
        not normalise the string — the filesystem resolves it at write
        time, so ``..`` escapes the directory. The destination filename is
        now decided in Python and passed in as data.

        This asserts the containment invariant against real Mail and a
        real save. It does NOT prove the traversal is unreachable, because
        it uses whatever names the inbox happens to carry rather than a
        crafted hostile one — see the limitation recorded in
        docs/research/attachment-property-10000.md. What it does catch is
        the whole class of regression where the saved path stops being a
        direct child of the requested directory.
        """
        msg_id = self._first_message_with_attachments(connector, test_account)
        if msg_id is None:
            pytest.skip("test inbox has no messages with attachments")

        # Save into a subdirectory so an escape has somewhere visible to
        # land: anything written to `sentinel_root` is outside the target.
        sentinel_root = tmp_path
        target = sentinel_root / "target"
        target.mkdir()

        attachments, _ = connector._enumerate_attachments_for_message(msg_id)
        assert attachments, "enumeration returned nothing to save"
        saved, warnings = connector.save_attachments(msg_id, target)

        strays = [
            p for p in sentinel_root.iterdir() if p.resolve() != target.resolve()
        ]
        assert not strays, (
            f"save_attachments wrote outside its target directory: "
            f"{[str(p) for p in strays]}; warnings={warnings}"
        )

        for path in target.rglob("*"):
            assert path.parent.resolve() == target.resolve(), (
                f"{path} is not a direct child of {target} — a name with a "
                f"separator reached the destination path"
            )
        assert saved == len(list(target.iterdir())), (
            f"reported {saved} saved but {len(list(target.iterdir()))} files "
            f"in {target}"
        )

    def test_failed_save_reports_a_reason(
        self, connector: AppleMailConnector, test_account: str, tmp_path: Path
    ) -> None:
        """A save that fails must say WHY, never return a bare 0.

        Pass 2 used to wrap each save in an unqualified ``try`` with no
        on-error branch, so a total failure surfaced as ``(0, [])`` — no
        files and no reason. Uses a real, EXISTING but read-only
        directory so the failure happens inside Mail's save, past the
        directory-validation gate.
        """
        msg_id = self._first_message_with_attachments(connector, test_account)
        if msg_id is None:
            pytest.skip("test inbox has no messages with attachments")

        attachments, _ = connector._enumerate_attachments_for_message(msg_id)
        assert attachments, "enumeration returned nothing to save"

        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            saved, warnings = connector.save_attachments(msg_id, readonly)
        finally:
            readonly.chmod(0o755)

        assert saved == 0, "nothing should be written to a read-only dir"
        assert warnings, "a failed save must explain itself, never a bare 0"
        assert any("save failed" in w for w in warnings), (
            f"expected a save-failure warning naming the cause, got {warnings}"
        )


@pytest.mark.integration
class TestDateFilterAppleScriptPath:
    """``date_from`` / ``date_to`` on the AppleScript search path.

    Regression tests for the ISO-coercion defect. AppleScript's ``date``
    coercion does not parse ISO 8601 and does not fail on it:

        osascript -e 'return (date "2026-09-01") as text'
        Monday, October 9, 12169 at 00:00:00

    Every real message is earlier than the year 12169, so
    ``date_from`` excluded everything (always zero rows) and
    ``date_to`` excluded nothing (a silent no-op). Both were silent.

    These call ``_search_messages_applescript`` directly rather than
    ``search_messages`` on purpose: ``search_messages`` tries IMAP first,
    and on an account with an IMAP opt-in the correct IMAP ``SINCE``
    would mask the AppleScript defect entirely.

    Unit tests mock ``_run_applescript`` and structurally cannot see any
    of this, which is why the AppleScript branch stayed broken from
    2026-04-21 while the IMAP branch was tested and correct.
    """

    def test_far_past_date_from_does_not_empty_the_result(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """A cutoff before all mail must change nothing. It returned zero."""
        baseline = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5
        )
        assert baseline, "need a non-empty INBOX for this test to mean anything"

        filtered = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5,
            date_from="2000-01-01",
        )
        assert len(filtered) == len(baseline), (
            f"date_from=2000-01-01 must not exclude mail newer than 2000; "
            f"got {len(filtered)} rows against a baseline of {len(baseline)}"
        )

    def test_far_past_date_to_excludes_everything(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """The mirror defect: date_to excluded nothing at all."""
        baseline = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5
        )
        assert baseline, "need a non-empty INBOX for this test to mean anything"

        filtered = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5,
            date_to="2000-01-01",
        )
        assert filtered == [], (
            f"date_to=2000-01-01 must exclude mail newer than 2000; "
            f"got {len(filtered)} rows"
        )

    def test_a_row_survives_its_own_date_as_both_bounds(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Both bounds are inclusive, checked against a message the search
        itself returned rather than against an assumption about ordering."""
        baseline = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5
        )
        assert baseline, "need a non-empty INBOX for this test to mean anything"

        raw = str(baseline[0]["date_received"])
        try:
            parsed = _dt.datetime.strptime(raw, "%A, %B %d, %Y at %H:%M:%S")
        except ValueError:
            pytest.skip(f"unrecognised Mail date format: {raw!r}")
        day = parsed.date().isoformat()

        from_rows = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=50, date_from=day,
        )
        assert any(r["id"] == baseline[0]["id"] for r in from_rows), (
            f"message {baseline[0]['id']} is dated {day}; date_from={day} "
            f"is inclusive and must still return it"
        )

        to_rows = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=50, date_to=day,
        )
        assert any(r["id"] == baseline[0]["id"] for r in to_rows), (
            f"message {baseline[0]['id']} is dated {day}; date_to={day} "
            f"covers the whole of that day and must still return it"
        )


@pytest.mark.integration
class TestSearchResultOrdering:
    """``limit=N`` must return the N newest messages, not the N oldest.

    Mail returns ``messages of mailbox`` newest-first. The search loop
    iterated it backwards (``from total to 1 by -1``), which walks
    oldest to newest, so ``limit=N`` short-circuited on the N OLDEST
    messages in the mailbox. A caller probing recent mail got the
    oldest instead, with nothing to indicate it.

    The code comment claimed "newest-first" while doing the opposite,
    which is why reading it did not find this. Only measuring did.
    """

    def _mailbox_bounds(
        self, connector: AppleMailConnector, account: str
    ) -> tuple[str, str]:
        """(first, last) ``date received`` of ``messages of mailbox``."""
        raw = connector._run_applescript(
            f'tell application "Mail"\n'
            f'  set ms to messages of mailbox "INBOX" of account "{account}"\n'
            f'  return (date received of item 1 of ms as text) & "|" & '
            f'(date received of item (count of ms) of ms as text)\n'
            f'end tell'
        ).strip()
        first, last = raw.split("|")
        return first, last

    def test_limit_returns_the_newest_not_the_oldest(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        first_raw, last_raw = self._mailbox_bounds(connector, test_account)
        fmt = "%A, %B %d, %Y at %H:%M:%S"
        try:
            newest = _dt.datetime.strptime(first_raw, fmt)
            oldest = _dt.datetime.strptime(last_raw, fmt)
        except ValueError:
            pytest.skip(f"unrecognised Mail date format: {first_raw!r}")
        if newest == oldest:
            pytest.skip("mailbox has no date spread to distinguish ends")

        rows = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=5
        )
        assert rows, "need a non-empty INBOX for this test to mean anything"

        got = _dt.datetime.strptime(str(rows[0]["date_received"]), fmt)
        midpoint = oldest + (newest - oldest) / 2
        assert got > midpoint, (
            f"limit=5 returned {got} first, which is in the older half of a "
            f"mailbox spanning {oldest} to {newest}; the newest messages "
            f"were expected"
        )

    def test_results_are_in_descending_date_order(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Newest-first is the documented contract, so the rows must be
        ordered, not merely drawn from the newest end."""
        rows = connector._search_messages_applescript(
            account=test_account, mailbox="INBOX", limit=10
        )
        assert len(rows) >= 2, "need at least two rows to check ordering"

        fmt = "%A, %B %d, %Y at %H:%M:%S"
        try:
            dates = [
                _dt.datetime.strptime(str(r["date_received"]), fmt)
                for r in rows
            ]
        except ValueError:
            pytest.skip("unrecognised Mail date format")

        assert dates == sorted(dates, reverse=True), (
            f"rows must be newest-first; got {[d.isoformat() for d in dates]}"
        )


class TestBulkCrossScanCountsEachIdOnce:
    """One message, however many mailboxes hold it, is counted once.

    The cross-scan path (no ``account``/``source_mailbox``) walks every
    mailbox of every account. Gmail files one message under every label it
    carries, each label being a mailbox, so before the fix a message in the
    INBOX also matched in All Mail and ``update_message`` returned 2 for one
    message. Unit tests pin the emitted script; this is the observation
    that the count Mail reports is 1.
    """

    @staticmethod
    def _unflagged_inbox_message_id(account: str) -> str | None:
        """Numeric Mail id of an unflagged INBOX message, via AppleScript.

        Unflagged, so the test can restore the message by clearing the
        flag rather than reconstructing a colour. ``INBOX`` is the one
        mailbox name IMAP mandates, so it is not a choice of this test.
        """
        import subprocess

        from apple_mail_mcp.utils import escape_applescript_string

        acc = escape_applescript_string(account)
        r = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                'tell application "Mail"\n'
                f'  set mb to mailbox "INBOX" of account "{acc}"\n'
                '  repeat with m in (messages of mb)\n'
                '    if flagged status of m is false then return id of m\n'
                '  end repeat\n'
                '  return ""\n'
                'end tell',
            ],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None

    def test_flag_by_id_without_scope_counts_exactly_one(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        msg_id = self._unflagged_inbox_message_id(test_account)
        if msg_id is None:
            pytest.skip(f"No unflagged INBOX message in account {test_account!r}")

        try:
            flagged = connector.update_message([msg_id], flag_color="orange")
            assert flagged == 1, (
                f"one message flagged, Mail counted {flagged}: the cross-scan "
                f"is counting once per mailbox that holds the message"
            )
        finally:
            cleared = connector.update_message([msg_id], flagged=False)
        assert cleared == 1


class TestRuleMutationsActOnTheConfirmedRule:
    """The name check and the mutation happen in one AppleScript call.

    Creates a test-prefixed rule, then asks the connector to delete the
    rule at that index *as if* a different name had been confirmed. The
    rule must survive and the connector must say so; deleting it with the
    right name must then succeed. Self-cleaning.
    """

    RULE = "[apple-mail-mcp-test] confirmed-name guard"

    def _index_of(self, connector: AppleMailConnector) -> int | None:
        return next(
            (r["index"] for r in connector.list_rules() if r["name"] == self.RULE),
            None,
        )

    def test_mismatched_name_applies_nothing(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleChangedError

        # Pre-clean a leftover from a failed run.
        stale = self._index_of(connector)
        if stale is not None:
            connector.delete_rule(stale)

        index = connector.create_rule(
            name=self.RULE,
            conditions=[{"field": "subject", "operator": "contains",
                         "value": "this-string-will-not-match-anything-zzz"}],
            actions={"mark_read": True},
            match_logic="all",
            enabled=False,
        )
        try:
            with pytest.raises(MailRuleChangedError) as exc:
                connector.delete_rule(index, expected_name="not the confirmed rule")
            assert exc.value.actual_name == self.RULE
            assert self._index_of(connector) == index, "the rule was deleted anyway"

            with pytest.raises(MailRuleChangedError):
                connector.update_rule(index, enabled=True, expected_name="wrong")
            still = next(r for r in connector.list_rules() if r["name"] == self.RULE)
            assert still["enabled"] is False, "the update was applied anyway"

            connector.update_rule(index, enabled=True, expected_name=self.RULE)
            now = next(r for r in connector.list_rules() if r["name"] == self.RULE)
            assert now["enabled"] is True
        finally:
            leftover = self._index_of(connector)
            if leftover is not None:
                assert connector.delete_rule(leftover, expected_name=self.RULE) == self.RULE
        assert self._index_of(connector) is None
