# ProseMirror ⇄ Yjs ⇄ Markdown Bridge — Spike (Collaborative Editing, Slice 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that a **pure-Python** bridge can faithfully round-trip the constrained proposal schema across **markdown ⇄ ProseMirror-JSON ⇄ Yjs `XmlFragment`**, matching the canonical JS (`y-prosemirror`) byte-for-byte on the structured representation, with the `split_sections` invariant preserved — producing a **GO / NO-GO** decision for the server-authoritative design in the spec.

**Architecture:** A new pure-Python `collab/` package (`markdown-it-py` for parse, a constrained serializer for emit, `pycrdt` for the Yjs side). The canonical JS libraries are run **once, offline**, to generate committed **golden fixtures**; Python tests assert (a) markdown round-trip stability, (b) the `split_sections` invariant on serializer output, and (c) **fidelity**: our Python reading of a `y-prosemirror`-produced Yjs document equals the JS ProseMirror-JSON.

**Tech Stack:** Python 3.12 (`uv`), `prosemirror` (prosemirror-py, model/schema), `pycrdt`, `markdown-it-py` (already a dep). JS golden generation (offline only): `yjs`, `y-prosemirror`, `prosemirror-model`, `prosemirror-markdown`, `prosemirror-schema-list` via `pnpm`.

## Global Constraints

- **Pure Python at runtime — NO Node component in the backend.** Node is used *only offline* to generate committed golden fixtures; tests and production read pure-Python output.
- **Constrained schema only:** block — `heading` (**levels 1–6**; level 2 is the section boundary — but H1 titles and other levels occur in existing docs and **MUST survive** round-trip), `paragraph`, `bullet_list`, `ordered_list`, `list_item`, `blockquote`, `code_block`, `horizontal_rule`, `image`; marks — `strong`, `em`, `strikethrough`, `code`, `link`; plus `hard_break` and a custom inline atom **`citation`**. No other markdown constructs.
- **Bare markers are literal text, not citations.** Only the full form `[@key]<!-- ev:ev-… -->` (`MARKER_RE`) becomes a `citation` node; a bare `[@x9]` (citekey only — present in existing docs) must round-trip as **plain text**, unescaped, so the existing bare-marker scan still finds it.
- **Round-trip is idempotence, not byte-equality with arbitrary input.** Existing docs may be re-normalized on the first checkpoint (e.g., blank lines inserted between blocks); that cosmetic diff is acceptable, but it **must be documented** in the findings as a hydration consequence.
- **Citation on-disk form is exactly** `[@<citekey>]<!-- ev:ev-<hex> -->` (see `src/call_manager/proposal_assist/citations/markers.py`, `MARKER_RE` / `build_marker`). A `citation` node carries attrs `{citekey, ev_id}` and serializes back to this exact string.
- **`split_sections` invariant must hold on serializer output:** `"".join(s.text for s in split_sections(out)) == out`, and the ordered list of H2 headings is preserved. (`src/call_manager/proposal_assist/editing/sectionizer.py`.)
- **Markdown is normalized by the serializer** — the serializer is the single canonical writer; round-trip is defined as `serialize(parse(md)) == serialize(parse(serialize(parse(md))))` (idempotent after one normalization pass), not byte-equality with arbitrary input.
- **`pnpm add` is subject to a 7-day `minimumReleaseAge` cooldown** → install golden-gen deps with `--config.minimumReleaseAge=0` (ephemeral; does not persist).
- **This is a SPIKE.** The deliverable is a working, tested bridge library **plus a written GO/NO-GO**. If any in-scope construct cannot round-trip faithfully, **STOP and report it** in the findings doc — do not paper over it with a lossy hack.

---

### Task 0: Dependencies + scaffold

**Files:**
- Modify: `pyproject.toml` (via `uv add`)
- Modify: `frontend/package.json` (via `pnpm add`)
- Create: `src/call_manager/proposal_assist/collab/__init__.py`
- Create: `tests/proposal_assist/collab/__init__.py`
- Create: `frontend/scripts/` (directory for the golden generator)

**Interfaces:**
- Produces: the `call_manager.proposal_assist.collab` package (empty), importable.

- [ ] **Step 1: Add backend deps + regenerate the no-build list**

Run: `uv add prosemirror pycrdt`
Expected: `pyproject.toml` `[project].dependencies` gains `prosemirror` and `pycrdt`; `uv.lock` updated. (`markdown-it-py` is already present.)

The repo pins `[tool.uv] exclude-newer = "2026-06-14T00:00:00Z"` and a **`no-build-package` list generated from `uv.lock`** (CI enforces it stays in sync). After the add:

Run: `uv run python scripts/gen_no_build.py`
Expected: `no-build-package` updated to include the new deps; **no dep flagged as source-only** (both `prosemirror` and `pycrdt` must resolve to wheels published before `exclude-newer`). If the script flags a source-only dep, STOP and record it (it would run a third-party build backend — a supply-chain blocker) before proceeding.

- [ ] **Step 2: Add JS golden-gen deps (cooldown-bypassed, dev only)**

Run: `cd frontend && pnpm add -D --config.minimumReleaseAge=0 yjs y-prosemirror prosemirror-model prosemirror-markdown prosemirror-schema-list`
Expected: these appear under `devDependencies`; lockfile updated.

- [ ] **Step 3: Create the package + test package**

Create `src/call_manager/proposal_assist/collab/__init__.py` containing only:
```python
"""Pure-Python bridge between markdown, ProseMirror JSON, and Yjs documents."""
```
Create empty `tests/proposal_assist/collab/__init__.py`.

- [ ] **Step 4: Smoke-import test**

Create `tests/proposal_assist/collab/test_imports.py`:
```python
def test_deps_import():
    import markdown_it  # noqa: F401
    import prosemirror  # noqa: F401
    import pycrdt  # noqa: F401
    from call_manager.proposal_assist import collab  # noqa: F401
```
Run: `uv run pytest tests/proposal_assist/collab/test_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock frontend/package.json frontend/pnpm-lock.yaml \
        src/call_manager/proposal_assist/collab/__init__.py \
        tests/proposal_assist/collab/__init__.py tests/proposal_assist/collab/test_imports.py
git commit -m "feat(collab): scaffold bridge package + spike deps"
```

---

### Task 1: The shared fixture corpus + citation marker confirmation

**Files:**
- Create: `tests/proposal_assist/collab/fixtures.py`

**Interfaces:**
- Produces: `CORPUS: list[tuple[str, str]]` — `(name, markdown)` pairs covering every in-scope construct. Later tasks iterate this list.

- [ ] **Step 1: Write the corpus**

Create `tests/proposal_assist/collab/fixtures.py`. Use `build_marker` so citation fixtures are exactly the on-disk form (never hand-type the marker), and pull in the **real** seeded skeleton. The corpus must collectively cover **every** node/mark in the schema plus the existing-document shapes from High-2 (H1 title, bare marker, tight layout):
```python
from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.artifacts import SPEC_SKELETON

_CITE = build_marker("smith2021", "ev-0a1b2c3d4e5f")   # full marker → citation node
_CITE2 = build_marker("jones2020", "ev-9f8e7d6c5b4a")

CORPUS: list[tuple[str, str]] = [
    ("preamble_only", "Intro paragraph with no heading.\n"),
    ("single_section", "## Aims\n\nWe aim to do the thing.\n"),
    ("multi_section_h3",
     "## Approach\n\n### Phase 1\n\nDo the first part.\n\n### Phase 2\n\nThen the second.\n"),
    ("marks_and_links",
     "## Novelty\n\nThis is **bold**, *italic*, ~~struck~~, `code`, and a [link](https://example.org).\n"),
    ("nested_marked_list",
     "## Plan\n\n- **bold item** with a [link](https://example.org)\n  - nested item\n- second\n\n1. one\n2. two\n"),
    ("blockquote_and_code",
     "## Notes\n\n> a quote\n\n```python\nprint('hi')\n```\n"),
    ("horizontal_rule", "## Sep\n\nabove\n\n---\n\nbelow\n"),
    ("image", "## Figure\n\n![alt text](https://example.org/fig.png)\n"),
    ("hard_break", "## Address\n\nline one\\\nline two after a hard break.\n"),
    # a "## " inside a fence must NOT split a section:
    ("fenced_h2_not_a_section", "## Methods\n\n```\n## not a heading\n```\n\nReal body.\n"),
    ("citation", f"## Key Claims\n\nThe sky is blue {_CITE} and well studied.\n"),
    ("adjacent_citations", f"## Key Claims\n\nMultiple sources agree {_CITE}{_CITE2} on this.\n"),
    # High-2 existing-document shapes — MUST hydrate without content migration:
    ("bare_marker_is_literal_text", "## Background\n\nold body [@x9] stays literal text.\n"),
    ("h1_title_preamble", "# Proposal Title\n\nIntro under an H1 title.\n\n## Aims\n\nBody.\n"),
    # byte-identical to tests/test_editing_save.py's "production layout" (tight, H1 + bare marker):
    ("production_layout", "# T\nintro\n## Background\nold body [@x9]\n## Aims\naims\n"),
    ("spec_skeleton", SPEC_SKELETON),
]
```

- [ ] **Step 2: Verify the corpus is self-consistent with the existing sectionizer**

Create `tests/proposal_assist/collab/test_fixtures.py`:
```python
from call_manager.proposal_assist.editing.sectionizer import split_sections
from .fixtures import CORPUS

def test_corpus_roundtrips_through_sectionizer():
    # Every fixture must already satisfy the byte-identical split/join invariant,
    # so any later break is the bridge's fault, not the fixture's.
    for name, md in CORPUS:
        assert "".join(s.text for s in split_sections(md)) == md, name

def test_fenced_h2_is_not_a_section_boundary():
    md = dict(CORPUS)["fenced_h2_not_a_section"]
    headings = [s.heading for s in split_sections(md) if s.heading]
    assert headings == ["Methods"]  # the fenced "## not a heading" is body, not a boundary
```
Run: `uv run pytest tests/proposal_assist/collab/test_fixtures.py -v`
Expected: PASS (proves the corpus is valid input before the bridge touches it).

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/collab/fixtures.py tests/proposal_assist/collab/test_fixtures.py
git commit -m "test(collab): shared bridge fixture corpus"
```

---

### Task 2: Constrained schema (Python + matching JS)

**Files:**
- Create: `src/call_manager/proposal_assist/collab/schema.py`
- Create: `frontend/scripts/collab-schema.mjs`

**Interfaces:**
- Produces (Python): `SCHEMA` — a `prosemirror.model.Schema` with the constrained node/mark set; `CITATION_NODE = "citation"`.
- Produces (JS): `export const schema` — a `prosemirror-model` `Schema` with the identical spec, for golden generation and (later) the editor.

- [ ] **Step 1: Write the Python schema**

Create `src/call_manager/proposal_assist/collab/schema.py` defining a `prosemirror.model.Schema` whose nodes are exactly the Global-Constraints set and whose marks are `strong, em, strikethrough, code, link`. The `heading` node must accept a `level` attr spanning **1–6** (H1 titles exist in real docs). The `citation` node is an **inline atom** (`inline: True, atom: True, group: "inline"`) with `attrs={"citekey": {}, "ev_id": {}}`. Build on `prosemirror.schema.basic` / `prosemirror.schema.list_` node specs where they match, overriding only what differs.

- [ ] **Step 2: Test the Python schema shape**

Create `tests/proposal_assist/collab/test_schema.py`:
```python
from call_manager.proposal_assist.collab.schema import SCHEMA, CITATION_NODE

def test_schema_has_constrained_nodes():
    for n in ["doc","paragraph","heading","bullet_list","ordered_list",
              "list_item","blockquote","code_block","horizontal_rule",
              "image","hard_break","citation"]:
        assert n in SCHEMA.nodes, n

def test_schema_has_constrained_marks():
    for m in ["strong","em","strikethrough","code","link"]:
        assert m in SCHEMA.marks, m

def test_citation_is_inline_atom():
    cite = SCHEMA.nodes[CITATION_NODE]
    assert cite.spec.get("inline") is True
    assert cite.spec.get("atom") is True
    assert set(cite.spec["attrs"]) == {"citekey", "ev_id"}

def test_heading_carries_a_level_attr():
    # H1-H6 must be representable (real docs use H1 titles).
    assert "level" in SCHEMA.nodes["heading"].spec.get("attrs", {})
```
Run: `uv run pytest tests/proposal_assist/collab/test_schema.py -v`
Expected: PASS.

- [ ] **Step 3: Write the matching JS schema**

Create `frontend/scripts/collab-schema.mjs` exporting a `prosemirror-model` `Schema` with the **same** nodes/marks/attrs (use `prosemirror-schema-list`'s list nodes; define the same `citation` inline-atom spec with `parseDOM`/`toDOM` for a `span[data-citekey][data-ev-id]`). This single file is the JS source of truth reused by Task 4 and (later) the Tiptap editor.

- [ ] **Step 4: Sanity-run the JS schema**

Run: `cd frontend && node -e "import('./scripts/collab-schema.mjs').then(m=>console.log(Object.keys(m.schema.nodes).join(',')))"`
Expected: prints the node names including `citation`.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/schema.py frontend/scripts/collab-schema.mjs \
        tests/proposal_assist/collab/test_schema.py
git commit -m "feat(collab): constrained ProseMirror schema (Python + JS)"
```

---

### Task 3: Markdown ⇄ ProseMirror-JSON (Python)

**Files:**
- Create: `src/call_manager/proposal_assist/collab/markdown_bridge.py`

**Interfaces:**
- Produces: `markdown_to_pm(md: str) -> dict` (ProseMirror doc JSON, validates against `SCHEMA`) and `pm_to_markdown(doc: dict) -> str`.
- Consumes: `SCHEMA`, `CITATION_NODE` from Task 2; `markers.MARKER_RE` / `markers.build_marker`.

- [ ] **Step 1: Write the round-trip test FIRST**

Create `tests/proposal_assist/collab/test_markdown_bridge.py`:
```python
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.editing.sectionizer import split_sections
from .fixtures import CORPUS

def test_markdown_roundtrip_is_idempotent():
    # serialize(parse(x)) is the canonical normal form; applying it again is a no-op.
    for name, md in CORPUS:
        once = pm_to_markdown(markdown_to_pm(md))
        twice = pm_to_markdown(markdown_to_pm(once))
        assert once == twice, name

def test_split_sections_invariant_holds_on_output():
    for name, md in CORPUS:
        out = pm_to_markdown(markdown_to_pm(md))
        assert "".join(s.text for s in split_sections(out)) == out, name

def test_h2_headings_preserved():
    for name, md in CORPUS:
        before = [s.heading for s in split_sections(md) if s.heading]
        out = pm_to_markdown(markdown_to_pm(md))
        after = [s.heading for s in split_sections(out) if s.heading]
        assert before == after, name

def test_citation_marker_survives_roundtrip():
    md = dict(CORPUS)["citation"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert "[@smith2021]<!-- ev:ev-0a1b2c3d4e5f -->" in out

def test_adjacent_citations_survive():
    md = dict(CORPUS)["adjacent_citations"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert "[@smith2021]<!-- ev:ev-0a1b2c3d4e5f -->" in out
    assert "[@jones2020]<!-- ev:ev-9f8e7d6c5b4a -->" in out

def test_bare_marker_stays_literal_text_unescaped():
    # A citekey-only marker is NOT a citation node; it must remain literal,
    # UNESCAPED text so the existing bare-marker scan still matches it.
    out = pm_to_markdown(markdown_to_pm(dict(CORPUS)["bare_marker_is_literal_text"]))
    assert "[@x9]" in out  # not "\\[@x9\\]"

def test_h1_title_in_preamble_survives():
    out = pm_to_markdown(markdown_to_pm(dict(CORPUS)["h1_title_preamble"]))
    assert out.lstrip().startswith("# Proposal Title")

def test_production_layout_hydrates():
    # The exact tests/test_editing_save.py doc (H1 + bare marker + tight layout)
    # must round-trip idempotently and preserve its H2 sections.
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    md = dict(CORPUS)["production_layout"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert pm_to_markdown(markdown_to_pm(out)) == out             # idempotent
    assert "[@x9]" in out                                          # bare marker literal
    assert [s.heading for s in split_sections(out) if s.heading] == ["Background", "Aims"]
```
Run: `uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py -v`
Expected: FAIL (module not implemented). **If `test_bare_marker_stays_literal_text_unescaped` or `test_production_layout_hydrates` can't be made green, that is a hydration/migration finding — record it in Task 6 before forcing it.**

- [ ] **Step 2: Implement `markdown_to_pm`**

In `markdown_bridge.py`, parse with `markdown_it.MarkdownIt("commonmark").enable(["strikethrough"])`, walk the token stream, and build ProseMirror-JSON nodes for the constrained schema. Add a `markdown-it` **inline rule** that recognizes `MARKER_RE` and emits a `citation` node `{type:"citation", attrs:{citekey, ev_id}}` (so the marker never reaches text). Validate the result by constructing `SCHEMA.node_from_json(doc)` (raises on an out-of-schema doc).

- [ ] **Step 3: Implement `pm_to_markdown`**

Implement a serializer for the constrained schema. **Evaluate `prosemirror-py-converter` first**; if it covers the schema cleanly, depend on it, otherwise write a focused `MarkdownSerializer` over the node/mark set (small — the schema is constrained). The `citation` node serializes to `build_marker(attrs.citekey, attrs.ev_id)`. Ensure non-final blocks emit a trailing newline so the `split_sections` `##`-boundary rule is satisfied.

- [ ] **Step 4: Run the round-trip tests**

Run: `uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py -v`
Expected: PASS. **If any fixture cannot be made idempotent, record it in the findings doc (Task 6) as a NO-GO candidate before forcing it.**

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/markdown_bridge.py \
        tests/proposal_assist/collab/test_markdown_bridge.py
git commit -m "feat(collab): markdown<->prosemirror-json bridge"
```

---

### Task 4: JS golden fixtures (the `y-prosemirror` reference)

**Files:**
- Create: `frontend/scripts/gen_collab_goldens.mjs`
- Create: `tests/proposal_assist/collab/goldens/` (generated JSON, committed)

**Interfaces:**
- Produces: one `goldens/<name>.json` per corpus entry: `{ "markdown": str, "pm_json": object, "ydoc_update_b64": str }`.

- [ ] **Step 1: Mirror the corpus into the generator**

Create `frontend/scripts/gen_collab_goldens.mjs`. Define the **same** `(name, markdown)` corpus as `fixtures.py` (a JSON literal at the top of the file; the Task 6 test asserts the two corpora match so they can't silently drift).

- [ ] **Step 2: Implement generation against the canonical libs**

For each fixture: parse markdown with `prosemirror-markdown`'s `MarkdownParser` configured for `collab-schema.mjs`'s `schema` (with a custom token for the citation marker), producing a PM doc → `doc.toJSON()` for `pm_json`. Then `import { prosemirrorToYDoc, yDocToProsemirrorJSON } from 'y-prosemirror'`, build `const ydoc = prosemirrorToYDoc(doc, 'prosemirror')`, and `Buffer.from(Y.encodeStateAsUpdate(ydoc)).toString('base64')` for `ydoc_update_b64`.

**Self-verify each golden before writing it** (so a Task-5 failure can only be a Python bug, never a bad fixture): assert `deepStrictEqual(yDocToProsemirrorJSON(ydoc, 'prosemirror'), pm_json)` — i.e. the JS round-trip through Yjs is itself an identity. Throw and abort the whole generation if any fixture fails this. Only then write each object to `goldens/<name>.json`.

- [ ] **Step 3: Generate + eyeball**

Run: `cd frontend && node scripts/gen_collab_goldens.mjs`
Expected: `tests/proposal_assist/collab/goldens/*.json` created, one per fixture, each with the three keys and non-empty `ydoc_update_b64`.

- [ ] **Step 4: Commit the generator + goldens**

```bash
git add frontend/scripts/gen_collab_goldens.mjs tests/proposal_assist/collab/goldens/
git commit -m "test(collab): JS-reference golden fixtures (y-prosemirror)"
```

---

### Task 5: ProseMirror-JSON ⇄ Yjs `XmlFragment` (Python, `pycrdt`) + fidelity gate

**Files:**
- Create: `src/call_manager/proposal_assist/collab/yjs_bridge.py`

**Interfaces:**
- Produces: `pm_json_to_ydoc(doc: dict) -> pycrdt.Doc` (populates the `'prosemirror'` `XmlFragment`) and `ydoc_to_pm_json(ydoc: pycrdt.Doc) -> dict` (reads it back).
- Consumes: `SCHEMA` (Task 2); golden files (Task 4).

- [ ] **Step 1: Write the fidelity + round-trip tests FIRST**

Create `tests/proposal_assist/collab/test_yjs_bridge.py`:
```python
import base64, json, pathlib
import pycrdt
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from .fixtures import CORPUS

GOLDENS = pathlib.Path(__file__).parent / "goldens"

def _golden(name): return json.loads((GOLDENS / f"{name}.json").read_text())

def test_pm_yjs_roundtrip_stable():
    # PM -> Yjs (pycrdt) -> PM is identity for our own output.
    for name, _ in CORPUS:
        pm = _golden(name)["pm_json"]
        ydoc = pm_json_to_ydoc(pm)
        assert ydoc_to_pm_json(ydoc) == pm, name

def test_fidelity_against_y_prosemirror():
    # THE CRITICAL GATE: reading a y-prosemirror-produced document in Python
    # yields exactly the JS ProseMirror JSON.
    for name, _ in CORPUS:
        g = _golden(name)
        ydoc = pycrdt.Doc()
        ydoc.apply_update(base64.b64decode(g["ydoc_update_b64"]))
        assert ydoc_to_pm_json(ydoc) == g["pm_json"], name
```
Run: `uv run pytest tests/proposal_assist/collab/test_yjs_bridge.py -v`
Expected: FAIL (module not implemented).

- [ ] **Step 2: Inspect the reference structure FIRST (do not assume the mapping)**

The internal `XmlFragment` shape is the thing being spiked — derive it, don't bake it in. Write a throwaway diagnostic that loads a golden update into a `pycrdt.Doc`, reads `ydoc.get("prosemirror", type=pycrdt.XmlFragment)`, and prints the actual tree: each node's `nodeName`, its attributes, and how text + marks are represented (e.g. `XmlText` with formatting attributes vs nested elements; how the `citation` atom appears).

Run: `uv run python -c "<diagnostic that dumps the XmlFragment of goldens/marks_and_links.json and goldens/citation.json>"`
Expected: a printed structure you can read off. Record the observed mapping in a comment in `yjs_bridge.py`.

- [ ] **Step 3: Implement the Yjs bridge to MATCH the observed reference**

In `yjs_bridge.py`, implement `pm_json_to_ydoc` / `ydoc_to_pm_json` over `ydoc.get("prosemirror", type=pycrdt.XmlFragment)` to **reproduce the structure observed in Step 2** (this is the Python analogue of `yXmlFragmentToProsemirrorJSON`). Do not invent a representation; match what `y-prosemirror` actually produces.

- [ ] **Step 4: Run the tests — fidelity is the gate**

Run: `uv run pytest tests/proposal_assist/collab/test_yjs_bridge.py -v`
Expected: PASS. **Declare NO-GO only if `pycrdt` genuinely cannot read or represent the reference structure faithfully** (e.g., a construct `y-prosemirror` encodes in a way `pycrdt` can't round-trip) — *not* merely because a first guessed mapping was wrong. Record the exact construct and the pycrdt limitation in the findings doc.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/yjs_bridge.py \
        tests/proposal_assist/collab/test_yjs_bridge.py
git commit -m "feat(collab): prosemirror-json<->yjs xmlfragment bridge + fidelity tests"
```

---

### Task 6: Full pipeline test + GO/NO-GO findings

**Files:**
- Create: `tests/proposal_assist/collab/test_full_pipeline.py`
- Create: `docs/superpowers/notes/2026-06-30-bridge-spike-findings.md`

**Interfaces:**
- Consumes: every module above.

- [ ] **Step 1: Assert the JS and Python corpora cannot drift**

Add to `test_full_pipeline.py`:
```python
import json, pathlib
from .fixtures import CORPUS

def test_js_and_python_corpora_match():
    goldens = pathlib.Path(__file__).parent / "goldens"
    py = {name: md for name, md in CORPUS}
    js = {p.stem: json.loads(p.read_text())["markdown"] for p in goldens.glob("*.json")}
    assert py == js  # same names, same markdown on both sides
```

- [ ] **Step 2: Assert the whole pipeline end-to-end**

Add:
```python
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json

def test_md_to_yjs_to_md_is_canonical():
    for name, md in CORPUS:
        norm = pm_to_markdown(markdown_to_pm(md))
        ydoc = pm_json_to_ydoc(markdown_to_pm(md))
        back = pm_to_markdown(ydoc_to_pm_json(ydoc))
        assert back == norm, name
```
Run: `uv run pytest tests/proposal_assist/collab/ -v`
Expected: PASS for the whole `collab` suite.

- [ ] **Step 3: Write the GO/NO-GO findings doc**

Create `docs/superpowers/notes/2026-06-30-bridge-spike-findings.md` recording: **GO or NO-GO**; the list of constructs that round-trip cleanly; any markdown **normalization caveats** (what input shapes get rewritten); any **schema constraints discovered** (constructs we must forbid in the editor); the chosen PM→markdown approach (`prosemirror-py-converter` vs hand-written); any fidelity gaps vs `y-prosemirror`; and an explicit **hydration verdict** — whether every current proposal/spec shape (H1 titles, bare markers, tight production layouts, the spec skeleton) hydrates without content migration, with any normalization/migration step listed as a Slice-1 prerequisite. This doc is the input to the Slice 1 (sync-server) plan.

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/collab/test_full_pipeline.py \
        docs/superpowers/notes/2026-06-30-bridge-spike-findings.md
git commit -m "test(collab): full bridge pipeline + spike GO/NO-GO findings"
```

---

## Definition of done (spike gate)

- The corpus **collectively covers every node/mark in the schema** (headings incl. H1, paragraphs, both list kinds + nesting, blockquote, code block, horizontal rule, image, hard break, the citation node, marks/links) **and the existing-document shapes** (H1 title, bare marker, tight production layout, spec skeleton).
- `uv run pytest tests/proposal_assist/collab/ -v` is green: markdown round-trip is idempotent, the `split_sections` invariant holds on output, citation markers survive, bare markers stay literal & unescaped, **and `ydoc_to_pm_json` of a `y-prosemirror`-produced document equals the JS ProseMirror JSON for every fixture**.
- The findings doc states **GO** (proceed to Slice 1: sync server + persistence + auth) or **NO-GO** with the specific construct(s) that failed and a recommended fallback (e.g., a narrow Node serialization worker for those constructs only), **and explicitly answers: do all current proposal/spec document shapes hydrate without content migration?** Any construct that needs normalization or a migration pass is listed as a Slice-1 prerequisite.

## Notes for the implementer

- These tests are **pure-Python, no Postgres** — they do not use `testcontainers`/`pg_session`; they run fast and need no DB. Run them in isolation, not via the flaky whole-suite run.
- `node`/`pnpm` are needed **only** for Task 4 (golden generation); the golden JSON is committed, so the Python test suite (and CI) never invokes Node.
- The fidelity test in Task 5 is the single most important assertion in this spike. If it cannot be made green for a fixture, that is the headline result — surface it, don't bury it.
