"""Pure helpers for the citation source viewer (Tier 1, extracted text).

Numbering, marker→link transform (sentinel + post-sanitization injection),
and evidence-span location. No git/DB/file I/O — callers own those.
"""

from __future__ import annotations

import html as _html
import re

import bleach
import lxml.html
import markdown

from call_manager.proposal_assist.citations.ledger import find_by_id
from call_manager.proposal_assist.citations.markers import MARKER_RE, find_markers
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)


def build_citation_numbering(proposal_text: str, ledger: dict) -> dict[str, int]:
    """Map each live, ledger-backed evidence id to its citation number.

    Numbering is per citekey (= per reference) in first-appearance order across
    the WHOLE proposal; repeat cites of a citekey share the number. Markers with
    no matching ledger row are excluded (they render inert)."""
    number_by_citekey: dict[str, int] = {}
    number_by_ev: dict[str, int] = {}
    for m in find_markers(proposal_text):
        if find_by_id(ledger, m["ev_id"]) is None:
            continue
        citekey = m["citekey"]
        if citekey not in number_by_citekey:
            number_by_citekey[citekey] = len(number_by_citekey) + 1
        number_by_ev[m["ev_id"]] = number_by_citekey[citekey]
    return number_by_ev


def evidence_order_for_reference(
    proposal_text: str, ledger: dict, reference_id: str
) -> list[str]:
    """Ordered (document order) list of live evidence ids whose ledger row cites
    ``reference_id``. Drives prev/next + 'i of N'.

    Duplicate marker ids (data anomaly) are collapsed to first occurrence so that
    ``order.index(ev_id)`` is always unambiguous."""
    out: list[str] = []
    seen: set[str] = set()
    for m in find_markers(proposal_text):
        ev_id = m["ev_id"]
        if ev_id in seen:
            continue
        row = find_by_id(ledger, ev_id)
        if row is not None and str(row.get("reference_id")) == str(reference_id):
            seen.add(ev_id)
            out.append(ev_id)
    return out


def build_citation_stances(ledger: dict) -> dict[str, str]:
    """Map each evidence id to its stance (e.g. 'supports'/'contradicts'), used
    to tint the inline citation marker. Rows without a stance are omitted."""
    return {
        e["id"]: e["stance"]
        for e in ledger.get("evidence", [])
        if e.get("id") and e.get("stance")
    }


# ---------------------------------------------------------------------------
# Sentinel-based marker → cite-link transform
# ---------------------------------------------------------------------------

_SENT_OPEN = ""
_SENT_CLOSE = ""
_SENTINEL_RE = re.compile(
    re.escape(_SENT_OPEN) + r"(?P<n>\d+):(?P<ev>ev-[0-9a-f]{6,})" + re.escape(_SENT_CLOSE)
)

_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em",
    "code", "pre", "ul", "ol", "li", "blockquote", "a", "table", "thead",
    "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRS = {"a": ["href", "title"]}
_NO_INJECT_TAGS = {"code", "pre"}


_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)


def _code_span_ranges(text: str) -> list[tuple[int, int]]:
    """Return (start, end) intervals occupied by backtick code spans."""
    return [(m.start(), m.end()) for m in _CODE_SPAN_RE.finditer(text)]


def _to_sentinels(raw_text: str, number_by_ev: dict[str, int]) -> str:
    """Replace each numbered marker with a plain-text sentinel before markdown.
    Markers absent from ``number_by_ev`` are left untouched (their visible
    ``[@citekey]`` renders as inert text; bleach strips the ev-comment).
    Markers that fall inside a backtick code span are never replaced so that
    they render literally inside ``<code>`` after markdown processing."""
    cleaned = raw_text.replace(_SENT_OPEN, "").replace(_SENT_CLOSE, "")
    code_ranges = _code_span_ranges(cleaned)

    def _in_code_span(start: int, end: int) -> bool:
        return any(cs <= start and end <= ce for cs, ce in code_ranges)

    def _sub(m: re.Match) -> str:
        if _in_code_span(m.start(), m.end()):
            return m.group(0)
        ev = m.group("ev_id")
        n = number_by_ev.get(ev)
        if n is None:
            return m.group(0)
        return f"{_SENT_OPEN}{n}:{ev}{_SENT_CLOSE}"

    return MARKER_RE.sub(_sub, cleaned)


def _inject_links(
    sanitized_html: str, workspace_id: object, stance_by_ev: dict | None = None
) -> str:
    """Replace sentinels with trusted cite links, ONLY inside eligible text
    nodes — never within attributes or <code>/<pre>."""
    if _SENT_OPEN not in sanitized_html:
        return sanitized_html
    root = lxml.html.fragment_fromstring(sanitized_html, create_parent="div")

    def _link_html(n: str, ev: str) -> str:
        href = f"/workspaces/{workspace_id}/citations/{ev}/reader"
        stance = (stance_by_ev or {}).get(ev)
        cls = f"cite cite--{_html.escape(stance)}" if stance else "cite"
        return f'<a class="{cls}" href="{_html.escape(href)}">[{n}]</a>'

    def _expand(text: str | None) -> str | None:
        if text is None or _SENT_OPEN not in text:
            return None
        return _SENTINEL_RE.sub(lambda m: _link_html(m.group("n"), m.group("ev")), text)

    for el in list(root.iter()):
        in_code = el.tag in _NO_INJECT_TAGS or any(
            a.tag in _NO_INJECT_TAGS for a in el.iterancestors()
        )
        if in_code:
            continue
        new_text = _expand(el.text)
        if new_text is not None:
            frag = lxml.html.fragment_fromstring("<x>" + new_text + "</x>")
            el.text = frag.text
            for i, child in enumerate(list(frag)):
                el.insert(i, child)
        new_tail = _expand(el.tail)
        if new_tail is not None and el.getparent() is not None:
            parent = el.getparent()
            frag = lxml.html.fragment_fromstring("<x>" + new_tail + "</x>")
            el.tail = frag.text
            idx = parent.index(el)
            for i, child in enumerate(list(frag)):
                parent.insert(idx + 1 + i, child)

    return (root.text or "") + "".join(
        lxml.html.tostring(c, encoding="unicode") for c in root
    )


def _all_indices(haystack: str, needle: str) -> list[int]:
    out, start = [], 0
    if not needle:
        return out
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return out
        out.append(i)
        start = i + 1


def locate_evidence(
    full_text: str, chunk_text: str | None, quote: str
) -> tuple[int, int] | None:
    """Return [start, end) of the evidence span in ``full_text``, or None.

    Recomputes live (never trusts stored offsets):
      1. If the cited chunk is available, find the quote uniquely WITHIN the
         chunk, place the chunk UNIQUELY in full_text, map the offsets, and
         accept only if in-bounds and the span normalizes to the quote.
      2. Otherwise / on any failure: find the quote uniquely in full_text.
      3. Ambiguous or not found -> None."""
    if chunk_text:
        try:
            cstart, cend = find_quote_offsets(chunk_text, quote)
        except (QuoteNotFound, QuoteAmbiguous):
            cstart = cend = None
        if cstart is not None:
            placements = _all_indices(full_text, chunk_text)
            if len(placements) == 1:
                s = placements[0] + cstart
                e = placements[0] + cend
                if 0 <= s < e <= len(full_text) and (
                    normalize_for_quote_match(full_text[s:e])
                    == normalize_for_quote_match(quote)
                ):
                    return (s, e)
    try:
        return find_quote_offsets(full_text, quote)
    except (QuoteNotFound, QuoteAmbiguous):
        return None


def render_proposal_with_citations(
    raw_text: str,
    *,
    number_by_ev: dict[str, int],
    workspace_id: object,
    stance_by_ev: dict | None = None,
) -> str:
    """markdown → bleach → inject cite links. With an empty map this is exactly
    the legacy render (markers inert). ``stance_by_ev`` (optional) tints each
    marker via a ``cite--<stance>`` class."""
    with_sentinels = _to_sentinels(raw_text, number_by_ev)
    rendered = markdown.markdown(with_sentinels, extensions=["extra"])
    sanitized = bleach.clean(
        rendered, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True
    )
    return _inject_links(sanitized, workspace_id, stance_by_ev)
