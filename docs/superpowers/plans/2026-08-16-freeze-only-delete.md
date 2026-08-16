# Freeze-Only Delete (Retention Slice A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deleting a workspace stops destroying files. It freezes the workspace — users evicted, work stopped, every byte kept — so a later slice can restore it intact.

**Architecture:** Remove the one production call to `scrub_workspace_bytes` from the delete route, stop the test helper composing it, invert every test that asserts deletion removes bytes, and correct the user-facing copy that promises it does. `scrub_workspace_bytes` itself is kept for the future purge slice.

**Tech Stack:** Python 3.12, FastAPI, pytest (+ testcontainers Postgres); React 19 + Mantine on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` (revision 4, Slice A — sections 0.1, 0.2, 2).

## Global Constraints

- **Deletion must destroy nothing on disk.** Everything else deletion does is unchanged: marking `soft_deleted_at`, cancelling jobs and queued turns, terminalizing running turns, evicting collab rooms, running the retirement coordinator.
- **`scrub_workspace_bytes` is KEPT, not deleted.** Slice E's purge needs this logic. It ends this slice with zero production callers, deliberately, and its docstring must say so and name the slice that will call it. Do not delete it as dead code.
- **The inverted tests are the deliverable.** A test that asserts bytes are removed must become one asserting they survive. If a test still passes unchanged, the behaviour did not change.
- **The user-facing copy must stop promising byte removal.** It appears in two places in `WorkspaceSettingsModal.tsx` and both are wrong after this change.
- No database migration. No new routes. No changes to who may delete (already owner-only).
- Work on a branch. Never commit to `master`. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~64s for the full suite).
- **Never pipe pytest through `tail` to read its exit code** — the pipeline's status is `tail`'s. Redirect to a file, then `echo $?`.
- **`pytest` exits 4 on "no tests ran"** — a path typo reads as success. Confirm a nonzero collected count.
- There is no TypeScript linter on the frontend; `pnpm exec tsc --noEmit` is the gate.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/call_manager/web/api/workspaces.py:1530` | the one production scrub call — removed | 1 |
| `src/call_manager/workspaces.py:181` | `scrub_workspace_bytes` docstring — rewritten, function kept | 1 |
| `tests/workspace_delete_helper.py` | stops composing the scrub | 1 |
| `tests/test_workspaces_service.py` and any other test asserting removal | inverted | 1 |
| `frontend/src/cockpit/WorkspaceSettingsModal.tsx:80,90` | confirmation copy | 2 |

---

### Task 1: Delete stops destroying bytes

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (around line 1530)
- Modify: `src/call_manager/workspaces.py` (the `scrub_workspace_bytes` docstring, around line 181)
- Modify: `tests/workspace_delete_helper.py`
- Modify: `tests/test_workspaces_service.py` and every other test the survey in Step 1 turns up

**Interfaces:**
- Consumes: nothing.
- Produces: a delete path that leaves `<workspaces_dir>/<workspace_id>/` untouched. Slice C (restore) depends on this; Slice E (purge) will be the sole caller of the retained `scrub_workspace_bytes`.

- [ ] **Step 1: Survey every test that depends on deletion removing bytes**

Do this before changing anything — you need the full list, and the survey output goes in your report.

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "scrub_workspace_bytes\|soft_delete_workspace_for_test" tests/ | sort
grep -rn "not .*\.exists()\|assert not .*dir" tests/test_workspaces_service.py tests/proposal_assist/test_workspaces_delete_route.py tests/test_api_references.py
```

Record which assertions are about **bytes being gone after a delete** (these invert) versus about something else (these do not). Known starting point: `tests/test_workspaces_service.py:91` (`assert not attach_dir.exists()  # bytes scrubbed`) and its test name `test_soft_delete_workspace_cascades_and_scrubs` at line 26.

- [ ] **Step 2: Invert the byte-survival assertions**

For each assertion found in Step 1 that asserts removal, flip it to assert survival, and rename any test whose name claims scrubbing. In `tests/test_workspaces_service.py`, rename `test_soft_delete_workspace_cascades_and_scrubs` to `test_soft_delete_cascades_and_keeps_bytes` and change line 91:

```python
    assert attach_dir.exists(), (
        "Slice A: deleting a workspace FREEZES it and destroys nothing, so a "
        "later restore is lossless. Only an admin purge removes bytes."
    )
```

Keep every other assertion in that test exactly as it is — the DB cascade behaviour is unchanged and must stay covered.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_workspaces_service.py -q > /tmp/a1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/a1.log
```

Expected: FAIL on the inverted assertions — the bytes are still being scrubbed, because the production change has not been made yet. **If these pass now, stop and report:** it would mean the assertions were not actually exercising the scrub, and the rest of the task would be building on a false premise.

- [ ] **Step 4: Remove the scrub from the delete route**

In `src/call_manager/web/api/workspaces.py`, delete the `scrub_workspace_bytes(...)` call at line 1530 and its now-stale import. Update the comment block directly above it (lines ~1524-1529), which currently ends "THEN scrub bytes, THEN 204":

```python
    # DB soft-delete is durable. Retire in-process (bounded ~5s, never raises):
    # tombstone the turns registry so no new subprocess installs and tear down any
    # live one, and tear down both collab rooms (close editors 1008, no
    # checkpoint). This SUBSUMES the old signal_from_request permission
    # reconciliation — the rooms are gone. THEN 204.
    #
    # Slice A: deletion FREEZES a workspace and destroys nothing on disk. It used
    # to scrub attachment and reference bytes here; that removal is what made a
    # later restore impossible, so it moved to the admin purge (Slice E). Users
    # are already locked out by the liveness gates and the room eviction above —
    # keeping the bytes changes what an admin can recover, not who can reach them.
    signal_workspace_retirement(request, workspace_id=workspace_id)
    return Response(status_code=204)
```

- [ ] **Step 5: Stop the test helper composing the scrub**

`tests/workspace_delete_helper.py` reproduces the production delete for tests that only need the DB half. Production no longer scrubs, so neither should it — otherwise tests exercise behaviour production does not have.

Remove the `scrub_workspace_bytes(...)` call and its import, and replace the docstring's last line:

```python
def soft_delete_workspace_for_test(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    timeout_seconds: int = 10,
) -> None:
    """The DB half of a delete, with no retirement — what production does minus
    the in-process teardown.

    Slice A: this no longer scrubs bytes, because production no longer does.
    A test that specifically wants byte removal should call
    `scrub_workspace_bytes` directly and say why.
    """
    soft_delete_workspace_db(
        session, workspace_id=workspace_id, timeout_seconds=timeout_seconds
    )
```

Note the `workspaces_dir` parameter is now unused — remove it from the signature and from **every call site** the Step 1 survey found. Do not leave it as an ignored argument; a parameter that does nothing is a lie the next reader has to disprove.

- [ ] **Step 6: Rewrite the `scrub_workspace_bytes` docstring**

The function stays. Its docstring is now wrong in three ways: it calls itself "the FS half of a soft-delete" (it is no longer on that path), it describes ordering relative to the DB commit (no longer relevant), and it claims failed cleanups are "collected by a future sweep" (**no such sweep exists anywhere in the codebase** — a verified false statement).

In `src/call_manager/workspaces.py`, replace the docstring with:

```python
    """Best-effort removal of attachment and reference bytes from disk.

    NOT called by the delete path. Slice A made deletion freeze-only: it
    destroys nothing, so a later restore is lossless. This function is retained
    for Slice E's admin purge, which is the deliberate act that removes a
    deleted workspace's files for real — and which will extend it to the whole
    workspace tree rather than these two subtrees.

    It therefore has NO production caller today. That is intentional; deleting
    it would mean re-deriving this logic from git history nobody will think to
    search.

    Attachment and reference DB rows are never touched here — they are the audit
    trail. Never raises on OSError: a failed cleanup leaves orphaned bytes that
    nothing reads and nothing collects. There is no sweep.
    """
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_workspaces_service.py tests/proposal_assist/test_workspaces_delete_route.py -q > /tmp/a1.log 2>&1; echo "EXIT=$?"; tail -8 /tmp/a1.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass, `RUFF_EXIT=0`.

- [ ] **Step 8: Run the full suite**

This slice changes a path many tests touch, so a targeted run is not enough.

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/a1-full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/a1-full.log
```

Expected: `3357 passed, 1 skipped`, exit 0. Any failure here is a test that depended on deletion removing bytes and was missed by the Step 1 survey — invert it the same way and record it in your report as a survey miss.

- [ ] **Step 9: Prove the change is real**

Re-add the `scrub_workspace_bytes(...)` call to the delete route, run `tests/test_workspaces_service.py`, and confirm the inverted assertion FAILS. Remove it again, re-run, confirm green. md5-verify the file before and after so the restore is provably exact, and report both hashes.

- [ ] **Step 10: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/workspaces.py src/call_manager/workspaces.py tests/
git commit -m "feat(delete): freeze-only -- deletion destroys nothing on disk

Deleting a workspace already evicts users, cancels jobs and turns, and tears
down collab rooms. It also scrubbed attachment and reference bytes, which is
what made a later restore impossible: rows survived while their files did not.

Byte removal moves to the admin purge (Slice E). scrub_workspace_bytes is
retained for it and now has no production caller, deliberately.

Its docstring also claimed failed cleanups are collected by a future sweep.
No such sweep exists anywhere in the codebase."
```

---

### Task 2: Delete confirmation stops promising byte removal

The UI tells users deletion scrubs their uploaded files. After Task 1 that is false, and it is the kind of false that matters: a user may delete a workspace *specifically* to remove uploads.

**Files:**
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.tsx` (the description around line 80 and the `window.confirm` string around line 90)
- Test: `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx` (create or extend, following the conventions of the nearest existing cockpit test)

**Interfaces:**
- Consumes: Task 1's behaviour change. No API change — the endpoint and its 204 are unchanged.
- Produces: nothing consumed later.

- [ ] **Step 1: Write the failing test**

Assert the copy no longer promises removal and does state retention. **`renderWorkspaceSettingsModal()` below is illustrative** — read the nearest existing test in `frontend/src/cockpit/` and use whatever render and mocking helpers it actually uses, by their real names. Do not introduce a different testing style, and do not create a helper by that name just to match this snippet.

```tsx
it("does not promise that deleting removes uploaded files", async () => {
  renderWorkspaceSettingsModal();

  const body = await screen.findByTestId("delete-workspace-description");
  expect(body.textContent).not.toMatch(/scrub/i);
  expect(body.textContent).toMatch(/remain|retained|kept/i);
});
```

Add `data-testid="delete-workspace-description"` to the `<p>` in Step 2 so the test has a stable handle.

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test -- WorkspaceSettingsModal 2>&1 | tail -20
```

Expected: FAIL — the current copy contains "scrubs".

- [ ] **Step 3: Correct both strings**

Replace the description paragraph:

```tsx
        <p data-testid="delete-workspace-description">
          Deletes the workspace: it disappears for everyone, collaborators lose access, and any
          running work stops. Its contents are kept and remain accessible to an administrator,
          who can restore it or remove it permanently. You cannot undo this yourself.
        </p>
```

and the confirmation prompt:

```tsx
            if (
              window.confirm(
                `Delete "${name}"? Everyone loses access immediately. The contents are kept ` +
                  `and only an administrator can restore or permanently remove them.`,
              )
            ) {
```

Two things this copy must not do: claim the deletion is unrecoverable (an admin *can* restore it now), and claim files are removed (they are not). It must also not overpromise — the owner cannot restore it themselves.

- [ ] **Step 4: Run the frontend gates**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test 2>&1 | tail -8
pnpm exec tsc --noEmit ; echo "TSC_EXIT=$?"
```

Expected: tests pass, `TSC_EXIT=0`.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/WorkspaceSettingsModal.tsx frontend/src/cockpit/WorkspaceSettingsModal.test.tsx
git commit -m "fix(ui): delete no longer scrubs files, so stop saying it does

The modal promised deletion scrubs uploaded file bytes and cannot be undone.
After Slice A both are false, and a user may delete a workspace specifically
to remove uploads. The copy now says what is true: access ends immediately,
contents are kept, and only an administrator can restore or remove them."
```

---

## Done when

- The delete route contains no call to `scrub_workspace_bytes`, and deleting a workspace leaves `<workspaces_dir>/<workspace_id>/` intact on disk.
- `scrub_workspace_bytes` still exists, with a docstring stating it has no production caller, naming Slice E, and no longer claiming a sweep exists.
- `tests/workspace_delete_helper.py` no longer scrubs, and its `workspaces_dir` parameter is gone from the signature and every call site.
- Every test that asserted deletion removes bytes now asserts they survive; the Step 1 survey list and any Step 8 survey misses are recorded in the report.
- The Step 9 mutation was performed and reported: restoring the scrub call turns the inverted assertion red, with matching md5 before and after.
- The delete confirmation states contents are kept and an administrator can restore them; it no longer says "scrub" and no longer claims the action cannot be undone.
- `uv run pytest -n 16 --dist loadfile` → `3357 passed, 1 skipped`; `uv run ruff check .` clean; frontend `pnpm run test` and `tsc --noEmit` clean.
- No migration, no new route, no change to who may delete.
