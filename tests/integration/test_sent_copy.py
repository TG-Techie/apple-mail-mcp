"""A send finds the copy it filed in Sent by identity, against the real Mail.

Run via:
    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> pytest tests/integration/test_sent_copy.py --run-integration -v

By subject alone, the check of a send's files read the oldest message in
Sent with the subject, so a send under a subject used before was checked
against an earlier message, reported its file missing, and was sent
again. The send now takes Sent's ids before its window opens and looks
for the one message with its subject whose id was not among them.

Two tests, in this order. The first sends one message, from the test
account to test@example.com (example.com takes no mail; its "nullMX"
delivery report reaches the INBOX later), with one small file, and
checks that the result names a Sent copy that carries the file. The
second sends nothing: it takes a message in Sent whose subject carries
the suite's prefix, leaves that message's id out of a snapshot of Sent,
and checks that the look by its subject finds exactly that id, and that
with nothing left out it finds nothing, older copies of the subject
included. The first test's copy is such a message and stays in Sent
until this module ends (``module_trash`` moves it to Trash then), so
run together the second always has one; run alone, it skips when Sent
holds none. Each connector keeps its compose ledger in a temporary
directory, never the data home the daemon's ledger lives in.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.compose_ledger import ComposeLedger
from apple_mail_mcp.mail_connector import (
    AppleMailConnector,
    _SentCopy,
    _SentCopyUnidentified,
    _wrap_as_json_script,
)
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import MailTrash, assert_sent

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run.",
)


@pytest.fixture(scope="module")
def connector(tmp_path_factory: pytest.TempPathFactory) -> AppleMailConnector:
    return AppleMailConnector(
        timeout=90,
        compose_ledger=ComposeLedger(tmp_path_factory.mktemp("compose_windows")),
    )


@pytest.fixture(scope="module")
def module_trash(connector: AppleMailConnector) -> Iterator[MailTrash]:
    """What this module sent, moved to Trash when the module ends rather
    than when the test that sent it does, so the lookup test after it
    has a message with the suite's prefix in Sent to find."""
    account = os.getenv("MAIL_TEST_ACCOUNT")
    if not account:
        pytest.fail(
            "MAIL_TEST_ACCOUNT is not set. Integration tests run against "
            "exactly the account it names; there is no default."
        )
    with MailTrash(connector, account) as trash:
        yield trash


def _sent_record(connector: AppleMailConnector, mail_id: str) -> dict[str, Any]:
    """The message in Sent with Mail's id ``mail_id``, read on its own:
    its subject, Message-ID and file names."""
    raw = connector._run_applescript(_wrap_as_json_script(f"""
tell application "Mail"
    set m to first message of sent mailbox whose id is "{escape_applescript_string(mail_id)}"
    set attNames to name of every mail attachment of m
    if attNames is missing value then set attNames to {{}}
    set resultData to {{|subject|:(subject of m), |message_id|:(message id of m), |attachment_names|:attNames}}
end tell
""", timeout=connector.timeout))
    return dict(parse_applescript_json(raw))


def _sent_ids(connector: AppleMailConnector) -> list[int]:
    """Mail's ids for every message in Sent, every account's."""
    raw = connector._run_applescript(_wrap_as_json_script(
        'tell application "Mail"\n'
        "    set resultData to (id of every message of sent mailbox)\n"
        "end tell",
        timeout=connector.timeout,
    ))
    return [int(i) for i in parse_applescript_json(raw)]


def test_a_send_returns_the_copy_it_filed(
    connector: AppleMailConnector,
    module_trash: MailTrash,
    test_account: str,
    tmp_path: Path,
) -> None:
    """The one send in this module: the result names a Sent copy, read
    back by that id, with the send's subject, Message-ID and file."""
    hexid = uuid.uuid4().hex[:8]
    attached = tmp_path / f"sent-copy-{hexid}.txt"
    attached.write_text(f"sent copy {hexid}\n")
    subject = module_trash.sent(f"{TEST_DRAFT_SUBJECT_PREFIX}sent-copy-{hexid}")
    module_trash.windows(subject)

    looks: list[float] = []
    look = connector._sent_ids_with_subject

    def counted(subject: str) -> list[int]:
        looks.append(time.monotonic())
        return look(subject)

    connector._sent_ids_with_subject = counted  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        result = connector._send_html_email(
            to=["test@example.com"], cc=None, bcc=None, subject=subject,
            body=f"<p>sent copy <b>marker-{hexid}</b></p>",
            from_account=test_account, attachment_paths=[attached],
        )
    finally:
        del connector._sent_ids_with_subject
    took = time.monotonic() - started
    waited = looks[-1] - looks[0] if looks else 0.0
    print(
        f"result keys {sorted(result)}; the send and its look took {took:.1f}s; "
        f"{len(looks)} look(s), the last {waited:.1f}s after the first"
    )
    assert_sent(result)
    copy = _sent_record(connector, result["sent_message_id"])
    print(f"the copy by that id: files {copy['attachment_names']}")
    assert copy["subject"] == subject
    assert str(copy["message_id"]).strip("<>") == result["sent_rfc_message_id"]
    assert attached.name in copy["attachment_names"]


def test_the_look_finds_the_one_id_its_snapshot_lacks(
    connector: AppleMailConnector,
) -> None:
    """Sends nothing. A message in Sent with the suite's prefix, its id
    left out of a snapshot of Sent: the look by its subject finds that
    id. With nothing left out it finds nothing, though the subject is in
    Sent: an older copy of a subject is never taken for a new one."""
    prefix = escape_applescript_string(TEST_DRAFT_SUBJECT_PREFIX)
    raw = connector._run_applescript(_wrap_as_json_script(f"""
tell application "Mail"
    set resultData to {{}}
    set marked to (messages of sent mailbox whose subject begins with "{prefix}")
    if (count of marked) > 0 then
        set m to item 1 of marked
        set resultData to {{|id|:(id of m), |subject|:(subject of m)}}
    end if
end tell
""", timeout=connector.timeout))
    marked = parse_applescript_json(raw)
    if not marked:
        pytest.skip(
            "no message in Sent carries the suite's prefix; this module's "
            "send test leaves one there when it runs first"
        )
    target, subject = int(marked["id"]), str(marked["subject"])
    everything = _sent_ids(connector)
    assert target in everything

    found = connector._find_sent_copy(subject, [i for i in everything if i != target])
    print(f"left out {target}: found {found}")
    assert isinstance(found, _SentCopy)
    assert found.mail_id == str(target)

    connector._SENT_APPEAR_POLLS = 0  # one look: nothing new will come
    try:
        nothing = connector._find_sent_copy(subject, everything)
    finally:
        del connector._SENT_APPEAR_POLLS
    print(f"left out nothing: {nothing}")
    assert isinstance(nothing, _SentCopyUnidentified)
