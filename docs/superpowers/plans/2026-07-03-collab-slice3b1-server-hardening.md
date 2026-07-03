# Collab Slice 3b-1 — Server Hardening + Write-Path Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining server-side collab flag-flip prerequisites — bounded log replay, an unfoolable section-map guard, evict-safe rooms, and a real-`y-prosemirror`-verified incremental write path — entirely server-side, flag OFF.

**Architecture:** Four independent components over the existing `collab/` modules: (1) compact the durable Yjs log on checkpoint by reusing the lowest merged-range id so replay stays merged-first; (2) replace the section-map count check with positional attribution; (3) tombstone evicted rooms and recheck at every persist/broadcast site; (4) extend the existing Node/`y-prosemirror` write-path harness with incremental `apply_pm_section_to_fragment` fixtures. No schema, no migration, no frontend runtime, flag stays OFF.

**Tech Stack:** Python 3.12, `uv`, `ruff`, `pytest` (+ testcontainers Postgres), pycrdt, FastAPI; Node (`yjs`/`y-prosemirror`) for the gated write-path harness. Design spec: `docs/superpowers/specs/2026-07-03-collab-slice3b1-server-hardening-design.md`.

## Global Constraints

- **The collab flag STAYS OFF in prod.** Every new behavior is reachable only when `is_collab_enabled(...)` is true, which prod never sets.
- **Flag-off parity is a hard gate.** `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, and the member/rubric/soft-delete suites MUST stay green **unchanged**. The flag-off accept / staleness / spec-lock / citation paths must be byte-for-byte identical after this slice.
- **No migration.** Component 1 reuses `yjs_updates` + the existing `last_checkpoint_update_id` watermark; Components 2/3 are logic-only; Component 4 is a dev harness. No DB schema touch.
- **No frontend runtime.** The Tiptap editor is Slice 3b-3. Component 4's Node harness is a build/validation script, not shipped UI.
- Run pytest with `EMBEDDING_MODEL=bge-m3`; verify via grouped-dir runs (`tests/proposal_assist/collab/`, `tests/proposal_assist/`, `tests/test_api_spec_lock.py`), never the whole suite (testcontainers PG flake under load); `-p no:cacheprovider`.
- CI runs `ruff check .` (NOT `ruff format`). Keep imports sorted (I001), avoid nested-`with` (SIM117), match each file's existing style.
- Collab tests live under `tests/proposal_assist/collab/`.
- Infra noise (`psycopg ... was deleted while still open` ResourceWarning; testcontainers `connection refused` under load) is NOT a test failure — re-run the affected grouped dir in isolation. Do NOT "fix" product code to chase it.

## File Structure

**Modified files:**
- `collab/store.py` — add `compact_snapshot(...)` (overwrite the lowest-id row + delete the other snapshotted ids).
- `collab/checkpoint.py` — call `compact_snapshot` inside `checkpoint_artifact`'s existing txn/mutex on the authored main path.
- `collab/yjs_bridge.py` — `apply_pm_section_to_fragment`: replace the count check with positional attribution.
- `collab/room.py` — `Room.evicted` flag; `evict()` sets it; `Room.handle` rechecks it.
- `proposals/kernel_accept.py` — `RoomEvicted` error; recheck in `_body` before commit + before the post-lock broadcast; add `RoomEvicted` to `_CLEAN_ACCEPT_ERRORS`.
- `web/proposals.py` — map `RoomEvicted` → 409 retry on the collab-on accept branch.
- `tests/proposal_assist/collab/test_collab_writepath.py` — incremental producer corpus.
- `frontend/scripts/verify_python_writepath.mjs` — incremental verify.

**New files:**
- `tests/proposal_assist/collab/test_collab_compaction.py` — compaction behavior + convergence.
- `tests/proposal_assist/collab/test_yjs_section_attribution.py` — positional-attribution divergence tests.
- `tests/proposal_assist/collab/test_collab_evict_tombstone.py` — tombstone + recheck tests.
- `docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md` — write-path validation result.

---

## Task 1: `store.compact_snapshot` helper (id-reuse squash)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/store.py`
- Test: `tests/proposal_assist/collab/test_collab_compaction.py`

**Interfaces:**
- Consumes: `YjsUpdate` (columns `id`, `workspace_id`, `artifact`, `actor_id`, `update_bytes`); `store.append_update`, `store.load_updates`, `store.load_rows`.
- Produces: `compact_snapshot(session, *, workspace_id: uuid.UUID, artifact: str, min_id: int, merged_update: bytes, delete_ids: list[int]) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_collab_compaction.py`:

```python
import uuid

from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.canonical import build_doc
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from tests.proposal_assist.conftest import make_user, make_workspace


def _append(pg_session_factory, wid, md, actor_id=None):
    upd = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=upd)
        s.commit()


def _mk_ws(pg_session_factory, email):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=email))
        s.commit()
        return ws.id


def test_compact_snapshot_reuses_min_id_and_preserves_doc(pg_session_factory):
    wid = _mk_ws(pg_session_factory, "compact-min@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n\n## B\n\nBody b.\n")
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 2
    before = ydoc_to_pm_json(build_doc([b for (_, _, b) in rows]))
    min_id = rows[0][0]
    merged = build_doc([b for (_, _, b) in rows]).get_update()
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=min_id,
            merged_update=merged, delete_ids=[rows[1][0]],
        )
        s.commit()
    with pg_session_factory() as s:
        after_rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        after_updates = store.load_updates(s, workspace_id=wid, artifact="draft")
    assert [r[0] for r in after_rows] == [min_id]          # only the reused row survives
    assert ydoc_to_pm_json(build_doc(after_updates)) == before   # doc unchanged


def test_compact_snapshot_merged_replays_before_a_lower_tail(pg_session_factory):
    # THE ordering regression (Codex High): a tail row can end up with an id LOWER
    # than a naive new merged row; the id-reuse design keeps the merged row at
    # min_id so it ALWAYS replays first. Simulate a tail that committed during the
    # checkpoint window (id > max_id) and assert convergence.
    wid = _mk_ws(pg_session_factory, "compact-order@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")                     # id 1
    _append(pg_session_factory, wid, "## A\n\nBody a.\n\n## B\n\nBody b.\n")  # id 2 (snapshot max)
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    min_id, max_id = rows[0][0], rows[-1][0]
    merged = build_doc([b for (_, _, b) in rows]).get_update()
    # A concurrent client append lands AFTER the snapshot (id > max_id) = tail.
    _append(pg_session_factory, wid, "## A\n\nBody a EDITED.\n\n## B\n\nBody b.\n")
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=min_id,
            merged_update=merged, delete_ids=[max_id],
        )
        s.commit()
    with pg_session_factory() as s:
        after = store.load_updates(s, workspace_id=wid, artifact="draft")
        after_rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    # merged row kept min_id, tail kept its higher id → ORDER BY id = [merged, tail].
    assert after_rows[0][0] == min_id and after_rows[-1][0] > max_id
    # Replays without loss (merged→tail converges; the bug shape [tail, merged] would not).
    assert "EDITED." in pm_to_markdown(ydoc_to_pm_json(build_doc(after)))


def test_compact_snapshot_does_not_clobber_unlisted_rows(pg_session_factory):
    wid = _mk_ws(pg_session_factory, "compact-noclobber@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a2.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a3.\n")
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    # Compact only the first two; the third must survive (delete by explicit id).
    merged = build_doc([rows[0][2], rows[1][2]]).get_update()
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=rows[0][0],
            merged_update=merged, delete_ids=[rows[1][0]],
        )
        s.commit()
    with pg_session_factory() as s:
        after = [r[0] for r in store.load_rows(s, workspace_id=wid, artifact="draft")]
    assert after == [rows[0][0], rows[2][0]]   # min_id (reused) + the untouched third
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_compaction.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: module ... store has no attribute 'compact_snapshot'`.

- [ ] **Step 3: Implement `compact_snapshot`**

In `collab/store.py`, extend the imports and add the helper:

```python
from sqlalchemy import delete as sa_delete, select, update as sa_update
```

(the existing `from sqlalchemy import select` becomes the line above — keep imports sorted, I001).

```python
def compact_snapshot(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    min_id: int,
    merged_update: bytes,
    delete_ids: list[int],
) -> None:
    """Squash a checkpointed prefix of the log into ONE full-state row WITHOUT
    changing replay order. The merged full-state update OVERWRITES the row with
    the LOWEST id in the snapshot (``min_id``), so it keeps an id below every tail
    row (tail ids are all > max_id >= min_id) and ``load_updates`` (ORDER BY id)
    always replays merged-before-tail. pycrdt does NOT converge tail-before-merged,
    so this ordering is load-bearing (Slice 3b-1). The other merged rows are
    deleted by EXPLICIT id (never a blanket ``id <= max_id``), so a row that
    commits after the snapshot is never clobbered. Stages on the caller's txn."""
    session.execute(
        sa_update(YjsUpdate)
        .where(
            YjsUpdate.workspace_id == workspace_id,
            YjsUpdate.artifact == artifact,
            YjsUpdate.id == min_id,
        )
        .values(update_bytes=merged_update)
    )
    if delete_ids:
        session.execute(
            sa_delete(YjsUpdate).where(
                YjsUpdate.workspace_id == workspace_id,
                YjsUpdate.artifact == artifact,
                YjsUpdate.id.in_(delete_ids),
            )
        )
    session.flush()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_compaction.py -q -p no:cacheprovider`
Expected: PASS (3 tests).

- [ ] **Step 5: Ruff + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_compaction.py`
Expected: `All checks passed!`

```bash
git add src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_compaction.py
git commit -m "feat(collab): store.compact_snapshot — id-reuse log squash (merged replays first)"
```

---

## Task 2: Wire compaction into `checkpoint_artifact`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py` (`checkpoint_artifact`, insert after `advance_checkpoint`, before the final `return`, lines ~132-139)
- Test: `tests/proposal_assist/collab/test_collab_compaction.py` (add cases)

**Interfaces:**
- Consumes: `store.compact_snapshot(...)` (Task 1); the existing `rows` snapshot + `doc` in `checkpoint_artifact`.
- Produces: no new symbols; `checkpoint_artifact` now compacts the log it just checkpointed.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_compaction.py` (reuse `_append`/`_mk_ws` above; import checkpoint + `_init_repo`/`state`):

```python
from pathlib import Path

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from tests.proposal_assist.collab.test_collab_checkpoint import _init_repo


def _enable(pg_session_factory, wid):
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()


def test_checkpoint_compacts_log_to_single_row(pg_session_factory, tmp_path):
    wid = _mk_ws(pg_session_factory, "cp-compact@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\n\nseed.\n")
    _enable(pg_session_factory, wid)
    # Two AUTHORED edits (actor_id set → checkpoint commits + compacts).
    _append(pg_session_factory, wid, "## A\n\nedit one.\n", actor_id=1)
    _append(pg_session_factory, wid, "## A\n\nedit one.\n\n## B\n\nedit two.\n", actor_id=1)
    with pg_session_factory() as s:
        before = ydoc_to_pm_json(
            build_doc(store.load_updates(s, workspace_id=wid, artifact="draft"))
        )
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        after = ydoc_to_pm_json(
            build_doc(store.load_updates(s, workspace_id=wid, artifact="draft"))
        )
    assert len(rows) == 1                # collapsed to the single merged row
    assert after == before               # doc unchanged by compaction


def test_checkpoint_single_row_no_compaction_noop(pg_session_factory, tmp_path):
    wid = _mk_ws(pg_session_factory, "cp-single@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\n\nseed.\n")
    _enable(pg_session_factory, wid)
    _append(pg_session_factory, wid, "## A\n\none edit.\n", actor_id=1)
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 1   # a single-row log is left as-is (no pointless rewrite)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_compaction.py -k checkpoint -q -p no:cacheprovider`
Expected: FAIL — `test_checkpoint_compacts_log_to_single_row` asserts `len(rows) == 1` but the log still has 2+ rows (no compaction yet).

- [ ] **Step 3: Insert compaction into `checkpoint_artifact`**

In `collab/checkpoint.py`, on the authored main path, after the `state.advance_checkpoint(...)` call and before the final `return CheckpointResult(...)` (lines ~132-139), add:

```python
    # Compact the just-checkpointed prefix into the merged full-state (Slice 3b-1),
    # in THIS transaction/mutex. Reuse the lowest snapshot id so replay stays
    # merged-first; delete the other snapshotted ids explicitly. Guard >= 2 rows
    # (a single-row log has nothing to collapse and must not be pointlessly
    # rewritten). `doc` is the merged doc already built above.
    if len(rows) >= 2:
        store.compact_snapshot(
            session,
            workspace_id=workspace_id,
            artifact=artifact,
            min_id=rows[0][0],
            merged_update=doc.get_update(),
            delete_ids=[rid for (rid, _, _) in rows[1:]],
        )
```

`store` and `rows`/`doc` are already in scope (`store` imported at line 20, `rows` from `load_rows`, `doc` from `build_doc`). The two early-return no-op paths (`max_id <= watermark or head == ""`, and the unauthored seed-only path) do NOT build a doc and are ≤1 meaningful row — they legitimately skip compaction.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_compaction.py -q -p no:cacheprovider`
Expected: PASS (all Task-1 + Task-2 cases).

- [ ] **Step 5: Existing checkpoint-suite regression + ruff + commit**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint_triggers.py tests/proposal_assist/collab/test_collab_store_checkpoint.py -q -p no:cacheprovider`
Expected: PASS unchanged (compaction is transparent to the checkpoint's git/provenance behavior).

Run: `uv run ruff check src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_compaction.py`
Expected: `All checks passed!`

```bash
git add src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_compaction.py
git commit -m "feat(collab): checkpoint_artifact compacts the log it checkpoints (bounded replay)"
```

---

## Task 3: Section-map positional line-attribution

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/yjs_bridge.py` (`apply_pm_section_to_fragment`, the count-check block ~lines 319-329)
- Test: `tests/proposal_assist/collab/test_yjs_section_attribution.py`

**Interfaces:**
- Consumes: `split_sections`, `pm_to_markdown`, `_block_starts_section`, `SectionMapDivergenceError` (all in `yjs_bridge.py`).
- Produces: no new symbols; the guard now verifies partitions coincide, not just counts.

- [ ] **Step 1: Write the regression tests (positive: no spurious raise)**

Create `tests/proposal_assist/collab/test_yjs_section_attribution.py`. The positive suite pins that the stricter guard does NOT false-raise on any normal or `##NoSpace`/no-preamble doc (the existing `test_yjs_section_replace.py` cases plus a fenced-`##` doc):

```python
import pytest

from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    SectionMapDivergenceError,
    apply_pm_section_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections


def _blocks_of(md):
    return markdown_to_pm(md).get("content") or []


@pytest.mark.parametrize("md", [
    "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n",
    "## A\n\nBody a.\n\n## B\n\nBody b.\n",                    # no preamble
    "Intro.\n\n##NoSpace para\n\n## A\n\nBody.\n",             # ##NoSpace phantom
    "## Methods\n\n```\n## not a heading\n```\n\nReal body.\n",  # fenced ## (not a boundary)
])
def test_attribution_no_spurious_raise_on_valid_docs(md):
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    n = len(split_sections(pm_to_markdown(ydoc_to_pm_json(doc))))
    # Editing the LAST section must apply cleanly (exercises the attribution path).
    update = apply_pm_section_to_fragment(
        doc, section_index=n - 1, blocks=_blocks_of("## Z\n\nEdited last.\n"),
    )
    assert update


def test_attribution_still_raises_on_hard_break_phantom():
    # The 3a hard_break phantom (split_sections=3 vs block-map=2) still raises —
    # now caught by attribution (and the count too).
    phantom = [
        {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "x"}, {"type": "hard_break"},
            {"type": "text", "text": "## FAKE"},
        ]},
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "B"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Body b."}]},
    ]
    doc = pm_json_to_ydoc({"type": "doc", "content": phantom})
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))
```

- [ ] **Step 2: Run the tests to verify current behavior**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_attribution.py -q -p no:cacheprovider`
Expected: PASS against the CURRENT count-check code (these pin behavior that must be PRESERVED). If any `_no_spurious_raise` case FAILS, the attribution you write in Step 3 is too strict — that is the signal to fix the normalization, not the test.

- [ ] **Step 3: Replace the count check with positional attribution**

In `yjs_bridge.py` `apply_pm_section_to_fragment`, replace the count-check block (the `n_line_sections = len(split_sections(...))` line and its `if len(starts) != n_line_sections:` raise) with:

```python
        # Positional attribution (Slice 3b-1): a matching COUNT is necessary but
        # NOT sufficient — a compensating pair (one phantom block-boundary + one
        # hidden real line-boundary) nets to the same count yet misaligns the
        # partitions, splicing the wrong section. Verify the two partitions
        # COINCIDE: each block range must serialize to the SAME markdown as the
        # correspondingly-indexed split_sections section. Trailing newlines are
        # normalized away because split_sections attaches the inter-section blank
        # line to the PRECEDING section, which the per-range serialization drops.
        line_sections = split_sections(pm_to_markdown(pm_json))
        ends = starts[1:] + [len(blocks_json)]
        if len(starts) != len(line_sections):
            raise SectionMapDivergenceError(
                f"section map divergence: {len(line_sections)} line-scan sections "
                f"vs {len(starts)} block-boundary sections"
            )
        for k, (s_i, e_i) in enumerate(zip(starts, ends)):
            range_md = (
                pm_to_markdown({"type": "doc", "content": blocks_json[s_i:e_i]})
                if e_i > s_i
                else ""
            )
            if range_md.rstrip("\n") != line_sections[k].text.rstrip("\n"):
                raise SectionMapDivergenceError(
                    f"section {k} block-range does not match its line-scan section — "
                    "a boundary falls inside a multi-line block; re-edit the paragraph"
                )
```

- [ ] **Step 4: Run the positive tests + the full section-replace suite**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_attribution.py tests/proposal_assist/collab/test_yjs_section_replace.py -q -p no:cacheprovider`
Expected: PASS (no spurious raise on any valid doc; the hard_break phantom still raises; all 3a section-replace cases green).

- [ ] **Step 5: Add the compensating-divergence test (empirical RED-first)**

The compensating case (matching count, misaligned partitions) depends on this serializer's fence/hard_break behavior and MUST be constructed empirically. In `test_yjs_section_attribution.py`, build a PM doc whose block-map count EQUALS the split_sections count but whose partitions differ — e.g. a doc with a hard_break-`## FAKE` phantom in one paragraph AND a genuine `## H` heading arranged so `_block_starts_section` count == `split_sections` count. Assert (a) the counts are equal (so the old check would PASS) and (b) `apply_pm_section_to_fragment` now raises `SectionMapDivergenceError` via attribution:

```python
def test_attribution_catches_compensating_divergence():
    # Construct empirically: counts must MATCH (old guard would pass) while the
    # partitions misalign. Verify the precondition explicitly; if it cannot be
    # constructed against this serializer, the attribution is defense-in-depth and
    # the count check already suffices for reachable inputs — record that as a
    # finding in the commit message rather than asserting a vacuous pass.
    from call_manager.proposal_assist.collab.yjs_bridge import _block_starts_section
    blocks = [
        # section 0 preamble holds a hidden real boundary (hard_break + "## X"):
        {"type": "paragraph", "content": [
            {"type": "text", "text": "pre"}, {"type": "hard_break"},
            {"type": "text", "text": "## X"},
        ]},
        # a real heading whose first line IS "## Y":
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Y"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "body y"}]},
    ]
    doc = pm_json_to_ydoc({"type": "doc", "content": blocks})
    pm_json = ydoc_to_pm_json(doc)
    blocks_json = pm_json.get("content") or []
    starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
    n_line = len(split_sections(pm_to_markdown(pm_json)))
    if len(starts) == n_line:
        # Precondition met (counts match) → attribution MUST catch the misalignment.
        with pytest.raises(SectionMapDivergenceError):
            apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## Y\n\nz.\n"))
    else:
        # Counts differ here → the count check already catches it; the compensating
        # shape needs a different construction. Assert the divergence IS caught
        # (either way the guard must refuse), and note it for the reviewer.
        with pytest.raises(SectionMapDivergenceError):
            apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## Y\n\nz.\n"))
```

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_attribution.py::test_attribution_catches_compensating_divergence -q -p no:cacheprovider`
Expected: PASS. If the `len(starts) == n_line` branch is NOT taken (counts differ for this construction), note in the commit message that a true compensating case could not be constructed against this serializer and the attribution is defense-in-depth.

- [ ] **Step 6: Ruff + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_attribution.py`
Expected: `All checks passed!`

```bash
git add src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_attribution.py
git commit -m "fix(collab): section-map positional attribution (catch compensating divergence)"
```

---

## Task 4: Evict-mid-op zombie window — tombstone + recheck

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`Room.__init__`, `evict`, `Room.handle`)
- Modify: `src/call_manager/proposal_assist/proposals/kernel_accept.py` (`RoomEvicted`, `_body` recheck, broadcast recheck, `_CLEAN_ACCEPT_ERRORS`)
- Modify: `src/call_manager/proposal_assist/web/proposals.py` (map `RoomEvicted` → 409)
- Test: `tests/proposal_assist/collab/test_collab_evict_tombstone.py`

**Interfaces:**
- Consumes: `Room.clients`, `Room._lock`, `RoomManager.evict`, `MANAGER.operate`, `accept_via_kernel`.
- Produces:
  - `Room.evicted: bool` (default `False`; set `True` by `evict()`).
  - `RoomEvicted(Exception)` in `kernel_accept.py` (in `_CLEAN_ACCEPT_ERRORS`).

**Reviewer note (for the plan-Codex-review):** the always-present path is `Room.handle` (client updates) — fully closed by the under-lock recheck. The kernel↔kernel race (two concurrent accepts where the first fails and evicts) is narrow: `_body`s are serialized by `room.lock`, and the second op's recheck-before-commit sees the tombstone the first op's evict set (evict runs on the event loop during the second op's `await run_in_threadpool`). The residual (evict fires between the second op's recheck and its commit) is vanishingly small under the single event loop; flag is OFF. Scrutinize this ordering.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_evict_tombstone.py`:

```python
import pytest

from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from tests.proposal_assist.collab.test_collab_room import FakeClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_evict_sets_tombstone(pg_session_factory):
    import uuid
    key = (uuid.uuid4(), "draft")

    async def _load():
        return []

    client = FakeClient()
    room = await MANAGER.acquire(key, load=_load, client=client,
                                 state=ClientState(user_id=1, can_write=True))
    assert room.evicted is False
    await MANAGER.evict(key)
    assert room.evicted is True          # tombstoned
    assert client.closed is not None      # socket force-closed


@pytest.mark.anyio
async def test_handle_drops_write_on_tombstoned_room(pg_session_factory):
    # A write frame arriving after eviction must NOT apply or persist.
    import uuid

    from call_manager.proposal_assist.collab import protocol as P
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc

    key = (uuid.uuid4(), "draft")

    async def _load():
        return []

    client = FakeClient()
    room = await MANAGER.acquire(key, load=_load, client=client,
                                 state=ClientState(user_id=1, can_write=True))
    room.evicted = True
    persisted = []

    async def _persist(uid, update):
        persisted.append(update)

    upd = pm_json_to_ydoc(markdown_to_pm("## A\n\nx.\n")).get_update()
    await room.handle(sender=client, state=room.clients[client],
                      message=P.create_update_message(upd), persist=_persist)
    assert persisted == []   # tombstoned room did not persist the write
    await MANAGER.evict(key)
```

(Confirm the exact write-frame constructor name against `protocol.py` — `create_update_message` is used by `room.handle`'s own broadcast and by `kernel_accept.py`; if `handle` classifies via `is_sync_write`, use the frame shape the existing `test_collab_room.py` write tests build.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_evict_tombstone.py -q -p no:cacheprovider`
Expected: FAIL — `Room` has no `evicted` attribute; the write is persisted (no recheck yet).

- [ ] **Step 3: Add `Room.evicted` + set it in `evict` + recheck in `handle`**

In `collab/room.py` `Room.__init__`, add after `self.poisoned = False`:

```python
        self.evicted = False   # tombstone: in-flight ops recheck & abort (Slice 3b-1)
```

In `RoomManager.evict`, set the tombstone after popping and before closing clients:

```python
    async def evict(self, key: RoomKey) -> None:
        async with self._lock:
            room = self._rooms.pop(key, None)
        if room is None:
            return
        room.evicted = True   # tombstone: block any in-flight op's persist/broadcast (Slice 3b-1)
        for client in list(room.clients):
            with suppress(Exception):
                await client.close(code=_WS_INTERNAL_ERROR)
```

In `Room.handle`, the `is_sync_write` branch — add the recheck inside the lock (beside the existing `can_write` re-check, before `apply_update`) and before the post-lock broadcast:

```python
            async with self._lock:
                if not state.can_write:
                    return
                if self.evicted:
                    return  # room tombstoned mid-flight — drop (no apply/persist/broadcast)
                try:
                    self.doc.apply_update(update)
                ...
                try:
                    await persist(state.user_id, update)
                except Exception as e:
                    self.poisoned = True
                    raise PersistError(str(self.key)) from e
            if self.evicted:
                return  # evicted after persist — skip broadcast (sockets already closed)
            await self._broadcast(P.create_update_message(update), exclude=sender)
            return
```

- [ ] **Step 4: Add `RoomEvicted` + kernel-path rechecks + route mapping**

In `proposals/kernel_accept.py`, define the error near the top and add it to `_CLEAN_ACCEPT_ERRORS`:

```python
class RoomEvicted(Exception):
    """The room was evicted (poison recovery) while this accept was in flight.
    Pre-commit abort: the pc stays pending; a fresh connect rebuilds from the log."""
```

```python
_CLEAN_ACCEPT_ERRORS = (
    SpecLockedError,
    SectionStructureError,
    UnsupportedKernelKind,
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    CommitMutexTimeout,
    IndexError,
    RoomEvicted,
)
```

In `_body`, recheck before `s.commit()` (the kernel already mutated `room.doc`; refusing to commit means the durable log is untouched and the mutation is discarded with the detached room):

```python
                        else:
                            raise UnsupportedKernelKind(kind)
                        if room.evicted:
                            raise RoomEvicted(str(key))  # tombstoned mid-accept — abort, pc pending
                        s.commit()
                        s.expunge(res.pc)
                        return res
```

Recheck before the post-lock broadcast:

```python
            if result.update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(result.update))
            return result.pc
```

In `web/proposals.py`, import `RoomEvicted` and map it on the collab-on accept branch (place beside the `CommitMutexTimeout` handler):

```python
        except RoomEvicted as exc:
            raise HTTPException(
                status_code=409, detail="workspace busy; retry the accept"
            ) from exc
```

- [ ] **Step 5: Run the tombstone tests + the evict/operate/kernel-accept regression suites**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_evict_tombstone.py tests/proposal_assist/collab/test_collab_room.py tests/proposal_assist/collab/test_collab_room_operate.py tests/proposal_assist/collab/test_kernel_accept_evict.py tests/proposal_assist/collab/test_collab_kernel_accept.py -q -p no:cacheprovider`
Expected: PASS (new tombstone behavior; existing evict/operate/accept semantics unchanged — the recheck is a no-op when `evicted` is False).

- [ ] **Step 6: Ruff + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/room.py src/call_manager/proposal_assist/proposals/kernel_accept.py src/call_manager/proposal_assist/web/proposals.py tests/proposal_assist/collab/test_collab_evict_tombstone.py`
Expected: `All checks passed!`

```bash
git add src/call_manager/proposal_assist/collab/room.py src/call_manager/proposal_assist/proposals/kernel_accept.py src/call_manager/proposal_assist/web/proposals.py tests/proposal_assist/collab/test_collab_evict_tombstone.py
git commit -m "fix(collab): tombstone evicted rooms + recheck at every persist/broadcast site"
```

---

## Task 5: Extend the write-path harness with incremental fixtures

**Files:**
- Modify: `tests/proposal_assist/collab/test_collab_writepath.py` (incremental producer corpus)
- Modify: `frontend/scripts/verify_python_writepath.mjs` (incremental verify)
- Create: `docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md`

**Interfaces:**
- Consumes: `pm_json_to_ydoc`, `apply_pm_section_to_fragment`, `markdown_to_pm`, `ydoc_to_pm_json`, `build_doc`; the existing `goldens/python_writepath.json` + `REGEN_WRITEPATH` pattern; the Node `reconstruct`/`compareAll` exports.
- Produces: a committed incremental corpus + a findings note recording the GO/NO-GO.

**Context:** The existing pair validates FULL-STATE Python docs against real `y-prosemirror`. This task adds INCREMENTAL fixtures: apply a full-state base then an `apply_pm_section_to_fragment` update, and assert the reconstructed PM. The Node side stays out of pytest CI (REGEN-gated, like the existing corpus).

- [ ] **Step 1: Add the incremental producer + a Python structural guard**

In `test_collab_writepath.py`, add an incremental corpus. Each entry is `{name, pm_json (expected post-edit), base_b64 (full-state of the pre-edit doc), incr_b64 (apply_pm_section_to_fragment output)}`, written to a sibling golden `goldens/python_writepath_incremental.json` under `REGEN_WRITEPATH`. Cases: `replace_middle`, `grow_blocks`, `shrink_blocks`, `clear_blocks_empty` (`blocks=[]`), `clear_preamble` (`blocks=[]` on section 0). Add a Python structural guard test that applies base+incr via `build_doc` and asserts the reconstructed PM equals the expected (the same non-byte-determinism approach the existing `test_writepath_committed_bytes_reconstruct_pm` uses):

```python
INCREMENTAL = GOLDENS / "python_writepath_incremental.json"

_INCR_CASES = [
    ("replace_middle", "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, "## A\n\nA EDITED.\n"),
    ("grow_blocks",    "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, "## A\n\nP1.\n\nP2.\n"),
    ("shrink_blocks",  "Pre.\n\n## A\n\nP1.\n\nP2.\n\n## B\n\nBody b.\n", 1, "## A\n\nOnly.\n"),
    ("clear_blocks_empty", "Pre.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n", 1, ""),
    ("clear_preamble", "Pre.\n\n## A\n\nBody a.\n", 0, ""),
]


def _build_incremental_entries():
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_section_to_fragment

    entries = []
    for name, base_md, idx, new_md in _INCR_CASES:
        doc = pm_json_to_ydoc(markdown_to_pm(base_md), client_id=_CLIENT_ID)
        base = doc.get_update()
        blocks = (markdown_to_pm(new_md).get("content") or []) if new_md.strip() else []
        incr = apply_pm_section_to_fragment(doc, section_index=idx, blocks=blocks)
        entries.append({
            "name": name,
            "pm_json": ydoc_to_pm_json(doc),
            "base_b64": base64.b64encode(base).decode("ascii"),
            "incr_b64": base64.b64encode(incr).decode("ascii"),
        })
    return entries


def _maybe_regenerate_incremental():
    if not os.environ.get("REGEN_WRITEPATH"):
        return
    INCREMENTAL.write_text(json.dumps(_build_incremental_entries(), indent=2) + "\n")


def test_incremental_committed_bytes_reconstruct_pm():
    """Committed base+incremental bytes reconstruct the expected post-edit PM."""
    _maybe_regenerate_incremental()
    assert INCREMENTAL.exists(), "python_writepath_incremental.json missing; REGEN_WRITEPATH=1"
    for entry in json.loads(INCREMENTAL.read_text()):
        doc = build_doc([base64.b64decode(entry["base_b64"]), base64.b64decode(entry["incr_b64"])])
        assert ydoc_to_pm_json(doc) == entry["pm_json"], entry["name"]
```

- [ ] **Step 2: Regenerate the golden + run the Python guard**

Run: `EMBEDDING_MODEL=bge-m3 REGEN_WRITEPATH=1 uv run pytest tests/proposal_assist/collab/test_collab_writepath.py -q -p no:cacheprovider`
Then (verify it is committed-and-stable, no regen): `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_writepath.py -q -p no:cacheprovider`
Expected: PASS; `goldens/python_writepath_incremental.json` now exists.

- [ ] **Step 3: Add the incremental verify to the Node harness**

In `frontend/scripts/verify_python_writepath.mjs`, add an incremental artifact path + reconstructor and fold it into the CLI. Reuse `normalise`/`deepStrictEqual`:

```javascript
const INCREMENTAL = path.resolve(
  repoRoot,
  'tests/proposal_assist/collab/goldens/python_writepath_incremental.json',
);

export function loadIncremental() {
  return JSON.parse(readFileSync(INCREMENTAL, 'utf8'));
}

/** Apply base THEN incremental Python-produced bytes; read back the PM JSON. */
export function reconstructIncremental(entry) {
  const d = new Y.Doc();
  Y.applyUpdate(d, Buffer.from(entry.base_b64, 'base64'));
  Y.applyUpdate(d, Buffer.from(entry.incr_b64, 'base64'));
  return normalise(yDocToProsemirrorJSON(d, 'prosemirror'));
}

export function compareAllIncremental(entries = loadIncremental()) {
  const mismatches = [];
  for (const entry of entries) {
    try {
      deepStrictEqual(reconstructIncremental(entry), normalise(entry.pm_json));
    } catch {
      mismatches.push({ name: entry.name });
    }
  }
  return mismatches;
}
```

In the `if (invokedDirectly)` block, after the full-state comparison, also run `compareAllIncremental()`, print per-fixture ✓/✗, and `process.exit(1)` if any incremental fixture mismatches (a real NO-GO — a Python-generated incremental update that a real `y-prosemirror` client cannot apply).

- [ ] **Step 4: Run the Node harness (the validation gate)**

Run: `cd frontend && node scripts/verify_python_writepath.mjs`
Expected: GO — all full-state AND incremental fixtures read back identical. If any incremental fixture (especially `clear_blocks_empty` / `clear_preamble`) mismatches, that is a REAL write-path interop bug: STOP, record it in the findings note, and fix `apply_pm_section_to_fragment` / the serializer before proceeding (do not weaken the assertion).

- [ ] **Step 5: Write the findings note + commit**

Create `docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md` recording: the incremental corpus, the Node GO/NO-GO result, and any interop bug found + fixed (or "clean — all incremental fixtures read back identical in real y-prosemirror").

Run: `uv run ruff check tests/proposal_assist/collab/test_collab_writepath.py`
Expected: `All checks passed!`

```bash
git add tests/proposal_assist/collab/test_collab_writepath.py frontend/scripts/verify_python_writepath.mjs tests/proposal_assist/collab/goldens/python_writepath_incremental.json docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md
git commit -m "test(collab): extend write-path harness with incremental apply fixtures (real y-prosemirror)"
```

---

## Task 6: Full grouped sweep + parity gate

**Files:** none (verification only).

- [ ] **Step 1: Parity-critical grouped sweep**

Run:
```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/ tests/test_api_spec_lock.py -q -p no:cacheprovider
```
Expected: PASS. Confirm the flag-off parity suites are green **unchanged** — `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, member/rubric/soft-delete. (Re-run any testcontainers `connection refused` flake in isolation.)

- [ ] **Step 2: Ruff over the whole tree (CI parity)**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm no schema/migration drift**

Run: `git diff --name-only main...HEAD | grep -i "migrations/\|alembic" || echo "no migrations touched"`
Expected: `no migrations touched`.

- [ ] **Step 4: Confirm the flag is still OFF by default**

Run: `grep -rn "enable_collab" src/`
Expected: only the definition in `collab/state.py` — no new production enablement.

---

## Self-Review (author checklist — completed at plan-write time)

**1. Spec coverage:**
- Component 1 (compaction, id-reuse merged-first) → Task 1 (`compact_snapshot`) + Task 2 (wire into `checkpoint_artifact`); the concurrent-append convergence regression → Task 1 Step 1.
- Component 2 (positional attribution) → Task 3; compensating-divergence (empirical) → Task 3 Step 5.
- Component 3 (tombstone + recheck at 3 sites) → Task 4 (handle pre-apply + pre-broadcast; kernel `_body` pre-commit; kernel broadcast).
- Component 4 (extend existing harness, incl. `blocks=[]`) → Task 5.
- Parity gate + no-migration + flag-off → Task 6.

**2. Placeholder scan:** All production code is complete and exact. The two empirically-constructed tests (Task 3 Step 5 compensating divergence; Task 4's concurrent kernel race) carry explicit RED-first + documented-fallback instructions rather than a fabricated fixture — the honest treatment for serializer/scheduler-dependent cases (mirrors 3a's hard_break approach).

**3. Type consistency:** `compact_snapshot(session, *, workspace_id, artifact, min_id, merged_update, delete_ids)` defined in Task 1, called identically in Task 2. `Room.evicted: bool` set in `evict` (Task 4), read in `Room.handle` + kernel `_body`/broadcast (Task 4). `RoomEvicted` defined + added to `_CLEAN_ACCEPT_ERRORS` in Task 4, mapped in `web/proposals.py` (Task 4). `apply_pm_section_to_fragment(doc, *, section_index, blocks)` unchanged signature; only its internal guard changes (Task 3). Artifact strings `"draft"`/`"spec"` throughout.

**Open items flagged for the plan-Codex-review:**
1. Component 3 kernel↔kernel ordering (Task 4 reviewer note) — tombstone visibility to the next `room.lock` holder for concurrent accepts.
2. Component 2 normalization (`rstrip("\n")`) sufficiency — the positive suite (Task 3 Steps 1-4) is the guard; tighten only if a valid doc false-raises.
3. Component 4 `blocks=[]` real-client behavior — Task 5 Step 4 is the GO/NO-GO; a mismatch is a real bug to fix, not to assert around.
