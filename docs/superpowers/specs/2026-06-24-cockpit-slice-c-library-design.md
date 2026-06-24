# Cockpit Cutover Slice C — Library (reference management + image attachments)

**Date:** 2026-06-24
**Status:** Design — ready for plan
**Sub-project:** Cockpit cutover (sub-project 1 of the full-React migration)
**Builds on:** Slice A front-door (`fb43638`) + Slice B chat

## 1. Context & goal

The React cockpit already has the **read** side of references — list, chunk
browsing, and the full select-to-cite flow (`useReferences` / `useReferenceChunks`
/ `useAddCitation` + `CitationComposer`; the sidebar References group renders a
static read-only list). It has **no** way to *manage* references (add / edit
metadata / waive / change kind / delete / reprocess), and the chat Composer's
image affordance is a disabled stub ("arrive with the Files slice"). Both still
live only in Jinja.

**Slice C closes both gaps as one "Library" slice:** the reference
write/management set at full parity with the Jinja UI, *and* the chat image
attachments deferred from Slice B. Everything is built as **additive typed
`/api` endpoints that reuse the existing service functions** (no logic
reimplementation) plus React Query mutation hooks and a new bottom-panel
**Library** tab. **No DB schema change.** The Jinja reference/files routes keep
working (coexistence until the Slice G teardown).

Goal in one sentence: *a workspace member can add, curate, and delete references
and attach images to chat turns entirely in the React cockpit, at parity with
the Jinja workspace UI.*

## 2. Scope

**In scope**

*References (member-gated, mirroring Jinja):*
- Add via **upload** (kind + auto-classify), **drag-drop** (SHA-256 dedup), and **identifier fetch** (DOI / PMID / arXiv / URL).
- Edit/approve **CSL metadata** (title / authors / year / venue / DOI; server regenerates the citekey).
- **Metadata waiver** (override the fail-closed citation gate).
- **Change kind** (paper / prior_proposal / analysis / data).
- **Delete** (hard-delete if uncited, tombstone if cited — surface the `{outcome}`).
- **Reprocess** a failed extraction.
- **Status badges** (`text_status` / `metadata_status`).

*Attachments / media (writes owner-gated, mirroring Jinja):*
- **Upload / list / delete** workspace image attachments (PNG/JPEG/WebP/GIF).
- **Include images in a chat turn** (Composer picker + inline upload → `attachment_ids`).
- **Render image blocks** in the transcript (`TurnList`, currently skipped).

*Home:* a new bottom-panel **Library** tab (References section + Files section);
the sidebar References group stays the quick read/cite view; the Composer (right
pane) gains the include picker.

**Out of scope (YAGNI / pre-existing Jinja gaps — not parity, deferred):**
- Search / filter / sort beyond newest-first; bulk multi-select operations.
- Reference versioning / replace; manual citekey override; cross-workspace dedup; tagging/annotation; metadata auto-refresh.
- Attachment captions/descriptions UI, thumbnails, token-cost preview, batch image upload progress UI beyond a simple queue.
- New binary-serve endpoints — reference download and image bytes reuse the existing Jinja `/raw` and `/files/{aid}` routes via plain links / `<img src>`.
- Any DB schema change. The Jinja routes are NOT removed (Slice G owns teardown).

## 3. Architecture overview

```
Bottom panel:  Inbox · Problems · Activity · [Library]      ◀── new tab
                                              │
                 ┌────────────────────────────┴───────────────────────┐
                 │  References section          Files section          │
                 │  + Add / + Fetch / drop      + Upload / drop         │
                 │  rows: status·kind·actions   rows: thumb·delete      │
                 └──────────────┬───────────────────────┬──────────────┘
   api/references.ts mutations  │                       │  api/files.ts (useFiles + mutations)
                 ┌──────────────▼───────────┐  ┌─────────▼───────────────┐
  POST /api/.../references          (upload) │  GET  /api/.../files (list)│
  POST /api/.../references/fetch            │  POST /api/.../files (upld) │
  POST /api/.../references/{rid}/metadata   │  DELETE /api/.../files/{aid}│
  POST /api/.../references/{rid}/waiver     └─────────┬───────────────────┘
  PATCH /api/.../references/{rid}/kind                │  attachment_ids
  POST /api/.../references/{rid}/delete   Composer (right pane) ──▶ POST /api/.../turns
  POST /api/.../references/{rid}/reprocess          (Slice B endpoint + attachment_ids)
                 │ reuse services (no reimplementation)
   ingest_bytes · detect_source_type · delete_or_tombstone_reference · reprocess_reference
   · reference_ingest_limiter.slot() · attachments storage/tokens · submit_user_turn
```

The migration discipline holds: additive `/api` → React route/components under
the existing AppShell → the Jinja routes stay until Slice G. `web/api/workspaces.py`
is already ~1000 lines, so the new endpoints go in **new modules**
(`web/api/references.py`, `web/api/files.py`) mounted under `/api`, not appended
to `workspaces.py`.

## 4. Backend — references `/api` (`web/api/references.py`)

A new member-gated router (perms via `require_member` from
`proposal_assist.web._perms`), reusing the exact service functions the Jinja
routes call, acquiring the `reference_ingest_limiter` slot before any ingest, and
emitting the **same `WorkspaceEvent` audit rows** the Jinja routes write (so the
Activity feed and parity hold). The runtime error envelope
(`{error:{code,message,detail?}}`) is produced by the global HTTPException
handler; handlers `raise HTTPException`. **There is NO global `OverCapacity`
handler** (`main.py` registers handlers only for `StarletteHTTPException`,
`AccountInactive`, `RequestValidationError`, and bare `Exception`), so each
ingest/reprocess handler must catch `OverCapacity` locally and raise
`HTTPException(429, …, headers={"Retry-After": …})` — mirroring the Jinja
per-route catches (`references.py:272/383/787`). `OverCapacity` is raised by
`reference_ingest_limiter.slot()` (admission control), NOT by `ingest_bytes`
itself.

| Endpoint | Body | Reuses | Returns | Errors |
|---|---|---|---|---|
| `POST .../references` | multipart `file`, `kind`, `auto_classify` | `ingest_bytes` (under `limiter.slot()`, in `run_in_threadpool`) | `IngestResultOut {reference_id, text_status, text_error}` | bad MIME/size→400, `OverCapacity`→429+Retry-After |
| `POST .../references/fetch` | `{input, kind}` | `detect_source_type` dispatch → `ingest_bytes` | `IngestResultOut` | invalid/unresolvable source→400, `SSRFError`→400, `FetchError`→502, `OverCapacity`→429 |
| `POST .../references/{rid}/metadata` | `{title, authors: list[str], year?, venue?, doi?}` | `_parse_authors` + `generate_citekey` (service fns); CSL build + citekey upgrade + **both IntegrityError-retries inlined in the handler**, mirroring the Jinja route — see Notes | `MetadataOut {citekey, metadata_status, csl_json}` | missing title→400, not-found→404 |
| `POST .../references/{rid}/waiver` | `{waiver: bool, reason?}` | the waiver setter | `WaiverOut {metadata_waiver, reason}` | not-found→404 |
| `PATCH .../references/{rid}/kind` | `{kind}` | kind update | `KindOut {kind}` | bad kind→400, not-found→404 |
| `POST .../references/{rid}/delete` | — | `delete_or_tombstone_reference` | `DeleteResultOut {outcome: "deleted"｜"tombstoned"}` | `ValueError` (not-found/cross-workspace)→404 |
| `POST .../references/{rid}/reprocess` | — | `reprocess_reference` (under `limiter.slot()`) | `IngestResultOut` | `ReprocessConflict`→409, `OverCapacity`→429 |

Notes:
- **Authors (list → string contract):** the request carries `authors` as a
  `list[str]` (typed-API friendly), but `_parse_authors(text: str)` splits on
  newlines internally (`references.py:447/459`), so the handler must
  `"\n".join(authors)` **before** calling it: `authors: list[str] → "\n".join →
  _parse_authors(str)`. No new author/CSL parsing.
- **Reuse boundary = the existing service functions; Slice C touches ZERO Jinja
  routes and extracts NO new shared helper.** The complex/risky logic already
  lives in service functions (`ingest_bytes`, `detect_source_type` +
  `dispatch_fetch`, `delete_or_tombstone_reference`, `reprocess_reference`,
  `generate_citekey`, `_parse_authors`, `_read_capped`) — each `/api` handler
  calls those directly. The thin per-route orchestration around them (acquire
  `limiter.slot()`, the drag-drop PDF-magic + SHA-256 dedup SELECT, the 50 MB
  cap via `_read_capped`, the one-line audit write) is *wiring, not logic*, and
  is written inline in the handler, mirroring the proven Jinja flow step-for-step
  — do NOT skip a step (a thin wrapper that drops the slot/dedup/cap reintroduces
  the overload/abuse paths the Jinja layer guards). The whole web app is being
  retired to React (Jinja deleted route-by-route; these reference routes go at
  **Slice G**), so refactoring the working Jinja routes to share a helper would
  be throwaway churn — the short-lived Jinja↔`/api` duplication collapses to a
  single copy when Slice G deletes the Jinja routes.
- **Two IntegrityError-retries (both must appear in the `/api` metadata handler,
  copied from the Jinja route):** (1) the **citekey-collision** retry *inside*
  `generate_citekey`/`ingest.py:249-279` (author-year suffix fallback) — inherited
  for free by calling `generate_citekey`; (2) the **metadata_approved audit-commit**
  retry the Jinja route wraps around `session.commit()` (`references.py:579-605`)
  — the `/api` handler must include this same `try/except IntegrityError →
  rollback → re-fetch → re-apply → commit` loop, or concurrent author-year
  approvals 500. The handler builds the CSL dict from the body (reusing
  `_parse_authors` on `"\n".join(authors)`), so the two retries stay distinct.
- **Audit (exact parity strings):** each write emits the *exact* `WorkspaceEvent`
  kind the reused route writes — verified against the code:
  `reference.added`, `reference.metadata_approved`,
  `reference_metadata_waived` / `reference_metadata_waiver_cleared` (waiver
  on/off — note the underscore form), `reference.kind_changed`,
  `reference.reprocessed`, `reference.tombstoned` / `reference.deleted`
  (`references.py:289/583/655/698/799`, `deletion.py:97/143`). The **Activity
  feed maps these exact strings** (`proposal_assist/activity/feed.py:73`), so a
  drifted kind silently breaks the feed — the §14 audit tests assert the literals.
- **Library read-model (additive):** §9's Library rows need fields the current
  `GET /api/.../references` (`ReferenceListItemOut`, `workspaces.py:925` —
  `{id, citekey, title, kind, text_status}`) does not return. **Additively extend
  `ReferenceListItemOut`** with `metadata_status`, `metadata_waiver: bool`,
  `text_error: str | None`, and `filename`. The sidebar's `useReferences` is
  unaffected (it ignores the extra fields); the Library reuses the same hook +
  endpoint. The read endpoint stays in `workspaces.py` (the new module is
  writes-only); mutations invalidate `["references", id]` to refresh both.

## 5. Backend — files `/api` (`web/api/files.py`)

A new router for image attachments. **Writes are owner-gated** (`require_owner`),
list is member/viewer (`require_member`) — mirroring the Jinja `files.py` exactly
(this asymmetry vs references is intentional parity, not a new policy).

| Endpoint | Perms | Body | Reuses | Returns |
|---|---|---|---|---|
| `GET .../files` | member (= owner-or-viewer) | — | attachment query | `FileListItemOut[] {id, filename, mime_type, uploaded_at}` |
| `POST .../files` | owner | multipart `file` only | `files.py` upload core: `_probe_image_format` (Pillow), `_read_capped` (5 MB), `estimate_image_tokens`, atomic storage; **kind is hardcoded `"figure"`** as in Jinja | `FileOut {attachment_id, filename, mime_type}` |
| `DELETE .../files/{aid}` | owner | — | row delete + best-effort unlink | **204 No Content** |

- **No `kind` param on upload (parity):** Jinja `upload_file` accepts only
  `file: UploadFile` and hardcodes `kind="figure"` (`files.py:244/311`) — there is
  no kind to reuse. Slice C does the same. A kind selector would be a *new*
  capability beyond Jinja → out of scope.
- **`FileListItemOut` is intentionally lean** (`{id, filename, mime_type,
  uploaded_at}`) — the Jinja `file_row.html` renders only thumbnail + filename +
  delete, never `width_px`/`height_px`. Those columns exist on the model but are
  nullable (`models.py:1195` — pre-B7 rows) and are not displayed, so they are
  omitted from the read-model (a future enhancement that surfaces dimensions must
  type them `int | None`).
- **`DELETE` returns 204 No Content** (a JSON-era choice; the Jinja route's
  empty-200/303-redirect is form-driven HTML and is deliberately NOT mimicked).
- Reuse the module-private helpers `_ALLOWED_MIMES`, `_MAX_BYTES`,
  `_probe_image_format`, `_read_capped`, `_sanitize_filename`, `_SUFFIX_BY_MIME`
  from `proposal_assist/web/files.py`. They are underscore-prefixed; importing
  them across modules is acceptable, but confirm at plan time (or re-export
  without the underscore) rather than assume.
- **Image bytes are served by the existing Jinja** `GET /workspaces/{id}/files/{aid}`
  (returns image bytes, `Cache-Control: private`; cookie-authed); the React
  `<img src>` points there. No new binary-serve endpoint.

## 6. Backend — turn extension (`web/api/turns.py`)

`SubmitTurnIn` (Slice B) currently has `content` + `reference_ids`. Add
`attachment_ids: list[str] = []`; in `submit_turn`, pass
`attachment_ids=[uuid.UUID(a) for a in body.attachment_ids]` to `submit_user_turn`
(which already accepts the param and enforces `MAX_TURN_INCLUSIONS = 5` across
`attachment_ids` + `reference_ids`, raising `ValueError` over-cap → map to 400).
Purely additive; no change to streaming or the read-model.

## 7. OpenAPI codegen

Regenerate `frontend/src/api/schema.gen.ts` with
`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (mandatory prefix — the
`.env` mxbai default corrupts the output). The new `references`/`files` paths +
the extended `SubmitTurnIn` are additive.

## 8. Frontend — hooks

- `api/references.ts` (new): `useUploadReference`, `useFetchReference`,
  `useApproveMetadata`, `useToggleWaiver`, `useUpdateKind`, `useDeleteReference`,
  `useReprocessReference` — each invalidating `["references", id]` on settle.
  Reuse `unwrap`/`ApiError`. Multipart upload via `openapi-fetch`'s body-serializer
  bypass or a thin `fetch` for the `FormData` POST (the codebase pattern — confirm
  at plan time; upload may need a raw `fetch` with the session cookie since
  `openapi-fetch` JSON-serializes by default).
- `api/files.ts` (new): `useFiles(id)` (query `["files", id]`), `useUploadFile`,
  `useDeleteFile` (invalidate `["files", id]`).
- Extend the Slice B `useSubmitTurn` to accept + send `attachment_ids`.

## 9. Frontend — Library bottom-panel tab

A new **Library** tab registered in `BottomPanel.tsx` alongside
Inbox·Problems·Activity (add the `"library"` bottom-tab key to the cockpit nav
type + the tab button + the `activeTab === "library"` render branch).

The rows read from the extended `ReferenceListItemOut` (§4 Library read-model)
via the existing `useReferences` hook. `LibraryPanel` contents:
- **References section:** an add area (drag-drop dropzone + a file picker with a
  *reference* kind selector + an identifier-fetch input) and a table of
  non-tombstoned references — each row labels with **`title → citekey → filename`
  fallback** (`title`/`citekey` are often null until approved; `filename` always
  present — a deliberate improvement over Jinja's filename-only row, stated as
  such). Row shows kind (inline dropdown → `useUpdateKind`), `text_status` +
  `metadata_status` badges, and actions: **edit metadata** (opens a modal —
  title/authors/year/venue/DOI → `useApproveMetadata`; the modal is a React
  improvement over Jinja's separate full-page form, not a parity gap), **waiver**
  toggle, **reprocess** (shown on `failed` → `useReprocessReference`,
  409→"already processing"), **download** (link to the Jinja `/raw`), **delete**
  (confirm dialog → `useDeleteReference`, feedback distinguishes `tombstoned` vs
  `deleted`).
- **Files section:** an upload area (dropzone + picker) and a list of attachments
  — thumbnail (`<img src=/workspaces/{id}/files/{aid}>`), filename, **delete**
  (owner-only; confirm). (Dimensions are not shown — parity with Jinja's
  thumbnail+filename row.)
- The drag-drop uploader ports the *behavior* of the Jinja `app.js` queue (bounded
  concurrency + 429-retry/backoff + placeholder rows), not the code, so it can't
  hammer the ingest limiter.

The sidebar References group is unchanged (quick read/cite). React Query shares
the `["references", id]` cache, so the sidebar list refreshes when the Library
mutates.

## 10. Frontend — Composer attachments + transcript rendering

- **Composer** (`cockpit/chat/Composer.tsx`): replace the disabled 📎 with an
  image affordance — drag/paste/click uploads via `useUploadFile` (→ `attachment_id`),
  shows selected images as removable chips, and includes them in the submit as
  `attachment_ids`. Reference + image inclusions share the 5-cap (disable adding
  past 5, with a hint). "Inline upload" is purely a Composer convenience over the
  same `attachment_ids` turn API — the turn endpoint stays JSON (no multipart turn).
- **TurnList** (`cockpit/chat/TurnList.tsx`): render `image` content blocks
  (currently skipped) as `<img src=/workspaces/{id}/files/{attachment_id}>` with
  alt text; unknown block types keep their current muted-fallback.

## 11. Cross-cutting

- **Permissions (parity):** references writes **member**-gated; files upload/delete
  **owner**-gated; all lists/serves member/viewer. Mirrors Jinja exactly. Note
  `require_member` = owner OR `WorkspaceViewer` (`_perms.py:35-40`), the *same*
  access set as Jinja's `_require_owner_or_viewer` (`files.py:155/364`) — "member"
  here does NOT exclude viewers.
- **Status freshness:** ingestion is **synchronous** — `ingest_bytes`/`reprocess_reference`
  run Phase 1→2(→3) inside the request (via `run_in_threadpool`, bounded by the
  limiter), so the mutation **response carries the terminal `text_status`**. The UI
  shows the mutation's pending state during the (possibly slow) upload, then
  renders the result; no polling infrastructure. Cross-member additions appear on
  the next refetch (React Query default / on tab focus). A `refetchInterval` while
  any row is non-terminal is available if ever needed but is NOT built (no
  background job exists to make it necessary).
- **Delete UX:** confirm dialog for delete (both references and files); reference
  delete surfaces `{outcome}` so a tombstoned (still-cited, still-resolvable) ref
  reads differently from a hard-delete.
- **Audit + Activity:** every `/api` write emits the same `WorkspaceEvent` rows as
  the Jinja routes, so the existing Activity feed stays complete across both UIs.
- **No DB schema change;** alembic head is untouched.

## 12. Reuse inventory (no reimplementation)

`ingest_bytes` + `IngestResult` · `reprocess_reference` + `ReprocessConflict` ·
`delete_or_tombstone_reference` (returns `tombstoned｜deleted`, writes its audit
row in-tx) · `detect_source_type`/fetch dispatch · `_parse_authors` + CSL/citekey
gen · `reference_ingest_limiter.slot()` · `ReferenceStorage` · attachments
`storage`/`tokens`/`_probe_image_format`/`_read_capped` + `submit_user_turn`
(already supports `attachment_ids`) · `require_member`/`require_owner` · the error
envelope + `OverCapacity`→429 pattern · existing read `/api` `list_references`,
`get_reference_chunks`, select-to-cite · existing Jinja binary serves `/raw` and
`/files/{aid}` · React `unwrap`/`ApiError`, `useReferences`, the BottomPanel +
two-axis nav, the cockpit shell.

## 13. Execution decomposition (two plans under this one spec)

The slice has two largely independent halves; recommend **two sequential
implementation plans**, each independently shippable:

- **Plan C-refs (References management):** §4 references `/api` + §8 `api/references.ts`
  hooks + §9 Library tab References section + OpenAPI regen. Lands the full
  reference write/management parity.
- **Plan C-media (Attachments):** §5 files `/api` + §6 turn `attachment_ids` + §8
  `api/files.ts` + §9 Library Files section + §10 Composer + TurnList rendering.
  Lands chat image attachments.

(One combined plan is acceptable if preferred, but two keeps each review-sized.)

## 14. Testing

**Backend** (reuse the `tests/test_api_*` harness + the existing reference/
attachment test fixtures): per-endpoint success; perms (member vs owner, non-member
403/404 non-leak); error mappings — `400` (bad MIME/size, missing title, bad kind),
`409` `ReprocessConflict`, `429` `OverCapacity`, and the **fetch path's `400`
(invalid/SSRF) + `502` `FetchError`**; `{outcome}` for delete-vs-tombstone; the
metadata IntegrityError-retry path; **audit-event assertions on the exact literal
`kind` strings** (`reference.added`, `reference.metadata_approved`,
`reference_metadata_waived`/`…_waiver_cleared`, `reference.kind_changed`,
`reference.reprocessed`, `reference.tombstoned`/`reference.deleted` — the strings
the Activity feed maps); the turn endpoint accepting `attachment_ids` and enforcing
the 5-cap (over-cap `ValueError`→400). Assert **service reuse** (the handler calls
`ingest_bytes`/`delete_or_tombstone_reference`/etc., not a reimplementation).

**Frontend** (vitest + Testing Library): hook tests (mock `api`); LibraryPanel
(rows, status badges, kind dropdown, edit-metadata modal, waiver, reprocess-on-failed,
delete-confirm + `{outcome}` feedback, files thumbnails + delete); the dropzone
queue/backpressure; Composer (upload→chip→submit `attachment_ids`, 5-cap disable);
TurnList image-block render. Type gate `tsc --noEmit`; build.

## 15. Risks

- **Service-reuse drift:** the `/api` handlers must reproduce the Jinja routes'
  full flow around the shared service functions (limiter slot, dedup, 50 MB cap,
  the metadata IntegrityError retry) — a thin wrapper that *skips* a step
  reintroduces overload/abuse paths or the citekey-collision 500. The orchestration
  is inlined (NOT factored — Jinja is deleted at Slice G, so a shared helper would
  be throwaway); the audit-`kind` literals are therefore duplicated across the
  Jinja and `/api` paths during coexistence and are pinned by tests on BOTH sides
  (§14) — which is what actually guards against drift until Slice G collapses them
  to one copy.
- **Two live write paths to one corpus** during coexistence: both Jinja and `/api`
  mutate the same tables; audit-event parity + React Query invalidation must both
  fire or the views diverge.
- **Citekey IntegrityError race:** the approve path's collision retry must be
  reused, or concurrent author-year approvals 500.
- **Tombstone semantics:** the UI must not present `tombstoned` as fully "gone" —
  cited tombstoned refs still resolve in existing citations.
- **Multipart from `openapi-fetch`:** the JSON client may not serialize `FormData`
  cleanly; uploads may need a raw `fetch` (same-origin cookie) — settle the
  pattern in Plan C-refs Task 1.
- **Slice size:** large; the two-plan split (and per-task review) is the mitigation.

## 16. Global constraints / gotchas (for the plans)

- **No DB schema change**; alembic head untouched. **Touch ZERO Jinja routes and
  extract NO new shared helper** — reuse the existing service functions directly
  and inline the thin per-route orchestration (mirroring the proven Jinja flow).
  The whole web app is being retired to React; these Jinja reference routes are
  deleted at Slice G, so the short-lived Jinja↔`/api` duplication is intentional
  and self-resolving.
- **New `/api` modules** (`web/api/references.py`, `web/api/files.py`) mounted via
  `web/api/__init__.py`; do NOT append to the already-large `workspaces.py`. The
  **read** `list_references` + `ReferenceListItemOut` stay in `workspaces.py` and
  are extended **additively** (§4 Library read-model) — the new modules are writes.
- **No global `OverCapacity` handler exists** — each ingest/fetch/reprocess
  handler catches `OverCapacity` locally → `429` + `Retry-After` (mirrors the
  Jinja per-route catches). The error envelope only auto-handles `HTTPException`.
- **Exact audit `kind` strings** must match the reused routes verbatim (§4 / §14)
  — the Activity feed (`feed.py:73`) maps the literals; a drift breaks the feed.
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`**; additive diff only.
- **No `routes.gen.ts` change** (no new SPA routes — the Library tab is in-cockpit).
- **Backend tests:** targeted files only (whole-suite drops the testcontainers
  Postgres); `ruff check/format` touched files (F401 fails).
- **Frontend:** Node 22 + pnpm; `tsc --noEmit` is the gate; `pnpm vitest run <file>`.
- **Binary serves are reused** (`/raw`, `/files/{aid}`) — do not build new ones.
- **Permissions mirror Jinja exactly** (references=member, files writes=owner).
