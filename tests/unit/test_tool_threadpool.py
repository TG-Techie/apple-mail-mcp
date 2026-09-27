"""A tool waiting on Mail does not hold up anyone else's request.

The resident daemon serves every session from one event loop. A tool
whose connector call runs on that loop stops the loop for as long as
osascript takes, up to the connector's 60 s timeout, and every session
waits. These drive each async tool through the in-process server with a
connector call that blocks until a concurrent ``list_tools`` has been
answered, so the call is released only if the other request could be
served while it waited. A tool that blocks the loop instead waits out
``STALL_SECONDS`` and fails.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import anyio
import anyio.to_thread
import pytest
from fastmcp import Client

from apple_mail_mcp import server

STALL_SECONDS = 2.0

_RULE_ROW = {"index": 1, "name": "Junk filter", "enabled": True}
_DRAFT_STATE = {
    "to": ["a@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "old",
    "body": "old",
    "account": None,
}


@dataclass(frozen=True)
class Case:
    tool: str
    arguments: dict[str, Any]
    blocking_method: str
    blocking_returns: Any
    other_returns: dict[str, Any] = field(default_factory=dict)


CASES = [
    Case(
        "delete_rule", {"rule_index": 1}, "delete_rule", "Junk filter", {"list_rules": [_RULE_ROW]}
    ),
    Case(
        "update_rule",
        {"rule_index": 1, "enabled": False},
        "update_rule",
        None,
        {"list_rules": [_RULE_ROW]},
    ),
    Case("delete_mailbox", {"account": "TestAccount", "name": "Empty"}, "delete_mailbox", 0),
    Case(
        "draft_create",
        {"to": ["a@example.com"], "subject": "s", "body": "b"},
        "create_draft",
        {"draft_id": "draft-2", "sent_message_id": ""},
    ),
    Case(
        "draft_update",
        {"draft_id": "draft-1", "body": "revised"},
        "create_draft",
        {"draft_id": "draft-2", "sent_message_id": ""},
        {"get_draft_state": _DRAFT_STATE, "delete_draft": True},
    ),
    Case(
        "draft_send",
        {"draft_id": "draft-1"},
        "create_draft",
        {"draft_id": "", "sent_message_id": ""},
        {"get_draft_state": _DRAFT_STATE, "delete_draft": True},
    ),
    Case(
        "email_send_html",
        {"to": ["a@example.com"], "subject": "s", "body": "<p>b</p>"},
        "_send_html_email",
        {"draft_id": "", "sent_message_id": ""},
    ),
    # Control: a sync tool, which fastmcp already runs in a worker
    # thread. It passes whatever the async tools do, so a failure above
    # is about those tools and not about this harness.
    Case("delete_messages", {"message_ids": ["msg-1"]}, "delete_messages", 1),
]


@pytest.fixture
def mock_mail(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    mock = MagicMock()
    monkeypatch.setattr(server, "mail", mock)
    return mock


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.tool)
async def test_a_blocking_mail_call_leaves_the_server_answering(
    case: Case, mock_mail: MagicMock
) -> None:
    for method, value in case.other_returns.items():
        getattr(mock_mail, method).return_value = value

    entered = threading.Event()
    release = threading.Event()
    released_in_time: list[bool] = []

    def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        released_in_time.append(release.wait(STALL_SECONDS))
        return case.blocking_returns

    getattr(mock_mail, case.blocking_method).side_effect = blocking_call

    async def answer_yes(message: str, response_type: Any, params: Any, ctx: Any) -> Any:
        return True

    async def other_session() -> None:
        assert await anyio.to_thread.run_sync(entered.wait, STALL_SECONDS)
        listed = await client.list_tools()
        release.set()
        assert case.tool in {t.name for t in listed}

    with anyio.fail_after(STALL_SECONDS * 3):
        async with Client(server.mcp, elicitation_handler=answer_yes) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(other_session)
                result = await client.call_tool(case.tool, case.arguments)

    assert released_in_time == [True], (
        f"{case.tool}: list_tools was not answered while "
        f"{case.blocking_method} was running; the event loop was blocked"
    )
    body = result.structured_content
    assert body is not None
    assert body["success"] is True, body
