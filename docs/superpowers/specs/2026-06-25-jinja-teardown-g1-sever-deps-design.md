# Jinja Workspace Teardown — G1: Sever React's Jinja Dependencies (Design Spec)

**Date:** 2026-06-25
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the legacy Jinja `/workspaces/{id}` workspace UI
now that the React cockpit (Slices A–F) is feature-complete.
**This is G1 of a two-slice teardown.** G1 = **sever** (additive + relocation, NOTHING
deleted). G2 (separate, later) = **delete** the orphaned Jinja UI.
**Builds on:** cockpit cutover complete at master `d335897`.

---

## 1. Goal & invariant

The legacy Jinja workspace UI cannot be deleted yet because the React app and the kept
`/api` backend still depend on pieces that live *inside* the doomed Jinja modules. G1
removes every such dependency so that G2 becomes a pure orphan-removal.

**Invariant after G1 (verifiable):**
1. The React cockpit calls **only** `/api/*` — no `frontend/src` reference to a Jinja
   `/workspaces/…` route.
2. The kept backend (`web/api/*` + the 7 live JSON modules) imports **nothing** from the
   doomed Jinja modules (`proposal_assist/web/{workspaces,files,references,sections,citations,annotations}.py`).
3. **Nothing is deleted.** The Jinja UI still runs unchanged; G1 only removes *reliance* on it.

No DB schema change.

---

## 2. The keep / delete partition (audit result — context for the whole teardown)

`proposal_assist/web/` mixes the legacy Jinja UI with live JSON APIs the React cockpit
consumes. The teardown must NOT touch the kept set.

**KEEP (live — the React backend):**
- All of `src/call_manager/web/api/*.py` (10 modules: calls, me, workspaces, turns,
  references, files, settings, rubric, citation_audit, export).
- The **7 JSON modules** under `proposal_assist/web/` that serve `/api/workspaces/…`:
  `proposals.py` (Inbox), `activity.py` (Activity tab), `problems.py`, `coherence.py`,
  `evaluator.py` (+ scorecard), `review.py` (Panel review), `uncited_claims.py`.
- Shared helpers already in neutral spots: `proposal_assist/web/_perms.py`
  (`require_member`/`require_owner`), `proposal_assist/web/_cloud_gate.py`.

**DELETE in G2 (legacy Jinja UI — NOT in G1):** the Jinja routes/templates of
`workspaces.py` (the two-pane UI, `:399–1347`), `sections.py`, `citations.py`,
`annotations.py`, and the Jinja routes of `files.py` (`:148–417`) + `references.py`
(`:141–752`); their templates under `web/templates/proposal_assist/`; their 6
`include_router` registrations; the `base.html` `/workspaces` nav link.

---

## 3. Current cross-dependencies G1 must sever (audit, verified)

### 3.1 React → Jinja binary serves (3 call sites, 2 files)
The cockpit fetches binary content directly from Jinja routes that have **no `/api`
equivalent**:
- `frontend/src/cockpit/LibraryPanel.tsx:174` — `href={`/workspaces/${id}/references/${r.id}/raw`}`
  (reference original-file download).
- `frontend/src/cockpit/LibraryPanel.tsx:225` — `src={`/workspaces/${id}/files/${f.id}`}`
  (Files-tab attachment image).
- `frontend/src/cockpit/chat/TurnList.tsx:33` — `src={`/workspaces/${id}/files/${b.attachment_id}`}`
  (chat image attachment).
The serving Jinja routes: `references.py:819` `serve_reference_raw` (member-gated,
`FileResponse` of the original bytes) and `files.py:356` `serve_file` (member-gated,
`FileResponse` of attachment bytes).

### 3.2 Kept backend → helpers defined inside doomed Jinja modules
- **`_session_dep`** — defined in `workspaces.py:394` (duplicated in `references.py:93`,
  `files.py:111`). The **7 kept JSON modules import it from `workspaces.py`**:
  `problems.py:19`, `activity.py:25`, `review.py:32`, `proposals.py:26`,
  `coherence.py:20`, `evaluator.py:24`, `uncited_claims.py:28`. (Doomed importers —
  `sections.py:56`, `annotations.py:59` from workspaces; `citations.py:37` from references
  — are deleted in G2, no action.)
- **Workspace settings constants + parser** — `web/api/settings.py:21` imports
  `_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_MAX_WORKSPACE_TITLE_LEN`,
  `_parse_deadline_form` from `workspaces.py`.
- **File-upload helpers** — `web/api/files.py:24` imports `_ALLOWED_MIMES`, `_MAX_BYTES`,
  `_PIL_FORMAT_TO_MIME`, `_SUFFIX_BY_MIME`, `_probe_image_format`, `_read_capped`,
  `_sanitize_filename`, `_storage_for` from `files.py`.
- **Reference-upload helpers** — `web/api/references.py:35` imports `_ALLOWED_MIMES`,
  `_MAX_BYTES`, `_read_capped`, `_sanitize_filename`, `_storage_for`, and `:274` imports
  `_parse_authors` from `references.py`. (The files vs references `_ALLOWED_MIMES`/`_MAX_BYTES`
  sets DIFFER — images vs PDFs/docx — so they relocate to separate homes.)

---

## 4. Design

### 4.1 Add the two binary serves to `/api`, repoint React
- `web/api/references.py`: add `GET /workspaces/{workspace_id}/references/{reference_id}/raw`
  — `require_member`, transcribe the existing `serve_reference_raw` logic verbatim (storage
  lookup via `_storage_for` + `suffix_for_mime`, `FileResponse` with the same
  `Content-Disposition: attachment; filename=…` + `X-Content-Type-Options: nosniff` +
  `Cache-Control: private, max-age=300`). 404 if the reference or its bytes are missing.
- `web/api/files.py`: add `GET /workspaces/{workspace_id}/files/{attachment_id}` — same
  pattern from the existing `serve_file` (member-gated, `FileResponse` of attachment bytes,
  same cache/nosniff headers). 404 on missing.
- These reuse the relocated upload helpers (§4.2). Binary 200 responses are **exempt from
  the JSON envelope** (consistent with Slice F export and the existing serves).
- **Repoint React** (3 literals → prefix `/api`): `LibraryPanel.tsx:174` →
  `/api/workspaces/${id}/references/${r.id}/raw`; `LibraryPanel.tsx:225` →
  `/api/workspaces/${id}/files/${f.id}`; `TurnList.tsx:33` →
  `/api/workspaces/${id}/files/${b.attachment_id}`. Update the two test expectations
  (`LibraryPanel.test.tsx:141`, `TurnList.test.tsx:61`) to the `/api` URLs.
- The Jinja `serve_reference_raw`/`serve_file` routes STAY (deleted in G2); React simply
  stops calling them.

### 4.2 Relocate the shared helpers into neutral, Jinja-free modules
**Approach (lowest-risk, strictly additive):** for each shared symbol, place the definition
in a neutral module that imports no templates, and **repoint the kept importers** to it. To
keep G1 strictly additive and the Jinja UI byte-for-byte unchanged, the doomed modules'
own copies are **left in place** — a transient duplication that G2 removes when it deletes
those modules. (The plan reads each live file to transcribe definitions exactly.)

| Symbol(s) | From | Kept importer to repoint | Recommended neutral home |
|---|---|---|---|
| `_session_dep` | `workspaces.py:394` | the 7 JSON modules (`problems`/`activity`/`review`/`proposals`/`coherence`/`evaluator`/`uncited_claims`) | new `proposal_assist/web/_deps.py` |
| `_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_MAX_WORKSPACE_TITLE_LEN`, `_parse_deadline_form` | `workspaces.py` | `web/api/settings.py` | new `proposal_assist/web/workspace_meta.py` |
| `_ALLOWED_MIMES`, `_MAX_BYTES`, `_PIL_FORMAT_TO_MIME`, `_SUFFIX_BY_MIME`, `_probe_image_format`, `_read_capped`, `_sanitize_filename`, `_storage_for` | `files.py` | `web/api/files.py` | new `proposal_assist/web/_file_uploads.py` |
| `_ALLOWED_MIMES`, `_MAX_BYTES`, `_read_capped`, `_sanitize_filename`, `_storage_for`, `_parse_authors` | `references.py` | `web/api/references.py` | new `proposal_assist/references/uploads.py` |

Module-naming is a recommendation; the plan may consolidate (e.g. one `_uploads.py`) if the
files/references helper sets are genuinely identical — it must verify they are not before
merging them. `_perms.py` and `_cloud_gate.py` are already neutral and are NOT moved.

**Per-symbol grounding the plan must do:** transcribe each definition from the live file,
and check whether a symbol is itself a re-export of an already-neutral canonical source —
e.g. `_CITATION_STYLES` likely derives from `citations/bibliography.py:CITATION_STYLES`, in
which case `web/api/settings.py` should import that canonical name directly rather than
relocate a copy. Prefer importing an existing neutral source over creating a new copy.

### 4.3 Severance proof (the verify gate)
- **Backend guard test** `tests/test_jinja_severance.py`: reads the source of each kept
  module (`web/api/*.py` + the 7 JSON modules) and asserts none contains an import
  `from call_manager.proposal_assist.web.{workspaces,files,references,sections,citations,annotations} import …`.
  This is a durable regression guard against re-coupling.
- **Frontend guard** (plan verification step): `grep -rn '"/workspaces/\|`/workspaces/' frontend/src --include=*.ts --include=*.tsx | grep -v schema.gen` returns **nothing**
  (only React `/cockpit` routes + `/api/…` remain). `schema.gen.ts` still lists the Jinja
  paths — that's fine; it's generated and they're deleted in G2.

---

## 5. Testing

**Backend (targeted files only):**
- `test_api_references.py`: add raw-serve cases — member 200 returns the original bytes +
  `Content-Disposition` filename; non-member 403; missing-reference/missing-bytes 404.
- `test_api_files.py`: add attachment-byte-serve cases — member 200 returns the bytes +
  cache/nosniff headers; non-member 403; missing-attachment 404.
- After the relocations, the existing suites for the 7 JSON modules + `web/api/settings.py`
  / `files.py` / `references.py` must stay green (run the affected files).
- `test_jinja_severance.py` (the guard above).

**Frontend (`tsc` is the gate):**
- `LibraryPanel.test.tsx` + `TurnList.test.tsx`: updated to assert the `/api/...` URLs.
- `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build` clean.

**OpenAPI regen** (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`) — adds the 2
new `/api` binary-serve paths to `schema.gen.ts`; `routes.gen.ts` empty diff. (The React
repoint uses raw `href`/`src` URL strings, not `openapi-fetch`, so no type dependency — the
regen is hygiene.)

---

## 6. Out of scope — deferred to G2

- ALL deletion: the Jinja routes/templates/modules, the 6 `include_router` removals.
- Removing the transient helper duplication left in the doomed modules (§4.2).
- The Jinja binary serves `serve_reference_raw`/`serve_file` (kept until React is verified
  on `/api`, then deleted in G2).
- `base.html`'s `/workspaces` nav link repoint (`base.html` is shared with the still-Jinja
  profile/admin/auth pages — it stays until those sub-projects; its workspace link is
  repointed/removed in G2).
- Any change to the 7 kept JSON modules beyond the single `_session_dep` import line.

---

## 7. Risks / notes

- **The `_session_dep` relocation touches the cockpit's whole JSON backend** (Inbox,
  Activity, Problems, Scorecard, Panel review). It's a one-line import change per module,
  but it's the highest-risk edit — covered by re-running each module's test suite + the app
  importing cleanly. This is exactly why G1 ships and is verified before G2 deletes anything.
- **Transient duplication** (§4.2) is deliberate and bounded to the G1→G2 window — the
  alternative (editing the doomed modules to re-import) would touch code we want left
  byte-unchanged in G1. G2 deletes the duplicates with their modules.
- **The files vs references upload-helper sets must be confirmed distinct** before any
  consolidation — they govern different MIME allowlists (images vs documents).
- **Reachability already confirmed:** no email digest, redirect, or live React flow targets
  a Jinja `/workspaces/{id}` page; only `base.html:40`'s nav link does, and that's a G2 item.
