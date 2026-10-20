"""Unit tests for the connector's compositions: drafts, sends, compose windows."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.compose_ledger import WindowOperation
from apple_mail_mcp.compose_script import (
    build_attachment_ax_verify_script,
    build_open_compose_script,
    build_paste_script,
    files_paste_fill,
    paste_probe_strings,
    text_paste_fill,
)
from apple_mail_mcp.compose_window import ComposeWindow, WindowSnapshot
from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftInvalidIdError,
    MailDraftNotFoundError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.utils import SANITIZE_MAX_LENGTH


@pytest.fixture(autouse=True)
def _windows_before_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every composition reads Mail's windows before it opens its own
    (``_mail_window_snapshot``). Answered here with none, so each test's
    scripted outcomes start at the opening script; what the read is for
    is tested in test_compose_window_tending.py."""
    monkeypatch.setattr(
        AppleMailConnector,
        "_mail_window_snapshot",
        lambda self: WindowSnapshot(mail_pid=77701, window_ids=frozenset()),
    )


class TestComposition:
    """The one composition behind every draft the connector saves and
    every message it sends (``_compose``): the window it opens
    (``build_open_compose_script``), and the paste placements it
    uses. Each pin is a measurement: the loopback
    read-back of 2026-09-27 (docs/research/icloud-draft-resync.md,
    Observation 10), the re-save spike of the same day
    (docs/research/draft-resave-spike.md), or the paste-focus failure of
    2026-09-05 (docs/research/paste-focus-failed.md).
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _compose(
        self,
        connector: AppleMailConnector,
        sender: str | None = None,
        *,
        seed: str = "new",
        to: list[str] | None = None,
        operation: WindowOperation = "save",
    ) -> str:
        return build_open_compose_script(
            seed=seed,
            seed_id=None if seed == "new" else "160989",
            reply_all=False,
            to=["a@example.com"] if to is None else to,
            cc=["c@example.com"],
            bcc=["b@example.com"],
            subject='Say "hi"',
            sender=sender,
            operation=operation,
        )

    def test_the_window_is_visible_and_its_body_seeded(
        self, connector: AppleMailConnector
    ) -> None:
        """An empty body's WebArea refused keyboard focus by every route
        tried on 2026-09-05; any content fixed it."""
        script = self._compose(connector)
        assert "make new outgoing message" in script
        assert 'visible:true, content:" "' in script

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_reply_or_forward_is_mails_own_window(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail's verb, opening its window: without one, every edit of
        what Mail wrote replaced it (measured 2026-09-26), and a draft
        saved without one was re-saved under a new id
        (draft-resave-spike.md)."""
        script = self._compose(connector, seed=seed)
        assert 'whose id is "160989"' in script
        assert f"{seed} origMsg opening window true" in script
        assert "make new outgoing message" not in script
        assert "opening window false" not in script

    def test_recipients_and_subject_go_through_the_model_escaped(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._compose(connector, to=['a"b\x00@example.com'])
        for kind, addr in (("to", 'a\\"b@example.com'), ("cc", "c@example.com"),
                           ("bcc", "b@example.com")):
            assert f"delete (every {kind} recipient of theMessage)" in script
            assert f'repeat with addr in {{"{addr}"}}' in script
            assert (
                f"make new {kind} recipient at end of {kind} recipients of "
                "theMessage with properties {address:addr}"
            ) in script
        assert "\x00" not in script
        assert 'subject:"Say \\"hi\\""' in script

    @pytest.mark.parametrize("seed", ["new", "reply", "forward"])
    def test_no_file_is_attached_through_the_dictionary(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """``make new attachment`` after the body was pasted sent Mail's
        empty cite blockquote again, and on a reply whose body was never
        touched it unquoted the original; files are pasted instead."""
        assert "make new attachment" not in self._compose(connector, seed=seed)

    def test_a_named_sender_is_set_last(
        self, connector: AppleMailConnector
    ) -> None:
        """Set before the recipients, the sender cost a saved draft its
        recipients (icloud-draft-resync.md, Observation 6)."""
        script = self._compose(connector, sender="Alice Smith <me@example.com>")
        sender_at = script.index(
            'set sender of theMessage to "Alice Smith <me@example.com>"'
        )
        assert sender_at > script.rindex("make new bcc recipient")

    def test_the_sender_is_sanitized_and_escaped(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._compose(connector, sender='Al"ice\x00 <me@example.com>')
        assert "\x00" not in script
        assert 'set sender of theMessage to "Al\\"ice <me@example.com>"' in script

    def test_no_sender_leaves_mails_default(
        self, connector: AppleMailConnector
    ) -> None:
        assert "set sender" not in self._compose(connector)

    def test_the_window_is_found_by_counted_names(
        self, connector: AppleMailConnector
    ) -> None:
        """Never guessed from the subject: a window of the same name
        already open (a Mail with several "New Message" windows, say) is
        refused, not pasted into."""
        script = self._compose(connector)
        assert "set beforeNames to name of windows" in script
        assert "afterCount > beforeCount" in script
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in script
        assert "|window|:newName" in script

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_window_whose_subject_is_set_is_named_again_after(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail retitles a compose window the moment its subject is set
        (measured 2026-09-27, docs/research/icloud-draft-resync.md,
        Observation 12): named only before the override, a reply or
        forward with its own subject was addressed by a name no window
        had, and the paste failed NO_BODY_AREA."""
        script = self._compose(connector, seed=seed)
        subject_at = script.index("set subject of theMessage")
        assert script.index("afterCount > beforeCount") < subject_at
        assert script.rindex("afterCount > beforeCount") > subject_at
        assert script.rindex("afterCount > beforeCount") < script.index(
            "|window|:newName"
        )

    @pytest.mark.parametrize("seed", ["new", "reply", "forward"])
    def test_a_window_whose_subject_is_not_set_is_named_once(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """A fresh message's subject is set as it is made, and a reply or
        forward without an override keeps Mail's: nothing retitles it."""
        script = build_open_compose_script(
            seed=seed, seed_id=None if seed == "new" else "160989",
            reply_all=False, to=["a@example.com"], cc=None, bcc=None,
            subject="Fresh" if seed == "new" else None, sender=None,
            operation="save",
        )
        assert script.count("afterCount > beforeCount") == 1

    def test_what_mail_will_send_is_read_back_from_the_model(
        self, connector: AppleMailConnector
    ) -> None:
        """The subject and recipients the window holds, after every
        override: a send is gated on them, and a save finds its draft by
        that subject."""
        script = self._compose(connector)
        assert "|subject|:(subject of theMessage as text)" in script
        for group in ("to", "cc", "bcc"):
            assert f"address of {group} recipients of theMessage" in script
            assert f"|{group}|:" in script

    @pytest.mark.parametrize(
        ("operation", "mailbox", "other"),
        [
            ("save", "drafts mailbox", "sent mailbox"),
            ("send", "sent mailbox", "drafts mailbox"),
        ],
    )
    def test_the_mailbox_its_ending_is_found_in_is_snapshot_before_it_opens(
        self,
        connector: AppleMailConnector,
        operation: WindowOperation,
        mailbox: str,
        other: str,
    ) -> None:
        """A saved draft is the Drafts entry that was not there before,
        and a sent message's copy the Sent entry: the subject alone found
        an older message of the same subject."""
        script = self._compose(connector, operation=operation)
        snapshot_at = script.index(
            f"set beforeIds to (id of every message of {mailbox})"
        )
        assert snapshot_at < script.index("make new outgoing message")
        assert "|before_ids|:beforeIds" in script
        assert f"id of every message of {other}" not in script

    def test_the_fresh_body_paste_replaces_everything(
        self, connector: AppleMailConnector
    ) -> None:
        """Select all, delete, then paste. Pasted above the seed instead,
        the sent message carried an empty <blockquote type="cite">."""
        script = build_paste_script(
            window_name="S", fill=text_paste_fill("hi", plain=True),
            placement="replace", undo_first=False,
        )
        select_at = script.index('keystroke "a" using command down')
        delete_at = script.index("key code 51")
        paste_at = script.index('keystroke "v" using command down')
        assert select_at < delete_at < paste_at
        assert "public.utf8-plain-text" in script

    def test_a_paste_above_what_mail_wrote_never_selects_or_deletes(
        self, connector: AppleMailConnector
    ) -> None:
        """A reply's quote and a forward's attachments live in the body:
        a select-all or a delete there would take them."""
        script = build_paste_script(
            window_name="S", fill=text_paste_fill("<p>hi</p>", plain=False),
            placement="above", undo_first=False,
        )
        assert "key code 126 using command down" in script
        assert 'keystroke "a" using command down' not in script
        assert "key code 51" not in script
        assert "public.html" in script

    def test_files_are_pasted_as_file_urls_at_the_end(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        script = build_paste_script(
            window_name="S", fill=files_paste_fill([f]),
            placement="end", undo_first=False,
        )
        assert "writeObjects:fileURLs" in script
        assert f.resolve().as_posix() in script
        end_at = script.index("key code 125 using command down")
        assert end_at < script.index('keystroke "v" using command down')
        assert 'keystroke "a" using command down' not in script
        assert "key code 51" not in script

    def test_a_file_paste_that_fails_is_salvaged_and_nothing_sent(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        captured = _scripted(
            connector, ["PASTE_FOCUS_FAILED:body area would not take focus"]
        )
        with pytest.raises(MailAppleScriptError, match="PASTE_FOCUS_FAILED"):
            connector._paste_attachments(
                ComposeWindow(
                    name="S", subject="S", to=[], cc=[], bcc=[], before_ids=[],
                    window_id=2781,
                ),
                [f],
            )
        # file paste, then the salvage (close, Save) of Mail's window
        # 2781; no AX verify, no send.
        assert len(captured) == 2
        assert captured[1].startswith("set closeId to 2781\n")


class TestDeleteDraft:
    """Tests for AppleMailConnector.delete_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        assert connector.delete_draft("160991") is True
        mock_run.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_script_embeds_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        connector.delete_draft("160991")
        script = mock_run.call_args[0][0]
        assert 'whose id is "160991"' in script
        # Scoped to Mail's own aggregate drafts mailbox, which covers every
        # account's drafts under whatever name the locale gives it; not to
        # mailboxes whose English name happens to contain "Drafts".
        assert "message of drafts mailbox" in script
        assert 'contains "Drafts"' not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.delete_draft("999999")

    def test_delete_draft_invalid_id_path_traversal(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("../etc/passwd")

    def test_delete_draft_invalid_id_with_quotes(
        self, connector: AppleMailConnector
    ) -> None:
        # Quote injection that could break out of the AppleScript string.
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft('1"; do something --')

    def test_delete_draft_empty_id(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_strips_whitespace_from_result(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # AppleScript output sometimes carries trailing newlines.
        mock_run.return_value = "OK\n"
        assert connector.delete_draft("160991") is True


class TestFindMessageByMessageId:
    """Tests for AppleMailConnector.find_message_by_message_id."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_on_match(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989"
        result = connector.find_message_by_message_id(
            "<calendar-abc123@google.com>"
        )
        assert result == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        result = connector.find_message_by_message_id(
            "<missing@example.com>"
        )
        assert result is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_empty_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        result = connector.find_message_by_message_id("")
        assert result is None
        # No need to call AppleScript for an empty input.
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_trailing_whitespace(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989\n"
        assert connector.find_message_by_message_id("<x@y>") == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_message_id_with_quotes_is_escaped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Quotes/backslashes in the Message-ID must be escaped to prevent
        AppleScript injection. Real Message-IDs almost never contain these
        but we shouldn't trust the wire."""
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id('<weird"id@host>')
        script = mock_run.call_args[0][0]
        # Escaped quote inside the AppleScript string literal.
        assert '\\"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_whose_message_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("<x@y>")
        script = mock_run.call_args[0][0]
        # Compound clause queries both bare and bracketed forms (#205 follow-up).
        assert "whose" in script
        assert "message id is" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bracketless_input_queries_both_forms(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail.app's ``message id`` storage normalization varies by
        account: IMAP-backed accounts (iCloud, Gmail) store the bare RFC
        Message-ID; some other paths may store with angle brackets. The
        resolver therefore queries both forms in a single ``whose``
        clause so a caller doesn't need to know the storage convention.
        """
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("abc@example.com")
        script = mock_run.call_args[0][0]
        assert 'message id is "abc@example.com"' in script
        assert 'message id is "<abc@example.com>"' in script
        assert "whose" in script and " or " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bracketed_input_queries_both_forms(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Existing callers (e.g. update_draft passing In-Reply-To)
        already include brackets; strip them and query both forms so
        we don't depend on Mail.app's storage convention.
        """
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("<abc@example.com>")
        script = mock_run.call_args[0][0]
        assert 'message id is "abc@example.com"' in script
        assert 'message id is "<abc@example.com>"' in script
        assert "<<" not in script and ">>" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_for_bare_rfc_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Read tools (#148) emit bare RFC ids on the IMAP path. Round-trip
        through ``create_draft(reply_to=...)`` requires this call to return
        Mail's internal id when the input is bare. Unit test asserts API
        surface; an integration test asserts the AppleScript actually
        matches against Mail.app's storage.
        """
        mock_run.return_value = "54957"
        result = connector.find_message_by_message_id(
            "1779175169746.aa805a12-74b6-4330-93ff-72a175ed8679@example.com"
        )
        assert result == "54957"


class TestGetDraftState:
    """Tests for AppleMailConnector.get_draft_state."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_full_draft_state(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"160991",'
            '"to":["a@example.com","b@example.com"],'
            '"cc":["c@example.com"],"bcc":[],'
            '"subject":"Re: hello",'
            '"body":"hi there\\n\\n-- original --","in_reply_to":"<orig@x>",'
            '"references":"<orig@x>",'
            '"attachment_names":["report.pdf"]}'
        )
        state = connector.get_draft_state("160991")
        assert state == {
            "draft_id": "160991",
            "to": ["a@example.com", "b@example.com"],
            "cc": ["c@example.com"],
            "bcc": [],
            "subject": "Re: hello",
            "body": "hi there\n\n-- original --",
            "in_reply_to": "<orig@x>",
            "references": "<orig@x>",
            "attachment_names": ["report.pdf"],
        }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reads_the_sender_back(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The draft's own sender is part of its state, so update_draft can
        rebuild it in the same account instead of Mail's default."""
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[],"sender":"Alice Smith <alice@icloud.com>"}'
        )
        state = connector.get_draft_state("x")
        assert state["sender"] == "Alice Smith <alice@icloud.com>"
        script = mock_run.call_args.args[0]
        assert "sender of foundDraft" in script
        assert "|sender|:" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reads_the_account_back(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A draft id names a draft in any account. The account the draft
        sits in is part of its state, read from the mailbox Mail resolves
        the draft to, so a tool acting by id can say which account it is
        about to touch."""
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[],"sender":"","account":"Work"}'
        )
        state = connector.get_draft_state("x")
        assert state["account"] == "Work"
        script = mock_run.call_args.args[0]
        assert "name of account of mailbox of foundDraft" in script
        assert "|account|:" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        with pytest.raises(MailDraftNotFoundError):
            connector.get_draft_state("999999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_internal_found_flag(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert "found" not in state

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_handles_empty_recipient_lists(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert state["to"] == []
        assert state["cc"] == []
        assert state["bcc"] == []
        assert state["attachment_names"] == []

    def test_invalid_id_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.get_draft_state("../escape")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_iterates_drafts_mailboxes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        # Scoped to Mail's aggregate drafts mailbox (locale-independent),
        # not to mailboxes named "Drafts" in English.
        assert "messages of drafts mailbox" in script
        assert 'contains "Drafts"' not in script
        # Should use as-text id comparison (probes showed numeric whose
        # clauses are unreliable on IMAP-backed Drafts), and read each
        # id inside a try: a draft that vanished between the listing and
        # the walk reaching it (Mail's re-save of a draft with a named
        # sender) is skipped, not a failure of the whole read.
        assert "set candId to (id of d as text)" in script
        assert "if candId is targetId then" in script
        walk = script[script.index("repeat with d in messages of drafts mailbox"):]
        assert walk.index("try") < walk.index("id of d as text")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_reads_threading_headers(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        assert '"In-Reply-To"' in script
        assert '"References"' in script


class TestCreateDraft:
    """Tests for AppleMailConnector.create_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_seed_raises(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed must be"):
            connector.create_draft(seed="bogus")

    def test_reply_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="reply")

    def test_forward_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="forward")

    def test_new_requires_to(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'to' is required"):
            connector.create_draft(seed="new", subject="hi", body="x")

    def test_new_requires_subject(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'subject' is required"):
            connector.create_draft(seed="new", to=["x@example.com"], body="x")

    # ------------------------------------------------------------------
    # Fresh seed (`seed='new'`)
    # ------------------------------------------------------------------

    def _save(
        self,
        connector: AppleMailConnector,
        *,
        seed: str = "new",
        body: str = "hello",
        outcomes: list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], dict[str, str]]:
        """Save a draft through create_draft against a well-behaved Mail
        (or ``outcomes``), returning every script run and the result."""
        if seed == "new":
            kwargs.setdefault("to", ["a@example.com"])
            kwargs.setdefault("subject", "hi")
        else:
            kwargs.setdefault("seed_id", "160989")
        files = kwargs.get("attachment_paths") or []
        captured = _scripted(
            connector,
            outcomes or _compose_outcomes(
                body, window="hi", seed=seed, plain=True, send=False,
                draft_id="161055", file_names=[Path(f).name for f in files],
            ),
        )
        result = connector.create_draft(seed=seed, body=body, **kwargs)
        return captured, result

    def test_new_save_returns_draft_id(
        self, connector: AppleMailConnector
    ) -> None:
        _, result = self._save(connector)
        assert result == {"draft_id": "161055", "sent_message_id": ""}

    def test_a_fresh_draft_is_composed_in_a_window_and_saved_from_it(
        self, connector: AppleMailConnector
    ) -> None:
        """A draft saved through the dictionary (``set content``, then
        ``save``) was re-saved by Mail under a new id when it named a
        sender (draft-resave-spike.md), held its body inside Mail's
        ``<blockquote type="cite">`` (icloud-draft-resync.md,
        Observation 10), and left a hidden outgoing message behind. A
        fresh draft is composed as a fresh send is, then closed with
        Save: open, paste over the seed, read back, close, find the id."""
        scripts, _ = self._save(
            connector, to=["a@example.com", "b@example.com"],
            cc=["c@example.com"], body="line1\nline2",
        )
        assert len(scripts) == 5
        open_s, paste_s, readback_s, close_s, find_s = scripts
        assert 'visible:true, content:" "' in open_s
        assert 'subject:"hi"' in open_s
        assert '{"a@example.com", "b@example.com"}' in open_s
        assert '{"c@example.com"}' in open_s
        assert "set beforeIds to (id of every message of drafts mailbox)" in open_s
        assert "public.utf8-plain-text" in paste_s
        assert "line1\nline2" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "my tendClickClose(closeIdx)" in close_s
        assert 'click button "Save" of sheet 1 of window closeIdx' in close_s
        assert 'messages of drafts mailbox whose subject is "hi"' in find_s
        assert "{5, 6}" in find_s

    @pytest.mark.parametrize(
        ("seed", "body"),
        [("new", "x"), ("new", ""), ("reply", "x"), ("reply", ""),
         ("forward", "x"), ("forward", "")],
    )
    def test_no_draft_is_saved_through_the_dictionary(
        self, connector: AppleMailConnector, seed: str, body: str
    ) -> None:
        """Every seed, with a body or without, is saved from a window
        closed with Save: never Mail's ``save`` verb, never ``content``,
        never a message made without a window."""
        scripts, result = self._save(connector, seed=seed, body=body)
        assert result["draft_id"] == "161055"
        joined = "\n".join(scripts)
        assert "save theMessage" not in joined
        assert "set content" not in joined
        assert "visible:false" not in joined
        assert "opening window false" not in joined
        assert "AXCloseButton" in scripts[-2]

    def test_an_empty_fresh_body_still_replaces_the_seed(
        self, connector: AppleMailConnector
    ) -> None:
        """The one-space seed sits inside Mail's cite blockquote; left in
        place it would be saved and sent quoted. An empty body is an
        empty paste over everything, which arrived empty and unquoted
        (icloud-draft-resync.md, Observation 10)."""
        scripts, _ = self._save(connector, body="")
        assert 'keystroke "a" using command down' in scripts[1]
        assert "key code 51" in scripts[1]

    def test_a_save_whose_draft_never_appears_is_an_error(
        self, connector: AppleMailConnector
    ) -> None:
        """The new draft's id is found by diffing Drafts before and after
        the save, and the draft takes a moment to appear (measured: none
        of three lookups saw it at 0.5 s, all did by 1.5 s). The script
        polls for it with a bound; if it still has not appeared, that is
        reported, not returned as success with an empty id that every
        later call rejects."""
        from apple_mail_mcp.exceptions import MailDraftNotSettledError

        outcomes = _compose_outcomes(
            "hello", window="hi", plain=True, send=False, draft_id="",
        )
        with pytest.raises(MailDraftNotSettledError, match="did not appear"):
            self._save(connector, outcomes=outcomes)

    def test_the_save_polls_for_the_new_draft(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._save(connector)
        find_s = scripts[-1]
        assert "delay 0.5\n" not in find_s, "a fixed delay is a guess"
        assert "repeat with attempt from 1 to" in find_s
        assert "messages of drafts mailbox whose subject is" in find_s

    def test_a_save_sends_nothing(self, connector: AppleMailConnector) -> None:
        scripts, _ = self._save(connector)
        assert all("click sendBtn" not in s for s in scripts)

    def test_new_send_returns_its_sent_copys_ids(
        self, connector: AppleMailConnector
    ) -> None:
        _scripted(connector, _compose_outcomes("hello", window="hi", plain=True))
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="hello",
            send_now=True,
        )
        assert result == _SENT

    def test_new_send_composes_a_window_and_pastes_the_body_as_plain_text(
        self, connector: AppleMailConnector
    ) -> None:
        """seed="new" + send_now=True is the fresh composition: a visible
        compose window, the body pasted as plain text over the seed and
        read back, then the verified send. The body never goes through
        ``content`` (it arrives inside <blockquote type="cite">, which
        iOS Mail draws as a purple bar), and there is no mailto: URL (it
        can neither name a sender nor carry a file)."""
        captured = _scripted(
            connector, _compose_outcomes("line1\nline2", window="hi", plain=True)
        )
        connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="line1\nline2",
            send_now=True,
        )
        assert len(captured) == 6
        compose_s, paste_s, readback_s, send_s, _, _ = captured
        assert "make new outgoing message" in compose_s
        assert all("open location" not in s for s in captured)
        assert all("mailto:" not in s for s in captured)
        assert all("set content" not in s for s in captured)
        assert "public.utf8-plain-text" in paste_s
        assert "line1\nline2" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "click sendBtn" in send_s

    def test_the_window_mail_opened_is_the_one_pasted_into_and_sent(
        self, connector: AppleMailConnector
    ) -> None:
        """Every later step addresses the window the compose script
        reported, not a name guessed from the subject, and the look for
        its Sent copy goes by the subject Mail holds."""
        outcomes = _compose_outcomes("x", window="the new one", plain=True)
        outcomes[0] = _compose_meta("the new one", subject="hi")
        captured = _scripted(connector, outcomes)
        connector.create_draft(
            seed="new", to=["a@example.com"], subject="hi", body="x",
            send_now=True,
        )
        for script in captured[1:4]:
            assert 'set composeName to "the new one"' in script or (
                'window "the new one"' in script
            )
        assert 'sent mailbox whose subject is "hi"' in captured[4]

    def test_new_send_no_compose_window_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """A compose window that never appears stops everything."""
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            raise MailAppleScriptError(
                "execution error: NO_COMPOSE_WINDOW: no compose window "
                "appeared within 5 s (-2700)"
            )

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="NO_COMPOSE_WINDOW") as exc:
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                send_now=True,
            )
        # The open script, then the look for a window Mail opened late.
        assert len(captured) == 2
        assert "set expectedName to \"hi\"" in captured[1]
        assert "(compose window: none found open)" in str(exc.value)

    def test_new_send_names_its_sender(
        self, connector: AppleMailConnector
    ) -> None:
        """from_account on a fresh send is honoured: the resolved sender is
        set on the composed message. Before this the mailto: path could
        not set one and refused."""
        captured = _scripted(connector, _compose_outcomes("x", window="hi", plain=True))
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                send_now=True,
                from_account="Work",
            )
        resolve.assert_called_once_with("Work")
        assert 'set sender of theMessage to "Alice Smith <me@example.com>"' in captured[0]

    def test_new_send_from_an_unknown_account_composes_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        captured = _scripted(connector, ["unused"])
        accounts = [{
            "id": "U-1", "name": "Home", "email_addresses": ["me@example.com"],
            "full_name": None,
        }]
        with patch.object(connector, "list_accounts", return_value=accounts):
            with pytest.raises(MailAccountNotFoundError):
                connector.create_draft(
                    seed="new",
                    to=["a@example.com"],
                    subject="hi",
                    body="x",
                    send_now=True,
                    from_account="Work",
                )
        assert captured == []

    def test_new_send_carries_attachments(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Attachments on a fresh send are pasted after the body, seen in
        the compose window before Send, and read on the Sent copy the
        send filed. Before this the mailto: path could not carry them and
        refused."""
        f1 = tmp_path / "report.pdf"
        f1.write_bytes(b"%PDF-fake")
        f2 = tmp_path / "data.csv"
        f2.write_text("a,b,c")
        captured = _scripted(
            connector,
            _compose_outcomes(
                "x", window="hi", plain=True, file_names=[f1.name, f2.name]
            ),
        )
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="x",
            attachment_paths=[f1, f2],
            send_now=True,
        )
        assert result == _SENT
        assert len(captured) == 8
        (compose_s, paste_s, readback_s, files_s, verify_s, send_s,
         _, copy_s) = captured
        assert "make new attachment" not in compose_s
        assert "writeObjects:fileURLs" in files_s
        assert str(f1.resolve()) in files_s
        assert str(f2.resolve()) in files_s
        assert f1.name in verify_s and f2.name in verify_s
        assert "click sendBtn" in send_s
        assert "name of every mail attachment of m" in copy_s

    def test_new_send_missing_attachment_composes_nothing(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with pytest.raises(FileNotFoundError):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                attachment_paths=[tmp_path / "ghost.pdf"],
                send_now=True,
            )
        assert captured == []

    def test_the_mailto_send_path_is_gone(self) -> None:
        """One composition for every fresh send: the mailto: path and its
        helpers were deleted, not kept beside it."""
        for name in (
            "_send_new_via_eml",
            "_send_html_new_with_attachments",
            "_build_attach_compose_script",
            "_inject_html_and_send",
            "_build_emlx_bytes",
        ):
            assert not hasattr(AppleMailConnector, name), name

    def test_one_composition_is_left(self) -> None:
        """The dictionary save and the split between a fresh send, a note
        above a seed and everything else were deleted, not kept beside
        the one composition."""
        for name in (
            "_send_fresh",
            "_build_fresh_compose_script",
            "_compose_note_above_seed",
            "_open_seeded_compose",
            "_build_attachment_block",
            "_check_sent_attachment_count",
        ):
            assert not hasattr(AppleMailConnector, name), name

    @pytest.mark.parametrize(
        ("resolved", "in_script"),
        [
            # #158: a Display Name <email> sender is embedded verbatim.
            ("Alice Smith <me@x.com>", "Alice Smith <me@x.com>"),
            # #158: an account with no display name gives the bare form.
            ("me@x.com", "me@x.com"),
            # #173: sanitize_input before escaping strips a null byte.
            ("Alice\x00Smith <me@x.com>", "AliceSmith <me@x.com>"),
        ],
    )
    def test_a_saved_draft_names_its_sender(
        self, connector: AppleMailConnector, resolved: str, in_script: str
    ) -> None:
        with patch.object(
            connector, "_resolve_account_to_sender", return_value=resolved
        ):
            scripts, _ = self._save(connector, from_account="Gmail")
        assert "\x00" not in scripts[0]
        assert f'set sender of theMessage to "{in_script}"' in scripts[0]

    def test_a_saved_draft_from_an_unknown_account_composes_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        accounts = [{
            "id": "U-1", "name": "Home", "email_addresses": ["me@example.com"],
            "full_name": None,
        }]
        captured = _scripted(connector, ["unused"])
        with patch.object(connector, "list_accounts", return_value=accounts):
            with pytest.raises(MailAccountNotFoundError):
                connector.create_draft(
                    seed="new", to=["a@example.com"], subject="hi", body="x",
                    from_account="Work",
                )
        assert captured == []

    def test_a_saved_drafts_files_are_pasted_after_the_body(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """As on a fresh send: pasted as file URLs, then seen in the
        window, then saved with it. ``make new attachment`` after the
        paste would bring the cite blockquote back."""
        f1 = tmp_path / "report.pdf"
        f1.write_bytes(b"%PDF-fake")
        f2 = tmp_path / "data.csv"
        f2.write_text("a,b,c")
        scripts, result = self._save(connector, attachment_paths=[f1, f2])
        assert result["draft_id"] == "161055"
        assert len(scripts) == 7
        _, paste_s, _, files_s, verify_s, close_s, _ = scripts
        assert 'keystroke "a" using command down' in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert str(f1.resolve()) in files_s and str(f2.resolve()) in files_s
        assert "key code 125 using command down" in files_s
        assert f1.name in verify_s and f2.name in verify_s
        assert "AXCloseButton" in close_s
        assert all("make new attachment" not in s for s in scripts)

    def test_new_attachment_missing_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                attachment_paths=[tmp_path / "does-not-exist.pdf"],
            )

    def test_recipient_none_means_no_block(
        self, connector: AppleMailConnector
    ) -> None:
        """For reply/forward, cc=None should not emit a delete-and-replace
        block (preserves Mail's auto-derived recipients)."""
        scripts, _ = self._save(connector, seed="reply", body="")
        open_s = scripts[0]
        # cc/bcc not specified → no clear-and-add block for them.
        assert "delete (every cc recipient" not in open_s
        assert "delete (every bcc recipient" not in open_s
        # to also not specified → no clear for to either.
        assert "delete (every to recipient" not in open_s

    def test_recipient_empty_list_clears(
        self, connector: AppleMailConnector
    ) -> None:
        """cc=[] explicitly clears auto-derived cc recipients."""
        scripts, _ = self._save(connector, seed="reply", body="", cc=[])
        assert "delete (every cc recipient of theMessage)" in scripts[0]

    # ------------------------------------------------------------------
    # Reply and forward seeds
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("seed", "reply_all", "verb"),
        [("reply", False, "reply"), ("reply", True, "reply to all"),
         ("forward", False, "forward")],
    )
    def test_a_seeded_draft_opens_mails_own_window(
        self, connector: AppleMailConnector, seed: str, reply_all: bool,
        verb: str,
    ) -> None:
        scripts, _ = self._save(
            connector, seed=seed, body="", reply_all=reply_all,
        )
        assert f"set theMessage to {verb} origMsg opening window true" in scripts[0]
        if not reply_all:
            assert "reply to all" not in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_body_leaves_what_mail_wrote_untouched(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """No note: nothing is pasted, and Mail's quote or forwarded
        message is saved as Mail made it. Open, close with Save, find."""
        scripts, _ = self._save(connector, seed=seed, body="")
        assert len(scripts) == 3
        assert all('keystroke "v"' not in s for s in scripts)
        assert all("set content" not in s for s in scripts)

    def test_reply_subject_override(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._save(
            connector, seed="reply", body="", subject="custom subject",
        )
        assert 'set subject of theMessage to "custom subject"' in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_bodied_seed_is_sent_through_the_verified_send(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Mail's dictionary ``send`` confirmed nothing. Every send is the
        window's Send button, read back: open, gate, click and verify."""
        captured = _scripted(
            connector, _compose_outcomes("", window="Re: hi", seed=seed)
        )
        result = connector.create_draft(
            seed=seed, seed_id="160989", to=["a@example.com"], body="",
            send_now=True,
        )
        assert result == _SENT
        assert len(captured) == 4  # open, verified send, the Sent look
        assert "click sendBtn" in captured[1]
        assert all("tell theMessage to send" not in s for s in captured)

    # ------------------------------------------------------------------
    # A note on a reply or forward goes above what Mail wrote
    # ------------------------------------------------------------------

    _SEEDED_META = (
        '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
        '"to": ["a@example.com"], "cc": [], "bcc": [], "before_ids": [5, 6]}'
    )
    _NOTE = "a note for you"

    def _run_seeded(
        self,
        connector: AppleMailConnector,
        *,
        seed: str,
        send_now: bool,
        outcomes: list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], dict[str, str]]:
        """Run create_draft with a note, answering each script in turn:
        open the window, paste, read back, then send and look for its
        Sent copy, or close-and-save and find the saved draft's id."""
        tail = ["SENT", *_sent_copy_outcomes()] if send_now else ["SALVAGED", "7"]
        answers = outcomes or [self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE, *tail]
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            return answers[min(len(scripts) - 1, len(answers) - 1)]

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        result = connector.create_draft(
            seed=seed, seed_id="160989", body=self._NOTE, send_now=send_now,
            **kwargs,
        )
        return scripts, result

    def test_a_forward_note_is_pasted_above_the_forwarded_message(
        self, connector: AppleMailConnector
    ) -> None:
        """Setting the content of Mail's forward replaced the forwarded
        message and dropped its attachments (read back through the
        loopback, 2026-09-26). The note is pasted above it instead, in a
        visible compose window, and the content is never set."""
        scripts, result = self._run_seeded(
            connector, seed="forward", send_now=True, to=["a@example.com"],
        )
        assert result == _SENT
        # open, paste, read-back, verified send, the Sent look (two)
        assert len(scripts) == 6
        open_s, paste_s, _, send_s, ids_s, _ = scripts
        assert 'whose id is "160989"' in open_s
        assert "forward origMsg opening window true" in open_s
        assert "beforeNames" in open_s
        assert "key code 126 using command down" in paste_s
        assert "public.utf8-plain-text" in paste_s
        assert self._NOTE in paste_s
        assert "enabled of sendBtn" in send_s
        assert 'set composeName to "Fwd: Probe"' in send_s
        assert 'sent mailbox whose subject is "Fwd: Probe"' in ids_s
        assert all("set content of theMessage" not in s for s in scripts)

    def test_a_reply_note_is_pasted_above_the_quote(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._run_seeded(
            connector, seed="reply", send_now=True, to=["a@example.com"],
            subject="custom subject",
        )
        open_s = scripts[0]
        assert "reply origMsg opening window true" in open_s
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'set subject of theMessage to "custom subject"' in open_s
        assert "key code 126 using command down" in scripts[1]
        assert all("set content of theMessage" not in s for s in scripts)

    def test_a_reply_all_note_opens_a_reply_all_window(
        self, connector: AppleMailConnector
    ) -> None:
        scripts, _ = self._run_seeded(
            connector, seed="reply", send_now=True, reply_all=True,
            to=["a@example.com"], cc=[],
        )
        assert "reply to all origMsg opening window true" in scripts[0]

    def test_a_saved_note_is_saved_from_its_window(
        self, connector: AppleMailConnector
    ) -> None:
        """Without send_now the window is closed with Save, and the new
        draft is found among the Drafts that were not there before."""
        scripts, result = self._run_seeded(
            connector, seed="reply", send_now=False,
        )
        assert result == {"draft_id": "7", "sent_message_id": ""}
        assert len(scripts) == 5  # open, paste, read-back, save, find id
        assert "id of every message of drafts mailbox" in scripts[0]
        assert "AXCloseButton" in scripts[3]
        assert "{5, 6}" in scripts[4]
        assert 'whose subject is "Fwd: Probe"' in scripts[4]
        assert all("enabled of sendBtn" not in s for s in scripts)

    @pytest.mark.parametrize("send_now", [False, True])
    @pytest.mark.parametrize("body", ["", _NOTE])
    def test_a_seeds_files_are_pasted_after_what_mail_wrote(
        self, connector: AppleMailConnector, tmp_path: Path, send_now: bool,
        body: str,
    ) -> None:
        """A caller's file on a reply or forward is pasted at the end,
        after the note (if any) and after Mail's quote or forwarded
        message. Attached through the dictionary on a window whose body
        was untouched, it unquoted the original and dropped a forward's
        own files (measured 2026-09-27)."""
        f = tmp_path / "caller.txt"
        f.write_text("mine")
        outcomes = _compose_outcomes(
            body, window="Fwd: Probe", seed="forward", plain=True,
            file_names=["caller.txt"], send=send_now,
        )
        if send_now:
            # The forward's Sent copy carries the original's files too.
            outcomes[-1] = _sent_copy_outcomes(["theirs.pdf", "caller.txt"])[1]
        captured = _scripted(connector, outcomes)
        result = connector.create_draft(
            seed="forward", seed_id="160989", to=["a@example.com"],
            body=body, attachment_paths=[f], send_now=send_now,
        )
        assert result["draft_id"] == ("" if send_now else "7")
        assert all("make new attachment" not in s for s in captured)
        files_at = 3 if body else 1
        assert "writeObjects:fileURLs" in captured[files_at]
        assert "key code 125 using command down" in captured[files_at]
        if body:
            assert "key code 126 using command down" in captured[1]
        if send_now:
            assert result == _SENT
            assert "click sendBtn" in captured[files_at + 2]
            assert "name of every mail attachment of m" in captured[-1]
        else:
            assert "AXCloseButton" in captured[files_at + 2]

    def test_an_off_list_recipient_read_back_discards_the_window(
        self, connector: AppleMailConnector
    ) -> None:
        """The window is gated on the recipients Mail will actually send
        to, read back from the model, before anything is pasted."""
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        meta = (
            '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
            '"to": ["evil@other.com"], "cc": [], "bcc": [], "before_ids": []}'
        )
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            self._run_seeded(
                connector, seed="forward", send_now=True,
                to=["a@example.com"], outcomes=[meta, "DISCARDED"],
            )

    def test_an_off_list_read_back_pastes_and_sends_nothing(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailOutboundDisallowedError

        meta = (
            '{"window": "Fwd: Probe", "subject": "Fwd: Probe", '
            '"to": ["evil@other.com"], "cc": [], "bcc": [], "before_ids": []}'
        )
        scripts: list[str] = []
        answers = [meta, "DISCARDED"]

        def fake_run(script: str) -> str:
            scripts.append(script)
            return answers[min(len(scripts) - 1, 1)]

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailOutboundDisallowedError):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE, send_now=True,
            )
        assert len(scripts) == 2
        assert "AXCloseButton" in scripts[1]
        assert all('keystroke "v"' not in s for s in scripts)

    def test_a_window_that_will_not_close_fails_the_save_loudly(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailAppleScriptError, match="window still open"):
            self._run_seeded(
                connector, seed="reply", send_now=False,
                outcomes=[
                    self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE,
                    "SALVAGE_FAILED:window still open",
                ],
            )

    def test_a_saved_note_that_never_appears_in_drafts_is_not_settled(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotSettledError

        with pytest.raises(MailDraftNotSettledError):
            self._run_seeded(
                connector, seed="reply", send_now=False,
                outcomes=[
                    self._SEEDED_META, "PASTED_UNVERIFIED", self._NOTE,
                    "SALVAGED", "",
                ],
            )

    def test_a_seed_that_is_gone_is_message_not_found_on_the_window_path(
        self, connector: AppleMailConnector
    ) -> None:
        def fake_run(script: str) -> str:
            raise MailAppleScriptError("execution error: SEED_NOT_FOUND (-2700)")

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE,
            )

    def test_the_new_window_is_found_even_when_its_name_is_already_open(
        self, connector: AppleMailConnector
    ) -> None:
        """A window of the same name can already be open: a draft saved
        through the dictionary, as this connector's once were, left one
        behind (docs/research/icloud-draft-resync.md, Obs. 5), and a
        name-set diff then saw no new window. Names are counted instead,
        and a new window whose name is not unique is refused before
        anything is pasted or sent: every later step addresses it by
        name."""
        scripts, _ = self._run_seeded(
            connector, seed="forward", send_now=True, to=["a@example.com"],
        )
        open_s = scripts[0]
        assert "afterCount > beforeCount" in open_s
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in open_s

    def test_a_new_window_that_shares_its_name_stops_everything(
        self, connector: AppleMailConnector
    ) -> None:
        scripts: list[str] = []

        def fake_run(script: str) -> str:
            scripts.append(script)
            raise MailAppleScriptError(
                "execution error: COMPOSE_WINDOW_NOT_UNIQUE: Fwd: Probe (-2700)"
            )

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="COMPOSE_WINDOW_NOT_UNIQUE"):
            connector.create_draft(
                seed="forward", seed_id="160989", to=["a@example.com"],
                body=self._NOTE, send_now=True,
            )
        # The open script, then the look for a window it did not report;
        # nothing pasted, nothing sent.
        assert len(scripts) == 2
        assert "set beforeIds to {}" in scripts[1]

    def test_plain_paste_probes_look_for_the_text_itself(self) -> None:
        """A plain note is read back as the text it is. Angle brackets in
        it are text, not a sign the paste degraded to raw source."""
        snippet, raw = paste_probe_strings(
            "<see> the  attached & more", plain=True
        )
        assert snippet == "<see> the attached & mor"
        assert raw == ""
        html_snippet, html_raw = paste_probe_strings(
            "<p>hi <b>there</b></p>"
        )
        assert html_snippet == "hi there"
        assert html_raw == "<p>hi <b>there</"

    # ------------------------------------------------------------------
    # Seed lookup error mapping
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_seed_not_found_raises_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("SEED_NOT_FOUND")
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply", seed_id="999999", body="x"
            )

    # ------------------------------------------------------------------
    # RFC 5322 Message-ID seed_id support (#205)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    @patch.object(AppleMailConnector, "find_message_by_message_id")
    def test_an_rfc_message_id_seed_is_resolved(
        self,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
        seed: str,
    ) -> None:
        """Read tools (#148) emit RFC ids on the IMAP path. create_draft
        must resolve them to Mail's internal id before building the
        `whose id is` AppleScript clause. (#205)
        """
        mock_resolve.return_value = "160989"
        scripts, _ = self._save(
            connector, seed=seed, body="", seed_id="abc-123@example.com",
        )
        mock_resolve.assert_called_once_with("abc-123@example.com")
        # AppleScript looks up by Mail's internal id, not the RFC id.
        assert 'whose id is "160989"' in scripts[0]
        assert all("abc-123@example.com" not in s for s in scripts)

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    def test_internal_numeric_seed_id_skips_resolver(
        self,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Existing callers passing Mail's internal numeric id (no '@')
        must keep working without a resolver round-trip. (#205)
        """
        scripts, _ = self._save(connector, seed="reply", body="")
        mock_resolve.assert_not_called()
        assert 'whose id is "160989"' in scripts[0]

    @patch.object(AppleMailConnector, "find_message_by_message_id")
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_unresolvable_rfc_seed_raises_message_not_found(
        self,
        mock_run: MagicMock,
        mock_resolve: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """When the RFC id doesn't match any message, surface the same
        MailMessageNotFoundError the AppleScript SEED_NOT_FOUND path
        produces — caller can't tell the difference. (#205)
        """
        mock_resolve.return_value = None
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply",
                seed_id="missing@example.com",
                body="x",
            )
        # AppleScript should not run if we can't resolve the seed.
        mock_run.assert_not_called()


class TestExtractDraftAttachments:
    """Tests for AppleMailConnector.extract_draft_attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_invalid_id_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.extract_draft_attachments(
                "../escape", ["foo.pdf"], tmp_path
            )

    def test_missing_dest_dir_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.extract_draft_attachments(
                "160991", ["foo.pdf"], tmp_path / "nonexistent"
            )

    def test_no_attachments_returns_empty(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Empty attachment list short-circuits without calling AppleScript.
        result = connector.extract_draft_attachments("160991", [], tmp_path)
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_extract_creates_subdirs_and_returns_paths(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate AppleScript actually writing the files (the real
        # Mail.app would save here). We poke real bytes into the
        # expected paths so the file-existence filter at the end picks
        # them up.
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "report.pdf").write_bytes(b"%PDF-fake")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            (tmp_path / "1" / "data.csv").write_text("a,b,c")
            return "2"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["report.pdf", "data.csv"], tmp_path
        )
        assert paths == [
            tmp_path / "0" / "report.pdf",
            tmp_path / "1" / "data.csv",
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_partial_extraction_returns_only_existing(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate Mail.app writing only the first file (e.g., second
        # attachment was a Mail-internal sentinel that errored on save).
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "ok.pdf").write_bytes(b"x")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            return "1"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["ok.pdf", "missing.pdf"], tmp_path
        )
        assert paths == [tmp_path / "0" / "ok.pdf"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "ERR_NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.extract_draft_attachments(
                "999999", ["foo.pdf"], tmp_path
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_save_command(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "0"
        connector.extract_draft_attachments(
            "160991", ["a.pdf"], tmp_path
        )
        script = mock_run.call_args[0][0]
        assert "save a in (POSIX file tp)" in script
        assert "mail attachments of foundDraft" in script


def _compose_meta(
    window: str,
    *,
    subject: str | None = None,
    to: list[str] | None = None,
    before_ids: list[int] | None = None,
) -> str:
    """What the composition's first script reports: the window it opened,
    the subject and recipients Mail holds, and the ids of the mailbox its
    ending is looked up in (Drafts for a save, Sent for a send) before."""
    return json.dumps({
        "window": window,
        "subject": window if subject is None else subject,
        "to": ["a@example.com"] if to is None else to,
        "cc": [],
        "bcc": [],
        "before_ids": [5, 6] if before_ids is None else before_ids,
    })


# What a send returns when it finds its Sent copy: Mail's id for the copy
# a well-behaved Mail files beside the two _compose_meta saw, and its
# RFC Message-ID, bare.
_SENT = {
    "draft_id": "",
    "sent_message_id": "9",
    "sent_rfc_message_id": "copy-9@example.com",
}


def _sent_copy_outcomes(
    file_names: list[str] | None = None, *, ids: list[int] | None = None
) -> list[str]:
    """What Mail answers to a send's look for its Sent copy: the ids of
    the Sent messages with its subject (the two it had before and the new
    one), then that one copy read: its id, Message-ID and files."""
    return [
        json.dumps([5, 6, 9] if ids is None else ids),
        json.dumps({
            "id": "9",
            "message_id": "<copy-9@example.com>",
            "attachment_names": file_names or [],
        }),
    ]


def _compose_outcomes(
    body: str,
    *,
    window: str,
    seed: str = "new",
    plain: bool = False,
    file_names: list[str] | None = None,
    send: bool = True,
    draft_id: str = "7",
) -> list[str]:
    """What a well-behaved Mail answers to the composition, script by
    script: open the window; paste the body and read it back (always on a
    fresh message, on a reply or forward only when there is a body); with
    files, the file paste and the AX verify; then the verified send and
    the look for its Sent copy, or the close with Save and the new draft's
    id."""
    outcomes = [_compose_meta(window)]
    if seed == "new" or body:
        snippet, _ = paste_probe_strings(body, plain=plain)
        outcomes += ["PASTED_UNVERIFIED", f"pad {snippet} pad"]
    if file_names:
        outcomes += ["PASTED_UNVERIFIED", "ATTACHMENTS_VERIFIED"]
    if send:
        outcomes += ["SENT", *_sent_copy_outcomes(file_names)]
    else:
        outcomes += ["SALVAGED", draft_id]
    return outcomes


def _snapshot_before_compose(connector: AppleMailConnector) -> None:
    """Answer the read of Mail's windows a composition takes before it
    opens its own (``_mail_window_snapshot``) with no windows, so a test's
    scripted outcomes start at the opening script."""
    connector._mail_window_snapshot = lambda: WindowSnapshot(  # type: ignore[method-assign]
        mail_pid=77701, window_ids=frozenset()
    )


def _scripted(connector: AppleMailConnector, outcomes: list[str]) -> list[str]:
    """Answer the connector's osascript calls with ``outcomes`` in order
    (the last repeats) and return the list each script is captured in.
    The window snapshot a composition reads first is answered apart
    (``_snapshot_before_compose``)."""
    captured: list[str] = []

    def fake_run(script: str) -> str:
        captured.append(script)
        return outcomes[min(len(captured) - 1, len(outcomes) - 1)]

    connector._run_applescript = fake_run  # type: ignore[method-assign]
    _snapshot_before_compose(connector)
    return captured


def _run_html_flow(
    connector: AppleMailConnector,
    *,
    body: str = "<p>Hi there probe</p>",
    subject: str = "Hello",
) -> list[str]:
    """Drive the fresh HTML flow with a well-behaved mock and return the
    captured scripts: [compose, paste, read-back, verified-send, the Sent
    ids with its subject, its Sent copy read]."""
    captured = _scripted(connector, _compose_outcomes(body, window=subject))
    result = connector._send_html_email(
        to=["test@example.com"],
        cc=None,
        bcc=None,
        subject=subject,
        body=body,
        from_account=None,
    )
    assert result == _SENT
    return captured


class TestSendHtmlEmail:
    """Tests for AppleMailConnector._send_html_email (fresh mode).

    A fresh HTML send is the one composition (``_compose``): SIX
    osascript invocations — compose a visible window → verified paste →
    read-back (fresh process — same-process AX reads are stale after a
    WebKit re-render) → verified send → the Sent ids with its subject →
    its Sent copy read.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_html_send_uses_clipboard_path(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = _run_html_flow(connector)
        assert len(scripts) == 6
        compose_s, paste_s, readback_s, send_s, _, _ = scripts
        assert "make new outgoing message" in compose_s
        assert all("open location" not in s for s in scripts)
        # Clipboard-inject landmarks live in the paste script, which
        # replaces the seeded body rather than pasting above it.
        assert "public.html" in paste_s
        assert "Make Rich Text" in paste_s
        assert "AXWebArea" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "AXWebArea" in readback_s
        assert "click sendBtn" in send_s
        # Must NOT use the draft-save path anywhere, nor set content.
        assert all("saving yes" not in s for s in scripts)
        assert all("set content" not in s for s in scripts)

    def test_html_send_no_body_area_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """NO_BODY_AREA from the paste step → MailAppleScriptError."""
        _scripted(connector, [_compose_meta("Hi"), "NO_BODY_AREA:x"])
        with pytest.raises(MailAppleScriptError, match="NO_BODY_AREA"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="<p>x</p>",
                from_account=None,
            )

    def test_a_reply_and_a_forward_at_once_are_refused(
        self, connector: AppleMailConnector
    ) -> None:
        """One message is one seed: refused before any AppleScript."""
        captured = _scripted(connector, ["unused"])
        with pytest.raises(ValueError, match="mutually exclusive"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="",
                body="<p>x</p>",
                from_account=None,
                reply_to="12345",
                forward_of="67890",
            )
        assert captured == []

    def test_html_fresh_names_its_sender(
        self, connector: AppleMailConnector
    ) -> None:
        """A fresh HTML send sets the named account as the sender of the
        window it composes; before this it was refused, since the mailto:
        window it used could not take one."""
        captured = _scripted(connector, _compose_outcomes("<p>x</p>", window="Hi"))
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="<p>x</p>",
                from_account="Work",
            )
        resolve.assert_called_once_with("Work")
        assert 'set sender of theMessage to "Alice Smith <me@example.com>"' in captured[0]

    def test_html_send_fresh_carries_cc_bcc(
        self, connector: AppleMailConnector
    ) -> None:
        """Regression: the fresh path once accepted cc/bcc, allowlist-
        validated them, then silently dropped them — the mail went out
        WITHOUT them. They are recipients of the composed message."""
        captured = _scripted(connector, _compose_outcomes("<p>x</p>", window="Hi"))
        connector._send_html_email(
            to=["test@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Hi",
            body="<p>x</p>",
            from_account=None,
        )
        compose_s = captured[0]
        for kind, addr in (("cc", "cc1@example.com"), ("bcc", "bcc1@example.com")):
            assert f'repeat with addr in {{"{addr}"}}' in compose_s
            assert (
                f"make new {kind} recipient at end of {kind} recipients of "
                "theMessage with properties {address:addr}"
            ) in compose_s

    def test_html_send_escapes_subject(
        self, connector: AppleMailConnector
    ) -> None:
        """A subject with AppleScript-special characters is escaped in the
        compose script."""
        scripts = _run_html_flow(connector, subject='Hello "World" & <Test>')
        assert 'subject:"Hello \\"World\\" & <Test>"' in scripts[0]

    def test_html_send_escapes_body(
        self, connector: AppleMailConnector
    ) -> None:
        """Body with AppleScript-special characters (quotes, backslashes) is
        escaped before interpolation so the paste script remains valid."""
        scripts = _run_html_flow(
            connector, body='<p>He said "hello" and back\\slash probe</p>'
        )
        paste_s = scripts[1]
        assert '\\"' in paste_s or "\\\\back" in paste_s

    @pytest.mark.parametrize("group", ["to", "cc", "bcc"])
    def test_html_fresh_refuses_an_off_list_recipient_itself(
        self,
        connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        group: str,
    ) -> None:
        """The allowlist is met at the connector as well as at the tool
        (docs/guides/SECURITY_CHECKLIST.md): a fresh send refuses an
        off-list recipient in any group itself, before any AppleScript
        runs, whatever its caller checked."""
        policy = tmp_path / "only-example-org.yaml"
        policy.write_text("email:\n  allowed_outbound:\n    - '*@example.org'\n")
        monkeypatch.setenv("APPLE_MAIL_MCP_COMMS_CONFIG", str(policy))
        recipients: dict[str, list[str] | None] = {
            "to": ["a@example.org"], "cc": None, "bcc": None,
        }
        recipients[group] = ["off@example.com"]
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="off@example.com"):
            connector._send_html_email(
                to=recipients["to"] or [],
                cc=recipients["cc"],
                bcc=recipients["bcc"],
                subject="Hi",
                body="<p>x</p>",
                from_account=None,
            )
        assert captured == []


class TestSendHtmlWithAttachments:
    """Fresh HTML send WITH attachments: the fresh composition with the
    files pasted after the body (as file URLs — attached through the
    dictionary after the paste, they brought Mail's cite blockquote
    back, measured 2026-09-27).

    Flow is EIGHT osascript invocations: compose → paste → read-back →
    file paste → AX attachment verify (the send is NOT attempted unless
    every file is visible in the compose window) → verified send → the
    Sent ids with its subject → its Sent copy read, files and all.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def _drive(
        self,
        connector: AppleMailConnector,
        tmp_path: Path,
        *,
        outcomes_override: dict[int, str] | None = None,
        n_files: int = 1,
    ) -> tuple[list[str], list[Path], dict[str, Any] | None, Exception | None]:
        files = []
        for i in range(n_files):
            f = tmp_path / f"att{i}.txt"
            f.write_text(f"content {i}")
            files.append(f)
        body = "<p>with attachment probe</p>"
        outcomes = _compose_outcomes(
            body, window="Attached", file_names=[f.name for f in files]
        )
        for idx, val in (outcomes_override or {}).items():
            outcomes[idx] = val
        captured = _scripted(connector, outcomes)
        result: dict[str, Any] | None = None
        err: Exception | None = None
        try:
            result = connector._send_html_email(
                to=["test@example.com"],
                cc=["cc1@example.com"],
                bcc=None,
                subject="Attached",
                body=body,
                from_account=None,
                attachment_paths=files,
            )
        except Exception as e:  # noqa: BLE001
            err = e
        return captured, files, result, err

    def test_happy_path_eight_scripts(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        scripts, files, result, err = self._drive(connector, tmp_path)
        assert err is None
        assert result == _SENT
        assert len(scripts) == 8
        (compose_s, paste_s, readback_s, files_s, verify_s, send_s,
         ids_s, copy_s) = scripts
        # Compose: scriptable outgoing message, recipients through the
        # model, no attachment through the dictionary.
        assert "make new outgoing message" in compose_s
        assert "open location" not in compose_s
        assert "make new attachment" not in compose_s
        assert "test@example.com" in compose_s
        assert "cc1@example.com" in compose_s
        # The body replaces the seed, then the files go in after it.
        assert "public.html" in paste_s
        assert 'keystroke "a" using command down' in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert files[0].resolve().as_posix() in files_s
        assert "key code 125 using command down" in files_s
        # AX verify names the file.
        assert files[0].name in verify_s
        assert "click sendBtn" in send_s
        # Post-send: the new Sent id with the subject, then that copy's
        # files, read by its id.
        assert 'id of every message of sent mailbox whose subject is "Attached"' in ids_s
        assert 'first message of sent mailbox whose id is "9"' in copy_s
        assert "name of every mail attachment of m" in copy_s

    def test_missing_file_raises_before_applescript(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        called: list[bool] = []
        connector._run_applescript = lambda _: (called.append(True), "X")[1]  # type: ignore[method-assign]
        with pytest.raises(FileNotFoundError):
            connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None,
                subject="s", body="<p>b</p>", from_account=None,
                attachment_paths=[tmp_path / "ghost.pdf"],
            )
        assert not called

    def test_ax_verify_failure_raises_and_no_send(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """If the attachment never appears in the compose window's AX
        tree, the send must NOT be attempted (would dispatch without the
        attachment — a silent failure)."""
        scripts, _, result, err = self._drive(
            connector, tmp_path,
            outcomes_override={4: "ATTACH_MISSING:att0.txt"},
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "att0.txt" in str(err)
        assert "NOT attempted" in str(err)
        # compose, paste, read-back, file paste, verify, salvage — no send.
        assert len(scripts) == 6
        assert "AXCloseButton" in scripts[5]
        assert all("click sendBtn" not in s for s in scripts)

    def test_a_file_missing_from_the_sent_copy_raises(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Dispatch succeeded but the Sent copy lacks a file that was
        attached → loud error naming it (the mail DID go out; the error
        must say so)."""
        scripts, _, result, err = self._drive(
            connector, tmp_path, n_files=2,
            outcomes_override={7: _sent_copy_outcomes(["att1.txt"])[1]},
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "WAS sent" in str(err)
        assert "att0.txt" in str(err)
        assert "id 9" in str(err)
        assert len(scripts) == 8

    def test_the_sent_copy_may_carry_more_than_was_pasted(
        self, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """A forward's Sent copy carries the original's files as well as
        the caller's: every file pasted must be there, not only those."""
        _, _, result, err = self._drive(
            connector, tmp_path,
            outcomes_override={7: _sent_copy_outcomes(["theirs.pdf", "att0.txt"])[1]},
        )
        assert err is None
        assert result == _SENT

    def test_ax_verify_accepts_a_file_shown_as_an_image(self) -> None:
        """A compose window shows a PNG inline, as an AXImage described
        by its file name, and a PDF or text file as an AXButton described
        "name.ext, N KB" (measured 2026-09-27,
        docs/research/icloud-draft-resync.md, Observation 10). The
        check matching only AXButton refused every image. What an image
        looks like live is covered by test_loopback.py; here, that the
        script accepts either role for a description naming the file."""
        script = build_attachment_ax_verify_script(
            window_name="w", filenames=["probe.png"],
        )
        assert (
            '(r is "AXButton" or r is "AXImage") and '
            "((description of el) as text) contains fname"
        ) in script
        assert '"probe.png"' in script


def _sent_mailbox(
    ids: list[list[int]], names: list[str] | None = None
) -> Callable[[str], str]:
    """Mail after a send, as the look for its Sent copy sees it: the ids
    of the Sent messages with its subject, one list a look (the last
    repeats), and copy 9, read by its id, carrying ``names``."""
    looks: list[str] = []

    def answer(script: str) -> str:
        if "whose id is" in script:
            return _sent_copy_outcomes(names)[1]
        looks.append(script)
        return json.dumps(ids[min(len(looks), len(ids)) - 1])

    return answer


class TestASendFindsItsSentCopyByIdentity:
    """After the verified send, a send finds the copy it filed in Sent:
    the one message in Sent with the window's subject whose id was not
    there before the window opened (``_compose_meta`` saw 5 and 6). By
    subject alone, a send whose subject had been used before read the
    oldest copy of it, reported the file just sent missing from a
    message that carried it, and was sent again.

    A send that went out ends one of three ways: its copy found and
    carrying every file pasted (its ids returned), its copy found and
    lacking one (raised, saying the message WAS sent), or its copy not
    identified (no id, and one warning saying why)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @pytest.fixture
    def sleeps(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        from apple_mail_mcp import mail_connector as mc_mod

        slept: list[float] = []
        monkeypatch.setattr(mc_mod.time, "sleep", slept.append)
        return slept

    def _send(
        self,
        connector: AppleMailConnector,
        tmp_path: Path,
        sent_mailbox: Callable[[str], str],
        *,
        files: tuple[str, ...] = (),
        subject: str = "Same subject",
    ) -> tuple[list[str], dict[str, Any] | None, Exception | None]:
        """Send with a well-behaved Mail up to SENT, then ``sent_mailbox``
        answering the look for the copy; the scripts after SENT, and the
        result or what was raised."""
        paths = []
        for name in files:
            path = tmp_path / name
            path.write_text(name)
            paths.append(path)
        body = "<p>again probe</p>"
        until_sent = _compose_outcomes(body, window=subject, file_names=list(files))[:-2]
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            if len(captured) <= len(until_sent):
                return until_sent[len(captured) - 1]
            return sent_mailbox(script)

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        try:
            result = connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject=subject,
                body=body, from_account=None, attachment_paths=paths or None,
            )
        except Exception as e:  # noqa: BLE001
            return captured[len(until_sent):], None, e
        return captured[len(until_sent):], result, None

    def test_the_copy_is_the_one_id_the_snapshot_did_not_have(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 9, 6]])
        )
        assert err is None
        assert result == _SENT
        assert len(after) == 2
        assert (
            'id of every message of sent mailbox whose subject is "Same subject"'
        ) in after[0]
        assert 'first message of sent mailbox whose id is "9"' in after[1]
        assert sleeps == []

    def test_the_files_are_checked_on_that_copy_not_an_older_one(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Copy 5 is an earlier send of the same subject with other files:
        it is never read, so what it carries decides nothing."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 9]], ["archive.zip"]),
            files=("archive.zip",),
        )
        assert err is None
        assert result == _SENT
        assert all('whose id is "5"' not in s for s in after)
        assert all("first message of sent mailbox whose subject" not in s for s in after)

    def test_a_copy_listed_late_is_waited_for(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """A subject Sent already held satisfies the verified send before
        the new copy is listed; the look waits for it."""
        _, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6], [5, 6], [5, 6], [5, 6, 9]])
        )
        assert err is None
        assert result == _SENT
        assert sleeps == [AppleMailConnector._SENT_APPEAR_INTERVAL_S] * 3

    def test_a_copy_never_listed_is_a_warning_not_an_error(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Mail accepted the message (its window closed after Send, with no
        sheet): nothing is raised, no id is guessed, the warning says what
        was seen and what was not, and the files are said to be
        unverified, not missing."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6]], ["archive.zip"]),
            files=("archive.zip",),
        )
        assert err is None
        assert result is not None
        polls = AppleMailConnector._SENT_APPEAR_POLLS
        interval = AppleMailConnector._SENT_APPEAR_INTERVAL_S
        assert sleeps == [interval] * polls
        assert len(after) == polls + 1
        assert all("whose id is" not in s for s in after)
        assert result["sent_message_id"] == ""
        assert result["sent_rfc_message_id"] == ""
        assert result["draft_id"] == ""
        [warning] = result["warnings"]
        assert "Mail accepted the message" in warning
        assert "window closed after Send, with no sheet" in warning
        assert f"within {polls * interval:g}s" in warning
        assert "unverified" in warning
        assert "lacks" not in warning
        assert "Outbox" in warning

    def test_without_files_the_warning_says_nothing_of_files(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        _, result, _ = self._send(connector, tmp_path, _sent_mailbox([[5, 6]]))
        assert result is not None
        [warning] = result["warnings"]
        assert "file" not in warning

    def test_two_new_copies_are_not_guessed_between(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Another send of the same subject since the window opened: which
        copy is this one's cannot be told by id, and waiting will not
        tell it either."""
        after, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9, 10]])
        )
        assert err is None
        assert result is not None
        assert result["sent_message_id"] == ""
        [warning] = result["warnings"]
        assert "2 messages" in warning
        assert len(after) == 1
        assert sleeps == []

    def test_a_failed_look_is_a_warning_and_the_window_stays_sent(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        ends: list[Any] = []
        connector._record_window_end = (  # type: ignore[method-assign]
            lambda window, state: ends.append(state)
        )

        def busy(script: str) -> str:
            raise MailAppleScriptError("Mail automation busy")

        _, result, err = self._send(connector, tmp_path, busy)
        assert err is None
        assert result is not None
        assert result["sent_message_id"] == ""
        [warning] = result["warnings"]
        assert "Mail automation busy" in warning
        assert [state.how for state in ends] == ["sent"]

    def test_a_file_missing_from_the_found_copy_raises_that_it_was_sent(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        _, result, err = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9]], ["other.pdf"]),
            files=("archive.zip",),
        )
        assert result is None
        assert isinstance(err, MailAppleScriptError)
        assert "WAS sent" in str(err)
        assert "archive.zip" in str(err)
        assert "id 9" in str(err)

    def test_the_subject_is_escaped_in_the_look(
        self, connector: AppleMailConnector, tmp_path: Path, sleeps: list[float]
    ) -> None:
        after, _, _ = self._send(
            connector, tmp_path, _sent_mailbox([[5, 6, 9]]), subject='Say "hi"'
        )
        assert 'whose subject is "Say \\"hi\\""' in after[0]


_OVERLONG = SANITIZE_MAX_LENGTH + 1


def _overlong_fresh_html(c: AppleMailConnector) -> None:
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="s",
        body="x" * _OVERLONG, from_account=None,
    )


def _overlong_html_reply(c: AppleMailConnector) -> None:
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="",
        body="x" * _OVERLONG, from_account=None, reply_to="12345",
    )


def _overlong_html_forward(c: AppleMailConnector) -> None:
    """By RFC Message-ID, which is looked up in Mail unless refused first."""
    c._send_html_email(
        to=["a@example.com"], cc=None, bcc=None, subject="",
        body="x" * _OVERLONG, from_account=None, forward_of="abc@example.com",
    )


def _overlong_fresh_plain_send(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="new", to=["a@example.com"], subject="s",
        body="x" * _OVERLONG, send_now=True,
    )


def _overlong_fresh_saved_draft(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="new", to=["a@example.com"], subject="s", body="x" * _OVERLONG,
    )


def _overlong_reply_note(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="reply", seed_id="12345", to=["a@example.com"],
        body="x" * _OVERLONG,
    )


def _overlong_forward_note_sent(c: AppleMailConnector) -> None:
    c.create_draft(
        seed="forward", seed_id="12345", to=["a@example.com"],
        body="x" * _OVERLONG, send_now=True,
    )


class TestAnOverlongBodyIsRefusedNotCut:
    """``sanitize_input`` cuts at SANITIZE_MAX_LENGTH without a word, and
    every body a caller hands the connector passes through it on its way
    to a paste (a fresh draft or send, an HTML reply or forward, a note
    above a reply or forward). A longer body is refused, on every path,
    with one message, before anything is composed or looked up."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @pytest.mark.parametrize(
        "call",
        [
            _overlong_fresh_html,
            _overlong_html_reply,
            _overlong_html_forward,
            _overlong_fresh_plain_send,
            _overlong_fresh_saved_draft,
            _overlong_reply_note,
            _overlong_forward_note_sent,
        ],
        ids=lambda f: f.__name__.removeprefix("_overlong_"),
    )
    def test_refused_before_any_applescript(
        self, connector: AppleMailConnector, call: Any
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with pytest.raises(ValueError) as refused:
            call(connector)
        assert str(refused.value) == (
            f"body is {_OVERLONG} characters; a message body carries at "
            f"most {SANITIZE_MAX_LENGTH}. Nothing was composed, saved or "
            "sent."
        )
        assert captured == []

    def test_a_body_at_the_limit_is_carried_whole(
        self, connector: AppleMailConnector
    ) -> None:
        body = "y" * SANITIZE_MAX_LENGTH
        captured = _scripted(
            connector,
            _compose_outcomes(
                body, window="s", plain=True, send=False, draft_id="4242",
            ),
        )
        result = connector.create_draft(
            seed="new", to=["a@example.com"], subject="s", body=body,
        )
        assert result["draft_id"] == "4242"
        assert f'set theBody to "{body}"' in captured[1]


class TestVerifiedSendPrimitives:
    """Phase 0 of PLAN-html-reply-send: every UI action in the send paths
    gets a mechanical read-back (docs/reference/UI_GROUNDING_MAIL_SEND.md).

    Contract for the verified send every compose-window path ends in
    (fresh plain, fresh HTML, reply):
      - PRE: Send button resolved on a window found BY NAME (never a bare
        `window 1`), existence checked, and `enabled` checked — clicking a
        disabled button is a silent no-op (the 2026-07-20 vanished send).
      - ACT: click by AX reference.
      - POST: poll up to 15 s until the compose window is gone (SENT); a
        sheet mid-flight surfaces its static texts (SHEET:), and a window
        still open with no sheet is WINDOW_STILL_OPEN. The copy in Sent is
        not the block's: the send looks for it by identity afterwards.
      - All non-SENT sentinels raise MailAppleScriptError with the detail.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # Where the verified send sits among a fresh send's scripts: after
    # compose, paste and read-back, before the look for its Sent copy.
    _SEND_AT = 3

    def _plain_fresh_scripts(self, connector: AppleMailConnector) -> list[str]:
        """Captured scripts for a fresh plain send through create_draft:
        [compose, paste, read-back, verified-send, Sent ids, Sent copy]."""
        captured = _scripted(
            connector, _compose_outcomes("x", window="Probe", plain=True)
        )
        connector.create_draft(
            seed="new",
            to=["test@example.com"],
            subject="Probe",
            body="x",
            send_now=True,
        )
        return captured

    def _html_scripts(self, connector: AppleMailConnector) -> list[str]:
        """Captured scripts for the fresh HTML flow:
        [compose, paste, read-back, verified-send, Sent ids, Sent copy]."""
        return _run_html_flow(connector)

    # -- precondition: send-enabled check, window by name ------------------

    def test_plain_fresh_send_checks_send_enabled(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._plain_fresh_scripts(connector)[self._SEND_AT]
        assert "enabled of sendBtn" in send_s
        assert "SEND_DISABLED" in send_s

    def test_html_script_checks_send_enabled(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._html_scripts(connector)[self._SEND_AT]
        assert "enabled of sendBtn" in send_s
        assert "SEND_DISABLED" in send_s

    def test_html_script_resolves_window_by_name(
        self, connector: AppleMailConnector
    ) -> None:
        """The HTML path may not act on a bare `window 1` — the compose
        window is resolved by its name in every script."""
        for script in self._html_scripts(connector):
            assert "set w to window 1" not in script

    # -- postcondition: the window gone, sheet surfacing --------------------

    @staticmethod
    def _assert_the_postcondition_is_the_window(send_s: str) -> None:
        """Gone within 15 s is SENT; a sheet is SHEET:; still open with no
        sheet is WINDOW_STILL_OPEN. Never a Sent lookup: by subject it
        was satisfied by any earlier message of the subject, and a copy
        slower than 15 s would make a message that went read as not sent."""
        assert "repeat 15 times" in send_s
        assert 'if not winOpen then\n                    set sendOutcome to "SENT"' in send_s
        assert '"SHEET:"' in send_s
        assert '"WINDOW_STILL_OPEN:' in send_s
        assert "sent mailbox" not in send_s
        assert "composeSubject" not in send_s
        assert "POSTCONDITION_TIMEOUT" not in send_s

    def test_plain_fresh_send_verifies_dispatch(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._plain_fresh_scripts(connector)[self._SEND_AT]
        self._assert_the_postcondition_is_the_window(send_s)

    def test_html_script_verifies_dispatch(
        self, connector: AppleMailConnector
    ) -> None:
        send_s = self._html_scripts(connector)[self._SEND_AT]
        self._assert_the_postcondition_is_the_window(send_s)

    def test_a_window_still_open_is_not_sent_and_is_saved_to_drafts(
        self, connector: AppleMailConnector
    ) -> None:
        """WINDOW_STILL_OPEN raises, says what was seen, salvages the
        window to Drafts, and no Sent copy is looked for."""
        outcomes = _compose_outcomes("x", window="Probe", plain=True)
        still_open = "WINDOW_STILL_OPEN:no sheet, still open 15s after Send on Probe"
        captured = _scripted(
            connector, outcomes[:self._SEND_AT] + [still_open, "SALVAGED"]
        )
        with pytest.raises(MailAppleScriptError, match="WINDOW_STILL_OPEN") as exc:
            connector.create_draft(
                seed="new", to=["test@example.com"], subject="Probe", body="x",
                send_now=True,
            )
        assert "compose window: SALVAGED" in str(exc.value)
        assert len(captured) == self._SEND_AT + 2
        assert 'click button "Save" of sheet 1 of window closeIdx' in captured[-1]
        assert all("sent mailbox whose subject" not in s for s in captured)

    def test_a_window_gone_goes_on_to_look_for_the_copy(
        self, connector: AppleMailConnector
    ) -> None:
        """SENT from the block is the window gone; whether the copy
        reached Sent is the look's to find, after it."""
        scripts = self._plain_fresh_scripts(connector)
        assert "click sendBtn" in scripts[self._SEND_AT]
        assert "sent mailbox whose subject is" in scripts[self._SEND_AT + 1]

    # -- paste read-back (2026-07-22 raw-<p> regression) -------------------

    def test_html_paste_script_verifies_focus(
        self, connector: AppleMailConnector
    ) -> None:
        """Focus is SET and VERIFIED (a click can leave focus in the To
        field, sending cmd+v to the wrong control)."""
        paste_s = self._html_scripts(connector)[1]
        assert "set focused of bodyArea to true" in paste_s
        assert "AXFocusedUIElement" in paste_s
        assert "PASTE_FOCUS_FAILED" in paste_s

    def test_html_readback_runs_in_fresh_process_and_gates_send(
        self, connector: AppleMailConnector
    ) -> None:
        """A bad read-back (raw source visible = paste degraded to
        literal text) must retry once then raise PASTE_FAILED WITHOUT
        ever running the send script — shipping without this sent
        literal <p> tags on 2026-07-21."""
        body = "<p>Hi there probe</p>"
        captured: list[str] = []

        def fake_run(script: str) -> str:
            captured.append(script)
            n = len(captured)
            if n == 1:
                return _compose_meta("Probe", to=["test@example.com"])
            if n in (2, 4):  # paste attempts
                return "PASTED_UNVERIFIED"
            return "<p>Hi there probe</p>"  # read-back sees RAW source

        connector._run_applescript = fake_run  # type: ignore[method-assign]
        with pytest.raises(MailAppleScriptError, match="PASTE_FAILED"):
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Probe",
                body=body,
                from_account=None,
            )
        # compose, paste, readback, retry-paste (with undo), retry-readback,
        # salvage-to-draft — and NO send script.
        assert len(captured) == 6
        assert 'keystroke "z" using command down' in captured[3]
        # Headless policy: the failed compose window is salvaged to
        # Drafts (close → Save), never left open.
        assert 'button "Save"' in captured[5]
        assert all("click sendBtn" not in s for s in captured)

    def test_paste_probe_strings(self) -> None:
        snippet, raw = paste_probe_strings(
            "<p>Hello <b>world</b> of probes</p>"
        )
        assert snippet.startswith("Hello world")
        assert "<" not in snippet
        assert raw.startswith("<p>Hello ") and len(raw) == 16
        # Text-led body: no raw marker (nothing tag-like to detect).
        _, raw2 = paste_probe_strings("plain text body")
        assert raw2 == ""

    # -- clipboard restore must survive AppleScript errors -----------------

    def test_html_paste_script_restores_clipboard_on_error(
        self, connector: AppleMailConnector
    ) -> None:
        """The paste script is wrapped so a mid-script error still restores
        the user's clipboard before the error propagates; the success path
        restores IMMEDIATELY after the paste (shortest possible hold)."""
        paste_s = self._html_scripts(connector)[1]
        assert "on error" in paste_s
        # restore loop appears at least twice: success path + error path
        assert paste_s.count("repeat with pair in savedPairs") >= 2

    # -- sentinel → exception mapping --------------------------------------

    @pytest.mark.parametrize(
        "sentinel",
        ["SEND_DISABLED", "SHEET:Save this message as a draft?",
         "WINDOW_STILL_OPEN:window still open"],
    )
    def test_plain_fresh_sentinels_raise_with_detail(
        self, connector: AppleMailConnector, sentinel: str
    ) -> None:
        outcomes = _compose_outcomes("x", window="Probe", plain=True)
        _scripted(connector, outcomes[:self._SEND_AT] + [sentinel])
        with pytest.raises(MailAppleScriptError) as exc:
            connector.create_draft(
                seed="new",
                to=["test@example.com"],
                subject="Probe",
                body="x",
                send_now=True,
            )
        assert sentinel.split(":")[0] in str(exc.value)

    @pytest.mark.parametrize(
        "sentinel",
        ["SEND_DISABLED", "SHEET:Save this message as a draft?",
         "WINDOW_STILL_OPEN:window still open"],
    )
    def test_html_sentinels_raise_with_detail(
        self, connector: AppleMailConnector, sentinel: str
    ) -> None:
        """Verified-send sentinels from the send script surface as errors."""
        body = "<p>Hi there probe</p>"
        outcomes = _compose_outcomes(body, window="Probe")
        _scripted(connector, outcomes[:self._SEND_AT] + [sentinel])
        with pytest.raises(MailAppleScriptError) as exc:
            connector._send_html_email(
                to=["test@example.com"],
                cc=None,
                bcc=None,
                subject="Probe",
                body=body,
                from_account=None,
            )
        assert sentinel.split(":")[0] in str(exc.value)

    # -- closing one compose window ----------------------------------------
    #
    # System Events turns `set w to window i` into a reference by name,
    # which reads and clicks the first window of that name
    # (docs/research/compose-window-tending.md, Observation 7). A close
    # finds its window by Mail's id, ties it to System Events' list by
    # name and position, and addresses it by index alone.

    def _close_script(
        self, connector: AppleMailConnector, mode: str, window_id: int | None = 2781
    ) -> str:
        captured = _scripted(connector, ["X"])
        if mode == "save":
            connector._salvage_compose_to_draft('Probe "1"', window_id)
        else:
            connector._discard_compose_window('Probe "1"', window_id)
        assert len(captured) == 1
        return captured[0]

    def test_discard_uses_the_curly_apostrophe_and_verifies(
        self, connector: AppleMailConnector
    ) -> None:
        """`Don’t Save` carries U+2019 (a straight quote never matches) and
        the block must verify the window actually closed — both Mail-
        dictionary discard routes fail silently (grounding report)."""
        script = self._close_script(connector, "discard")
        assert 'click button "Don’t Save" of sheet 1 of window closeIdx' in script
        assert 'set closeOutcome to "DISCARDED" & sheetNote' in script
        assert 'set closeOutcome to "DISCARD_FAILED:window still open"' in script

    @pytest.mark.parametrize("mode", ["save", "discard"])
    def test_a_close_addresses_mails_window_by_id(
        self, connector: AppleMailConnector, mode: str
    ) -> None:
        script = self._close_script(connector, mode)
        assert script.startswith('set closeId to 2781\nset closeName to "Probe \\"1\\""')
        assert "set idBounds to bounds of window id closeId" in script
        # Tied to System Events' list by name and position, before any click.
        found_at = script.index("set closeIdx to my tendIndexOf(closeName, closePos)")
        assert found_at < script.index("my tendClickClose(closeIdx)")
        # Closed means Mail's window of that id is gone: not listed, or
        # listed neither visible nor minimized and gone from System Events
        # at its place (Mail keeps some closed windows listed).
        assert "if not (exists window id wid) then return true" in script
        assert "if (visible of window id wid) or (miniaturized of window id wid) then return false" in script
        assert "return (my tendIndexOf(nm, pos)) is 0" in script
        assert script.count("my tendGone(closeId, closeName, closePos)") == 2

    @pytest.mark.parametrize("mode", ["save", "discard"])
    def test_no_window_is_held_in_a_variable_or_clicked_by_name(
        self, connector: AppleMailConnector, mode: str
    ) -> None:
        """Held in a variable, a System Events window is a by-name
        reference; so is any element found in it."""
        script = self._close_script(connector, mode)
        assert "to window " not in script.replace("to window id", "")
        assert "repeat with w in windows" not in script
        assert "click button k of window i" in script
        assert "of window closeName" not in script
        assert "first button of window" not in script

    def test_a_window_mail_cannot_tell_from_another_in_its_place_is_left(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        twins_at = script.index("if twins is not 1 then")
        assert twins_at < script.index("my tendClickClose(closeIdx)")
        assert "so which to close cannot be told; none was closed" in script

    def test_a_window_renamed_since_is_left(self, connector: AppleMailConnector) -> None:
        script = self._close_script(connector, "save")
        renamed_at = script.index("if closeOutcome is \"\" and idName is not closeName")
        assert renamed_at < script.index("my tendClickClose(closeIdx)")

    def test_without_an_id_only_the_one_window_of_its_name_is_closed(
        self, connector: AppleMailConnector
    ) -> None:
        """A window the opening script got no id for is closed by name,
        and only while its name is the only one: the first window of a
        name could be someone else's."""
        script = self._close_script(connector, "save", window_id=None)
        assert script.startswith("set closeId to 0\n")
        count_at = script.index("set nameCount to count of (windows whose name is closeName)")
        assert count_at < script.index("my tendClickClose(closeIdx)")
        assert "else if nameCount > 1 then" in script
        assert "so which to close cannot be told by name; none was closed" in script

    def test_a_close_that_cannot_run_is_a_failure_of_its_kind(
        self, connector: AppleMailConnector
    ) -> None:
        def fail(script: str) -> str:
            raise MailAppleScriptError("Mail got an error")

        connector._run_applescript = fail  # type: ignore[method-assign]
        assert connector._salvage_compose_to_draft("P", 1).startswith("SALVAGE_FAILED:")
        assert connector._discard_compose_window("P", 1).startswith("DISCARD_FAILED:")

    # -- salvage, and Mail's send-error sheet --------------------------------
    #
    # When Mail cannot send through the account's server it puts a sheet
    # on the compose window, "Cannot send message using the server …",
    # with Try Later / Try With Selected Server / Connection Doctor /
    # Edit SMTP Server List / Edit Message, and no Save: pressing Save on
    # it, as the salvage did, failed and left the window open. The sheet
    # cannot be provoked on demand, so these read the script's text; no
    # live test covers it.

    def test_salvage_dismisses_the_send_error_sheet_before_closing(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        edit_at = script.index('click button "Edit Message" of sheet 1 of window closeIdx')
        close_at = script.index("my tendClickClose(closeIdx)")
        save_at = script.index('click button "Save" of sheet 1 of window closeIdx')
        assert edit_at < close_at < save_at
        # Only that sheet is dismissed that way: the button is looked for
        # before it is pressed.
        assert 'exists button "Edit Message" of sheet 1 of window closeIdx' in script

    def test_salvage_reports_the_send_error_sheet_text(
        self, connector: AppleMailConnector
    ) -> None:
        script = self._close_script(connector, "save")
        assert "value of static texts of sheet 1 of window closeIdx" in script
        assert "Mail's send-error sheet: " in script
        # The text rides on every outcome after the sheet was read,
        # failures included.
        assert 'set closeOutcome to "SALVAGED" & sheetNote' in script
        assert 'set closeOutcome to "SALVAGE_FAILED:window still open" & sheetNote' in script
        assert 'set closeOutcome to "SALVAGE_FAILED:" & errMsg & sheetNote' in script
        assert "\nreturn closeOutcome\n" in script

    def test_a_discard_has_no_send_error_sheet_step(
        self, connector: AppleMailConnector
    ) -> None:
        assert "Edit Message" not in self._close_script(connector, "discard")

    def test_the_sheet_text_reaches_the_raised_error(
        self, connector: AppleMailConnector
    ) -> None:
        """Whatever the salvage reports is part of the send's error."""
        sheet = (
            "SALVAGED (Mail's send-error sheet: Cannot send message using "
            "the server smtp.example.com. | )"
        )
        outcomes = _compose_outcomes("<p>Hi there probe</p>", window="Probe")
        _scripted(
            connector, outcomes[:self._SEND_AT] + ["WINDOW_STILL_OPEN:x", sheet]
        )
        with pytest.raises(MailAppleScriptError) as exc:
            connector._send_html_email(
                to=["test@example.com"], cc=None, bcc=None, subject="Probe",
                body="<p>Hi there probe</p>", from_account=None,
            )
        assert "Cannot send message using the server smtp.example.com." in str(
            exc.value
        )


class TestHtmlReplyAndForward:
    """``_send_html_email(reply_to=...)`` and ``(forward_of=...)``: the one
    composition (``_compose``) on Mail's own reply or forward window, the
    HTML pasted above what Mail wrote and the files after it. Until
    2026-09-27 the HTML reply had a composition of its own, which took no
    files, and there was no HTML forward: the "forward" of 2026-09-26 was
    a fresh message with the original pasted into it.

    The outbound allowlist is met twice: on the recipients the caller
    names, before any AppleScript; and on the recipients the window
    holds, read back from the outgoing-message model after every
    override, which include those Mail derived for a reply. Off-list at
    the second, the window is discarded, and nothing is pasted or sent.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    _BODY = "<p>html above <b>what Mail wrote</b></p>"
    _WINDOW = {"reply": "Re: Probe", "forward": "Fwd: Probe"}

    def _send(
        self,
        connector: AppleMailConnector,
        seed: str,
        *,
        to: list[str] | None = None,
        subject: str = "",
        body: str = _BODY,
        files: list[Path] | None = None,
        from_account: str | None = None,
        seed_id: str = "12345",
        outcomes: list[str] | None = None,
    ) -> list[str]:
        """Send an HTML reply or forward against a well-behaved Mail and
        return the scripts it ran: open, paste, read-back, then (with
        files) the file paste and its check, the verified send, and the
        look for its Sent copy (the ids with its subject, the copy
        read)."""
        if outcomes is None:
            outcomes = _compose_outcomes(
                body, window=self._WINDOW[seed], seed=seed,
                file_names=[f.name for f in files or []],
            )
        captured = _scripted(connector, outcomes)
        mode = {"reply_to": seed_id} if seed == "reply" else {"forward_of": seed_id}
        result = connector._send_html_email(
            to=["a@example.com"] if to is None else to,
            cc=None,
            bcc=None,
            subject=subject,
            body=body,
            from_account=from_account,
            attachment_paths=files,
            **mode,
        )
        assert result == _SENT
        return captured

    def test_a_reply_opens_mails_reply_window_and_keeps_what_mail_derived(
        self, connector: AppleMailConnector
    ) -> None:
        """No recipients and no subject given: Mail's own stay, and the
        window's recipients are read from the model, not the UI."""
        scripts = self._send(connector, "reply", to=[])
        # open, paste, read-back, verified send, the Sent look (two)
        assert len(scripts) == 6
        open_s = scripts[0]
        assert 'whose id is "12345"' in open_s
        assert "reply origMsg opening window true" in open_s
        assert "beforeNames" in open_s
        assert "afterCount > beforeCount" in open_s
        assert "COMPOSE_WINDOW_NOT_UNIQUE" in open_s
        assert "address of to recipients of theMessage" in open_s
        assert "delete (every to recipient of theMessage)" not in open_s
        assert "set subject of theMessage" not in open_s
        # A send snapshots Sent, where its copy is looked up; never Drafts.
        assert "set beforeIds to (id of every message of sent mailbox)" in open_s
        assert "drafts mailbox" not in open_s

    def test_a_forward_opens_mails_forward_window_with_its_recipients(
        self, connector: AppleMailConnector
    ) -> None:
        scripts = self._send(connector, "forward", to=["alice@example.com"])
        open_s = scripts[0]
        assert 'whose id is "12345"' in open_s
        assert "forward origMsg opening window true" in open_s
        assert "make new outgoing message" not in open_s
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'repeat with addr in {"alice@example.com"}' in open_s

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_the_html_goes_above_what_mail_wrote_and_is_sent_verified(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """cmd+up, then the paste: never a select-all or a delete, which
        would take Mail's quote or forwarded message with it. The content
        is never set, and the window's Send button sends."""
        scripts = self._send(connector, seed)
        _, paste_s, readback_s, send_s, ids_s, _ = scripts
        assert "key code 126 using command down" in paste_s
        assert "public.html" in paste_s
        assert 'keystroke "a" using command down' not in paste_s
        assert "key code 51" not in paste_s
        assert "AXWebArea" in readback_s
        assert "enabled of sendBtn" in send_s
        assert f'set composeName to "{self._WINDOW[seed]}"' in send_s
        assert f'sent mailbox whose subject is "{self._WINDOW[seed]}"' in ids_s
        assert all("set content" not in s for s in scripts)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_files_go_after_what_mail_wrote_and_are_checked_in_the_sent_copy(
        self, connector: AppleMailConnector, tmp_path: Path, seed: str
    ) -> None:
        """Pasted at the end, never attached through the dictionary: on a
        reply or forward whose body was untouched, ``make new attachment``
        sent the original unquoted and dropped a forward's own files
        (docs/research/icloud-draft-resync.md, Observation 11). The Sent
        copy must carry the caller's file; a forward's carries the
        original's as well."""
        mine = tmp_path / "mine.txt"
        mine.write_text("mine")
        outcomes = _compose_outcomes(
            self._BODY, window=self._WINDOW[seed], seed=seed,
            file_names=["mine.txt"],
        )
        outcomes[-1] = _sent_copy_outcomes(["theirs.pdf", "mine.txt"])[1]
        scripts = self._send(connector, seed, files=[mine], outcomes=outcomes)
        assert len(scripts) == 8
        _, paste_s, _, files_s, verify_s, send_s, ids_s, copy_s = scripts
        assert "key code 126 using command down" in paste_s
        assert "writeObjects:fileURLs" in files_s
        assert mine.resolve().as_posix() in files_s
        assert "key code 125 using command down" in files_s
        assert '"mine.txt"' in verify_s
        assert "click sendBtn" in send_s
        assert f'whose subject is "{self._WINDOW[seed]}"' in ids_s
        assert 'whose id is "9"' in copy_s
        assert "name of every mail attachment of m" in copy_s
        assert all("make new attachment" not in s for s in scripts)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_file_missing_from_the_sent_copy_says_it_was_sent(
        self, connector: AppleMailConnector, tmp_path: Path, seed: str
    ) -> None:
        mine = tmp_path / "mine.txt"
        mine.write_text("mine")
        outcomes = _compose_outcomes(
            self._BODY, window=self._WINDOW[seed], seed=seed,
            file_names=["mine.txt"],
        )
        outcomes[-1] = _sent_copy_outcomes(["theirs.pdf"])[1]
        with pytest.raises(MailAppleScriptError, match="WAS sent"):
            self._send(connector, seed, files=[mine], outcomes=outcomes)

    def test_derived_recipients_off_the_list_discard_the_window(
        self, connector: AppleMailConnector
    ) -> None:
        """Mail derived an off-list recipient for the reply: the window is
        discarded, the HTML never pasted, nothing sent."""
        meta = _compose_meta("Re: Probe", to=["evil@other.com"])
        captured = _scripted(connector, [meta, "DISCARDED"])
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            connector._send_html_email(
                to=[], cc=None, bcc=None, subject="", body="<p>x</p>",
                from_account=None, reply_to="12345",
            )
        assert len(captured) == 2
        assert "AXCloseButton" in captured[1]
        assert all('keystroke "v"' not in s for s in captured)

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_off_list_recipient_the_caller_names_never_reaches_mail(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        mode = {"reply_to": "12345"} if seed == "reply" else {"forward_of": "12345"}
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="evil@other.com"):
            connector._send_html_email(
                to=["a@example.com"], cc=["evil@other.com"], bcc=None,
                subject="", body="<p>x</p>", from_account=None, **mode,
            )
        assert captured == []

    def test_a_forward_to_no_one_never_reaches_mail(
        self, connector: AppleMailConnector
    ) -> None:
        """Mail derives no recipient for a forward: the connector refuses
        one that names nobody, whatever its caller checked."""
        captured = _scripted(connector, ["unused"])
        with pytest.raises(MailOutboundDisallowedError, match="no recipients"):
            connector._send_html_email(
                to=[], cc=None, bcc=None, subject="", body="<p>x</p>",
                from_account=None, forward_of="12345",
            )
        assert captured == []

    def test_explicit_recipients_replace_mails_in_the_model(
        self, connector: AppleMailConnector
    ) -> None:
        """There is no reply-all: a caller wanting it names everyone."""
        outcomes = _compose_outcomes(self._BODY, window="Re: Probe", seed="reply")
        outcomes[0] = _compose_meta(
            "Re: Probe", to=["alice@example.com", "bob@example.com"]
        )
        scripts = self._send(
            connector, "reply", to=["alice@example.com", "bob@example.com"],
            outcomes=outcomes,
        )
        open_s = scripts[0]
        assert "delete (every to recipient of theMessage)" in open_s
        assert 'repeat with addr in {"alice@example.com", "bob@example.com"}' in open_s
        assert "reply to all" not in open_s

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_subject_given_replaces_mails(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        scripts = self._send(connector, seed, subject='Say "hi"')
        assert 'set subject of theMessage to "Say \\"hi\\""' in scripts[0]

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_a_named_sender_is_set_last(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@example.com>",
        ) as resolve:
            scripts = self._send(connector, seed, from_account="Work")
        resolve.assert_called_once_with("Work")
        open_s = scripts[0]
        sender_at = open_s.index(
            'set sender of theMessage to "Alice Smith <me@example.com>"'
        )
        assert sender_at > open_s.index("opening window true")
        assert sender_at > open_s.rindex("make new to recipient")

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_rfc_message_id_is_resolved_to_mails_id_first(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Read tools on the IMAP path hand out RFC 5322 Message-IDs as
        ids (#148); the seed lookup matches Mail's own id (#205)."""
        with patch.object(
            connector, "find_message_by_message_id", return_value="4242",
        ) as find:
            scripts = self._send(connector, seed, seed_id="<abc@example.com>")
        find.assert_called_once_with("<abc@example.com>")
        assert 'whose id is "4242"' in scripts[0]

    def test_an_rfc_message_id_that_matches_nothing_never_reaches_mail(
        self, connector: AppleMailConnector
    ) -> None:
        captured = _scripted(connector, ["unused"])
        with patch.object(
            connector, "find_message_by_message_id", return_value=None,
        ), pytest.raises(MailMessageNotFoundError):
            connector._send_html_email(
                to=["a@example.com"], cc=None, bcc=None, subject="",
                body="<p>x</p>", from_account=None,
                forward_of="abc@example.com",
            )
        assert captured == []

    @pytest.mark.parametrize("seed", ["reply", "forward"])
    def test_an_empty_body_leaves_mails_part_as_mail_made_it(
        self, connector: AppleMailConnector, seed: str
    ) -> None:
        """Nothing is pasted: the window is opened, gated and sent, and
        its Sent copy looked for."""
        scripts = self._send(connector, seed, body="")
        assert len(scripts) == 4
        assert "click sendBtn" in scripts[1]
        assert all('keystroke "v"' not in s for s in scripts)
