# Freeze-Only Delete (Retention Slice A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deleting a workspace stops destroying files. It freezes the workspace — users evicted, work stopped, every byte kept — so a later slice can restore it intact.

**Architecture:** Remove the one production call to `scrub_workspace_bytes` from the DELETE route, stop the test helper composing it, invert every test that asserts deletion removes bytes, correct every statement in the codebase that says deletion scrubs, and fix the user-facing copy. `scrub_workspace_bytes` itself is kept for the future purge slice.

**Tech Stack:** Python 3.12, FastAPI, pytest (+ testcontainers Postgres); React 19 + Mantine on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` (revision 4, Slice A — sections 0.1, 0.2, 2).

## Global Constraints

- **Deletion must destroy nothing on disk.** Everything else deletion does is unchanged: marking `soft_deleted_at`, cancelling jobs and queued turns, terminalizing running turns, evicting collab rooms, running the retirement coordinator.
- **The new route contract is `DB commit → retire → 204`.** The old contract was `DB commit → retire → scrub → 204`; it is named in a docstring, a comment, and a test, all of which must change together.
- **`scrub_workspace_bytes` is KEPT, not deleted.** Slice E's purge needs this logic. It ends this slice with zero production callers, deliberately, and its docstring must say so and name the slice that will call it. Do not delete it as dead code.
- **Slice A ships BEFORE inspection (B), restore (C) and purge (E).** No user-facing text may promise an admin can access, restore, or permanently remove anything — none of that exists yet. State only what is true today.
- **The DELETE route's docstring is its OpenAPI description** and appears in `frontend/src/api/schema.gen.ts:617`. Changing it requires regenerating that file, which is a hard CI gate.
- No database migration. No new routes. No change to who may delete (already owner-only).
- Work on a branch. Never commit to `master`. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~64s for the full suite).
- **Never pipe a test command through `tail` to judge success** — the pipeline's exit status is `tail`'s, which is always 0. This applies to `pnpm` too. Always redirect to a file and echo `$?`, as every command below does.
- **`pytest` exits 4 on "no tests ran"** — a path typo reads as success. Confirm a nonzero collected count.
- **Stage files individually — never `git add <directory>`.** The tree carries unrelated untracked notes under `docs/superpowers/`, and a directory-wide add sweeps them into your commit. This already happened once on this branch and had to be undone.
- There is no TypeScript linter on the frontend; `tsc --noEmit` is the gate.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/call_manager/web/api/workspaces.py` | the one production scrub call — removed; route docstring corrected | 1, 2 |
| `tests/proposal_assist/test_api_workspaces_delete.py` | the REAL route test — whole-tree survival assertions | 1 |
| `tests/workspace_delete_helper.py` | stops composing the scrub | 1 |
| `tests/test_workspaces_service.py` | helper-level assertions inverted | 1 |
| `src/call_manager/workspaces.py` | `scrub_workspace_bytes` docstring + both runtime warnings; `soft_delete_workspace_db` ordering docstring | 2 |
| `src/call_manager/proposal_assist/retirement.py`, `row_locks.py` | budget comments that still count the scrub | 2 |
| `frontend/src/api/schema.gen.ts` | regenerated after the route docstring changes | 2 |
| `frontend/src/cockpit/WorkspaceSettingsModal.tsx` + its test | confirmation copy | 3 |

---

### Task 1: Delete stops destroying bytes

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (the scrub call around line 1530 and the comment block above it)
- Modify: `tests/workspace_delete_helper.py`
- Modify: `tests/proposal_assist/test_api_workspaces_delete.py`
- Modify: `tests/test_workspaces_service.py`, plus anything the Step 1 survey adds

**Interfaces:**
- Consumes: nothing.
- Produces: a DELETE route that leaves `<workspaces_dir>/<workspace_id>/` byte-identical. Slice C (restore) depends on this; Slice E (purge) will be the sole caller of the retained `scrub_workspace_bytes`.

- [ ] **Step 1: Survey every test that depends on deletion removing bytes**

Do this before changing anything. The survey output goes in your report.

**There are two distinct layers and the survey must cover both**, because a test that exercises the helper cannot detect a change to the route:

- **Route-level** — tests that call `DELETE /api/workspaces/{id}`. These exercise the production path.
- **Helper-level** — tests calling `soft_delete_workspace_for_test`, which composes the DB half and the scrub directly and never touches the route.

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "scrub_workspace_bytes\|soft_delete_workspace_for_test" tests/ | sort > /tmp/survey-helper.txt
grep -rln "delete(f\"/api/workspaces/\|delete(\"/api/workspaces/\|\.delete(" tests/ | sort > /tmp/survey-routes.txt
grep -rn "exists()\|is_dir()\|is_file()\|read_text()" $(cat /tmp/survey-routes.txt) | sort > /tmp/survey-fs.txt
wc -l /tmp/survey-helper.txt /tmp/survey-routes.txt /tmp/survey-fs.txt
cat /tmp/survey-fs.txt
```

Known starting points, both of which must appear in your survey:
- **Route-level:** `tests/proposal_assist/test_api_workspaces_delete.py` — the assertion at line 68 depends directly on deletion removing bytes, and the test `test_delete_runs_db_then_retire_then_scrub_in_that_order` pins the old four-step contract by name.
- **Helper-level:** `tests/test_workspaces_service.py:91` (`assert not attach_dir.exists()  # bytes scrubbed`), inside `test_soft_delete_workspace_cascades_and_scrubs` at line 26.

Also note `tests/proposal_assist/test_workspaces_delete_route.py` exists and is a *different* file from `test_api_workspaces_delete.py`. Check both.

- [ ] **Step 2: Strengthen the ROUTE test to prove the whole-tree invariant**

The claim is "deletion destroys nothing," not "the attachment directory still exists." Directory existence is much weaker than the invariant — a scrub that emptied a directory without removing it would pass.

In `tests/proposal_assist/test_api_workspaces_delete.py`, seed a sentinel in each of the three byte families and compare **exact contents** after the delete:

```python
def test_delete_keeps_every_byte_on_disk(client, pg_session_factory, tmp_path):
    """Slice A: deletion FREEZES a workspace. Attachment, reference and repository
    bytes must all survive byte-identical, so a later restore is lossless.

    Contents, not existence: a scrub that emptied a directory without removing it
    would satisfy an exists() check while destroying exactly what restore needs.
    """
    ws_id = _seed_workspace_with_bytes(
        tmp_path,
        attachment=b"ATTACHMENT SENTINEL",
        reference=b"REFERENCE SENTINEL",
        proposal="# PROPOSAL SENTINEL\n",
    )

    attach = _attachment_path(tmp_path, ws_id)
    ref = _reference_path(tmp_path, ws_id)
    repo_doc = _proposal_path(tmp_path, ws_id)
    before = (attach.read_bytes(), ref.read_bytes(), repo_doc.read_text(encoding="utf-8"))

    r = client.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204

    assert attach.read_bytes() == before[0], "attachment bytes were destroyed"
    assert ref.read_bytes() == before[1], "reference bytes were destroyed"
    assert repo_doc.read_text(encoding="utf-8") == before[2], "proposal was destroyed"
```

Build `_seed_workspace_with_bytes` and the three path helpers from the seeding patterns already in that file and in `tests/test_workspaces_service.py:51` (which seeds an on-disk attachment tree). Use the real storage classes — `AttachmentStorage` and `ReferenceStorage` — to place the bytes, so the paths are the ones production actually uses rather than paths you invented.

- [ ] **Step 3: Rewrite the ordering test to the new contract**

`test_delete_runs_db_then_retire_then_scrub_in_that_order` pins a contract that no longer exists. Rename it to `test_delete_runs_db_then_retire_then_204_in_that_order` and drop the scrub from the expected sequence, keeping the DB-then-retire ordering assertion exactly as strong as it is now. That ordering is a real invariant from an earlier slice — do not weaken it while removing the third step.

- [ ] **Step 4: Invert the helper-level assertions**

In `tests/test_workspaces_service.py`, rename `test_soft_delete_workspace_cascades_and_scrubs` to `test_soft_delete_cascades_and_keeps_bytes` and change line 91:

```python
    assert attach_dir.exists(), (
        "Slice A: deleting a workspace FREEZES it and destroys nothing, so a "
        "later restore is lossless. Only an admin purge removes bytes."
    )
```

Keep every other assertion in that test unchanged — the DB cascade behaviour is not changing and must stay covered.

- [ ] **Step 5: Run both layers to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_api_workspaces_delete.py tests/test_workspaces_service.py -q > /tmp/a1.log 2>&1; echo "PYTEST_EXIT=$?"; tail -25 /tmp/a1.log
```

Expected: FAIL — bytes are still being scrubbed, because the production change has not been made yet. **If these pass now, stop and report:** it would mean the assertions never exercised the scrub, and everything after would be built on a false premise.

- [ ] **Step 6: Remove the scrub from the DELETE route**

In `src/call_manager/web/api/workspaces.py`, delete the `scrub_workspace_bytes(...)` call at line 1530 and its now-unused import. Replace the comment block above it:

```python
    # DB soft-delete is durable. Retire in-process (bounded ~5s, never raises):
    # tombstone the turns registry so no new subprocess installs and tear down any
    # live one, and tear down both collab rooms (close editors 1008, no
    # checkpoint). This SUBSUMES the old signal_from_request permission
    # reconciliation — the rooms are gone. THEN 204.
    #
    # Slice A: deletion FREEZES a workspace and destroys nothing on disk. It used
    # to scrub attachment and reference bytes here; that removal is what made a
    # later restore impossible, because the rows survived while their files did
    # not. Byte removal moves to the admin purge (Slice E). Users are already
    # locked out by the liveness gates and the room eviction above — keeping the
    # bytes changes what can be recovered later, not who can reach them now.
    signal_workspace_retirement(request, workspace_id=workspace_id)
    return Response(status_code=204)
```

- [ ] **Step 7: Stop the test helper composing the scrub**

`tests/workspace_delete_helper.py` reproduces production's delete for tests that only need the DB half. Production no longer scrubs, so neither should it.

Remove the `scrub_workspace_bytes(...)` call and its import, and update the docstring:

```python
def soft_delete_workspace_for_test(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    timeout_seconds: int = 10,
) -> None:
    """The DB half of a delete, with no retirement — what production does minus
    the in-process teardown.

    Slice A: this no longer scrubs bytes, because production no longer does. A
    test that specifically wants byte removal should call `scrub_workspace_bytes`
    directly and say why in a comment.
    """
    soft_delete_workspace_db(
        session, workspace_id=workspace_id, timeout_seconds=timeout_seconds
    )
```

The `workspaces_dir` parameter is now unused. Remove it from the signature **and from every call site** the Step 1 survey found. Do not leave it as an ignored argument — a parameter that does nothing is a lie the next reader has to disprove.

- [ ] **Step 8: Run both layers to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_api_workspaces_delete.py tests/proposal_assist/test_workspaces_delete_route.py tests/test_workspaces_service.py -q > /tmp/a1.log 2>&1; echo "PYTEST_EXIT=$?"; tail -10 /tmp/a1.log
uv run ruff check . > /tmp/a1-ruff.log 2>&1; echo "RUFF_EXIT=$?"
```

Expected: all pass, both exits 0.

- [ ] **Step 9: Run the full suite**

This slice changes a path many tests touch, so a targeted run is not enough.

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/a1-full.log 2>&1; echo "PYTEST_EXIT=$?"; tail -3 /tmp/a1-full.log
```

Expected: `3357 passed, 1 skipped`, exit 0. Any failure is a test the Step 1 survey missed — invert it the same way and **record it in your report as a survey miss**, so the gap in the survey commands is visible rather than silently patched.

- [ ] **Step 10: Prove the production change is pinned by a production-path test**

The mutation must run the **route** test. Running only `tests/test_workspaces_service.py` would prove nothing here: that file exercises `soft_delete_workspace_for_test`, not the route, so after Step 7 it stays green whether or not the route scrubs.

```bash
cd /home/parlar_ai/dev/call-monitor
md5sum src/call_manager/web/api/workspaces.py
# re-add the scrub call to the DELETE route, then:
uv run pytest tests/proposal_assist/test_api_workspaces_delete.py -q > /tmp/a1-mut.log 2>&1; echo "PYTEST_EXIT=$?"; tail -12 /tmp/a1-mut.log
# restore, then:
md5sum src/call_manager/web/api/workspaces.py
uv run pytest tests/proposal_assist/test_api_workspaces_delete.py -q > /tmp/a1-restored.log 2>&1; echo "PYTEST_EXIT=$?"; tail -4 /tmp/a1-restored.log
```

Expected: RED with the scrub restored (`test_delete_keeps_every_byte_on_disk` failing on a destroyed sentinel), green after, and the two md5 hashes identical. Report all of it.

- [ ] **Step 11: Commit**

Stage files individually.

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/workspaces.py tests/workspace_delete_helper.py tests/test_workspaces_service.py tests/proposal_assist/test_api_workspaces_delete.py
git status --short   # confirm nothing unrelated is staged
git commit -m "feat(delete): freeze-only -- deletion destroys nothing on disk

Deleting a workspace already evicts users, cancels jobs and turns, and tears
down collab rooms. It also scrubbed attachment and reference bytes, which is
what made a later restore impossible: rows survived while their files did not.
Byte removal moves to the admin purge (Slice E).

The route test now seeds attachment, reference and proposal sentinels and
compares exact contents after the delete. Directory existence would not have
caught a scrub that emptied a directory without removing it."
```

---

### Task 2: Correct every statement that says deletion scrubs

Task 1 changed the behaviour. Six places still describe the old behaviour, and one of them is a public API description. A codebase that documents behaviour it no longer has is worse than one that documents nothing.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (route docstring, ~line 1500)
- Modify: `src/call_manager/workspaces.py` (`soft_delete_workspace_db` docstring ~line 90; `scrub_workspace_bytes` docstring ~line 181; both runtime warnings ~lines 197 and 207)
- Modify: `src/call_manager/proposal_assist/retirement.py` (~line 38), `src/call_manager/proposal_assist/row_locks.py` (~line 83)
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)

**Interfaces:**
- Consumes: Task 1's behaviour change.
- Produces: nothing consumed later.

- [ ] **Step 1: Find them all**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "scrub" src/ tests/ | grep -v "def scrub_workspace_bytes" > /tmp/a2-scrub.txt
grep -rn "future sweep" src/ tests/ >> /tmp/a2-scrub.txt
cat /tmp/a2-scrub.txt
```

**Search `tests/` as well as `src/`.** Task 1's review found a stale statement in a test docstring — `tests/proposal_assist/test_workspaces_delete_route.py:217` still describes "the route's db/retire/scrub ORDER" — which a `src/`-only sweep would have missed entirely. A test that documents a contract the route no longer has is exactly as misleading as a wrong docstring in production code.

Every line is either a statement that must change or a deliberate reference to the retained function. Classify each in your report — do not silently skip any.

Three are already known and must appear in your classification, each flagged by Task 1's reviewer:
- `src/call_manager/proposal_assist/row_locks.py:83` — lists "scrub" among the consumers of the DELETE route's time budget. The route runs no scrub, so it contributes nothing to that budget.
- `src/call_manager/proposal_assist/retirement.py:38` — justifies the retirement barrier's duration partly by the hazard of "scrub_workspace_bytes starting to delete files while teardown is still in flight". Task 1's implementer judged this still valid as forward-looking guidance; its reviewer disagreed, on the grounds that the sentence is phrased as a justification for the barrier's duration *in the DELETE route*, a call site that no longer scrubs at all. **Decide this yourself on the merits and say which reading you took and why** — do not simply adopt either verdict.
- `tests/proposal_assist/test_workspaces_delete_route.py:217` — the test docstring above.

- [ ] **Step 2: Correct the route docstring**

`src/call_manager/web/api/workspaces.py` around line 1500 currently says the route "scrubs file bytes" and that "The ORDER is the contract (spec B5): DB commit → retire → scrub → 204."

```python
    """Owner-only soft-delete. Cascades active jobs + queued turns and retires the
    workspace in-process. Re-delete of an already-soft-deleted workspace → 404
    (require_owner filters soft_deleted_at) — not idempotent, by design.

    Destroys nothing on disk (Slice A): the workspace is frozen, not erased, so
    its contents remain for a later admin purge. Users are locked out by the
    liveness gates and the room eviction, not by byte removal.

    The ORDER is the contract: DB commit → retire → 204."""
```

- [ ] **Step 3: Correct `soft_delete_workspace_db`'s ordering docstring**

Around line 90 of `src/call_manager/workspaces.py`, the docstring describes the interleaving as "DB commit -> retire -> scrub -> 204". Change that sequence to "DB commit -> retire -> 204" and adjust the surrounding sentence so it no longer refers to an FS scrub as part of a delete. Leave the rest of that docstring — the mutex, `CommitMutexTimeout`, and `WorkspaceDeleted` contracts — exactly as it is.

- [ ] **Step 4: Correct `scrub_workspace_bytes`' docstring and BOTH runtime warnings**

The docstring is wrong three ways: it calls itself "the FS half of a soft-delete" (no longer true), describes ordering against the DB commit (no longer relevant), and claims failed cleanups are "collected by a future sweep" — **verified false, there is no sweep anywhere in the codebase.**

```python
    """Best-effort removal of attachment and reference bytes from disk.

    NOT called by the delete path. Slice A made deletion freeze-only: it destroys
    nothing, so a later restore is lossless. This function is retained for Slice
    E's admin purge, which is the deliberate act that removes a deleted
    workspace's files for real — and which will extend it to the whole workspace
    tree rather than these two subtrees.

    It therefore has NO production caller today. That is intentional; deleting it
    would mean re-deriving this logic from git history nobody will think to search.

    Attachment and reference DB rows are never touched here — they are the audit
    trail. Never raises on OSError: a failed cleanup leaves orphaned bytes that
    nothing reads and nothing collects. There is no sweep.
    """
```

Both `logger.warning` calls in this function (around lines 197 and 207) also tell operators the files "will be collected by a future sweep". Change both messages to say the bytes are left in place and removed only by an admin purge. **An operator acting on a log line is the whole point of the log line** — a false one is worse than none.

- [ ] **Step 5: Correct the two budget comments**

`src/call_manager/proposal_assist/retirement.py` around line 38 justifies the retirement barrier partly by "scrub_workspace_bytes starts deleting files while teardown is still in flight" — a hazard that no longer exists on this path. Keep the barrier and its budget unchanged; correct the justification, and note the hazard returns in Slice E's purge, where the barrier must be proven rather than best-effort.

`src/call_manager/proposal_assist/row_locks.py` around line 83 lists "the route's own transaction, retirement barrier and scrub" as consumers of the DELETE route's <6.0s budget. Remove the scrub from that list. Do not change `MAX_CONTENDED_ROWS` or any timeout — the budget is unchanged, only its accounting.

- [ ] **Step 6: Regenerate the OpenAPI types**

Step 2 changed a route docstring, which is that route's OpenAPI description and appears in `frontend/src/api/schema.gen.ts:617`. The drift check is a hard CI gate.

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm install --frozen-lockfile > /tmp/a2-pnpm.log 2>&1; echo "PNPM_EXIT=$?"
cd /home/parlar_ai/dev/call-monitor
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh > /tmp/a2-gen.log 2>&1; echo "GEN_EXIT=$?"
grep -c "scrubs file bytes" frontend/src/api/schema.gen.ts ; echo "(expect 0)"
```

`EMBEDDING_MODEL=bge-m3` is required: the default emits a startup warning that corrupts the generated file. Assert **both** exit codes are 0 — a failed generation still leaves the old file in place, so a drift check alone can pass while nothing was generated.

- [ ] **Step 7: Verify**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/a2-full.log 2>&1; echo "PYTEST_EXIT=$?"; tail -3 /tmp/a2-full.log
uv run ruff check . > /tmp/a2-ruff.log 2>&1; echo "RUFF_EXIT=$?"
grep -rn "future sweep" src/ ; echo "(expect no matches)"
```

Expected: `3357 passed, 1 skipped`, both exits 0, no "future sweep" anywhere.

- [ ] **Step 8: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/workspaces.py src/call_manager/workspaces.py src/call_manager/proposal_assist/retirement.py src/call_manager/proposal_assist/row_locks.py frontend/src/api/schema.gen.ts
git status --short   # confirm nothing unrelated is staged
git commit -m "docs: stop describing a scrub the delete path no longer does

Six places still documented the old behaviour, including the DELETE route's
OpenAPI description. Also removed two runtime warnings promising failed
cleanups are collected by a future sweep -- no sweep exists anywhere in the
codebase, and an operator acting on that line would wait forever."
```

---

### Task 3: Delete confirmation stops promising byte removal

The UI tells users deletion scrubs their uploaded files and cannot be undone. After Task 1 the first is false. The second is *still true today* — and must stay, because inspection, restore and purge have not shipped.

**Files:**
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.tsx` (description ~line 80, `window.confirm` string ~line 90)
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx` (extend the existing tests around line 40)

**Interfaces:**
- Consumes: Task 1's behaviour change. No API change.
- Produces: nothing consumed later.

- [ ] **Step 1: Extend the existing test**

Do **not** add a new `data-testid` and query it — the attribute does not exist yet, so the test would fail at lookup rather than on the copy, and would pass later for reasons unrelated to what it claims to check. Query the visible text through the existing render, following the conventions already in `WorkspaceSettingsModal.test.tsx`.

Assert on **both** strings — the description and the confirmation prompt — since both are wrong today:

```tsx
it("does not claim deleting removes uploaded files", async () => {
  // render via whatever helper this file already uses
  const body = await screen.findByText(/deletes the workspace/i);
  expect(body.textContent).not.toMatch(/scrub/i);
  expect(body.textContent).toMatch(/kept|retained|remain/i);
});

it("the confirmation prompt says the same thing as the description", async () => {
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  // render, then click the delete button through the existing helper
  const prompt = confirmSpy.mock.calls[0][0] as string;
  expect(prompt).not.toMatch(/scrub/i);
  expect(prompt).toMatch(/kept|retained|remain/i);
  confirmSpy.mockRestore();
});
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test -- WorkspaceSettingsModal > /tmp/a3.log 2>&1; echo "TEST_EXIT=$?"; tail -25 /tmp/a3.log
```

Expected: FAIL — both strings currently contain "scrubs".

- [ ] **Step 3: Correct both strings — today's truth only**

**Slice A ships before inspection, restore and purge.** The copy must not say an administrator can access, restore, or permanently remove the workspace: none of that is built. It must also keep telling the owner they cannot undo this, because they cannot.

```tsx
        <p>
          Deletes the workspace: it disappears for everyone, collaborators lose access, and any
          running work stops. Its contents are kept on the server rather than erased. You cannot
          undo this.
        </p>
```

```tsx
            if (
              window.confirm(
                `Delete "${name}"? Everyone loses access immediately and you cannot undo this. ` +
                  `The contents are kept on the server rather than erased.`,
              )
            ) {
```

- [ ] **Step 4: Run the frontend gates**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test > /tmp/a3-all.log 2>&1; echo "TEST_EXIT=$?"; tail -8 /tmp/a3-all.log
pnpm exec tsc --noEmit > /tmp/a3-tsc.log 2>&1; echo "TSC_EXIT=$?"
```

Expected: both exits 0.

- [ ] **Step 4b: Correct the test-count tripwire in `CLAUDE.md`**

This slice added a test, which makes `CLAUDE.md:25-26` actively misleading rather than merely stale. It currently reads:

> Expected result is **`3357 passed, 1 skipped`**. If you have `ANTHROPIC_API_KEY` exported you will instead see `3358 passed` …

**`3358 passed` is now the normal count with no API key.** So the document's stated signal for "you have a key exported" is exactly the number a developer without one will see. Someone would conclude they had a key set, or that a live API call ran when none did.

Re-derive both numbers rather than assuming — run the suite and read the count, then state it:

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/a3-count.log 2>&1; echo "PYTEST_EXIT=$?"; tail -2 /tmp/a3-count.log
```

Update both numbers in `CLAUDE.md` so the no-key count and the with-key count are each correct and remain one apart. Keep the surrounding explanation — the skip is still gated on `ANTHROPIC_API_KEY` and still makes a live call; only the arithmetic changed.

- [ ] **Step 4c: Two leftovers Task 2's review found**

Both are one-line prose fixes in files Task 2 did not touch, flagged by its reviewer as the same genre of drift that task existed to close.

**`tests/test_workspaces_service.py:1-3`** — the module docstring describes both service halves as sitting "behind the JSON `DELETE /api/workspaces/{id}` endpoint". That is now only true of `soft_delete_workspace_db`. `scrub_workspace_bytes` is exercised directly by one test in that file and is reached by no route at all. Reword so it does not imply the scrub is part of that endpoint.

**`src/call_manager/proposal_assist/retirement.py:46-47`** — the prose reads "headroom. headroom.", a duplicated word. Pre-existing, unrelated to this slice, and a two-second fix while the file is open.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add CLAUDE.md tests/test_workspaces_service.py src/call_manager/proposal_assist/retirement.py frontend/src/cockpit/WorkspaceSettingsModal.tsx frontend/src/cockpit/WorkspaceSettingsModal.test.tsx
git status --short   # confirm nothing unrelated is staged
git commit -m "fix(ui): delete no longer scrubs files, so stop saying it does

Both the description and the confirm prompt promised deletion scrubs uploaded
file bytes. A user may delete a workspace specifically to remove uploads.

Deliberately does NOT promise an admin can restore or remove the contents --
inspection, restore and purge have not shipped. 'You cannot undo this' stays,
because today it is still true."
```

---

## Done when

- The DELETE route contains no call to `scrub_workspace_bytes`, and deleting a workspace leaves attachment, reference and repository bytes **byte-identical** — proven by sentinel contents in the route test, not directory existence.
- `test_delete_runs_db_then_retire_then_scrub_in_that_order` is renamed and pins `DB → retire → 204`, with its DB-then-retire ordering assertion no weaker than before.
- The Step 10 mutation was run against the **route** test and reported: restoring the scrub call turns `test_delete_keeps_every_byte_on_disk` red, with matching md5 before and after.
- `scrub_workspace_bytes` still exists, documented as having no production caller and naming Slice E.
- `grep -rn "future sweep" src/` returns nothing, and neither runtime warning promises one.
- The DELETE route's OpenAPI description no longer mentions scrubbing, and `schema.gen.ts` was regenerated with both exit codes 0.
- `tests/workspace_delete_helper.py` no longer scrubs, and `workspaces_dir` is gone from its signature and every call site.
- The delete copy says contents are kept and still says the owner cannot undo it; it promises no admin capability.
- `uv run pytest -n 16 --dist loadfile` → `3360 passed, 1 skipped`; `ruff check` clean; frontend tests and `tsc --noEmit` clean.
- Every commit was staged file-by-file, with `git status --short` checked before committing.
