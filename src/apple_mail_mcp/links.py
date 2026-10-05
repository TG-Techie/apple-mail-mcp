"""A message's links, read from its raw RFC 822 source.

Mail's ``content`` is a plain-text rendering, and an HTML mail often
carries a URL only in an ``<a href>``, so the URL never reaches it. The
source does carry it. This module reads the anchors of every text/html
part (decoded by the stdlib ``email`` package, quoted-printable and
base64 included) and, for a message with no HTML part, the bare
``http(s)://`` URLs of its text/plain parts.

What it returns is the sender's markup, unverified: the shown text can
name a different place than the URL goes to. Both read paths (AppleScript
``source of`` and IMAP ``BODY[]``) hand their source here, so the same
message gives the same links whichever path read it.
"""

from __future__ import annotations

import re
from email import message_from_bytes, message_from_string, policy
from email.message import EmailMessage, Message
from html.parser import HTMLParser

# Links returned per message; more are dropped, with a warning.
MAX_LINKS = 200
# Largest source read for links, in bytes. Above it a message gets no
# links and a warning: a source this size is attachments, not markup.
MAX_SOURCE_BYTES = 10 * 1024 * 1024

_KEPT_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_PLAIN_URL = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?'\""


class _AnchorParser(HTMLParser):
    """Collects ``(href, text)`` for each ``<a href>``, in document order.

    The text is what the anchor shows: its character data, nested tags
    included, entities decoded, whitespace collapsed. An anchor left
    open ends at the next ``<a>`` or at the end of the part, since HTML
    does not nest anchors.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._end_anchor()
            self._href = next((v for k, v in attrs if k == "href" and v is not None), None)
        elif tag == "br" and self._href is not None:
            self._text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._end_anchor()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._end_anchor()

    def _end_anchor(self) -> None:
        if self._href is not None:
            self.anchors.append((self._href.strip(), " ".join("".join(self._text).split())))
        self._href = None
        self._text = []


def _kept(url: str) -> bool:
    match = _SCHEME.match(url)
    return match is not None and match.group(1).lower() in _KEPT_SCHEMES


def _part_text(part: Message) -> str:
    """A text part's content, transfer encoding and charset undone. An
    unknown or wrong charset falls back to UTF-8 with replacement, so
    the URLs, which are ASCII, still come through."""
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _anchors(html: str) -> list[tuple[str, str]]:
    parser = _AnchorParser()
    parser.feed(html)
    parser.close()
    return parser.anchors


def _trim_plain_url(url: str) -> str:
    """Drop sentence punctuation after a URL in prose, and a closing
    parenthesis the URL did not open."""
    while url:
        if url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


def _plain_urls(text: str) -> list[tuple[str, str]]:
    return [(_trim_plain_url(m.group(0)), "") for m in _PLAIN_URL.finditer(text)]


def _body_parts(message: EmailMessage) -> tuple[list[str], list[str]]:
    """The decoded text of the message's text/html and text/plain parts,
    in order, leaving out parts sent as attachments."""
    html: list[str] = []
    plain: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.is_attachment():
            continue
        content_type = part.get_content_type()
        if content_type == "text/html":
            html.append(_part_text(part))
        elif content_type == "text/plain":
            plain.append(_part_text(part))
    return html, plain


def _parse(source: str | bytes) -> EmailMessage:
    if isinstance(source, bytes):
        return message_from_bytes(source, _class=EmailMessage, policy=policy.default)
    return message_from_string(source, _class=EmailMessage, policy=policy.default)


def source_unreadable_warning(message_id: str, error: str) -> str:
    return f"links not read for message {message_id}: its source could not be read: {error}"


def source_too_large_warning(message_id: str, size: int) -> str:
    return (
        f"links not read for message {message_id}: its source is too large "
        f"({size} bytes; the limit is {MAX_SOURCE_BYTES})"
    )


def links_from_source(
    source: str | bytes, *, message_id: str
) -> tuple[list[dict[str, str]], list[str]]:
    """The links of the message whose raw source is ``source``.

    Anchors of every text/html part, in document order; with no HTML
    part, the ``http(s)://`` URLs of the text/plain parts, with text
    ``""``. Only http, https and mailto URLs are kept. Identical
    ``(url, text)`` pairs are kept once, and at most ``MAX_LINKS``.

    Returns:
        ``(links, warnings)``: links as ``{"url", "text"}`` dicts, and a
        warning naming ``message_id`` when the source was over
        ``MAX_SOURCE_BYTES`` (no links then) or the cap dropped links.
    """
    size = len(source) if isinstance(source, bytes) else len(source.encode("utf-8", "replace"))
    if size > MAX_SOURCE_BYTES:
        return [], [source_too_large_warning(message_id, size)]

    html, plain = _body_parts(_parse(source))
    found: list[tuple[str, str]] = []
    if html:
        for part in html:
            found += _anchors(part)
    else:
        for part in plain:
            found += _plain_urls(part)

    unique = list(dict.fromkeys(pair for pair in found if _kept(pair[0])))
    warnings: list[str] = []
    if len(unique) > MAX_LINKS:
        warnings.append(
            f"message {message_id} has {len(unique)} links; only the first "
            f"{MAX_LINKS} are returned"
        )
    links = [{"url": url, "text": text} for url, text in unique[:MAX_LINKS]]
    return links, warnings
