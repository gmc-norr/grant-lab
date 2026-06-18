# Cockpit SP2a-4a — Citation Peek + Source Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a member click a citation pill in the cockpit and instantly see the cited source — the evidence sentence highlighted in its document, navigable across that reference's cites, with a ⇱ pin to keep it open in the right pane.

**Architecture:** One new **read-only JSON endpoint** `GET /api/workspaces/{id}/citations/{ev_id}/source` mirrors the existing Jinja `citation_reader_fragment` (`proposal_assist/web/citations.py`) but returns the (windowed) source **text plus a client-side `[start,end)` highlight span** instead of injected `<mark>` HTML. The frontend adds a `useCitationSource` TanStack-Query hook, a presentational `SourceViewer` component, and wires the already-present `onPillClick` callback to open a **center-split peek** (pin → right pane). No DB migration — `ReferenceChunk` already carries every field, and `locate_evidence()` already maps an evidence quote to a chunk-local span.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · pytest + testcontainers Postgres · React 18 · TanStack Query 5 · CodeMirror 6 · openapi-fetch / openapi-typescript · Vitest 2 + jsdom · pnpm.

**Scope:** This is **SP2a-4a** (the READ path — peek/source-viewer/pin). **SP2a-4b** (the WRITE path — claim-first select-to-cite: floating toolbar, editor selection bridge, source-span offset derivation, `POST .../citations` human direct commit) is a **separate later plan** and is OUT OF SCOPE here. The pill click in this plan opens the peek; it does not yet create citations.

**Branch / worktree:** All work happens in the existing worktree `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4` (branch `cockpit-sp2a-4`, off `master`). Every subagent MUST `cd` into that worktree and confirm `git branch --show-current` prints `cockpit-sp2a-4` before any git op — the main tree is on a different branch.

---

## Context the engineer needs (read this first)

**Marker grammar** (already shared FE↔BE): a live citation is `[@<citekey>]<!-- ev:<ev_id> -->` where `ev_id` is `ev-` + ≥6 hex (12 in practice). The frontend renders it as an atomic pill; the pill's click fires `onPillClick(citekey, evId)`. The `ev_id` is the key for the new source endpoint.

**Evidence ledger** (`citations.json`, v2) — each row: `{id, citekey, reference_id, locator:{chunk_index, section_path}, evidence_quote, evidence_quote_offsets, stance?, confidence?, claim_context:{text,hash}, citation_snapshot, created_at, updated_at}`. Loaded with `load_ledger(text|None)`; rows found with `find_by_id(ledger, ev_id)`.

**Existing reader machinery we reuse (do NOT reimplement):**
- `proposal_assist/citations/reader.py::locate_evidence(full_text, chunk_text, quote) -> (start,end)|None` — recomputes the span live (never trusts stored offsets): finds the quote uniquely within the chunk, places the chunk uniquely in the full text, maps + verifies; falls back to a whole-document unique match; `None` if ambiguous/not found.
- `reader.py::evidence_order_for_reference(proposal_text, ledger, reference_id) -> [ev_id,...]` — document-order list of a reference's live cites; drives "i of N" + prev/next.
- `proposal_assist/references/storage.py::ReferenceStorage(root).text_path_for(workspace_id, reference_id) -> Path` — the extracted `text.txt` path.
- The Jinja sibling `proposal_assist/web/citations.py::citation_reader_fragment` (lines 348–430) and its `_reader_body` (lines 291–322) are the **reference implementation** for the resolve-and-window logic. We build a JSON analogue; we do **not** modify the Jinja path.

**`/api` surface conventions** (`web/api/workspaces.py`, `web/api/__init__.py`):
- The router is `APIRouter()` mounted under prefix `/api` (so a route `/workspaces/{id}/...` is served at `/api/workspaces/{id}/...`).
- Handlers `raise HTTPException(status_code, detail=...)`; the global handler converts to the `{error:{code,message,detail?}}` envelope. Status→code mapping used by tests: 403→`forbidden`, 404→`not_found`, 400→`bad_request`, 409→`conflict`, 429→`too_many_requests`.
- Read endpoints are **member-gated** via `require_member(workspace_id=, user=, session=)`. They open a session with `request.app.state.session_factory` and read settings from `request.app.state.settings`.
- A missing/cross-workspace reference must **never leak** (see the `_citation_map` ownership guard `ref.workspace_id == workspace_id` and the existing `test_draft_does_not_leak_cross_workspace_reference_title`).

**Frontend conventions:**
- API hooks live in `frontend/src/api/`; they call `unwrap(await api.GET(...))` (openapi-fetch typed client) and throw `ApiError` (`.code`) on the error envelope.
- Component tests **spy on the client** — `vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never)` — there is no MSW. Render with `QueryClientProvider` + `MemoryRouter`. Hooks are tested with `renderHook` + a `QueryClientProvider` wrapper.
- The generated types file `frontend/src/api/schema.gen.ts` is regenerated by `pnpm gen:api` (delegates to `scripts/gen_openapi_types.sh`). **GOTCHA (memory `project_openapi_codegen_stdout_gotcha`):** the dev `.env` `EMBEDDING_MODEL=mxbai-embed-large` logs a WARNING to stdout that corrupts the JSON; run the generator with `EMBEDDING_MODEL=bge-m3` prefixed.
- Styling: global `--cm-*` tokens in `frontend/src/styles/tokens.css` (imported in `main.tsx`); the cockpit's `.cockpit` / `.cockpit-main` classes are currently **unstyled** plain class names (not CSS modules). This plan adds a global `frontend/src/styles/cockpit.css`.

**Test infra commands** (every command is fully explicit — shell state does NOT persist between subagent Bash calls):
- Backend (one test file, testcontainers Postgres — run the new file in isolation; the full suite is flaky under load per memory `project_full_test_suite_flaky`):
  `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run pytest tests/test_api_workspaces.py -q`
- Frontend (Node 22 via nvm + pnpm):
  `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run`
- tsc: `... && pnpm exec tsc --noEmit`
- ruff: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run ruff check src/call_manager/web/api/workspaces.py src/call_manager/proposal_assist/citations/reader.py tests/test_api_workspaces.py`

---

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/call_manager/proposal_assist/citations/reader.py` | **Modify** — add the pure `build_source_window(...)` helper (offsets, not HTML) | T1 |
| `tests/proposal_assist/test_citation_reader_transform.py` | **Modify** — unit tests for `build_source_window` | T1 |
| `src/call_manager/web/api/workspaces.py` | **Modify** — add `CitationSourceOut` model + `GET .../citations/{ev_id}/source` route | T2 |
| `tests/test_api_workspaces.py` | **Modify** — endpoint tests (auth, leak-guard, no_text, prev/next) | T2 |
| `frontend/src/api/schema.gen.ts` | **Regenerate** — pick up the new path + `CitationSourceOut` type | T3 |
| `frontend/src/api/citations.ts` | **Create** — `useCitationSource(id, evId)` hook | T4 |
| `frontend/src/api/citations.test.tsx` | **Create** — hook tests | T4 |
| `frontend/src/cockpit/SourceViewer.tsx` | **Create** — presentational source-viewer (highlight, nav, pin/close) | T5 |
| `frontend/src/cockpit/SourceViewer.test.tsx` | **Create** — component tests | T5 |
| `frontend/src/routes/Cockpit.tsx` | **Modify** — peek/pinned state, wire `onPillClick`, render panes | T6 |
| `frontend/src/styles/cockpit.css` | **Create** — 3-region grid + source-viewer + `<mark>` styles | T6 |
| `frontend/src/main.tsx` | **Modify** — import `cockpit.css` | T6 |
| `frontend/src/routes/Cockpit.test.tsx` | **Modify** — peek open / pin / close / navigate tests | T6 |

---

## Task 1: Backend pure helper `build_source_window`

A pure, I/O-free function that turns `(full_text, chunk_text, quote)` into `(display_text, highlight_offsets_into_display_text, note)`. This is the JSON analogue of the Jinja `_reader_body` — same locate-then-window matrix, but it returns offsets for the client to wrap in `<mark>` instead of emitting HTML. Keeping it pure makes the six branches unit-testable without a DB or HTTP.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/reader.py`
- Test: `tests/proposal_assist/test_citation_reader_transform.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/proposal_assist/test_citation_reader_transform.py`:

```python
from call_manager.proposal_assist.citations.reader import build_source_window


def test_build_source_window_normal_doc_returns_full_text_and_span():
    full = "Intro paragraph.\n\nThe cat sat on the mat.\n\nConclusion.\n"
    chunk = "The cat sat on the mat."
    quote = "The cat sat on the mat."
    text, hl, note = build_source_window(full, chunk, quote)
    assert text == full
    assert hl is not None and full[hl[0] : hl[1]] == quote
    assert note is None


def test_build_source_window_large_doc_windows_around_span():
    quote = "NEEDLE SENTENCE."
    full = ("x" * 20_000) + quote + ("y" * 20_000)
    text, hl, note = build_source_window(full, None, quote, max_chars=1_000, window_radius=50)
    assert hl is not None and text[hl[0] : hl[1]] == quote
    assert len(text) <= 50 + len(quote) + 50
    assert note is not None and "large document" in note


def test_build_source_window_span_not_found_with_chunk_shows_chunk():
    full = "totally unrelated document body."
    chunk = "The cited section text."
    text, hl, note = build_source_window(full, chunk, "quote that is absent everywhere")
    assert text == chunk
    assert hl == (0, len(chunk))
    assert note is not None and "couldn't pinpoint" in note.lower()


def test_build_source_window_span_not_found_no_chunk_returns_head():
    full = "some document " * 50
    text, hl, note = build_source_window(full, None, "absent quote", window_radius=100)
    assert hl is None
    assert text == full[:200]
    assert note is not None and "couldn't pinpoint" in note.lower()


def test_build_source_window_text_absent_chunk_present():
    text, hl, note = build_source_window(None, "the cited chunk", "anything")
    assert text == "the cited chunk"
    assert hl == (0, len("the cited chunk"))
    assert note == "Showing the cited section."


def test_build_source_window_both_absent():
    assert build_source_window(None, None, "q") == ("", None, None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q -k build_source_window`
Expected: FAIL — `ImportError: cannot import name 'build_source_window'`.

- [ ] **Step 3: Implement the helper**

In `src/call_manager/proposal_assist/citations/reader.py`, add module constants near the top (after the imports) and the function next to `locate_evidence`:

```python
# Mirror the Jinja reader's limits (web/citations.py _MAX_READER_CHARS /
# _WINDOW_RADIUS) so the JSON peek and the legacy HTML drawer window identically.
SOURCE_MAX_CHARS = 500_000
SOURCE_WINDOW_RADIUS = 4_000


def build_source_window(
    full_text: str | None,
    chunk_text: str | None,
    quote: str,
    *,
    max_chars: int = SOURCE_MAX_CHARS,
    window_radius: int = SOURCE_WINDOW_RADIUS,
) -> tuple[str, tuple[int, int] | None, str | None]:
    """JSON analogue of the Jinja ``_reader_body``: return ``(text, highlight, note)``.

    ``text`` is the (possibly windowed) source to display; ``highlight`` is the
    ``[start, end)`` evidence span as offsets INTO ``text`` (NOT the original
    ``full_text``), or ``None`` when the span can't be pinpointed; ``note`` is an
    optional human explanation of any degradation. The missing-data matrix mirrors
    the shipped HTML drawer exactly:

      - full_text and chunk both absent  -> ("", None, None)
      - full_text absent, chunk present  -> (chunk, whole-span, "Showing the cited section.")
      - span not found, chunk present    -> (chunk, whole-span, "...couldn't pinpoint...")
      - span not found, no chunk         -> (full_text head, None, "Couldn't pinpoint...")
      - span found, oversized doc        -> (window, windowed-span, "...large document.")
      - span found, normal doc           -> (full_text, span, None)
    """
    if full_text is None and chunk_text is None:
        return "", None, None
    if full_text is None:
        # text.txt absent but the cited chunk is available — show the chunk whole.
        return chunk_text, (0, len(chunk_text)), "Showing the cited section."
    span = locate_evidence(full_text, chunk_text, quote)
    if span is None:
        if chunk_text is not None:
            return (
                chunk_text,
                (0, len(chunk_text)),
                "Showing the cited section — couldn't pinpoint the exact sentence "
                "in the full document.",
            )
        return full_text[: 2 * window_radius], None, "Couldn't pinpoint the cited sentence."
    s, e = span
    if len(full_text) > max_chars:
        lo = max(0, s - window_radius)
        hi = min(len(full_text), e + window_radius)
        return full_text[lo:hi], (s - lo, e - lo), "Showing the cited section of a large document."
    return full_text, (s, e), None
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run pytest tests/proposal_assist/test_citation_reader_transform.py -q -k build_source_window`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git add src/call_manager/proposal_assist/citations/reader.py tests/proposal_assist/test_citation_reader_transform.py
git commit -m "feat(citations): add build_source_window pure helper for the JSON peek

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Backend JSON endpoint `GET .../citations/{ev_id}/source`

A member-gated read that resolves an `ev_id` → ledger row → reference (ownership-guarded) → chunk + extracted text, then returns the windowed text, the highlight span, the claim/stance, and "i of N" prev/next navigation. JSON analogue of the Jinja `citation_reader_fragment`.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`
- Test: `tests/test_api_workspaces.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api_workspaces.py` (the helpers `_app`, `_seed`, `_client`, `_init_repo`, and imports `Reference`, `empty_ledger`, `dump_ledger`, `workspace_repo_dir`, `CITATIONS_FILENAME` already exist in this file):

```python
# --- SP2a-4a: citation source endpoint --------------------------------------
from call_manager.db.models import ReferenceChunk  # noqa: E402
from call_manager.proposal_assist.references.storage import ReferenceStorage  # noqa: E402


def _seed_source_reference(
    pg_session_factory,
    tmp_path,
    ws_id,
    *,
    rid_workspace_id=None,
    full_text="Intro.\n\nThe cat sat on the mat.\n\nEnd.\n",
    chunk_text="The cat sat on the mat.",
    text_status="done",
    title="The Smith 2021 Study",
    citekey="smith2021",
):
    """Seed a Reference (+ one ReferenceChunk + text.txt) for the source endpoint.

    `rid_workspace_id` lets the Reference belong to a DIFFERENT workspace than the
    one whose ledger points at it (the cross-workspace leak guard test)."""
    owner_ws = rid_workspace_id or ws_id
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=owner_ws,
            kind="paper",
            source_type="uploaded",
            title=title,
            filename="smith2021.pdf",
            storage_path="refs/smith2021.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            text_status=text_status,
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=owner_ws,
                chunk_index=0,
                section_path=None,
                text=chunk_text,
                embedding=[0.0] * 1024,  # Vector(1024); value irrelevant to this route
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    if text_status == "done":
        storage = ReferenceStorage(tmp_path)
        tp = storage.text_path_for(workspace_id=owner_ws, reference_id=rid)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(full_text, encoding="utf-8")
    return rid


def _commit_ledger(tmp_path, ws_id, rows):
    """Write citations.json with `rows` and commit it into the workspace repo."""
    from call_manager.proposal_assist.citations.gitio import _git_env

    repo = workspace_repo_dir(tmp_path, ws_id)
    ledger = empty_ledger()
    ledger["evidence"].extend(rows)
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-qm", "ledger"], cwd=repo, check=True, env=_git_env())


def _source_setup(app, pg_session_factory, tmp_path, *, ev_id, quote, **ref_kwargs):
    owner_id, member_id, stranger_id, ws_id = _seed(
        app,
        pg_session_factory,
        tmp_path,
        f"preamble\n\n## Methods\nSee [@smith2021]<!-- ev:{ev_id} --> here.\n",
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, **ref_kwargs)
    _commit_ledger(
        tmp_path,
        ws_id,
        [
            {
                "id": ev_id,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": quote,
                "stance": "supports",
                "claim_context": {"text": "The cat is relevant.", "hash": "x"},
            }
        ],
    )
    return owner_id, member_id, stranger_id, ws_id, rid


def test_citation_source_happy_path(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    quote = "The cat sat on the mat."
    owner_id, _, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote=quote
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["evidence_id"] == ev_id
    assert "The Smith 2021 Study" in b["source_line"]
    assert b["claim"] == "The cat is relevant."
    assert b["stance"] == "supports"
    assert b["index"] == 1 and b["total"] == 1
    assert b["prev_ev"] is None and b["next_ev"] is None
    assert b["no_text"] is False and b["tombstoned"] is False
    assert b["highlight"] is not None
    s, e = b["highlight"]
    assert b["text"][s:e] == quote


def test_citation_source_member_can_read(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    _, member_id, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    c = _client(app, member_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text


def test_citation_source_non_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    _, _, stranger_id, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    c = _client(app, stranger_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_citation_source_unknown_ev_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id="ev-abc123def456", quote="The cat sat on the mat."
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/ev-000000000000/source")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_citation_source_cross_workspace_reference_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The ledger row's reference_id points at a Reference owned by a DIFFERENT
    workspace — the ownership guard must refuse it (no cross-workspace leak)."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    # Make a second workspace to own the reference.
    with pg_session_factory() as s:
        from call_manager.db.models import User, Workspace

        other_owner = User(status="active", email="other@ki.se")
        s.add(other_owner)
        s.flush()
        ws_b = Workspace(title="B", owner_id=other_owner.id, current_phase="spec")
        s.add(ws_b)
        s.commit()
        ws_b_id = ws_b.id
    owner_id, _, _, ws_id, _ = _source_setup(
        app,
        pg_session_factory,
        tmp_path,
        ev_id=ev_id,
        quote="The cat sat on the mat.",
        rid_workspace_id=ws_b_id,
        title="Cross-Workspace Secret",
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_citation_source_not_indexed_returns_no_text(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    owner_id, _, _, ws_id, _ = _source_setup(
        app,
        pg_session_factory,
        tmp_path,
        ev_id=ev_id,
        quote="The cat sat on the mat.",
        text_status="pending",
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["no_text"] is True
    assert b["text"] == ""
    assert b["highlight"] is None


def test_citation_source_prev_next_navigation(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Two cites of the SAME reference -> index/total + prev/next wiring."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev1, ev2 = "ev-aaaaaaaaaaaa", "ev-bbbbbbbbbbbb"
    owner_id, _, _, ws_id = _seed(
        app,
        pg_session_factory,
        tmp_path,
        f"preamble\n\n## Methods\nA [@smith2021]<!-- ev:{ev1} --> and "
        f"B [@smith2021]<!-- ev:{ev2} -->.\n",
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id)
    _commit_ledger(
        tmp_path,
        ws_id,
        [
            {
                "id": ev1,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": "The cat sat on the mat.",
            },
            {
                "id": ev2,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": "The cat sat on the mat.",
            },
        ],
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev1}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["index"] == 1 and b["total"] == 2
    assert b["prev_ev"] is None and b["next_ev"] == ev2
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run pytest tests/test_api_workspaces.py -q -k citation_source`
Expected: FAIL with 404/`405 Method Not Allowed` or assertion errors (route not defined yet).

- [ ] **Step 3: Implement the model + route**

In `src/call_manager/web/api/workspaces.py`, add these imports to the existing import block:

```python
from sqlalchemy import select
from call_manager.db.models import Reference, ReferenceChunk, User
from call_manager.proposal_assist.references.storage import ReferenceStorage
```

(`Reference` and `User` are already imported on line 18 — extend that line to add `ReferenceChunk`; add the `select` and `ReferenceStorage` imports.)

Add the response model after `CitationOut` (around line 53):

```python
class CitationSourceOut(BaseModel):
    evidence_id: str
    reference_id: str | None = None
    source_line: str
    claim: str | None = None
    stance: str | None = None
    confidence: str | None = None
    index: int
    total: int
    prev_ev: str | None = None
    next_ev: str | None = None
    tombstoned: bool = False
    no_text: bool = False
    note: str | None = None
    text: str
    highlight: list[int] | None = None
```

Add the route after `get_draft` (before `post_save_section`):

```python
@router.get(
    "/workspaces/{workspace_id}/citations/{evidence_id}/source",
    response_model=CitationSourceOut,
)
def get_citation_source(
    workspace_id: uuid.UUID,
    evidence_id: str,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CitationSourceOut:
    """The cited source for ONE evidence id, for the cockpit citation peek.

    Member-gated, read-only. JSON analogue of the Jinja citation_reader_fragment
    (proposal_assist/web/citations.py): resolve the ledger row -> reference (
    ownership-guarded) -> chunk + extracted text, then return the (windowed) text
    plus a client-side [start,end) highlight. An unknown evidence id or a
    missing/cross-workspace reference is a 404; a not-yet-indexed source returns
    200 with no_text=True (so the peek shows a notice, not an error)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        citations_path = repo_dir / CITATIONS_FILENAME
        try:
            ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=404, detail="citations are unavailable") from e
        ev = find_by_id(ledger, evidence_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="no such evidence id")
        # Resolve + ownership-guard the reference (never leak a cross-workspace ref).
        ref = None
        try:
            ref_uuid = uuid.UUID(str(ev.get("reference_id")))
        except (ValueError, TypeError):
            ref_uuid = None
        if ref_uuid is not None:
            ref = session.get(Reference, ref_uuid)
        if ref is None or ref.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="the cited reference is unavailable")
        # Resolve the cited chunk (if the ledger row names one).
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
        # Extracted text only when indexing is complete (mirrors the Jinja route).
        storage = ReferenceStorage(settings.workspaces_dir)
        text_path = storage.text_path_for(workspace_id=workspace_id, reference_id=ref.id)
        text_indexed = ref.text_status == "done"
        full_text = (
            text_path.read_text(encoding="utf-8")
            if text_indexed and text_path.exists()
            else None
        )
        # Position among this reference's live cites (document order) for "i of N".
        proposal_path = repo_dir / PROPOSAL_FILENAME
        proposal_text = proposal_path.read_text() if proposal_path.exists() else ""
        order = citation_reader.evidence_order_for_reference(
            proposal_text, ledger, ev["reference_id"]
        )
        total = len(order)
        pos = order.index(evidence_id) if evidence_id in order else -1
        idx = pos + 1 if pos >= 0 else 0
        prev_ev = order[pos - 1] if pos > 0 else None
        next_ev = order[pos + 1] if 0 <= pos < total - 1 else None
        no_text = not text_indexed
        if no_text:
            text, highlight, note = "", None, None
        else:
            text, highlight, note = citation_reader.build_source_window(
                full_text, chunk.text if chunk else None, ev.get("evidence_quote") or ""
            )
        csl = ref.csl_json or ref.csl_json_extracted or {}
        csl_title = csl.get("title") if isinstance(csl, dict) else None
        title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
        bits = [ref.citekey] if ref.citekey else []
        bits.append(str(title))
        source_line = " · ".join(b for b in bits if b)
        return CitationSourceOut(
            evidence_id=evidence_id,
            reference_id=str(ref_uuid),
            source_line=source_line,
            claim=(ev.get("claim_context") or {}).get("text"),
            stance=ev.get("stance"),
            confidence=ev.get("confidence"),
            index=idx,
            total=total,
            prev_ev=prev_ev,
            next_ev=next_ev,
            tombstoned=ref.tombstoned_at is not None,
            no_text=no_text,
            note=note,
            text=text,
            highlight=list(highlight) if highlight is not None else None,
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4 && uv run pytest tests/test_api_workspaces.py -q -k citation_source`
Expected: PASS (7 passed). If a `connection refused` appears, re-run once in isolation (testcontainer flakiness, memory `project_full_test_suite_flaky`).

- [ ] **Step 5: Lint + full-file test, then commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
uv run pytest tests/test_api_workspaces.py -q
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): GET /api/workspaces/{id}/citations/{ev_id}/source for the cockpit peek

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Regenerate `schema.gen.ts`

The frontend's typed client only knows routes that exist in `schema.gen.ts`. Regenerate it so `useCitationSource` can call the new path with full types.

**Files:**
- Regenerate: `frontend/src/api/schema.gen.ts`

- [ ] **Step 1: Regenerate with the bge-m3 override**

Run (the `EMBEDDING_MODEL=bge-m3` prefix avoids the mxbai stdout-warning that corrupts the JSON — memory `project_openapi_codegen_stdout_gotcha`):

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend
nvm use >/dev/null
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```

Expected: `wrote frontend/src/api/schema.gen.ts`.

- [ ] **Step 2: Verify the new path + type landed**

Run:
```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend
grep -c "citations/{evidence_id}/source" src/api/schema.gen.ts
grep -c "CitationSourceOut" src/api/schema.gen.ts
```
Expected: both print a count ≥ 1.

- [ ] **Step 3: Type-check + commit**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend
nvm use >/dev/null && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git add frontend/src/api/schema.gen.ts
git commit -m "chore(spa): regen schema.gen.ts for the citation source endpoint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Frontend `useCitationSource` hook

A TanStack-Query hook that fetches the source for a selected `ev_id`, disabled until a pill is clicked.

**Files:**
- Create: `frontend/src/api/citations.ts`
- Test: `frontend/src/api/citations.test.tsx`

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/api/citations.test.tsx` (mirrors `src/api/editor.test.tsx`):

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCitationSource } from "./citations";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const SOURCE = {
  evidence_id: "ev-abc123def456",
  reference_id: "r1",
  source_line: "smith2021 · The Smith 2021 Study",
  claim: null,
  stance: "supports",
  confidence: null,
  index: 1,
  total: 1,
  prev_ev: null,
  next_ev: null,
  tombstoned: false,
  no_text: false,
  note: null,
  text: "The cat sat.",
  highlight: [0, 12],
};

describe("useCitationSource", () => {
  it("returns the unwrapped source when an ev id is selected", async () => {
    const spy = vi
      .spyOn(clientMod.api, "GET")
      .mockResolvedValue({ data: SOURCE, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useCitationSource("ws-1", "ev-abc123def456"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(SOURCE);
    expect(spy).toHaveBeenCalledOnce();
  });

  it("does not fetch while ev id is null", () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const qc = new QueryClient();
    const { result } = renderHook(() => useCitationSource("ws-1", null), { wrapper: wrapper(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/api/citations.test.tsx`
Expected: FAIL — cannot resolve `./citations`.

- [ ] **Step 3: Implement the hook**

Create `frontend/src/api/citations.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/citations/{evId}/source — the cited source for the
 *  citation peek. Disabled until an evId is selected (clicking a pill sets it).
 *  evId is part of the queryKey so navigating prev/next refetches per citation. */
export function useCitationSource(id: string, evId: string | null) {
  return useQuery({
    queryKey: ["citation-source", id, evId],
    enabled: evId !== null,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/citations/{evidence_id}/source", {
          params: { path: { workspace_id: id, evidence_id: evId as string } },
        }),
      ),
  });
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/api/citations.test.tsx`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git add frontend/src/api/citations.ts frontend/src/api/citations.test.tsx
git commit -m "feat(spa): useCitationSource hook for the cockpit citation peek

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend `SourceViewer` component

A pure presentational component: given a `CitationSourceOut`, render the source text with the evidence span wrapped in `<mark>`, a header with "i of N" + prev/next + pin/close, the claim line, the not-available/degraded notices, and a tombstoned banner. No data fetching — easy to unit-test.

**Files:**
- Create: `frontend/src/cockpit/SourceViewer.tsx`
- Test: `frontend/src/cockpit/SourceViewer.test.tsx`

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/cockpit/SourceViewer.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SourceViewer, type CitationSource } from "./SourceViewer";

function source(over: Partial<CitationSource> = {}): CitationSource {
  return {
    evidence_id: "ev-1",
    reference_id: "r1",
    source_line: "smith2021 · The Smith 2021 Study",
    claim: "The claim sentence.",
    stance: "supports",
    confidence: null,
    index: 1,
    total: 2,
    prev_ev: null,
    next_ev: "ev-2",
    tombstoned: false,
    no_text: false,
    note: null,
    text: "abcNEEDLEdef",
    highlight: [3, 9],
    ...over,
  };
}

describe("SourceViewer", () => {
  const noop = () => {};

  it("wraps the highlight span in a <mark>", () => {
    const { container } = render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={noop} />,
    );
    const mark = container.querySelector("mark");
    expect(mark?.textContent).toBe("NEEDLE");
  });

  it("renders the source line, claim and position", () => {
    render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={noop} />,
    );
    expect(screen.getByText(/The Smith 2021 Study/)).toBeInTheDocument();
    expect(screen.getByText(/The claim sentence\./)).toBeInTheDocument();
    expect(screen.getByText(/1 of 2/)).toBeInTheDocument();
  });

  it("shows the not-available notice when no_text", () => {
    render(
      <SourceViewer
        source={source({ no_text: true, text: "", highlight: null })}
        pinned={false}
        onClose={noop}
        onPin={noop}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/isn.t available yet/i)).toBeInTheDocument();
  });

  it("renders a tombstoned banner", () => {
    render(
      <SourceViewer
        source={source({ tombstoned: true })}
        pinned={false}
        onClose={noop}
        onPin={noop}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/has been removed/i)).toBeInTheDocument();
  });

  it("fires onNavigate with next_ev and disables prev when prev_ev is null", () => {
    const onNavigate = vi.fn();
    render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={onNavigate} />,
    );
    expect(screen.getByRole("button", { name: /previous citation/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /next citation/i }));
    expect(onNavigate).toHaveBeenCalledWith("ev-2");
  });

  it("shows Pin when not pinned and hides it when pinned; Close always fires", () => {
    const onPin = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(
      <SourceViewer source={source()} pinned={false} onClose={onClose} onPin={onPin} onNavigate={noop} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /pin source/i }));
    expect(onPin).toHaveBeenCalledOnce();
    rerender(
      <SourceViewer source={source()} pinned onClose={onClose} onPin={onPin} onNavigate={noop} />,
    );
    expect(screen.queryByRole("button", { name: /pin source/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /close source/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/cockpit/SourceViewer.test.tsx`
Expected: FAIL — cannot resolve `./SourceViewer`.

- [ ] **Step 3: Implement the component**

Create `frontend/src/cockpit/SourceViewer.tsx`:

```tsx
import type { components } from "../api/schema.gen";

export type CitationSource = components["schemas"]["CitationSourceOut"];

type Props = {
  source: CitationSource;
  pinned: boolean;
  onClose: () => void;
  onPin: () => void;
  onNavigate: (evId: string) => void;
};

/** Presentational cited-source pane: highlighted evidence + nav + pin/close.
 *  Pure (no data fetching); the Cockpit owns selection + the useCitationSource
 *  query and passes the resolved source in. */
export function SourceViewer({ source, pinned, onClose, onPin, onNavigate }: Props) {
  const { text, highlight } = source;
  return (
    <aside className="source-viewer" aria-label="Cited source">
      <header className="source-viewer-head">
        <span className="source-viewer-title">{source.source_line}</span>
        {source.total > 0 && (
          <span className="source-viewer-pos">
            {source.index} of {source.total}
          </span>
        )}
        <div className="source-viewer-actions">
          <button
            type="button"
            aria-label="Previous citation"
            disabled={!source.prev_ev}
            onClick={() => source.prev_ev && onNavigate(source.prev_ev)}
          >
            ‹
          </button>
          <button
            type="button"
            aria-label="Next citation"
            disabled={!source.next_ev}
            onClick={() => source.next_ev && onNavigate(source.next_ev)}
          >
            ›
          </button>
          {!pinned && (
            <button type="button" aria-label="Pin source" onClick={onPin}>
              ⇱ Pin
            </button>
          )}
          <button type="button" aria-label="Close source" onClick={onClose}>
            ✕
          </button>
        </div>
      </header>

      {source.tombstoned && (
        <p className="source-viewer-tombstoned" role="note">
          This reference has been removed.
        </p>
      )}
      {source.stance && (
        <p className="source-viewer-meta">
          <span className={`source-viewer-stance stance-${source.stance}`}>{source.stance}</span>
        </p>
      )}
      {source.claim && (
        <p className="source-viewer-claim">
          <strong>Claim:</strong> {source.claim}
        </p>
      )}

      {source.no_text ? (
        <p className="source-viewer-empty">The source text isn’t available yet.</p>
      ) : (
        <div className="source-viewer-body">
          {highlight ? (
            <>
              {text.slice(0, highlight[0])}
              <mark>{text.slice(highlight[0], highlight[1])}</mark>
              {text.slice(highlight[1])}
            </>
          ) : (
            text
          )}
        </div>
      )}

      {source.note && <p className="source-viewer-note">{source.note}</p>}
    </aside>
  );
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/cockpit/SourceViewer.test.tsx`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git add frontend/src/cockpit/SourceViewer.tsx frontend/src/cockpit/SourceViewer.test.tsx
git commit -m "feat(spa): SourceViewer component (highlighted cited source + nav + pin)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Wire the peek into Cockpit + 3-region layout + CSS

Open the peek when a pill is clicked, render it as a center split beside the editor, and promote it to the right pane on pin. Add the cockpit grid + source-viewer styles.

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Create: `frontend/src/styles/cockpit.css`
- Modify: `frontend/src/main.tsx`
- Test: `frontend/src/routes/Cockpit.test.tsx`

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/routes/Cockpit.test.tsx`. First add helpers near the existing `draftPayload`/`renderCockpit` (do NOT duplicate those):

```tsx
const PEEK_EV = "ev-abc123def456";

function draftWithPill() {
  return {
    data: {
      head: "a".repeat(40),
      can_edit: true,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        {
          index: 1,
          heading: "Methods",
          text: `## Methods\nSee [@smith2021]<!-- ev:${PEEK_EV} --> here.\n`,
          sha: "s1",
        },
      ],
      citations: {
        [PEEK_EV]: { citekey: "smith2021", number: 1, stance: "supports", title: "Smith 2021" },
      },
    },
    error: undefined,
  };
}

function sourcePayload(over: Record<string, unknown> = {}) {
  return {
    data: {
      evidence_id: PEEK_EV,
      reference_id: "r1",
      source_line: "smith2021 · The Smith 2021 Study",
      claim: null,
      stance: "supports",
      confidence: null,
      index: 1,
      total: 1,
      prev_ev: null,
      next_ev: null,
      tombstoned: false,
      no_text: false,
      note: null,
      text: "The cat sat on the mat.",
      highlight: [0, 23],
      ...over,
    },
    error: undefined,
  };
}

// Route GET by path: /draft -> draft, /source -> source. The whole impl is cast
// `as never` because openapi-fetch's GET has a complex generic signature that a
// plain (path: string) lambda doesn't structurally satisfy under tsc.
function mockGetByPath(draft: unknown, source: unknown) {
  return vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) =>
    path.endsWith("/source")
      ? Promise.resolve(source)
      : Promise.resolve(draft)) as never);
}
```

Then add the tests:

```tsx
it("clicking a citation pill opens the source peek", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  // Open the Methods section so its pill renders.
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const pill = container.querySelector(".cm-citation-pill");
  expect(pill).not.toBeNull();
  fireEvent.mouseDown(pill!);
  expect(await screen.findByText(/The Smith 2021 Study/)).toBeInTheDocument();
  expect(container.querySelector(".source-viewer mark")?.textContent).toBe(
    "The cat sat on the mat.",
  );
});

it("pinning moves the source to the pinned pane and closes the center peek", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  fireEvent.mouseDown(container.querySelector(".cm-citation-pill")!);
  await screen.findByText(/The Smith 2021 Study/);
  fireEvent.click(screen.getByRole("button", { name: /pin source/i }));
  // Pinned pane shows it; the Pin button is gone (only the pinned, unpinnable copy remains).
  expect(container.querySelector(".cockpit-pinned .source-viewer")).not.toBeNull();
  expect(screen.queryByRole("button", { name: /pin source/i })).toBeNull();
});

it("closing the peek removes the source pane", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  fireEvent.mouseDown(container.querySelector(".cm-citation-pill")!);
  await screen.findByText(/The Smith 2021 Study/);
  fireEvent.click(screen.getByRole("button", { name: /close source/i }));
  expect(container.querySelector(".cockpit-peek .source-viewer")).toBeNull();
});
```

(Confirm `fireEvent` and `screen` are already imported at the top of the file; they are used by the existing tests.)

- [ ] **Step 2: Run to verify they fail**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/routes/Cockpit.test.tsx`
Expected: FAIL — no `.source-viewer` rendered (pill click is still a no-op).

- [ ] **Step 3: Implement the wiring**

In `frontend/src/routes/Cockpit.tsx`:

1. Add imports:
```tsx
import { useCitationSource } from "../api/citations";
import { SourceViewer } from "../cockpit/SourceViewer";
```

2. Add state after the existing `saveError` state (line ~41):
```tsx
  // SP2a-4a citation peek: clicking a pill sets peekEv (center split); ⇱ Pin
  // promotes it to pinnedEv (right pane). Both drive a useCitationSource query.
  const [peekEv, setPeekEv] = useState<string | null>(null);
  const [pinnedEv, setPinnedEv] = useState<string | null>(null);
```

3. Add the queries after `const save = useSaveSection(id);` (these hooks must run before the early returns — they already sit above them):
```tsx
  const peek = useCitationSource(id, peekEv);
  const pinned = useCitationSource(id, pinnedEv);
```
   (Place them alongside `useDraft`/`useSaveSection`, i.e. BEFORE the `if (draft.isLoading)` early returns, so hook order is stable.)

4. Replace the no-op `onPillClick` (lines 147–149) with:
```tsx
  // Clicking a citation pill opens the source peek (SP2a-4a). select-to-cite
  // (writing a NEW citation) is SP2a-4b.
  const onPillClick = (_citekey: string, evId: string) => setPeekEv(evId);
```

5. Render the panes. Change the root to carry the pinned pane and add the peek as a center split. Replace the outer `<main className="cockpit">…</main>` return so the structure is: outline `<nav>` · `<section className="cockpit-main">` (existing editor block) · the center peek `<section className="cockpit-peek">` (only when `peekEv` set) · the pinned `<aside className="cockpit-pinned">` (only when `pinnedEv` set). Concretely, insert these two blocks **immediately after** the closing `</section>` of `cockpit-main` and before the closing `</main>`:

```tsx
        {peekEv !== null && (
          <section className="cockpit-peek" aria-label="Citation source peek">
            {peek.isLoading && <p aria-busy="true">Loading source…</p>}
            {peek.isError && <p role="alert">Could not load the cited source.</p>}
            {peek.data && (
              <SourceViewer
                source={peek.data}
                pinned={false}
                onClose={() => setPeekEv(null)}
                onPin={() => {
                  setPinnedEv(peekEv);
                  setPeekEv(null);
                }}
                onNavigate={(evId) => setPeekEv(evId)}
              />
            )}
          </section>
        )}

        {pinnedEv !== null && (
          <aside className="cockpit-pinned" aria-label="Pinned source">
            {pinned.isLoading && <p aria-busy="true">Loading source…</p>}
            {pinned.isError && <p role="alert">Could not load the cited source.</p>}
            {pinned.data && (
              <SourceViewer
                source={pinned.data}
                pinned
                onClose={() => setPinnedEv(null)}
                onPin={() => {}}
                onNavigate={(evId) => setPinnedEv(evId)}
              />
            )}
          </aside>
        )}
```

   Leave the existing `<nav>` and `<section className="cockpit-main">` blocks unchanged.

- [ ] **Step 4: Add the stylesheet + import it**

Create `frontend/src/styles/cockpit.css`:

```css
/* Cockpit layout: Outline | editor (+ center peek) | pinned source.
   Plain global classes (the Cockpit uses bare className strings, not CSS
   modules). Tokens come from styles/tokens.css (loaded globally in main.tsx). */
.cockpit {
  display: grid;
  grid-template-columns: minmax(12rem, 16rem) minmax(0, 1fr) auto auto;
  gap: 1rem;
  align-items: start;
}
.cockpit-main {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 0;
}
.cockpit-peek,
.cockpit-pinned {
  min-width: 18rem;
  max-width: 28rem;
}

.source-viewer {
  border: 1px solid var(--cm-border);
  border-radius: 8px;
  background: var(--cm-surface);
  box-shadow: var(--cm-shadow-sm);
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.source-viewer-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.source-viewer-title {
  font-weight: 600;
  color: var(--cm-ink-strong);
}
.source-viewer-pos {
  color: var(--cm-muted);
  font-size: 0.85em;
}
.source-viewer-actions {
  margin-left: auto;
  display: flex;
  gap: 0.25rem;
}
.source-viewer-actions button {
  border: 1px solid var(--cm-border);
  background: var(--cm-surface-sunken);
  border-radius: 4px;
  padding: 0 0.4rem;
  cursor: pointer;
  color: var(--cm-ink);
}
.source-viewer-actions button:disabled {
  opacity: 0.4;
  cursor: default;
}
.source-viewer-body {
  white-space: pre-wrap;
  max-height: 60vh;
  overflow: auto;
  font-size: 0.92em;
  line-height: 1.5;
  color: var(--cm-ink);
}
.source-viewer-body mark {
  background: var(--cm-warn-bg);
  color: var(--cm-ink-strong);
  border-radius: 2px;
  padding: 0 1px;
}
.source-viewer-claim {
  font-size: 0.9em;
  color: var(--cm-muted);
}
.source-viewer-empty,
.source-viewer-note {
  color: var(--cm-muted);
  font-size: 0.85em;
}
.source-viewer-tombstoned {
  color: var(--cm-bad-text);
  background: var(--cm-bad-bg);
  border: 1px solid var(--cm-bad-border);
  border-radius: 4px;
  padding: 0.25rem 0.5rem;
  font-size: 0.85em;
}
.source-viewer-stance {
  display: inline-block;
  font-size: 0.75em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0 0.35rem;
  border-radius: 999px;
  background: var(--cm-good-bg);
  color: var(--cm-good-text);
  border: 1px solid var(--cm-good-border);
}
```

In `frontend/src/main.tsx`, add the import after the existing tokens import:
```tsx
import "./styles/tokens.css";
import "./styles/cockpit.css";
```

- [ ] **Step 5: Run to verify they pass**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend && nvm use >/dev/null && pnpm exec vitest run src/routes/Cockpit.test.tsx`
Expected: PASS (all existing Cockpit tests + the 3 new ones).

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git add frontend/src/routes/Cockpit.tsx frontend/src/routes/Cockpit.test.tsx frontend/src/styles/cockpit.css frontend/src/main.tsx
git commit -m "feat(spa): open a citation source peek on pill click (pin to right pane)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Full verification

Confirm the whole slice is green and the generated types haven't drifted.

- [ ] **Step 1: Backend tests (isolation)**

Run:
```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
uv run pytest tests/test_api_workspaces.py tests/proposal_assist/test_citation_reader_transform.py -q
```
Expected: all pass. (Re-run any `connection refused` failure in isolation — testcontainer flakiness.)

- [ ] **Step 2: Backend lint**

Run:
```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
uv run ruff check src/call_manager/web/api/workspaces.py src/call_manager/proposal_assist/citations/reader.py tests/test_api_workspaces.py tests/proposal_assist/test_citation_reader_transform.py
```
Expected: `All checks passed!`

- [ ] **Step 3: Frontend suite + tsc**

Run:
```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend
nvm use >/dev/null
pnpm exec tsc --noEmit && pnpm exec vitest run
```
Expected: tsc clean; all vitest files pass.

- [ ] **Step 4: Schema-drift gate**

Re-run the generator and confirm it produces NO diff (the committed `schema.gen.ts` already matches the backend):
```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4/frontend
nvm use >/dev/null
EMBEDDING_MODEL=bge-m3 pnpm gen:api
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4
git diff --exit-code frontend/src/api/schema.gen.ts && echo "NO DRIFT"
```
Expected: `NO DRIFT` (exit 0). If it diffs, the endpoint changed after Task 3 — commit the regenerated file.

- [ ] **Step 5: Deferred note**

The Playwright e2e cases in `frontend/e2e/cockpit.spec.ts` stay `test.skip` (they need a running backend + seeded fixture + real caret geometry, unavailable under jsdom). A live peek e2e is deferred to the SP2a e2e hardening pass — do NOT unskip here. Leave a one-line note in the final PR/commit summary that the peek is covered by component tests, with e2e deferred.

---

## Out of scope (SP2a-4b — the next plan)

- `POST /api/workspaces/{id}/citations` human-direct-commit endpoint (reusing `insert_citation_impl` / the `insert_from_annotation` pattern as the named human actor).
- Editor selection bridge (a production-safe way to read the CodeMirror selection + insert at an offset — NOT the dev-gated `__cmView`).
- The floating selection toolbar (`discuss · cite from a source · comment`).
- Claim-first select-to-cite: select claim span → select supporting span in the `SourceViewer` → derive `(chunk_index, quote, offsets)` client-side, fuzzy-match, confirm chip → commit.
- A reference-chunk listing endpoint if span-selection needs raw chunk text beyond the single cited chunk.

These build directly on this plan's `SourceViewer` + source endpoint.
