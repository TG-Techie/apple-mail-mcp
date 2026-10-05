"""Unit tests for links.py: a message's links, read from its raw source."""

from __future__ import annotations

import base64

from apple_mail_mcp.links import MAX_LINKS, MAX_SOURCE_BYTES, links_from_source


def _single(content_type: str, body: str, *, cte: str = "7bit") -> str:
    return (
        "From: a@example.com\r\n"
        "To: b@example.com\r\n"
        "Subject: links\r\n"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: {cte}\r\n"
        "\r\n"
        f"{body}\r\n"
    )


def _html(body: str) -> str:
    return _single("text/html", body)


def _links(source: str | bytes) -> list[dict[str, str]]:
    links, warnings = links_from_source(source, message_id="1")
    assert warnings == []
    return links


class TestAnchors:
    def test_href_and_visible_text(self) -> None:
        source = _html('<p>Please <a href="https://example.com/a">follow this link</a>.</p>')
        assert _links(source) == [
            {"url": "https://example.com/a", "text": "follow this link"}
        ]

    def test_nested_tags_and_whitespace_collapse(self) -> None:
        source = _html(
            '<a href="https://example.com/n">\n  <span>Accept</span>\n'
            "  <b>the\n invite</b> </a>"
        )
        assert _links(source) == [
            {"url": "https://example.com/n", "text": "Accept the invite"}
        ]

    def test_br_inside_an_anchor_separates_words(self) -> None:
        source = _html('<a href="https://example.com/b">Join<br>now</a>')
        assert _links(source)[0]["text"] == "Join now"

    def test_entities_in_href_are_unescaped(self) -> None:
        source = _html('<a href="https://example.com/x?a=1&amp;b=2">x</a>')
        assert _links(source)[0]["url"] == "https://example.com/x?a=1&b=2"

    def test_entities_in_text_are_unescaped(self) -> None:
        source = _html('<a href="https://example.com/t">Tom &amp; Jerry</a>')
        assert _links(source)[0]["text"] == "Tom & Jerry"

    def test_uppercase_tags_and_attributes(self) -> None:
        source = _html('<A HREF="https://example.com/U">Upper</A>')
        assert _links(source) == [{"url": "https://example.com/U", "text": "Upper"}]

    def test_href_surrounding_whitespace_is_stripped(self) -> None:
        source = _html('<a href="  https://example.com/s \n">s</a>')
        assert _links(source)[0]["url"] == "https://example.com/s"

    def test_relative_and_javascript_hrefs_are_dropped(self) -> None:
        source = _html(
            '<a href="/relative">r</a><a href="javascript:alert(1)">j</a>'
            '<a href="#top">t</a><a name="anchor">n</a>'
            '<a href="https://example.com/kept">k</a>'
        )
        assert _links(source) == [{"url": "https://example.com/kept", "text": "k"}]

    def test_unclosed_anchor_ends_at_the_next_and_at_the_end(self) -> None:
        source = _html(
            '<a href="https://example.com/1">one<a href="https://example.com/2">two'
        )
        assert _links(source) == [
            {"url": "https://example.com/1", "text": "one"},
            {"url": "https://example.com/2", "text": "two"},
        ]

    def test_document_order(self) -> None:
        source = _html(
            '<a href="https://example.com/z">z</a><a href="https://example.com/a">a</a>'
        )
        assert [link["url"] for link in _links(source)] == [
            "https://example.com/z",
            "https://example.com/a",
        ]


class TestSchemes:
    def test_keeps_http_https_and_mailto_case_insensitively(self) -> None:
        source = _html(
            '<a href="http://example.com/h">h</a>'
            '<a href="HTTPS://example.com/s">s</a>'
            '<a href="mailto:someone@example.com">m</a>'
            '<a href="ftp://example.com/f">f</a>'
            '<a href="data:text/html,hi">d</a>'
            '<a href="tel:+15550100">t</a>'
            '<a href="vbscript:x">v</a>'
        )
        assert [link["url"] for link in _links(source)] == [
            "http://example.com/h",
            "HTTPS://example.com/s",
            "mailto:someone@example.com",
        ]


class TestMime:
    def test_multipart_alternative_takes_the_html_part_only(self) -> None:
        source = (
            "From: a@example.com\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/alternative; boundary="BOUND"\r\n'
            "\r\n"
            "--BOUND\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Follow this link: https://example.com/plain-only\r\n"
            "--BOUND\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            '<a href="https://example.com/html">Follow this link</a>\r\n'
            "--BOUND--\r\n"
        )
        assert _links(source) == [
            {"url": "https://example.com/html", "text": "Follow this link"}
        ]

    def test_quoted_printable_html(self) -> None:
        html = (
            '<a href=3D"https://example.com/qp?a=3D1&amp;b=3D2">'
            "Caf=C3=A9 link with a long enough line to be soft-=\r\nwrapped</a>"
        )
        source = _single("text/html", html, cte="quoted-printable")
        assert _links(source) == [
            {
                "url": "https://example.com/qp?a=1&b=2",
                "text": "Café link with a long enough line to be soft-wrapped",
            }
        ]

    def test_base64_html(self) -> None:
        html = '<a href="https://example.com/b64">Bäse</a>'
        encoded = base64.encodebytes(html.encode()).decode()
        source = _single("text/html", encoded, cte="base64")
        assert _links(source) == [{"url": "https://example.com/b64", "text": "Bäse"}]

    def test_bytes_source(self) -> None:
        source = _html('<a href="https://example.com/bytes">b</a>').encode()
        assert _links(source) == [{"url": "https://example.com/bytes", "text": "b"}]

    def test_every_html_part_in_order(self) -> None:
        source = (
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="M"\r\n'
            "\r\n"
            "--M\r\n"
            "Content-Type: text/html\r\n"
            "\r\n"
            '<a href="https://example.com/first">1</a>\r\n'
            "--M\r\n"
            "Content-Type: text/html\r\n"
            "\r\n"
            '<a href="https://example.com/second">2</a>\r\n'
            "--M--\r\n"
        )
        assert [link["url"] for link in _links(source)] == [
            "https://example.com/first",
            "https://example.com/second",
        ]

    def test_html_attachment_is_not_the_message(self) -> None:
        source = (
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="M"\r\n'
            "\r\n"
            "--M\r\n"
            "Content-Type: text/html\r\n"
            "\r\n"
            '<a href="https://example.com/body">body</a>\r\n'
            "--M\r\n"
            "Content-Type: text/html\r\n"
            'Content-Disposition: attachment; filename="page.html"\r\n'
            "\r\n"
            '<a href="https://example.com/attached">file</a>\r\n'
            "--M--\r\n"
        )
        assert [link["url"] for link in _links(source)] == ["https://example.com/body"]

    def test_unknown_charset_still_reads(self) -> None:
        source = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=x-no-such-charset\r\n"
            "\r\n"
            '<a href="https://example.com/cs">cs</a>\r\n'
        )
        assert _links(source) == [{"url": "https://example.com/cs", "text": "cs"}]


class TestPlainTextFallback:
    def test_urls_from_text_plain_with_empty_text(self) -> None:
        source = _single(
            "text/plain",
            "Join here: https://example.com/join?x=1. Or (http://example.com/p).\r\n"
            "Mail me: mailto:someone@example.com is not taken from plain text.",
        )
        assert _links(source) == [
            {"url": "https://example.com/join?x=1", "text": ""},
            {"url": "http://example.com/p", "text": ""},
        ]

    def test_balanced_parentheses_are_kept(self) -> None:
        source = _single("text/plain", "See https://example.com/wiki/A_(b) now")
        assert _links(source) == [{"url": "https://example.com/wiki/A_(b)", "text": ""}]

    def test_angle_bracketed_url(self) -> None:
        source = _single("text/plain", "Link: <https://example.com/angle>")
        assert _links(source) == [{"url": "https://example.com/angle", "text": ""}]

    def test_no_parts_with_links(self) -> None:
        assert _links(_single("text/plain", "nothing here")) == []


class TestDedupeAndCap:
    def test_identical_pairs_are_kept_once(self) -> None:
        source = _html(
            '<a href="https://example.com/d">same</a>'
            '<a href="https://example.com/d">same</a>'
            '<a href="https://example.com/d">other text</a>'
        )
        assert _links(source) == [
            {"url": "https://example.com/d", "text": "same"},
            {"url": "https://example.com/d", "text": "other text"},
        ]

    def test_cap_truncates_with_a_warning(self) -> None:
        anchors = "".join(
            f'<a href="https://example.com/{i}">{i}</a>' for i in range(MAX_LINKS + 5)
        )
        links, warnings = links_from_source(_html(anchors), message_id="77")
        assert len(links) == MAX_LINKS
        assert links[-1]["url"] == f"https://example.com/{MAX_LINKS - 1}"
        assert len(warnings) == 1
        assert "77" in warnings[0]
        assert str(MAX_LINKS + 5) in warnings[0]

    def test_cap_counts_after_dedupe(self) -> None:
        anchors = '<a href="https://example.com/same">s</a>' * (MAX_LINKS + 5)
        links, warnings = links_from_source(_html(anchors), message_id="1")
        assert len(links) == 1
        assert warnings == []


class TestSizeBound:
    def test_oversized_source_returns_no_links_and_a_warning(self) -> None:
        body = '<a href="https://example.com/big">big</a>' + "x" * MAX_SOURCE_BYTES
        links, warnings = links_from_source(_html(body), message_id="9")
        assert links == []
        assert len(warnings) == 1
        assert "9" in warnings[0]
        assert "too large" in warnings[0]
