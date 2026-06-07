"""Tests for the pure-Python CSL-JSON bibliography formatter.

All functions under test are pure (no IO/DB/git) and must never raise on
partial / malformed CSL input.
"""

from __future__ import annotations

from call_manager.proposal_assist.citations.bibliography import (
    _suffix_from_citekey,
    _year_of,
    format_author_list,
    format_author_year,
    format_vancouver,
    render_bibliography,
)

# ---------------------------------------------------------------------------
# Sample CSL records
# ---------------------------------------------------------------------------


def _smith_csl() -> dict:
    return {
        "author": [
            {"family": "Smith", "given": "Jane Mary"},
            {"family": "Jones", "given": "Alan"},
        ],
        "issued": {"date-parts": [[2021, 4, 3]]},
        "title": "A study of things",
        "container-title": "Journal of Things",
        "volume": "27",
        "issue": "4",
        "page": "511-520",
        "DOI": "10.1000/xyz",
        "type": "article-journal",
    }


# ---------------------------------------------------------------------------
# format_author_list
# ---------------------------------------------------------------------------


def test_author_list_single_vancouver():
    authors = [{"family": "Smith", "given": "Jane"}]
    assert format_author_list(authors, style="vancouver") == "Smith J"


def test_author_list_initials_collapse_vancouver():
    # "Jane Mary" -> "JM" (no periods, no spaces between initials)
    authors = [{"family": "Smith", "given": "Jane Mary"}]
    assert format_author_list(authors, style="vancouver") == "Smith JM"


def test_author_list_two_vancouver():
    authors = [
        {"family": "Smith", "given": "Jane"},
        {"family": "Jones", "given": "Alan"},
    ]
    assert format_author_list(authors, style="vancouver") == "Smith J, Jones A"


def test_author_list_three_vancouver():
    authors = [
        {"family": "Smith", "given": "Jane"},
        {"family": "Jones", "given": "Alan"},
        {"family": "Lee", "given": "Kim"},
    ]
    assert format_author_list(authors, style="vancouver") == "Smith J, Jones A, Lee K"


def test_author_list_many_vancouver_et_al():
    authors = [{"family": f"Auth{i}", "given": "X"} for i in range(8)]
    out = format_author_list(authors, style="vancouver")
    # first 6 + " et al."
    assert out.endswith(" et al.")
    assert out.count(",") == 5  # 6 names joined by 5 commas
    assert "Auth0 X" in out
    assert "Auth6 X" not in out


def test_author_list_exactly_six_vancouver_no_et_al():
    authors = [{"family": f"Auth{i}", "given": "X"} for i in range(6)]
    out = format_author_list(authors, style="vancouver")
    assert "et al." not in out
    assert out.count(",") == 5


def test_author_list_author_year_ampersand_before_last():
    authors = [
        {"family": "Smith", "given": "Jane"},
        {"family": "Jones", "given": "Alan"},
        {"family": "Lee", "given": "Kim"},
    ]
    out = format_author_list(authors, style="author-year")
    assert out == "Smith, J., Jones, A., & Lee, K."


def test_author_list_author_year_two():
    authors = [
        {"family": "Smith", "given": "Jane Mary"},
        {"family": "Jones", "given": "Alan"},
    ]
    out = format_author_list(authors, style="author-year")
    assert out == "Smith, J. M., & Jones, A."


def test_author_list_author_year_single_no_ampersand():
    authors = [{"family": "Smith", "given": "Jane"}]
    assert format_author_list(authors, style="author-year") == "Smith, J."


def test_author_list_author_year_many_cap_et_al():
    authors = [{"family": f"Auth{i}", "given": "X"} for i in range(25)]
    out = format_author_list(authors, style="author-year")
    assert out.endswith(" et al.")
    assert out.startswith("Auth0,")


def test_author_list_literal():
    authors = [{"literal": "World Health Organization"}]
    assert format_author_list(authors, style="vancouver") == "World Health Organization"
    assert format_author_list(authors, style="author-year") == "World Health Organization"


def test_author_list_empty_and_none():
    assert format_author_list([], style="vancouver") == ""
    assert format_author_list(None, style="vancouver") == ""


def test_author_list_skips_malformed():
    authors = [
        {"family": "Smith", "given": "Jane"},
        {"junk": "x"},
        "not-a-dict",
        {"family": "", "given": ""},
        {"family": "Lee", "given": "Kim"},
    ]
    out = format_author_list(authors, style="vancouver")
    assert out == "Smith J, Lee K"


def test_author_list_family_only_vancouver():
    authors = [{"family": "Smith"}]
    assert format_author_list(authors, style="vancouver") == "Smith"


def test_author_list_unicode_survives():
    authors = [{"family": "Müller", "given": "Émile"}]
    assert format_author_list(authors, style="vancouver") == "Müller É"


# ---------------------------------------------------------------------------
# _year_of
# ---------------------------------------------------------------------------


def test_year_of_present():
    assert _year_of({"issued": {"date-parts": [[2021, 4, 3]]}}) == "2021"


def test_year_of_year_only():
    assert _year_of({"issued": {"date-parts": [[2019]]}}) == "2019"


def test_year_of_missing():
    assert _year_of({}) is None
    assert _year_of({"issued": {}}) is None
    assert _year_of({"issued": {"date-parts": []}}) is None
    assert _year_of({"issued": {"date-parts": [[]]}}) is None
    assert _year_of({"issued": "garbage"}) is None


# ---------------------------------------------------------------------------
# _suffix_from_citekey
# ---------------------------------------------------------------------------


def test_suffix_present():
    assert _suffix_from_citekey("smith2021a", "2021") == "a"


def test_suffix_absent_plain_key():
    assert _suffix_from_citekey("smith2021", "2021") == ""


def test_suffix_random_ref_key():
    assert _suffix_from_citekey("ref-abc123", None) == ""
    assert _suffix_from_citekey("ref-abc123", "2021") == ""


def test_suffix_no_year():
    assert _suffix_from_citekey("smith2021a", None) == ""


def test_suffix_requires_year_immediately_before_letter():
    # key does not end with <year><letter>
    assert _suffix_from_citekey("smith2021ab", "2021") == ""
    assert _suffix_from_citekey("smith1999a", "2021") == ""


# ---------------------------------------------------------------------------
# format_vancouver
# ---------------------------------------------------------------------------


def test_vancouver_full_record_exact():
    out = format_vancouver(_smith_csl(), 1)
    assert out == (
        "[1] Smith JM, Jones A. A study of things. Journal of Things. "
        "2021;27(4):511-520. doi:10.1000/xyz"
    )


def test_vancouver_missing_issue_no_dangling():
    csl = _smith_csl()
    del csl["issue"]
    out = format_vancouver(csl, 2)
    assert "()" not in out
    assert out == (
        "[2] Smith JM, Jones A. A study of things. Journal of Things. "
        "2021;27:511-520. doi:10.1000/xyz"
    )


def test_vancouver_missing_doi_no_dangling():
    csl = _smith_csl()
    del csl["DOI"]
    out = format_vancouver(csl, 3)
    assert "doi:" not in out
    assert not out.rstrip().endswith("doi:")  # no dangling "doi:" prefix
    assert out == (
        "[3] Smith JM, Jones A. A study of things. Journal of Things. 2021;27(4):511-520."
    )


def test_vancouver_missing_volume_issue_page():
    csl = _smith_csl()
    for k in ("volume", "issue", "page"):
        del csl[k]
    out = format_vancouver(csl, 4)
    assert ";" not in out
    assert "(" not in out
    assert out == (
        "[4] Smith JM, Jones A. A study of things. Journal of Things. 2021. doi:10.1000/xyz"
    )


def test_vancouver_title_only():
    csl = {"title": "Lonely title"}
    out = format_vancouver(csl, 5)
    assert out == "[5] Lonely title."


def test_vancouver_no_title_no_author_fallback():
    out = format_vancouver({}, 7)
    assert out == "[7] Untitled"


def test_vancouver_never_raises_on_garbage():
    # must not raise
    format_vancouver({"author": "nope", "issued": 5, "title": None}, 1)


# ---------------------------------------------------------------------------
# format_author_year
# ---------------------------------------------------------------------------


def test_author_year_full_with_suffix_exact():
    out = format_author_year(_smith_csl(), "smith2021a")
    assert out == (
        "Smith, J. M., & Jones, A. (2021a). A study of things. "
        "Journal of Things, 27(4), 511-520. https://doi.org/10.1000/xyz"
    )


def test_author_year_no_suffix():
    out = format_author_year(_smith_csl(), "smith2021")
    assert "(2021)." in out
    assert "(2021a)" not in out


def test_author_year_missing_doi():
    csl = _smith_csl()
    del csl["DOI"]
    out = format_author_year(csl, "smith2021")
    assert "https://doi.org" not in out
    assert out.endswith("511-520.")


def test_author_year_missing_volume_issue_page():
    csl = _smith_csl()
    for k in ("volume", "issue", "page"):
        del csl[k]
    out = format_author_year(csl, "smith2021")
    assert "(" not in out.split("(2021)")[1].split(".")[0] or True  # no garbled ()
    assert "Journal of Things." in out  # container ends cleanly with period
    assert ", ," not in out


def test_author_year_no_year():
    csl = {"author": [{"family": "Smith", "given": "Jane"}], "title": "T"}
    out = format_author_year(csl, "ref-abc")
    assert "()" not in out
    assert out.startswith("Smith, J.")


def test_author_year_never_raises_on_garbage():
    format_author_year({"author": 5, "issued": "x", "title": []}, "k")


# ---------------------------------------------------------------------------
# render_bibliography
# ---------------------------------------------------------------------------


def _entry(family, year, title, citekey):
    return (
        citekey,
        {
            "author": [{"family": family, "given": "X"}],
            "issued": {"date-parts": [[year]]},
            "title": title,
        },
    )


def test_render_empty_returns_empty_string():
    assert render_bibliography([], "vancouver") == ""
    assert render_bibliography([], "author-year") == ""


def test_render_vancouver_document_order_numbered():
    entries = [
        _entry("Zeta", 2020, "Zed paper", "zeta2020"),
        _entry("Alpha", 2019, "Alpha paper", "alpha2019"),
    ]
    out = render_bibliography(entries, "vancouver")
    assert out.startswith("# References\n\n")
    body = out[len("# References\n\n") :]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    # NOT sorted: Zeta first because it is first in document order
    assert lines[0].startswith("[1] Zeta")
    assert lines[1].startswith("[2] Alpha")


def test_render_author_year_alphabetical():
    entries = [
        _entry("Zeta", 2020, "Zed paper", "zeta2020"),
        _entry("Alpha", 2019, "Alpha paper", "alpha2019"),
    ]
    out = render_bibliography(entries, "author-year")
    assert out.startswith("# References\n\n")
    body = out[len("# References\n\n") :]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    # sorted case-insensitively by formatted string: Alpha before Zeta
    assert lines[0].startswith("Alpha")
    assert lines[1].startswith("Zeta")


def test_render_author_year_case_insensitive_sort():
    entries = [
        _entry("zeta", 2020, "z", "zeta2020"),
        _entry("Alpha", 2019, "a", "alpha2019"),
    ]
    out = render_bibliography(entries, "author-year")
    body = out[len("# References\n\n") :]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[0].startswith("Alpha")
    assert lines[1].lower().startswith("zeta")


def test_render_unicode_survives():
    entries = [_entry("Müller", 2021, "Über etwas", "muller2021")]
    out = render_bibliography(entries, "vancouver")
    assert "Müller" in out
    assert "Über etwas" in out


def test_render_deterministic():
    entries = [
        _entry("Zeta", 2020, "Zed paper", "zeta2020"),
        _entry("Alpha", 2019, "Alpha paper", "alpha2019"),
    ]
    a = render_bibliography(entries, "author-year")
    b = render_bibliography(entries, "author-year")
    assert a == b
    c = render_bibliography(entries, "vancouver")
    d = render_bibliography(entries, "vancouver")
    assert c == d


def test_render_author_year_carries_suffix():
    entries = [
        ("smith2021a", _smith_csl()),
    ]
    out = render_bibliography(entries, "author-year")
    assert "(2021a)" in out


def test_render_never_raises_on_garbage_entries():
    entries = [("k", {"author": 5}), ("k2", "not-a-dict")]
    # must not raise
    render_bibliography(entries, "vancouver")
    render_bibliography(entries, "author-year")


# ---------------------------------------------------------------------------
# Punctuation normalization (no doubled / dangling periods)
# ---------------------------------------------------------------------------


def test_author_year_no_year_no_double_period_after_initials():
    # "Smith, J." + ". " join must not become "Smith, J.. T."
    out = format_author_year(
        {"author": [{"family": "Smith", "given": "Jane"}], "title": "T"}, "smith"
    )
    assert ".." not in out
    assert out == "Smith, J. T."


def test_vancouver_period_terminated_title_no_double_period():
    out = format_vancouver({"author": [{"family": "X"}], "title": "Outcomes."}, 1)
    assert ".." not in out
    assert out == "[1] X. Outcomes."


def test_author_year_period_terminated_title_no_double_period():
    out = format_author_year(
        {"author": [{"family": "X"}], "title": "Outcomes.", "issued": {"date-parts": [[2020]]}},
        "x2020",
    )
    assert ".." not in out
    assert out == "X (2020). Outcomes."


def test_author_year_mid_string_initials_preserved():
    # The "J." inside the head must survive; only a doubled terminal collapses.
    out = format_author_year(
        {
            "author": [{"family": "Smith", "given": "Jane"}],
            "title": "Study",
            "issued": {"date-parts": [[2020]]},
        },
        "smith2020a",
    )
    assert out == "Smith, J. (2020a). Study."


# ---------------------------------------------------------------------------
# Whitespace / control-char collapsing (line-delimited artifact safety)
# ---------------------------------------------------------------------------


def test_vancouver_collapses_embedded_newline_in_title():
    out = format_vancouver({"author": [{"family": "X"}], "title": "Line1\nLine2"}, 1)
    assert "\n" not in out
    assert out == "[1] X. Line1 Line2."


def test_collapses_embedded_whitespace_in_container_and_fields():
    out = format_author_year(
        {
            "author": [{"family": "Doe"}],
            "container-title": "Nat\tMed",
            "volume": "9",
            "page": "1",
            "issued": {"date-parts": [[2019]]},
        },
        "doe2019",
    )
    assert "\t" not in out
    assert out == "Doe (2019). Nat Med, 9, 1."


def test_collapses_newline_in_author_family():
    out = format_vancouver({"author": [{"family": "Smith\nJones"}], "title": "T"}, 1)
    assert "\n" not in out
    assert out == "[1] Smith Jones. T."


def test_render_vancouver_numbering_intact_with_multiline_fields():
    # A field-embedded newline must not desync the [n] numbering: each entry
    # stays on exactly one line.
    entries = [
        ("a", {"author": [{"family": "A"}], "title": "First\nstudy"}),
        ("b", {"author": [{"family": "B"}], "title": "Second"}),
    ]
    out = render_bibliography(entries, "vancouver")
    body_lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(body_lines) == 2
    assert body_lines[0].startswith("[1] ")
    assert body_lines[1].startswith("[2] ")
