# Collab Post-Flip Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the carried Minors from the 3b-5d.D4 final review (ledger `.superpowers/sdd/progress.md`): the borderless-table detector blind spot (a live data-corruption vector), the missing coverage twins, the annotation-route `IndexError` parity nit, the Claims-insert activity-feed double row, and the kill-switch runbook note.

**Architecture:** No new modules. Three narrow tasks: (1) strengthen `has_pipe_tables` with an anchored GFM delimiter-row heuristic (the audit script imports the same detector, so it inherits the fix); (2) a test/parity batch — spec-variant severance-recheck tests, a spec-artifact handover test, the `IndexError→409` route mapping, and two comment/noqa nits; (3) split `checkpoint_artifact`'s provenance flag so the annotation kernel path emits exactly one user-visible feed row, plus the ops runbook paragraph in the D4 spec.

**Tech Stack:** Python 3.12 / FastAPI / pycrdt collab kernel / pytest + testcontainers PG. No frontend changes.

## Global Constraints

- Work in an isolated worktree: `.worktrees/collab-postflip-hardening`, branch `collab-postflip-hardening` off `master` (post-D4, ≥ `0c53a72`). Subagents: `git -C <abs-worktree-path>` for every git command; verify the branch before committing.
- **Behavioral floor:** every existing test passes UNCHANGED. Detector changes may only ADD detections (false positives only delay enablement — safe direction). `ArtifactVersion` provenance must be preserved at every kernel checkpoint site (the D4 Task-6/Task-8 documented deviation depends on it).
- No migration, no schema change, no new locks.
- Pytest: always `EMBEDDING_MODEL=bge-m3 uv run pytest <files> -p no:cacheprovider`; NEVER the whole suite (`connection refused`/`too many clients` = infra → re-run the file isolated; `UndefinedTable` = migration-ordering → report).
- Kill-switch-flipping tests: autouse reset fixture; arming around `create_app` goes via `monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")`.
- Explicit-path `git add` only. Commit trailer (last line of every body): `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Merges local `--no-ff` only; do NOT push; do NOT invoke /code-review.

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/collab/flip.py` | Modify — delimiter-row heuristic + comment fixes (Task 1) |
| `tests/proposal_assist/collab/test_collab_flip.py` | Modify — detector cases (Task 1) |
| `tests/proposal_assist/collab/test_collab_save_severance.py` | Modify — spec-save recheck twin (Task 2) |
| `tests/proposal_assist/collab/test_collab_init_spec_kernel.py` | Modify — init_spec recheck test (Task 2) |
| `tests/proposal_assist/collab/test_collab_kill_switch_handover.py` | Modify — spec-artifact handover case (Task 2) |
| `src/call_manager/web/api/annotations.py` | Modify — `IndexError→409` mapping (Task 2) |
| `tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py` | Modify — IndexError-route test (Task 2) + single-feed-row test (Task 3) |
| `tests/proposal_assist/collab/test_collab_kill_switch.py` + `tests/test_api_spec.py` + `tests/test_api_spec_lock.py` | Modify — noqa/docstring nits (Task 2) |
| `src/call_manager/proposal_assist/collab/checkpoint.py` | Modify — `record_event` split (Task 3) |
| `src/call_manager/proposal_assist/collab/kernel.py` | Modify — annotation site passes `record_event=False` (Task 3) |
| `docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md` | Modify — §Operations runbook note (Task 3) |

---

### Task 1: Borderless-table detector — anchored delimiter-row heuristic

**Why (final-review Minor 3, severity raised post-flip):** `_TABLE_ROW` requires ≥2 pipes per line, so a valid borderless 2-column GFM table (`Name | Value` / `---- | -----`) is NOT held back → the workspace auto-enables and the table flattens on the first room round-trip = silent data corruption. The audit script imports `has_pipe_tables`, so it shares the blind spot; local audit currently reports clean, but only under the weak detector.

**Files:** Modify `src/call_manager/proposal_assist/collab/flip.py` (:33-52), `tests/proposal_assist/collab/test_collab_flip.py`.

**Interfaces:** `has_pipe_tables(md: str) -> bool` signature unchanged. Consumers (read models, audit script) untouched.

- [ ] **Step 1: Write the failing tests** — add to the detector test group in `test_collab_flip.py` (match its existing style):

```python
def test_detects_borderless_two_column_table():
    md = "# T\n\nName | Value\n---- | -----\nfoo | bar\n"
    assert has_pipe_tables(md) is True


def test_detects_aligned_delimiter_row():
    md = "col a | col b\n:--- | ---:\nx | y\n"
    assert has_pipe_tables(md) is True


def test_single_pipe_prose_still_clean():
    # A lone prose pipe (no delimiter row anywhere) must NOT trigger the hold-back.
    md = "# T\n\neither | or, as they say.\n\nMore prose.\n"
    assert has_pipe_tables(md) is False


def test_delimiter_row_inside_fence_still_clean():
    md = "```\nName | Value\n---- | -----\n```\nprose\n"
    assert has_pipe_tables(md) is False
```

- [ ] **Step 2: Run to verify the two new detections FAIL** (`test_detects_borderless_two_column_table`, `test_detects_aligned_delimiter_row`) and the two clean cases already pass:
`EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip.py -p no:cacheprovider -q`

- [ ] **Step 3: Implement** in `flip.py` — add an anchored GFM delimiter-row pattern alongside `_TABLE_ROW` and check both; fix the overstating comment; document the fence-scanner limitation (final-review Minor 6) without changing its behavior:

```python
# A pipe-table-shaped row: a line with >=2 pipes separating cell-ish content
# (bordered header/body rows, with or without a leading pipe). Does NOT match
# borderless 2-column rows (single interior pipe) — those are caught by
# _DELIMITER_ROW below. Conservative ON PURPOSE: a false positive only DELAYS
# enablement (the artifact stays on the CM6 editor); a false negative flattens
# a table on the first room round-trip.
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")

# A GFM delimiter row (`---- | -----`, `:--- | ---:`, `|---|---|`): the whole
# line is dash-cells (>=3 dashes, optional alignment colons) joined by >=1 pipe.
# This is the unambiguous table signature that catches BORDERLESS 2-column
# tables, whose header/body rows have only a single interior pipe (post-flip
# hardening; D4 final-review Minor 3). Prose-safe: real prose lines never
# consist solely of dashes and pipes.
_DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def has_pipe_tables(md: str) -> bool:
    in_fence = False
    for line in md.splitlines():
        stripped = line.lstrip()
        # NOTE: generic fence toggle — the closing fence's character/length is
        # not matched to the opener (CommonMark would). Mismatches err toward
        # treating more lines as fenced, i.e. false NEGATIVES are possible only
        # for a table following an unclosed pseudo-fence; accepted (D4
        # final-review Minor 6) — indented-code pseudo-fences are exotic in
        # proposal prose.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(line) or _DELIMITER_ROW.match(line):
            return True
    return False
```

- [ ] **Step 4: Run the full flip test file + the read-model file** (the hold-back is exercised there too):
`EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip.py tests/proposal_assist/collab/test_collab_flip_readmodel.py -p no:cacheprovider -q` → all pass. `uv run ruff check src/call_manager/proposal_assist/collab/flip.py tests/proposal_assist/collab/test_collab_flip.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/flip.py tests/proposal_assist/collab/test_collab_flip.py
git commit -m "fix(collab): hold back borderless 2-column tables — anchored delimiter-row heuristic (post-flip hardening)"
```
(body: one line on the blind spot + the trailer line)

---

### Task 2: Coverage twins + IndexError parity + nits

**Files:** Modify `tests/proposal_assist/collab/test_collab_save_severance.py`, `tests/proposal_assist/collab/test_collab_init_spec_kernel.py`, `tests/proposal_assist/collab/test_collab_kill_switch_handover.py`, `src/call_manager/web/api/annotations.py`, `tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py`, `tests/proposal_assist/collab/test_collab_kill_switch.py`, `tests/test_api_spec.py`, `tests/test_api_spec_lock.py`.

**Interfaces:** one production change only — the annotation insert route gains the `except IndexError` → 409 mapping its siblings have (`workspaces.py:456/:997/:1069` — copy the sibling's exact detail string and comment style from `:997` (post_citation)). Everything else is tests/comments.

- [ ] **Step 1: Spec-variant severance-recheck tests (D4 final-review carry).** The under-mutex recheck (`raise_if_collab_severed` seam family, commit 8656880) has draft-save/cite/accept-replay/annotation-insert tests but no SPEC variants. Read the existing draft-save recheck test in `test_collab_save_severance.py` (added by the D4 final fix wave — the white-box test that flips the flag after the guard read) and add its twin for `post_save_spec_section` (artifact="spec"). Read `test_collab_init_spec_kernel.py`'s disabled-path test and add the recheck interleaving test for `post_init_spec`'s `_git` branch: flag OFF at route entry, monkeypatch so the flag flips before/at mutex acquisition (same mechanics as the save twin), assert 409 with `COLLAB_SEVERED_DETAIL` and NO git write (HEAD unchanged, no skeleton file mutation).
- [ ] **Step 2: Run both files** → new tests pass, existing unchanged.
- [ ] **Step 3: Handover spec-artifact twin.** In `test_collab_kill_switch_handover.py`, add `test_handover_drains_and_truncates_spec_artifact`: mirror the existing draft drain test (same helpers) with `artifact="spec"` / `proposal-spec.md`, asserting the spec file's git content equals the drained doc, the spec log rows are truncated, and the draft artifact is untouched (cross-artifact isolation).
- [ ] **Step 4: `IndexError→409` in the annotation insert route.** In `annotations.py`'s kernel branch (the `insert_annotation_via_kernel` call site), add the `except IndexError` mapping mirroring `workspaces.py:997`'s post_citation block verbatim (same status, detail, comment). Test in `test_collab_annotation_insert_kernel.py`: monkeypatch `insert_annotation_via_kernel` (at the annotations.py import site) to raise `IndexError`, POST the insert on an enabled workspace, assert 409 and that the annotation row's status is unchanged (not burned).
- [ ] **Step 5: Nits.** Remove the dead `# noqa: F811` in `tests/proposal_assist/collab/test_collab_kill_switch.py` (`_settings_env` is fixture-injected; ruff confirms nothing is redefined). In `tests/test_api_spec.py` + `tests/test_api_spec_lock.py`, fix the `_cm6_world`/reset-fixture docstrings that say "module scope" — the reset fixture is function-scoped; make the wording truthful.
- [ ] **Step 6: Run the batch** — the five test files above + `uv run ruff check` on every touched file → all green, existing tests unchanged.
- [ ] **Step 7: Commit** (two commits are fine if cleaner: `test(collab): post-flip coverage twins — spec severance/init_spec recheck + spec handover` and `fix(collab): annotation insert maps IndexError→409 — sibling parity + nits`; explicit paths; trailer.)

---

### Task 3: Single feed row for kernel Claims insert + runbook note

**Why (final-review Minor 5):** `apply_annotation_insert_to_room` calls `checkpoint_artifact` (which records `WorkspaceEvent kind="collab_checkpoint"` → feed "saved a version") AND emits its own `kind="user_citation_insert"` (→ "added a citation") — two feed rows for one click; the git path emits one. The `ArtifactVersion` from the checkpoint MUST survive (documented D4 deviation).

**Files:** Modify `src/call_manager/proposal_assist/collab/checkpoint.py` (:62, :120-140), `src/call_manager/proposal_assist/collab/kernel.py` (`apply_annotation_insert_to_room`'s checkpoint call, ~:508), `tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py`, `docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md`.

**Interfaces:** `checkpoint_artifact(..., record_version: bool = True, record_event: bool | None = None)` — `record_event=None` means "follow `record_version`" so EVERY existing call site keeps byte-identical behavior with zero edits; `record_event=False` with `record_version=True` records the `ArtifactVersion` but suppresses the `collab_checkpoint` `WorkspaceEvent`.

- [ ] **Step 1: Failing test** — extend the enabled-path test in `test_collab_annotation_insert_kernel.py` (or add a sibling): after a kernel annotation insert, assert the workspace's `WorkspaceEvent` rows for this op contain EXACTLY ONE `user_citation_insert` and ZERO `collab_checkpoint`, and that an `ArtifactVersion` row for the checkpoint commit exists.
- [ ] **Step 2: Run** → fails on the `collab_checkpoint` count (currently 1).
- [ ] **Step 3: Implement.** In `checkpoint.py`: add the parameter (`record_event: bool | None = None`; resolve `effective_event = record_version if record_event is None else record_event`), split the provenance block: `if changed and record_version:` adds `ArtifactVersion`; `if changed and effective_event:` adds the `WorkspaceEvent`. Keep comment explaining why they can diverge (kernel ops that emit their own user-action event). In `kernel.py`: `apply_annotation_insert_to_room`'s `checkpoint_artifact(...)` call gains `record_event=False`, with a one-line comment pointing at the op's own `user_citation_insert` emission below. Do NOT touch the other kernel sites: `kernel_init_spec`'s checkpoint event IS its documented provenance row (kernel.py:313-314); cite/accept sites emit no same-transaction user event.
- [ ] **Step 4: Run** the annotation kernel file + checkpoint/scan/trigger files (`test_collab_checkpoint.py`, `test_collab_checkpoint_scan.py`, `test_collab_checkpoint_triggers.py`, `test_collab_checkpoint_analysis.py`) + the feed tests (find them: `grep -rl collab_checkpoint tests/ --include='test_*'`) → all pass, existing unchanged.
- [ ] **Step 5: Runbook note.** Append a short `## Operations note (post-flip)` section to the D4 spec: the kill-switch is a per-process env var read once at `create_app` (restart-scoped — it cannot arm mid-process); the single-instance deployment constraint is what makes it globally effective; the startup handover is per-artifact fault-isolated and retries on every scan tick — before lifting the switch, check the logs for handover failure lines, because lifting early replays a stale log ONLY for artifacts whose handover never succeeded (spec §3.2/§10 mechanics).
- [ ] **Step 6: Ruff + commit** — `fix(collab): one feed row per kernel Claims insert — record_event split (post-flip hardening)` + a `docs(collab):` commit for the spec note (or one commit; explicit paths; trailer).

---

### Task 4: Acceptance gates (controller-run)

- [ ] Grouped backend: `tests/proposal_assist/collab` + `tests/test_api_spec.py tests/test_api_spec_lock.py tests/test_api_workspaces.py tests/test_api_export.py tests/proposal_assist/test_api_annotations_insert.py tests/test_analysis_insert.py` → all pass.
- [ ] `uv run ruff check .` → clean.
- [ ] Two-browser e2e both modes (flip.py + checkpoint.py are production surfaces the e2e crosses): `bash scripts/collab_e2e_run.sh` and `E2E_SPA_MODE=prod bash scripts/collab_e2e_run.sh` → 5/5 each.
- [ ] Re-run `EMBEDDING_MODEL=bge-m3 uv run python scripts/collab_table_audit.py` (now with the strong detector) and REPORT the count — if still clean, note in the report that the hold-back is removable pending the tables feature (do NOT remove it in this slice).
- [ ] Stop for review — do NOT merge. Local `--no-ff` merge only after the final review, per house convention.

## Self-review notes

- Spec coverage: all seven approved scope items map to Tasks 1–3 (detector+audit / twins+parity+nits / feed+runbook); audit re-run is in Task 4.
- Type consistency: `record_event: bool | None = None` is the only signature change; default-follows-`record_version` guarantees zero behavioral drift at unedited call sites (tail-scan's `record_version=False` keeps suppressing both).
- Deliberately OUT of scope: removing the hold-back (needs the strong-detector audit + a decision), CM6 teardown (kill-switch dependency), tables feature, compact-before-truncate waste (emergency path only, carry).
