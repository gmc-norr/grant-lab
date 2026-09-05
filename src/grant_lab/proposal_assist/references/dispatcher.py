"""Route a user-typed input string to the right reference-fetch backend.

The agent + the UI both call dispatch_fetch(input) with whatever the
user typed (DOI, PMID, arXiv ID, or URL). Regex detection picks the
right backend.

Note: ordering matters. PMID and arXiv ID patterns overlap with PMID
being a strict numeric prefix; we check DOI (most specific prefix)
first, then PMID (purely numeric), then arXiv (numeric-dot-numeric),
then URL (only one with a scheme).
"""

from __future__ import annotations

import re

from grant_lab.proposal_assist.references.fetcher import (
    fetch_arxiv,
    fetch_crossref_doi,
    fetch_generic_url,
    fetch_pubmed_pmid,
)

_DOI_RE = re.compile(r"^10\.\d{4,9}/")
_PMID_RE = re.compile(r"^\d{6,9}$")
_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def detect_source_type(input_str: str) -> str:
    """Classify the input as one of 'crossref'|'pubmed'|'arxiv'|'web'.
    Raises ValueError if the input matches no pattern."""
    s = (input_str or "").strip()
    if _DOI_RE.match(s):
        return "crossref"
    if _PMID_RE.fullmatch(s):
        return "pubmed"
    if _ARXIV_RE.fullmatch(s):
        return "arxiv"
    if _URL_RE.match(s):
        return "web"
    raise ValueError(f"unrecognized input: {input_str!r}")


def dispatch_fetch(input_str: str) -> tuple[bytes, str, str, dict]:
    """Detect source type + call the matching backend. Returns the
    backend's tuple unchanged (bytes, filename, mime, metadata)."""
    s = input_str.strip()
    source = detect_source_type(s)
    if source == "crossref":
        return fetch_crossref_doi(s)
    if source == "pubmed":
        return fetch_pubmed_pmid(s)
    if source == "arxiv":
        return fetch_arxiv(s)
    return fetch_generic_url(s)
