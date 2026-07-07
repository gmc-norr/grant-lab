# Collab Slice 3b-5d·D1b — Stale-Snapshot 409 Concurrency Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, like D1a) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a live-collab writer from hitting a spurious `409`/stale when they Cite or Edit a section whose live Yjs room is ahead of git HEAD, by baselining both toolbar-input gates on *anchor-relocation against the live section* instead of a client git sha.

**Architecture:** Backend-only (Approach 1). Two keyword-only params, each defaulting to today's behavior: (A) `apply_citation_to_room(enforce_section_sha=True)` — `direct_cite_via_kernel` passes `False` so the direct Cite path rests on the anchor guards it already has; (B) `build_selection_block(allow_anchor_relocation=False)` — the collab-draft branch of `turns.py` passes `True` so an Edit `modify` relocates by anchor instead of rejecting on the stale git sha. Accept-time `kernel_accept_prose` (already live-vs-live) and the inbox `refresh_staleness` (already room-canonical) are untouched.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / pycrdt (pure-Python Yjs) / pytest + testcontainers Postgres; Playwright (e2e). No schema, no migration, no new dependency.

## Global Constraints

- Feature flag **stays OFF** in prod until the explicit 3b-5d flip (D4). This slice is test-only until then.
- **Flag-off parity is a HARD gate — byte-for-byte.** Both new params default to the current behavior; only `direct_cite_via_kernel` and the collab-draft branch of `turns.py` pass the non-default value.
- **No migration, no schema change** for collab slices.
- Backend changes are additive; the two new params are keyword-only with safe defaults.
- pytest is run with `EMBEDDING_MODEL=bge-m3`, grouped by directory (**never the whole suite** — testcontainers PG drops under load), `-p no:cacheprovider`, and the final sweep **always includes `tests/test_api_spec.py`**.
- Lint gate is `ruff check .` (NOT `ruff format`). Frontend gate is `pnpm exec tsc --noEmit` + `pnpm exec vitest run` (there is no TS linter).
- Explicit-path `git add` only (never `-A`). Never touch master/main from a worktree (this runs in the main tree). Merges are **local `--no-ff` only**, not pushed without an explicit ask. Do NOT invoke `/code-review` or `/ultrareview`.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## File Structure

**Modified (source):**
- `src/call_manager/proposal_assist/collab/kernel.py` — `apply_citation_to_room`: add `enforce_section_sha: bool = True`, guard the sha-equality with it (Component A core).
- `src/call_manager/proposal_assist/citations/collab_cite.py` — `direct_cite_via_kernel`: pass `enforce_section_sha=False` into `apply_citation_to_room` (Component A wiring).
- `src/call_manager/proposal_assist/turns/selection.py` — `build_selection_block`: add `allow_anchor_relocation: bool = False` + a module-level helper `_relocated_selection_block` reusing `anchor_occurrences` (Component B core).
- `src/call_manager/web/api/turns.py` — collab-draft branch: pass `allow_anchor_relocation=collab_draft` (Component B wiring).

**Modified (tests):**
- `tests/proposal_assist/collab/test_direct_cite_via_kernel.py` — re-point the stale-does-not-evict test to a gone-anchor trigger; add a stale-sha-allowed regression test.
- `tests/proposal_assist/turns/test_selection.py` — add the collab-relocation unit tests.

**Created (tests):**
- `tests/proposal_assist/test_submit_turn_collab_relocation.py` — route/integration wiring test for the Edit submit path (Codex-Medium).

**Modified (e2e):**
- `frontend/e2e/collab.spec.ts` — new skip-gated scenario (e): co-edit → cite-without-save.

**Modified (docs, at the end):**
- `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` — append a D1b outcome section.

---

### Task 0: Cut the branch

**Files:** none (git only).

- [ ] **Step 1: Confirm you are on master with the D1b spec commits and a clean tree**

Run: `git -C /home/parlar_ai/dev/call-monitor branch --show-current && git -C /home/parlar_ai/dev/call-monitor log --oneline -2 && git -C /home/parlar_ai/dev/call-monitor status --porcelain`
Expected: branch `master`; top commits `92f4013` (fold-in) over `d70846d` (spec); only the two unrelated untracked `docs/superpowers/...` files, nothing staged.

- [ ] **Step 2: Cut the feature branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b collab-slice3b5d1b
```
Expected: "Switched to a new branch 'collab-slice3b5d1b'".

---

### Task 1: Component A — direct Cite ignores the stale section sha

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (`apply_citation_to_room`, signature ~line 65-83 + guard ~line 92)
- Modify: `src/call_manager/proposal_assist/citations/collab_cite.py` (`direct_cite_via_kernel` call ~line 61-67)
- Test: `tests/proposal_assist/collab/test_direct_cite_via_kernel.py`

**Interfaces:**
- Produces: `apply_citation_to_room(..., require_anchor_in_section: bool = False, enforce_section_sha: bool = True) -> CitationApplyResult`. When `enforce_section_sha=False`, the `live.sha != base_section_sha` check is skipped; correctness rests on `require_anchor_in_section` (anchor present in the selected section) + `compute_citation_insert`'s whole-doc ambiguity (`CiteInvalid`→422) / ref-not-found (`CiteRefNotFound`→404) / anchor-moved (`CiteStale`→409).
- Consumes (unchanged): `kernel_accept_citation` calls `apply_citation_to_room` with the default `enforce_section_sha=True` → terminal-stale parity preserved.

- [ ] **Step 1: Re-point the existing stale-no-evict test to a gone-anchor trigger**

In `tests/proposal_assist/collab/test_direct_cite_via_kernel.py`, replace `test_direct_cite_stale_does_not_evict` (currently triggers `CiteStale` via `base_section_sha="dead"*16`, which D1b no longer rejects) with a gone-anchor trigger. Its intent — a **clean** stale error does not evict — is preserved; only the trigger changes.

```python
@pytest.mark.anyio
async def test_direct_cite_anchor_gone_is_clean_stale_no_evict(pg_session_factory, tmp_path, monkeypatch):
    # D1b: the direct path no longer enforces base_section_sha, so "stale" is now
    # surfaced by a GONE anchor (require_anchor_in_section), not a bad sha. It must
    # still be a CLEAN pre-mutation error → no eviction, no lingering room.
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck2@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    with pg_session_factory() as s:
        bsha = _base_sha(s, wid, str(tmp_path))
    evict_calls: list = []
    _orig_evict = MANAGER.evict
    async def _spy_evict(key):
        evict_calls.append(key)
        return await _orig_evict(key)
    monkeypatch.setattr(MANAGER, "evict", _spy_evict)
    with pytest.raises(CiteStale):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), workspace_id=wid,
            actor_id=oid, section_index=1, base_section_sha=bsha,
            anchor="NOT PRESENT IN THIS SECTION", reference_id=rid, chunk_index=0,
            evidence_quote="azure", quote_start=0, quote_end=5, stance="supports")
    assert evict_calls == []  # clean error → evict must NOT have been called
    assert MANAGER.get((wid, "draft")) is None  # clean abort → no lingering room
```

- [ ] **Step 2: Add the stale-sha-allowed regression test**

Append to the same file:

```python
@pytest.mark.anyio
async def test_direct_cite_stale_sha_allowed_when_anchor_present(pg_session_factory, tmp_path):
    # D1b THE regression: a mismatched/stale base_section_sha (the toolbar's git sha,
    # while the live room is ahead) with a still-present anchor SUCCEEDS — direct_cite
    # passes enforce_section_sha=False and relies on anchor-relocation.
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck6@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    out = await direct_cite_via_kernel(
        session_factory=pg_session_factory, workspaces_dir=str(tmp_path), workspace_id=wid,
        actor_id=oid, section_index=1, base_section_sha="dead" * 16,  # STALE — now ignored
        anchor="The claim here.", reference_id=rid, chunk_index=0,
        evidence_quote="azure", quote_start=0, quote_end=5, stance="supports")
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in disk
```

- [ ] **Step 3: Run both tests — verify they FAIL (behavior not yet changed)**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_direct_cite_via_kernel.py -p no:cacheprovider -q`
Expected: `test_direct_cite_stale_sha_allowed_when_anchor_present` FAILS (raises `CiteStale` today); `test_direct_cite_anchor_gone_is_clean_stale_no_evict` PASSES already (gone anchor raises `CiteStale` even with today's sha-enforced code, since the sha matches `bsha`).

- [ ] **Step 4: Add the `enforce_section_sha` param + guard in the kernel**

In `src/call_manager/proposal_assist/collab/kernel.py`, add the keyword-only param to `apply_citation_to_room` (place it after `require_anchor_in_section=False,` in the signature):

```python
    require_anchor_in_section=False,
    enforce_section_sha=True,
) -> CitationApplyResult:
```

Then change the sha guard (currently `if live.sha != base_section_sha:`):

```python
    live = sections[section_index]
    if enforce_section_sha and live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
```

- [ ] **Step 5: Wire `direct_cite_via_kernel` to skip sha enforcement**

In `src/call_manager/proposal_assist/citations/collab_cite.py`, the `apply_citation_to_room(...)` call inside `_body` currently ends with `require_anchor_in_section=True)`. Add the new kwarg:

```python
                        workspaces_dir=workspaces_dir, require_anchor_in_section=True,
                        enforce_section_sha=False)
```

- [ ] **Step 6: Run the file — verify all pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_direct_cite_via_kernel.py -p no:cacheprovider -q`
Expected: all tests PASS (the regression now succeeds; the gone-anchor test still cleanly stales).

- [ ] **Step 7: Run the pc-path parity + apply-unit tests — verify unchanged**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_apply_citation_to_room.py tests/proposal_assist/collab/test_collab_kernel_citation.py -p no:cacheprovider -q`
Expected: all PASS unchanged — `test_apply_citation_stale_section_sha_raises` and `kernel_accept_citation` still stale on a sha mismatch (they use the default `enforce_section_sha=True`).

- [ ] **Step 8: Lint + commit**

```bash
cd /home/parlar_ai/dev/call-monitor && ruff check src/call_manager/proposal_assist/collab/kernel.py src/call_manager/proposal_assist/citations/collab_cite.py tests/proposal_assist/collab/test_direct_cite_via_kernel.py
git add src/call_manager/proposal_assist/collab/kernel.py src/call_manager/proposal_assist/citations/collab_cite.py tests/proposal_assist/collab/test_direct_cite_via_kernel.py
git commit -m "$(cat <<'EOF'
fix(collab): direct cite baselines on the live anchor, not the git section sha (3b-5d.D1b)

apply_citation_to_room gains enforce_section_sha=True (default preserves the
inbox pc-accept terminal-stale parity); direct_cite_via_kernel passes False so
a live-collab cite no longer 409s just because the room is ahead of git HEAD.
Correctness rests on the guards it already had: anchor present-in-section +
whole-doc ambiguity(422)/ref-not-found(404)/anchor-moved(409).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Component B — `build_selection_block` relocates by anchor on the collab path

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/selection.py` (add param + `_relocated_selection_block` helper)
- Test: `tests/proposal_assist/turns/test_selection.py`

**Interfaces:**
- Produces: `build_selection_block(*, repo_dir, sel, sections=None, allow_anchor_relocation: bool = False) -> dict`. When `allow_anchor_relocation=True` and the live section exists, the git sha/offsets are ignored and the anchor is relocated in `live.text` via `anchor_occurrences`: exactly one → verified block with recomputed `from_offset`/`to_offset`; zero → `modify` raises `SelectionConflictError`, `ask` returns unverified; multiple → `modify` raises, `ask` returns unverified.
- Consumes: `anchor_occurrences(text: str, anchor: str) -> list[int]` from `call_manager.proposal_assist.citations.markers` (marker-aware, code-point start indices).

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/proposal_assist/turns/test_selection.py`:

```python
def test_collab_relocation_modify_verifies_short_unique_span(tmp_path):
    # git has an OLD Aims; the live room is AHEAD → the client's git sha is stale.
    # A unique SHORT span ("diagnostic") relocates and verifies (no 409). THE regression.
    # Bogus client offsets (999,1002) prove the block recomputes them from the anchor.
    live_doc = "p\n\n## Aims\nPrecision medicine improves diagnostic outcomes.\n"
    repo = _repo(tmp_path, "p\n\n## Aims\nold aims text.\n")
    live_secs = split_sections(live_doc)
    frm = live_secs[1].text.index("diagnostic")
    block = build_selection_block(
        repo_dir=repo,
        sel=_sel(repo, live_doc, 1, 999, 1002, "diagnostic", rt="modify", base_sha="0" * 40),
        sections=live_secs, allow_anchor_relocation=True,
    )
    assert block == {
        "type": "selection", "request_type": "modify", "section_index": 1,
        "anchor": "diagnostic", "from_offset": frm, "to_offset": frm + len("diagnostic"),
        "verified": True, "heading": "Aims",
    }


def test_collab_relocation_modify_anchor_gone_raises(tmp_path):
    live_doc = "p\n\n## Aims\nPrecision medicine outcomes.\n"  # no "diagnostic"
    repo = _repo(tmp_path, "p\n\n## Aims\nx.\n")
    live_secs = split_sections(live_doc)
    with pytest.raises(SelectionConflictError, match="no longer in this section"):
        build_selection_block(
            repo_dir=repo,
            sel=_sel(repo, live_doc, 1, 0, 10, "diagnostic", rt="modify", base_sha="0" * 40),
            sections=live_secs, allow_anchor_relocation=True,
        )


def test_collab_relocation_modify_ambiguous_raises(tmp_path):
    live_doc = "p\n\n## Aims\nrisk here and risk there.\n"  # "risk" appears twice
    repo = _repo(tmp_path, "p\n\n## Aims\nx.\n")
    live_secs = split_sections(live_doc)
    with pytest.raises(SelectionConflictError, match="appears more than once"):
        build_selection_block(
            repo_dir=repo,
            sel=_sel(repo, live_doc, 1, 0, 4, "risk", rt="modify", base_sha="0" * 40),
            sections=live_secs, allow_anchor_relocation=True,
        )


def test_collab_relocation_ask_anchor_gone_is_unverified(tmp_path):
    live_doc = "p\n\n## Aims\nPrecision medicine outcomes.\n"
    repo = _repo(tmp_path, "p\n\n## Aims\nx.\n")
    live_secs = split_sections(live_doc)
    block = build_selection_block(
        repo_dir=repo,
        sel=_sel(repo, live_doc, 1, 0, 10, "diagnostic", rt="ask", base_sha="0" * 40),
        sections=live_secs, allow_anchor_relocation=True,
    )
    assert block["verified"] is False and block["anchor"] == "diagnostic"


def test_flag_off_modify_stale_sha_still_409(tmp_path):
    # Default allow_anchor_relocation=False → unchanged flag-off behavior (parity).
    live_doc = "p\n\n## Aims\nPrecision medicine improves diagnostic outcomes.\n"
    repo = _repo(tmp_path, live_doc)
    live_secs = split_sections(live_doc)
    with pytest.raises(SelectionConflictError):
        build_selection_block(
            repo_dir=repo,
            sel=_sel(repo, live_doc, 1, 0, 10, "diagnostic", rt="modify", base_sha="stale" * 8),
            sections=live_secs,  # NO allow_anchor_relocation → default False
        )
```

- [ ] **Step 2: Run — verify they FAIL**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/turns/test_selection.py -p no:cacheprovider -q`
Expected: the four `test_collab_relocation_*` tests FAIL (`build_selection_block` has no `allow_anchor_relocation` kwarg → `TypeError`); `test_flag_off_modify_stale_sha_still_409` PASSES already.

- [ ] **Step 3: Implement the relocation branch**

In `src/call_manager/proposal_assist/turns/selection.py`, add the import near the top (after the existing `split_sections` import):

```python
from call_manager.proposal_assist.citations.markers import anchor_occurrences
```

Add a module-level helper above `build_selection_block`:

```python
def _relocated_selection_block(sel: SelectionInput, live) -> dict:
    """Collab path: the client's base_section_sha and offsets are git-derived and the
    live room is ahead of git, so re-locate the anchor in the LIVE section text and
    ignore the stale sha. Verified iff the anchor occurs exactly once (marker-aware).
    A modify that can't be uniquely located is a real conflict (409); an ask stays
    leniently unverified."""
    occ = anchor_occurrences(live.text, sel.anchor)
    if len(occ) == 1:
        frm = occ[0]
        to = frm + len(sel.anchor)
        if to - frm > MAX_SELECTION_CODEPOINTS:
            raise SelectionConflictError("selection is too long — select a smaller span")
        return {
            "type": "selection",
            "request_type": sel.request_type,
            "section_index": sel.section_index,
            "anchor": sel.anchor,
            "from_offset": frm,
            "to_offset": to,
            "verified": True,
            "heading": live.heading,
        }
    if sel.request_type == "modify":
        if not occ:
            raise SelectionConflictError(
                "the text you selected is no longer in this section — reselect"
            )
        raise SelectionConflictError(
            "your selection appears more than once — select a more specific range"
        )
    # ask: leniently unverified (unchanged spirit of the git-path unverified branch).
    if len(sel.anchor) > MAX_SELECTION_CODEPOINTS:
        raise SelectionConflictError("selection is too long — select a smaller span")
    return {
        "type": "selection",
        "request_type": sel.request_type,
        "section_index": sel.section_index,
        "anchor": sel.anchor,
        "from_offset": sel.from_offset,
        "to_offset": sel.to_offset,
        "verified": False,
        "heading": live.heading,
    }
```

Change the `build_selection_block` signature and insert the branch right after `live` is computed:

```python
def build_selection_block(
    *, repo_dir: Path, sel: SelectionInput, sections=None, allow_anchor_relocation=False
) -> dict:
    if sections is None:
        proposal = repo_dir / PROPOSAL_FILENAME
        sections = split_sections(proposal.read_text()) if proposal.exists() else []
    live = sections[sel.section_index] if 0 <= sel.section_index < len(sections) else None

    if allow_anchor_relocation and live is not None:
        return _relocated_selection_block(sel, live)

    verified = live is not None and live.sha == sel.base_section_sha
```

(Everything from `verified = ...` onward is unchanged.)

- [ ] **Step 4: Run — verify all pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/turns/test_selection.py -p no:cacheprovider -q`
Expected: all PASS (the existing git-path tests and the new relocation tests).

- [ ] **Step 5: Lint + commit**

```bash
cd /home/parlar_ai/dev/call-monitor && ruff check src/call_manager/proposal_assist/turns/selection.py tests/proposal_assist/turns/test_selection.py
git add src/call_manager/proposal_assist/turns/selection.py tests/proposal_assist/turns/test_selection.py
git commit -m "$(cat <<'EOF'
feat(collab): build_selection_block relocates by anchor on the collab path (3b-5d.D1b)

allow_anchor_relocation=False by default (flag-off byte-for-byte). When True,
an Edit selection is re-located in the LIVE section via anchor_occurrences
(marker-aware) instead of gating on the stale git base_section_sha: unique →
verified with recomputed offsets; modify with a gone/ambiguous anchor → a real
409; ask stays leniently unverified.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Component B wiring — `turns.py` passes `allow_anchor_relocation` (Codex-Medium)

**Files:**
- Modify: `src/call_manager/web/api/turns.py` (~line 146-172, the `selection_block` build)
- Create: `tests/proposal_assist/test_submit_turn_collab_relocation.py`

**Interfaces:**
- Consumes: `build_selection_block(..., allow_anchor_relocation=...)` (Task 2). A unit test on the function cannot catch a missing call-site wire; this route test pins it.

- [ ] **Step 1: Write the failing route/integration test**

Create `tests/proposal_assist/test_submit_turn_collab_relocation.py`:

```python
"""POST /api/workspaces/{id}/turns — collab Edit submit relocates by anchor (3b-5d.D1b).

A unit test on build_selection_block cannot catch a plan that adds
allow_anchor_relocation but forgets to pass it from the collab-draft branch of
turns.py (Codex-Medium). This drives the real route: a collab-enabled draft
(so turns.py builds `sections` from the canonical live read) + a deliberately
stale git base_section_sha + a present anchor → 200 with a verified block whose
offsets were recomputed, NOT the client's.
"""

from __future__ import annotations

import pytest

from call_manager.db.models import WorkspaceEvent
from call_manager.proposal_assist.collab import state
from tests.test_api_workspaces import _app, _client, _seed

# Preamble (0) + one H2 section (1) with a UNIQUE anchor word "diagnostic".
_DOC = "# Title\npreamble\n## Aims\nPrecision medicine improves diagnostic outcomes.\n"


def _ack(pg_session_factory, ws_id, user_id):
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=user_id))
        s.commit()


def test_collab_modify_stale_git_sha_relocates_and_submits(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, _DOC)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()
    _ack(pg_session_factory, ws_id, owner_id)
    client = _client(app, owner_id)

    r = client.post(
        f"/api/workspaces/{ws_id}/turns",
        json={
            "content": "Shorten this.",
            "selection_context": {
                "request_type": "modify",
                "section_index": 1,
                "base_commit_sha": "0" * 40,
                "base_section_sha": "stale" * 8,  # git sha the room is ahead of
                "anchor": "diagnostic",           # still present + unique in the live section
                "from_offset": 999,               # bogus client offsets — must be recomputed
                "to_offset": 1009,
            },
        },
    )
    assert r.status_code == 200  # was 409 before the collab-branch wire
    turn = r.json()["user_turn"]
    blocks = turn["content"] if isinstance(turn["content"], list) else [turn["content"]]
    sel = [b for b in blocks if b.get("type") == "selection"][0]
    assert sel["verified"] is True and sel["request_type"] == "modify"
    assert sel["anchor"] == "diagnostic"
    assert (sel["from_offset"], sel["to_offset"]) != (999, 1009)  # recomputed live offsets
```

- [ ] **Step 2: Run — verify it FAILS (wire not yet added)**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_submit_turn_collab_relocation.py -p no:cacheprovider -q`
Expected: FAIL with `assert 409 == 200` — the collab branch does not yet pass `allow_anchor_relocation`, so the stale sha triggers the modify 409.

- [ ] **Step 3: Wire the collab-draft branch**

In `src/call_manager/web/api/turns.py`, replace the block that computes `sections` (currently `sections = None` then `if sc.target_artifact == "draft" and collab_state.is_collab_enabled(...)`) and the `build_selection_block(...)` call so a single `collab_draft` boolean drives both:

```python
            repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
            sections = None
            collab_draft = sc.target_artifact == "draft" and collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="draft"
            )
            if collab_draft:
                raw = canonical_artifact_markdown(
                    session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
                )
                sections = split_sections(raw)
            try:
                selection_block = build_selection_block(
                    repo_dir=repo_dir,
                    sel=SelectionInput(
                        request_type=sc.request_type,
                        section_index=sc.section_index,
                        base_commit_sha=sc.base_commit_sha,
                        base_section_sha=sc.base_section_sha,
                        anchor=sc.anchor,
                        from_offset=sc.from_offset,
                        to_offset=sc.to_offset,
                    ),
                    sections=sections,
                    allow_anchor_relocation=collab_draft,
                )
            except SelectionConflictError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
```

- [ ] **Step 4: Run — verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_submit_turn_collab_relocation.py -p no:cacheprovider -q`
Expected: PASS (200 + recomputed offsets).

- [ ] **Step 5: Run the flag-off route parity — verify unchanged**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_submit_turn_selection.py -p no:cacheprovider -q`
Expected: all PASS — flag-off turns still 409 on a dirty section and allow unverified ask (those tests never enable collab → `collab_draft=False` → old path).

- [ ] **Step 6: Lint + commit**

```bash
cd /home/parlar_ai/dev/call-monitor && ruff check src/call_manager/web/api/turns.py tests/proposal_assist/test_submit_turn_collab_relocation.py
git add src/call_manager/web/api/turns.py tests/proposal_assist/test_submit_turn_collab_relocation.py
git commit -m "$(cat <<'EOF'
feat(collab): wire allow_anchor_relocation from the collab turns branch (3b-5d.D1b)

A single collab_draft bool now drives both the canonical-live sections read and
allow_anchor_relocation, so a collab Edit modify with a stale git sha but a
present anchor submits (200) instead of 409ing. Route test pins the wire that a
build_selection_block unit test can't.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: e2e acceptance — scenario (e): co-edit → cite-without-save

**Files:**
- Modify: `frontend/e2e/collab.spec.ts` (add scenario (e) inside the existing describe block, after scenario (d))

**Interfaces:**
- Consumes: the existing `openAs`, `WS`/`COOKIE_*` env, and the seeded citeable reference + "diagnostic" claim from `scripts/collab_e2e_seed.py`. Reuses the `citation-pill` / composer selectors from scenario (c).

- [ ] **Step 1: Add scenario (e)**

Insert before the closing `});` of the `test.describe("collab two-browser (distinct users)", ...)` block in `frontend/e2e/collab.spec.ts`:

```typescript
  // (e) Cite after a co-edit with NO save-first — the D1b acceptance gate. Both users
  //     edit the draft so the live room is ahead of git HEAD; the toolbar's git
  //     base_section_sha is now stale, but the cite still lands because the backend
  //     relocates by anchor (3b-5d.D1b). Inverse of (c), which checkpoints to a
  //     git==live base first to AVOID this path.
  test("owner cites after both users edit without saving; the pill converges", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Both append at the document end (the Aims section, which holds "diagnostic"),
    // so that section's live sha diverges from git — with NO "Save a version".
    await edA.click();
    await A.page.keyboard.press("Control+End");
    await A.page.keyboard.type(" Owner note.");
    await expect(edB).toContainText("Owner note", { timeout: 8_000 });
    await edB.click();
    await B.page.keyboard.press("Control+End");
    await B.page.keyboard.type(" Member note.");
    await expect(edA).toContainText("Member note", { timeout: 8_000 });

    // Cite "diagnostic" WITHOUT saving first. Pre-D1b this 409s (git sha != live sha).
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    await chunk.selectText();
    await chunk.dispatchEvent("mouseup");
    await composer.getByRole("button", { name: /add citation/i }).click();

    // The pill lands for the owner and converges to the member — no save was needed.
    await expect(edA.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });
    await expect(edB.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });

    await A.ctx.close();
    await B.ctx.close();
  });
```

- [ ] **Step 2: Typecheck the e2e spec**

Run: `cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit`
Expected: exit 0 (no type errors introduced).

- [ ] **Step 3: Run the full two-browser e2e suite (the D1b acceptance run)**

Run: `cd /home/parlar_ai/dev/call-monitor && bash scripts/collab_e2e_run.sh`
Expected: all five scenarios (a–e) PASS; scenario (e) proves the co-edit → cite-without-save path; teardown leaves ports free and 0 PG containers.

If Control+End does not move the caret to the document end reliably headless (Tiptap/ProseMirror should honor it), fall back to clicking the last visible paragraph before typing; do NOT weaken the assertion. If the run flakes on timing (single-box contention, per the D0 note), re-run scenario (e) in isolation with `--grep "cites after both users edit"`.

- [ ] **Step 4: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/e2e/collab.spec.ts
git commit -m "$(cat <<'EOF'
test(collab): e2e scenario (e) — cite after a co-edit with no save-first (3b-5d.D1b)

Both users edit the draft so the live room is ahead of git HEAD, then the owner
cites "diagnostic" WITHOUT saving; the citation-pill converges to both clients.
The direct inverse of scenario (c) (which checkpoints to a git==live base first).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Full verification gate + findings note + STOP for user-gated merge

**Files:**
- Modify: `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` (append a D1b outcome section)

- [ ] **Step 1: Backend grouped sweep (always include the OpenAPI spec test)**

Run:
```bash
cd /home/parlar_ai/dev/call-manager 2>/dev/null; cd /home/parlar_ai/dev/call-monitor
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist tests/test_api_spec.py -p no:cacheprovider -q > /tmp/d1b-pytest.log 2>&1; echo "PYTEST_EXIT=$?"; tail -25 /tmp/d1b-pytest.log
```
Expected: `PYTEST_EXIT=0`, all passed/skipped, no failures. (If a testcontainers connection-refused flake appears, re-run the flagged file in isolation — that is infra, not a regression.)

- [ ] **Step 2: Lint the whole change surface**

Run: `cd /home/parlar_ai/dev/call-monitor && ruff check .`
Expected: exit 0, "All checks passed!".

- [ ] **Step 3: Frontend gates (no source changed, but the e2e spec did)**

Run: `cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit && pnpm exec vitest run src/collab`
Expected: tsc exit 0; collab vitest all pass (unchanged — no frontend source touched).

- [ ] **Step 4: Prove flag-off parity explicitly (diff review)**

Run: `git -C /home/parlar_ai/dev/call-monitor diff master...HEAD -- src/`
Confirm by inspection: every behavioral change is gated behind a non-default param value that only `direct_cite_via_kernel` (`enforce_section_sha=False`) and the `collab_draft` branch of `turns.py` (`allow_anchor_relocation=True`) pass. No other caller changed; no schema/migration; `git diff --name-only master...HEAD` shows no `migrations/` or `models.py`.

- [ ] **Step 5: Append the D1b outcome to the findings note**

Add a `## D1b outcome (2026-07-07)` section to `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` summarizing: the two backend gates converted to anchor-relocation; flag-off byte-for-byte; the five-scenario e2e (incl. new (e)); tests added; and that D2 (relay-spoof filter), D3 (analysis re-fire), D4 (the flip) remain before the flip.

- [ ] **Step 6: Commit the note**

```bash
cd /home/parlar_ai/dev/call-monitor
git add docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md
git commit -m "$(cat <<'EOF'
docs(collab): record D1b outcome (stale-snapshot 409 fixed; co-edit cite green) (3b-5d.D1b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: STOP — present the merge decision (user-gated)**

Do NOT merge. Summarize: branch `collab-slice3b5d1b`, commits, diff surface (backend + tests + e2e + docs; no schema/migration), all gates green. Offer: (1) Codex-review the diff first; (2) merge now (local `--no-ff`); (3) hold. Wait for the user's explicit choice.

---

## Self-Review

**1. Spec coverage.** Component A (§5) → Task 1. Component B core (§6) → Task 2. Component B wiring (§6 + Codex-Medium finding) → Task 3. Testing §10 unit → Task 2; route/integration → Tasks 1+3; e2e (e) → Task 4. Parity §7 → Tasks 1 Step 7, 3 Step 5, 5 Step 4. Anchor-shape/short-span (Codex-Low) → Task 2 `test_collab_relocation_modify_verifies_short_unique_span` + `..._ambiguous_raises`. Global constraints §11 → header + Task 5. No gaps.

**2. Placeholder scan.** No TBD/TODO; every code step shows complete code; every test step shows the assertion; every run step gives the exact command + expected output. The single conditional (Task 4 Step 3 Control+End fallback) specifies the exact fallback and forbids weakening the assertion.

**3. Type consistency.** `enforce_section_sha: bool = True` (Task 1) used identically in the kernel guard and the `direct_cite_via_kernel` call. `allow_anchor_relocation: bool = False` (Task 2) matches the `turns.py` call (Task 3) and the unit tests. `_relocated_selection_block(sel, live) -> dict` returns the exact block shape of the existing verified/unverified branches. `anchor_occurrences(text, anchor) -> list[int]` used with `len(occ)` semantics consistent with the cite path.
