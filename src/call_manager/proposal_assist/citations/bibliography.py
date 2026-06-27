"""Pure-Python CSL-JSON -> formatted-text bibliography formatter.

Supports two v1 citation styles:
  - "vancouver"   : numbered, document-order reference list.
  - "author-year" : alphabetically-sorted author-year reference list.

Every function here is pure: no DB / git / IO, deterministic, and never raises
on partial or malformed CSL-JSON (it degrades gracefully, omitting absent
fields without leaving dangling punctuation).

The CSL field names used here match what ``references/csl.py`` produces:
``author`` (list of ``{family, given}`` or ``{literal}``),
``issued.date-parts[[year, month, day]]``, ``title``, ``container-title``,
``volume``, ``issue``, ``page``, ``DOI``, ``type``.

The ``build_bibliography`` orchestration (fail-closed, ledger-resolved) lives
in a later task; this module is the formatting layer only.
"""

from __future__ import annotations

import re
import uuid

from call_manager.db.models import Reference
from call_manager.proposal_assist.citations.audit import _APPROVED_METADATA
from call_manager.proposal_assist.citations.gitio import write_and_commit_files
from call_manager.proposal_assist.citations.ledger import find_by_citekey
from call_manager.proposal_assist.citations.markers import MARKER_RE, find_markers

_REFERENCES_HEADING = "# References\n\n"

#: Visible citation token "[@citekey]" (the bracketed part of a marker, ignoring
#: any trailing ev comment). Used to detect residual/bare markers after the
#: full-marker substitution in compose_export — see the fail-closed scrub there.
_BARE_MARKER_RE = re.compile(r"\[@([A-Za-z0-9_-]+)\]")

#: The two v1 citation styles. Canonical source — the DB CHECK constraint
#: (migration 0040 / models.Workspace), the settings route, and the MCP tool all
#: validate against this set. ``render_bibliography`` treats anything other than
#: "author-year" as "vancouver", but callers should reject unknown styles first.
CITATION_STYLES = frozenset({"vancouver", "author-year"})

#: Filename of the rendered bibliography git artifact. This is a SEPARATE file
#: from proposal.md and is NEVER merged into it (spec D13).
BIBLIOGRAPHY_FILENAME = "bibliography.md"

# Vancouver: list author cap before " et al." truncation.
_VANCOUVER_AUTHOR_CAP = 6
# Author-year: a (generous) cap before truncating to "First et al.".
_AUTHOR_YEAR_AUTHOR_CAP = 20


# ---------------------------------------------------------------------------
# Author formatting
# ---------------------------------------------------------------------------


def _initials(given: str, *, dotted: bool) -> str:
    """Turn a given-name string into initials.

    Vancouver: "Jane Mary" -> "JM" (no dots, no spaces).
    Author-year (dotted): "Jane Mary" -> "J. M.".
    """
    parts = [p for p in str(given).replace("-", " ").split() if p]
    letters = [p[0].upper() for p in parts if p]
    if not letters:
        return ""
    if dotted:
        return " ".join(f"{ltr}." for ltr in letters)
    return "".join(letters)


def _clean(value: object) -> str:
    """Collapse ALL internal whitespace (incl. newlines/tabs) to single spaces.

    The bibliography is a line-delimited markdown artifact: an embedded newline
    in any field would split one entry across lines and desync Vancouver's
    ``[n]`` numbering. ``csl_json`` is approved canonical, but approval does not
    guarantee single-line fields, so every extracted string is normalized here.
    """
    return " ".join(str(value).split())


def _one_author_vancouver(entry: dict) -> str | None:
    """Render a single author entry, Vancouver style ("Smith JM")."""
    literal = entry.get("literal")
    if isinstance(literal, str) and literal.strip():
        return _clean(literal)
    family = entry.get("family")
    if not (isinstance(family, str) and family.strip()):
        return None
    family = _clean(family)
    given = entry.get("given")
    inits = _initials(given, dotted=False) if isinstance(given, str) else ""
    return f"{family} {inits}" if inits else family


def _one_author_year(entry: dict) -> str | None:
    """Render a single author entry, author-year style ("Smith, J. M.")."""
    literal = entry.get("literal")
    if isinstance(literal, str) and literal.strip():
        return _clean(literal)
    family = entry.get("family")
    if not (isinstance(family, str) and family.strip()):
        return None
    family = _clean(family)
    given = entry.get("given")
    inits = _initials(given, dotted=True) if isinstance(given, str) else ""
    return f"{family}, {inits}" if inits else family


def format_author_list(authors: list[dict] | None, *, style: str) -> str:
    """Format an author list for the given style.

    Returns "" for empty / None / all-malformed input. Never raises.
    """
    if not authors or not isinstance(authors, list):
        return ""

    if style == "author-year":
        rendered = [r for a in authors if isinstance(a, dict) for r in (_one_author_year(a),) if r]
        if not rendered:
            return ""
        if len(rendered) > _AUTHOR_YEAR_AUTHOR_CAP:
            return f"{rendered[0]}, et al."
        if len(rendered) == 1:
            return rendered[0]
        # "A, B, & C"  /  "A & B"
        return ", ".join(rendered[:-1]) + ", & " + rendered[-1]

    # default: vancouver
    rendered = [r for a in authors if isinstance(a, dict) for r in (_one_author_vancouver(a),) if r]
    if not rendered:
        return ""
    if len(rendered) > _VANCOUVER_AUTHOR_CAP:
        return ", ".join(rendered[:_VANCOUVER_AUTHOR_CAP]) + " et al."
    return ", ".join(rendered)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _year_of(csl: dict) -> str | None:
    """Extract the year from ``issued.date-parts[0][0]``. Defensive."""
    try:
        issued = csl.get("issued")
        if not isinstance(issued, dict):
            return None
        date_parts = issued.get("date-parts")
        if not (isinstance(date_parts, list) and date_parts):
            return None
        first = date_parts[0]
        if not (isinstance(first, list) and first):
            return None
        year = first[0]
        if year is None or str(year).strip() == "":
            return None
        return str(year)
    except Exception:  # noqa: BLE001 - never raise
        return None


def _suffix_from_citekey(citekey: str, year: str | None) -> str:
    """Return the trailing single ``[a-z]`` of a ``<year><letter>`` citekey.

    e.g. ("smith2021a", "2021") -> "a"; ("smith2021", "2021") -> "".
    Returns "" when there is no year, or the citekey does not end with the
    year immediately followed by exactly one lowercase letter. Disambiguation
    suffixes are lowercase-only by convention (the citekey allocator emits
    a/b/c), so an uppercase trailing letter is treated as not-a-suffix.
    """
    if not year or not isinstance(citekey, str):
        return ""
    if len(citekey) < len(year) + 1:
        return ""
    last = citekey[-1]
    if not (last.isalpha() and last.islower() and last.isascii()):
        return ""
    if citekey[:-1].endswith(year):
        return last
    return ""


def _title_of(csl: dict) -> str:
    title = csl.get("title")
    return _clean(title) if isinstance(title, str) and title.strip() else ""


def _container_of(csl: dict) -> str:
    ct = csl.get("container-title")
    return _clean(ct) if isinstance(ct, str) and ct.strip() else ""


def _str_field(csl: dict, key: str) -> str:
    val = csl.get(key)
    if val is None:
        return ""
    return _clean(val)


def _doi_of(csl: dict) -> str:
    return _str_field(csl, "DOI")


def _volume_issue_page(csl: dict) -> tuple[str, str, str]:
    return (
        _str_field(csl, "volume"),
        _str_field(csl, "issue"),
        _str_field(csl, "page"),
    )


# ---------------------------------------------------------------------------
# Per-entry formatting
# ---------------------------------------------------------------------------


def _assemble(pieces: list[str]) -> str:
    """Join sentence pieces with ``". "`` and terminate with a single period.

    Each piece may already end in its own terminator: a period (author-year
    initial "Smith, J." or title "Outcomes."), a question mark ("Is obesity a
    disease?"), or an exclamation mark ("Surprising results!").

    Handling (applied to EVERY piece, not just the last):
    - A piece ending in ``.`` has it stripped before joining so we never emit a
      doubled ``..`` (e.g. "Smith, J." becomes "Smith, J" for joining,
      preserving the mid-string initial); the ``". "`` separator re-terminates.
    - A piece ending in ``?`` or ``!`` keeps that terminator as the separator —
      it is joined to the NEXT piece with a single space (so an intermediate
      "Is obesity a disease?" yields "...disease? Container.", not
      "...disease?. Container.").
    - The final terminator is ``.`` unless the last piece's own terminator is
      ``?``/``!`` (then that is kept).
    """
    if not pieces:
        return "."
    out = ""
    for i, piece in enumerate(pieces):
        # Strip a single trailing "." (intermediate initials, "Title.") so the
        # separator re-terminates without a doubled "..". "?"/"!" are kept and
        # act as the separator-into-next-piece.
        cleaned = piece[:-1] if piece.endswith(".") else piece
        if i == 0:
            out = cleaned
            continue
        # Choose the separator based on how the PREVIOUS assembled text ended:
        # a "?"/"!" terminator is already there → just a space; otherwise ". ".
        sep = " " if out[-1:] in "?!" else ". "
        out += sep + cleaned
    # Final terminator: keep "?"/"!" on the last piece, else add ".".
    return out if out[-1:] in "?!" else out + "."


def format_vancouver(csl: dict, number: int) -> str:
    """Format a single CSL record as a numbered Vancouver entry.

    Shape: ``[n] Authors. Title. Container. year;volume(issue):page. doi:DOI``
    Absent segments are omitted cleanly (no dangling " . ;()"). If both title
    and authors are absent, falls back to ``[n] Untitled``.
    """
    if not isinstance(csl, dict):
        csl = {}

    authors = format_author_list(csl.get("author"), style="vancouver")
    title = _title_of(csl)

    if not authors and not title:
        return f"[{number}] Untitled"

    # Sentence-style pieces, each ending with a period.
    pieces: list[str] = []
    if authors:
        pieces.append(authors)
    if title:
        pieces.append(title)

    container = _container_of(csl)
    if container:
        pieces.append(container)

    # Locator block: year;volume(issue):page
    year = _year_of(csl)
    volume, issue, page = _volume_issue_page(csl)
    locator = _build_locator_vancouver(year, volume, issue, page)
    if locator:
        pieces.append(locator)

    out = f"[{number}] {_assemble(pieces)}"

    doi = _doi_of(csl)
    if doi:
        out += f" doi:{doi}"
    return out


def _build_locator_vancouver(year: str | None, volume: str, issue: str, page: str) -> str:
    """Build the ``year;volume(issue):page`` locator, omitting absent parts."""
    if not year:
        # Without a year the volume/issue/page block has no anchor; drop it.
        return ""
    locator = year
    if volume:
        locator += f";{volume}"
        if issue:
            locator += f"({issue})"
        if page:
            locator += f":{page}"
    elif page:
        # No volume but a page exists; attach with a colon after the year.
        locator += f":{page}"
    return locator


def format_author_year(csl: dict, citekey: str) -> str:
    """Format a single CSL record as an author-year entry.

    Shape:
    ``Authors (year[suffix]). Title. Container, volume(issue), page. https://doi.org/DOI``
    Absent parts are omitted cleanly. The disambiguation suffix is carried in
    from the citekey (e.g. "smith2021a" -> "2021a").
    """
    if not isinstance(csl, dict):
        csl = {}

    authors = format_author_list(csl.get("author"), style="author-year")
    year = _year_of(csl)
    suffix = _suffix_from_citekey(citekey, year)
    title = _title_of(csl)

    # Leading "Authors (year)." block.
    head = authors
    if year:
        year_part = f"({year}{suffix})"
        head = f"{head} {year_part}" if head else year_part

    pieces: list[str] = []
    if head:
        pieces.append(head)
    if title:
        pieces.append(title)

    # "Container, volume(issue), page" block.
    container = _container_of(csl)
    volume, issue, page = _volume_issue_page(csl)
    locator = _build_locator_author_year(container, volume, issue, page)
    if locator:
        pieces.append(locator)

    if not pieces:
        return "Untitled."

    out = _assemble(pieces)

    doi = _doi_of(csl)
    if doi:
        out += f" https://doi.org/{doi}"
    return out


def _build_locator_author_year(container: str, volume: str, issue: str, page: str) -> str:
    """Build the ``Container, volume(issue), page`` locator, omitting absent parts."""
    vip = ""
    if volume:
        vip = volume
        if issue:
            vip += f"({issue})"
        if page:
            vip += f", {page}"
    elif page:
        vip = page

    if container and vip:
        return f"{container}, {vip}"
    if container:
        return container
    return vip


# ---------------------------------------------------------------------------
# Whole-bibliography rendering
# ---------------------------------------------------------------------------


def render_bibliography(entries: list[tuple[str, dict]], style: str) -> str:
    """Render a list of (citekey, csl_json) entries into a markdown string.

    ``entries`` are given in DOCUMENT ORDER.
    - "vancouver": numbered 1..N in the given order; one per line.
    - "author-year": each formatted, then sorted alphabetically by the
      formatted string (case-insensitive).
    Empty ``entries`` -> "" (so callers can skip writing). Never raises.
    """
    if not entries:
        return ""

    if style == "author-year":
        pairs: list[tuple[str, str]] = []  # (formatted, citekey)
        for item in entries:
            citekey, csl = _unpack(item)
            pairs.append((format_author_year(csl, citekey), citekey))
        # Sort case-insensitively by formatted text; citekey breaks ties so the
        # order is fully deterministic even when two entries render identically.
        pairs.sort(key=lambda p: (p[0].casefold(), p[1]))
        lines = [fmt for fmt, _ck in pairs]
    else:  # vancouver
        lines = []
        for number, item in enumerate(entries, start=1):
            _citekey, csl = _unpack(item)
            lines.append(format_vancouver(csl, number))

    return _REFERENCES_HEADING + "\n".join(lines) + "\n"


def _unpack(item: object) -> tuple[str, dict]:
    """Defensively split an entry into (citekey, csl_dict)."""
    citekey = ""
    csl: dict = {}
    try:
        citekey, csl = item  # type: ignore[misc]
    except Exception:  # noqa: BLE001 - tolerate malformed entries
        return "", {}
    if not isinstance(csl, dict):
        csl = {}
    if not isinstance(citekey, str):
        citekey = ""
    return citekey, csl


# ---------------------------------------------------------------------------
# Fail-closed build orchestration
# ---------------------------------------------------------------------------


class BuildBibliographyBlocked(Exception):
    """Raised when one or more cited references are not citable.

    Carries ``blocked``: a list of ``{citekey, reference_id, metadata_status,
    reason}`` dicts (reasons: ``unresolved_citekey``, ``reference_missing``,
    ``reference_tombstoned``, ``metadata_unapproved``; plus ``unresolved_marker``
    raised by :func:`compose_export` for a bare/malformed marker). The build is
    fail-closed: if ANY reference is blocked, NOTHING is rendered or written.
    """

    def __init__(self, blocked: list[dict]) -> None:
        super().__init__(f"bibliography blocked: {len(blocked)} reference(s) not citable")
        self.blocked = blocked


def _ordered_unique_citekeys(proposal_text: str) -> list[str]:
    """Unique citekeys in first-occurrence (document) order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for m in find_markers(proposal_text or ""):
        ck = m.get("citekey")
        if ck and ck not in seen:
            seen.add(ck)
            ordered.append(ck)
    return ordered


def _resolve_cited_entries(
    *, proposal_text: str, ledger: dict, session, workspace_id
) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Resolve every cited citekey to its approved csl_json (fail-closed).

    Returns ``(ok_entries, blocked)`` where ``ok_entries`` is a list of
    ``(citekey, csl_json)`` in document order (one per unique citekey that
    resolves to an approved, live reference), and ``blocked`` lists the
    ``{citekey, reference_id, metadata_status, reason}`` dicts for every citekey
    that fails resolution. The two outputs are mutually exclusive per citekey.

    This is the shared fail-closed resolution used by both
    :func:`build_bibliography` (the rendered artifact) and :func:`compose_export`
    (the inline-citation transform), so the export reflects exactly the same
    contract as the bibliography. Resolution goes via the LEDGER (canonical),
    then the Reference DB row (workspace-scoped). The returned CSL prefers the
    approved canonical ``csl_json`` and falls back to ``csl_json_extracted``
    (where ``verified_external`` and waived references hold their metadata).
    """
    ok_entries: list[tuple[str, dict]] = []
    blocked: list[dict] = []

    for citekey in _ordered_unique_citekeys(proposal_text):
        rows = find_by_citekey(ledger, citekey)
        if not rows:
            blocked.append(
                {
                    "citekey": citekey,
                    "reference_id": None,
                    "metadata_status": None,
                    "reason": "unresolved_citekey",
                }
            )
            continue

        # find_by_citekey returns every evidence row for this citekey (repeated
        # cites). They must all bind to one reference_id; a disagreement is
        # ledger drift, so fail closed rather than silently trusting the first.
        ref_ids = {r.get("reference_id") for r in rows if isinstance(r, dict)}
        ref_ids.discard(None)
        ref_id_raw = next(iter(ref_ids)) if len(ref_ids) == 1 else None
        try:
            ref_uuid = uuid.UUID(str(ref_id_raw))
        except (ValueError, TypeError):
            blocked.append(
                {
                    "citekey": citekey,
                    "reference_id": None,
                    "metadata_status": None,
                    "reason": "unresolved_citekey",
                }
            )
            continue

        ref = session.get(Reference, ref_uuid)
        # A row in another workspace is reported as "missing" too: from this
        # workspace's standpoint the reference is absent, and the id came from
        # this workspace's own ledger, so no cross-workspace detail is leaked.
        if ref is None or getattr(ref, "workspace_id", None) != workspace_id:
            blocked.append(
                {
                    "citekey": citekey,
                    "reference_id": str(ref_uuid),
                    "metadata_status": None,
                    "reason": "reference_missing",
                }
            )
            continue

        if getattr(ref, "tombstoned_at", None) is not None:
            blocked.append(
                {
                    "citekey": citekey,
                    "reference_id": str(ref_uuid),
                    "metadata_status": getattr(ref, "metadata_status", None),
                    "reason": "reference_tombstoned",
                }
            )
            continue

        # Fail-closed gate: render only from verified canonical metadata.
        if getattr(ref, "metadata_status", None) not in _APPROVED_METADATA and not getattr(
            ref, "metadata_waiver", False
        ):
            blocked.append(
                {
                    "citekey": citekey,
                    "reference_id": str(ref_uuid),
                    "metadata_status": getattr(ref, "metadata_status", None),
                    "reason": "metadata_unapproved",
                }
            )
            continue

        # Prefer the human-approved canonical csl_json; fall back to the
        # extracted/verified metadata. A "verified_external" reference (fetched
        # by DOI/PMID/arXiv) stores its AUTHORITATIVE provider metadata in
        # csl_json_extracted with csl_json still NULL, and a waived reference may
        # likewise only have extracted metadata — without this fallback both
        # would render as "Untitled". Mirrors insert_citation's citekey source
        # (tools/citations.py). Never fabricates data: a reference with neither
        # column still renders "Untitled".
        csl = ref.csl_json or ref.csl_json_extracted or {}
        ok_entries.append((citekey, csl))

    return ok_entries, blocked


def build_bibliography(
    *,
    proposal_text: str,
    ledger: dict,
    session,
    workspace_id,
    style: str,
    repo_dir,
    commit: bool = True,
) -> dict:
    """Render the cited references into a formatted bibliography (fail-closed).

    Resolves each marker citekey via the LEDGER (canonical), then the Reference
    DB row (workspace-scoped). The fail-closed gate refuses to render if ANY
    cited reference's ``metadata_status`` is not approved AND has no waiver — no
    partial write. Renders from ``csl_json`` (approved canonical), falling back
    to ``csl_json_extracted`` for ``verified_external``/waived references whose
    canonical column is still NULL. Only marker-present citekeys are rendered.

    ``commit`` optionally writes the result to ``bibliography.md`` (a separate git
    artifact, NEVER merged into proposal.md). NOTE: no production caller currently
    passes ``commit=True`` — the bibliography is a DERIVED artifact, so the MCP
    tool and the composed-export route both render with ``commit=False`` and
    consume the returned text (a persisted snapshot would only drift). The write
    path is retained + unit-tested for optional reuse.

    Returns ``{"bibliography_md", "citekey_count", "commit_sha"}`` — ``commit_sha``
    is the new commit's SHA when ``commit`` is True, else None. (The MCP tool
    wrapper strips ``commit_sha`` from its own result since it never persists.)
    Raises :class:`BuildBibliographyBlocked` if any reference is blocked.
    """
    ok_entries, blocked = _resolve_cited_entries(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace_id,
    )

    if blocked:
        # Fail-closed: NO render, NO partial write.
        raise BuildBibliographyBlocked(blocked)

    if not ok_entries:
        # No citations at all: nothing to render and nothing to commit. Returning
        # without writing avoids creating/truncating bibliography.md to an empty
        # file (render_bibliography returns "" precisely so callers can skip).
        return {"bibliography_md": "", "citekey_count": 0, "commit_sha": None}

    bib_text = render_bibliography(ok_entries, style)

    sha = None
    if commit:
        sha = write_and_commit_files(
            repo_dir,
            {BIBLIOGRAPHY_FILENAME: bib_text},
            "cite: build bibliography",
        )

    return {
        "bibliography_md": bib_text,
        "citekey_count": len(ok_entries),
        "commit_sha": sha,
    }


# ---------------------------------------------------------------------------
# Composed export (inline citations + appended bibliography)
# ---------------------------------------------------------------------------


def _author_count(authors: object) -> int:
    """Count authors that have a renderable name (family or literal)."""
    if not isinstance(authors, list):
        return 0
    n = 0
    for a in authors:
        if not isinstance(a, dict):
            continue
        literal = a.get("literal")
        family = a.get("family")
        if (isinstance(literal, str) and literal.strip()) or (
            isinstance(family, str) and family.strip()
        ):
            n += 1
    return n


def _inline_author_year(csl: dict, citekey: str) -> str:
    """Render an in-text author-year citation, e.g. ``(Smith 2021a)`` or
    ``(Smith et al. 2021)``. Degrades gracefully: ``(year)`` with no author,
    ``(citekey)`` with neither — never an empty marker and never a ``[@`` leak.
    """
    if not isinstance(csl, dict):
        csl = {}

    name = ""
    authors = csl.get("author")
    if isinstance(authors, list):
        first = next((a for a in authors if isinstance(a, dict)), None)
        if first is not None:
            literal = first.get("literal")
            family = first.get("family")
            if isinstance(literal, str) and literal.strip():
                name = _clean(literal)
            elif isinstance(family, str) and family.strip():
                name = _clean(family)
        if name and _author_count(authors) > 1:
            name = f"{name} et al."

    year = _year_of(csl)
    suffix = _suffix_from_citekey(citekey, year)
    if name and year:
        return f"({name} {year}{suffix})"
    if name:
        return f"({name})"
    if year:
        return f"({year}{suffix})"
    return f"({citekey})"


#: Matches an opening or closing fenced code block delimiter (``` or ~~~).
_FENCE_LINE_RE = re.compile(r"^\s*(?:```|~~~)")

#: Matches an inline-code span (single backtick — we strip their content to
#: avoid false bare-marker hits in ``code``, then restore the backtick delimiters).
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _prose_only(text: str) -> str:
    """Return *text* with fenced code blocks and inline-code spans blanked out.

    Used by the residual bare-marker scrub in :func:`compose_export` so that a
    literal ``[@token]`` inside a code context is not mistaken for an unresolved
    citation marker.  The blanked regions are replaced with the same number of
    space characters so that all character offsets (and therefore the length of the
    returned string) are preserved — making it safe to feed the result to a regex
    without invalidating any position information.

    Fence tracking mirrors ``iter_proposal_sentences`` in ``citations/claims.py``:
    both the opening and closing ``` / ~~~ fence lines are excluded.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.rstrip("\n")
        if _FENCE_LINE_RE.match(stripped):
            in_fence = not in_fence
            # Fence delimiter lines themselves are blanked (they contain no prose).
            out.append(" " * len(line))
            continue
        if in_fence:
            out.append(" " * len(line))
            continue
        # Prose line: blank inline-code spans so their content is invisible to the
        # bare-marker regex while keeping the surrounding text intact.
        blanked = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group()), line)
        out.append(blanked)
    return "".join(out)


def compose_export(
    *,
    proposal_text: str,
    ledger: dict,
    session,
    workspace_id,
    style: str,
    bibliography_text: str | None = None,
) -> str:
    """Transform inline markers into rendered citations and append the bibliography.

    Each ``[@citekey]<!-- ev:id -->`` marker is replaced (the WHOLE span, so the
    ``ev`` comment never leaks) with the in-text citation for ``style``:
      - "vancouver": ``[N]`` where N is the citekey's document-order number;
      - "author-year": ``(Family Year[suffix])``.
    Repeated cites of one citekey collapse to the same number / form.

    Fail-closed, identically to :func:`build_bibliography`: if ANY cited reference
    is unresolved/missing/tombstoned/unapproved, raises
    :class:`BuildBibliographyBlocked` and produces NOTHING — callers must not
    serve a half-transformed document. ``bibliography_text`` (typically from a
    prior ``build_bibliography(commit=False)``) is appended when provided.
    """
    ok_entries, blocked = _resolve_cited_entries(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace_id,
    )
    if blocked:
        raise BuildBibliographyBlocked(blocked)

    csl_by_key = {ck: csl for ck, csl in ok_entries}
    number_by_key = {ck: i for i, (ck, _csl) in enumerate(ok_entries, start=1)}

    def _replace(m) -> str:
        citekey = m.group("citekey")
        if style == "author-year":
            return _inline_author_year(csl_by_key.get(citekey, {}), citekey)
        number = number_by_key.get(citekey)
        # Every marker citekey is in the map (none blocked → none unresolved);
        # the empty fallback only guards a theoretical miss and never leaks "[@".
        return f"[{number}]" if number is not None else ""

    composed = MARKER_RE.sub(_replace, proposal_text or "")

    # Defense-in-depth: MARKER_RE only matches WELL-FORMED markers (with the ev
    # comment). A hand-edited / malformed proposal could carry a bare "[@key]"
    # that bypassed both resolution and substitution and would leak verbatim. The
    # "[@key]" token is reserved for citations here, so any residual one is an
    # unresolved citation — fail closed rather than emit a leaked marker.
    #
    # Code-fence/inline-code awareness: a bare "[@key]" inside a fenced code
    # block (``` / ~~~) or an inline-code span (`) is literal text and must NOT
    # block export.  We scan only the PROSE regions (mirroring the fence-tracking
    # logic from iter_proposal_sentences in citations/claims.py).
    residual = _BARE_MARKER_RE.findall(_prose_only(composed))
    if residual:
        raise BuildBibliographyBlocked(
            [
                {
                    "citekey": ck,
                    "reference_id": None,
                    "metadata_status": None,
                    "reason": "unresolved_marker",
                }
                for ck in dict.fromkeys(residual)  # unique, document order
            ]
        )

    if bibliography_text:
        composed = composed.rstrip("\n") + "\n\n" + bibliography_text
    return composed
