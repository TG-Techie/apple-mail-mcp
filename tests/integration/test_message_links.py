"""Live: ``get_message(include_links=True)`` reads the links of a real
HTML message, through ``source of`` on the AppleScript path.

Two messages, both in the test account:

- A draft this test saves the way the HTML compose tests do
  (``_compose`` with ``plain=False``, a compose window closed with
  Save), with one ``<a href>`` whose URL carries an ``&amp;`` entity.
  Nothing is sent, and the draft goes to Trash when the test ends
  (``MailTrash``). Saving names the account as the sender, so the test
  skips, as the suite's other sender tests do, when Mail lists no
  address for the account.
- A message already in the account's INBOX whose HTML has an anchor,
  read only. Its links are held against the anchors a regex finds in
  the same source, decoded by the stdlib's compat32 parser rather than
  the policy the connector uses. Nothing of the message is printed or
  asserted on by value: a mismatch reports counts.

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<account> uv run pytest \\
        tests/integration/test_message_links.py --run-integration -v
"""

from __future__ import annotations

import email
import html
import os
import re
import uuid
from typing import Any, cast

import pytest

from apple_mail_mcp.links import MAX_LINKS
from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script
from apple_mail_mcp.utils import escape_applescript_string, parse_applescript_json

from .conftest import TEST_DRAFT_SUBJECT_PREFIX
from .mail_readback import MailTrash, draft_source

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests disabled by default. Use --run-integration to run.",
    ),
    pytest.mark.skipif(
        os.getenv("MAIL_TEST_MODE") != "true", reason="MAIL_TEST_MODE != 'true'"
    ),
]

# How many of the INBOX's first messages the finder reads the source of.
_CANDIDATES = 20

_HREF = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_KEPT = ("http:", "https:", "mailto:")


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=120)


def test_links_of_a_saved_html_draft(
    connector: AppleMailConnector, test_account: str
) -> None:
    account = next(
        (a for a in connector.list_accounts() if a["name"] == test_account), None
    )
    if account is None or not account.get("email_addresses"):
        pytest.skip(f"test account {test_account!r} has no email addresses")
    hexid = uuid.uuid4().hex[:8]
    subject = f"{TEST_DRAFT_SUBJECT_PREFIX}links-{hexid}"
    url = f"https://example.com/zzz?id={hexid}&x=1"
    with MailTrash(connector, test_account) as trash:
        trash.windows(subject)
        trash.drafts(subject)
        result = connector._compose(
            seed="new",
            seed_id=None,
            reply_all=False,
            to=["test1@example.com"],
            cc=None,
            bcc=None,
            subject=subject,
            body=(
                f'<p>Please <a href="https://example.com/zzz?id={hexid}&amp;x=1">'
                "follow <b>this</b> link</a> to join.</p>"
            ),
            plain=False,
            attachment_paths=None,
            from_account=test_account,
            send_now=False,
        )
        draft_id = result["draft_id"]
        assert draft_id, result
        assert "text/html" in draft_source(connector, subject)

        row = connector.get_message(draft_id, include_links=True)
        print(f"links: {row['links']}")
        print(f"url in content: {url in row['content']}")

        assert row["id"] == draft_id
        assert {"url": url, "text": "follow this link"} in row["links"]
        assert not row.get("warnings")
        assert {"source", "source_size", "source_error"}.isdisjoint(row)
        assert "links" not in connector.get_message(draft_id)


def _anchored_html_message(connector: AppleMailConnector, account: str) -> tuple[str, str]:
    """``(id, source)`` of the first of the INBOX's first ``_CANDIDATES``
    messages whose source has an HTML part with an ``href``, or skip."""
    account_literal = escape_applescript_string(account)
    body = f"""
tell application "Mail"
    set resultData to {{}}
    set box to mailbox "INBOX" of account "{account_literal}"
    set n to count of messages of box
    if n > {_CANDIDATES} then set n to {_CANDIDATES}
    repeat with i from 1 to n
        set m to message i of box
        try
            if (message size of m) < 2000000 then
                set src to source of m
                if src contains "text/html" and src contains "href" then
                    set resultData to {{|id|:(id of m as text), |source|:src}}
                    exit repeat
                end if
            end if
        end try
    end repeat
end tell
"""
    raw = connector._run_applescript(_wrap_as_json_script(body, timeout=120))
    found = cast(Any, parse_applescript_json(raw))
    if not found:
        pytest.skip(
            f"none of the first {_CANDIDATES} INBOX messages of {account!r} "
            "has an HTML part with an href"
        )
    return str(found["id"]), str(found["source"])


def _regex_hrefs(source: str) -> set[str]:
    """The kept-scheme hrefs of every inline text/html part, by regex,
    with comments removed: an oracle independent of the HTML parser."""
    urls: set[str] = set()
    for part in email.message_from_string(source).walk():
        if part.get_content_type() != "text/html" or part.get_filename():
            continue
        payload = cast(bytes, part.get_payload(decode=True) or b"")
        text = _COMMENT.sub("", payload.decode(part.get_content_charset() or "utf-8", "replace"))
        for match in _HREF.finditer(text):
            url = html.unescape(next(g for g in match.groups() if g is not None)).strip()
            if url.lower().startswith(_KEPT):
                urls.add(url)
    return urls


def test_links_of_an_html_message_in_the_inbox(
    connector: AppleMailConnector, test_account: str
) -> None:
    mail_id, source = _anchored_html_message(connector, test_account)
    row = connector.get_message(mail_id, include_content=False, include_links=True)
    got = {link["url"] for link in row["links"]}
    expected = _regex_hrefs(source)
    print(
        f"links: {len(row['links'])}, distinct urls: {len(got)}, "
        f"regex urls: {len(expected)}, with text: "
        f"{sum(1 for link in row['links'] if link['text'])}"
    )
    assert row["id"] == mail_id
    assert {"source", "source_size", "source_error"}.isdisjoint(row)
    assert row["links"], "an anchored HTML message gave no links"
    if len(row["links"]) < MAX_LINKS:
        assert not row.get("warnings")
        assert got == expected, (
            f"{len(got - expected)} url(s) only in links, "
            f"{len(expected - got)} only in the regex's"
        )
    else:
        assert got <= expected
