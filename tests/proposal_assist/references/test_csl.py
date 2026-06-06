"""Tests for CSL-JSON helper functions.

Written before the implementation (TDD red-first):
  src/call_manager/proposal_assist/references/csl.py

Covers:
- csl_from_source_meta: crossref / arxiv / pubmed / unknown
- csl_from_pdf_info: with metadata / without / parse error
- merge_csl: first-wins, empties skipped
- generate_citekey: base, collision suffix, missing author/year
"""

import io
import uuid

from reportlab.pdfgen.canvas import Canvas

from call_manager.db.models import Reference
from call_manager.proposal_assist.references.csl import (
    csl_from_pdf_info,
    csl_from_source_meta,
    generate_citekey,
    merge_csl,
)
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# csl_from_source_meta — crossref
# ---------------------------------------------------------------------------


CROSSREF_MESSAGE = {
    "title": ["Effects of climate change on biodiversity"],
    "author": [
        {"family": "Smith", "given": "John A.", "extra_key": "ignored"},
        {"family": "Jones", "given": "Beth"},
    ],
    "issued": {"date-parts": [[2021, 3, 15]]},
    "DOI": "10.1234/example.doi",
    "container-title": ["Journal of Ecology"],
    "volume": "99",
    "issue": "2",
    "page": "100-115",
    "type": "journal-article",
}


def test_csl_from_crossref_title():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    assert csl["title"] == "Effects of climate change on biodiversity"


def test_csl_from_crossref_authors_family_given_only():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    # extra_key must be stripped; only family/given kept
    assert csl["author"] == [
        {"family": "Smith", "given": "John A."},
        {"family": "Jones", "given": "Beth"},
    ]


def test_csl_from_crossref_issued():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    assert csl["issued"] == {"date-parts": [[2021, 3, 15]]}


def test_csl_from_crossref_doi():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    assert csl["DOI"] == "10.1234/example.doi"


def test_csl_from_crossref_container_title():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    assert csl["container-title"] == "Journal of Ecology"


def test_csl_from_crossref_volume_issue_page():
    meta = {"message": CROSSREF_MESSAGE}
    csl = csl_from_source_meta("crossref", meta)
    assert csl["volume"] == "99"
    assert csl["issue"] == "2"
    assert csl["page"] == "100-115"


def test_csl_from_crossref_type_mapping():
    meta = {"message": {**CROSSREF_MESSAGE, "type": "journal-article"}}
    assert csl_from_source_meta("crossref", meta)["type"] == "article-journal"

    meta2 = {"message": {**CROSSREF_MESSAGE, "type": "proceedings-article"}}
    assert csl_from_source_meta("crossref", meta2)["type"] == "paper-conference"

    meta3 = {"message": {**CROSSREF_MESSAGE, "type": "book"}}
    assert csl_from_source_meta("crossref", meta3)["type"] == "book"

    meta4 = {"message": {**CROSSREF_MESSAGE, "type": "posted-content"}}
    assert csl_from_source_meta("crossref", meta4)["type"] == "article"


def test_csl_from_crossref_type_extended_map():
    """Extended type-map entries added in FIX 1."""
    cases = [
        ("journal-article", "article-journal"),
        ("dissertation", "thesis"),
        ("monograph", "book"),
        ("book-chapter", "chapter"),
        ("dataset", "dataset"),
    ]
    for crossref_type, expected_csl_type in cases:
        meta = {"message": {**CROSSREF_MESSAGE, "type": crossref_type}}
        result = csl_from_source_meta("crossref", meta)["type"]
        assert result == expected_csl_type, (
            f"{crossref_type!r} → {result!r}, expected {expected_csl_type!r}"
        )


def test_csl_from_crossref_type_default_for_unknown():
    meta = {"message": {**CROSSREF_MESSAGE, "type": "some-unknown-type"}}
    assert csl_from_source_meta("crossref", meta)["type"] == "article-journal"


def test_csl_from_crossref_type_other_defaults_to_article_journal():
    meta = {"message": {**CROSSREF_MESSAGE, "type": "other"}}
    assert csl_from_source_meta("crossref", meta)["type"] == "article-journal"


def test_csl_from_crossref_missing_container_title_omitted():
    msg = {k: v for k, v in CROSSREF_MESSAGE.items() if k != "container-title"}
    csl = csl_from_source_meta("crossref", {"message": msg})
    assert "container-title" not in csl


def test_csl_from_crossref_malformed_no_raise():
    """A record missing 'title' must not raise — should return {} or partial."""
    # missing title list
    result = csl_from_source_meta("crossref", {"message": {}})
    # No exception raised; may be partial or empty dict
    assert isinstance(result, dict)


def test_csl_from_crossref_empty_message_no_raise():
    result = csl_from_source_meta("crossref", {})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# csl_from_source_meta — arxiv
# ---------------------------------------------------------------------------


ARXIV_META = {
    "title": "Attention Is All You Need",
    "authors": ["Vaswani, Ashish", "Shazeer, Noam"],
    "year": 2017,
    "url": "https://arxiv.org/abs/1706.03762",
}


def test_csl_from_arxiv_title():
    csl = csl_from_source_meta("arxiv", ARXIV_META)
    assert csl["title"] == "Attention Is All You Need"


def test_csl_from_arxiv_literal_authors():
    csl = csl_from_source_meta("arxiv", ARXIV_META)
    assert csl["author"] == [
        {"literal": "Vaswani, Ashish"},
        {"literal": "Shazeer, Noam"},
    ]


def test_csl_from_arxiv_issued():
    csl = csl_from_source_meta("arxiv", ARXIV_META)
    assert csl["issued"] == {"date-parts": [[2017]]}


def test_csl_from_arxiv_url():
    csl = csl_from_source_meta("arxiv", ARXIV_META)
    assert csl["URL"] == "https://arxiv.org/abs/1706.03762"


def test_csl_from_arxiv_type():
    csl = csl_from_source_meta("arxiv", ARXIV_META)
    assert csl["type"] == "article"


def test_csl_from_arxiv_missing_year_no_issued():
    meta = {k: v for k, v in ARXIV_META.items() if k != "year"}
    csl = csl_from_source_meta("arxiv", meta)
    assert "issued" not in csl


def test_csl_from_arxiv_malformed_no_raise():
    result = csl_from_source_meta("arxiv", {})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# csl_from_source_meta — pubmed
# ---------------------------------------------------------------------------


PUBMED_META_STRUCTURED = {
    "title": "CRISPR-Cas9 genome editing",
    "authors": [
        {"family": "Doudna", "given": "Jennifer A."},
        {"family": "Charpentier", "given": "Emmanuelle"},
    ],
    "pmid": "12345678",
}

PUBMED_META_STRING_AUTHORS = {
    "title": "Some paper",
    "authors": ["Alice Smith", "Bob Jones"],
    "pmid": "99999999",
}


def test_csl_from_pubmed_title():
    csl = csl_from_source_meta("pubmed", PUBMED_META_STRUCTURED)
    assert csl["title"] == "CRISPR-Cas9 genome editing"


def test_csl_from_pubmed_structured_authors():
    csl = csl_from_source_meta("pubmed", PUBMED_META_STRUCTURED)
    assert csl["author"] == [
        {"family": "Doudna", "given": "Jennifer A."},
        {"family": "Charpentier", "given": "Emmanuelle"},
    ]


def test_csl_from_pubmed_string_authors_to_literal():
    csl = csl_from_source_meta("pubmed", PUBMED_META_STRING_AUTHORS)
    assert csl["author"] == [
        {"literal": "Alice Smith"},
        {"literal": "Bob Jones"},
    ]


def test_csl_from_pubmed_pmid():
    csl = csl_from_source_meta("pubmed", PUBMED_META_STRUCTURED)
    assert csl["PMID"] == "12345678"


def test_csl_from_pubmed_type():
    csl = csl_from_source_meta("pubmed", PUBMED_META_STRUCTURED)
    assert csl["type"] == "article-journal"


def test_csl_from_pubmed_malformed_no_raise():
    result = csl_from_source_meta("pubmed", {})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# csl_from_source_meta — unknown source_type
# ---------------------------------------------------------------------------


def test_csl_from_unknown_source_type_returns_empty():
    assert csl_from_source_meta("unknown_source", {"foo": "bar"}) == {}


def test_csl_from_empty_source_type_returns_empty():
    assert csl_from_source_meta("", {}) == {}


# ---------------------------------------------------------------------------
# csl_from_pdf_info
# ---------------------------------------------------------------------------


def _make_pdf(title: str | None = None, author: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf)
    if title is not None:
        c.setTitle(title)
    if author is not None:
        c.setAuthor(author)
    c.drawString(72, 720, "Hello world")
    c.showPage()
    c.save()
    return buf.getvalue()


def test_csl_from_pdf_info_title_extracted():
    data = _make_pdf(title="My Paper")
    csl = csl_from_pdf_info(data)
    assert csl["title"] == "My Paper"


def test_csl_from_pdf_info_author_extracted():
    data = _make_pdf(author="Jane Roe")
    csl = csl_from_pdf_info(data)
    assert csl["author"] == [{"literal": "Jane Roe"}]


def test_csl_from_pdf_info_both_fields():
    data = _make_pdf(title="My Paper", author="Jane Roe")
    csl = csl_from_pdf_info(data)
    assert csl["title"] == "My Paper"
    assert csl["author"] == [{"literal": "Jane Roe"}]


def test_csl_from_pdf_info_no_metadata_returns_empty():
    # reportlab always writes placeholder defaults (Title=b"untitled",
    # Author=b"anonymous") — we cannot test "no /Info" via reportlab.
    # Instead verify that a raw PDF byte stream with no /Info dict at all
    # (a minimal hand-crafted stub) yields {}.
    minimal_pdf = (
        b"%PDF-1.4\n1 0 obj<</Type /Catalog>>endobj\nxref\n0 1\n"
        b"0000000000 65535 f \ntrailer<</Size 1/Root 1 0 R>>\nstartxref\n9\n%%EOF"
    )
    csl = csl_from_pdf_info(minimal_pdf)
    assert csl == {}


def test_csl_from_pdf_info_garbage_input_returns_empty():
    csl = csl_from_pdf_info(b"not a pdf at all")
    assert csl == {}


def test_csl_from_pdf_info_empty_bytes_returns_empty():
    csl = csl_from_pdf_info(b"")
    assert csl == {}


def test_csl_from_pdf_info_placeholder_defaults_filtered():
    """reportlab writes Title=b'untitled' and Author=b'anonymous' when no
    metadata is set.  The placeholder filter must drop both so the result is {}.
    """
    data = _make_pdf()  # no title/author → reportlab defaults
    csl = csl_from_pdf_info(data)
    assert csl == {}


def test_csl_from_pdf_info_real_metadata_not_filtered():
    """Real Title/Author values must not be dropped by the placeholder filter."""
    data = _make_pdf(title="Quantum Mechanics Review", author="Werner Heisenberg")
    csl = csl_from_pdf_info(data)
    assert csl["title"] == "Quantum Mechanics Review"
    assert csl["author"] == [{"literal": "Werner Heisenberg"}]


# ---------------------------------------------------------------------------
# merge_csl
# ---------------------------------------------------------------------------


def test_merge_csl_first_wins():
    result = merge_csl({"title": "A"}, {"title": "B", "year": 2})
    assert result["title"] == "A"
    assert result["year"] == 2


def test_merge_csl_empty_string_skipped():
    result = merge_csl({"title": ""}, {"title": "Fallback"})
    assert result["title"] == "Fallback"


def test_merge_csl_none_skipped():
    result = merge_csl({"DOI": None}, {"DOI": "10.1/x"})
    assert result["DOI"] == "10.1/x"


def test_merge_csl_empty_list_skipped():
    result = merge_csl({"author": []}, {"author": [{"literal": "Jane"}]})
    assert result["author"] == [{"literal": "Jane"}]


def test_merge_csl_no_args_returns_empty():
    assert merge_csl() == {}


def test_merge_csl_single_arg():
    assert merge_csl({"title": "X"}) == {"title": "X"}


def test_merge_csl_all_empty_values_skipped():
    result = merge_csl({"a": None, "b": ""}, {"a": "val"})
    assert result["a"] == "val"
    assert "b" not in result


# ---------------------------------------------------------------------------
# generate_citekey — requires database
# ---------------------------------------------------------------------------


def _insert_ref_with_citekey(session, workspace_id, citekey: str):
    ref = Reference(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename=f"{citekey}.pdf",
        storage_path=f"ws/{workspace_id}/{citekey}.pdf",
        mime_type="application/pdf",
        citekey=citekey,
    )
    session.add(ref)
    session.flush()
    return ref


def test_generate_citekey_base_from_family_year(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    csl = {"author": [{"family": "Smith", "given": "Jane"}], "issued": {"date-parts": [[2021]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key == "smith2021"


def test_generate_citekey_collision_appends_suffix(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    # Pre-insert "smith2021" in the workspace
    with pg_session_factory() as s:
        _insert_ref_with_citekey(s, ws_id, "smith2021")
        s.commit()

    csl = {"author": [{"family": "Smith", "given": "John"}], "issued": {"date-parts": [[2021]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key == "smith2021a"


def test_generate_citekey_collision_two_existing(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        _insert_ref_with_citekey(s, ws_id, "smith2021")
        _insert_ref_with_citekey(s, ws_id, "smith2021a")
        s.commit()

    csl = {"author": [{"family": "Smith", "given": "Bob"}], "issued": {"date-parts": [[2021]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key == "smith2021b"


def test_generate_citekey_same_key_different_workspace_no_collision(pg_session_factory):
    """smith2021 in ws_a must not block ws_b from getting smith2021."""
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=make_user(s, email="a@ki.se"))
        ws_b = make_workspace(s, owner=make_user(s, email="b@ki.se"))
        s.commit()
        a_id, b_id = ws_a.id, ws_b.id

    with pg_session_factory() as s:
        _insert_ref_with_citekey(s, a_id, "smith2021")
        s.commit()

    csl = {"author": [{"family": "Smith", "given": "Jane"}], "issued": {"date-parts": [[2021]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, b_id, s)
    assert key == "smith2021"


def test_generate_citekey_literal_author_uses_last_word(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    csl = {"author": [{"literal": "John von Neumann"}], "issued": {"date-parts": [[1945]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key == "neumann1945"


def test_generate_citekey_missing_author_returns_ref_prefix(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    csl = {"issued": {"date-parts": [[2020]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key.startswith("ref-")
    assert len(key) == len("ref-") + 12  # 12 hex chars


def test_generate_citekey_missing_year_returns_ref_prefix(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    csl = {"author": [{"family": "Brown"}]}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key.startswith("ref-")


def test_generate_citekey_empty_csl_returns_ref_prefix(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        key = generate_citekey({}, ws_id, s)
    assert key.startswith("ref-")


def test_generate_citekey_special_chars_stripped(pg_session_factory):
    """Umlauts/hyphens in surname are stripped, not transliterated."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    csl = {
        "author": [{"family": "Müller-Berg", "given": "Klaus"}],
        "issued": {"date-parts": [[2010]]},
    }
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    # re.sub(r"[^a-z0-9]", "", "müller-berg2010") → "lllerberg2010"? No:
    # lowercased "müller-berg2010" → strip non-[a-z0-9] → "llerberg2010"
    # Actually: "m" "ü"(stripped) "l" "l" "e" "r" "-"(stripped) "b" "e" "r" "g" "2" "0" "1" "0"
    # → "mllerberg2010"  (ü → stripped entirely in ASCII re.sub)
    assert key == "mllerberg2010"


def test_generate_citekey_unrelated_numeric_suffix_no_spurious_letter(pg_session_factory):
    """A pre-existing key like 'smith20219' must NOT force a suffix on 'smith2021'.

    The LIKE query matches it, but the narrowing filter should exclude it because
    it has a digit suffix rather than a single lowercase letter.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        _insert_ref_with_citekey(s, ws_id, "smith20219")
        s.commit()

    csl = {"author": [{"family": "Smith", "given": "Jane"}], "issued": {"date-parts": [[2021]]}}
    with pg_session_factory() as s:
        key = generate_citekey(csl, ws_id, s)
    assert key == "smith2021"
