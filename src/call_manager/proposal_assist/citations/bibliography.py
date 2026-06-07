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

_REFERENCES_HEADING = "# References\n\n"

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


def _one_author_vancouver(entry: dict) -> str | None:
    """Render a single author entry, Vancouver style ("Smith JM")."""
    literal = entry.get("literal")
    if isinstance(literal, str) and literal.strip():
        return literal.strip()
    family = entry.get("family")
    if not (isinstance(family, str) and family.strip()):
        return None
    family = family.strip()
    given = entry.get("given")
    inits = _initials(given, dotted=False) if isinstance(given, str) else ""
    return f"{family} {inits}" if inits else family


def _one_author_year(entry: dict) -> str | None:
    """Render a single author entry, author-year style ("Smith, J. M.")."""
    literal = entry.get("literal")
    if isinstance(literal, str) and literal.strip():
        return literal.strip()
    family = entry.get("family")
    if not (isinstance(family, str) and family.strip()):
        return None
    family = family.strip()
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
    year immediately followed by exactly one lowercase letter.
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
    return title.strip() if isinstance(title, str) and title.strip() else ""


def _container_of(csl: dict) -> str:
    ct = csl.get("container-title")
    return ct.strip() if isinstance(ct, str) and ct.strip() else ""


def _str_field(csl: dict, key: str) -> str:
    val = csl.get(key)
    if val is None:
        return ""
    s = str(val).strip()
    return s


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

    body = ". ".join(pieces)
    out = f"[{number}] {body}."

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

    out = ". ".join(pieces) + "."

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
        lines = []
        for item in entries:
            citekey, csl = _unpack(item)
            lines.append(format_author_year(csl, citekey))
        lines.sort(key=lambda s: s.casefold())
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
