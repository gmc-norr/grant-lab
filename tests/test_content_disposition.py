"""Review 2026-09-02 §2.7 / A1 item 8: `Content-Disposition` 500s on any
non-Latin-1 title or filename — `safe_title("研究")` kept the characters
(`isalnum()` is Unicode-aware) and Starlette encodes headers as latin-1.
Reproduced on the export routes and the attachment serve; Swedish å/ä/ö are
Latin-1 and merely mojibake in some clients.

RFC 6266 / 5987: an ASCII fallback in `filename=` plus the real name in
`filename*=UTF-8''<percent-encoded>`.
"""

from __future__ import annotations

from grant_lab.web.http_headers import content_disposition


def test_ascii_name_is_unchanged_and_carries_no_star_form():
    assert (
        content_disposition("attachment", "My_Proposal.md")
        == 'attachment; filename="My_Proposal.md"'
    )


def test_non_latin1_name_gets_an_ascii_fallback_and_the_utf8_form():
    header = content_disposition("attachment", "研究.md")
    assert header.startswith('attachment; filename="')
    assert "filename*=UTF-8''%E7%A0%94%E7%A9%B6.md" in header
    header.encode("latin-1")  # what Starlette does; must not raise


def test_latin1_but_non_ascii_name_still_gets_the_utf8_form():
    header = content_disposition("inline", "förslag.pdf")
    assert 'filename="f_rslag.pdf"' in header or 'filename="forslag.pdf"' in header
    assert "filename*=UTF-8''f%C3%B6rslag.pdf" in header


def test_quotes_and_control_characters_never_reach_the_header():
    header = content_disposition("inline", 'a"b\r\nc.txt')
    assert "\r" not in header and "\n" not in header
    assert header.count('"') == 2  # the two around the fallback only
