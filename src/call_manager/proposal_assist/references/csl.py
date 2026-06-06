"""CSL-JSON conversion and citekey generation helpers.

Pure functions (except generate_citekey which needs a database session for
collision checking). No function should raise on malformed / missing input —
they return {} or partial results defensively.
"""

from __future__ import annotations

import io
import re
import string
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Reference

# ---------------------------------------------------------------------------
# Type-map: Crossref work type → CSL type
# ---------------------------------------------------------------------------

_CROSSREF_TYPE_MAP: dict[str, str] = {
    "journal-article": "article-journal",
    "proceedings-article": "paper-conference",
    "book": "book",
    "book-chapter": "chapter",
    "posted-content": "article",
    "report": "report",
    "dataset": "dataset",
}

# Keys to keep from a Crossref author entry.
_AUTHOR_KEEP_KEYS = {"family", "given", "literal"}


# ---------------------------------------------------------------------------
# csl_from_source_meta
# ---------------------------------------------------------------------------


def csl_from_source_meta(source_type: str, meta: dict) -> dict:
    """Convert a fetcher's meta dict to a (subset) CSL-JSON dict.

    Defensive: never raises even on completely malformed input.
    Returns {} for unknown source_type or on catastrophic parse failure.
    """
    try:
        if source_type == "crossref":
            return _from_crossref(meta)
        if source_type == "arxiv":
            return _from_arxiv(meta)
        if source_type == "pubmed":
            return _from_pubmed(meta)
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _from_crossref(meta: dict) -> dict:
    message = meta.get("message")
    if not isinstance(message, dict):
        return {}

    out: dict = {}

    # title
    try:
        titles = message.get("title")
        if titles and isinstance(titles, list) and titles[0]:
            out["title"] = titles[0]
    except Exception:  # noqa: BLE001
        pass

    # author — keep only family/given/literal keys
    try:
        authors = message.get("author")
        if authors and isinstance(authors, list):
            cleaned = []
            for a in authors:
                if isinstance(a, dict):
                    entry = {k: v for k, v in a.items() if k in _AUTHOR_KEEP_KEYS}
                    if entry:
                        cleaned.append(entry)
            if cleaned:
                out["author"] = cleaned
    except Exception:  # noqa: BLE001
        pass

    # issued
    try:
        issued = message.get("issued")
        if issued:
            out["issued"] = issued
    except Exception:  # noqa: BLE001
        pass

    # DOI
    try:
        doi = message.get("DOI")
        if doi:
            out["DOI"] = doi
    except Exception:  # noqa: BLE001
        pass

    # container-title
    try:
        ct = message.get("container-title")
        if ct and isinstance(ct, list) and ct[0]:
            out["container-title"] = ct[0]
    except Exception:  # noqa: BLE001
        pass

    # volume / issue / page
    for key in ("volume", "issue", "page"):
        try:
            val = message.get(key)
            if val is not None and val != "":
                out[key] = val
        except Exception:  # noqa: BLE001
            pass

    # type
    try:
        raw_type = message.get("type", "")
        out["type"] = _CROSSREF_TYPE_MAP.get(raw_type, "article-journal")
    except Exception:  # noqa: BLE001
        out["type"] = "article-journal"

    return out


def _from_arxiv(meta: dict) -> dict:
    if not isinstance(meta, dict):
        return {}

    out: dict = {}

    try:
        title = meta.get("title")
        if title:
            out["title"] = title
    except Exception:  # noqa: BLE001
        pass

    try:
        authors = meta.get("authors")
        if authors and isinstance(authors, list):
            out["author"] = [{"literal": a} for a in authors if a]
    except Exception:  # noqa: BLE001
        pass

    try:
        year = meta.get("year")
        if year is not None:
            out["issued"] = {"date-parts": [[year]]}
    except Exception:  # noqa: BLE001
        pass

    try:
        url = meta.get("url")
        if url:
            out["URL"] = url
    except Exception:  # noqa: BLE001
        pass

    out["type"] = "article"
    return out


def _from_pubmed(meta: dict) -> dict:
    if not isinstance(meta, dict):
        return {}

    out: dict = {}

    try:
        title = meta.get("title")
        if title:
            out["title"] = title
    except Exception:  # noqa: BLE001
        pass

    try:
        authors = meta.get("authors")
        if authors and isinstance(authors, list):
            cleaned = []
            for a in authors:
                if isinstance(a, dict):
                    entry = {k: v for k, v in a.items() if k in _AUTHOR_KEEP_KEYS}
                    if entry:
                        cleaned.append(entry)
                elif isinstance(a, str) and a:
                    cleaned.append({"literal": a})
            if cleaned:
                out["author"] = cleaned
    except Exception:  # noqa: BLE001
        pass

    try:
        pmid = meta.get("pmid")
        if pmid is not None:
            out["PMID"] = pmid
    except Exception:  # noqa: BLE001
        pass

    out["type"] = "article-journal"
    return out


# ---------------------------------------------------------------------------
# csl_from_pdf_info
# ---------------------------------------------------------------------------


def csl_from_pdf_info(data: bytes) -> dict:
    """Extract title / author from a PDF's /Info dictionary.

    Returns a (partial) CSL-JSON dict with 'title' and/or 'author' keys.
    Returns {} on any parse error or absent metadata — never raises.
    """
    if not data:
        return {}

    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser

        parser = PDFParser(io.BytesIO(data))
        doc = PDFDocument(parser)

        info_list = doc.info  # list of dicts
        if not info_list:
            return {}

        info = info_list[0]
        out: dict = {}

        raw_title = info.get("Title")
        if raw_title:
            if isinstance(raw_title, bytes):
                raw_title = raw_title.decode("utf-8", errors="replace")
            title = raw_title.strip()
            if title:
                out["title"] = title

        raw_author = info.get("Author")
        if raw_author:
            if isinstance(raw_author, bytes):
                raw_author = raw_author.decode("utf-8", errors="replace")
            author = raw_author.strip()
            if author:
                out["author"] = [{"literal": author}]

        return out
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# merge_csl
# ---------------------------------------------------------------------------


def merge_csl(*parts: dict) -> dict:
    """Merge multiple CSL-JSON dicts; first non-empty value per key wins.

    'Empty' means None, "", or [].
    """
    out: dict = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if key in out:
                continue
            # Skip empty values
            if value is None or value == "" or value == []:
                continue
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# generate_citekey
# ---------------------------------------------------------------------------


def generate_citekey(csl: dict, workspace_id, session: Session) -> str:
    """Generate a unique citekey for a reference within a workspace.

    Strategy:
    - If author[0] has 'family', use surname.
    - Else if 'literal', use last whitespace-delimited word.
    - year from issued.date-parts[0][0].
    - base = re.sub(r"[^a-z0-9]", "", f"{surname}{year}".lower())
    - Collision-check against existing citekeys in the workspace starting
      with base. Returns base if free, else base+"a", base+"b", …
    - If author or year missing: return "ref-<12 random hex chars>".
    """
    surname: str | None = None
    year: int | str | None = None

    try:
        authors = csl.get("author")
        if authors and isinstance(authors, list) and authors:
            first = authors[0]
            if isinstance(first, dict):
                if "family" in first and first["family"]:
                    surname = str(first["family"])
                elif "literal" in first and first["literal"]:
                    words = str(first["literal"]).split()
                    if words:
                        surname = words[-1]
    except Exception:  # noqa: BLE001
        pass

    try:
        issued = csl.get("issued")
        if issued and isinstance(issued, dict):
            date_parts = issued.get("date-parts")
            if (
                date_parts
                and isinstance(date_parts, list)
                and date_parts[0]
                and isinstance(date_parts[0], list)
            ):
                year = date_parts[0][0]
    except Exception:  # noqa: BLE001
        pass

    if not surname or year is None:
        return f"ref-{uuid.uuid4().hex[:12]}"

    base = re.sub(r"[^a-z0-9]", "", f"{surname}{year}".lower())
    if not base:
        return f"ref-{uuid.uuid4().hex[:12]}"

    # Query existing citekeys in the workspace that start with base
    try:
        existing: set[str] = set(
            session.scalars(
                select(Reference.citekey).where(
                    Reference.workspace_id == workspace_id,
                    Reference.citekey.like(base + "%"),
                )
            )
        )
    except Exception:  # noqa: BLE001
        existing = set()

    if base not in existing:
        return base

    for letter in string.ascii_lowercase:
        candidate = base + letter
        if candidate not in existing:
            return candidate

    # Fallback if all 26 suffixes taken (extremely unlikely in v1)
    return f"ref-{uuid.uuid4().hex[:12]}"
