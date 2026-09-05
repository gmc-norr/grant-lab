"""Tests for the whitespace-tolerant quote matching helper.

Written first (TDD/RED) before quotes.py exists.
"""

import pytest

from grant_lab.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)

# ---------------------------------------------------------------------------
# normalize_for_quote_match
# ---------------------------------------------------------------------------


class TestNormalizeForQuoteMatch:
    def test_collapses_multiple_spaces(self):
        assert normalize_for_quote_match("hello   world") == "hello world"

    def test_collapses_tabs(self):
        assert normalize_for_quote_match("hello\t\tworld") == "hello world"

    def test_collapses_newlines(self):
        assert normalize_for_quote_match("hello\nworld") == "hello world"

    def test_collapses_mixed_whitespace(self):
        assert normalize_for_quote_match("a\n  \t b") == "a b"

    def test_strips_leading_whitespace(self):
        assert normalize_for_quote_match("  hello") == "hello"

    def test_strips_trailing_whitespace(self):
        assert normalize_for_quote_match("hello  ") == "hello"

    def test_strips_both_ends(self):
        assert normalize_for_quote_match("  hello world  ") == "hello world"

    def test_empty_string(self):
        assert normalize_for_quote_match("") == ""

    def test_whitespace_only(self):
        assert normalize_for_quote_match("   \n\t  ") == ""

    def test_no_change_needed(self):
        assert normalize_for_quote_match("already normal") == "already normal"


# ---------------------------------------------------------------------------
# find_quote_offsets — exact match
# ---------------------------------------------------------------------------


class TestFindQuoteOffsetsExact:
    def test_exact_match_at_start(self):
        chunk = "hello world"
        quote = "hello"
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote

    def test_exact_match_at_end(self):
        chunk = "hello world"
        quote = "world"
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote

    def test_exact_match_in_middle(self):
        chunk = "the quick brown fox jumps"
        quote = "quick brown fox"
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote

    def test_exact_match_full_chunk(self):
        chunk = "exact"
        start, end = find_quote_offsets(chunk, chunk)
        assert chunk[start:end] == chunk

    def test_offsets_are_correct_positions(self):
        chunk = "AABBBCC"
        quote = "BBB"
        start, end = find_quote_offsets(chunk, quote)
        assert start == 2
        assert end == 5
        assert chunk[start:end] == "BBB"


# ---------------------------------------------------------------------------
# find_quote_offsets — whitespace-tolerant match
# ---------------------------------------------------------------------------


class TestFindQuoteOffsetsWhitespaceTolerant:
    def test_chunk_has_newline_quote_has_space(self):
        chunk = "reduces\nrecurrence  by   30%"
        quote = "reduces recurrence by 30%"
        start, end = find_quote_offsets(chunk, quote)
        raw_span = chunk[start:end]
        # Raw span is the original text including line breaks
        assert raw_span == "reduces\nrecurrence  by   30%"
        # And normalising both sides gives the same string
        assert normalize_for_quote_match(raw_span) == normalize_for_quote_match(quote)

    def test_chunk_has_internal_whitespace_various(self):
        chunk = "start\t\tmiddle   end"
        quote = "start middle end"
        start, end = find_quote_offsets(chunk, quote)
        assert normalize_for_quote_match(chunk[start:end]) == normalize_for_quote_match(quote)

    def test_leading_trailing_whitespace_in_quote_tolerated(self):
        chunk = "reduces\nrecurrence  by   30%"
        quote = "  reduces recurrence by 30%  "
        start, end = find_quote_offsets(chunk, quote)
        raw_span = chunk[start:end]
        assert normalize_for_quote_match(raw_span) == normalize_for_quote_match(quote)

    def test_quote_in_middle_of_larger_chunk(self):
        prefix = "Introduction: "
        body = "reduces\nrecurrence  by   30%"
        suffix = " (see table 1)."
        chunk = prefix + body + suffix
        quote = "reduces recurrence by 30%"
        start, end = find_quote_offsets(chunk, quote)
        assert start == len(prefix)
        assert end == len(prefix) + len(body)
        assert chunk[start:end] == body

    def test_offsets_non_zero_position(self):
        chunk = "First sentence. Second sentence. Third sentence."
        quote = "Second sentence."
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote
        assert start > 0


# ---------------------------------------------------------------------------
# find_quote_offsets — error cases
# ---------------------------------------------------------------------------


class TestFindQuoteOffsetsErrors:
    def test_not_found_raises_quote_not_found(self):
        with pytest.raises(QuoteNotFound):
            find_quote_offsets("hello world", "foo bar")

    def test_empty_quote_raises_quote_not_found(self):
        with pytest.raises(QuoteNotFound):
            find_quote_offsets("hello world", "")

    def test_whitespace_only_quote_raises_quote_not_found(self):
        with pytest.raises(QuoteNotFound):
            find_quote_offsets("hello world", "   \n\t  ")

    def test_ambiguous_raises_quote_ambiguous(self):
        # Quote appears twice
        chunk = "the cat sat. the cat sat."
        quote = "the cat sat"
        with pytest.raises(QuoteAmbiguous):
            find_quote_offsets(chunk, quote)

    def test_ambiguous_with_whitespace_variants(self):
        # Both occurrences match whitespace-tolerantly
        chunk = "foo  bar and foo   bar"
        quote = "foo bar"
        with pytest.raises(QuoteAmbiguous):
            find_quote_offsets(chunk, quote)

    def test_ambiguous_counts_all_overlapping(self):
        # "aa" appears twice in "aaa" via overlapping detection
        chunk = "aaa"
        quote = "aa"
        with pytest.raises(QuoteAmbiguous):
            find_quote_offsets(chunk, quote)


# ---------------------------------------------------------------------------
# find_quote_offsets — Unicode
# ---------------------------------------------------------------------------


class TestFindQuoteOffsetsUnicode:
    def test_unicode_accents_exact_match(self):
        chunk = "Le café est naïf"
        quote = "café est naïf"
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote

    def test_unicode_em_dash(self):
        chunk = "before — after"
        quote = "before — after"
        start, end = find_quote_offsets(chunk, quote)
        assert chunk[start:end] == quote

    def test_unicode_quote_with_em_dash_and_whitespace(self):
        chunk = "surrounding text café\n—\nnaïve ending text"
        quote = "café — naïve"
        start, end = find_quote_offsets(chunk, quote)
        raw_span = chunk[start:end]
        assert normalize_for_quote_match(raw_span) == normalize_for_quote_match(quote)

    def test_unicode_offsets_map_correctly_in_context(self):
        prefix = "Ignore this. "
        target = "café — naïve"
        suffix = " ignore that."
        chunk = prefix + target + suffix
        start, end = find_quote_offsets(chunk, target)
        assert chunk[start:end] == target
        # Verify byte-level: start/end are Python str (code-point) offsets
        assert start == len(prefix)
        assert end == len(prefix) + len(target)

    def test_unicode_not_found(self):
        with pytest.raises(QuoteNotFound):
            find_quote_offsets("café est naïf", "résumé")
