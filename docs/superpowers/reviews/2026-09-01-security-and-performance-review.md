# Security/code review + performance review (2026-09-01)

> **Verification note (2026-09-01):** The findings as originally reported are
> preserved below, but their severity labels and recommendation order are superseded.
> Direct source verification found material overstatements in the performance section,
> including timer callbacks described as React renders, already-paginated dashboard
> results described as needing virtualization, and an empty-queue precheck that would
> duplicate the queue claim query. Use the verified verdicts and implementation order
> appended to this document before scheduling work.

Two read-only passes over the current tree, done in the same session: a full-codebase
security/correctness review (auth, DB, proposal_assist fencing, ranking/scheduler, web API,
frontend), followed by a performance-focused pass (backend queries, scheduler/polling
overhead, frontend rendering). Findings below are as reported; a few were spot-checked
against the source directly (noted inline) and severities adjusted where the initial
read was too harsh.

No High-severity, currently-exploitable issue was found in either pass.

---

## Part 1 — Security & code review

### Auth, sessions, CSRF/CSWSH, headers — exemplary, one low note

Argon2 password hashing with rehash-on-login, PKCE-enforced OIDC with algorithm pinning,
homoglyph-proof domain allowlisting, `SameSite=Lax` + Origin/Referer CSRF layering with
correct loopback/scheme/port normalization, CSWSH origin checks before `accept()`,
anti-enumeration on login/register, strict `SESSION_SECRET`/`APP_BASE_URL` startup
validation, full security-header set (CSP, HSTS, X-Frame-Options, Permissions-Policy).

- **Low** — [src/call_manager/web/middleware.py](../../../src/call_manager/web/middleware.py#L71):
  comment claims `Referrer-Policy` suppresses the `Origin` header — it only affects
  `Referer`. The header choice itself is correct; only the comment misleads.

### Database & migrations

Models, indexes, cascades, and CHECK constraints all matched `docs/ARCHITECTURE.md`; no
raw-SQL injection paths found anywhere in the DB layer.

- Three migrations ([0065](../../../alembic/versions/0065_artifact_versions_unique_sha.py),
  [0066](../../../alembic/versions/0066_workspace_current_branch.py),
  [0068](../../../alembic/versions/0068_branch_entity.py)) were initially flagged High by
  the automated pass for "unsafe data migration"/"backfill coupled to app code". Read
  directly and downgraded: these are deliberate, heavily-commented dedup/backfill
  migrations that intentionally keep backfill logic in tested application modules rather
  than inline. Not defects, but worth a couple of low-severity polish items:
  - **Low** — 0065's `downgrade()` only drops the index, doesn't restore the deduplicated
    rows (correct — they were true duplicates — but the non-restorative nature of
    downgrade isn't documented in the migration itself).
  - **Low** — 0065's `DELETE` has no batching; could briefly lock `artifact_versions` on a
    large drifted table.
- **Low** — [src/call_manager/db/models.py](../../../src/call_manager/db/models.py#L1855-L1866):
  `ReferenceChunk` double-cascades to a workspace (via `reference_id` AND a direct FK) —
  redundant, not incorrect.
- **Low** — [src/call_manager/workspaces.py](../../../src/call_manager/workspaces.py#L148-L155):
  per-job loop in `soft_delete_workspace_db` is a mild N+1, low impact at current
  job-count scale.

### Concurrency & fencing (proposal_assist)

The three-primitive epoch-fence design (`live_at_generation`,
`acquire_commit_mutex_at_generation`, `acquire_shared_lease`) holds up — TOCTOU windows
around collab retirement, write-permission re-checks under lock, and workspace generation
re-validation are all closed and pinned by tests.

- **Medium** — [src/call_manager/proposal_assist/references/fetcher.py](../../../src/call_manager/proposal_assist/references/fetcher.py#L308):
  the redirect `Location` header isn't validated as a well-formed URL before `urljoin()`;
  a malicious server could burn through the 5-hop redirect budget with malformed values.
  Socket pinning still holds (no SSRF), this is a robustness gap only.
- **Low** — [src/call_manager/proposal_assist/references/ingest.py](../../../src/call_manager/proposal_assist/references/ingest.py#L160-L175):
  citekey generation retries 4 times on collision; under pathological concurrent load
  could leave a reference without a citekey (data-quality only, not data-loss).

### Ranking pipeline & scheduler

Prompt injection is defended with explicit `<<<CALL_BODY>>>` fencing and system-prompt
warnings; single-instance scheduler enforced via a Postgres advisory lock; job claiming
uses `SELECT ... FOR UPDATE SKIP LOCKED`; the spend ledger commits are isolated from
job-completion commits so spend is never lost even on a later failure.

- **Medium** (documented, accepted trade-off) — concurrent daily-cap checks
  (coherence/evaluator/review/uncited-claims drains, and per-job ranking escalation) can
  each overshoot the cap by one job before the ledger updates; the code explicitly
  documents this as deliberate rather than adding a global lock.
- **Low** — flat 60s retry (no backoff) for failed ranking jobs; a fresh `httpx.Client`
  per Ollama call instead of a persistent connection — both marked as deferred/acceptable
  at current scale.

### Web API

Consistent IDOR protection (`require_member`/`require_owner` on every workspace route,
404-not-403 to avoid existence oracles), sanitized 5xx error envelopes, well-implemented
upload guards (size caps, decompression-bomb limits, content-type/format cross-check,
allowlisted filenames).

- **Medium** — [src/call_manager/web/api/admin_users.py](../../../src/call_manager/web/api/admin_users.py#L228-L241):
  admin password-reset route has no per-route rate limit beyond the admin-auth check.
- **Medium** — [src/call_manager/web/api/files.py](../../../src/call_manager/web/api/files.py#L207-L214),
  [src/call_manager/web/api/export.py](../../../src/call_manager/web/api/export.py#L39-L41):
  `Content-Disposition` filenames aren't RFC 5987-encoded, so non-ASCII/special-char
  filenames can misrender in some clients.
- **Low** — [src/call_manager/web/api/export.py](../../../src/call_manager/web/api/export.py#L63-L65):
  a raw `ValueError` message leaks into a 400 response instead of the standard error
  envelope (low-sensitivity, member-only route).

### Frontend (React SPA)

No XSS surface found (zero `dangerouslySetInnerHTML`/`innerHTML`, Tiptap content is
schema-isolated), no secrets in `localStorage`, `credentials: "same-origin"` used
correctly, TypeScript strict mode on.

- **Medium** — [frontend/src/api/client.ts](../../../frontend/src/api/client.ts#L48): on
  401, `window.location.assign(e.error.detail?.redirect ?? "/auth/no-access")` navigates
  to a **server-provided** path via `window.location`, bypassing React Router's
  relative-path-only guard used elsewhere (`Login.tsx`'s `navigate(data.redirect)`).
  Currently safe because the backend's `safe_next()` already restricts these to relative
  paths, but it's a second, weaker enforcement point that would silently regress if that
  backend guard were ever loosened.
- **Low** — [frontend/src/routes/Login.tsx](../../../frontend/src/routes/Login.tsx#L74):
  `next` query param isn't independently validated client-side before being submitted
  (server-side `safe_next()` is the actual guard — fine, just single-layered).

### Recommendations (security/code), priority order

1. Add RFC 5987 encoding to `Content-Disposition` (files.py, export.py).
2. Rate-limit the admin password-reset route as defense-in-depth.
3. Validate/repair the redirect `Location` header in `fetcher.py` before `urljoin()`.
4. Fix the misleading `Referrer-Policy` comment in `middleware.py`.
5. Optional: constrain `client.ts`'s 401-redirect to a relative-path check client-side
   too, so it doesn't solely depend on the backend guard.

---

## Part 2 — Performance review (excludes LLM/model latency)

### Backend DB/API query performance

No N+1 patterns found in the ORM layer; `dashboard.py` explicitly `defer()`s the
embedding/full-text columns (measured 71-78% of row size) and uses `joinedload`
correctly everywhere it's needed.

- **High** — [src/call_manager/web/api/turns.py](../../../src/call_manager/web/api/turns.py#L228-L231):
  `list_turns` loads **all** `ChatTurn` rows for a workspace with no pagination/limit.
  Frequently-hit endpoint; the table grows unboundedly with chat history.
- **Medium** — [src/call_manager/proposal_assist/web/review.py](../../../src/call_manager/proposal_assist/web/review.py#L188):
  `ReviewRunItem` list is loaded unbounded per review run.
- **Medium/Low** — [src/call_manager/web/api/files.py](../../../src/call_manager/web/api/files.py#L66):
  `Attachment` list loaded unbounded per workspace; low real-world impact given the
  5 MB/file cap, but same pattern.
- **Low** — [src/call_manager/web/dashboard.py](../../../src/call_manager/web/dashboard.py#L363-L379):
  four separate COUNT queries for summary metrics that could be combined; negligible
  measured cost, flagged only for consistency.

### Scheduler / polling overhead

- **High** — manual-only drain ticks (coherence/evaluator/review/uncited-claims,
  [src/call_manager/scheduler.py](../../../src/call_manager/scheduler.py#L816-L993)) run
  a full claim-attempt every 20s regardless of queue depth — ~216k no-op attempts/day per
  tick type. Cheap per-tick, but should short-circuit on an empty queue the way
  `drain_analysis_queue()` already does.
- **High** — `turn_drain_tick` polls every 5s
  ([scheduler.py](../../../src/call_manager/scheduler.py#L780)) unconditionally; the
  comment states this is meant as a floor now that an SSE bridge pushes turn completion —
  worth lengthening if that push path is confirmed live end-to-end.
- **High** (frontend) — [frontend/src/collab/useSettled.ts](../../../frontend/src/collab/useSettled.ts#L18):
  `setInterval(() => setSettled(provider.isSettled()), 200)` — 5 re-renders/sec per
  mounted editor, continuously, for the whole time an editor is open.
- **Medium** — [frontend/src/api/problems.ts](../../../frontend/src/api/problems.ts#L35):
  `refetchInterval: 15000` polls unconditionally, including when the panel isn't visible.
- **Medium** — reconcile ticks (jobs/passes every 60s, analysis every 900s,
  [scheduler.py](../../../src/call_manager/scheduler.py#L375)) do near-full-table scans
  on an idle instance; individually cheap, adds up.
- **Medium** — [frontend/src/api/dashboard.ts](../../../frontend/src/api/dashboard.ts#L49):
  polls every 5s while any ranking is pending, with no backoff/cap — a large backfill
  (1000+ pending) means hours of constant 5s polling.
- **Medium** — coherence/evaluator polls at 1.5s and review at 4s while a pass is running
  ([frontend/src/api/coherence.ts](../../../frontend/src/api/coherence.ts#L24) et al.) —
  correctly gated on job status, but no backoff over a run that can last minutes.
- **Medium** — [frontend/src/api/admin.ts](../../../frontend/src/api/admin.ts#L61):
  `staleTime: 0, gcTime: 0` on the golden-set list forces a refetch on every tab
  click/window focus (deliberate, to avoid stale audit data — low-traffic surface).
- Already correct: annotations/rubric-setup polling stop on terminal status; the global
  20s `staleTime` + `refetchOnWindowFocus: false` avoids thundering-herd refetches; turn
  completion is pushed via SSE, not polled, in `CockpitChat.tsx`.

### Frontend rendering & bundle

- **High** — [frontend/src/collab/useEditorTick.ts](../../../frontend/src/collab/useEditorTick.ts#L1-L11)
  re-renders `EditorToolbar` on every keystroke, which recalculates the block-option loop
  and calls `editor.can().setNode()` per option every time
  ([EditorToolbar.tsx](../../../frontend/src/collab/EditorToolbar.tsx#L52-L70)) — likely
  the largest source of perceptible input lag in long documents.
- **High** — [frontend/src/routes/Dashboard.tsx](../../../frontend/src/routes/Dashboard.tsx#L25-L29):
  calls list has no virtualization; every row mounts unconditionally.
- **Medium** — list row components aren't memoized: `CallRow`, `TurnItem`
  ([cockpit/chat/TurnList.tsx](../../../frontend/src/cockpit/chat/TurnList.tsx#L44)),
  `VersionGraphRow`
  ([cockpit/VersionGraph.tsx](../../../frontend/src/cockpit/VersionGraph.tsx#L169-L189)).
- **Medium** — [frontend/src/collab/TiptapSelectionToolbar.tsx](../../../frontend/src/collab/TiptapSelectionToolbar.tsx#L90-L115):
  anchor/offset math recomputed unmemoized on every render.
- **Medium** — [frontend/src/cockpit/ProblemsPanel.tsx](../../../frontend/src/cockpit/ProblemsPanel.tsx#L47-L50):
  category filtering done inline in the render body, unmemoized.
- Already correct: route-level code-splitting keeps Tiptap/Yjs out of the dashboard-only
  bundle (`router.tsx`); lane assignment in `VersionGraph.tsx` and merged turns in
  `CockpitChat.tsx` are properly memoized.

### Recommendations (performance), priority order

1. `useSettled()` — replace the 200ms timer with an event-driven signal off the collab
   socket's existing sync/awareness events.
2. Paginate `GET /api/workspaces/{id}/turns` (and apply the same pattern to
   `review.py`/`files.py`).
3. Memoize `EditorToolbar`'s block-option calculation and `TiptapSelectionToolbar`'s
   anchor math, keyed on claim/section-sha.
4. Short-circuit the 20s manual-only drain ticks on an empty queue.
5. Add virtualization to the Dashboard calls list.
6. Add a backoff/cap to the dashboard and pass-status polling intervals.

---

## Part 3 — Independent verification and corrected priority

This section supersedes both recommendation lists above. Verification followed each
negative finding into its owning function, relevant consumers, and nearby tests. It did
not repeat the original review's repository-wide negative searches (for example, “no raw
SQL injection path”) and is not a production profile. Performance claims without timing,
cardinality, or query-plan evidence are therefore classified as measurement candidates,
not defects.

### Security and correctness verdicts

| Finding | Verdict | Action |
|---|---|---|
| `Content-Disposition` in `files.py` and `export.py` | **Confirmed functional defect.** Both filename sanitizers retain Unicode alphanumerics, but the routes put them in a raw Latin-1 response header. Executed probe: `safe_title("研究")` followed by response construction raises `UnicodeEncodeError`. | **Implement now.** Emit an ASCII fallback plus RFC 5987 `filename*=UTF-8''...`; add non-Latin tests for file serve and both exports. |
| Composed export exposes `str(ValueError)` | **Partly confirmed.** It is a 409 in the standard API error envelope, not the reported raw 400. The member-visible message can still disclose parser detail. | Replace the public suffix with a stable message while retaining the exception in logs. Low-risk companion to the filename fix. |
| Redirect `Location` handling in `fetcher.py` | **Partly confirmed.** Every joined target is validated and DNS-pinned on the next loop before a request, so SSRF remains closed. A malformed value can make `urljoin()` raise `ValueError` outside the `FetchError` contract. A malicious server can consume the redirect allowance with valid redirects anyway. | Catch URL-join parse failures and raise `FetchError`. Robustness only; do not describe as an SSRF fix. |
| `Referrer-Policy` comment | **Confirmed comment defect.** Referrer Policy governs `Referer`, not whether a form POST has a normal `Origin`. The `same-origin` policy itself is suitable. | Correct the comment when touching middleware; no runtime change. |
| Migration 0065 deduplication | **Accepted one-time migration tradeoff.** The delete is set-based and unbatched; downgrade cannot recreate intentionally removed duplicate rows. The migration logic is otherwise deliberate. | Document the non-restorative downgrade. Change execution strategy only if this migration is still pending on a measured large table. |
| Migrations 0066/0068 | **No defect confirmed.** Their tested application-code backfill and cyclic-FK construction are deliberate. | None. |
| `ReferenceChunk` cascade paths | **Confirmed redundancy, no incorrect behavior.** The direct workspace FK supports the denormalized search key and enforces its lifetime independently of the parent reference. | None. |
| Soft-delete queued-turn loop | **Confirmed low-scale query loop.** The four pass cancellations are set-based; only queued turn outcomes are processed individually to preserve per-turn audit semantics. | Keep unless measured queued-turn cardinality makes delete latency material. |
| Citekey collision retries | **Confirmed bounded retry, recommendation refuted.** The fourth collision is re-raised, logged with traceback by the outer handler, and marks metadata failed while preserving the ingested reference. | None; the suggested warning already exists. |
| Daily-cap overshoot | **Accepted guardrail semantics.** Preflight caps cannot reserve the unknown final token cost, and independently running pass families can cross the global threshold concurrently. | None unless the product requires a strict accounting ceiling; that would need reservation/serialization, not a local check. |
| Flat ranking retries and per-call Ollama client | **Confirmed documented tradeoffs.** Defaults are a 60-second queue tick and a local endpoint; no measured bottleneck was supplied. | Defer pending failure-rate or connection-cost evidence. |
| Admin password-reset rate limit | **Observation confirmed; security priority refuted.** The route requires an admin, hashes under the normal password policy, increments the target session epoch, and revokes live sockets. Throttling does not strengthen the authority boundary against a compromised admin. | Do not prioritize. Audit/step-up authentication would address a real privileged-action threat better than a route-local counter. |
| SPA account-inactive redirect | **Overstated.** This is a 403 `account_inactive` path, not 401. Its backend target is one of two hard-coded local paths, rather than arbitrary output from `safe_next()`. | Optional shared local-path validator only; no current open redirect. |
| Login `next` parameter | **Defense-in-depth observation only.** Both password and OIDC server paths apply `safe_next()` before redirecting. | None required. |

### Performance verdicts

| Finding | Verdict | Action |
|---|---|---|
| Unbounded turn-history response | **Confirmed scaling risk.** `list_turns()` returns every row in ascending position order; the SPA consumes the complete array. `(workspace_id, position)` is indexed, so cursor pagination fits the model. | **Implement next.** Return the newest bounded page plus `before_position`/`has_more`, preserve ascending display order, and add “load older” prepend behavior. Do not silently add a limit to the existing contract. |
| Unbounded `ReviewRunItem` list | **Refuted as a scaling issue.** The CHECK-pinned `REVIEW_KINDS` vocabulary contains exactly six items per run. | Do not paginate. |
| Unbounded attachment list | **Confirmed but unmeasured.** The response contains metadata only, but neither row count nor total workspace storage is capped. A 5 MB per-file limit does not bound file count. | Defer. If real workspaces accumulate many files, add count/byte quotas before list pagination. |
| Four dashboard aggregate queries | **Confirmed query shape, unmeasured impact.** The same builder serves the paginated SPA API, but the original review supplied no timing showing the round trips matter. | Keep until query telemetry identifies it as material. |
| Four manual pass drains every 20 seconds | **Cadence confirmed; proposed fix refuted.** At defaults these issue 17,280 claim queries/day in total, not ~216,000 attempts/day per type. Each drain already returns immediately when the indexed claim finds no row. A separate empty check would duplicate the database work. | Do not add an empty-queue query. Consider event-driven wakeups only after measuring scheduler statement cost. |
| Turn drain every 5 seconds | **Confirmed, with a false rationale in the original review.** Repository-wide call-site search found only the interval trigger. SSE streams completion to the browser; they do not wake the worker. The default produces 17,280 queue queries/day and bounds median enqueue delay, but cost was not measured. | Instrument query latency and enqueue-to-claim delay before changing it. An event-driven wakeup plus a slower recovery poll is the coherent optimization. |
| `useSettled()` every 200 ms | **Overstated.** It calls `provider.isSettled()` and a state setter five times per second; React normally bails out for the unchanged boolean, so it does not commit five renders per second. Existing provider events cannot represent “buffer drained and 350 ms elapsed,” so replacing the timer with sync/awareness events would be incomplete. | Keep. Revisit only with browser-profile evidence; an event-driven replacement would need a dedicated provider settled event/timer. |
| Problems polling every 15 seconds | **Confirmed intentional tradeoff.** `BottomPanel` keeps the query mounted to maintain the visible Problems badge even when that tab is closed. TanStack deduplicates the panel's second observer, and background-window polling is not enabled. | Keep unless endpoint telemetry shows meaningful load. |
| Reconcile scans | **Partly confirmed.** Analysis reconciliation walks every live workspace and projects its draft every 15 minutes. Pass reconciliation is an in-memory list with an empty fast path; job retirement uses two set-based active-row updates. | Instrument the analysis sweep as workspace count grows; do not group or optimize all three together. |
| Dashboard/pass-status fixed polling | **Confirmed, intentionally status-gated.** Dashboard polling stops at zero pending; pass polling stops at terminal status. No request-latency or load evidence was supplied. | Defer backoff until telemetry shows load, because it directly reduces completion freshness. |
| Admin `staleTime: 0, gcTime: 0` | **Refuted citation and recommendation.** The cited hook is deleted-workspace detail, not the golden-set list. Every read intentionally writes a privacy audit event, so caching would violate the contract. | Keep. |
| EditorToolbar transaction rerenders | **Confirmed mechanism, unverified impact.** It rerenders on every editor transaction and recomputes formatting state. The proposed claim/section-SHA memo key is unavailable to the component and would be incorrect: command availability and active marks depend on the live selection. | Profile typing in representative long documents. If material, derive a small formatting-state snapshot from editor transactions and rerender only when that snapshot changes. |
| Dashboard virtualization | **Refuted as High.** The backend defaults to 50 rows and caps a page at 200; the SPA mounts only the current page. | Do not add a virtualization dependency without a measured page-render problem. |
| Unmemoized row components | **Unverified optimization idea.** React Query structural sharing may preserve unchanged rows; custom comparators can also stale score/status fields. No render profile was supplied. | Measure before memoizing. |
| Selection-toolbar offset math and Problems grouping | **Mechanisms confirmed, impact refuted at current shape.** Offset work occurs only while a selection exists; grouping handles a small fixed category set. Neither has timing evidence. | Do not prioritize. |

### Implementation order

1. **Unicode-safe `Content-Disposition`** for attachment serving and both Markdown
  exports, with non-Latin regression tests. This is the only finding in this review
  reproduced as a current user-facing failure.
2. **Cursor-paginate turn history end to end.** Treat it as an API/UX contract change:
  newest bounded page, ascending display, and explicit loading of older turns.
3. **Small robustness cleanup:** normalize malformed redirect locations to `FetchError`,
  stop returning raw citation-ledger parser details, and correct the Referrer-Policy
  comment. These can share one narrow hardening change.
4. **Add measurements before performance changes:** turn-response row/byte cardinality,
  scheduler query duration and enqueue-to-claim delay, analysis-reconcile duration by
  workspace count, and editor commit/keystroke profiles on representative documents.
5. **Do not implement from the original list:** an extra empty-queue check, review-item
  pagination, dashboard virtualization, claim/SHA toolbar memoization, an awareness-based
  `useSettled()` replacement, deleted-workspace caching, or blanket row/filter memoization.
