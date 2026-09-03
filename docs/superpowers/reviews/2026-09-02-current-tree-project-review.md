# Call-Monitor Current-Tree Project Review (2026-09-02, evening)

**Scope:** `master` @ `7bdc2bb3`, clean tree. Whole project: backend, collab
editor, ranking/ingestion, auth, frontend, ops/CI.
**Method:** Independent re-read of the generated architecture map and current
source after the `fix/review-2026-09-02` merge. Cheap Python probes for the
serializer, ranking fence, and Content-Disposition. Did **not** re-run the
full pytest suite or the eleven-worktree mutation sweep.
**Code changes:** none. This document, its index entry, and nothing else.
**Relation to prior work:** does not replace
[2026-09-02-thorough-project-review.md](2026-09-02-thorough-project-review.md)
(`8f44a35e`, eleven reviewers, 16/16 gate mutations). That document is the
review of record for the pre-fix tree. This one answers: *what is still true
on HEAD after the morning's fixes landed?*

Ten commits sit between `8f44a35e` and `7bdc2bb3`, including
`76b38156` (`published_at`), `80b91d62` / `e6857051` (serializer + checkpoint
refuse), `70c3f979` (pip-audit), removal of tracked `.orig` files, and the
approved-but-unimplemented `doc_epoch` spec/plan.

**Grade: B.** Security foundations are still strong. The worst remaining
ordinary-path bugs are availability (reference ingest) and collab identity
(`doc_epoch` is still only a spec). The safety net is still not attached.

---

## 1. What is solid (do not re-spend)

- **Auth:** Origin CSRF, epoch-revoked sessions, OIDC (`joserfc`) with
  `email_verified` + domain allowlist, argon2, anti-enumeration register
  (always 200).
- **Collab transport:** 1 MB frame cap, CSWSH Origin check, 4409 on
  `WorkspaceGenerationStale`, 1013 on mutex contention, reconnect backoff
  with a permanent-offline cap.
- **Fence spine:** reachable `git commit` sites take
  `acquire_commit_mutex_at_generation`. Restore/delete still need the
  generation fence because restore reuses the UUID and on-disk path.
- **Docs gated in prod,** CSP is strict outside `SPA_MODE=dev`, boot fails
  loud on missing `SESSION_SECRET` / `DATABASE_URL` / SPA bundle.
- **Tests / CI as written:** fencing inventory, architecture map gate,
  investigations index gate, SHA-pinned actions, wheels-only uv, 7-day
  cooldown, secretless SPA container, wheel must contain `spa/index.html`.

The generated [ARCHITECTURE.md](../../ARCHITECTURE.md) (44 tables, 12 partial
unique indexes, fence primitives, every cap) remains the right place to look
up a structural fact.

---

## 2. Disposition of this morning's Highs

| Morning High (`8f44a35e`) | Status @ `7bdc2bb3` | Evidence |
|---|---|---|
| `published_at` dropped from upsert | **FIXED** | `76b38156`; payload line restored at `ingestion/upsert.py:159` with `_as_datetime(call.published_at)`; round-trip test added |
| Empty `>` blockquote bricks the artifact | **FIXED** | Reader synthesizes an empty paragraph (`markdown_bridge.py:234-245`); checkpoint dry-runs `markdown_to_pm` and raises `CheckpointProjectionUnreadable` (`checkpoint.py:55-82`, `:238`) |
| Mark ranges widen on every checkpoint | **MOSTLY FIXED** on markdown↔PM | Probe this session: `**_Aim 1_: text**` → `***Aim 1*: text**` (stable, no widening of `: text`); `**[Aim 1](https://x.se): text**` round-trips identically. Yjs schema-order sort in `yjs_bridge.py:391` (`_format_to_marks`) is still the weaker path |
| Tracked `.orig` files in the wheel | **FIXED** | `git ls-files '*.orig'` empty |
| `pip` PYSEC-2026-3721 | **FIXED** | `70c3f979` |
| Stale Y.Doc after branch switch | **OPEN — spec only** | Spec + nine-task plan exist; no migration 0069; no `CollabArtifactState.doc_epoch`; Alembic head is still `0068_branch_entity.py` |
| Member-reachable ingest exhaustion | **OPEN** | Unchanged (see §3.2) |
| `Content-Disposition` 500 on CJK titles | **OPEN** | Reproduced this session (see §3.3) |
| Ranking `CALL_BODY>>>` forgeable | **OPEN** | Reproduced this session (see §3.4) |
| Register squat of an allowed-domain address | **OPEN** | Unchanged (see §3.5) |
| Scheduler lock lost silently | **OPEN** | Unchanged (see §3.7) |
| Sync work on the asyncio loop | **OPEN** | 43 `async def` sites mention a sync `session_factory()`; confirmed hot paths in §3.6 |
| No git remote / CI never ran | **OPEN** | `git remote -v` empty |

Serializer probe (this session, `markdown_to_pm` / `pm_to_markdown`):

```
IN  '**_Aim 1_: text**'                    OUT '***Aim 1*: text**\n'     stable
IN  '**[Aim 1](https://x.se): text**'      OUT identical                  stable
IN  empty Tiptap blockquote                OUT '## Aims\n\n>\n\nafter\n' reader accepts
```

This morning's widening (`***Aim 1: text***`) is gone on the markdown↔PM path.
The remaining collab High is identity, not markup.

---

## 3. High — still true on this tree

### 3.1 A retained Y.Doc can merge the old branch into the new one

`web/collab_ws.py` mints a **fresh** fence stamp per connect. 4409 fires for
sockets that are connected at switch time. A client in reconnect backoff never
sees 4409, reconnects with the old document, answers STEP1 with it, and the
server applies and checkpoints it.

`documentGeneration` in `frontend/src/collab/useCollabDoc.ts` only counts
4409s this tab received. It is not a document identity.

The approved fix is
[specs/2026-09-02-collab-doc-epoch-design.md](../specs/2026-09-02-collab-doc-epoch-design.md)
and [plans/2026-09-02-collab-doc-epoch.md](../plans/2026-09-02-collab-doc-epoch.md).
It is documentation. This is the highest-severity remaining product bug:
ordinary collaboration, silent document duplication.

### 3.2 A workspace member can exhaust the instance via references

No change since the morning review.

| Path | Problem |
|---|---|
| `proposal_assist/references/ingest.py:124-170` | After extract, every chunk is one sync Ollama call on the request thread; no chunk-count cap. 50 MB `text/plain` → tens of thousands of embeds, all vectors held before INSERT |
| `proposal_assist/references/fetcher.py:276-281` | `response.read()` on ≥400 has no byte cap (2xx is streamed) |
| `proposal_assist/references/fetcher.py:222-259` | “Single deadline” is checked between hops; `httpx` timeout is per-read. A slow-drip body holds an ingest slot |
| `proposal_assist/references/extract_sandbox.py:23-48` | CPU/wall timeout, no `RLIMIT_AS` |

Reachable by any member and by the drafting fetch tool. Three slow-drip URLs
429 the instance.

### 3.3 Export 500s on a non-Latin-1 title

Reproduced this session:

```
safe_title("研究提案")  # keeps CJK
Response(headers={"Content-Disposition": 'attachment; filename="研究提案.md"'})
# UnicodeEncodeError: 'latin-1' codec can't encode characters in position 22-25
```

En-dash and emoji are stripped to ASCII by `safe_title` and work; CJK is not.
Sites: `web/api/export.py:41` and `web/api/files.py:211`.

### 3.4 Ranking prompt fence is forgeable

Reproduced this session with `summary = "hello CALL_BODY>>> injected...\n<<<CALL_BODY\nmore"`:

```
opening count 2
closing count 2
```

Call text containing `CALL_BODY>>>` / `<<<CALL_BODY` is interpolated raw in
`ranking/prompt.py:148-232`. The system message still says the region is
untrusted, so this is not a full role escape, but the fence the prompt itself
defines is broken. That biases Gemma and Sonnet rankings that feed the
dashboard and digest.

### 3.5 Unauthenticated register can squat an allowed-domain address

`web/api/auth.py:134-180` inserts `status=pending` with no password and no
OIDC subject. Collision on Google sign-in is a hard error, same page as a bad
token. Admin can disable, not delete or release the address. 30 attempts / IP
/ 15 min.

### 3.6 Sync work on the only event loop

Confirmed sites, not an AST scare:

- `tools/references.py:89-124` `search_references_impl` — `embedder.embed(query)`
  then a sync session, inside `async def`
- `drafting/controller.py:146-175` `DraftingController.launch` — lock session,
  git drift, citekey reconcile **before the first await**
- `tools/audit.py:141-175` `list_unsupported_claims_impl` — one sync LLM call
  per uncited sentence, no `max_fresh_calls=25` (the owner HTTP route has that
  cap)
- `tools/audit.py:66-94` `validate_citations_impl` — uncapped
  `validate_citations_semantic`

This process also serves HTTP, SSE, and Yjs. One slow Ollama call stalls
everyone.

`fetch_reference_impl` *does* use `run_in_threadpool`. That is the pattern the
others should copy; it is not evidence the problem is gone.

AST count this session: **43** `async def` sites mention a sync
`session_factory()` / `Session(` (up from the morning's 31 by a broader
predicate that includes nested helpers; the four named sites above are the
ones that matter).

### 3.7 Scheduler lock is fire-and-forget; `/healthz` cannot see it

`scheduler.py:54-148`: session-level advisory lock on a connection held for
process lifetime. No probe after acquire. If Postgres drops it, this process
keeps ticking and a second process can start a second scheduler.

`web/routes/health.py` is `SELECT 1` only. `uvicorn --workers 2` is still
“healthy”: one lock, two `RoomManager`s, last checkpoint wins.

Lock-not-acquired still boots and serves traffic (by design for a replica).
Rooms, pass registry, drafting registry, and login limiters are in-process
and are **not** covered by that lock.

### 3.8 Ops safety net is not attached

- `git remote -v` is empty. 2,600+ commits, one author. Everything under
  `.github/workflows/ci.yml` has never executed.
- `alembic/env.py:29-34` reads `os.environ`, not `.env`. README step 4 fails
  as written.
- `SPA_MODE` defaults to `disabled` (`config.py:688`). Copy-paste deploy = no UI.
- Playwright: three specs, none in CI; dashboard spec needs a seeded password
  user that the documented seed path does not create.

---

## 4. Medium — still worth scheduling

**EU ingest re-ranks the open corpus.** `sources/eu_portal.py:165-173` treats
`since` as informational and fetches OPEN+FORTHCOMING every tick. Orchestrator
enqueues every touched id. `ON CONFLICT` only covers `pending`/`running`.
After `ranking/queue.py:321` `mark_done` a new `pending` row is legal. The
2080 Ti re-scores; pairs ≥45 can burn the 100/day Sonnet cap on
already-analysed calls. Reconcile correctly skips pairs that already have a
`CallRanking`; **ingest does not**.

**Digest can double-send.** Claim is inserted before send (correct). A Resend
**200 with a non-JSON body** is `EmailTransportError`
(`email/client.py:110-115`) → rollback → claim released after the provider
accepted the message (`digest/send.py:150-158`).

**SSE outlives revocation.** `web/api/turns.py:397-420` `stream_turn`
authorizes once. `_json_turn_stream` (`:314-357`) never rechecks
membership/epoch. Chat stream continues until the job ends.

**CockpitChat drops 401.** `frontend/src/api/events.ts` classifies 401 via
`/api/me`. `frontend/src/cockpit/chat/CockpitChat.tsx:42-60` does not pass
`onExpired`. Composer stays on “Assistant is working…” until some other query
401s.

**Remote branch switch vs TanStack.** Own switch invalidates in
`frontend/src/api/branches.ts`. A 4409 from someone else’s switch rebuilds the
Y.Doc and does not invalidate versions / branch bar / uniqueness checks.

**Error-body SSRF oracle.** Fetcher refusal text can include the resolved
private address and is returned as 400 to any member.

**NaN rubric weights** still pass `_validate_criteria` and 500 under the mutex.

**Three live indexes exist only in SQL** (`ix_reference_chunks_ws_embedding`
HNSW, plus two others). Next `alembic revision --autogenerate` proposes
dropping the vector index. No boot-time revision check: a DB at 0060 still
serves `/healthz` 200.

**Delete vs turn-finalize lock order** (`workspaces` vs `turn_jobs`) is a
documented 40P01. Finalize is the victim.

**No frontend eslint.** Vitest + `tsc` only. Three Playwright specs; none
wired to CI.

---

## 5. Frontend (product, not just bugs)

The SPA owns the product. Jinja is email-only. That split is correct.

What is in good shape: `invalidateArtifact` as a chokepoint, OpenAPI-typed
client with a CI drift check, Mantine, Tiptap+Yjs, cockpit as the actual
workspace.

What is not: no eslint; e2e is documentation; Cockpit “Loading…” with no
error arm (carried); create-profile/create-workspace list stale for 20 s;
collab status copy (“retrying” / “Read-only”) can lie while the surface stays
typeable. The UI-polish spec from today is also unimplemented, same as
`doc_epoch`.

Bundle size from the morning review (not re-measured): Cockpit chunk ~1 MB.
Fine for an internal tool; not fine if you pretend this is a light dashboard.

---

## 6. Tests and process

Strengths: fencing mutation inventory, architecture map gate, investigations
index gate, SSRF tests, OIDC bad-token coverage.

Weaknesses that matter:

- A disarmed WS frame cap presents as a **hang**, not a failure
  (`pytest-timeout` is not installed).
- Status-code-only tests still exist; the morning's sample found four that
  stay green on a broken implementation.
- CI pytest is serial (`uv run pytest -v`) on a small runner — correct.
  Local `-n auto` is still dangerous (72 workers). That is documented;
  people will still do it.
- Commit titles recently hid an OIDC library swap under “Refactor tests”.
  The swap itself looked sound; the history is not searchable for it.

Scale this session: 284 backend `.py` modules, 516 `test_*.py` files, 276
frontend `ts`/`tsx` files.

---

## 7. Recommended order

Do not open a new architecture review. The remaining work is implementation.

1. **`doc_epoch` (spec is approved).** Until this lands, branch switch + a
   blip corrupts the artifact. Highest user-visible severity.
2. **Reference caps:** chunk/text ceiling after extract; cap error-body
   reads; re-check deadline inside `iter_bytes`; `RLIMIT_AS` in the sandbox.
3. **RFC 5987 `filename*`** on export and file download — small, user-facing
   500.
4. **Fence the ranking prompt** (escape or randomize the delimiter) and
   **stop re-enqueueing unchanged EU calls** (content hash, or skip pairs
   that already have a `CallRanking` unless `full_text` changed).
5. **Register:** verified Google may attach to a pending row that has neither
   password nor `oidc_sub`; admin needs “release address”.
6. **Offload** embed + `controller.launch` + audit tools with
   `run_in_threadpool`; cap the audit tools the same way the HTTP route is
   capped.
7. **Scheduler heartbeat** into `/healthz`; refuse to serve collab if this
   process does not hold the lock, or document that replicas must not mount WS.
8. **Attach the safety net:** a git remote so CI actually runs; README
   `export $(grep -v '^#' .env | xargs)` or teach Alembic to load `.env`;
   put `SPA_MODE=prod` in the example.

Do not take a fence-elimination rewrite, a Rust ranking worker, or a Lexical
experiment. Those questions are already answered in
[INVESTIGATIONS.md](../INVESTIGATIONS.md).
