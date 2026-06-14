# References: drag-and-drop batch PDF upload + member write access

**Date:** 2026-06-14
**Status:** Approved (design); revised after two Codex review rounds
**Area:** proposal-assist / references

## Problem

Adding references to a workspace today is tedious and restrictive:

1. **One file at a time.** The only upload path is a single-file form (`POST /workspaces/{id}/references`, [references.py:180-262](../../../src/call_manager/proposal_assist/web/references.py#L180)). Loading a paper set means repeating pick-kind-then-upload N times.
2. **Owner-only.** Every reference *write* endpoint calls `_require_owner`; workspace members stored in `workspace_viewers` ([models.py:717](../../../src/call_manager/db/models.py#L717)) are read-only and cannot contribute references.
3. **Manual kind selection.** Each upload forces a `kind` choice (`paper` / `prior_proposal` / `analysis` / `data`).

## Goals

- Drop **multiple PDFs at once** onto a workspace and have them ingested.
- Let **any workspace member** (owner ∪ members), not just the owner, fully manage references.
- Have the **model auto-classify** each dropped PDF's `kind` instead of asking the user.
- Keep the existing single-file picker for **non-PDF material** (preliminary data as CSV/XLSX, etc.).
- Do all of the above without exhausting server memory or stalling unrelated requests under load.

## Non-goals (deferred to separate work)

- System-wide role refactor (the `admin` / `user` model is already approximated by `users.is_admin`; no change here).
- Renaming `workspace_viewers` → a `members`/`workspace_users` table, or a membership migration.
- Extending member write rights **beyond references** (sections, files, turns, annotations stay as-is for now).
- Async/background ingestion queue (kept synchronous; see Approach C below).
- Drag-drop of non-PDF formats.

## Role model for this feature (references-only)

Resolves the conflict Codex raised against the documented owner/editor/viewer model
([2026-06-10 §3.3](2026-06-10-user-system-and-security-hardening-design.md#L83)):

- **Decision:** a workspace has an **owner** (creator) and **members** (everyone else with access, currently stored in `workspace_viewers`). Any member has **full reference rights**: add, edit metadata, change kind, waive, delete, reprocess. There is no read-only tier for references.
- This is an **intentional, accepted privilege expansion**, scoped to references. Existing `workspace_viewers` rows were added under read-only expectations; the absence of a `read_only` system role does **not** prove those users were meant to gain delete/waive rights — it only means no schema-level guarantee is being violated. We therefore treat this as a deliberate product decision, not a "no-op."
- **Rollout prerequisite:** before enabling member writes in any populated environment, **review existing `workspace_viewers` memberships** so no one silently gains delete/waive/reprocess on data they were only meant to read. (Manual review step; see Rollout below.)
- **Supersedes** the 2026-06-10 §3.3 "viewers are read-only" rule **for references only**. Other workspace write surfaces are unchanged. (Note: `web/citations.py` already gates its operations on owner-or-member, so member-write is not unprecedented in the codebase.)
- **Capability:** `can_manage_references` = user is the workspace owner **or** has a `workspace_viewers` row. Because the references panel is already member-gated, this is true for everyone who can open the panel — but it is computed explicitly (not hardcoded) so a future read-only tier can re-narrow it. Kept **separate from `is_owner`**, which continues to gate owner-only surfaces (e.g. the Settings link).

## Decisions (brainstorming + two review rounds)

| Question | Decision |
|---|---|
| Who can manage references | Owner **or** any member (`can_manage_references`). References-only scope. |
| Kind on batch upload | Model auto-classifies dropped PDFs; explicit `auto_classify` signal so it never overwrites a deliberate pick. |
| Accepted drag-drop formats | PDF only (magic-byte verified server-side). Other formats stay on the single-file picker. |
| Batch mechanism | Approach A — throttled parallel POSTs, with **bounded server-side admission (fail-fast 429) + threadpool offload**. |
| Ingest concurrency limiter | A single app-level limiter **shared** by upload, web fetch, and MCP-tool fetch; acquired **before** buffering the body. |
| Delete | New member-gated route over `delete_or_tombstone_reference` (modified to emit its audit event in-transaction). |
| Failed-ingest recovery | Dedup short-circuits on **successful** refs only; failed refs are recovered via an explicit **reprocess** route. |

## Design

### 1. Batch mechanism + bounded server execution (Codex #2, #6)

The drop zone is a frontend enhancement: JS reads dropped PDFs and fires **one `POST /workspaces/{id}/references` per file** (throttled to ~3 concurrent in the browser, politeness only), reusing the existing endpoint. Each completed request returns its `reference_row.html` partial → per-file progress and error isolation.

The real protection is server-side. Two defects in the prior revision are fixed:

- **Bounded admission, not just bounded ingest.** Today the route reads the whole file into memory (`_read_capped`, up to 50 MB) *before* any limiter, so many waiting requests could each retain 50 MB. The limiter must be acquired **before** buffering the body. An app-level `threading.Semaphore(reference_ingest_concurrency)` (default 3) is **try-acquired (non-blocking) up front**; if no slot is free, the request **fails fast with `429`** (and a `Retry-After`) rather than queueing a 50 MB buffer. This bounds both in-flight memory and ingest concurrency to N.
- **Why `threading.Semaphore`, not `asyncio.Semaphore`.** Drafting turns (which run the MCP `fetch_reference` tool) and web requests may not share an event loop, so a loop-bound `asyncio.Semaphore` could not be shared across them. A thread semaphore bounds the real CPU/GPU work regardless of loop, and its non-blocking `acquire(blocking=False)` is safe to call from an async handler.
- **Shared across all ingest paths.** The same limiter guards the upload route, the web fetch route, and the MCP-tool `fetch_reference` path — all of which run the heavy `ingest_bytes`. It is threaded into the tool via `ToolContext` (built by both context factories). "Concurrent ingests capped" is only true if every entry point shares it.
- **Off the event loop, with its own session.** The synchronous `ingest_bytes` is offloaded via `run_in_threadpool`/`asyncio.to_thread`. The request-scoped SQLAlchemy `Session` is **not** passed into the worker thread — the offloaded wrapper opens and commits its **own** session from `session_factory` for all phases (Phase 1 included; Phase 2/3 already use fresh sessions). The route then re-reads the row from a fresh session (as it already does) to render the partial.
- **Batch caps:** client-enforced and documented — max **25** files per drop, max **200 MB** total per drop; over-cap drops are rejected client-side. The 50 MB per-file cap is still enforced server-side. Client caps are UX only; the 429 admission gate is the server's guarantee.

Rejected alternatives: **B** (single multi-file POST) — one slow request, timeout risk, weak progress; **C** (async background queue with polling) — the right move only when batches outgrow synchronous handling; a real architecture change against a single-instance APScheduler. Deferred.

### 2. Permissions, capability wiring & shared guard (Codex #1, #5)

- Extract the membership guard into a new shared module **`proposal_assist/web/_perms.py`** exposing `require_member` (owner or `workspace_viewers` row) and `require_owner`. **Both** `web/references.py` and `web/citations.py` import from it — renaming `_require_owner_or_viewer` in place would break the existing `citations.py` import ([citations.py:41](../../../src/call_manager/proposal_assist/web/citations.py#L41)). `citations.py` keeps identical behavior (member = owner or viewer).
- Apply `require_member` to **all** reference endpoints — read *and* write: panel, raw, upload, fetch, metadata GET/POST, waiver, delete, kind, reprocess.
- Template context passes **two** flags: `is_owner` (Settings link only) and `can_manage_references` (all reference controls). The hardcoded `"is_owner": True` at [references.py:260](../../../src/call_manager/proposal_assist/web/references.py#L260) and every partial-returning route must pass the **computed** flags, never a literal.
- `workspace_references.html`: Settings link behind `is_owner`; upload form, fetch form, drop zone behind `can_manage_references`. `partials/reference_row.html`: metadata-review, waiver, kind dropdown, delete, reprocess behind `can_manage_references`.

### 3. Auto-classification of `kind` + shared kinds constant (Codex #3, #7)

Dropped PDFs carry no user-chosen kind; the single-file picker is unchanged.

- **Shared constant:** move `_ALLOWED_KINDS` out of `web/references.py` into a domain module **`references/kinds.py`** (`ALLOWED_KINDS`), imported by `web/references.py`, `ingest.py`, and `metadata.py`. `metadata.py` (domain) must not import from `web/*` (inverted/circular dependency).
- **Explicit signal:** the drag-drop POST sends `auto_classify=true` (and no `kind`). `ingest_bytes` gains `auto_classify_kind: bool = False`. When true: insert provisional `kind='paper'`, then apply the classified kind in Phase 3 if valid. When false (single-file picker, fetch): the user's explicit `kind` is authoritative and **never** overwritten — a provisional `paper` is thus distinguishable from a deliberate `paper`.
- **No CSL pollution:** today `extract_reference_metadata` feeds the whole LLM dict into `merge_csl` ([metadata.py:91-107](../../../src/call_manager/proposal_assist/references/metadata.py#L91)). Extend the prompt to also emit a **sentinel key `_kind`**; parsing **pops `_kind`** and validates it against `ALLOWED_KINDS` *before* the remaining dict is merged as CSL. `extract_reference_metadata` returns the validated kind (or `None`) alongside `(csl, source, status)`. Single LLM round-trip; CSL stays clean.
- **Fallback:** LLM capped / down / `_kind` missing or invalid → kind stays provisional `paper`. Non-fatal; never blocks ingest.

### 4. Manual kind correction

- Inline kind dropdown per row → **`PATCH /workspaces/{id}/references/{ref_id}/kind`**, member-gated, validated against `ALLOWED_KINDS`, rejects tombstoned refs (404). Returns the updated row partial. Emits an audit event (§7).

### 5. Delete / tombstone with atomic audit (Codex #4)

- New route **`POST /workspaces/{id}/references/{ref_id}/delete`**, member-gated, delegating to `delete_or_tombstone_reference` ([deletion.py:41](../../../src/call_manager/proposal_assist/references/deletion.py#L41)): **cited → tombstone** (bytes/chunks retained), **uncited → hard delete**.
- **Atomic audit:** the helper currently `session.commit()`s internally ([deletion.py:100,127](../../../src/call_manager/proposal_assist/references/deletion.py#L100)), so a separate post-call audit commit could be lost on failure. Since the helper has **no other callers**, modify it to accept `actor_id` and write the `reference.deleted` / `reference.tombstoned` `WorkspaceEvent` **inside its own transaction** (one commit covers the delete/tombstone, the claim-analysis invalidation, and the audit row). The route does not commit audit separately.
- **Confirmation:** the delete button uses `hx-confirm`.
- **Panel excludes tombstoned.** Add `.where(Reference.tombstoned_at.is_(None))` to the panel query ([references.py:157](../../../src/call_manager/proposal_assist/web/references.py#L157)) (matching MCP `list_references`). Both outcomes remove the row from view; on a tombstone the response carries a brief inline note ("retained because it is cited").
- **Reject edits to tombstoned refs:** metadata POST, kind PATCH, waiver POST, and reprocess guard `ref.tombstoned_at is None`, else 404.

### 6. Failed-ingest recovery: dedup + reprocess (Codex #3)

`original_sha256` is set in Phase 1 (before extraction), so even a `text_status='failed'` ref has a hash. Naive "dedup → return existing row" would make a re-dropped failed file silently return its own failed row with no path forward. Resolved by distinguishing failure classes:

- **Pre-ingest / transport failures** (non-PDF 400, oversize 400, overload 429, aborted upload): **no row is created** (they fail before Phase 1 insert), so re-dropping is a clean fresh upload — no dedup conflict.
- **Retained ingestion failures** (Phase 2/3 fail after insert → `text_status='failed'`): the row persists with stored bytes.
- **Dedup short-circuit** ("already present", skip ingest) applies **only** to a same-`(workspace_id, original_sha256)` non-tombstoned ref with `text_status='done'`. A `failed` (or in-progress) same-hash ref does **not** report "already present."
- **Reprocess route:** **`POST /workspaces/{id}/references/{ref_id}/reprocess`**, member-gated, re-runs ingest Stage 2/3 against the already-stored bytes (no re-upload). A failed row renders a **Reprocess** control; the drop-zone failed-state "retry" calls reprocess for retained-failure rows, and re-uploads for transport failures.
- Re-dropping a file whose hash matches an existing **failed** ref returns that existing row (no duplicate) surfaced in its failed+reprocess state — not a misleading "already present."

### 7. Drop-zone UX, progress & validation (Codex #6, prior round)

- A dashed drop area: *"Drop PDF references here."* Also clickable → multi-select dialog.
- **Progressive enhancement:** the single-file `<form>` keeps working with JS disabled. The drop zone + batch logic live in [app.js](../../../src/call_manager/web/static/app.js).
- **Optimistic placeholder rows** with a state machine: **queued** (waiting for a client slot) → **processing** (POST in flight) → **done** (replaced by the returned partial) / **failed** (message + retry) / on **429** → back to **queued** with backoff (server overload is transient, not a failure). The ≤3 client throttle governs queued→processing.
- **Client validation:** non-PDF filtered before upload with a message pointing at the single-file picker; over-cap drops (>25 files / >200 MB) rejected with a message.
- **Server PDF detection:** the auto_classify path verifies the `%PDF-` magic-byte header and rejects mismatches with 400 (don't trust the browser `content_type`). Existing `ALLOWED_KINDS`/MIME allowlist, 50 MB cap, filename sanitization still apply.
- **403 vs 404:** non-members → **403**; a member requesting a ref id from another workspace or non-existent → **404** (never leak existence).

## Affected components

| Component | Change |
|---|---|
| `web/_perms.py` *(new)* | Shared `require_member` / `require_owner`; imported by references + citations web modules. |
| `web/references.py` | Use shared guards on all ref endpoints; compute `is_owner` + `can_manage_references`; `auto_classify` field; up-front 429 admission + shared semaphore + threadpool offload (own session); `%PDF-` sniff; sha256 dedup (done-only); new `delete`, `kind`, `reprocess` routes; panel excludes tombstoned; tombstone guards; audit events. |
| `web/citations.py` | Update import + call sites to `require_member` (behavior unchanged). |
| `references/kinds.py` *(new)* | `ALLOWED_KINDS` shared constant. |
| `references/ingest.py` | `auto_classify_kind` param; provisional `kind='paper'`; apply classified kind in Phase 3; own-session entry for threadpool use; import `ALLOWED_KINDS`. |
| `references/metadata.py` | Prompt emits sentinel `_kind`; parse pops + validates pre-merge; return validated kind; import `ALLOWED_KINDS`. |
| `references/deletion.py` | Accept `actor_id`; emit `reference.deleted`/`reference.tombstoned` audit event in its own transaction. |
| `web/templates/.../workspace_references.html` | Drop zone; `is_owner` (Settings) vs `can_manage_references` (controls). |
| `web/templates/.../partials/reference_row.html` | Inline kind dropdown; delete (`hx-confirm`); reprocess; tombstoned/"already present"/failed states; `can_manage_references` gating. |
| `web/static/app.js` | Drop handling: PDF + cap filter, ≤3 throttle, placeholder state machine incl. 429-backoff + failed/retry/reprocess. |
| `config.py` (+ config tests) | `reference_ingest_concurrency`, `reference_max_batch_files`, `reference_max_batch_bytes`. |
| `main.py` | Construct the shared ingest semaphore on app state. |

## Error handling

- **Per-file upload failure** (corrupt/oversize/non-PDF): placeholder row → `failed` + message + retry; siblings proceed. Retain-on-failure unchanged; retained failures recover via reprocess (§6).
- **Overload (429):** request fails fast before buffering; the drop JS backs off and retries; the event loop stays responsive.
- **Classification failure:** kind stays `paper`; ingest still succeeds; logged, not surfaced.
- **Delete of a cited reference:** tombstoned (retained) with inline note; uncited → hard delete; audit atomic with the action.

## Security & audit (Codex #1, #4)

- New `WorkspaceEvent`s (actor = current user): `reference.added`, `reference.kind_changed` (old/new), `reference.metadata_approved`, `reference.reprocessed`, `reference.deleted` / `reference.tombstoned`. Delete/tombstone events are written in the deletion helper's own transaction (atomic). The existing waiver event is unchanged.
- All reference routes remain workspace-scoped (404 on cross-workspace ids); membership enforced server-side, never UI-only.

## Rollout

1. Land code behind the existing flow (single-file path untouched, works without JS).
2. **Review existing `workspace_viewers` memberships** in each populated environment before announcing member writes — confirm every current viewer is an intended collaborator (they will gain delete/waive/reprocess).
3. Set `reference_ingest_concurrency` for the deployment's GPU/DB headroom (default 3).

## Testing

- **Permissions:** a member (non-owner) can upload / fetch / metadata / kind / waiver / delete / reprocess (previously 403); non-member 403; cross-workspace ref id 404. Settings stays owner-only; controls follow `can_manage_references`; no route returns a hardcoded capability flag. `citations.py` still works after the guard move.
- **Bounded admission:** at capacity, a new upload returns 429 **without** buffering the body; the limiter is shared across upload + fetch + tool paths; concurrent ingests never exceed N.
- **Thread session:** the request session is never used inside the worker thread; the offloaded ingest opens/commits its own session.
- **Auto-classify:** `_kind` parsed, popped, applied; CSL never contains `_kind`; invalid/missing → stays `paper`; explicit-kind single-file upload never overwritten.
- **Delete + atomic audit:** cited → tombstoned (+ note, row removed) with a `reference.tombstoned` event in the same transaction; uncited → deleted with `reference.deleted`; panel excludes tombstoned; metadata/kind/waiver/reprocess on a tombstoned ref → 404; delete requires confirmation.
- **Dedup vs reprocess:** re-dropping an identical **done** PDF returns "already present" (no second ingest); re-dropping a **failed** PDF returns the failed row with reprocess (no duplicate); reprocess re-runs Stage 2/3 on stored bytes and flips `failed → done`.
- **Drop zone (app.js tests):** non-PDF/over-cap rejected; ≤3 concurrency; placeholder transitions incl. 429-backoff and failed→retry/reprocess; single-file form works with JS disabled.
- **Server PDF sniff:** non-PDF body with PDF `content_type` on the auto_classify path → 400.
- **Config:** new settings parse with correct defaults/bounds.

## Future work (deferred)

- Async/background ingestion (Approach C) if batches outgrow synchronous handling.
- System-wide role refactor: rename `workspace_viewers` → members, extend member writes to sections/files/turns/citations/annotations, reconcile the full 2026-06-10 role design.
- Drag-drop for non-PDF formats; enforce sha256 dedup with a DB unique index.
