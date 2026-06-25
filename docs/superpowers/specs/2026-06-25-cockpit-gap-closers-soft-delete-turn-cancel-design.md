# Cockpit Gap-Closers — Soft-Delete + Turn-Cancel (Design Spec)

**Date:** 2026-06-25
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the legacy Jinja `/workspaces/{id}` workspace UI.
**Builds on:** Jinja teardown G1 (sever) merged at master `beff5de`.
**Blocks:** Jinja teardown G2 (delete) — spec `d9cca04`. G2's "pure orphan-removal" premise
holds for the two routes below **only after this slice lands**.

---

## 1. Why this slice exists

The cockpit cutover (Slices A–F) was believed feature-complete, but a per-route parity audit
during G2 plan-prep found **two owner-only Jinja routes with no React/`/api` equivalent**:

1. **Workspace soft-delete** — `POST /workspaces/{id}/delete`
   ([`proposal_assist/web/workspaces.py:1251`](../../../src/call_manager/proposal_assist/web/workspaces.py)).
2. **Turn-cancel** — `POST /workspaces/{id}/turns/{job}/cancel`
   ([`proposal_assist/web/workspaces.py:1347`](../../../src/call_manager/proposal_assist/web/workspaces.py)).

Deleting the Jinja UI (G2) without first replacing these would be a **functional regression**: an
owner would lose the ability to delete a workspace or cancel a queued turn. This slice migrates
both to React + `/api` so G2 becomes a true orphan-removal. (A third, larger gap — the
claim-analysis annotation workflow — is its **own** subsequent feature slice; out of scope here.)

This is **additive only**: no DB schema change, and no behavior change to any existing surface.
The Jinja routes themselves are **not** deleted here (G2 deletes them); this slice only adds their
replacements and moves the *kept-behavior* tests off the doomed route.

---

## 2. Goal & invariant

**Goal:** Owner-only soft-delete and turn-cancel are reachable from the React cockpit through
typed `/api` endpoints, with parity to today's Jinja behavior (cascade cancels + byte scrub on
delete; race-safe queued-only cancel + audit event on cancel).

**Invariant after this slice:**
1. An owner can soft-delete a workspace from the cockpit settings modal and cancel an in-flight
   turn from the cockpit chat — without touching any Jinja page.
2. The cascade-cancel behavior on soft-delete (coherence/evaluator/review/uncited-claims jobs +
   queued turns) and the byte-scrub of attachments/references are byte-for-byte the same logic as
   today, now invoked from a shared service.
3. The `cloud_turn_cancelled` audit event and race-safe "no-op once running" semantics of
   turn-cancel are preserved.
4. The kept-behavior cascade tests target the `/api` surface; the Jinja routes still pass their
   own dedicated tests (regression net) until G2 deletes them.

No DB schema change. No new dependency.

---

## 3. Architecture

### 3.1 Backend — soft-delete

**Extract a domain service.** Add to [`src/call_manager/workspaces.py`](../../../src/call_manager/workspaces.py)
(the non-web home of `create_workspace`):

```python
def soft_delete_workspace(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path
) -> None:
    ...
```

The body is the **verbatim orchestration** currently inline in the Jinja `soft_delete` handler,
in the same order (the order is a prior Codex-finding fix — do not reorder):

1. Set `ws.soft_deleted_at = datetime.now(UTC)`.
2. `cancel_coherence_jobs_for_workspace(session, workspace_id=…)`
3. `cancel_evaluator_jobs_for_workspace(session, workspace_id=…)`
4. `cancel_review_runs_for_workspace(session, workspace_id=…)`
5. `cancel_uncited_claims_jobs_for_workspace(session, workspace_id=…)`
6. For each `TurnJob` with `status == "queued"`:
   `record_terminal_outcome(session, …, outcome="cancelled", expected_status="queued",
   raw_error="workspace soft-deleted", emit_chat=False)`
7. `session.commit()`  — DB-durable state first.
8. Best-effort byte scrub **after** commit (errors logged, non-fatal):
   `AttachmentStorage(root=workspaces_dir).delete_workspace(workspace_id=…)` then
   `ReferenceStorage(root=workspaces_dir).delete_workspace(workspace_id=…)`.

The function takes `workspaces_dir: Path` (resolved from `settings.workspaces_dir` by the caller),
**not** a `Request` — it is pure orchestration with no web coupling. The caller passes the
`require_owner`-resolved workspace's id.

**New `/api` endpoint.** In [`src/call_manager/web/api/workspaces.py`](../../../src/call_manager/web/api/workspaces.py):

```
DELETE /api/workspaces/{workspace_id}  → 204 No Content
```

- Owner-only via `require_owner` (the `web/api` guard, session-factory context-manager pattern —
  mirror [`web/api/settings.py`](../../../src/call_manager/web/api/settings.py) `remove_member`).
- Resolves the workspace (cross-owner → `require_owner` raises 403), then calls
  `soft_delete_workspace(session, workspace_id=ws.id, workspaces_dir=Path(settings.workspaces_dir))`.
- Returns `Response(status_code=204)`. **Idempotent**: deleting an already-soft-deleted workspace
  still returns 204 (the cascade helpers and the `soft_deleted_at` set are no-op-safe; this matches
  the Jinja route, which never special-cased a re-delete). The 204 carries no body (the JSON
  envelope does not wrap empty 2xx — same as `remove_member`).

**Repoint the doomed Jinja route.** Change the Jinja `soft_delete` handler body
([`proposal_assist/web/workspaces.py:1251`](../../../src/call_manager/proposal_assist/web/workspaces.py))
to: `_require_owner(...)` → `soft_delete_workspace(session, workspace_id=ws.id,
workspaces_dir=Path(settings.workspaces_dir))` → `RedirectResponse("/workspaces", 303)`. This
removes the inline duplicate **now** (rather than carrying it to G2) and turns the route's existing
dedicated test (`test_web_workspaces_delete.py`) into a free regression net proving the extraction
is behavior-identical. The route + that test are deleted in G2; the **service** is the durable home.

### 3.2 Backend — turn-cancel

**New `/api` endpoint.** In [`src/call_manager/web/api/turns.py`](../../../src/call_manager/web/api/turns.py)
(natural home — it owns the turn lifecycle surface):

```
POST /api/workspaces/{workspace_id}/turns/{job_id}/cancel  → 200 {"cancelled": bool}
```

- Owner-only via `require_owner` (session-factory context-manager pattern, matching `submit_turn`).
- Body is a **verbatim reuse** of the Jinja handler:
  ```python
  did = record_terminal_outcome(
      session, turn_job_id=job_id, workspace_id=ws.id,
      outcome="cancelled", expected_status="queued", raw_error=None, emit_chat=True,
  )
  session.commit()
  return CancelTurnResponse(cancelled=did)
  ```
- **Race safety + IDOR are inherited from the helper**: the conditional `UPDATE … WHERE
  status='queued' AND workspace_id=…` means a turn already moved to `running` → `cancelled=false`
  ("too late"), and a `job_id` belonging to another workspace → `cancelled=false` (the
  `workspace_id` predicate fails) with no row leak. Non-owner → 403 from `require_owner`.
- `record_terminal_outcome(outcome="cancelled")` writes the `cloud_turn_cancelled` `WorkspaceEvent`
  and (with `emit_chat=True`) the visible `turn_outcome` chat row — the same audit + surface as the
  Jinja route. No reorder, no extra event.
- **The Jinja `cancel_turn` route is left unchanged** (deleted in G2). It is a thin
  `record_terminal_outcome` call with no inline logic worth extracting; repointing it would be
  churn on doomed code for no dedup gain (unlike soft-delete, whose 8-step body is substantial).

### 3.3 Frontend — settings-modal delete (owner-only)

[`WorkspaceSettingsModal.tsx`](../../../frontend/src/cockpit/WorkspaceSettingsModal.tsx) (already
owner-only — opened from the `TitleBar` gear, gated on `meta?.role === "owner"`) gains a separated
**Danger zone**:

- A visually separated section below Save with a short caption ("Soft-deletes the workspace and
  scrubs uploaded file bytes. Audit rows are kept. This cannot be undone.") and a `Delete workspace`
  button.
- On click → `window.confirm` echoing the title:
  `Delete "<title>"? This scrubs all uploaded file bytes and cannot be undone.` (the member-remove
  idiom in `CockpitSidebar`). The title comes from the loaded `settings.data.title`.
- On confirm → `useDeleteWorkspace(id)` mutation (`DELETE /api/workspaces/{id}`). `onSuccess`:
  `navigate("/cockpit")` (the workspace is now 404; mirrors the Jinja redirect to `/workspaces`)
  **and** invalidate the `["workspaces"]` list query so the overview drops the row. Close the modal.
- Error → an inline `"Could not delete workspace."` note (same style as the existing
  `update.isError` note); the button is disabled while `delete.isPending`.

New api hook in [`frontend/src/api/settings.ts`](../../../frontend/src/api/settings.ts):
`useDeleteWorkspace(id)` — `api.DELETE("/api/workspaces/{workspace_id}", …)`, invalidates
`["workspaces"]`. (Navigation is done by the caller via `useNavigate`, not in the hook.)

### 3.4 Frontend — chat cancel (owner-only)

[`CockpitChat.tsx`](../../../frontend/src/cockpit/chat/CockpitChat.tsx) footer: while a turn is
in flight and the viewer is the owner, show a cancel affordance.

- Condition: `d.is_owner && (jobId || d.running_job_id)`. (`is_owner` is already on the
  `TurnsResponse` read-model; `running_job_id` covers `queued|running`.)
- Render an "Assistant is working… **Cancel**" row above the (disabled) `Composer`. The job id to
  cancel is `jobId ?? d.running_job_id`.
- On click → `useCancelTurn(id)` mutation (`POST .../turns/{job_id}/cancel`). On settle, invalidate
  `["turns", id]`. If the response is `cancelled === false`, show a brief inline note
  `Too late — the assistant already started.` (faithful to the Jinja "Too late…" copy). On
  `cancelled === true`, the read-model refetch clears the in-flight state; the `cloud_turn_cancelled`
  chat row appears via the normal turns refetch.
- The button is disabled while `cancel.isPending`.

New api hook in [`frontend/src/api/turns.ts`](../../../frontend/src/api/turns.ts):
`useCancelTurn(id)` — takes `jobId`, `api.POST(".../turns/{job_id}/cancel")`, `onSettled`
invalidates `["turns", id]`.

---

## 4. Tests

### 4.1 New backend tests
- **`DELETE /api/workspaces/{id}`** (extend `tests/test_api_workspaces.py` or a new
  `tests/test_api_workspaces_delete.py`):
  - owner delete → 204; `soft_deleted_at` set; a seeded queued coherence/evaluator/review/
    uncited-claims job flips to `failed`; a seeded queued `TurnJob` flips to `cancelled`; seeded
    attachment/reference bytes on disk are gone (DB rows remain).
  - non-owner (viewer and stranger) → 403; unknown workspace → 404 (from `require_owner`).
  - re-delete of an already-deleted workspace → still 204 (idempotent).
- **`POST /api/workspaces/{id}/turns/{job}/cancel`** (new `tests/test_api_turns_cancel.py`):
  - owner cancels a `queued` job → `{cancelled: true}`; job is `cancelled`; a `cloud_turn_cancelled`
    `WorkspaceEvent` exists; a `turn_outcome` chat row exists.
  - job already `running` → `{cancelled: false}`; status unchanged.
  - non-owner → 403; a `job_id` from another workspace → `{cancelled: false}` (no row leak), status
    unchanged in its real workspace.

### 4.2 Retarget the kept cascade tests (exactly these 4) → `DELETE /api/workspaces/{id}`
These assert **kept** behavior (job cascade on delete), so they follow the kept surface; moving them
now keeps **G2 a pure deletion**. Change each from
`client.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)` asserting `(200, 303)` to
`client.delete(f"/api/workspaces/{ws_id}")` asserting `204`:
- `tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py`
- `tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py`
- `tests/proposal_assist/review/test_soft_delete_cancels_review.py`
- `tests/proposal_assist/uncited_claims/test_soft_delete.py`

**Completeness gate (for the plan):** re-grep for any **kept** test that POSTs to
`/workspaces/{id}/delete` or `/turns/{job}/cancel` and verify the retarget set is exhaustive. The
audit found only these 4 are kept; every other `/delete`-touching file
(`test_web_workspaces_{settings,files,references,delete}.py`, `test_web_references_delete.py`) is a
**doomed** Jinja-route test G2 deletes wholesale — leave those untouched (their `/delete` usage is
incidental or is the dedicated route test). `tests/test_api_references.py` does **not** use the
workspace soft-delete route (its `/delete` is `references/{rid}/delete`, a kept route) — leave it.
The cancel route is hit only by `test_web_workspaces_{cancel,turns}.py`, both doomed — no kept
cancel test to retarget.

### 4.3 Frontend tests
- `WorkspaceSettingsModal.test.tsx`: delete button present; clicking with a confirmed
  `window.confirm` fires the DELETE and navigates to `/cockpit`; a declined confirm does nothing;
  error path shows the note. (Mock `window.confirm`; mock the router `navigate` per the existing
  test setup.)
- Chat cancel (extend `CockpitChat.test.tsx` or a focused test): cancel row visible only when
  `is_owner && in-flight`; hidden for a viewer; clicking fires the POST and invalidates turns;
  a `{cancelled: false}` response renders the "too late" note.

---

## 5. OpenAPI regen

After the two endpoints land, regenerate with
`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`. Expected: `frontend/src/api/schema.gen.ts`
**gains** the `DELETE /api/workspaces/{workspace_id}` and
`POST /api/workspaces/{workspace_id}/turns/{job_id}/cancel` paths (purely additive — no route is
deleted this slice). `frontend/src/routes.gen.ts` unchanged (no new React route; the controls live
in the existing settings modal and chat). Regen must run **after** the endpoints exist so the
generated types include them.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots clean.
- Backend suites green, including the 4 retargeted cascade tests, the new delete/cancel `/api`
  tests, and the **unchanged** `test_web_workspaces_delete.py` (the repointed Jinja route's
  regression net) + `test_web_workspaces_cancel.py`. `ruff` clean.
- Frontend `tsc` clean, `vitest` green (new modal + chat tests), `build` clean.
- Schema diff is **additive-only**: the two new paths added, nothing removed.
- Manual/behavioral parity check: the extracted `soft_delete_workspace` produces the same DB +
  filesystem end-state as the prior inline route (covered by the regression net + retargeted tests).

---

## 7. Risks / notes

- **Touching doomed code (the Jinja `soft_delete` repoint, §3.1).** Deliberate: it dedups the
  compliance-sensitive 8-step body immediately and the route's existing test becomes a regression
  net for the extraction. The diff on the doomed module is small and the module is deleted days
  later in G2. The turn-cancel Jinja route is **not** repointed (no dedup payoff) — asymmetry is
  intentional.
- **Extraction must be behavior-preserving.** Mitigation: the existing `test_web_workspaces_delete.py`
  exercises the repointed route end-to-end; the 4 retargeted cascade tests exercise the new `/api`
  path; both hit the same service. A divergence fails one of them.
- **Idempotent re-delete.** The `/api` DELETE returns 204 even on an already-deleted workspace
  (matches the Jinja route). If a stricter 404-on-already-deleted is ever wanted, that is a
  deliberate later change, not part of this parity slice.
- **Cancel affordance shows for `queued|running`.** Cancel only succeeds while `queued`; a click on
  a `running` job returns `{cancelled:false}` and surfaces "too late." This is the faithful Jinja
  behavior; distinguishing `queued` vs `running` in the read-model is unnecessary extra work
  (YAGNI).

---

## 8. Out of scope

- Deleting the Jinja `soft_delete` / `cancel_turn` routes and their dedicated tests
  (`test_web_workspaces_delete.py`, `test_web_workspaces_cancel.py`) → **G2** (spec `d9cca04`).
- The **claim-analysis annotation workflow** migration → its own subsequent feature slice.
- Any change to the soft-delete cascade/scrub logic or the cancel race semantics — this slice
  relocates and re-exposes existing behavior, it does not change it.
- A workspace-list-row delete affordance (the approved placement is the settings modal only).
- Hard-delete, undelete/restore, or a deleted-workspaces view — not part of today's behavior.
