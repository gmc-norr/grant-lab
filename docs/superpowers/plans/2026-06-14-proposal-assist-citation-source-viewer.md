# Citation Source Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every citation in the rendered proposal clickable, opening a responsive reference drawer that shows the cited reference's extracted text scrolled to — and highlighting — the exact verbatim evidence passage the claim came from.

**Architecture:** Additive on the current FastAPI + Jinja2 + HTMX stack. A new pure module `citations/reader.py` builds a proposal-wide citation numbering map, transforms `[@citekey]<!-- ev:id -->` markers into clickable links (sentinel pre-markdown → bleach → text-node-aware injection so agent HTML can't forge links), and locates evidence spans in extracted text. A new read-only route returns an HTML fragment loaded into a drawer; delegated JS in `app.js` opens/scrolls it (modal semantics only at the narrow breakpoint).

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX, `markdown` + `bleach` (render), `lxml.html` (text-node-aware injection — already an installed dep), pytest + httpx TestClient, `node --test` for JS.

**Spec:** `docs/superpowers/specs/2026-06-14-proposal-assist-citation-source-viewer-design.md` (approved, twice Codex-revised).

**Reused building blocks (do NOT reinvent):**
- `proposal_assist/citations/markers.py` → `find_markers(text) -> [{citekey, ev_id, start, end}]` (document order).
- `proposal_assist/citations/quotes.py` → `find_quote_offsets(text, quote) -> (start,end)` raising `QuoteNotFound`/`QuoteAmbiguous`; `normalize_for_quote_match(s)`.
- `proposal_assist/citations/ledger.py` → `load_ledger(text|None) -> {version, evidence:[...]}` (raises `ValueError` on corrupt); `find_by_id(ledger, ev_id)`.
- `proposal_assist/web/references.py` → `_require_owner_or_viewer(workspace_id, user, session)`, `_session_dep`.
- `db/models.py` → `Reference` (`title`, `csl_json`, `csl_json_extracted`, `citekey`, `text_status`, `tombstoned_at`), `ReferenceChunk` (`reference_id`, `chunk_index`, `section_path`, `text`, `workspace_id`).
- `references/storage.py` → `ReferenceStorage(root).text_path_for(workspace_id=…, reference_id=…)` → `<root>/<wid>/references/<rid>/text.txt`.
- Repo path convention: `Path(settings.workspaces_dir) / str(workspace_id) / "repo"`, files `proposal.md` (`PROPOSAL_FILENAME`) and `citations.json` (`CITATIONS_FILENAME`).

---

## File Structure

- **Create** `src/call_manager/proposal_assist/citations/reader.py` — pure helpers: numbering map, per-reference evidence ordering, sentinel transform + injection, render compose, evidence locator. No I/O.
- **Modify** `src/call_manager/proposal_assist/web/workspaces.py` — build the numbering map once in `_artifact_sections_and_rendered` and render full proposal + each section through it.
- **Modify** `src/call_manager/proposal_assist/web/sections.py` — thread the same proposal-wide map into `_section_ctx` for the view/edit/save fragments.
- **Modify** `src/call_manager/proposal_assist/web/citations.py` — new `GET …/citations/{evidence_id}/reader` route + context assembly + status-code/edge policy.
- **Create** `src/call_manager/web/templates/proposal_assist/partials/citation_reader.html` — drawer fragment (header + highlighted body + hints; `role="dialog"`).
- **Modify** `src/call_manager/web/templates/proposal_assist/workspace_main.html` — `#reference-drawer` container.
- **Modify** `src/call_manager/web/static/app.js` — delegated `a.cite` handling, drawer open/close/scroll, focus + modal (narrow-only).
- **Modify** `src/call_manager/web/static/app.css` — responsive drawer + `mark` highlight.
- **Create tests:** `tests/proposal_assist/test_citation_reader_transform.py`, `tests/proposal_assist/test_citation_locator.py`, `tests/proposal_assist/test_web_citation_reader_route.py`, `tests/js/citation_drawer.test.js`.

---

## Task 1: Numbering map + per-reference evidence ordering

**Files:**
- Create: `src/call_manager/proposal_assist/citations/reader.py`
- Test: `tests/proposal_assist/test_citation_reader_transform.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_citation_reader_transform.py
from call_manager.proposal_assist.citations.reader import (
    build_citation_numbering,
    evidence_order_for_reference,
)

LEDGER = {
    "version": 2,
    "evidence": [
        {"id": "ev-aaaaaa", "citekey": "wang2016", "reference_id": "R1"},
        {"id": "ev-bbbbbb", "citekey": "liu2017", "reference_id": "R2"},
        {"id": "ev-cccccc", "citekey": "wang2016", "reference_id": "R1"},
    ],
}
# wang2016 cited twice (ev-aaaaaa, ev-cccccc) + liu2017 once, in doc order.
PROPOSAL = (
    "First claim [@wang2016]<!-- ev:ev-aaaaaa -->. "
    "Second [@liu2017]<!-- ev:ev-bbbbbb -->. "
    "Third reuses [@wang2016]<!-- ev:ev-cccccc -->."
)


def test_numbering_is_per_reference_document_order():
    nums = build_citation_numbering(PROPOSAL, LEDGER)
    # wang2016 is first → [1]; both of its ev ids share the number.
    assert nums == {"ev-aaaaaa": 1, "ev-bbbbbb": 2, "ev-cccccc": 1}


def test_numbering_excludes_markers_absent_from_ledger():
    text = PROPOSAL + " Bogus [@ghost]<!-- ev:ev-ffffff -->."
    nums = build_citation_numbering(text, LEDGER)
    assert "ev-ffffff" not in nums  # no ledger row → inert, unnumbered


def test_numbering_empty_on_empty_ledger():
    assert build_citation_numbering(PROPOSAL, {"version": 2, "evidence": []}) == {}


def test_evidence_order_for_reference_is_document_order():
    order = evidence_order_for_reference(PROPOSAL, LEDGER, "R1")
    assert order == ["ev-aaaaaa", "ev-cccccc"]
    assert evidence_order_for_reference(PROPOSAL, LEDGER, "R2") == ["ev-bbbbbb"]


def test_build_citation_stances_maps_ev_to_stance():
    from call_manager.proposal_assist.citations.reader import build_citation_stances

    led = {
        "version": 2,
        "evidence": [
            {"id": "ev-aaaaaa", "citekey": "w", "reference_id": "R1", "stance": "supports"},
            {"id": "ev-bbbbbb", "citekey": "l", "reference_id": "R2"},  # no stance → omitted
        ],
    }
    assert build_citation_stances(led) == {"ev-aaaaaa": "supports"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q`
Expected: FAIL — `ModuleNotFoundError: …citations.reader` / `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/call_manager/proposal_assist/citations/reader.py
"""Pure helpers for the citation source viewer (Tier 1, extracted text).

Numbering, marker→link transform (sentinel + post-sanitization injection),
and evidence-span location. No git/DB/file I/O — callers own those.
"""

from __future__ import annotations

from call_manager.proposal_assist.citations.ledger import find_by_id
from call_manager.proposal_assist.citations.markers import find_markers


def build_citation_numbering(proposal_text: str, ledger: dict) -> dict[str, int]:
    """Map each live, ledger-backed evidence id to its citation number.

    Numbering is per citekey (≡ per reference) in first-appearance order across
    the WHOLE proposal; repeat cites of a citekey share the number. Markers with
    no matching ledger row are excluded (they render inert)."""
    number_by_citekey: dict[str, int] = {}
    number_by_ev: dict[str, int] = {}
    for m in find_markers(proposal_text):
        if find_by_id(ledger, m["ev_id"]) is None:
            continue  # unknown ev id → inert, unnumbered
        citekey = m["citekey"]
        if citekey not in number_by_citekey:
            number_by_citekey[citekey] = len(number_by_citekey) + 1
        number_by_ev[m["ev_id"]] = number_by_citekey[citekey]
    return number_by_ev


def evidence_order_for_reference(
    proposal_text: str, ledger: dict, reference_id: str
) -> list[str]:
    """Ordered (document order) list of live evidence ids whose ledger row cites
    ``reference_id``. Drives prev/next + 'i of N'."""
    out: list[str] = []
    for m in find_markers(proposal_text):
        row = find_by_id(ledger, m["ev_id"])
        if row is not None and str(row.get("reference_id")) == str(reference_id):
            out.append(m["ev_id"])
    return out


def build_citation_stances(ledger: dict) -> dict[str, str]:
    """Map each evidence id to its stance (e.g. 'supports'/'contradicts'), used
    to tint the inline citation marker. Rows without a stance are omitted."""
    return {
        e["id"]: e["stance"]
        for e in ledger.get("evidence", [])
        if e.get("id") and e.get("stance")
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/reader.py tests/proposal_assist/test_citation_reader_transform.py
git commit -m "feat(proposal-assist): citation numbering map + per-reference evidence order"
```

---

## Task 2: Sentinel transform + text-node-aware link injection + render compose

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/reader.py`
- Test: `tests/proposal_assist/test_citation_reader_transform.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/proposal_assist/test_citation_reader_transform.py
import re

from call_manager.proposal_assist.citations.reader import render_proposal_with_citations

WID = "11111111-1111-1111-1111-111111111111"


def _render(text):
    nums = build_citation_numbering(text, LEDGER)
    return render_proposal_with_citations(text, number_by_ev=nums, workspace_id=WID)


def test_marker_becomes_numbered_cite_link():
    html = _render("Claim [@wang2016]<!-- ev:ev-aaaaaa -->.")
    assert (
        '<a class="cite" '
        f'href="/workspaces/{WID}/citations/ev-aaaaaa/reader">[1]</a>'
    ) in html
    assert "[@wang2016]" not in html  # the raw marker is gone


def test_unknown_marker_stays_inert_text():
    html = _render("Bogus [@ghost]<!-- ev:ev-ffffff -->.")
    assert "cite" not in html  # no link injected
    assert "[@ghost]" in html  # visible citekey remains as plain text
    assert "ev-ffffff" not in html  # ev-comment stripped


def test_marker_inside_code_span_is_not_linked():
    # A marker the agent placed inside an inline code span must stay literal.
    html = _render("Use `[@wang2016]<!-- ev:ev-aaaaaa -->` literally.")
    assert "<code>" in html
    assert 'class="cite"' not in html
    # the visible token survives inside the code element
    assert "[@wang2016]" in html


def test_forged_anchor_in_agent_html_is_neutralized():
    # Agent-authored raw HTML cannot create a working cite link: bleach strips
    # the class (not allowlisted) so the drawer JS never matches it.
    html = _render(
        'Evil <a class="cite" href="/workspaces/x/citations/ev-zzz/reader">x</a> end.'
    )
    assert 'class="cite"' not in html  # only server-injected links carry it


def test_cite_link_carries_stance_class_when_provided():
    text = "Claim [@wang2016]<!-- ev:ev-aaaaaa -->."
    nums = build_citation_numbering(text, LEDGER)
    html = render_proposal_with_citations(
        text, number_by_ev=nums, workspace_id=WID,
        stance_by_ev={"ev-aaaaaa": "supports"},
    )
    assert 'class="cite cite--supports"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_proposal_with_citations'`.

- [ ] **Step 3: Write minimal implementation**

Add to `reader.py` (imports at top; functions below the Task 1 helpers):

```python
import html as _html

import bleach
import lxml.html
import markdown

from call_manager.proposal_assist.citations.markers import MARKER_RE

# Private-use sentinel wrappers — never occur in real prose; any pre-existing
# ones are stripped before processing so only OUR markers become links.
_SENT_OPEN = ""
_SENT_CLOSE = ""
_SENTINEL_RE = re.compile(_SENT_OPEN + r"(?P<n>\d+):(?P<ev>ev-[0-9a-f]{6,})" + _SENT_CLOSE)

# Same allowlist as web.workspaces._render_proposal_markdown — class is NOT
# allowed on <a>, so an agent cannot forge an a.cite (the JS hook).
_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em",
    "code", "pre", "ul", "ol", "li", "blockquote", "a", "table", "thead",
    "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRS = {"a": ["href", "title"]}

# Tags whose text content must NOT receive injected links.
_NO_INJECT_TAGS = {"code", "pre"}


def _to_sentinels(raw_text: str, number_by_ev: dict[str, int]) -> str:
    """Replace each numbered marker with a plain-text sentinel before markdown.

    Markers absent from ``number_by_ev`` are left untouched (their visible
    ``[@citekey]`` renders as inert text and the ev-comment is stripped by
    bleach), matching today's behaviour."""
    cleaned = raw_text.replace(_SENT_OPEN, "").replace(_SENT_CLOSE, "")

    def _sub(m):
        ev = m.group("ev_id")
        n = number_by_ev.get(ev)
        if n is None:
            return m.group(0)
        return f"{_SENT_OPEN}{n}:{ev}{_SENT_CLOSE}"

    return MARKER_RE.sub(_sub, cleaned)


def _inject_links(sanitized_html: str, workspace_id, stance_by_ev=None) -> str:
    """Replace sentinels with trusted cite links, but ONLY inside eligible text
    nodes — never within attributes or <code>/<pre>. lxml gives a real tree, so
    attributes are untouched automatically; we additionally skip code/pre."""
    if _SENT_OPEN not in sanitized_html:
        return sanitized_html
    # Wrap so a fragment with multiple top-level nodes parses cleanly.
    root = lxml.html.fragment_fromstring(sanitized_html, create_parent="div")

    def _link_html(n: str, ev: str) -> str:
        href = f"/workspaces/{workspace_id}/citations/{ev}/reader"
        stance = (stance_by_ev or {}).get(ev)
        cls = f"cite cite--{stance}" if stance else "cite"
        return f'<a class="{cls}" href="{_html.escape(href)}">[{n}]</a>'

    def _expand(text: str) -> str | None:
        if text is None or _SENT_OPEN not in text:
            return None
        return _SENTINEL_RE.sub(
            lambda m: _link_html(m.group("n"), m.group("ev")), text
        )

    for el in list(root.iter()):
        in_code = el.tag in _NO_INJECT_TAGS or any(
            a.tag in _NO_INJECT_TAGS for a in el.iterancestors()
        )
        if in_code:
            continue
        # el.text — text before the first child; el.tail — text after this element.
        new_text = _expand(el.text)
        if new_text is not None:
            frag = lxml.html.fragment_fromstring(
                "<x>" + new_text + "</x>"
            )
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

    inner = (root.text or "") + "".join(
        lxml.html.tostring(c, encoding="unicode") for c in root
    )
    return inner


def render_proposal_with_citations(
    raw_text: str, *, number_by_ev: dict[str, int], workspace_id, stance_by_ev=None
) -> str:
    """markdown → bleach → inject cite links. With an empty map this is exactly
    the legacy render (markers inert). ``stance_by_ev`` (optional) tints each
    marker by stance via a ``cite--<stance>`` class."""
    with_sentinels = _to_sentinels(raw_text, number_by_ev)
    rendered = markdown.markdown(with_sentinels, extensions=["extra"])
    sanitized = bleach.clean(
        rendered, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True
    )
    return _inject_links(sanitized, workspace_id, stance_by_ev)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q`
Expected: PASS (8 passed). If `test_marker_inside_code_span_is_not_linked` fails, confirm `extensions=["extra"]` keeps the backticked span as `<code>` and that `_NO_INJECT_TAGS` skips it.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/reader.py tests/proposal_assist/test_citation_reader_transform.py
git commit -m "feat(proposal-assist): sentinel marker transform + text-node-aware cite-link injection"
```

---

## Task 3: Wire the transform into every render path (proposal-wide numbering)

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:170-191` (`_artifact_sections_and_rendered`)
- Modify: `src/call_manager/proposal_assist/web/sections.py:86-93` (`_section_ctx`) and its callers
- Test: `tests/proposal_assist/test_web_citation_render_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_web_citation_render_wiring.py
"""Numbering must be proposal-wide: the same reference keeps its number in the
full render AND in a single-section fragment, and a corrupt ledger never breaks
the proposal render."""
from call_manager.proposal_assist.citations import reader


def test_numbering_is_stable_per_section_render(monkeypatch, make_workspace_repo):
    # make_workspace_repo: fixture writing proposal.md + citations.json into a
    # temp repo and returning (settings, workspace_id). See conftest note below.
    settings, wid = make_workspace_repo(
        proposal=(
            "## A\nClaim [@wang2016]<!-- ev:ev-aaaaaa -->.\n\n"
            "## B\nLater [@liu2017]<!-- ev:ev-bbbbbb --> and "
            "again [@wang2016]<!-- ev:ev-cccccc -->.\n"
        ),
        ledger={
            "version": 2,
            "evidence": [
                {"id": "ev-aaaaaa", "citekey": "wang2016", "reference_id": "R1"},
                {"id": "ev-bbbbbb", "citekey": "liu2017", "reference_id": "R2"},
                {"id": "ev-cccccc", "citekey": "wang2016", "reference_id": "R1"},
            ],
        },
    )
    from call_manager.proposal_assist.web.workspaces import _artifact_sections_and_rendered

    raw, rendered, sections = _artifact_sections_and_rendered(settings, wid)
    # Section "B" (rendered independently) must still show wang2016 as [1],
    # not restart at [1] for liu2017.
    section_b = next(s for s in sections if s["heading"] == "B")
    assert "/citations/ev-cccccc/reader\">[1]</a>" in section_b["rendered"]
    assert "/citations/ev-bbbbbb/reader\">[2]</a>" in section_b["rendered"]


def test_corrupt_ledger_renders_proposal_with_inert_markers(make_workspace_repo):
    settings, wid = make_workspace_repo(
        proposal="Claim [@wang2016]<!-- ev:ev-aaaaaa -->.",
        ledger_raw="{ this is not json",
    )
    from call_manager.proposal_assist.web.workspaces import _artifact_sections_and_rendered

    raw, rendered, sections = _artifact_sections_and_rendered(settings, wid)
    assert "Claim" in rendered          # proposal still renders
    assert 'class="cite"' not in rendered  # markers inert
    assert "[@wang2016]" in rendered     # visible token survives
```

Add the fixture to `tests/proposal_assist/conftest.py`:

```python
# tests/proposal_assist/conftest.py  (append)
import json
import uuid
from pathlib import Path
import pytest


@pytest.fixture
def make_workspace_repo(tmp_path):
    """Write proposal.md + citations.json under a temp workspaces_dir and return
    (settings_like, workspace_id). `settings_like` only needs `.workspaces_dir`."""
    class _S:
        def __init__(self, d): self.workspaces_dir = str(d)

    def _make(*, proposal="", ledger=None, ledger_raw=None):
        wid = uuid.uuid4()
        repo = Path(tmp_path) / str(wid) / "repo"
        repo.mkdir(parents=True)
        (repo / "proposal.md").write_text(proposal)
        if ledger_raw is not None:
            (repo / "citations.json").write_text(ledger_raw)
        elif ledger is not None:
            (repo / "citations.json").write_text(json.dumps(ledger))
        return _S(tmp_path), wid

    return _make
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_citation_render_wiring.py -q`
Expected: FAIL — numbers restart per section (`ev-cccccc` shows `[2]` or no link), and/or the corrupt-ledger case raises.

- [ ] **Step 3: Write minimal implementation**

In `web/workspaces.py`, add a helper that loads the ledger + builds the map (fail-safe), then thread it through `_artifact_sections_and_rendered`:

```python
# web/workspaces.py — imports
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME


def _citation_render_maps_for(
    settings, workspace_id, proposal_text: str
) -> tuple[dict[str, int], dict[str, str]]:
    """Proposal-wide (ev→number, ev→stance) maps. A missing/corrupt ledger
    yields EMPTY maps so markers render inert and the proposal still renders
    (never raises)."""
    repo_dir = Path(settings.workspaces_dir) / str(workspace_id) / "repo"
    citations_path = repo_dir / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except (ValueError, OSError):
        return {}, {}
    return (
        citation_reader.build_citation_numbering(proposal_text, ledger),
        citation_reader.build_citation_stances(ledger),
    )
```

Then change the body of `_artifact_sections_and_rendered` (lines 170-191):

```python
def _artifact_sections_and_rendered(settings, workspace_id: uuid.UUID):
    repo_dir = Path(settings.workspaces_dir) / str(workspace_id) / "repo"
    proposal_path = repo_dir / "proposal.md"
    if proposal_path.exists():
        raw = proposal_path.read_text()
        number_by_ev, stance_by_ev = _citation_render_maps_for(settings, workspace_id, raw)
        rendered = citation_reader.render_proposal_with_citations(
            raw, number_by_ev=number_by_ev, workspace_id=workspace_id, stance_by_ev=stance_by_ev
        )
    else:
        raw, rendered, number_by_ev, stance_by_ev = "", "<em>(no proposal.md yet)</em>", {}, {}
    sections = [
        {
            "index": s.index,
            "heading": s.heading,
            "sha": s.sha,
            "rendered": citation_reader.render_proposal_with_citations(
                s.text, number_by_ev=number_by_ev, workspace_id=workspace_id,
                stance_by_ev=stance_by_ev,
            ),
        }
        for s in split_sections(raw)
        if s.text
    ]
    return raw, rendered, sections
```

Now `web/sections.py` — `_section_ctx` must receive the shared map. Change its signature and update callers (the section view/edit/save routes build the map from the FULL proposal.md they already read via `_read_sections`):

```python
# web/sections.py
from call_manager.proposal_assist.web.workspaces import (
    _citation_render_maps_for,
    _render_proposal_markdown,  # keep existing import
)
from call_manager.proposal_assist.citations import reader as citation_reader


def _section_ctx(section, number_by_ev, stance_by_ev, workspace_id) -> dict:
    return {
        "index": section.index,
        "heading": section.heading,
        "sha": section.sha,
        "rendered": citation_reader.render_proposal_with_citations(
            section.text, number_by_ev=number_by_ev, workspace_id=workspace_id,
            stance_by_ev=stance_by_ev,
        ),
    }
```

In every `_section_ctx(...)` call site (`_view_fragment`, `get_section_edit`, `post_section_save`’s `artifact_sections = [...]`), pass the maps built from the full proposal text already in scope:

```python
# wherever the route has the full proposal text `raw` (from _read_sections) and
# `settings`/`workspace_id`:
number_by_ev, stance_by_ev = _citation_render_maps_for(settings, workspace_id, raw)
# ... _section_ctx(section, number_by_ev, stance_by_ev, workspace_id) ...
```

(If a call site lacks `settings`, read it from `request.app.state.settings`. `_read_sections` already returns the full `raw` text — reuse it; do not re-read the file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_citation_render_wiring.py tests/proposal_assist/test_web_sections.py -q`
Expected: PASS — proposal-wide numbering holds across sections; corrupt ledger renders inert; existing section tests still green.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/proposal_assist/web/sections.py tests/proposal_assist/test_web_citation_render_wiring.py tests/proposal_assist/conftest.py
git commit -m "feat(proposal-assist): render cite links via one proposal-wide numbering map (all render paths)"
```

---

## Task 4: Evidence locator

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/reader.py`
- Test: `tests/proposal_assist/test_citation_locator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_citation_locator.py
from call_manager.proposal_assist.citations.reader import locate_evidence

FULL = (
    "Intro paragraph about methods.\n\n"
    "The system achieved an AUC of 0.925 for whole-slide image classification.\n\n"
    "Later the AUC of 0.925 is mentioned again in a different sentence.\n"
)
CHUNK = "The system achieved an AUC of 0.925 for whole-slide image classification."
QUOTE = "AUC of 0.925 for whole-slide\nimage classification"  # newline like a PDF


def _span(full, se):
    s, e = se
    return full[s:e]


def test_locates_via_chunk_anchor_even_when_quote_is_duplicated():
    se = locate_evidence(FULL, CHUNK, QUOTE)
    assert se is not None
    # must pick the occurrence inside the cited chunk, not the later duplicate
    assert _span(FULL, se).replace("\n", " ") == "AUC of 0.925 for whole-slide image classification"
    assert se[0] < FULL.index("Later")  # first occurrence, anchored by chunk


def test_missing_chunk_falls_back_to_unique_quote_search():
    uniq = "AUC of 0.925 for whole-slide image classification"
    full = "Only one mention: " + uniq + " here."
    se = locate_evidence(full, None, uniq)
    assert se is not None and _span(full, se) == uniq


def test_ambiguous_quote_without_usable_chunk_returns_none():
    assert locate_evidence(FULL, None, "AUC of 0.925") is None  # appears twice


def test_stale_offsets_do_not_matter_quote_is_relocated():
    # The locator recomputes from text, so it never trusts stored offsets.
    se = locate_evidence(FULL, CHUNK, QUOTE)
    assert se is not None


def test_quote_absent_returns_none():
    assert locate_evidence(FULL, CHUNK, "not present anywhere") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_citation_locator.py -q`
Expected: FAIL — `ImportError: cannot import name 'locate_evidence'`.

- [ ] **Step 3: Write minimal implementation**

Add to `reader.py`:

```python
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
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

    Strategy (recomputes live — never trusts stored offsets):
      1. If the cited chunk is available, find the quote uniquely WITHIN the
         chunk, place the chunk UNIQUELY in full_text, map the offsets, and
         accept only if in-bounds and the span normalizes to the quote.
      2. Otherwise / on any failure: find the quote uniquely in full_text.
      3. Ambiguous or not found → None (caller falls back to chunk/notice)."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_citation_locator.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/reader.py tests/proposal_assist/test_citation_locator.py
git commit -m "feat(proposal-assist): chunk-anchored evidence locator (validated, stale-offset-proof)"
```

---

## Task 5: Reader route + drawer fragment template

**Files:**
- Modify: `src/call_manager/proposal_assist/web/citations.py`
- Create: `src/call_manager/web/templates/proposal_assist/partials/citation_reader.html`
- Test: `tests/proposal_assist/test_web_citation_reader_route.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_web_citation_reader_route.py
"""Reader route returns a drawer FRAGMENT. 200 for every expected state that
should render in the drawer; 404 only for an unknown evidence id."""
import json, uuid
from pathlib import Path


def _seed(client_app, *, proposal, ledger, ref, chunks, text_txt):
    """Helper that writes repo files + inserts Reference/ReferenceChunk rows.
    Implemented against the suite's existing workspace/reference fixtures
    (make_workspace, make_reference, make_chunk); see test_web_references.py."""
    ...  # use the established fixtures; returns (workspace_id, evidence_id)


def test_reader_returns_fragment_with_highlight(reader_env):
    client, wid, ev = reader_env(locatable=True)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    body = r.text
    assert 'id="ev-target"' in body          # the highlight anchor
    assert "<mark" in body
    assert 'role="dialog"' in body
    assert "SUPPORTS" in body.upper()         # stance chip


def test_unknown_evidence_id_is_404(reader_env):
    client, wid, _ = reader_env(locatable=True)
    r = client.get(f"/workspaces/{wid}/citations/ev-deadbeef99/reader")
    assert r.status_code == 404


def test_no_text_status_done_shows_notice_200(reader_env):
    client, wid, ev = reader_env(text_status="pending")
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "not available" in r.text.lower()


def test_tombstoned_reference_renders_with_hint_200(reader_env):
    client, wid, ev = reader_env(tombstoned=True)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "removed from the library" in r.text.lower()


def test_unlocatable_quote_falls_back_to_chunk_200(reader_env):
    client, wid, ev = reader_env(locatable=False)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "couldn't pinpoint" in r.text.lower() or "cited section" in r.text.lower()


def test_viewer_can_read(reader_env):
    client, wid, ev = reader_env(as_viewer=True)
    assert client.get(f"/workspaces/{wid}/citations/{ev}/reader").status_code == 200


def test_reference_text_is_html_escaped(reader_env):
    client, wid, ev = reader_env(text_txt="danger <script>alert(1)</script> ok")
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert "<script>alert" not in r.text
    assert "&lt;script&gt;" in r.text
```

> **Implementation note for the test author:** build the `reader_env` fixture on the suite's existing proposal-assist fixtures (see `tests/proposal_assist/test_web_references.py` / `conftest.py` for `make_workspace`, reference + chunk insertion, and the authenticated `TestClient`). `reader_env(**opts)` seeds: a workspace with `proposal.md` containing one marker, a `citations.json` ledger row (stance `"supports"`), a `Reference` (honouring `text_status`/`tombstoned`), a `ReferenceChunk`, and `text.txt` — then returns `(client, workspace_id, evidence_id)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_citation_reader_route.py -q`
Expected: FAIL — route returns 404/template missing for all cases.

- [ ] **Step 3: Write minimal implementation**

Add the route to `web/citations.py` (reusing its existing imports + `_require_owner_or_viewer`, `_session_dep`, `load_ledger`, `CITATIONS_FILENAME`):

```python
# web/citations.py — additional imports
import html as _html
from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.ledger import find_by_id
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from fastapi import HTTPException

_MAX_READER_CHARS = 500_000  # oversized-source cap (spec §Error handling)


def _source_line(ref: Reference) -> str:
    csl = ref.csl_json or ref.csl_json_extracted or {}
    title = ref.title or csl.get("title") or ref.filename
    bits = [ref.citekey] if ref.citekey else []
    bits.append(str(title))
    return " · ".join(b for b in bits if b)


@router.get("/{workspace_id}/citations/{evidence_id}/reader", response_class=HTMLResponse)
def citation_reader_fragment(
    workspace_id: uuid.UUID,
    evidence_id: str,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(workspace_id) / "repo"

    # Ledger (corrupt → degraded fragment, never 500).
    citations_path = repo_dir / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError:
        return _reader_response(request, ws, ctx={"error": "Citations are unavailable."})

    ev = find_by_id(ledger, evidence_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such evidence id")

    ref = session.get(Reference, uuid.UUID(str(ev["reference_id"])))
    if ref is None or ref.workspace_id != workspace_id:
        # Cited refs are tombstoned (retained), so a truly absent row is rare.
        return _reader_response(request, ws, ctx={"error": "The cited reference is unavailable."})

    locator = ev.get("locator") or {}
    chunk = None
    ci = locator.get("chunk_index")
    if ci is not None:
        chunk = session.scalar(
            select(ReferenceChunk).where(
                ReferenceChunk.reference_id == ref.id,
                ReferenceChunk.chunk_index == ci,
                ReferenceChunk.workspace_id == workspace_id,
            )
        )

    storage = ReferenceStorage(request.app.state.settings.workspaces_dir)
    text_path = storage.text_path_for(workspace_id=workspace_id, reference_id=ref.id)
    full_text = text_path.read_text(encoding="utf-8") if text_path.exists() else None

    # prev/next across this reference's live cited passages.
    proposal_path = repo_dir / PROPOSAL_FILENAME
    proposal_text = proposal_path.read_text() if proposal_path.exists() else ""
    # The ReferenceChunk join above uses ref.id (a UUID); evidence ordering uses
    # the ledger's reference_id STRING (ev["reference_id"]) — they are the same
    # reference, different types, so don't cross them.
    order = citation_reader.evidence_order_for_reference(
        proposal_text, ledger, ev["reference_id"]
    )
    idx = order.index(evidence_id) + 1 if evidence_id in order else 0
    total = len(order)
    pos = order.index(evidence_id) if evidence_id in order else -1
    prev_ev = order[pos - 1] if pos > 0 else None
    next_ev = order[pos + 1] if 0 <= pos < total - 1 else None

    body_html, note = _reader_body(full_text, chunk, ev.get("evidence_quote") or "")

    claim = (ev.get("claim_context") or {}).get("text")
    ctx = {
        "source_line": _source_line(ref),
        "claim": claim,
        "stance": (ev.get("stance") or "").upper(),
        "confidence": (ev.get("confidence") or "").upper(),
        "index": idx, "total": total,
        "prev_ev": prev_ev, "next_ev": next_ev,
        "tombstoned": ref.tombstoned_at is not None,
        "no_text": full_text is None and chunk is None,
        "note": note,
        "body_html": body_html,
        "evidence_id": evidence_id,
    }
    return _reader_response(request, ws, ctx=ctx)


def _reader_body(full_text, chunk, quote):
    """Return (html, note). Missing-data matrix + oversized window. The returned
    HTML is fully escaped except for the single injected <mark>."""
    if full_text is None and chunk is None:
        return "", None
    if full_text is None:  # text.txt absent, chunk present → highlight the chunk
        return _highlight_whole(chunk.text), "Showing the cited section."
    span = citation_reader.locate_evidence(full_text, chunk.text if chunk else None, quote)
    if span is None:
        if chunk is not None:
            return _highlight_whole(chunk.text), (
                "Showing the cited section — couldn't pinpoint the exact sentence "
                "in the full document."
            )
        return _html.escape(_window(full_text, None)), "Couldn't pinpoint the cited sentence."
    s, e = span
    if len(full_text) > _MAX_READER_CHARS:
        return _highlight_window(full_text, s, e), (
            "Showing the cited section of a large document."
        )
    return (
        _html.escape(full_text[:s])
        + "<mark id=\"ev-target\">" + _html.escape(full_text[s:e]) + "</mark>"
        + _html.escape(full_text[e:])
    ), None


def _highlight_whole(text):
    return "<mark id=\"ev-target\">" + _html.escape(text) + "</mark>"


def _window(full_text, center, radius=4000):
    if center is None:
        return full_text[: 2 * radius]
    lo = max(0, center - radius)
    hi = min(len(full_text), center + radius)
    return full_text[lo:hi]


def _highlight_window(full_text, s, e, radius=4000):
    lo = max(0, s - radius)
    hi = min(len(full_text), e + radius)
    return (
        _html.escape(full_text[lo:s])
        + "<mark id=\"ev-target\">" + _html.escape(full_text[s:e]) + "</mark>"
        + _html.escape(full_text[e:hi])
    )


def _reader_response(request, ws, *, ctx):
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/citation_reader.html",
        {"workspace": ws, **{
            "source_line": None, "claim": None, "stance": None, "confidence": None,
            "index": 0, "total": 0, "prev_ev": None, "next_ev": None,
            "tombstoned": False, "no_text": False, "note": None, "body_html": "",
            "evidence_id": None, "error": None,
        }, **ctx},
    )
```

Create the template `partials/citation_reader.html`:

```html
{# Reference reader drawer fragment. role=dialog; modal behaviors are applied by
   JS only at the narrow breakpoint (the desktop side panel is non-modal). #}
<div class="reference-reader" role="dialog" aria-modal="false"
     aria-label="Cited source{% if source_line %}: {{ source_line }}{% endif %}">
  <header class="reference-reader__head">
    <div class="reference-reader__titlerow">
      <span class="reference-reader__source">{{ source_line or "Source" }}</span>
      <button type="button" class="reference-reader__close" data-reader-close
              aria-label="Close">✕</button>
    </div>
    {% if error %}
      <p class="reference-reader__note">{{ error }}</p>
    {% else %}
      {% if stance or confidence or claim %}
      <div class="reference-reader__meta">
        {% if stance %}<span class="stance-chip stance-{{ stance|lower }}">{{ stance }}</span>{% endif %}
        {% if confidence %}<span class="confidence-chip">confidence: {{ confidence|lower }}</span>{% endif %}
        {% if claim %}<span class="reference-reader__claim">claim: “{{ claim }}”</span>{% endif %}
      </div>
      {% endif %}
      {% if total > 1 %}
      <nav class="reference-reader__nav" aria-label="Evidence in this reference">
        {% if prev_ev %}<a class="cite-nav" href="/workspaces/{{ workspace.id }}/citations/{{ prev_ev }}/reader">‹ prev</a>{% else %}<span class="cite-nav is-disabled">‹ prev</span>{% endif %}
        <span class="reference-reader__count">evidence {{ index }} of {{ total }}</span>
        {% if next_ev %}<a class="cite-nav" href="/workspaces/{{ workspace.id }}/citations/{{ next_ev }}/reader">next ›</a>{% else %}<span class="cite-nav is-disabled">next ›</span>{% endif %}
      </nav>
      {% endif %}
      {% if tombstoned %}<p class="reference-reader__hint">This reference was removed from the library; shown for citation reference.</p>{% endif %}
      {% if note %}<p class="reference-reader__hint">{{ note }}</p>{% endif %}
    {% endif %}
  </header>
  {% if not error %}
  <div class="reference-reader__body">
    {% if no_text %}
      <p class="reference-reader__note">Source text is not available yet.</p>
    {% else %}
      <pre class="reference-reader__text">{{ body_html | safe }}</pre>
    {% endif %}
  </div>
  {% endif %}
</div>
```

> The only unescaped value is `body_html`, which the route built by escaping all reference text and wrapping just the matched span in `<mark>`. Everything else (`source_line`, `claim`, `note`) is auto-escaped by Jinja.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_web_citation_reader_route.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/citations.py src/call_manager/web/templates/proposal_assist/partials/citation_reader.html tests/proposal_assist/test_web_citation_reader_route.py
git commit -m "feat(proposal-assist): citation reader route + drawer fragment (status policy, edge matrix)"
```

---

## Task 6: Drawer container + responsive CSS

**Files:**
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` (after the `</div>` that closes `.workspace-layout`)
- Modify: `src/call_manager/web/static/app.css`
- Test: manual + covered indirectly by Task 7 JS tests (no Python assertion here).

- [ ] **Step 1: Add the drawer container**

In `workspace_main.html`, immediately after the `.workspace-layout` block closes, add an empty drawer host (hidden until loaded) and a backdrop:

```html
<div id="reference-drawer" class="reference-drawer" hidden
     data-workspace-id="{{ workspace.id }}"></div>
<div id="reference-drawer-backdrop" class="reference-drawer-backdrop" hidden></div>
```

- [ ] **Step 2: Add CSS**

Append to `app.css`:

```css
/* Citation source viewer — clickable markers + reference drawer */
a.cite {
  font-variant-numeric: tabular-nums;
  font-size: 0.78em;
  font-weight: 700;
  color: var(--cm-primary);
  text-decoration: none;
  padding: 0 0.15em;
  cursor: pointer;
}
a.cite:hover { text-decoration: underline; }
/* Stance-tinted markers (Consensus-style scannability). Neutral/unknown keeps
   the default teal above. */
a.cite--supports { color: var(--cm-good-text); }
a.cite--contradicts { color: var(--cm-bad-text); }

.reference-drawer[hidden],
.reference-drawer-backdrop[hidden] { display: none; }

/* Desktop (≥1024px): the drawer takes the chat column — non-modal side panel. */
@media (min-width: 1024px) {
  .reference-drawer {
    position: fixed;
    top: 0; bottom: 0; left: 0;
    width: calc(50vw - 1.25rem);
    max-width: 720px;
    background: var(--cm-surface);
    border-right: 1px solid var(--cm-border);
    box-shadow: 4px 0 18px rgba(0,0,0,.10);
    z-index: 40;
    overflow: hidden;
  }
  .reference-drawer-backdrop { display: none !important; }
}

/* Narrow (<1024px): full overlay modal dialog. */
@media (max-width: 1023.98px) {
  .reference-drawer {
    position: fixed;
    inset: 0;
    z-index: 50;
    background: var(--cm-surface);
    overflow: hidden;
  }
  .reference-drawer-backdrop {
    position: fixed; inset: 0; z-index: 49;
    background: rgba(15,23,42,.45);
  }
  /* set by JS when the overlay is open */
  body.reader-scroll-lock { overflow: hidden; }
}

.reference-reader { display: flex; flex-direction: column; height: 100%; }
.reference-reader__head {
  padding: 0.7rem 0.9rem;
  background: var(--cm-primary-soft);
  border-bottom: 1px solid var(--cm-primary-border);
}
.reference-reader__titlerow { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.reference-reader__source { font-weight: 700; color: var(--cm-primary); }
.reference-reader__close { width: auto; margin: 0; padding: 0.1rem 0.5rem; background: transparent; border: none; color: var(--cm-muted); cursor: pointer; }
.reference-reader__meta { display: flex; gap: 0.5rem; align-items: center; margin-top: 0.35rem; font-size: 0.85rem; }
.reference-reader__claim { color: var(--cm-muted); font-style: italic; }
.reference-reader__nav { display: flex; justify-content: space-between; align-items: center; margin-top: 0.4rem; font-size: 0.85rem; }
.reference-reader__nav .cite-nav { color: var(--cm-primary); font-weight: 600; text-decoration: none; }
.reference-reader__nav .cite-nav.is-disabled { color: var(--cm-muted); opacity: 0.5; }
.reference-reader__count { color: var(--cm-muted); }
.reference-reader__hint { margin: 0.4rem 0 0; font-size: 0.82rem; color: var(--cm-warn-text); }
.reference-reader__note { margin: 1rem; color: var(--cm-muted); }
.reference-reader__body { flex: 1; overflow-y: auto; padding: 0.9rem; }
.reference-reader__text { white-space: pre-wrap; word-wrap: break-word; font-family: inherit; font-size: 0.9rem; line-height: 1.6; margin: 0; }
.reference-reader__text mark {
  background: var(--cm-warn-bg);
  border-bottom: 2px solid var(--cm-warn-border);
  border-radius: 2px;
  padding: 0 1px;
}
.stance-chip { border-radius: 999px; padding: 0 0.55em; font-size: 0.7rem; font-weight: 700; }
.stance-supports { background: var(--cm-good-bg); color: var(--cm-good-text); border: 1px solid var(--cm-good-border); }
.stance-contradicts { background: var(--cm-bad-bg); color: var(--cm-bad-text); border: 1px solid var(--cm-bad-border); }
.confidence-chip { border-radius: 999px; padding: 0 0.55em; font-size: 0.7rem; font-weight: 600; background: var(--cm-neutral-bg); color: var(--cm-muted); border: 1px solid var(--cm-neutral-border); }
```

- [ ] **Step 3: Verify CSS parses (brace balance) and the page still renders**

Run:
```bash
node -e "const s=require('fs').readFileSync('src/call_manager/web/static/app.css','utf8');const o=(s.match(/{/g)||[]).length,c=(s.match(/}/g)||[]).length;console.log(o===c?'balanced':'MISMATCH '+o+'/'+c)"
```
Expected: `balanced`.

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/workspace_main.html src/call_manager/web/static/app.css
git commit -m "feat(proposal-assist): reference-drawer container + responsive drawer/highlight CSS"
```

---

## Task 7: Drawer JS (delegated click, open/close/scroll, focus, modal narrow-only)

**Files:**
- Modify: `src/call_manager/web/static/app.js`
- Test: `tests/js/citation_drawer.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/js/citation_drawer.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "../../src/call_manager/web/static/app.js");

function load() {
  const handlers = {};
  const ctx = {
    document: {
      body: { addEventListener(n, fn) { (handlers[n] = handlers[n] || []).push(fn); }, classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);} } },
      addEventListener() {},
      readyState: "complete",
      getElementById() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    },
    window: { matchMedia: (q) => ({ matches: ctx._overlay }), htmx: { ajax() {} } },
    setTimeout: (fn) => fn(),
    _overlay: false,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), ctx);
  return { handlers, ctx };
}

test("isCiteLinkHref recognizes only the reader-route pattern", () => {
  const { ctx } = load();
  assert.strictEqual(
    ctx.citeReaderUrl({ getAttribute: () => "/workspaces/W/citations/ev-abc123/reader", classList: { contains: () => true } }),
    "/workspaces/W/citations/ev-abc123/reader"
  );
  // wrong href → null
  assert.strictEqual(
    ctx.citeReaderUrl({ getAttribute: () => "/evil", classList: { contains: () => true } }),
    null
  );
});

test("overlay mode only at the narrow breakpoint", () => {
  const { ctx } = load();
  ctx._overlay = false;
  assert.strictEqual(ctx.readerIsOverlay(), false);
  ctx._overlay = true;
  assert.strictEqual(ctx.readerIsOverlay(), true);
});

test("scroll-lock class is applied only in overlay mode", () => {
  const { ctx } = load();
  ctx._overlay = false;
  ctx.applyReaderModal(true);
  assert.ok(!ctx.document.body.classList.contains("reader-scroll-lock"), "desktop: no lock");
  ctx._overlay = true;
  ctx.applyReaderModal(true);
  assert.ok(ctx.document.body.classList.contains("reader-scroll-lock"), "overlay: locked");
  ctx.applyReaderModal(false);
  assert.ok(!ctx.document.body.classList.contains("reader-scroll-lock"), "cleared on close");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/js/citation_drawer.test.js`
Expected: FAIL — `ctx.citeReaderUrl is not a function`.

- [ ] **Step 3: Write minimal implementation**

Append to `app.js` (functions are top-level so the vm tests can call them; the wiring is delegated and CSP-safe):

```javascript
// --- Citation source viewer: open the reference drawer from a cite link ---
var READER_HREF_RE = /^\/workspaces\/[^/]+\/citations\/ev-[0-9a-f]{6,}\/reader$/;

function citeReaderUrl(el) {
  // Only links WE injected carry class="cite" (agent HTML can't — no class in
  // the bleach allowlist) AND match the exact reader-route shape.
  if (!el || !el.classList || !el.classList.contains("cite")) return null;
  var href = el.getAttribute ? el.getAttribute("href") : null;
  return href && READER_HREF_RE.test(href) ? href : null;
}

function readerIsOverlay() {
  return !!(window.matchMedia && window.matchMedia("(max-width: 1023.98px)").matches);
}

function applyReaderModal(open) {
  // Modal semantics (scroll lock + aria-modal + focus trap) are NARROW-ONLY.
  var drawer = document.getElementById("reference-drawer");
  var overlay = open && readerIsOverlay();
  if (document.body && document.body.classList) {
    document.body.classList[overlay ? "add" : "remove"]("reader-scroll-lock");
  }
  if (drawer && drawer.setAttribute) {
    drawer.setAttribute("aria-modal", overlay ? "true" : "false");
  }
}

function openReaderDrawer(url, triggerEl) {
  var drawer = document.getElementById("reference-drawer");
  var backdrop = document.getElementById("reference-drawer-backdrop");
  if (!drawer) return;
  drawer._returnFocusTo = triggerEl || null;
  if (window.htmx) {
    window.htmx.ajax("GET", url, { target: "#reference-drawer", swap: "innerHTML" });
  }
  drawer.hidden = false;
  if (backdrop) backdrop.hidden = !readerIsOverlay();
  applyReaderModal(true);
  // After the fragment swaps in, scroll the highlight into view + move focus.
  setTimeout(function () {
    var mark = document.getElementById("ev-target");
    if (mark && mark.scrollIntoView) mark.scrollIntoView({ block: "center" });
    if (readerIsOverlay()) {
      var closeBtn = drawer.querySelector("[data-reader-close]");
      if (closeBtn && closeBtn.focus) closeBtn.focus();
    }
  }, 0);
}

function closeReaderDrawer() {
  var drawer = document.getElementById("reference-drawer");
  var backdrop = document.getElementById("reference-drawer-backdrop");
  if (!drawer) return;
  drawer.hidden = true;
  if (backdrop) backdrop.hidden = true;
  applyReaderModal(false);
  var back = drawer._returnFocusTo;
  if (back && back.focus) back.focus();
}

document.body.addEventListener("click", function (evt) {
  var t = evt.target;
  // a cite marker in the proposal → open; a prev/next link inside the drawer
  // (class cite-nav) also routes here.
  var citeUrl = citeReaderUrl(t);
  if (citeUrl) {
    evt.preventDefault();
    openReaderDrawer(citeUrl, t);
    return;
  }
  if (t && t.getAttribute && /\/citations\/ev-[0-9a-f]{6,}\/reader$/.test(t.getAttribute("href") || "") && t.classList && t.classList.contains("cite-nav")) {
    evt.preventDefault();
    openReaderDrawer(t.getAttribute("href"), document.getElementById("reference-drawer") && document.getElementById("reference-drawer")._returnFocusTo);
    return;
  }
  if (t && t.closest && t.closest("[data-reader-close], #reference-drawer-backdrop")) {
    evt.preventDefault();
    closeReaderDrawer();
  }
});

document.body.addEventListener("keydown", function (evt) {
  if (evt.key === "Escape") {
    var drawer = document.getElementById("reference-drawer");
    if (drawer && !drawer.hidden) closeReaderDrawer();
  }
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/js/`
Expected: PASS — the new `citation_drawer.test.js` plus the existing `app_wiring.test.js` / `dedup.test.js` all green.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/static/app.js tests/js/citation_drawer.test.js
git commit -m "feat(proposal-assist): reference-drawer JS — open/scroll/close, focus + modal narrow-only"
```

---

## Task 8: Full-suite gate + manual smoke

**Files:** none (verification only).

- [ ] **Step 1: Run the proposal-assist Python suite + JS + lint**

```bash
uv run pytest tests/proposal_assist -q
node --test tests/js/
uv run ruff check src/call_manager/proposal_assist/citations/reader.py src/call_manager/proposal_assist/web/citations.py src/call_manager/proposal_assist/web/workspaces.py src/call_manager/proposal_assist/web/sections.py
```
Expected: all green; ruff clean.

- [ ] **Step 2: Manual smoke (demo workspace)**

With the dev server running and logged in, open the demo workspace, click a `[n]` marker in the proposal: the drawer opens with the source text, the cited sentence highlighted and scrolled to, prev/next stepping through that reference's passages, Esc/✕ closing it. Narrow the window below 1024px and confirm it becomes a full overlay with a backdrop.

- [ ] **Step 3: Commit (if any smoke fixes were needed)**

```bash
git add -A && git commit -m "fix(proposal-assist): citation source viewer smoke-test fixes"
```

---

## Self-Review (planner)

**Spec coverage:** Decisions 1–5 → Tasks 2/5 (text reader), 6/7 (responsive drawer), 5 (info-rich header), 1/2 (numbered `[n]`), 5 (read-only). Locator ladder + validation → Task 4. Text-node-aware injection + no-`class`-allowlist + forged-link test → Task 2. Proposal-wide numbering across all render paths + corrupt-ledger fail-safe → Task 3. Status-code policy, missing-data matrix, tombstone/stale hints, oversized window → Task 5. Modal-narrow-only + focus → Task 7. Tests enumerated in the spec are distributed across Tasks 1–7 + the Task 8 gate.

**Type/name consistency:** `build_citation_numbering`, `evidence_order_for_reference`, `render_proposal_with_citations`, `locate_evidence` are defined in Task 1/2/4 and used unchanged in Tasks 3 and 5. `_citation_numbering_for` defined in Task 3 (workspaces.py) and imported by sections.py in the same task. The reader route fragment context keys match the `citation_reader.html` template fields.

**Known follow-up (not a blocker):** the `reader_env` fixture (Task 5) must be wired to the suite's existing proposal-assist fixtures (`tests/proposal_assist/conftest.py` + `test_web_references.py` for the authenticated `TestClient`, workspace creation, and `Reference`/`ReferenceChunk` insertion). The fixture seeds one workspace + one marker + one ledger row + one reference/chunk + `text.txt`, parameterized by the option flags the route tests pass.
