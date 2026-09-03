# Call-Monitor Thorough Project Review (2026-09-02)

**Scope:** `master` @ `8f44a35e`, clean tree, the whole project: backend, frontend, data
layer, ops/deploy, tests, repo hygiene, and the disposition of every finding the three
prior reviews left open.
**Method:** baseline runs of every check the repo has; eleven reviewers, each in its own
worktree with its own venv, briefed to prove by mutation or executed probe (label RAN)
rather than by reading (label READ); the headline findings re-executed independently by
the coordinating session from the main tree. One extra reviewer was sent at the modules no
earlier reviewer had opened, because a review that converges on a narrowed brief
converges to the brief.
**Code changes:** none. This document, its index entry, and nothing else.
**Where the evidence lives:** every reviewer's full report (probe commands, mutation
lines, outputs) is reproduced verbatim in the appendices at the end of this file.

---

## 0. Baseline — what the repo's own checks say (all RAN)

| Check | Result |
|---|---|
| `uv run pytest -n 16 --dist loadfile` (key unset) | **4387 passed, 1 skipped** in 85 s |
| `pnpm exec vitest run` | 125 files, **878 passed** |
| `pnpm exec tsc --noEmit` | clean |
| `uv run ruff check` | clean |
| `uv run ruff format --check` | **254 files would be reformatted** (not a CI gate) |
| `scripts/gen_architecture.py` | no drift |
| `uv run pip-audit` | **red**: `pip 26.1.2` PYSEC-2026-3721 (transitive via `pip-api` ← `pip-audit`); `uv lock --upgrade-package pip --dry-run` resolves 26.2 under the cooldown |
| `pnpm audit --prod` | clean; dev-only criticals (vitest, vite) as CI already documents |
| `pnpm run build` | ok; `Cockpit` chunk 1,068 KB, `index` 748 KB (12 chunks, 1.3 MB) |
| eslint | **no configuration exists**; nothing lints the SPA |
| Playwright e2e | 3 specs; none wired to CI; `dashboard.spec.ts` cannot pass (no seeded password user), `cockpit.spec.ts` is two `test.skip` stubs |
| Git | **0 remotes configured**. 2,647 commits since 2026-04-14, one author. Every workflow under `.github/` (SHA-pinned CI, pip-audit, OSV, dependency-review, dependabot) has never executed anywhere |
| `app.log` | 342,903 lines over 2.93 days = **117k lines / 32 MB per day**; 99.65 % is `apscheduler.executors.default` at INFO |

---

## 1. Executive assessment

The foundations the earlier reviews praised are still real, and this pass measured them
rather than re-asserting them: a mutation sweep over the twelve highest-stakes gates
(CSRF, owner/member/admin guards, session epoch, SSRF fail-closed, both token caps, both
fence primitives, the collab frame cap and varuint bound, argon2 verify, OIDC
`email_verified` and domain allowlist) went **16 for 16** — every mutation was caught by
the test file that claims to pin it. The OIDC library swap that landed under a commit
titled "Refactor tests" was probed with fifteen classic bad tokens (wrong `iss`/`aud`,
expiry, `alg:none`, HS256-with-public-key confusion, unknown `kid`, unverified email) and
rejected all of them. The SSRF deny-list, the digest's HTML autoescape, the six
cloud-egress gates, and the fencing of every reachable `git commit` all held.

What this pass adds is concentrated in four places the earlier reviews did not measure:

1. **The collaborative editor corrupts documents on ordinary paths** (§2.1–2.3). Three
   reproduced-from-the-main-tree defects: a stale editor tab that reconnects after a
   branch switch merges the old branch's text into the new one; bold-plus-italic and
   link-inside-bold spans widen on every checkpoint; an empty blockquote commits a bare
   `>` that the reader cannot parse, which severs saves for that artifact.
2. **A three-day-old ingestion regression** (§2.4): the type-settling fix `8a297db1`
   dropped `published_at` from the upsert payload. Every call first seen after
   2026-08-29 lands with no publication date, which excludes it from backfill ranking
   and sinks it in the dashboard's default sort. Live impact today is zero rows (the live
   DB holds 58 calls, all older); impact begins with the next new call.
3. **A member can exhaust the instance through reference upload and fetch** (§2.5):
   no cap on chunk count after extraction (a 50 MB text file = 58,255 sequential Ollama
   calls on the request thread; markdown = 3.7 M), an uncapped read of 4xx/5xx bodies
   (200 MB pulled for a 1 MB limit), a per-read timeout mistaken for a deadline (a
   slow-drip server holds an ingest slot indefinitely), and an extraction sandbox with a
   CPU limit but no memory limit (a 2 MB PDF allocated 4 GB in the child).
4. **The project's safety net is not attached to anything** (§2.6). The clone has no git
   remote, so the CI that the documentation, the prior reviews, and the supply-chain
   design all lean on has never run, and five months of single-author work have no
   off-box copy. The README's migration step fails as written, a verbatim copy of
   `.env.example` yields a deployment with no UI and digest links to `localhost`, the
   cutover script resets the production DB role's password to a literal, and
   `uvicorn --workers 2` boots healthy while splitting every in-process room.

Of the 43 findings the three prior reviews left open, this pass re-verified every one:
**8 fixed, 3 partial, 1 refuted (the Referrer-Policy "comment defect" — the comment is
correct; MDN confirms `no-referrer` nulls `Origin` on form posts), 31 still open.** The
ones that matter most are unchanged: 31 `async def` sites still build a synchronous
Session on the app loop, the scheduler lock is still lost silently, `Content-Disposition`
still 500s on a non-Latin-1 title, and the ranking prompt fence is still forgeable.

**Grade: B−, unchanged from the review of record.** The security posture is good and is
now measured. The collab and ingestion defects are ordinary-path data-corruption and
availability bugs in the two features users touch most, and the operational gaps mean a
bad day would be discovered by users, not by the system.

---

## 2. Findings — high

Severity is harm-per-trigger: who can cause it and what is lost. Every High here has an
executed reproduction; the coordinating session re-ran the ones marked ✔ from the main
tree independently of the reviewer that found them.

### 2.1 Collab — a retained Y.Doc that reconnects after a branch switch merges the old branch into the new one ✔

`collab/room.py:153-155, 201-246`; `web/collab_ws.py:66-152`;
`frontend/src/collab/collabProvider.ts:266-278`.

The `e312b373` fix closes *connected* clients with 4409 at switch time. A client that is
**not connected** at that moment — in reconnect backoff after a network blip, asleep on a
half-open socket, or whose close frame was buffered into a dead socket — keeps its old
document, reconnects after the switch with a *fresh* generation stamp (so the fence never
fires), answers the server's STEP1 with its whole old document, and the server applies
it, persists it under that user's `actor_id`, and checkpoints the doubled document to the
new branch.

RAN (exact handshake through `Room.handle`, re-run from the main tree):
```
room before reconnect: '## Aims\n\nNew aims text.\n'
room AFTER reconnect:  '## Aims\n\nOld aims text.\n\n## Methods\n\nOld methods.\n\n## Aims\n\nNew aims text.\n'
```
Same symptom class as the duplicate-copies corruption already suffered once. The
2026-09-01 branch-leak spec lists only the 4409 storm as remaining; this is a different,
non-4409 path. **Fix shape:** a per-artifact `doc_epoch` in `collab_artifact_state`,
bumped by every `truncate_log` caller; the route closes 4409 on mismatch *before* sending
STEP1. The generation fence cannot do this because it is re-minted per connect.

### 2.2 Collab — the serializer mis-nests marks read back from Yjs; bold/italic/link ranges widen on every checkpoint ✔

`collab/markdown_bridge.py:516-520` (`close_to` stops at the first still-wanted mark);
`collab/yjs_bridge.py:395` (marks re-sorted into schema order: link < em < strong).

RAN (md → ydoc → md, exactly hydrate + checkpoint, re-run from the main tree):
```
IN : '**_Aim 1_: text**'               OUT: '***Aim 1: text***'                  (": text" gained italic)
IN : '**[Aim 1](https://x.se): text**' OUT: '[**Aim 1: text**](https://x.se)'    (link widened)
```
The live room is right; git, the canonical projection, export, the LLM's context, and —
because `apply_pm_section_to_fragment` re-parses the serialized section — **the live doc
after any citation insert or accept on that section** carry the corruption. **Fix shape:**
when the target set lacks any *active* mark, close down to the outermost such mark and
re-open the still-wanted inner ones; pin both shapes through `pm_json_to_ydoc`, not only
`markdown_to_pm`.

### 2.3 Collab — an empty blockquote serializes to a bare `>`, which the reader cannot parse; the next rehydration bricks the artifact ✔

`collab/markdown_bridge.py:218-221, 344-345, 633-635`; `collab/canonical.py:32-38`;
`collab/flip.py:202-204` (flag committed before the projection is built);
`web/api/workspaces.py:318-331`.

Tiptap's blockquote is `block+`; typing `> ` and moving on leaves `blockquote[paragraph()]`,
which checkpoints as `>\n`. RAN (re-run from the main tree): `checkpoint commits:
'## Aims\n\nintro\n\n>\n\nafter\n'` → `REHYDRATE RAISED: ValueError Invalid content for node
blockquote: <>`. Through the real draft route: two GETs both 500 with that error, the
collab flag is committed, and the git-first save path returns 409 — the artifact is
unreadable and unsaveable. The same `ValueError` is not in `_CLEAN_ACCEPT_ERRORS`, so a
pre-mutation failure evicts the whole room. Two siblings of the same class (Medium): a code
block containing a ``` line is emitted with a 3-backtick fence and swallows every later
section; a trailing hard break becomes a literal backslash and a hard break in a heading
renames the section. **Fix shape:** synthesize an empty paragraph for an empty blockquote
(the list-item rule already does this); fence length = longest backtick run + 1; drop a
block-final `hard_break`. Then make `checkpoint_artifact` dry-run `markdown_to_pm` on what
it is about to commit and refuse or log on failure — this one guard would have caught all
of §2.2–2.3 at commit time.

### 2.4 Ingestion — `published_at` dropped from the upsert payload since `8a297db1` ✔

`ingestion/upsert.py:147-170` (payload) and `:180` (update set derived from it).

The parent of `8a297db1` had `"published_at": call.published_at` at line 98; the rewrite
with `_as_text/_as_int/_as_datetime` wrappers omitted it. Both adapters still populate it
(`sources/vinnova.py:292,306`, `sources/eu_portal.py:283`). Because the update set is
derived from the same dict, pre-existing rows keep their value; new rows get NULL forever.

Consequences: `ranking/backfill.py:259` and `ranking/queue.py:151` filter
`published_at >= cutoff` (NULL is false), so **profile edit, bootstrap, `/admin/rerank`
and the daily reconcile never enqueue a call ingested after 08-29**; `web/dashboard.py:165`
sorts `published_at DESC NULLS LAST`, so every new call sinks below every old one; the
call-detail page shows "—".

RAN: probe persisted `published_at = None` for a `NormalizedCall` carrying a date; a real
two-call ingest followed by a profile create enqueued **0** backfill jobs. Live DB
(read-only count): 58 calls, 0 NULL — all predate the commit, so impact starts with the
next genuinely new call. Three hollow gates let it through: `test_ingestion_e2e.py:75-76`
asserts an order that `Call.id DESC` already satisfies (mutating the dates leaves it
green); `test_ingestion_poison_record.py:73` parametrises `published_at` under "still
ingests" but never reads it back; the backfill/queue tests seed `Call` rows directly.
**Fix shape:** one line in the payload, one round-trip test through `upsert_call`, repair
from `raw->>'publicationDate'` / `raw->>'Oppningsdatum'` if any NULL rows appear.

### 2.5 References — a member can exhaust the instance through upload and fetch ✔ (F2, F3)

| # | Where | Executed proof |
|---|---|---|
| a | `references/ingest.py:124-170` — no cap on extracted text or chunk count before embedding; each chunk is one synchronous Ollama call on the request thread inside `limiter.slot()`, all vectors held in memory before one INSERT | 50 MB `text/plain` (allowed MIME, under both caps) → **58,255 chunks** = 58,255 sequential embed calls, ~227 MB of vectors; 50 MB markdown → **3,744,914 chunks** |
| b | `references/fetcher.py:276-281` — `response.read()` on a ≥400 status materialises the whole body; the streaming cap covers only the 2xx branch | 200 MB served under `max_bytes=1 MiB`: status 200 → 1 MB read; **status 500 → 200 MB read**, peak RSS 626 MB ✔ |
| c | `references/fetcher.py:222-238, 259` — the "single deadline" is checked only *between* hops; `httpx.Client(timeout=remaining_s)` is a per-read timeout | 12 bytes at 0.4 s intervals with `timeout_s=1.0` → **returned after 4.5 s, no `FetchError`** ✔. Mutating line 259 to `timeout=None` leaves all 33 fetcher tests green (hollow, shape 4) |
| d | `references/extract_sandbox.py:46-48` — CPU-time limit, no `RLIMIT_AS`; pdfminer inflates streams whole | 1.99 MB PDF whose one page is `zlib(2 GiB of spaces)` → child **peak RSS 4.04 GB**, "finished" inside the 30 s budget |

All four are reachable by any workspace member (`web/api/references.py:100, 231`) and by
the drafting agent's fetch tool; (c) holds one of the instance's ingest slots, so three
such URLs 429 every upload, fetch, and agent-fetch on the instance until the drip ends.
**Fix shape:** enforce a chunk / text-length cap after `chunk_sections` (truncate with a
visible `[truncated]` section like the PDF page cap); read at most a few KB of an error
body; re-check `deadline_time` inside the `iter_bytes` loop; `setrlimit(RLIMIT_AS)` at the
top of the sandbox worker.

### 2.6 Ops — the safety net is not attached ✔

| # | Finding | Proof |
|---|---|---|
| a | **No git remote.** Everything under `.github/` is dead; no off-box copy of the code exists (only `~/db-backups` for Postgres) | `git remote -v` → nothing; 2,647 commits, 1 author |
| b | **A second process is tolerated by design, but only the scheduler is single-instance-safe.** Lock-not-acquired keeps booting with one WARNING and serves traffic; `RoomManager`, pass registry, drafting registry, lifecycle tombstones and login limiters are per-process | `uvicorn --workers 2` → `/healthz` 200 on both, 1 advisory lock, 2 worker pids. Two collaborators on different workers get two Yjs docs that never see each other; last checkpoint wins. Killing the lock backend post-boot: instance keeps serving 200 with no log line; a second instance then takes the lock |
| c | **README step 4 (`uv run alembic upgrade head`) fails as written**: pydantic-settings reads `.env`, `alembic/env.py:29-34` reads only `os.environ`, and nothing says to export | `env -i … uv run alembic current` → `RuntimeError: No database URL` ✔ |
| d | **`scripts/cutover_rename_database.sh:53-56` unconditionally sets the role password to the literal `'call_manager'`** and cites a rotation pattern that does not exist in `.env.example` or README; the script calls itself idempotent | READ ✔ |
| e | **`.env.example` contradicts the code**: it says `INITIAL_ADMIN_PASSWORD` is "(re)set on each startup"; `admin_bootstrap.py:105-119` and its tests never overwrite an existing hash. A verbatim copy yields `SPA_MODE=disabled` (no UI), `ADMIN_DASHBOARD_URL=http://localhost:8000/` (every digest link), a model id the code no longer names, and drafting on with no key (2,880 INFO lines/day). **57 Settings aliases** have no line in the example, incl. `SPA_MODE`, `COLLAB_KILL_SWITCH`, `SESSION_MAX_AGE_SECONDS`, the spend caps | RAN (example loaded as `.env`) |
| f | **Shutdown can block ~16 min**: `scheduler.shutdown(wait=True)` runs *before* subprocess teardown, ticks are bounded at 300/660/960 s, uvicorn has no `timeout_graceful_shutdown`; systemd's 90 s then SIGKILLs past the teardown | RAN (uvicorn defaults); idle exit 0.7 s |
| g | **Nothing states what must be backed up**, nothing is pruned: 36 of 44 tables have no deleter; soft-delete frees zero bytes; no purge exists | RAN census |
| h | **`app.log` grows 32 MB/day** of APScheduler "Running job" INFO lines; there is no `LOG_LEVEL` setting and `logging.py` sets only the root level | RAN |

### 2.7 Still open from the review of record, still High

- **31 `async def` sites build a synchronous Session on the app loop** (AST count, up
  from 28 by rule, not drift). `tools/references.py:124` still calls the embedder
  synchronously inside `search_references_impl`; `drafting/controller.py:146-175` does
  lock-session, git-drift and citekey reconciliation before its first await. One queued
  Ollama embed or a 10 s lease wait freezes every HTTP, SSE and WS request.
- **`Content-Disposition` 500s on any non-Latin-1 title or filename** (`export.py:41,80`,
  `files.py:211`): `safe_title("研究")` + `Response(headers=…)` → `UnicodeEncodeError`.
  Reproduced again; any member exporting a workspace with a CJK, en-dash or emoji title.

---

## 3. Findings — medium

Grouped by subsystem. Each has `path:line` and a scenario; RAN unless marked READ.
Full detail in the appendices.

### Fence spine and lifecycle
- **Delete and turn-finalize lock `workspaces` and `turn_jobs` in opposite orders** —
  a real `40P01`; finalize is the victim (`workspaces.py:180-230` M→W→J vs
  `turns/processor.py:651-702` J→W). The finalize's token stamp, `spend_ledger` row and
  `cloud_turn_completed` event roll back; the delete cancels the turn with
  `usage_unknown=True`. `workspaces.py:198-205` documents this race as "closed durably";
  it is closed by a deadlock. Docs disagree with each other on the order
  (`fencing.py:33-38`, `analysis/queue.py:302`); no test asserts an order across paths.
- **A Phase-0 worker claimed at generation G writes `_m2`/`_m3` into generation G+2
  after delete → restore** (`phases/setup.py:473-487, 512-514, 592-599`); only `_m1`
  takes Primitive B and `claimed_at_generation` is dropped after it. The comment at
  `workspaces.py:53-60` claiming the liveness check catches this is false. Spec
  mechanism 3 ("assert on the already-locked Workspace row") has zero users and is the
  cheap fix.
- **`_insert_citation_locked`'s generation fence is invisible to the static gate and
  has no behavioural pin** (`citations/core.py:575-577`): replacing it with a raw mutex +
  liveness re-read leaves `tests/fencing/*` and five behavioural files green; the same
  mutation on `review/pass_runner.py:476` reddens two tests, so the technique detects and
  this site is the hole. `post_init_spec._git` is pinned by a sentence only (READ).
- `fence_inventory.json` still says `run_review_pass` is `fenced:false`; it has been
  fenced and pinned since 2026-08-30. Memory's "2 git sites still take no mutex" is now
  **0 reachable + 1 dormant** (`bibliography.py:627` behind `commit=True`, which no
  production caller passes).

### Data layer
- **Three live indexes are absent from the ORM** — `ix_reference_chunks_ws_embedding`
  (HNSW, `vector_cosine_ops`, raw SQL in 0031), `ix_reference_chunks_ws_model` (0032),
  `ix_references_indexing_backlog` (partial, 0031). Measured with `compare_metadata`
  against a migrated pgvector container: **exactly 3 ops, all `remove_index`**, nothing
  else. The next `alembic revision --autogenerate` drops the vector index; `grep
  compare_metadata tests/` → nothing. 68 migrations, single head `0068`, three
  `pass`-only downgrades (0010, 0037, 0045). No boot-time revision check; a DB at 0060
  boots and `/healthz` is 200.
- `claim_classifications.reference_id` / `user_claim_annotations.reference_id` remain
  unindexed FKs (READ, carried).

### Ranking, ingestion, digest
- **Every EU tick re-enqueues the entire open EU corpus × every active profile**
  (`sources/eu_portal.py:165-173` ignores `since`; `orchestrator.py:185-189, 263` treats
  every fetched call as touched; no content hash). Two identical ingests → `['done',
  'pending']`. The 2080 Ti re-ranks the corpus daily, `rank_pair.py:167-170` re-escalates
  every ≥45 pair to Sonnet, and `worker.py:166` spends the 100/day cap on already-analysed
  pairs; the timeline gate re-evaluation drops a call to `0 / timeline` two weeks before
  its deadline.
- **The digest week-claim is only flushed before the send; a post-accept failure
  re-sends** (`digest/send.py:129-191`). Resend returning 200 with a non-JSON body
  (`email/client.py:110-115`) → `EmailTransportError` → rollback → claim released though
  the message went out. Probe: `committed DigestRun rows visible to another session at
  send time: 0`. The Sonnet ledger (`ranking/worker.py:190-211`) already shows the
  correct discipline.
- **Ranking prompt fence forgery** carried and re-run: a summary containing
  `CALL_BODY>>>` → 2 closing markers, 2 opening, injected text outside the fence.
- A near-miss Gemma reply (`4.0`, `"4"`, `Pass`, fenced JSON) is retried 5× at
  temperature 0 then dead-lettered with no ranking (Low); profile edits with a blank
  description skip backfill entirely (`web/api/profiles.py:398`, Low, READ).

### Web API
- **User-caused fetch failures reach the user as "internal server error"**
  (`references.py:271-272` → 502 → `main.py:515` sanitizes every ≥500). The codebase
  knows this trap (`turns.py:118-125`, `collab_checkpoint.py:56-80`); this site was
  missed.
- **`NaN`/`Infinity` weights pass `_validate_criteria`** (`rubric.py:113,131`) and fail
  inside `workspace_write` with the mutex held → 500; `-Infinity` → 400, proving `< 0`
  is the only gate. Fix: `Field(ge=0, allow_inf_nan=False)`.
- **The SSRF refusal message embeds the resolved private address** and the route echoes
  it in a 400 to any member (`fetcher.py:117` via `references.py:269-270`): an
  internal-DNS oracle using the server's resolver.
- `add_member` is a registration oracle (201 vs 400) and admits pending/disabled users;
  `get_spec` hands the locker's email to every member while `list_members` is owner-gated
  for exactly that; `create_workspace_api` accepts an archived profile; `dismiss_uncited_claim`
  stores an unbounded raw path segment; `profile_ids` unbounded → 65,535-parameter
  `OperationalError`. (All Low, all RAN.)
- Carried and still open: `list_turns` unbounded; SSE stream authorizes once and outlives
  logout/disable/removal; `add_member` case-sensitive email; activity feed discloses
  emails; login limiter consumes a slot on success; logout accepts an epoch-stale cookie;
  `str(exc)` in claim-analysis `last_error`, export 409 and references 400;
  **24 of 114** `/api` operations sit outside the error-envelope router (was 19/109).

### References and citations
- **Reprocess silently re-keys and de-approves a reference** (`ingest.py:285-296`;
  `csl.py:476-495` counts the row's own citekey as taken): upload → approve
  (`smith2021`) → extraction fails → Reprocess → `smith2021a`, `extracted`, LLM title
  over the approved one. Bibliography build is fail-closed on `metadata_unapproved`.
- **The deep citation check pins a Postgres connection `idle in transaction` across
  every LLM call** (`citation_audit.py:606-665`): up to 25 × 60 s per request on a pool
  of 10+20. `ingest.py:139-143` explicitly avoids this for embeds.
- `text_error` persists and returns the internal Ollama URL to every member (Low);
  the docstring promises CGNAT is refused, `is_private` does not cover 100.64/10 (Low).

### Frontend
- **A branch switch made by another member leaves every cockpit read-model on the old
  branch**: the 4409 handler rebuilds the Y.Doc but touches no TanStack cache; only the
  user's own switch invalidates. Status-bar pill, Versions pane, BranchBar and the
  selection toolbar's uniqueness check all run against the old branch.
- **Session expiry mid-turn: the SSE 401 classification is thrown away** — CockpitChat
  passes neither `onExpired` nor `onNoAccess`; the composer stays disabled under
  "Assistant is working…" until the unrelated Problems poller hits a 401 (≤ 15 s).
- `Cockpit` renders "Loading…" forever when cockpit-meta errors (no `isError` arm);
  create-workspace / create-profile never invalidate their lists (stale for 20 s with no
  refetch — "Create your first profile" after creating it); BranchBar swallows switch/
  delete failures (the server *aborts* a switch on lock timeout → 409 → modal closes,
  nothing said); the collab placeholder says "retrying" in two terminal states and the
  status bar says "Read-only" over a dead socket while the surface stays typeable
  (mutating the copy leaves 11/11 tests green).
- Low: the bubble-menu test cannot fail on `shouldShow` (mutation to `() => true` stays
  green); dead invalidation key `["proposals", id]`; `onSave` writes the raw buffer over
  the refetched canonical section (flag-off path); toggle buttons lack `aria-pressed`,
  composer controls unlabelled, no focus management on route change.

### Auth, admin, and the agent's tools (the modules no reviewer had opened)
- **An unauthenticated `POST /api/auth/register` squats any allowed-domain address and
  blocks that person's Google sign-in with no admin recovery surface**
  (`web/api/auth.py:134-180`; `auth/accounts.py:38-39` refuses on `email_collision`
  regardless of the row's status; `web/routes/oauth_google.py:161-163` answers
  `/auth/error`, the same page as a bad token). The admin API has approve/deny/disable/
  enable/role/reset-password and nothing that deletes a user or releases an address;
  `deny` maps to `disabled`, which still collides. Probe: register `victim@umu.se` with no
  cookie → 200; the victim's verified Google sign-in → `/auth/error` while pending, after
  deny, and after enable. 30 addresses per IP per 15 min. Fix shape: on collision with a
  pending row that has neither `oidc_sub` nor `password_hash`, attach the verified Google
  identity to it; give admins a "release address" action.
- **The agent's `list_unsupported_claims` and `validate_citations` tools make one
  synchronous LLM call per sentence / per ledger row on the event loop with no per-call
  cap** (`tools/audit.py:78-94, 141-175`); the owner-facing route for the same audit got
  `validate_citations_semantic_capped(max_fresh_calls=25)`, the tool did not. Probe: 50
  sentences with a 20 ms stub → a concurrent 5 ms ticker got zero turns for 1.01 s. This
  impl constructs no Session, so the prior AST count of loop-blocking sites does not
  include it. Converts a lead from the references review.
- An oversized `page` overflows the int8 OFFSET and 500s on `/api/calls` (any user) and
  both admin user lists (Low); four MCP handlers coerce `int(args.get(…))` outside
  `_safe_tool`, so a non-numeric argument reaches the model as a raw Python error and the
  operator sees nothing — the authors fixed exactly one of five (Low).
- Three more hollow gates by mutation: `test_approve_rejects_non_pending` never reaches
  the status guard it names (27 and then 50 tests stay green with the guard removed —
  and `approve` rewrites status, role, hash and epoch with none of the other guards); the
  declared-MIME-equals-sniffed-format check in `files.py:96` has no pin (39 green);
  `propose_citation`'s foreign-workspace refusal has no pin in any of 7 files (57 green).
- Refuted: the "broken limiter fails open" lead in `tools/references.py:506` is a dead
  branch (the only wired limiter is a non-blocking `Semaphore.acquire`, which cannot raise).

### Tests and CI
- **A disarmed frame cap shows as a hang, not a failure**: with `collab_ws.py:251`
  neutralised, `test_oversized_frame_closes_the_connection` blocks forever in
  `receive_bytes()`; `pytest-timeout` is not installed and no suite timeout exists. In
  CI a regression would be a job timeout with no failing test id.
- 194 of 4,156 tests (4.7 %) assert only a status code or `not None`; of the 18
  suspicious 2xx-only / not-None-only ones sampled, **4 pass on a broken implementation**
  (`test_reset_password_has_no_status_gate`, `test_reenable_already_enabled_noop`,
  `test_build_scheduler_includes_turn_reaper_job`, `test_tools_claude_adapter_wiring`).
- `PytestUnraisableExceptionWarning` still bypasses `error::ResourceWarning`
  (`pyproject.toml:256`); a targeted run of 34 tests showed none this time.
- Hollow gates found by mutation this pass: the `published_at` ordering test (§2.4), the
  fetch deadline (§2.5c), `test_post_citation_foreign_reference_is_404` (passes a
  nonexistent UUID, never a foreign one — mutating the cross-workspace predicate leaves
  78 tests green), `_insert_citation_locked` (above).

---

## 4. Repo hygiene (Low, all RAN)

- Both `.orig` files are **tracked** (added in `355e8107`), and
  `collab/room.py.orig` (49,725 bytes, still documenting 1013 as the branch-switch code)
  **ships inside the production wheel**. Neither holds anything not in the tree or history.
- `.worktrees/branch-entity` and `.worktrees/version-graph-lanes` are fully merged
  (0 ahead of master) and can be removed.
- `research-log.md`, `research-tree.yaml` and `literature/survey.md` (a model-routing
  literature survey, `430e7c24`) live at the app repo's root; `README.pdf` (142 KB) is
  tracked although `docs/**/*.pdf` is ignored.
- Commit-message drift: the last five commits are titled "Refactor and enhance codebase
  for clarity and performance", "Refactor API error handling and improve test coverage",
  and "Refactor tests to use test_client_factory … and update dependencies". The last of
  these swapped the OIDC JWT library (authlib → joserfc) and rewrote claim validation.
  The swap is sound (15/15 probe), but a security-relevant change under a test-refactor
  title is the kind of thing a future reader will not find.
- `SlidingWindowLimiter.sweep()` exists and is unwired; growth is bounded by the email
  length cap and the IP-first check, so this is documentation debt only.
- The `frontend/e2e/dashboard.spec.ts` selector fix at `8f44a35e` (the HEAD commit) was
  reviewed separately: it repairs one locator in a spec that nothing runs and that cannot
  pass without a seeded password user; four of its remaining assertions are inert or
  misleading (details in Appendix 0).

---

## 5. Checked and sound — do not respend this effort

- **Mutation sweep, 16/16 caught** (Appendix A8): CSRF Origin mismatch; `require_owner`
  downgrade to member; `require_member` removal; session epoch; SSRF blocked-IP predicate
  and adapter fail-closed; per-workspace and global token caps; both fence primitives;
  frame cap (by hang) and varuint bound (by the `< 2.0 s` pin); `verify_password`;
  OIDC `email_verified` and domain allowlist. Every catch came from the named file.
- **OIDC after the joserfc swap**: 15/15 bad tokens rejected; `iss`/`aud`/`exp`/`iat`
  with leeway, `nonce`, `email_verified`, `alg:none`, HS256 confusion, unknown `kid`
  (triggers exactly one JWKS refetch), garbage, over-long claims.
- **SSRF deny-list** (references fetcher): loopback, RFC1918, link-local, metadata,
  0.0.0.0/8, 192.0.0/24, 198.18/15, 240/4, multicast, broadcast, IPv6 loopback/link-local/
  ULA, v4-mapped, NAT64, 6to4, Teredo, `::`; `file:`/`data:`/`gopher:`; hex/decimal/
  `127.1` forms resolved then checked; redirects re-validated per hop; env proxies off.
- **Every reachable `git add`/`git commit` runs under `acquire_commit_mutex_at_generation`**
  (full enumeration in Appendix A1 §item 20). gitio env is hermetic and ceiling-bounded.
- **Fence gates detect** an added unfenced sink, a fencing import outside the allowlist,
  a raw `select` in a converted tool, a late mint; removing the fence at `save.py:219`,
  `effects.py:120`, `reconcile.py:84` each reddens the expected test.
- **Frame cap / bounded reader**: 8 frame shapes × writer/viewer = 16 doors at exactly
  1,000,000 bytes, all ≤ 1.4 ms; no `read_message` import outside `protocol.py`.
- **Digest**: `autoescape=True` unconditionally, no `|safe`, subject CR/LF stripped;
  subscription and status filters; Stockholm week in Python and a generated
  `date_trunc(... 'Europe/Stockholm')` column agree; `DIGEST_DRY_RUN` writes nothing.
- **Cloud egress**: all six reference/citation paths gate via `citation_cloud_permitted`
  or the turn gate; the four fan-out endpoints and four pass runners enforce the global
  cap (`8c2d4555`); compaction is capped (`36f92cc7`).
- **Boot**: missing `SESSION_SECRET`/`APP_BASE_URL`/`DATABASE_URL` fail loud naming the
  key; a bogus DB URL never echoes the password; `SPA_MODE=prod` without a bundle raises
  at `create_app`; `EMBEDDING_DIM` mismatch refused. uvicorn trusts only `127.0.0.1` for
  proxy headers; the app's hop logic is consistent. No logger emits passwords, secrets,
  cookies, tokens, prompts or reference text.
- **Ranking**: NULL/mismatched embeddings → UNKNOWN → LLM runs; gates always win over the
  LLM; missing budget/TRL → UNKNOWN never FAIL; the Sonnet ledger commits in its own short
  transaction before the merge; PubMed query injection sanitised.
- **Compaction vs concurrent append**, retire/switch with a persist in flight, viewer
  write frames dropped pre-decode, `actor_id` attribution (except via §2.1).
- The branch-leak spec's four bugs all landed (`e312b373`, `183f8cdd`, `ad2aaee1`,
  `99067e1c`, `355e8107`); a draft/spec read now takes `FOR SHARE` on the workspace row
  and reads the blob at `refs/heads/<branch>`, so the old-name/new-bytes window is closed.
- **Admin and drafting lifecycle** (Appendix A11, 24 mutations red): last-admin disable
  and demote guards (row-locked, safe under READ COMMITTED), approve epoch bump, current-
  password check, login status gate, register domain gate, ILIKE escaping, all three
  retention-inspection guards, post-claim daily-cap recheck, digest bounce threshold and
  status filter, adapter `tools=[]`/`setting_sources=[]`, profile update 403,
  cross-workspace attachment and reference scoping, limiter window, best-profile
  ownership, drafting exclusive in-flight. Subprocess teardown is bounded (3 s disconnect,
  SIGKILL, 2 s reap) and unconfirmed deaths still count against the ceiling.
- Frontend: the 401 chokepoint covers all queries/mutations; sign-out is a full-page nav;
  no `switch`/`default` in non-test source (`collabStatus.ts` uses `never`);
  `invalidateArtifact` prefix-matches both artifacts; 4409 loop bounded at 8; tsc strict
  is a real CI gate via the Docker build.

---

## 6. Disposition of the prior reviews' open findings

43 items re-verified (Appendices A1, A2). Summary:

| Status | Count | Notable |
|---|---|---|
| **Fixed** | 8 | branch-leak bugs (`355e8107`), review-runner mutex (`ab3c3a3d`), gitio hermetic env (`9b85df24`), global cap on fan-outs (`8c2d4555`), 4409 bound, reconnect give-up label and Versions invalidation (`2d2cd3aa`), citation egress gate (`c67109a8`), `base_branch` propagation (`ad2aaee1`; the 422 path has no test) |
| **Partial** | 3 | compaction capped but input unbounded (`36f92cc7`); Problems panel basis fixed, failed signals still invisible; README covers single-process and `SPA_MODE=prod` but not path/backup/confidentiality |
| **Refuted** | 1 | the `Referrer-Policy` comment is correct — `no-referrer` sets `Origin: null` on form posts (MDN); the two 2026-09-01 reviews' "comment defect" belongs on the refuted list |
| **Open** | 31 | §2.7 and the "carried" lines in §3; plus scheduler-lock loss, `/healthz` DB-only, `WORKSPACES_DIR` unvalidated, `trigger_job_now` lying on a lockless replica (now reachable on every non-lock worker), secret-scrub list, `_current_head` without timeout, WS TEXT-frame `KeyError`, no frontend lint gate, no `alembic check` |

Counts drifted since the prior reviews and are corrected here: alembic files 69 → **68**;
missing `.env.example` aliases 54 → **57**; `/api` ops outside the envelope 19/109 →
**24/114**; sync-Session-in-coroutine sites 28 → **31** (rule, not drift).

---

## 7. Recommended sequence

Ordered by measured harm per unit of effort.

1. **Attach the safety net** (one hour): add a remote and push; the CI is already written
   and will go red on `pip-audit` — bump `pip` with `uv lock --upgrade-package pip`.
   Take a `git bundle` today regardless.
2. **Restore `published_at`** (§2.4): one payload line, one round-trip test through
   `upsert_call`, a repair query if any NULL rows exist by then.
3. **Collab document integrity** (§2.1–2.3): `doc_epoch` checked before STEP1; empty
   blockquote → empty paragraph, fence length = longest run + 1, drop block-final hard
   breaks; the mark close/re-open fix; then a `markdown_to_pm` dry run inside
   `checkpoint_artifact`. Delete `room.py.orig` while there.
4. **Reference upload bounds** (§2.5): chunk/text cap after `chunk_sections`, error-body
   read limit, deadline inside the read loop, `RLIMIT_AS` in the sandbox. Four small
   changes, each with the probe from Appendix A4 turned into a test.
5. **Single-instance enforcement** (§2.6b): lock-not-acquired is fatal unless an explicit
   standby flag; `/healthz` reports lock ownership; `trigger_job_now` returns False on a
   non-running scheduler.
6. **Schema drift gate**: a `compare_metadata` test against the migrated container, then
   add the three indexes to the ORM (the HNSW one needs `postgresql_using="hnsw"` and
   `postgresql_ops`).
7. **Lock order and Phase-0 fence**: take the workspace row before the `turn_jobs`
   UPDATE in `_finalize_success`; thread `claimed_generation` into `_m2`/`_m3`; one
   delete→restore→resume test per pinned site (`_insert_citation_locked` first).
8. **Operator surface**: `env.py` falls back to `Settings().database_url` (or the README
   exports `.env`); regenerate `.env.example` from the Settings fields and load it in a
   test; fix or delete the cutover script; a "Backup and retention" README section;
   `--timeout-graceful-shutdown` and subprocess teardown before the tick wait; a
   `LOG_LEVEL` setting with `apscheduler` at WARNING.
9. **Carried Highs, plus two from the remainder**: move the embedder, the controller's
   pre-await work, and the audit tools' per-sentence LLM loop off the loop
   (`asyncio.to_thread`, per the ruff-ASYNC memory) and route the tool through the
   capped validator; attach a verified Google identity to an unverified pending row
   instead of refusing, and add an admin "release address" action; RFC 5987 `filename*`;
   neutralise the `CALL_BODY` marker; re-validate identity in the SSE loop; paginate
   `list_turns`.
10. **Frontend truthfulness, round two**: invalidate the four keys on 4409; pass
    `onExpired` from CockpitChat; `isError` arm in `Cockpit`; list invalidations on
    create; surface BranchBar errors; fix the two terminal-state labels.
11. **Test hygiene**: `pytest-timeout`; a foreign-workspace fixture for the citation
    404 test; side-effect assertions for the four 2xx-only tests; a lint gate (`oxlint`
    is zero-config and fits the 7-day cooldown); wire `collab.spec.ts` into CI and either
    seed a password user for `dashboard.spec.ts` or delete it and the `cockpit.spec.ts`
    stubs.
12. **Hygiene**: remove the two merged worktrees; move the research-survey files out of
    the app root or into `docs/`; untrack `README.pdf`; return to the precise commit
    titles the history had until 2026-08-31.

---

## 8. Final assessment

The prior review of record said this codebase "knows the right answer and writes it down
at one site." This pass found the same shape again, in new places: the checkpoint commits
whatever the serializer emits with no round-trip check while the parser 60 lines away
would have refused it; the fetcher computes a deadline and then hands httpx a per-read
timeout; the type-settling fix that closed two High findings dropped a column nobody
tested; the design accepts a second process for the scheduler's sake and forgets that
five other singletons live in the same process.

The good news is also measured. The gates that guard authorization, sessions, caps,
fences and the CRDT frame path are real — sixteen mutations, sixteen catches. The OIDC
leg survived a library swap intact. The SSRF and egress boundaries are thorough. A
rewrite remains unwarranted; the work is a short list of ordinary-path bugs in the two
features users touch most, and one afternoon of attaching the project to a remote, a
backup, and its own CI.

---

# Appendices — reviewer reports, verbatim

Each appendix is the full report one reviewer returned, including its probe commands,
mutation lines, outputs, "checked and sound" list, and "not read" list. Line numbers are
against `8f44a35e`.

## Appendix 0 — the HEAD commit (`8f44a35e`, `frontend/e2e/dashboard.spec.ts`)

Reviewed with the repo's diff reviewer before the whole-project pass. 9 candidates, 7
survived verification (5 confirmed, 2 plausible), 2 refuted.

1. **Line 22 — the selector fix is correct but the spec still cannot pass**: nothing runs
   it (CI runs vitest only; `scripts/collab_e2e_run.sh` hard-codes `collab.spec.ts`) and
   no seed creates an active `e2e@umu.se` with password `correct-horse-1`, so the failure
   moved from a strict-mode error at `fill()` to a login-denial timeout at `waitForURL`.
   The gitignored `frontend/test-results/.last-run.json` from five minutes before the
   commit records exactly this: status failed, alert "Invalid email or password."
2. **Line 35 — `getByLabel(/profile/i).selectOption(...).catch(() => {})` is a permanent
   no-op** of the same stale-Mantine-selector class the commit fixes: measured, the locator
   resolves to 3 elements in every state and `selectOption` cannot act on a Mantine
   Select; the blanket `.catch` swallows the strict-mode error, so the axe scan always
   runs against the unfiltered dashboard.
3. **Line 71 — the SPA-navigation test can never run its body**: on a fresh `/` no anchor
   matches `a[href^="/calls/"]` (rows link to `/?selected=N`; the only `/calls/N` anchor
   is in the reading pane, rendered once `?selected=` is set), so `test.skip(count === 0)`
   always fires with a misleading "no seeded calls" message. Stale since `e0980492`.
4. **Line 56 — the logout test can flake to `/login?next=%2F`**: the dashboard's 5 s
   ranking poll can 401 after logout bumps the epoch, and `unwrap`'s `redirectToLogin()`
   then supersedes TitleBar's `assign('/login')`; neither `waitForURL('**/login')` nor
   `toHaveURL(/\/login$/)` matches.
5. **Line 22 — two locator strategies on adjacent fields with no comment**: measured on
   the Mantine markup, `getByLabel(/password/i)` = 2 (the toggle's aria-label),
   `getByLabel('Password', {exact:true})` = 0 (the required asterisk is inside the
   label), `getByLabel(/^password/i)` = 1. A future "harmonising" edit re-triggers either
   trap.
6. **Line 32 — `expect(page.getByRole('heading')).toBeVisible()` does not prove the
   dashboard loaded**: the only heading on `/` is the sidebar's "Filters" h2, which
   renders independently of the dashboard query; a 500 from `/api/dashboard` passes.
7. **Line 23 — the login helper's only failure signal is a 30 s navigation timeout**:
   pending/no-access users, CSRF 403 and bad credentials all surface as "Timeout 30000ms
   exceeded" with no cause.

Refuted: "placeholder/title weakening" (the old locator never resolved the input at all);
"redundant `goto('/')`" (that hard GET is the serving contract the spec exists to test).


## Appendix A1 — prior backend findings re-verified

#### A1 — Prior backend items re-verified against master @ 8f44a35e (2026-09-01)

Tree read-only; probes under scratchpad (`probe_async_sessions.py`, `probe_fence.py`,
`probe_cd.py`, `probe_routers.py`, `probe_trigger.py`, `probe_ws_text.py`,
`probe_enclosing.py`). No mutation-by-line-number was possible (no worktree), so every
"gate" verdict below is READ unless a probe is named.

Summary: **FIXED 5** (3, 17, 20, 21 + bug-2/bug-3 of the branch-leak spec) · **PARTIAL 1** (18) ·
**REFUTED-NOW 1** (12) · **OPEN 21**.

##### Per-item verdicts

| # | Status | Label | Evidence (path:line) | Fix sha |
|---|---|---|---|---|
| 1 | OPEN | RAN | AST probe: **31** `async def`s build a sync Session on the loop (rule: zero-arg call to a name ending `session_factory` or in {sf,_sf,Session,SessionLocal}, nested sync defs/lambdas excluded). `tools/references.py:89 search_references_impl` still calls `embedder.embed(query)` sync at :124 (the file's only threadpool hops are :500/:566, in fetch). `drafting/controller.py:81 launch`: sync lock session :146-147, `reconcile_workspace_git_drift` :167, `reconcile_workspace_citekeys` :175, and `grep to_thread\|run_in_threadpool controller.py` = 0. Prior count was 28; my rule adds `main.py:92`, `files.py:78`, `references.py:90/199/541` (routes that open a session before their threadpool hop) — the delta is rule, not drift. | — |
| 2 | OPEN | READ | `scheduler.py:54-100` SchedulerLock only holds/releases; `try_acquire_scheduler_lock` :102-148; `main.py:233-237` acquires once, :312-313 releases at shutdown. No heartbeat/probe/re-acquire anywhere (`grep scheduler_lock\|heartbeat\|probe main.py scheduler.py`); `health.py:9-27` is DB-only. `git log -S"scheduler_lock_lost"` → nothing. | — |
| 3 | **FIXED** | READ | Spec's two open bugs both closed in **355e8107** (2026-09-01). Bug 2: `frontend/src/collab/useCollabDoc.ts:5` `MAX_DOCUMENT_REPLACEMENTS = 8`, :7 `DOCUMENT_REPLACEMENT_MAX_MS`, :63 cap check; tests inverted `useCollabDoc.test.tsx:86` "bounds repeated unsynced document replacements", :111 "resets the replacement budget after a document sync". Bug 3: `switch_branch` ordering is UNCHANGED (`branches.py:246-251` step-8 UPDATE, :253-263 step-9 checkout, route commits at `web/branches.py:212`) but the read side no longer pairs DB name with worktree bytes: `web/api/workspaces.py:263-276 _lock_artifact_read_snapshot` takes `FOR SHARE` on the workspace row (bounded → 429), so a read waits for an in-flight switch's row UPDATE to commit; then :309/:368 `read_branch_file(repo_dir, branch, …)` (`gitio.py:679-695`) resolves `refs/heads/<branch>` and reads the blob at that commit — name and bytes come from the same ref, never the worktree. Of the four read sites (now :304, :363, :539, :585): 304/363 use lock+ref-read; :539 `_gate` only echoes `branch` after `init_spec_via_kernel` (kernel takes the mutex); :585 `_git` is under `acquire_commit_mutex_at_generation` :596 (switch takes the same mutex `branches.py:181`). Test inverted: `tests/proposal_assist/test_api_branches.py:408 test_artifact_snapshot_stays_on_the_recorded_branch_during_checkout` (the old `test_KNOWN_DEFECT_…` is gone). Earlier commits e312b373/183f8cdd/ad2aaee1/99067e1c present. | 355e8107 |
| 4 | OPEN | RAN | `probe_fence.py`: summary containing `CALL_BODY>>>` → **closing markers = 2, opening = 2**, injected text sits outside the fence. `ranking/prompt.py:148-155` literal markers; `build_messages` :224-232 interpolates title/programme/text with no escaping. `git log -S"CALL_BODY"` shows only the original fbdfa059/f304c05e. | — |
| 5 | OPEN | READ | `web/api/turns.py:404-406` one `require_member` before `StreamingResponse`; generator `_json_turn_stream` :314-357 reopens sessions (:326, :339) but never rechecks user status / `session_epoch` / membership. | — |
| 6 | OPEN | READ | `analysis/pipeline.py:656-660` `mark_failed(s2, job2, error=str(exc))` → `analysis/queue.py:290` stores `error[:2000]` → `web/api/annotations.py:190` `ClaimJobOut(status="failed", error=job.last_error)`. | — |
| 7 | OPEN (gate gap) | RAN | `pyproject.toml:256` still `filterwarnings = ["error::ResourceWarning"]`; `git log -S"PytestUnraisableExceptionWarning"` → nothing. Targeted sample `uv run pytest tests/test_api_turns.py tests/test_analysis_queue.py -q -p no:cacheprovider -W error::pytest.PytestUnraisableExceptionWarning -rw` → **34 passed**, no warnings — not reproduced in this sample; the filter still does not cover the class the whole-project review saw in full runs. | — |
| 8 | OPEN | RAN | `probe_cd.py`: `safe_title("研究")` → `'研究'` (`proposal_assist/export.py:38-40` keeps any `isalnum()`), then `Response(headers={"Content-Disposition": …})` → **UnicodeEncodeError latin-1**; `files._sanitize_filename("研究.pdf")` → `'研究.pdf'` → same error. Sites `web/api/export.py:41`, `:80`; `web/api/files.py:211`. `git log -S"filename*="` → nothing. (Swedish å/ä/ö are Latin-1: no exception, but mojibake in some clients.) | — |
| 9 | OPEN | READ | `web/api/turns.py:226-232` selects every `ChatTurn` by position, no limit; `git log -S"before_position"` → nothing. | — |
| 10 | OPEN | READ | `references/fetcher.py:271-274` `urljoin(current_url, loc)` unguarded; the enclosing `except httpx.RequestError` :296 does not catch `ValueError`. | — |
| 11 | OPEN | READ | `web/api/export.py:59-61` `f"citations.json could not be read: {exc}"` in the 409 envelope; `web/api/references.py:218-219` and :273-274 `except ValueError → 400 str(e)`. (The third pass cited :238; lines moved.) | — |
| 12 | **REFUTED-NOW** | RAN (WebFetch) | MDN *Referrer-Policy › Effect on the Origin header*: "For other requests — such as HTML form submissions … — the user agent sets `Origin` to `null` when the referrer policy is: **`no-referrer`** …"; MDN *Origin* lists "Certain Referrer-Policy values, for requests that use neither GET nor HEAD and aren't made in cors mode (e.g., basic form posts)". The comment at `web/middleware.py:98-103` is therefore **correct**; project memory `referrer_policy_origin_null.md` records the empirical Chromium verification (2026-06-10). The two prior reviews' "comment defect" is the defect. Do not "fix" the comment. | n/a |
| 13 | OPEN | READ | `web/api/settings.py:157` `User.email == body.email`, no `.strip().lower()`; register/login normalize (`auth.py:87`, :147, :162); `db/models.py:64` `String(320)` unique → case-sensitive. | — |
| 14 | OPEN | READ | `proposal_assist/web/activity.py:77` `require_member`; :83-89 maps actor ids → `User.email`; `activity/feed.py:103-105` puts it in `actor`; the non-owner filter :79-80 hides only `member_added/removed` events, not actor emails. | — |
| 15 | OPEN | READ | `web/api/auth.py:90` both limiters consumed before `verify_password` :103; `auth/rate_limit.py:38-55` `allow()` appends (consumes) and there is no refund; `main.py:800-803` email 5/15 min, IP 30/15 min. | — |
| 16 | OPEN | READ | `web/api/auth.py:197-207`: `signer.verify(session)` (cookie carries `ep`, `auth/session_cookie.py:36-37`) → user fetched → epoch bumped with **no** `user.session_epoch != data.session_epoch` comparison (`auth/dependencies.py:44` does it for every other route). | — |
| 17 | **FIXED** | READ | **8c2d4555** (2026-08-30 "feat(caps): the four fan-out endpoints honour the cap they feed"): `web/coherence.py:65-69`, `web/evaluator.py:63-67`, `web/review.py:154-158`, `web/uncited_claims.py:104-108` call `check_global_daily_token_cap` and 429; pass-start rechecks `coherence/pass_runner.py:87`, `evaluator/pass_runner.py:115`, `uncited_claims/pass_runner.py:149`, `review/pass_runner.py:445-449`. Tests: `tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py`, `review/test_web_review.py:111` asserts 429. So the ledger (b95bef49/716ed622) records AND these sites enforce. Not mutation-proved (read-only tree). | 8c2d4555 |
| 18 | PARTIAL | READ | Cap check landed in **36f92cc7** (2026-08-29): `turns/compaction.py:156-172` raises `GlobalTokenCapExceeded`. The input is still unbounded: :175-178 joins every un-summarized turn's full `content` JSON (tool_result payloads included) into one message, no truncation. | 36f92cc7 (cap only) |
| 19 | OPEN | READ | `drafting/claude_adapter.py:93-100` unchanged: DATABASE_URL, SESSION_SECRET, RESEND_API_KEY, SMTP_USER, SMTP_PASSWORD, VINNOVA_API_KEY. `config.py:35` INITIAL_ADMIN_PASSWORD and :39 GOOGLE_CLIENT_SECRET absent; `VINNOVA_API_KEY` has no Settings alias (`grep -rn VINNOVA_API_KEY src` → only the adapter line). `tests/proposal_assist/test_drafting_claude_adapter.py:445` still asserts the 4-key subset. | — |
| 20 | **FIXED** | READ + RAN (enclosing-fn probe) | **ab3c3a3d** (2026-08-30 "fix(review): the last unfenced git-commit sink in production"): `review/pass_runner.py:476` takes `acquire_commit_mutex_at_generation` in `run_review_pass` before `write_and_commit_files` :515. Full enumeration below. | ab3c3a3d |
| 21 | **FIXED** | READ | **9b85df24** (2026-08-31): `citations/gitio.py:40-84 _git_env` sets `GIT_CONFIG_GLOBAL/SYSTEM=/dev/null` (:76-77), `GIT_TERMINAL_PROMPT=0` (:78), author/committer defaults (:81-84); `GIT_CEILING_DIRECTORIES` (:80) from 8ea4e39d; comment :69 "Hermetic, matching phases/setup.py:_git_env". | 9b85df24 |
| 22 | OPEN | READ | `citations/core.py:131-141 _current_head` runs `git rev-parse HEAD` with no `timeout=`; called at :623 inside `_insert_citation_locked` after the mutex at :575 (idempotent path). `gitio.py:226-233` has the bounded twin. | — |
| 23 | OPEN (count moved) | RAN | `probe_routers.py` (same `include_router` list as `create_app`, dev-only `_events` excluded): **114 /api operations (113 HTTP + 1 WS); 24 outside the envelope router (23 HTTP + 1 WS)** — proposals 3, coherence 2, evaluator 3, review 3, versions 2, branches 4, problems 1, uncited_claims 3, activity 1, collab_checkpoint 1, collab_ws 1. Only `web/api/__init__.py:33-36` declares `responses=`. Was 19 of 109; growth = branches (4) + versions. | — |
| 24 | OPEN ×3 | READ | (a) `init_spec_via_kernel` (`citations/collab_cite.py:107`): `grep -rn init_spec_via_kernel tests` → **0**; `tests/test_api_spec.py:232-296` hit the route with the collab kill-switch armed (:18-34), i.e. the `_git` path `workspaces.py:581`, never the kernel path; the evict arm :151-153 is untested. (b) `drafting/factory.py:100` `ports=build_ports(…)`: `tests/fencing` checks presence only (`test_detectors.py:150-156` on a synthetic snippet; `inventory.py:67-72`); no test asserts the value. (c) `SLICE_STATUS`: `test_fence_coverage.py:115-129` and :218-222 check rows→keys only; no keys→rows test. | — |
| 25 | OPEN | RAN | `probe_trigger.py`: never-started `_SafeBackgroundScheduler`, `add_job`, `trigger_job_now` → **returned True, job did not run**. `scheduler.py:1144-1150` checks only scheduler/job None. `main.py:229-230` still says "build the scheduler but do NOT start it (so trigger_job_now / introspection still work)". | — |
| 26 | OPEN | READ | `web/routes/health.py:9-27` `SELECT 1` only. `config.py:178-184` `workspaces_dir` default `var/workspaces` (relative); none of the validators :690-802 names it; `main.py`'s only use is :105; the first `mkdir(parents=True)` is lazy in `phases/setup.py:457`. | — |
| 27 | OPEN | RAN | `proposal_assist/web/collab_ws.py:250` `async for message in websocket.iter_bytes()`; Starlette 1.3.1 `websockets.py:123-128` `receive_bytes` → `message["bytes"]`. `probe_ws_text.py` (same loop) on a TEXT frame → server-side **`KeyError('bytes')`**. The route catches only `WebSocketDisconnect`/`PersistError` (:260-270); `finally` :271-277 still releases the room. | — |
| 28 | OPEN | READ | `db/models.py:2015-2017` and :2070-2072 `reference_id` FKs (SET NULL) without `index=True`; `claim_classifications.__table_args__` :1994-2005 = CHECK + UNIQUE(workspace_id, claim_sha, prompt_version); `user_claim_annotations` = UNIQUE + `ix_user_claim_annotations_ws_status`; `grep alembic/versions` for an index on either table → none. `docs/ARCHITECTURE.md:62,85` agree (2 and 3 indexes, none on reference_id). | — |

##### Item 20 — every `git add` / `git commit` subprocess in `src/`, with its fence

Spawn sites (only two functions ever run these verbs; `grep '"commit"\|"add"' src` + `subprocess.run` grep):

| spawn site | verb | function | fence |
|---|---|---|---|
| `citations/gitio.py:208` | `git add -- <paths>` | `write_and_commit_files` (:140) | none inside — discipline is at the 8 callers |
| `citations/gitio.py:237` | `git commit -q -m` (**no pathspec: whole index**) | same | same |
| `citations/gitio.py:304` | `git reset HEAD -- <paths>` (rollback) | same | same |
| `phases/setup.py:470` | `git add -A` | `_init_repo` (:454) | caller chain: only `_m1` (:364, called :348) at :409, inside `with self.session_factory() as s:` after `acquire_commit_mutex_at_generation(s, …)` :397 and before `s.commit()` → **fenced** |
| `phases/setup.py:471` | `git commit -q -m` | `_init_repo` | same |

`write_and_commit_files` callers (enclosing functions from `probe_enclosing.py`):

| caller | function | mutex in the same function |
|---|---|---|
| `citations/core.py:631` | `_insert_citation_locked` (:550) | `acquire_commit_mutex_at_generation` :575 ✔ |
| `web/api/workspaces.py:615` | `_git` in `post_init_spec` (:581) | :596 ✔ |
| `citations/bibliography.py:627` | `build_bibliography` (:573), only when `commit=True` | **none** (docstring :593-598 says so). Production callers `proposal_assist/export.py:81-88` and `tools/audit.py:214-221` pass `commit=False` → the unfenced branch is **unreachable in production** |
| `citations/human_cite.py:239` | `insert_citation_for_user` (:75) | :117 ✔ |
| `editing/save.py:334` | `save_section` (:165) | :219 ✔ |
| `analysis/insert.py:418` | `insert_from_annotation` (:333) | :365 ✔ |
| `collab/checkpoint.py:212` | `checkpoint_artifact` (:116) | :152 ✔ |
| `review/pass_runner.py:515` | `run_review_pass` (:318) | :476 ✔ (the site the finding named; ab3c3a3d) |

Verdict: every **reachable** `git add`/`git commit` runs under `acquire_commit_mutex_at_generation`; memory's "2 git sites still take NO mutex" is now 0 reachable + 1 dead branch (`build_bibliography(commit=True)`). The commit still carries no pathspec (gitio.py:238) — harmless while the mutex serializes stagers and the :304 rollback unstages on failure. `git branch`/`git checkout` (gitio.py:602/615/622) are out of scope; their callers in `branches.py` take the mutex at :181/:337/:439.

##### How bad today (OPEN items, for re-ranking)

- **1** — one queued Ollama embed inside `search_references` (httpx default 60 s) or a 10 s shared-lease wait in `launch` freezes every HTTP/SSE/WS request on the single loop; any agent turn triggers it. High.
- **2** — a PG failover / idle-kill silently drops the advisory lock; the running scheduler keeps going and a second replica (or a restart racing the old process) can start its own; single-replica deploys only lose detectability. Medium.
- **4** — any upstream funding source can place text outside the fence and bias Gemma/Sonnet ranking; still framed untrusted by the system prompt, so bias not escape. Medium-low.
- **5** — a removed/disabled/logged-out member keeps receiving the running job's chat turns until it terminates (minutes). Low-medium.
- **6** — SQL/driver/URL text from a failed analysis job is shown to every member. Low.
- **7** — test-quality only; the leak gate can pass silently. Low.
- **8** — any member exporting a workspace whose title has a non-Latin-1 char (CJK, en-dash, emoji, "→"), or serving such an attachment, gets a 500. Medium (reproduced user-facing failure).
- **9** — scaling only. Low.
- **10** — a malformed `Location` becomes a `ValueError`; route turns it into a 400 with the raw message (via item 11); agent fetch tool surfaces a tool error. Low.
- **11** — parser/internal text to members. Low.
- **13** — owner typing a mixed-case email gets "could not add collaborator". Low (usability).
- **14** — every member sees every actor's email; PII policy breach in an internal tool. Low-medium.
- **15** — 6th sign-in per email in 15 min denied; 30 sign-ins per NAT IP in 15 min lock a whole site; also a cheap lockout against a known email. Medium-low.
- **16** — an already-revoked cookie can force-logout its user repeatedly (and drop their collab sockets). Low.
- **18** — a workspace whose compaction keeps failing sends an ever-larger single Opus request; bounded only by the daily cap. Low-medium.
- **19** — when INITIAL_ADMIN_PASSWORD / GOOGLE_CLIENT_SECRET are exported in the real process env, the drafting subprocess inherits them; defense-in-depth behind `tools=[]`. Low-medium.
- **22** — a hung `git rev-parse` on the idempotent re-insert path pins the workspace commit mutex; other writers 429. Low.
- **23** — generated TS types lie about 422 for 23 ops. Low.
- **24** — coverage holes only. Low.
- **25** — only the replica that lost the lock: admin "run now" says success, nothing runs. Low.
- **26** — service started from another CWD silently serves an empty workspace tree (every draft 404s, new repos land in CWD) while /healthz stays 200. Medium (ops).
- **27** — a member can kill their own socket with a text frame, one traceback per frame; no leak. Low.
- **28** — seq scans on `references` deletes. Low.

##### Checked and sound
- Branch-leak spec bugs 1-4 all landed (e312b373, 183f8cdd, ad2aaee1, 99067e1c, 355e8107); the read-side fix (row `FOR SHARE` + ref-addressed reads) covers both orderings.
- Four fan-out endpoints + four pass runners now gate on the global daily cap (8c2d4555); compaction gates too (36f92cc7).
- gitio env is hermetic and ceiling-bounded (9b85df24, 8ea4e39d); every reachable git commit sink is mutex-fenced (ab3c3a3d).
- The Referrer-Policy comment is correct per MDN/Fetch; the prior "comment defect" should be added to the refuted list.

##### Prior findings touched
All 28 are prior (third pass §4.3-4.5, §5, §6.3; whole-project review; security/perf review) — statuses above.

##### Not read
`proposal_assist/tools/history.py` bodies (only grepped), `proposal_assist/reconcile.py` git-log path, `references/ingest.py`, migration bodies under `alembic/versions/`, `annotations.py` auth dependency (assumed `require_member` per prior review).

##### Leads (no proof)
- Item 15's IP limiter (30/15 min) behind a hospital NAT is a plausible real lockout at shift change; not measured.
- Item 10 through the agent fetch tool (`tools/references.py:419`) may surface as an uncaught tool exception rather than a clean tool error; not traced.

##### Incidental
- None new beyond item 12 being a review defect rather than a code defect.


## Appendix A2 — prior frontend, data-layer and ops findings re-verified

#### A2 — prior frontend / data-layer / ops items re-verified against master @ 8f44a35e

Main tree read-only; only the allowed `gen_openapi_types.sh` regenerate-then-revert touched it
(`git status --short` empty at the end). Probe scripts: `scratchpad/drift_probe.py`, `scratchpad/trigger_probe.py`.

##### Per-item verdicts

| # | Item | Status | Label | Evidence |
|---|------|--------|-------|----------|
| 1 | base_branch propagation | **PARTIAL** (propagation + types FIXED at ad2aaee1; 422 pin ABSENT) | RAN | `bash scripts/gen_openapi_types.sh && git diff --exit-code frontend/src/api/schema.gen.ts` -> exit 0; reverted; tree clean. Draft save sends it at `frontend/src/api/editor.ts:44`, spec save at `:87`; sole callers `cockpit/DraftPanel.tsx:11`, `cockpit/SpecPanel.tsx:12` through `editor/ArtifactSectionEditor.tsx:206` (`base_branch: branch`). `editor.test.tsx` (8 passed) asserts the body at :56/:110/:120/:168. The 409 mismatch IS pinned (`tests/proposal_assist/test_api_branches.py:348`). The 422 (save WITHOUT base_branch) is pinned by **no test**: an AST scan of every `tests/**/test_*.py` found no test function that posts a section save and asserts 422 (printed `NO test posts a section save and asserts 422`). Required-ness rests solely on `SaveSectionIn.base_branch: str` (`web/api/workspaces.py:166`). |
| 2 | 4409 reconnect: no backoff/ceiling across Y.Doc generations | **FIXED 355e8107** | RAN | `frontend/src/collab/useCollabDoc.ts:5-7` `MAX_DOCUMENT_REPLACEMENTS=8`, base 100 ms, cap 2 s; `:63-77` bounds + backoff, `:86` resets the budget on sync. `pnpm exec vitest run src/collab/useCollabDoc.test.tsx` -> 3 passed incl. `"bounds repeated unsynced document replacements"` (9 sockets then `offline-permanent`) and `"resets the replacement budget after a document sync"`. |
| 3 | Post-save optimistic write reverts server newline canonicalization | **OPEN** | READ | `editor/ArtifactSectionEditor.tsx:224-232` writes `text: content` (client bytes) next to `sha: result.section_sha` (server sha of canonical bytes). Server appends `\n` to a non-final section lacking one (`proposal_assist/editing/save.py:290-291`); `SaveResultOut` carries no text (`web/api/workspaces.py:169-175`). Ordering makes it a strict regression: `useSaveSection.onSuccess` (`api/editor.ts:48`) invalidates, TanStack 5.101.0 awaits `onSuccess` and `invalidateQueries` awaits the active refetch, so the canonical bytes have already landed when `mutateAsync` resolves — then `setQueryData` overwrites them. The existing test (`ArtifactSectionEditor.test.tsx:137-186`) uses `"target branch\n"` (already canonical) so cannot see it. Prior citation `:173` is now `:224`. |
| 4 | §4.9 give-up shown as "reconnecting…", §4.10 Save-a-version not invalidating Versions | **FIXED 2d2cd3aa** (both) | RAN | 4.9: `collabProvider.ts:294` sets `offline-permanent`; `collabStatus.ts:9` -> `collab-disconnected`; `shell/StatusBar.tsx:22` label "Disconnected — reload to reconnect". Tests: `collabProvider.test.ts:490` `"the status becomes terminal once retries are exhausted"`, `:174` `"gives up after maxReconnectAttempts consecutive failures (no infinite loop)"` (28 passed). 4.10: `api/invalidateArtifact.ts:36-40` invalidates `[artifact,id]`, `["versions",id]`, `["branches",id]`; `collab/useSaveVersion.ts:18` uses it. Tests `api/invalidateArtifact.test.ts:9` `"refreshes the artifact, version list, and branch tips"`, `:23` `"uses the versions PREFIX so both artifacts' lists refresh"`, `:60` `"every artifact invalidation goes through the helper"` (source-scan gate) — 3 passed. |
| 5 | No frontend lint gate | **OPEN** | RAN | `ls frontend/eslint* frontend/.eslintrc*` -> none; `grep eslint\|oxlint\|biome frontend/package.json` -> none; scripts are `dev/build/test/gen:api` only (`package.json:9-14`). CI runs vitest (`ci.yml:74`) and type-checks only via the Docker build (`frontend/Dockerfile:27` `pnpm run build` = `tsc && vite build`, `tsconfig:8-10` strict + noUnusedLocals/Parameters). 5 dead `eslint-disable` directives in `frontend/src` (3 for `react-hooks/exhaustive-deps`: `collab/useCollabDoc.ts:118`, `editor/CodeMirrorEditor.tsx`, `cockpit/CitationsPanel.tsx`) — a rule that has never run here. **Cheapest gate:** one devDep `oxlint` (zero config, bundles react-hooks + typescript rules), `"lint": "oxlint --deny-warnings src"` + one CI step after vitest. Constraint: `frontend/pnpm-workspace.yaml` `minimumReleaseAge: 10080` — pin a version >= 7 days old. Eslint flat config + typescript-eslint + eslint-plugin-react-hooks is the 3-4 dep alternative. |
| 6 | Playwright e2e wiring; cockpit.spec.ts | **OPEN** (confirmed; cockpit is inert) | RAN | Only `collab.spec.ts` is wired (`scripts/collab_e2e_run.sh:172`); `ci.yml` has no playwright step; no `e2e` script in `package.json`. `pnpm exec playwright test e2e/cockpit.spec.ts e2e/dashboard.spec.ts --list` -> 6 tests: cockpit 2 (both `test.skip` stubs, `cockpit.spec.ts:8,13`, header says "enabled in SP2a-4" — SP2a-4 merged and never enabled them; file dated Jun 22), dashboard 4. `dashboard.spec.ts:21-23` logs in as `e2e@umu.se`/`correct-horse-1`; `scripts/collab_e2e_seed.py` creates no password user (grep `password` -> none; it emits cookies only). HEAD 8f44a35e edited dashboard.spec.ts's password selector — a change to a test nothing can run. cockpit.spec.ts cannot fail (always skipped) and cannot pass meaningfully; dashboard.spec.ts cannot pass. |
| 7 | Data-layer drift (3 indexes incl. HNSW; no gate) | **OPEN — MEASURED** | RAN | `scratchpad/drift_probe.py`: `PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")`, `command.upgrade(cfg,"head")` -> `alembic_version: 0068`, 125 live indexes in `public`; `compare_metadata(MigrationContext, Base.metadata)` -> **DIFF OPS: 3**, all `remove_index`, no other op kind: (1) `reference_chunks.ix_reference_chunks_ws_embedding` cols=[embedding] `using='hnsw'` ops `{'embedding':'vector_cosine_ops'}` — created by raw SQL `alembic/versions/0031_references.py:111`; (2) `reference_chunks.ix_reference_chunks_ws_model` cols=[workspace_id, embedding_model] — `0032_reference_chunks_workspace_index.py:26`; (3) `references.ix_references_indexing_backlog` cols=[workspace_id, created_at] `where="((text_status = ANY (ARRAY['pending','running'])) OR (outline_status = ANY (ARRAY['pending','running'])))"` — `0031_references.py:80`. Zero add/modify ops: columns, FKs, constraints, every other index are in parity. ORM side: `db/models.py:1754-1791` `Reference.__table_args__` = 6 CheckConstraints + `uq_references_workspace_citekey`; `:1851-1853` `ReferenceChunk.__table_args__` = `uq_reference_chunks_ref_idx` only. Gate: `grep -rln "compare_metadata\|autogenerate" tests/` -> **nothing** (only comments at `db/models.py:149,158,262`, `db/__init__.py:5`). |
| 8 | Alembic: heads, pass-only downgrades, boot migrations, /healthz revision | **PARTIAL** (single head OK; rest OPEN) | RAN | 68 migration files (not 69; `alembic/versions/` holds only `0001..0068` + `__pycache__`); `uv run alembic heads` -> `0068 (head)`. AST scan: 3 pass-only downgrades — `0010_profile_embed_text_v2.py`, `0037_backfill_reference_original_sha256.py`, `0045_null_profile_embeddings.py` (all data backfills; 0010's docstring calls the no-op deliberate). No migration at boot: no `alembic`/`command.upgrade` in `main.py`; README:109 and :201 make it manual. `/healthz` (`web/routes/health.py:19-27`) is `SELECT 1` only; `grep alembic_version\|ScriptDirectory\|MigrationContext src/` -> none. |
| 9 | Scheduler lock ops surface | **OPEN** (+ one new twist) | RAN | No heartbeat/readiness (`grep heartbeat scheduler.py main.py` -> none; the only "heartbeat" is the per-job touch in `analysis/pipeline.py:205`). Without the lock at boot: one WARNING `scheduler.lock_not_acquired` (`main.py:239-251`), scheduler built `start=False`, `/healthz` stays 200. **New:** `POST /admin/digest/send` and `/admin/ingest/{slug}` (`web/api/admin.py:388-393, :421`) call `trigger_job_now`, which on a never-started scheduler returns True and the job never runs — probe `scratchpad/trigger_probe.py`: `running: False / trigger_job_now -> True / job actually ran: False` (`scheduler.py:1144-1150` checks only job existence; pre-start jobs sit in `_pending_jobs`). Lock lost after boot (PG restart): `SchedulerLock` (`scheduler.py:54-98`) has only `__init__`/`release`, nothing re-checks; the scheduler keeps running with no signal (harmless in a one-process deploy). |
| 10 | WORKSPACES_DIR default + README deployment section | **PARTIAL** | READ | Default still `"var/workspaces"` CWD-relative (`config.py:178-184`); consumed as `Path(settings.workspaces_dir)` with no resolve/is_absolute/exists check (`main.py:105`, `scheduler.py:757,1082,1093`, `drafting/controller.py:70`, `web/_file_uploads.py:49`). README covers one process (`README.md:31-33, :197`), `SPA_MODE=prod` (`:196`), migrations (`:201`), and "Keep the database and `WORKSPACES_DIR` on persistent storage" (`:205`). README does NOT say the path must be absolute / is CWD-relative, how to back it up (git repos + attachments, consistency with the DB), or that it is confidential data. `.env.example:106` ships the relative value. |
| 11 | .env.example vs config.py aliases | **OPEN** (count moved 54 -> 57) | RAN | Settings has 113 aliases; `.env.example` names 56; **57 missing**. None is required (all default). Day-one set: **`SPA_MODE`** (default `disabled` -> a prod boot from a copied example serves NO UI; README:196 says set it, the example never mentions it; `config.py:688`); **`ADMIN_DASHBOARD_URL`** (default `http://localhost:8000/`, used verbatim as the link in every digest email `digest/send.py:102-103`, NOT derived from `APP_BASE_URL` — the validator only appends a slash `config.py:690-693`); **`SESSION_MAX_AGE_SECONDS`** (30 d; security policy); **`COLLAB_KILL_SWITCH`** (the ops kill switch, undiscoverable); cost knobs `COHERENCE/EVALUATOR/UNCITED_CLAIMS_DAILY_TOKEN_CAP` (200k each), `REVIEW_DOLLAR_CAP_DEFAULT` (5.000), `REVIEW_RUNS_PER_DAY_DEFAULT` (5), `REVIEW_PRICE_INPUT/OUTPUT_PER_MTOK` (hard-coded price assumptions). `RETENTION_INSPECTION_ENABLED`, `DIGEST_DRY_RUN` default off (safe). The remaining ~40 are tick intervals/timeouts/pool sizes. |
| 12 | api_key.txt 0660 + two .orig files | **REFUTED-NOW (worse): the .orig files are TRACKED** | RAN | `git ls-files | grep .orig` -> both; added in **355e8107** (Sep 1: `+292` collabProvider.ts.orig, `+968` room.py.orig). Content: `room.py.orig` is byte-identical to `room.py` at 957f2343 (pre-4409, `_WS_TRY_AGAIN_LATER = 1013`); `collabProvider.ts.orig` matches no historical revision but differs from the current file only by the missing 10-line 4409 branch (`collabProvider.ts:267-275`). **Neither holds anything not already in tree/history** — both are stale pre-fix copies now committed under `src/` and `frontend/src/`. `api_key.txt`: 108 bytes, mode 0660 (group-readable), gitignored (`.gitignore:37`), **no reader** in `src/`, `scripts/`, README or `.env.example` — an orphan credential file (not read). |
| 13 | Problems panel hides signal failures | **PARTIAL** (10718188 fixed the split basis, not this) | READ | `problems/aggregate.py:106-159` wraps the 5 signals; only the corrupt/unreadable ledger becomes a row (`:113-121` `ledger_unreadable_problem`). Every other failure is `logger.exception` + swallowed: draft read `:89-94`, broken_citation generic `:115-116`, criterion_gap `:133-134`, over_length `:139-140`, coherence_drift `:147-148`, uncited_claim `:158-159`. Response `:170-175` has only `problems/counts/scorecard_stale/uncited_claims_stale` — no failed-signal field. A draft-read failure does set `draft_sha=""`, flipping the "may be outdated" flags (`cockpit/ProblemsPanel.tsx:92-97`) — the only visible degradation. `ProblemsPanel.tsx:83-86` renders "No problems detected." on an empty list; tests (`ProblemsPanel.test.tsx:40,46,70,76`) cover empty/rows/HTTP-error/uncited, none a failed signal. |
| 14 | Citation-LLM egress gate (c67109a8) | **FIXED c67109a8** | RAN | `uv run pytest tests/test_citation_cloud_egress_gate.py -q` -> 10 passed. Every path to the citation LLM and its gate: (a) `POST /citation-audit/deep` `web/api/citation_audit.py:139-153` — when provider egresses: 503 `cloud_llm_disabled` if `!drafting_enabled`, 403 `disclosure_not_acknowledged` without per-user ack; owner-only `:135`. (b) upload `web/api/references.py:112`, (c) second upload path `:225`, (d) reprocess `:558` — all through `_citation_service_if_permitted` (`:60-88`) -> `citation_cloud_permitted` (`proposal_assist/web/_cloud_gate.py:54-64`: provider, `drafting_enabled`, per-user ack) -> degrade to `None` + `citation_cloud_egress_withheld` log. (e) unattended claim-analysis ticks `scheduler.py:1106-1120` (-> `analysis/pipeline.py:400,607`) — gated by `settings.drafting_enabled` and the boot-time refusal `CITATION_LLM_ALLOW_CLOUD` (`citations/llm.py:80-90`: build fails -> app will not boot). No per-user consent there by design (`config.py:526`); the owner-only reanalyze endpoint (`web/api/annotations.py:435-468`, no disclosure check) enqueues into it. (f) agent tools inside a drafting turn: `drafting/factory.py:86-98` builds its own service into `ToolContext` -> `tools/audit.py:92`, `tools/references.py:550`; covered by the turn gate `web/api/turns.py:113` `gate_cloud_llm` (409/503/403); `review.py:151`, `coherence.py:39`, `evaluator.py:41`, `uncited_claims.py:97` gate their own runs. |
| 15 | /healthz, deploy runner, `--workers` guard | **PARTIAL** | READ | README:182-206: manual `alembic upgrade head` then `uv run uvicorn call_manager.main:create_app --factory --host 127.0.0.1 --port 8000` behind a TLS proxy; no systemd unit, no app Dockerfile (`docker-compose.yml` has only `db`); README:31-33/:197 say one process, no workers. **No code guard** for `--workers>1` (`grep WEB_CONCURRENCY\|--workers src/` -> none). The advisory lock (`main.py:236-251`) only makes extra workers skip the scheduler with a warning. In-process state that WOULD split across workers: `MANAGER = RoomManager(...)` module singleton (`proposal_assist/collab/room.py:971`), `SlidingWindowLimiter` x2 (`main.py:800-803`), `IngestLimiter` (`:829`). |

##### How bad each OPEN/PARTIAL item is today

- **1 (422 pin):** low — a future `base_branch: str = "main"` default would silently reopen the mixed-branch write and nothing red would say so.
- **3:** low/cosmetic — after every save the cache holds text != its sha until the next refetch; the buffer is dropped so no false "dirty"; a re-save of identical text short-circuits server-side (`changed=False`).
- **5:** process risk — the one rule the code assumes (`react-hooks/exhaustive-deps`, 3 disable comments) has never run; a dependency-array bug ships unless a human spots it.
- **6:** no e2e coverage of dashboard/cockpit; cockpit.spec.ts is inert scaffolding (always skipped); dashboard.spec.ts can only be run by someone who hand-seeds a password user.
- **7:** whoever next runs `alembic revision --autogenerate` gets a migration that **DROPs the HNSW index** (semantic reference search becomes a sequential scan over `reference_chunks`) plus the backlog index; nothing in CI would catch the drift or the drop.
- **8:** a deploy that forgets `alembic upgrade head` boots, reports 200 on `/healthz`, and 500s on the first query that touches a new column.
- **9:** a replica that lost/never got the lock shows green `/healthz` and answers the admin "send digest now" with `{"triggered": true}` while nothing runs; the only evidence is one WARNING at boot.
- **10:** a systemd unit with a different `WorkingDirectory` silently creates a second, empty workspace root; every workspace reports "no proposal file" until noticed; no boot-time detection.
- **11:** an operator who copies `.env.example` into prod gets no UI (`SPA_MODE`) and digest emails that link to `http://localhost:8000/`.
- **12:** a 968-line stale copy of `room.py` and a stale `collabProvider.ts` are committed in the source tree; `api_key.txt` is a group-readable secret with no consumer.
- **13:** a DB/runtime error inside criterion_gap or uncited_claim renders a clean "No problems detected." panel.
- **15:** `--workers 2` boots and looks healthy; two collaborators on the same artifact can land on different processes and edit two different in-memory Y.Docs of one room (what the persisted log does then is a lead, not measured).

##### Incidental (new things tripped over)

1. **Admin trigger lies on a lockless replica** — `trigger_job_now` returns True for a never-started scheduler (RAN probe above); `web/api/admin.py:388-393,:421` surface that as `{"triggered": true}`. Fix shape: `if not scheduler.running: return False` (or 503) in `scheduler.py:1144`.
2. **The two `.orig` files are committed** (355e8107), contradicting both 2026-09-01 reviews ("untracked"). `git rm` both; consider a `*.orig` line in `.gitignore` (none today).
3. **`GET /workspaces/{id}/problems` has no `response_model`** (`proposal_assist/web/problems.py:23-31` returns `-> dict`), so `schema.gen.ts` carries no shape for it and the SPA uses a hand-written type (`frontend/src/api/problems.ts:24`). The OpenAPI drift gate cannot see this endpoint; a field rename would pass CI and break the panel at runtime.
4. **5 dead `eslint-disable` directives** with no linter (item 5).
5. **Prior-review counts drifted:** alembic files 69 -> 68 (RAN); missing env aliases 54 -> 57 (RAN).
6. `cockpit.spec.ts` header promises enablement "in SP2a-4"; SP2a-4 merged and it never happened.

##### Checked and sound

- `schema.gen.ts` regenerates byte-identical (item 1).
- Both save paths and their only two callers send `base_branch`; `editor.test.tsx` pins the bodies.
- 4409 replacement loop is bounded (8 generations, backoff, reset-on-sync) — 3 tests green.
- Give-up state is truthful end to end (provider -> collabStatus -> StatusBar label) — 28 tests green.
- Versions/branches invalidation goes through one helper with a source-scan gate — 3 tests green.
- Single alembic head 0068; the 3 pass-only downgrades are data backfills.
- Type-check IS a CI gate (strict tsc via the Docker `pnpm run build`).
- All six citation-LLM egress paths are gated or covered by the turn gate — 10 tests green.
- Columns/FKs/constraints/all other indexes are in ORM<->migration parity (only the 3 indexes drift).

##### Prior findings touched (status only)

- Third-pass §4.9 / §4.10 — fixed at 2d2cd3aa. §5 "post-save optimistic write" — still open (:224). §5 "three live indexes / no drift gate" — still open, now measured (exactly those 3). §5 "problems panel clean bill" — still open. §5 "lost scheduler lock / healthz" — still open (+ admin-trigger twist). §5 "healthz DB only" — still open. §5 "WORKSPACES_DIR CWD-relative" — still open. P2-4 "no frontend lint gate" — still open. §8 contested citation-egress refutation — overturned and fixed at c67109a8 (confirmed).
- Whole-project review: base_branch tsc errors — fixed (ad2aaee1 + follow-ups); api_key.txt 0660 — unchanged; ".orig untracked" — wrong now, they are tracked.
- Security/perf review: 0065 non-restorative downgrade — not re-examined (out of lens).

##### Not read

`frontend/e2e/collab.spec.ts` body; migration bodies other than `downgrade()` and the index-creating lines of 0031/0032; `references/ingest.py` internals; the Yjs persistence path under multiple processes; `docs/ARCHITECTURE.md` beyond the index section.

##### Leads (no proof)

- Multi-worker collab divergence (item 15): two `RoomManager`s persisting the same artifact's Yjs log — needs a two-process run to see whether the log append fences it or interleaves.
- `room.py.orig` under `src/call_manager/` may be packaged into the wheel by `uv build` (not measured).
- Item 3 has no RAN proof; a vitest with a mocked save returning a sha for `content+"\n"` and a refetch that serves the canonical text would demonstrate the overwrite.

##### Final tree state

`git -C /home/parlar_ai/dev/call-monitor status --short` -> empty (verified after the schema.gen.ts revert and again at the end).


## Appendix A3 — HTTP API layer

#### A3 — HTTP API layer review (web/api, proposal_assist/web, dependencies, middleware, main.py handlers)

Base: master 8f44a35e, worktree `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-aaa4798bfc947e1dd`
(venv verified to import the worktree). All RAN evidence came from a temporary
`tests/_probe_a3_web_api.py` (8 probes, deleted afterwards) and two line-numbered
mutations, each reverted with `git checkout --`. `git status --short` is empty.

##### 1. Ranked findings

###### F1 — Medium — `src/call_manager/web/api/references.py:271-272` — every fetch failure is reported to the user as "internal server error"
`api_fetch_reference` maps `FetchError` (DNS failure, non-2xx from the DOI/URL host,
redirect loop, fetch budget — all caused by the string the user typed) to
`HTTPException(502, "fetch failed: …")`, but `main.py:515` rewrites every ≥500 `/api`
response to the fixed `{"code":"internal_error","message":"internal server error"}`. The
message written for the user never reaches the wire, and the HTTPException path is not
logged either, so the reason is lost on both sides. The codebase already knows this
trap: `turns.py:118-125` remaps its 503 to 429 "because the >=500 handler sanitizes",
and `collab_checkpoint.py:56-80` documents the same 503 holdout — this site was missed.
Scenario: a member pastes a DOI whose landing host is down → UI: "internal server error".
**RAN** (probe 1, `dispatch_fetch` monkeypatched to raise `FetchError("DNS resolution
failed for 'doi.org' …")`):
`POST /api/workspaces/{id}/references/fetch` → `502 {"error":{"code":"internal_error","message":"internal server error"}}`; the string "fetch failed" is absent.
Fix shape: answer a 4xx (`422 fetch_failed` with a sanitized reason, like the SSRF arm at
:269) or emit `api_error(502, "fetch_failed", …)` directly, which bypasses the sanitizer
(as `citation_audit_deep` already does for its 503).

###### F2 — Medium — `src/call_manager/web/api/rubric.py:113` (+`:131`) — `NaN`/`Infinity` weights pass validation and 500 under the commit mutex
`_validate_criteria` only rejects `weight < 0`. `NaN < 0` and `inf < 0` are both False;
Starlette's `json.loads` accepts the `NaN`/`Infinity` tokens and pydantic's default
`allow_inf_nan=True` admits them into `CriterionIn.weight: float`. `_apply_rubric` then
assigns `ws.rubric_criteria = criteria` inside `workspace_write` (mutex held, row
`FOR UPDATE`), the JSONB write fails, and the catch-all returns a sanitized 500. Same path
on `POST /activate`. Owner-only, so a self-inflicted 500 rather than a security hole — but
it holds the mutex and bypasses the route's own "400 before any lock" rule (`rubric.py:207-210`).
**RAN** (probe 2, raw JSON body, `raise_server_exceptions=False`):
`PUT /rubric` `"weight":NaN` → 500 `internal_error`; `"weight":Infinity` → 500;
`"weight":-Infinity` → `400 "criterion 0: weight must be >= 0"` (proves the `< 0` test is the only gate).
The failing layer (DB rejecting NaN in json) is READ, the 500 is measured.
Fix shape: `weight: float = Field(ge=0, allow_inf_nan=False)` (and `scale_max: int = Field(ge=1, le=10)`) so it is a 422 at the boundary.

###### F3 — Medium/Low — `src/call_manager/proposal_assist/references/fetcher.py:117` via `web/api/references.py:269-270` — SSRF refusal echoes the resolved internal IP to any member
The SSRF guard resolves the hostname with the app's resolver and, on a private answer,
raises `SSRFError(f"refused to connect to {host!r} → private IP {addr!r}")`; the route
returns `400 str(e)` to any workspace member. A member can submit
`http://<hostname>/x.pdf` for any name on the hospital network and read back the address
the server resolved — an internal-DNS oracle that uses the app's split-horizon view, not
the member's.
**RAN** (probe 8, `fetcher.socket.getaddrinfo` monkeypatched to return 10.31.7.42, member client):
`POST …/references/fetch {"input":"http://pacs-archive.hospital.internal/report.pdf"}` →
`400 {"error":{"code":"bad_request","message":"refused to connect to 'pacs-archive.hospital.internal' → private IP '10.31.7.42'"}}`.
Fix shape: keep the host, drop the address from the client-facing message (log it server-side).

###### F4 — Low — `src/call_manager/web/api/settings.py:160,172` — `add_member` is a registered-email oracle and accepts pending/disabled accounts
The comment at :159 says "Generic failure (no 'no such user' oracle)", but only the
failure side is generic: a registered email answers 201 with `{user_id, email}`, an
unregistered one 400. Any active user can create a workspace (`POST /workspaces` needs
only a call id) and probe. There is also no status check, so a `pending` (never approved)
or `disabled` account is added as a member (row + `member_added` event carrying its email,
shown in `list_members`). This defeats the anti-enumeration `auth.py` builds for
register/login, for any authenticated user.
**RAN** (probe 3, owner client):
`stranger@umu.se` (exists) → `201 {"user_id":3,"email":"stranger@umu.se","role":"member"}`;
`pending@umu.se` (status=pending) → `201`; `nobody@umu.se` → `400 "could not add collaborator"`.
Fix shape: uniform 202 "invitation recorded" regardless of existence, or restrict to `status == "active"` and still answer uniformly. Low: internal tool with a domain allowlist.

###### F5 — Low — `src/call_manager/web/api/me.py:221` — unbounded `profile_ids` list → 500 at 65,536 elements
`SubscriptionsIn.profile_ids: list[int]` has no `max_length`; `Profile.id.in_(desired)`
renders one bind parameter per element, and psycopg refuses more than 65,535 → 
`sqlalchemy.exc.OperationalError: number of parameters must be between 0 and 65535`,
which `main.py:658`'s OperationalError handler logs at ERROR with a full traceback and
answers 500. The body cap is 64 MiB (`config.py:677`), so ~9M ids fit in one request.
**RAN** (probe 4, 69,999 ids): `PUT /api/me/subscriptions` → `500 internal_error`;
traceback shows `OperationalError … number of parameters must be between 0 and 65535`.
Same shape (no `max_length`) on `SubmitTurnIn.reference_ids/attachment_ids` (capped later
by `MAX_TURN_INCLUSIONS` → 400 — READ) and `MetadataIn.authors` (joined to text, Text
columns, no failure — READ).
Fix shape: `Field(max_length=…)` on the list (there are never more than a few dozen profiles) → 422.

###### F6 — Low — hollow gate — `tests/test_api_workspaces.py:1123` `test_post_citation_foreign_reference_is_404` does not test a foreign reference
The test passes `uuid.uuid4()` — a *nonexistent* reference — so it exercises the
`ref is None` branch of `human_cite.py:157` and never the cross-workspace predicate it is
named for.
**RAN** mutation: `sed -i '157s/ref.workspace_id != workspace.id or //' src/call_manager/proposal_assist/citations/human_cite.py`
→ `uv run pytest tests/proposal_assist/citations/test_human_cite.py tests/test_api_workspaces.py -q` → **78 passed** (stays green); reverted.
Consequence today is bounded: the chunk lookup at `human_cite.py:166-168` is scoped
`ReferenceChunk.workspace_id == workspace.id`, so a foreign reference would still be
refused (as `422 CiteInvalid`, not 404) — but the guard the test names is unpinned on the
flag-off `POST /citations` path. (Kernel path `core.py:382` was not mutated.)
Fix shape: seed a reference in a second workspace, as `test_citation_source_cross_workspace_reference_is_404` (:845) does, and assert 404.

###### F7 — Low — `src/call_manager/web/api/workspaces.py:392,404` (`get_spec`) and `:781` (`post_lock_spec`) — the locker's email is served to every member
`_locker_email` (:279) resolves `spec_locked_by` to an email and `SpecOut.locked_by`
returns it on a member-gated read. The locker is always the owner (`require_owner` on
lock), and `list_members` (:1283-1294) is owner-gated precisely so member/owner emails
stay with the owner; the 2026-08-29 review recorded the same policy breach for the
activity feed — this is a second surface.
**RAN** (probe 7): owner locks; member `GET /spec` → `200 … "locked_by":"owner@umu.se"`; the
same member `GET /members` → 403.
Fix shape: return `locked_by_me: bool`/a display name, or gate the email on `is_owner`.

###### F8 — Low — `src/call_manager/web/api/workspaces.py:1552` — `create_workspace_api` accepts an archived profile
The check is existence only; every other profile consumer filters `is_active`
(`dashboard.py:149-155 set_default_profile`, `calls.py get_call_detail`, `me.py:221`).
Phase-0 then snapshots the archived profile's description as the rubric seed
(`phases/setup.py:446-449`).
**RAN** (probe 5): profile with `is_active=False` → `POST /api/workspaces` → 201 and the
row's `profile_id` is the archived id.
Fix shape: add `Profile.is_active.is_(True)` to the lookup → 400.

###### F9 — Low — `src/call_manager/proposal_assist/web/uncited_claims.py:165-177` — `dismiss_uncited_claim` stores an arbitrary path segment as an audit event
No shape check (claim hashes are hex), no existence check against the latest job's
claims, no bound beyond the URL line limit; any member can append unlimited junk
`uncited_claim_dismissed` rows, each also rendered as a "dismissal" row by the activity feed.
**RAN** (probe 6, member client): a 4,011-char non-hash → 200, stored verbatim in `payload.claim_hash`.
Fix shape: `^[0-9a-f]{16,64}$` on the path param and/or require membership in the latest completed job's claim hashes.

###### F10 — Low — READ — `src/call_manager/web/api/annotations.py:455` vs `:118-128` — `reanalyze_section` indexes the disk document while the Claims tab it serves indexes the live room
`get_annotations` sectionizes the room-canonical document once the collab room has
authored updates; `reanalyze_section` always does `split_sections(proposal.read_text())`
(git working tree). With collab on and an un-checkpointed edit that adds or removes a
section, index `n` from the tab lands on a different disk section: the job is enqueued
with that section's sha (or 404 when the live doc is longer), `latest_job.get(sec.sha)`
on the live sha never shows "pending", and the analysis targets stale text. Same "one
feature judging two documents" class as the Problems-panel fix (10718188).
`tests/proposal_assist/test_api_annotations_reanalyze.py` has four tests, all disk-only.
Fix shape: pick `sections` through the same `is_collab_enabled and has_authored_updates` branch `get_annotations` uses.

##### 2. Checked and sound
- **Body IDs vs path workspace**: `AddCitationIn.reference_id` → `human_cite.py:157` (flag-off) and `core.py:382` (kernel) both check `ref.workspace_id`; `SubmitTurnIn.attachment_ids/reference_ids` → `turns/submit.py:25-31` WHERE workspace_id, ValueError → 400; `remove_member` is a scoped DELETE; `proposals/accepting.py:44-63 _load_scoped` fetches by id AND workspace; `review_file` checks `run.workspace_id`; `get_reference_chunks`, `api_serve_reference_raw`, `api_serve_file`, `get_citation_source` all 404 cross-workspace.
- **Body sha strings never reach git argv**: `SaveSectionIn.base_commit_sha`/`AddCitationIn.base_commit_sha` are only compared (`save.py:283`, `human_cite.py:138`); `versions.read_version` gates on `is_sha` before `blob_sha`; `BranchIn.from_sha` is `is_sha` + `commit_exists` before `git branch` (`branches.py:386-396`, comment names the `--output=` vector); branch names pass `is_valid_branch_name`, delete/switch call `branch_exists` first.
- **Soft-deleted reachability through `require_member` IS pinned** — mutation `_perms.py:31` (drop `soft_deleted_at.is_(None)`) → `tests/test_workspace_restore_acceptance.py::test_the_owner_can_open_the_workspace_again_after_restore` FAILED (1 failed, 63 passed across 6 lifecycle/API files). Note: none of `test_api_workspaces.py`, `test_api_rubric.py`, `test_api_references.py` catch it themselves (99 passed under the same mutation) — one acceptance test is the only pin.
- **Job error fields to members** (`coherence/evaluator/uncited _serialize`, `review error_message`): `_fail` messages are fixed strings ("Coherence check failed.", "timed out", cap messages); review consolidation gets `f"workspace moved on before finalize: {reason}"` with a fence-exception str — no SQL/paths.
- `delete_or_tombstone_reference` commits internally (`deletion.py:113,148`); the route's missing `session.commit()` is correct.
- Negative `index`/`section_index`: `save.py:275`, `human_cite.py:135` bounds-check → 404/409/IndexError→404; no Python negative indexing.
- Enum-like strings are validated in Python before the DB (`status`, `citation_style`, `kind`, `role`, `stance`, `artifact` Literal, `ArtifactParam`) — no CHECK-constraint 500 found.
- Exception text returned via `str(e)`: `CiteStale/CiteInvalid/CiteRefNotFound`, `SelectionConflictError`, `SectionStructureError`, `ProposedChangeNotPending`, `InsertConflict`, `WeakPassword`, `TokenCapExceeded`, `Branch*` — all user-facing sentences (only `CiteInvalid("citations.json is invalid: {exc}")` carries a JSON decode message, owner-only).
- Mutex/lock discipline on the six `workspace_write` routes; `post_lock/unlock_spec` re-read under the mutex; `api_approve_metadata` double-23505 guard; admin last-admin guard takes row locks; `soft_delete_workspace_api` commit→retire→204 order.
- `get_current_user`: status gate before epoch; `AccountInactive` handler clears the cookie; `require_admin` composes it.
- Middleware: body-cap early rejects carry the full security-header set; conflicting `Content-Length` → 400; CSP `script-src 'self'` in prod.
- Unmatched `/api/*` → envelope 404 (`mount_spa` registered last).

##### 3. Prior findings touched (status only)
- `list_turns` unbounded (`turns.py:228-235`) — still open.
- SSE stream authorizes once (`turns.py:394-406`) — still open.
- `add_member` case-sensitive email (`settings.py:157`) — still open (F4 is a different defect on the same route).
- Activity feed emails to non-owner members (`activity.py:82-88`) — still open; F7 is a second surface.
- Claim-analysis raw `last_error` to members (`annotations.py:190`) — still open.
- Broad `except ValueError` → 400 (`references.py:273`) — still open.
- 19/109 ops outside the envelope router (`main.py:841-852`, proposal_assist routers carry their own `/api/workspaces` prefix) — still open.
- `export_composed` `str(exc)` in 409 (`export.py:63`) — still open.
- Content-Disposition not RFC 5987 (`files.py:207`, `export.py:39`) — still open.
- `Referrer-Policy` comment (`middleware.py:96`) — still open (comment only).
- Citation cloud egress ungated — fixed at c67109a8 (`citation_audit.py:131-146` gates on kill-switch + disclosure).
- Admin password-reset per-route rate limit (`admin_users.py:255`) — still open.

##### 4. Not read
- `web/dashboard.py`, `web/call_detail_view.py`, `web/filters.py` (only `api/calls.py` on top of them).
- `web/spa_serving.py`, `web/spa_routes.py`, `web/routes/*` (health, oauth_google), `web/client_ip.py`.
- `proposal_assist/web/collab_ws.py` read in full, but `collab/room.py`, `collab/ws_auth.py` beyond `resolve_access` not opened.
- `main.py:1-500` (lifespan/scheduler wiring) — only the handler block (:511-767) and router wiring (:839-869).

##### 5. Leads (no proof)
- `admin_users_list`/`admin_pending_list` `page: int = 1` has no upper bound; `(page-1)*50` beyond int8 range should fail in PG → 500 (admin-only, self-inflicted; not run).
- `submit_turn` member "ask" (`turns.py:105-110`): spec §7 allows it, but `list_turns` reports `disabled_reason="not_owner"` and `can_send=False` to the same member, and `cancel_turn` is owner-only so a member cannot cancel their own ask — read-model/UI mismatch.
- `get_citation_source` computes "i of N"/prev/next from `proposal_path.read_text()` (disk) while `get_draft` may serve the room-canonical doc — same class as F10, unmeasured.
- `branches.list_branches_route` maps `CommitMutexTimeout` → 409 without `Retry-After` while every other route uses 429 + `Retry-After: 2` (`_STATUS` calls it deliberate).
- `_parse_deadline` accepts naive datetimes into a timezone-aware column — interpretation depends on the server session TZ (not measured).
- `api_upload_reference`/`api_fetch_reference` close the membership session before the 50 MB read + ingest; a member removed mid-upload still lands the reference (TOCTOU shared by every long route).


## Appendix A4 — references and citations

#### A4 — References & citations subsystem (adversarial review)

Worktree: `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-a2dcf2b391e0fc8f3` @ 8f44a35e.
Venv verified: `call_manager.__file__` inside the worktree. Python 3.12.13; pdfminer-six 20260107,
python-docx 1.2.0, openpyxl 3.1.5, striprtf 0.0.32, httpx 0.28.1, starlette 1.3.1. **pymupdf 1.27
is installed but unused** — every PDF goes through pdfminer (`extract.py:152`).
Probe scripts: `scratchpad/probes/p1_error_body.py`, `p2_slow_drip.py`, `p4_pdf_stream_bomb.py`.
Two probe pytest files were created in the worktree, run, and deleted; `git status --short` is empty.

##### 1. Findings (ranked)

###### F1 — High — no cap on extracted text / chunk count before embedding (sibling-format gap)
`src/call_manager/proposal_assist/references/ingest.py:124-170` (chunk → embed loop),
`extract.py:38` (page cap exists for PDF only), `extract.py:86-89,117,233` (text/markdown/rtf: none).
The PDF extractor caps at 100 pages; text/plain, text/markdown, RTF and DOCX text have no cap at
all, and nothing between `chunk_sections` and `embedder.embed` bounds the chunk count. Every chunk
is one synchronous Ollama round-trip on the request thread inside `limiter.slot()`, then one
`ReferenceChunk` row with a 1024-float vector, all held in the `embedded` list before a single INSERT tx.
*Failure scenario:* any workspace member (`require_member`, `web/api/references.py:100`) uploads a
50 MB `text/plain` (allowed MIME, under the 50 MB cap and the 64 MB body middleware).
**RAN:**
```
uv run python - (chunk_sections over extract_text(mime="text/plain", 50 MiB))
upload bytes 52428800 / extract s 0.03 / sections 1
chunks (== sequential embed calls == ReferenceChunk rows): 58255
approx pgvector bytes at 1024 float4/row: 227 MB
md (50 MiB of "# h\nbody line\n"): extract s 4.88, sections 3744914, chunks 3744914
```
58,255 sequential GPU embed calls (tens of minutes of the shared 2080 Ti, one of
`reference_ingest_concurrency=3` slots, one threadpool thread) per upload; the markdown variant is
3.7 M calls and a ~30 GB `embedded` list **in the app process** (not the sandbox child).
Coverage: no test bounds chunk count — `grep 'len(chunks)' tests/proposal_assist/references/` finds
only `>=` assertions (`test_chunker.py:19,36`, `test_ingest.py:103`, `test_reprocess.py:146`).
*Fix shape:* after `chunk_sections` (ingest.py:127) enforce `_MAX_REFERENCE_CHUNKS` (and/or a
`len(full_text)` cap) — truncate with a visible `[truncated]` section exactly like the PDF page cap,
or raise `ExtractError`. Consider a per-page text cap in `_extract_pdf` too.

###### F2 — High — fetcher reads a 4xx/5xx body with no size cap
`src/call_manager/proposal_assist/references/fetcher.py:276-281` — `response.read()` on any
`status >= 400` materialises the entire body; the `max_bytes` streaming cap applies only to the 2xx
branch (lines 287-294). Reachable by any member via `POST /workspaces/{id}/references/fetch` with a
URL they control (`web/api/references.py:232` → `dispatch_fetch` → `fetch_generic_url`), and by the
agent fetch tool.
**RAN:** `p1_error_body.py` (MockTransport serving 200 MB, `max_bytes=1 MiB`):
```
status=200 max_bytes=1MB -> FetchError: ... exceeds max_bytes=1048576; bytes actually read = 1 MB
status=500 max_bytes=1MB -> FetchError: HTTP 500 ...; bytes actually read from server = 200 MB
peak RSS MB: 626
```
*Fix shape:* read at most a few KB for the snippet (`next(response.iter_bytes(4096), b"")`) —
never `response.read()` on an error status.

###### F3 — High — `timeout_s` is not a deadline: a slow-drip server holds an ingest slot indefinitely
`fetcher.py:222-238` computes a "single deadline for the entire redirect chain" but only checks it
*between* hops; `httpx.Client(timeout=remaining_s)` at `:259` is a **per-read** timeout. A body that
delivers one byte every `< remaining_s` seconds never trips it; the only bound is `max_bytes`
(50 MB at 1 byte/59 s ≈ 93 years). The fetch runs inside `limiter.slot()`
(`web/api/references.py:231-232`) — three such URLs make every reference upload / fetch / agent
fetch on the instance return 429 until the drip ends.
**RAN:** `p2_slow_drip.py` — real local HTTP server dripping 12 bytes at 0.4 s, `_resolve_and_validate`
patched only to let the probe reach 127.0.0.1:
```
timeout_s=1.0 -> returned 12 bytes after 4.5s (NO FetchError)
```
**RAN (mutation, hollow-gate shape 4):** `sed -i '259s/timeout=remaining_s,/timeout=None,/'` →
`uv run pytest test_fetcher_budgets.py test_fetcher_ssrf.py test_fetcher_backends.py` → **33 passed**.
The "deadline" tests (`test_fetcher_budgets.py:127-150`) only exercise the pre-hop check via a
monkeypatched `time.monotonic`; nothing pins in-hop enforcement. Reverted.
*Fix shape:* re-check `deadline_time` inside the `iter_bytes` loop (and on the error path), raising
`FetchError("fetch budget exhausted")`; keep the per-read timeout as `min(remaining_s, 10)`.

###### F4 — Medium — reprocess silently re-keys and de-approves a reference
`ingest.py:285-296` (Phase 3 runs again on reprocess with `source_meta=None`): it overwrites
`metadata_status`, `title`, and calls `generate_citekey` unconditionally; `csl.py:476-495` counts
the row's **own** citekey as taken (no `Reference.id != reference_id` filter), so the key shifts to
the next suffix.
*Failure scenario:* upload → user approves metadata (`api_approve_metadata`, status `approved`,
citekey `smith2021`) → text extraction later fails (Ollama down) → user clicks Reprocess.
**RAN** (probe test, deleted after run):
```
OBSERVED AFTER REPROCESS: {'citekey': 'smith2021a', 'metadata_status': 'extracted',
  'title': 'LLM Title', 'csl_json': {... 'title': 'Approved Title'}}
```
Harm: the human approval is lost (bibliography build is fail-closed on `metadata_unapproved`,
`bibliography.py:546-557`, so a later cite blocks export), the approved title is replaced by the
LLM's, and author-year rendering shows "(Smith 2021a)" / "Smith (2021a)." with no 2021 sibling
(`_suffix_from_citekey`, `bibliography.py:166-184`). Ledger canonicality (`core.py:265`,
`bibliography.py:503-519`) means an already-cited reference cannot be bound to the wrong row — but a
failed-text reference is never cited, so that guard never applies here.
Coverage: `test_reprocess.py` has zero assertions on citekey/metadata_status/title (grep: no hits).
*Fix shape:* in Phase 3, keep an existing `citekey` (mirror `web/api/references.py:347` and
`core.py:265`), never downgrade `approved`/`verified_external`, and exclude the row itself in
`generate_citekey`'s taken-set.

###### F5 — Medium — deep citation check pins a Postgres connection `idle in transaction` across every LLM call
`src/call_manager/web/api/citation_audit.py:606-665` opens one session, then
`validate_citations_semantic_capped` (`audit.py:867-922`) reads chunks/cached verdicts on it and
calls the LLM per row. Budget: `citation_deep_check_max_per_request=25` ×
`citation_llm_timeout_seconds=60` = up to 25 min per request holding one of
`pool_size=10, max_overflow=20` connections (`db/session.py:15-16`) plus a threadpool thread.
`ingest.py:139-143` explicitly avoids this pattern for embeds; this route did not.
**RAN** (probe test, deleted): stub LLM inspects `pg_stat_activity` on a separate connection during
each call:
```
DURING LLM CALL #1: idle-in-transaction backends = 2
    (85, 'idle in transaction', 'SELECT pg_try_advisory_lock(hashtext($1))')   <- scheduler lock, by design
    (86, 'idle in transaction', 'SELECT reference_chunks.id AS reference_chunks_id, ...')  <- the route
DURING LLM CALL #2: same two
```
*Fix shape:* resolve chunk + cache hit in a short session and end the transaction (`commit()`/
`rollback()`) before `citation_service.complete_json`; write via `store_verdict` as now.

###### F6 — Medium — the extraction sandbox bounds CPU time, not memory
`extract_sandbox.py:46-48` spawns the child with no `RLIMIT_AS`/`RLIMIT_DATA`; `extract.py:150-171`
caps pages, not bytes per page, and pdfminer inflates FlateDecode streams whole.
**RAN:** `p4_pdf_stream_bomb.py` — a 1.99 MB PDF with one page whose content stream is
`zlib(b" " * 2 GiB)`, through `extract_text_bounded(timeout_seconds=30)`:
```
pdf bytes on disk: 1.99 MB (inflates to 2 GiB)
ExtractError: ExtractError: pdf produced no text
wall 19.9s; child peak RSS = 4.04 GB
```
Within the 30 s budget the child allocated 4 GB and finished "successfully". A 50 MB upload has 25×
the budget; on a host sharing RAM with Postgres/Ollama the OOM killer or swap pressure lands on
neighbours before the timeout fires. (The DOCX/XLSX zip guard is sound at rest — see §2 — but the
same transient allocation applies, see leads.)
*Fix shape:* `resource.setrlimit(resource.RLIMIT_AS, (N, N))` at the top of `_worker`
(MemoryError → `ExtractError`), N ≈ 1-2 GiB; optionally cap per-page text length.

###### F7 — Low — docstring promises CGNAT is refused; the code does not
`fetcher.py:70-72` says "…ULA, CGNAT…"; `:77-84` uses `is_private`, which excludes 100.64.0.0/10.
**RAN:** `_is_private_ip("100.64.0.1") -> False` (all other probed classes refused, see §2).
Matters only where internal services live in shared address space (some k8s/VPC layouts); low for
an RFC1918 on-prem deployment. No test mentions `100.64`.
*Fix shape:* add `ip in ipaddress.ip_network("100.64.0.0/10")`.

###### F8 — Low — `text_error` leaks the internal Ollama URL to every member
`ingest.py:191,201` persist `f"{type(e).__name__}: {e}"`; `embeddings/client.py:179` wraps
`raise_for_status()` whose message carries the URL. Returned in `IngestResultOut.text_error`
(`web/api/references.py:186-190`) and shown in the library UI.
**RAN:** MockTransport 500 →
`LLMError: ollama embed failed: Server error '500 Internal Server Error' for url 'http://ollama-gpu-box.hospital.internal:11434/api/embed'`.
`client.py:183,190` similarly embed `data!r` / a 200-char response slice.
*Fix shape:* persist a class/short message, log the full one.

##### 2. Checked and sound
- **SSRF deny-list** (RAN, `_is_private_ip`/`_validate_url`): refused — 127/8, 10/8, 169.254.169.254,
  0.0.0.0, 0.0.0.1, 192.0.0.8, 198.18.0.1, 240.0.0.1, 255.255.255.255, 224/4, `::1`, `fe80::`,
  `fc00::`/`fd00::`, `::ffff:127.0.0.1`, `::ffff:10.0.0.1`, `::ffff:169.254.169.254`,
  NAT64 `64:ff9b::a9fe:a9fe`, 6to4 `2002:a9fe:a9fe::1`/`2002:7f00:1::1`, Teredo `2001::1`, `::`,
  `::ffff:0:0`; schemes `file:`/`data:`/`gopher:` refused; `http://0x7f000001/`, `http://2130706433/`,
  `http://127.1/` are not literals so they go through `getaddrinfo` and the *resolved* address is
  checked; bracketed IPv6 with zone id parses as link-local. Explicit `transport=` disables httpx
  env proxies (`trust_env` only applies when `transport is None`), so `HTTPS_PROXY` cannot bypass
  the pin. Redirect to `file:`/`data:`/private is re-validated on the next hop; pin-map miss
  re-validates on the fly (`_PinnedNetworkBackend.connect_tcp`).
- **Body size**: `MaxBodySizeMiddleware` (`web/middleware.py:143-235`, `MAX_REQUEST_BODY_BYTES`
  64 MiB, Content-Length + streaming backstop) bounds Starlette's multipart spool before the 50 MB
  route cap.
- **Zip bomb guard** (READ): Python `zipfile` truncates at the declared `file_size` (`_left`), so a
  lying central directory cannot exceed the declared total at rest; python-docx parses with
  `resolve_entities=False`; ipynb cap = upload cap; XML via defusedxml; CSV/XLSX column caps.
- **`running` strand**: `main.py:131-158` sweeps `running → failed` at startup; reprocess claims
  `failed → running` atomically (`UPDATE … WHERE text_status='failed' RETURNING`, `ingest.py:443-455`)
  and reverts on pre-extraction failure (`:481-494`); Phase-2 failure always writes `failed`.
  `outline_status` is constant `skipped` in v1; `metadata_status` has no running state.
- **Delete during ingest**: chunk INSERT hits the FK → caught (`ingest.py:190-205`); `write_text`
  does not `mkdir`, so a removed reference dir is not recreated (no orphan bytes).
- **Audit keys on content, not id**: `chunk_content_hash` (`audit.py:365`), verdict cache key =
  (claim, chunk content, quote, stance, validator version) (`verdict_cache.py:150-163`); a
  re-upload is a new reference id; reprocess rewrites chunks → `index_drift`; deleted mid-audit →
  `reference_missing`/`chunk_missing`; cross-workspace ids treated as missing.
- **Citekey → reference binding** goes through the ledger's `reference_id` (`bibliography.py:503-519`,
  `core.py:265`), so DB citekey drift (F4) cannot verify a citation against the wrong reference.
- **Egress**: all three reference routes gate via `citation_cloud_permitted` (provider + kill-switch +
  per-user disclosure ack, `_cloud_gate.py:435-447`); deep check refuses (503/403) instead of
  degrading; the MCP path runs inside a consented drafting turn; metadata extraction sends only
  `text[:6000]`; Crossref/PubMed/arXiv receive only the user-typed identifier (quoted), never text.
- **Bibliography injection**: export is a `text/markdown` attachment (`export.py:40,79`); the SPA has
  no `innerHTML`; `_clean` collapses newlines so a field cannot split entries or desync numbering;
  `neutralize_trust_delimiters` covers attribute/whitespace variants of the wrapper tags.
- **Approve route** (`references.py:347-350`) correctly keeps an existing citekey — the guard F4
  lacks exists here.

##### 3. Prior findings touched
- SSRF fail-open (third-pass §4.1) — fixed 8a297db1; the reference fetcher never had it (raises).
- Citation-LLM egress gate (third-pass §8 contested) — fixed c67109a8; verified on all reference
  routes + deep check. Unattended ticks still rely on the operator-level `CITATION_LLM_ALLOW_CLOUD`
  opt-in only (by design, documented `llm.py:84-87`).
- Malformed redirect `Location` → `urljoin` ValueError (sec review Medium) — still open
  (`fetcher.py:273` unchanged); robustness only.
- Citekey 4-retry on collision (sec review Low) — unchanged.
- `ReferenceChunk` double cascade (sec review Low) — unchanged, harmless.
- Broad `except ValueError` → 400 at `web/api/references.py:273` (third-pass §5) — still open.

##### 4. Not read
`citations/gitio.py` (only grepped: every `subprocess.run` carries `timeout=`), `citations/core.py`
(only `_resolve_citekey_for_ref` and insert steps 6-9), `collab_cite.py`, `reader.py`, `ledger.py`,
`markers.py`, `quotes.py`, `claims.py`, `human_cite.py` (grep only), `usage.py` beyond line 60,
`kinds.py`; tests under `tests/proposal_assist/citations/`, `test_extract_sandbox.py`,
`test_fetcher_ssrf.py` (grep only). `references/backfill.py` read, not probed.

##### 5. Leads (no proof)
- `tools/audit.py:86` calls the **uncapped** `validate_citations_semantic`: an agent
  `validate_citations` over a large ledger makes one LLM call per uncached row (bounded only by the
  daily token caps). The web route got the capped wrapper (Defect-2); the MCP tool did not.
- Zip guard transient allocation: `ZipExtFile.read()` calls `decompress(data, MAX_N=2^31-1)` in one
  shot before truncating to the declared size, so an honest `compress_size` with a lying `file_size`
  can still allocate ~2 GB in the child before the CRC check fails. Same RLIMIT fix as F6.
- pdfminer's document object cache retains decoded streams across pages, so a 100-page file of
  inflating streams accumulates rather than peaks per page — F6 in the multi-page form.
- `tools/references.py:506-512`: a broken limiter (`except Exception: acquired = True`) fails open.
- `fetch_crossref_doi` labels the publisher link `application/pdf` without sniffing; an HTML
  landing page becomes a `failed` reference (robustness only; `fetch_generic_url` does sniff).
- `_extract_docx` reads `doc.paragraphs` only — table cell text is never indexed (data quality).
- LLM-returned `csl_json_extracted` is stored unvalidated (any keys/shapes); the formatter and
  `generate_citekey` are defensive (checked), so only cosmetic output is possible. A crafted PDF can
  also steer `_kind` (`metadata.py:98-100`) under `auto_classify`.


## Appendix A5 — ingest → rank → digest

#### A5 — ingest → rank → digest (core product path)

Reviewer worktree: `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-a2cb704ec2d91aa78` @ 8f44a35e.
Venv verified: `call_manager.__file__` resolves inside the worktree. Probe file
`tests/test_zz_probe_a5.py` was created, run, and deleted; the one by-line mutation
was reverted with `git checkout --`. `git status --short` is empty.

All line numbers are against 8f44a35e.

---

##### 1. Ranked findings

###### F1 · High · `published_at` silently dropped from the upsert payload — a 3-day-old regression
**Where:** `src/call_manager/ingestion/upsert.py:147-170` (payload) and `:180` (update set built from the same payload).
**Defect:** commit `8a297db1` (2026-08-29 20:45, "settle types before the DB") rewrote the
payload with `_as_text/_as_int/_as_datetime` wrappers and dropped the line
`"published_at": call.published_at` that the previous version had at line 98. Both adapters
still populate `NormalizedCall.published_at` (`vinnova.py:292,306`, `eu_portal.py:283`); it
now goes nowhere. Because the ON CONFLICT `update_cols` is derived from the same dict, rows
that already existed keep their old value — **every call first seen since 2026-08-29 has
`calls.published_at IS NULL`, permanently.**

**Failure scenarios (each one is a downstream reader of `published_at`):**
1. `ranking/backfill.py:259` and `ranking/queue.py:151` filter `published_at >= cutoff`, which
   is NULL-false. So `backfill_profile` (called from profile edit `web/api/profiles.py:398-405`
   → `post_edit.py:40`, from the bootstrap worker `bootstrap_worker.py:250`, and from
   `/admin/rerank` `admin.py:384`) and the daily `reconcile_missing_rankings` **never enqueue a
   call ingested after 08-29**. A profile created or edited today is never ranked against
   this week's Vinnova calls (Vinnova only re-emits a round when it is modified upstream);
   EU calls are rescued by accident only because F3 re-enqueues the whole EU corpus daily.
   This is the brief's "call that should be visible to a profile but is hidden", and the
   answer to "does a profile edit invalidate and re-queue rankings": the trigger exists
   (backfill) but is a no-op for new calls.
2. `web/dashboard.py:165` — the default `newest` sort is `published_at DESC NULLS LAST`, so
   **every call ingested since the fix sinks below every older call** in the unranked feed.
3. `web/api/admin.py:192-194` — `rerank_estimate` counts `published_at >= cutoff` → undercounts.
4. `frontend/src/routes/CallDetailBody.tsx:109` renders "—" for the publication date.

**RAN — three executed proofs:**
- Probe 1 (upsert round-trip):
  ```
  PROBE1 persisted published_at = None
  assert None == datetime.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)   -> FAILED
  ```
- Probe 2 (real `run_ingestion` of two calls with `published_at` set, then a profile created
  afterwards):
  ```
  PROBE2 backfill enqueued=0 reconcile enqueued=0 jobs=0 calls_with_null_published_at=2
  ```
- By-line mutation of the only end-to-end test that claims to pin the ordering
  (`tests/test_ingestion_e2e.py:24,30`, dates swapped so "call two" becomes the OLDER call):
  ```
  sed -i '24s/2026, 4, 10/2026, 4, 12/; 30s/2026, 4, 12/2026, 4, 10/' tests/test_ingestion_e2e.py
  uv run pytest tests/test_ingestion_e2e.py -q -p no:cacheprovider   ->   1 passed
  ```
  Still green: the assertion "two before one" is satisfied by `Call.id DESC`, never by
  `published_at`. Hollow gate, shape (2) — already true before the field mattered.
- `git show 8a297db1^:src/call_manager/ingestion/upsert.py | grep -n published_at` → `98:` present;
  `grep -n published_at src/call_manager/ingestion/upsert.py` → nothing.

**Coverage:** three hollow gates let this through. `test_ingestion_e2e.py:75-76` (above);
`test_ingestion_poison_record.py:73` parametrises `("published_at", "2026-01-01")` under
"still ingests" but never reads the column back, so dropping the field passes the same as
coercing it; `test_ranking_backfill.py:9-22` and `test_ranking_queue.py:201` seed `Call(...)`
directly with `published_at`, bypassing `upsert_call` entirely.

**Fix shape:** add `"published_at": _as_datetime(call.published_at)` to the payload; add one
test that goes `NormalizedCall(published_at=ts)` → `upsert_call` → `Call.published_at == ts`;
make the e2e test insert the older call second (or assert on the API's `published_at` field).
A one-off `UPDATE calls SET published_at = ...` from `raw->>'publicationDate'` /
`raw->>'Oppningsdatum'` can repair the rows inserted since 08-29 (`raw` is stored).

---

###### F3 · Medium · Every EU tick re-enqueues the entire open EU corpus × every active profile
(Numbered F3 to keep the probe numbering; it is the second-ranked finding.)
**Where:** `sources/eu_portal.py:165-173` — the search request carries only
`status=OPEN&status=FORTHCOMING`; the `since` parameter is never referenced in the function
body (`grep -n since` → only the signature and a comment). `ingestion/orchestrator.py:185-189`
appends every fetched call to `touched_call_ids` whether inserted or unchanged, and `:263`
bulk-enqueues `touched × active profiles` with ON CONFLICT that only dedups against
*active* jobs. There is no content comparison anywhere on the path.

**Failure scenario:** each 03:00 EU tick creates |open EU calls| × |profiles| ranking jobs
for calls whose text has not changed. Consequences:
- The local Gemma queue re-ranks the whole EU corpus daily on the shared 2080 Ti (also the
  embedder and every proposal-assist pass runner).
- `rank_pair.py:167-170` re-escalates every pair whose local score is ≥ 45 to Sonnet again;
  `worker.py:166` then disables escalation for the day once `count_today >= remote_daily_cap`
  (100). The cap is spent on **already-analysed** pairs, so genuinely new calls that arrive
  later in the day are ranked local-only, and the same Sonnet summaries are re-billed daily.
- The timeline gate (`criteria.py:104`, FAIL when deadline < now+14d) is re-evaluated on each
  re-rank, so a call the user saw at 85 is rewritten to `0 / timeline` two weeks before its
  deadline and drops out of the digest's top/borderline buckets (`assemble.py:227,262`) and to
  the bottom of the ranked dashboard. Whether that is desired is a product decision; today it
  happens silently and only for sources that re-emit unchanged calls.

**RAN:** Probe 3 — same static adapter, two consecutive `run_ingestion` runs with the first
run's job marked DONE in between:
```
PROBE3 jobs after two identical ingests: ['done', 'pending']
```
**Fix shape:** enqueue only when the row is new or its content changed — e.g. compute a
content hash (title/summary/full_text/deadline/budget/programme) in `upsert_call`, store it,
and return `changed: bool` (or use `RETURNING (xmax = 0) AS inserted, <old-hash> IS DISTINCT
FROM <new-hash>`), then pass only changed ids to `bulk_enqueue_for_calls`. That also makes
"re-rank on upstream update" an explicit, testable contract instead of a side effect.

---

###### F4 · Medium · Digest week-claim is only *flushed* before the send; a post-accept failure re-sends
**Where:** `digest/send.py:129-136` (insert + flush), `:149-151` (send), `:186/:191` (commit
after the send); `:153-163` releases the claim on `EmailTransportError`.
**Defect:** the comment at `:119-128` says the claim "gates" the irreversible side effect,
but it is not durable at send time. Two concrete paths re-send within the same Stockholm week:
1. `email/client.py:110-115` — Resend returns **200** (message accepted) with a non-JSON body →
   `EmailTransportError` → `send.py:157` rollback → claim released → the next run (admin
   `POST /admin/digest/send`, or the 24 h misfire re-run) sends the same digest again.
2. Any exception after the provider accepted the message but before `:191` commits (DB
   connection drop, process kill) → the `with session_factory()` block exits without commit →
   same outcome.
The `included_call_ids` snapshot is lost with the row, so `assemble.py:117-129` will also
present the same calls as "new" a second time.

**RAN:** Probe 4 — a fake email client that opens a *second* session inside `send()` and
counts committed `DigestRun` rows for the recipient:
```
PROBE4 emails_sent= 1 committed DigestRun rows visible to another session at send time: 0
```
**Fix shape:** commit the claim in its own short transaction before sending (the same
discipline `worker.py:190-211` already applies to the Sonnet ledger); on a genuine transport
failure delete it in a fresh transaction; treat a 2xx-with-bad-body as *sent* (log, keep the
claim). A `status` column (`claimed/sent/bounced`) makes the state explicit.

---

###### F5 · Low · A near-miss Gemma reply is retried 5× at temperature 0, then the pair is dead-lettered with no ranking
**Where:** `ranking/prompt.py:307,319,326,278` (strict int / lowercase-literal / str-or-null
checks), `ranking/ollama_client.py` (no markdown-fence stripping — only
`anthropic_client.py:220` has it), `ranking/worker.py:179-188` + `queue.py:226` (flat retry ×5).
**Failure scenario:** Gemma emits `"rating": 4.0`, `"rating": "4"`, `"Pass"`, or a fenced
block. Every retry is the same deterministic reply; after five GPU runs the job is FAILED,
no `CallRanking` row exists, the call sorts as *unranked* for that profile and never enters
the digest. `reconcile_missing_rankings` re-tries seven days later with the same outcome.
**RAN:** `scratchpad/probe_parser.py`:
```
float rating 4.0             -> ParseError: topic_fit.rating must be int 0-5; got 4.0
string rating '4'            -> ParseError: topic_fit.rating must be int 0-5; got '4'
fenced json                  -> ParseError: invalid JSON: Expecting value: line 1 column 1
eligibility 'Pass'           -> ParseError: eligibility.rating must be 'pass'|'fail'|'unclear'; got 'Pass'
quoted_evidence non-str      -> ParseError: topic_fit.quoted_evidence must be string or null; got int
```
Answer to the brief's question: a malformed reply **skips** the row (nothing persisted, no
garbage), at the cost of five identical local runs. `response_format=json_object` makes
fences unlikely but does nothing for `4.0` / `"Pass"`.
**Fix shape:** normalise the obvious near-misses (int-valued float/str, case-fold the rating
words, coerce non-str `quoted_evidence` to null, reuse `_strip_markdown_fence`) and stop
retrying `ParseError` on the local path (dead-letter after one attempt, or one repair turn).

---

###### F6 · Low · Profile edits skip backfill entirely when the description is blank
**Where:** `web/api/profiles.py:398` — `if body.description.strip(): background.add_task(...)`.
**Failure scenario:** a bootstrap that exhausted its five attempts leaves `description=""`
(`create.py:117`, `bootstrap_worker.py:190-196`); the user then fills in areas, budget, TRL,
must_include and saves → no embedding refresh, no backfill; existing rankings stay computed
against the old rubric. **READ.** Fix: gate on "anything that feeds `build_profile_text`
or the gates changed", not on the description alone.

---

##### 2. Checked and sound
- **Digest HTML escaping:** `render.py:27-37` sets `autoescape=True` unconditionally for the
  HTML template (comment explains why not `select_autoescape`); no `|safe` in either template;
  `e.url` is never rendered (links go to the dashboard); the text template runs with autoescape
  off, correct for `text/plain`; `build_subject` strips CR/LF (`render.py:58-65`).
- **Subscription filtering:** `assemble.py:83-96` joins `Subscription.digest_enabled IS TRUE`
  and `Profile.is_active`; `send.py:31-42` excludes `status != 'active'` and bounce-disabled
  users; `DIGEST_DRY_RUN` logs and writes nothing (`send.py:106-117`).
- **Week boundary:** `week_start()` is Stockholm-Monday and the DB unique index is on a
  generated `date_trunc('week', sent_at, 'Europe/Stockholm')` column (migration 0033) —
  consistent on both sides; the misfire grace of 24 h relies on it and is safe.
- **Daily cap day boundary:** `remote_attempts.count_today` uses the shared
  `timeutil.stockholm_day_start_utc` (prior §4.14c fixed; `tests/test_stockholm_day_boundary.py`).
  Golden-set eval has no day-boundary logic (weekly Kendall tau-b only).
- **Sonnet ledger discipline:** row committed in its own transaction before the merge
  (`worker.py:202-211`); remote failures never propagate (`rank_pair.py:205-228`);
  `anthropic_client.py:107-121` wraps every `APIError` subclass; `max_tokens`/`length`
  truncations fail fast instead of burning retries.
- **NULL / mismatched embeddings:** `criteria.py:157-158` → UNKNOWN → LLM runs;
  `rank_pair.py:393-429` returns None with a warning on model mismatch; the 0.20 hard-fail band
  only zeroes below hard-fail, 0.20-0.30 goes to the LLM; must_include bypasses timeline and
  embedding but not budget/TRL (`rank_pair.py:126-136`, `aggregate.py:52-62`).
- **Missing criterion data:** absent budget / TRL / currency → UNKNOWN, never FAIL
  (`criteria.py:113-120,132-133`); currency mismatch → UNKNOWN.
- **Deterministic vs LLM disagreement:** gates always win (short-circuit before the LLM);
  eligibility `fail` with topic ≥ 3 escalates to Sonnet when configured (`rank_pair.py:169`).
- **EV extraction:** `ev.py` is string-literal-aware, tolerates key reordering, never raises;
  degenerate/out-of-range EV falls back to argmax.
- **Queue:** partial unique index + ON CONFLICT with the verbatim predicate; stuck-RUNNING
  reaper bumps attempts and dead-letters; tick budget reverts untouched claims without
  burning attempts; inactive profile → job DONE not FAILED.
- **Upsert typing / SSRF:** prior §4.1 and §4.2 are fixed at 8a297db1 (fail-closed transport
  in both adapters; identity fields rejected, metadata coerced; keywords element-typed).
- **Bootstrap job lifecycle:** claim counts the attempt, reaper at 30 min, dead-letter at 5,
  every post-claim exception transitions the job (`bootstrap_worker.py:148-199,263-268`);
  profile edits 409 only while `queued/running`, so a failed bootstrap does not lock the profile.
- **PubMed query injection:** `_sanitize_entrez_value` strips field tags/booleans/quotes.
- **Alerts:** `alerted` flips only after a successful send; stale-source backstop has a cooldown.

##### 3. Prior findings touched
- §4.1 adapters fail-open on refused SSRF target — **fixed 8a297db1** (`vinnova.py:145-156`, `eu_portal.py:181-192`).
- §4.2 non-string scalar halts ingestion — **fixed 8a297db1**; that same commit introduced F1.
- §4.3 `CALL_BODY>>>` forgeable from call text — **still open**: `prompt.py:226-234` interpolates
  `text` with no marker neutralisation; the tests at `test_ranking_prompt.py:96-145` pin the
  *placement* of injected title/programme inside the fence, not a forged closing marker.
- §4.11 SMTP-only deploy loses the digest — **fixed f6feeed2** (`email/factory.py:201-226`, one resolver).
- §4.14(c) three day-boundary implementations — **fixed** (`timeutil.py`; `digest/send.py:45-58`
  keeps its own, tested, week helper by design).
- §9 "remote-escalation ledger is sound" — **confirmed**.
- §8 refuted "shrunk embeddings zeroed by the 0.20 gate" and "stale-source cron ordering" — not reopened.

##### 4. Not read
`embeddings/cli.py` and `ingestion/cli.py` (structure only), `web/api/calls.py`,
`web/api/profiles.py` outside `update_profile`, `web/api/admin.py` outside the grep hits,
the React SPA beyond the `published_at` grep, and `tests/test_ranking_worker.py` /
`test_digest_assemble.py` bodies.

##### 5. Leads (no proof)
- **Timeline re-zeroing** (see F3): with daily re-rank, every open call is rewritten to
  `0 / timeline` 14 days before its deadline. If "surface until the deadline" is the intent,
  the gate should apply only on first rank or the digest/dashboard should read the gate row
  rather than the headline score.
- **Withdrawn upstream calls** persist until their deadline — documented TODO at
  `eu_portal.py:169-173`; no reaper. A withdrawn call with a far deadline stays in the digest
  candidate set and dashboard.
- **Same call from two sources / changed external_id** → two rows, both ranked, both eligible
  for the digest (`assemble.py` dedups by `call_id` only).
- **Upstream drops `deadlineDate`** → `upsert.py:180` overwrites `deadline` with NULL (only
  `full_text` is coalesced) → the call becomes "rolling", open forever, and joins the
  unbounded never-expire set in `assemble.py:131-152`.
- **must_include bypasses `expired` as well as `too_soon`** (`criteria.py:99-106` returns FAIL
  for both; `rank_pair.py:131` bypasses both) — an expired call can receive a full score;
  harmless today because every reader filters `deadline > now`.
- **No second opinion without `ANTHROPIC_API_KEY`:** a Gemma eligibility false-`fail` zeroes
  the call with no recourse (`aggregate.py:65-66`); the eligibility-fail escalation rule only
  exists on the hosted path.
- **Bootstrap output is live immediately:** `create.py:69-70` writes the LLM draft
  (seeded from PubMed titles + pasted text) straight into `profile.description`, which
  `prompt.py:207-216` places in the ranking *system* prompt, before any human review. Internal
  users only, so low — but the SECURITY note treats the profile as trusted.
- Live-DB sizing of F1 was not performed (the probe needs the main tree's `.env` and was
  blocked); the commit timestamp bounds it to calls first seen after 2026-08-29 20:45 CEST.


## Appendix A6 — collab and the CRDT ↔ git boundary

#### A6 — Collaborative-editing core and the CRDT ↔ git boundary

Worktree `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-ad71d48b960993bc8` at `8f44a35e`; venv verified to import the worktree. Probe scripts under `scratchpad/a6/` (`probe_bridge.py`, `probe_crdt_marks.py`, `probe_crdt_shapes.py`, `probe_stale_doc_merge.py`, `probe_frame_cap.py`). One scratch pytest was written into the worktree, run, and deleted; one mutation was applied by line number and reverted. `git status --short` is empty.

##### 1. Ranked findings

###### F1 — HIGH · a retained Y.Doc that reconnects after a branch switch merges the old branch into the new one, and the server cannot tell
`src/call_manager/proposal_assist/collab/room.py:153-155` (server sends STEP1 on connect), `room.py:201-246` (STEP2 applied + persisted, no identity check), `src/call_manager/proposal_assist/web/collab_ws.py:66-152` (URL carries no branch/generation; the stamp is minted fresh at every connect), `frontend/src/collab/collabProvider.ts:266-278` (every close code other than 1008/1009/4409 reconnects **with the same doc**).

**Defect.** The `e312b373` fix (spec `2026-09-01-branch-leak-remaining-bugs.md`) makes the *teardown* close connected clients with 4409 so they discard the doc. A client that is **not connected at switch time** — in reconnect backoff after a wifi blip (1006, up to 30 s × 12 attempts), asleep with a half-open TCP socket, or whose close frame was buffered into a dead socket (`_close_all` counts it as closed after 1 s, `room.py:936-959`) — keeps the old branch's Y.Doc, reconnects after the switch has committed with a *fresh* stamp (so `WorkspaceGenerationStale` never fires), and answers the server's STEP1 with a STEP2 carrying its whole document. The server applies it, persists it under **that user's `actor_id`**, and the next checkpoint commits the doubled document to the new branch. This is the same symptom class as the 3,183→41,539-byte / "13 duplicate copies" corruption the user already suffered; the spec lists only the 4409 storm as remaining.

**RAN** `probe_stale_doc_merge.py` — exact server-side handshake through `Room.handle`:
```
room before reconnect: '## Aims\n\nNew aims text.\n'
persisted rows: 1
room AFTER reconnect: '## Aims\n\nNew aims text.\n\n## Aims\n\nOld aims text.\n\n## Methods\n\nOld methods.\n'
```
**Fix shape.** Give the document an identity the socket must present: a per-artifact `doc_epoch` in `collab_artifact_state`, bumped by every `truncate_log` caller (switch, handover); the server sends it in the first frame (or the client passes `?epoch=` from the draft GET), and the route closes 4409 on mismatch *before* sending STEP1. The generation fence cannot do this because it is re-minted per connect.

###### F2 — HIGH · the serializer mis-nests marks read back from Yjs, so bold/italic/link ranges are silently widened in every checkpoint and in every kernel section replace
`src/call_manager/proposal_assist/collab/markdown_bridge.py:516-520` (`close_to` stops at the first active mark still wanted), `yjs_bridge.py:395` (`_format_to_marks` sorts marks by schema rank: link=0 < em=1 < strong=2).

**Defect.** Markdown-it always yields well-nested runs, but the Yjs read path rebuilds each run's mark list in schema order, so "outer mark ends, inner mark continues" is inevitable: run A = {em, strong}, run B = {strong}. `close_to` sees `strong` on top of `active`, stops, and never closes `em`; run B is emitted inside the still-open `*`. With a link the LINK range widens. The live room is correct; git, the canonical projection, export, the LLM's context, and — through `apply_pm_section_to_fragment` re-parsing the serialized section (`kernel.py:171-175`, `284-287`, `540-544`) — the **live doc after any citation insert or accept on that section** all carry the corruption. After a branch switch the editor shows it too.

**RAN** `probe_bridge.py` / `probe_crdt_marks.py` / `probe_crdt_shapes.py` (md → ydoc → md, exactly hydrate+checkpoint):
```
IN : '## S\n\n**_Aim 1_: text**\n'            OUT: '## S\n\n***Aim 1: text***\n'      (": text" gained italic)
IN : '## S\n\n**[Aim 1](https://x.se): text**\n' OUT: '## S\n\n[**Aim 1: text**](https://x.se)\n'  (link widened)
direct pm_to_markdown (strong,em order): '***Aim 1*: text**'   (em,strong order): '***Aim 1: text***'
read back from ydoc: marks [em, strong] then [strong]  -> via ydoc: '***Aim 1: text***'
```
**Fix shape.** In `_serialize_inline`, when the target set lacks any *active* mark, close down to the outermost such mark and re-open the still-wanted inner ones (or order each run's marks by "how far the mark extends to the right" before emitting). Add the two shapes above to `test_markdown_bridge.py` through `pm_json_to_ydoc`, not only through `markdown_to_pm`.

###### F3 — HIGH · an empty blockquote made in the editor serializes to `>`, which `markdown_to_pm` cannot parse; the next rehydration bricks the artifact and the first open of any such git document bricks it permanently
`markdown_bridge.py:218-221` (blockquote built with `content: []`; `.check()` at `:344-345` rejects `block+`), `:633-635` (serializer emits a bare `>` for an empty paragraph inside a quote), `canonical.py:32-38, 76-81` (empty log ⇒ parse HEAD), `hydrate.py:74`, `flip.py:202-204` (flag committed **before** the projection is built), `web/api/workspaces.py:318-331`, `collab_ws.py:153-210` (no `except ValueError`; the socket dies 1011, the provider gives up after 12).

**Defect.** Tiptap's Blockquote is `block+`, so "> " (input rule or toolbar) then moving on leaves `blockquote[paragraph()]`. The checkpoint commits `>\n`. After the next truncation (branch switch — and the target branch was founded from that same HEAD — or kill-switch handover) `load_or_hydrate` raises, every read-model call raises (draft GET, export, annotations, turns, problems, evaluator), git-first save is severed (409). For a git document that already holds a bare `>` (agent-written, or pre-collab), the **first** read commits the flag and then raises — there is no path back.

**RAN** `probe_crdt_shapes.py`:
```
CASE: D1b user made a blockquote and left it empty
checkpoint commits: '## Aims\n\nintro\n\n>\n\nafter\n'
REHYDRATE RAISED: ValueError Invalid content for node blockquote: <>
```
**RAN** scratch pytest through the real draft route (`client_authed_owner.get(/api/workspaces/{wid}/draft)` on `"# T\nintro\n## Body\nA plain claim.\n\n>\n\nmore\n"`):
```
DRAFT GET x2 -> ['ValueError: Invalid content for node blockquote: <>', 'ValueError: Invalid content for node blockquote: <>']
collab flag committed: True | git-first save now severed: True     1 passed in 3.83s
```
Also **READ**: a proposal `new_text` with the same shape makes `kernel_accept_prose` raise `ValueError` at `kernel.py:284`, which is not in `_CLEAN_ACCEPT_ERRORS` (`proposals/kernel_accept.py:45-61`), so a pre-mutation failure evicts the whole room (every client 1011).
**Fix shape.** Synthesize an empty paragraph for an empty blockquote in `_block_to_pm` (the list-item rule at `:279-280` already does this); add the same for a doc/quote whose only content was a reference definition (`D2`, `D3` in `probe_bridge.py` raise the same way). Then the systemic guard: `checkpoint_artifact` should dry-run `markdown_to_pm(md)` before `write_and_commit_files` and log a `collab.checkpoint_nonroundtrip` event, and `switch_branch` step 5→6 must refuse to truncate when the just-committed HEAD does not rehydrate.

###### F4 — MEDIUM · a code block whose text contains a ``` line is emitted with a 3-backtick fence; every later section is swallowed on rehydration and every accept on the doc fails until it is edited
`markdown_bridge.py:628-630` (fixed 3-backtick fence; contrast `_wrap_inline_code` at `:562-586` which sizes its delimiter to the body), `sectionizer.py:18` (`_FENCE_RE = ^\s*```` toggles on any ```-prefixed line), `yjs_bridge.py:313-339` (the block/line partition check).

**RAN** `probe_crdt_shapes.py`:
```
CASE: B1b code block whose text contains ONE line of ```, followed by another section
checkpoint commits: '## Methods\n\n```\nrun:\n```\nfoo\n```\n\n## Aims\n\nAims body.\n'
split_sections headings: ['', 'Methods']            <- "Aims" is gone for the sectionizer
rehydrated PM: [... {'type': 'code_block', 'content': [{'text': '\n## Aims\n\nAims body.'}]}]
```
and `probe_bridge.py` B1 (a legal 4-backtick source fence) is not idempotent: the second pass turns `inner` into a paragraph. Because the block partition still sees two sections while `split_sections` sees one, `apply_pm_section_to_fragment` raises `SectionMapDivergenceError` on every subsequent accept/citation insert. **Fix shape.** Fence length = longest backtick run in the body + 1 (≥3), in both the serializer and `_FENCE_RE` (which must then track the *opening* fence length, as CommonMark does).

###### F5 — MEDIUM · a trailing hard break (Shift-Enter, then Enter) becomes a literal backslash; a hard break inside a heading renames the section and splits it
`markdown_bridge.py:603-606` (`hard_break` → `\\\n` regardless of position).

**RAN** `probe_crdt_shapes.py`:
```
C1b: checkpoint commits '## Aims\n\nline one\\\n\n\nnext para\n' -> rehydrated text 'line one\\' -> re-serialized 'line one\\\\'
C2b: checkpoint commits '## Aim 1\\\nsubtitle\n\nbody\n' -> heading 'Aim 1\\' + paragraph 'subtitle'
```
CommonMark ignores a backslash at the end of a block, so the break is unrepresentable there; in a heading it cannot exist at all. **Fix shape.** Drop a block-final `hard_break`; inside a heading degrade it to a space (as `in_cell` already does).

###### F6 — MEDIUM · `create_workspace_branch` founds the branch on the last *checkpoint*, not on the live document, so a switch to it shows the user an older draft
`src/call_manager/proposal_assist/branches.py:309-416` (never calls `checkpoint_artifact`; `git branch <name> HEAD` at `:397`), `:216-224` (the *switch* checkpoints only the branch being **left**), `frontend/src/cockpit/BranchBar.tsx:91` (the bar's "Create new branch…" sends `fromSha` undefined ⇒ HEAD), `:140-141` (the switch modal says "Unsaved keystrokes will be lost", which is the wrong warning — they are checkpointed on the source).

**READ.** Checkpoints happen on last-leave, at `COLLAB_CHECKPOINT_MIN_TAIL` rows, on accept, or on "Save a version"; between them HEAD lags the room. Edit → "Create new branch…" → switch: the source gets the edits at step 5, the target is hydrated from a HEAD that predates them, and nothing in the UI says why the new branch is missing the last minutes of work. The `2026-08-30` worktrees design notes "a test explicitly pins that it copies nothing" and defers a real fork to a later stage. **Fix shape.** With `from_sha=None`, checkpoint both artifacts from the log under the mutex the function already holds before `git branch`; or make the bar's create pass the tip returned by a preceding "Save a version".

###### F7 — MEDIUM (systemic) · the checkpoint commits whatever the serializer emits with no round-trip self-check, so every construct above is canonicalised or broken silently
`checkpoint.py:205-212`. **READ.** `md = serialize_doc_to_markdown(doc)` goes straight to `write_and_commit_files`. A `markdown_to_pm(md)` dry run plus a `_canonical(...) == ydoc_to_pm_json(doc)` comparison at that point would have surfaced F2–F5 at commit time as an event instead of at the next branch switch. Further silent canonicalisations found (**RAN** `probe_bridge.py`): fence info string dropped (`B2`, the JS schema has no `language` attr either, so this only hits agent/git-authored fences); `&amp;`/`&copy;` decoded to `&`/`©` (`E1`); a pasted paragraph with 4 leading spaces becomes a code block on rehydration, turning any citation atom inside it into literal marker text (`E2`); a heading's trailing ` #` is stripped (`E4`).

###### F8 — LOW · `src/call_manager/proposal_assist/collab/room.py.orig` is a tracked, shipped copy of the module
Tracked since `355e8107`; it still documents 1013 (not 4409) as the branch-switch close code and imports `pycrdt`. Delete it.

##### 2. Checked and sound
- **Frame cap / bounded reader** — **RAN** `probe_frame_cap.py`: 8 frame shapes (STEP1/STEP2/UPDATE/AWARENESS, each with an unbounded continuation-byte varuint and with a *valid* length prefix followed by 999,995 adversarial body bytes) × writer and viewer = 16 doors, all **≤ 1.4 ms** at exactly 1,000,000 bytes. No `read_message`/`read_var_uint` import anywhere under `src/` except `protocol.py`'s own bounded reader. **Mutation** `room.py:189` (`P.guard_sync_frame(message)` → `pass`), then `test_collab_room.py` + `test_collab_protocol.py`: **RED** — `test_a_frame_at_the_ws_cap_cannot_freeze_the_room[step1_read_only_viewer]` fails after 105.96 s, so the gate pins the call site, not just the helper. Reverted.
- **Compaction vs a concurrent append** — `compact_snapshot` (`store.py:160-199`) overwrites only `min_id` and deletes explicit ids; `test_compact_snapshot_does_not_clobber_unlisted_rows` inserts a third row and asserts it survives (not a hollow shape).
- **Retire / switch while a persist is in flight** — `_retire_room` takes `room.lock` to set the tombstone (1 s bound) and reports `locked=False`, which `switch_branch:209-213` turns into `BranchBusy`; retirement ignores it by design. `test_reports_locked_False_when_room_lock_is_held` pins it.
- **Viewer (`can_write=False`)** — STEP1 answered, STEP2/UPDATE dropped before decode (`room.py:202`) and re-checked under the lock (`:220`), awareness relayed with per-user binding quota (64) and bind-after-accept; nothing else is mutated.
- **`actor_id` attribution** — the route persists `state.user_id`; the kernel persists the approver; compaction's `merged_actor_id` sits at/below the watermark so it never re-triggers authorship. (F1 is the one way a user is recorded as author of text they did not write.)
- **Empty-update / malformed-update handling** — `b"\x00\x00"` dropped, `apply_update` failures dropped before persist, `PersistError` → evict + reconnect-from-log.

##### 3. Prior findings touched
- Unbounded varuint P0 (third pass §0, P0-1) — **fixed at f92a0fe3**, re-verified above at the call site.
- "A branch read can pair the old name with the new bytes" (whole-project review, Medium) — still open; not re-derived.
- 4409 reconnect storm has no cap (spec 2026-09-01 Bug 2) — still open in `useCollabDoc.ts`; F1 is a different path (non-4409 reconnect).
- `apply_pm_section_to_fragment` delete-then-reinsert / pycrdt `insert(0)` scramble (memory, 2b98ce86) — fixed; the front-insert case now raises `SectionMapDivergenceError` (`yjs_bridge.py:364-375`).
- WS TEXT frame `KeyError` (third pass §5) — not re-examined.

##### 4. Not read
`collab/ws_auth.py` beyond its exports, `frontend/src/collab/useCollabDoc.ts`, the citation-side callers of `apply_citation_to_room` (`citations/collab_cite.py`, `human_cite.py`), `analysis/insert.py`, `gitio.checkout_branch`/`write_and_commit_files` bodies, `tests/proposal_assist/collab/test_collab_checkpoint*.py`, `test_collab_kill_switch*.py`.

##### 5. Leads
- `hydrate.read_head_markdown` reads the **working tree** file, not `git show HEAD:` — a crashed `write_and_commit_files` (write done, commit not) would seed the room from uncommitted bytes.
- Select-to-cite anchors are matched against the *serialized* markdown (`kernel.py:148-153`); text the user sees as "p value" is `*p* value` there, so anchors inside marked or escaped runs may fail with `CiteStale` (409). Not probed.
- Paragraph text that begins with `> `, `1) `, `---`, or `| ` (pasted, or typed after backspacing an input rule) is not escaped by `_escape_text` (`markdown_bridge.py:371`) and re-parses as a block construct on rehydration; only structure changes, no text loss. `## ` typed as text becomes a *section boundary* for the kernel immediately (`_block_starts_section`).
- Two adjacent `ordered_list` blocks with `order=1` merge into one list on rehydration (the second loses its restart).
- `markdown_to_pm(new_text)` failures in the kernel (F3's second half) evict the room for a pre-mutation error; `ValueError` belongs in `_CLEAN_ACCEPT_ERRORS`, or better, the parser should not raise on editor-producible shapes.


## Appendix A7 — React SPA

#### A7 — Frontend (React 19 + Vite SPA) adversarial review

Repo: call-monitor @ 8f44a35e, worktree `.claude/worktrees/agent-a4eed7107bb176f9b`.
Baseline: `pnpm exec tsc --noEmit -p .` clean; the two test files I mutated were 17/17 green before mutation.
Six RAN proofs: 3 line/pattern mutations (A, B, C) and 6 rendered/hook probes (1–6) in two scratch files (deleted; tree clean).
All line numbers are in-file (not the concatenated listings).

##### 1. Ranked findings

###### F1 — Medium/High · a branch switch made by ANOTHER member leaves every cockpit read-model on the old branch
`frontend/src/collab/useCollabDoc.ts:214-235` (`onDocumentReplaced`), `frontend/src/collab/collabProvider.ts:267-274`; server side `src/call_manager/proposal_assist/collab/room.py:760-780` (`prepare_branch_switch` closes BOTH rooms with 4409 for every client), `collab_ws.py:176`.

Defect: the 4409 handler rebuilds the Y.Doc and reconnects (the editor then shows the new branch's text) but touches no TanStack cache. Nothing else can: `useEventStream` has exactly one consumer (the per-job chat stream, `CockpitChat.tsx:42`), `["branches"|"draft"|"spec"|"versions", id]` have no interval, `refetchOnWindowFocus:false`, `staleTime` 20 s, and the observers stay mounted so no remount refetch ever happens. Only the user's OWN switch invalidates (`api/branches.ts:227-233`).

Failure scenario: A and B both in `/cockpit/W`. B switches `main → alt`. On A's screen: the editor body becomes `alt`'s text (correct), the status-bar pill `StatusBar.tsx:120-126` still says `main`, the Versions pane (`useVersions`) lists `main`'s history with `main`'s tip pill, `BranchBar.tsx:69-70` computes `others` from stale `current` and offers "Switch to alt" (the branch A is already on), and the selection toolbar's client-side uniqueness check `TiptapSelectionToolbar.tsx:73-84` runs the LIVE claim against `main`'s `sections` — `sections[claim.sectionIndex]` is a different/undefined section, so Ask/Edit/Cite silently disable (or mis-anchor when the text happens to match). Nothing tells A that the branch changed.

**RAN** (probe 6, real `useCollabDoc` + FakeSocket + QueryClient with the three keys seeded, `vi.spyOn(qc,"invalidateQueries"/"removeQueries")`, `drop(WS_DOCUMENT_REPLACED)`):
```
✓ A7 probe 6: a remote branch switch (4409) rebuilds the Y.Doc but touches no query cache
  FakeSocket.instances length 2 (reconnected); invalidate/remove never called;
  getQueryState(["branches","w"]).isInvalidated === false; branches.current === "main"; draft.head === "h-main"
```
Fix shape: give `onDocumentReplaced` (or `CollabEditor`, which has `useQueryClient`) the same four invalidations `useBranchMutation` does (`branches.ts:227-233`); or have the server include the new branch name in a `branch_changed` frame and invalidate on it. Also reset the toolbar's `claim` on doc generation change.

###### F2 — Medium · session expiry mid-turn: the SSE 401 classification is thrown away; the chat shows "Assistant is working…" over a closed stream
`frontend/src/api/events.ts:60-85` (probe → `es.close(); opts?.onExpired?.()`), `frontend/src/cockpit/chat/CockpitChat.tsx:42-60` (the ONLY caller; passes `{ resetCursorOnUrlChange: true }` and neither `onExpired` nor `onNoAccess`).

Failure scenario: owner submits a turn; admin revokes the session (epoch bump) while the stream is open. Three `onerror`s → `/api/me` probe → 401 → the EventSource is closed (auto-reconnect defeated), the callback is `undefined`, `jobId` stays set, `done` never arrives: Composer disabled, "Assistant is working…" + Cancel shown, no redirect from THIS path. The user is rescued only by the unrelated Problems poller (`api/problems.ts:42`, 15 s, mounted by BottomPanel) hitting `unwrap`'s 401 chokepoint — so the stall is ≤15 s on an active cockpit, but the events.ts 401/403 branch is dead code with a dead `Opts` API (`events.ts:5-6`).

**RAN** (probe 1, real CockpitChat, FakeES, `fetch` stubbed → `{status:401}`, `location.assign` spied):
```
✓ A7 probe 1: closes the stream, never redirects, and leaves the composer disabled
  fetch("/api/me",{credentials:"same-origin"}) called; instance.close() called; assign NOT called;
  textarea disabled; "Assistant is working…" still rendered
```
Coverage: `events.test.ts` has 3 cases (lines 6, 31, 70), none reaching `onerror`; `CockpitChat.test.tsx` never mentions `onExpired`.
Fix shape: pass `onExpired: redirectToLogin` and `onNoAccess` from CockpitChat (or make `useEventStream` default to `redirectToLogin`), and clear `jobId` on close so the composer re-enables.

###### F3 — Medium · `Cockpit` renders "Loading…" forever when cockpit-meta errors (deleted / non-member / 404 workspace)
`frontend/src/routes/Cockpit.tsx:69` — `if (meta.isLoading || !meta.data) return <p>Loading…</p>` has no `isError` arm; `frontend/src/api/workspace.ts:7-18` (`useCockpitMeta`) does not set `retry:false`, so the 404 is retried 3× (several seconds) before even settling into the same spinner.

Failure scenario: a member follows a link to a workspace that was soft-deleted (admin retention) or they were removed from → 404 → permanent "Loading…", no alert, no way out but the rail. `DraftPanel.tsx:33-40` has the proper error arm but is never reached.

**RAN** (probe 2, `useCockpitMeta` spied → `{isLoading:false,isError:true,data:undefined}`):
```
✓ A7 probe 2: shows Loading… and no alert for isError   (getByText("Loading…") present; queryByRole("alert") null)
```
Coverage: `Cockpit.test.tsx` never sets `isError` (grep: only `error: undefined`).
Fix shape: an `isError` branch with `role="alert"` ("Workspace not found or no longer shared with you"), `retry:false` on the meta query.

###### F4 — Medium · create-workspace / create-profile never invalidate their list; with `staleTime: 20_000` the list stays pre-create with NO refetch
`frontend/src/api/workspace.ts:53-62` (`useCreateWorkspace`, comment "the list refetches on next mount" is false inside `staleTime`), `frontend/src/api/profiles.ts:33-37` (`useCreateProfile`), `frontend/src/App.tsx:16` (`staleTime: 20_000`, `refetchOnWindowFocus:false`).

Failure scenarios: (a) Workspaces list → call → "Start drafting" → create → cockpit → rail "Workspaces" within 20 s: the new workspace is absent. (b) `/profiles` shows "No profiles yet" (`ProfilesList.tsx:32-39`) → "Create your first profile" → save (`ProfileNew.tsx:38` navigates to `/edit`) → "← All profiles": STILL "No profiles yet" with a "Create your first profile" CTA. (c) the dashboard's Profile facet (`CallsFilters.tsx:63-66`, from `["calls", params]`) also lacks the new profile inside the window.

**RAN** (probe 3, real `WorkspaceList` + real `useCreateWorkspace`, App's QueryClient defaults, GET counter):
```
✓ A7 probe 3: the list is served from cache: the new workspace is absent and no refetch runs
  listCalls === 1 after remount; "No workspaces yet" rendered; "Brand new" absent
```
**RAN** fix-shape flip (mutation C, pattern-anchored; diff shown by `git diff`): adding `const qc = useQueryClient()` + `onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] })` to `useCreateWorkspace` turns probe 3 red — `AssertionError: expected 2 to be 1` (the list refetched). Reverted.
Fix shape: the same one-liner in both hooks; for profiles also invalidate `["calls"]` so the facet refreshes.

###### F5 — Medium · BranchBar swallows switch/delete failures — no `onError`, no `isError` rendering
`frontend/src/cockpit/BranchBar.tsx:147-156` (switch: `switchBranch.mutate(confirm.name); closeSwitch();`), `:168-177` (delete). `useBranchMutation` (`api/branches.ts:223-236`) has only `onSuccess`.

Failure scenario: the server ABORTS a switch when it cannot linearize against in-flight persists (`room.py` docstring: "Abort the SWITCH, never the teardown") or the branch is gone → 409/404 → the confirm modal closes and the bar shows the old branch with no message. The user reads "nothing happened" and retries; every attempt is silent. A delete failure is equally silent.

**RAN** (probe 4, `useSwitchBranch` spied to `{isError:true, error: ApiError("conflict", "…aborted…")}`):
```
✓ A7 probe 4: isError on the switch mutation produces no alert and no message
```
Coverage: `BranchBar.test.tsx:20-37` mocks all three mutations as bare `vi.fn()` with no error state — the error path cannot be exercised by that harness.
Fix shape: render `switchBranch.isError`/`deleteBranch.isError` with `role="alert"` next to the bar, or `onError` → a note; keep the modal open on error.

###### F6 — Medium · the collab placeholder says "retrying" in two states where nothing retries; the status bar says "Read-only" over a dead socket while the surface stays typeable
`frontend/src/collab/CollabEditor.tsx:47-48` (`unreachable` includes `offline-permanent` and `closed-policy`) and `:72` ("Could not reach the live document — retrying."); provider: `collabProvider.ts:266` (`1008/1009 → closed-policy; return` — no reconnect), `:284-295` (`offline-permanent` after 12 tries — no reconnect). Post-sync: `collabStatus.ts:10` maps `closed-policy → "collab-readonly"` → StatusBar label "Read-only" (`StatusBar.tsx:23`), while `CollabEditor.tsx:97` `editable: props.canWrite` is set once and the editor is keyed only on `[props.doc]` (`:106`) — it stays typeable.

Failure scenario A: expired cookie/non-member on open → pre-accept 1008 arrives as 1006 → 12 retries (241 s) → `offline-permanent` → placeholder still says "retrying" indefinitely. Scenario B: owner removes a member mid-session → server 1008 → if not yet synced: "retrying" forever; if synced: the bar says "Read-only", the member keeps typing into a Y.Doc with no socket (`send()` no-ops), and loses it on reload.

**RAN** (mutation B, `CollabEditor.tsx:72` "retrying." → "gave up; reload the page."): `CollabEditor.unsynced.test.tsx` + `CollabEditor.test.tsx` + `CollabEditor.connecting.test.tsx` stay 11/11 green — the retry claim is unpinned (the closed-policy test only matches `/could not|can't|cannot|reach/i`, `unsynced.test.tsx:79-86`). Reverted.
Fix shape: split `unreachable` into `retrying` (offline) vs `terminal` (offline-permanent, closed-policy) with distinct copy; map `closed-policy` to `collab-disconnected` unless the close was specifically a permission downgrade; call `editor.setEditable(false)` on terminal states.

###### F7 — Low · the "viewport-aware bubble menu" test cannot fail on `shouldShow`, and mocks the BubbleMenu it claims to test
`frontend/src/collab/TiptapSelectionToolbar.tsx:37-38`; test `TiptapSelectionToolbar.test.tsx:24-26` (`deriveClaimAnchor` mocked to a constant), `:37-42` (BubbleMenu replaced by a `<div>`), `:124-139`.

**RAN** (mutation A, lines 37-38 → `= () => true`): 11/11 green. The assertion `expect(props.shouldShow?.({state:{}})).toBe(true)` is true for both the real predicate and "always show" because the mocked anchor is never null. The `options` `toMatchObject` does pin the constants, but no positioning/flip/hide behaviour of the real `BubbleMenu` (commits 977b25ab, 648e58dc) is exercised anywhere in vitest — only `e2e/collab.spec.ts` could.
Fix shape: have the test flip `deriveClaimAnchor` to return `null` and assert `shouldShow` is `false`; keep the real menu for one e2e assertion.

###### F8 — Low · dead invalidation key
`frontend/src/api/annotations.ts:47` invalidates `["proposals", id]`; no `queryKey` in `src/` starts with `"proposals"` (the proposal list is `["proposed-changes", id, artifact]`, `api/proposals.ts:25`). **READ** (grep `'"proposals"'` → one hit, this line). Harmless today (the insert route writes a citation, `web/api/annotations.py:226-266`, not a proposed change) but it is the "stringly key" failure shape the prior review flagged; either delete it or point it at `"proposed-changes"`.

###### F9 — Low (flag-off path) · `onSave` writes the raw buffer over the refetched canonical section
`frontend/src/editor/ArtifactSectionEditor.tsx:201-233`; `api/editor.ts:48` (`onSuccess: () => invalidateArtifact(...)` — TanStack awaits `onSuccess` before `mutateAsync` resolves, and `invalidateQueries` resolves after the refetch); server `editing/save.py:290` appends `\n` to any non-last section.

**RAN** (probe 5, real `useDraft` + `useSaveSection`): at the moment `mutateAsync` resolved, GET had been called twice and the cache held `"## A\nraw\n"` (canonical, sha `s1`); the component's `setQueryData` then stored `"## A\nraw"` under sha `s1`. Harm is small (text silently gains a newline on the next unrelated refetch; sha/text pairing is inconsistent in cache) and this is the CM6 path (collab is on in prod). Fix shape: drop the `setQueryData` (the awaited refetch is authoritative) or write `result`-derived text only.

###### F10 — Low · accessibility gaps on the main flows
- `frontend/src/collab/EditorToolbar.tsx:117-129, 132-163`: Bold/Italic/Strike/Code/list/quote are toggle buttons whose state is `variant="filled"` only — no `aria-pressed`; a screen-reader user cannot tell whether Bold is on.
- `frontend/src/cockpit/chat/Composer.tsx:76-85, 88-103`: the message `<textarea>` and the reference `<select>` have no label (placeholder/first option only); the send button is labelled.
- No focus management on route change anywhere (grep: only `autoFocus` on Login/Register); after the rail navigates, focus stays on the rail item and the new `<main>` is not announced. Mantine `Tabs`/`Menu` themselves are keyboard-reachable (checked: `ActivityBar.tsx` carries `aria-label` + `aria-current`; `VersionGraph.tsx:91-104` explicitly uses a controlled Popover for focus).
**READ**.

##### 2. Checked and sound
- 401 chokepoint `api/client.ts:40-44` covers every query/mutation; `403 account_inactive` navigates only to backend hard-coded paths; sign-out is a full-page `location.assign("/login")` (`TitleBar.tsx:39`) so the in-memory cache dies with it — no cross-user cache bleed on a shared machine.
- `Login.tsx:40` `navigate(data.redirect)` is fed by `auth/redirects.py:11-36` `safe_next` (single leading `/`, no `//`, no backslash, no scheme/netloc, byte cap).
- No `switch`/`default:` in non-test source (grep); `collabStatus.ts:13-18` uses the `never` pattern.
- `invalidateArtifact` prefix-matches `["versions", id]` (both artifacts) and `["branches", id]`; every own-branch mutation invalidates the full four-key set; `useSaveVersion` now routes through it (prior 4.10 fixed).
- `AppShell.tsx:59-60` keys the cockpit main + right pane by workspace id, so `/cockpit/A → /cockpit/B` remounts (no stale jobId/streamed turns); `DraftPanel`/`SpecPanel` key their editors by id too.
- `useHistoryNav.selectAndOpen` computes from `prev` inside the single `setSp` write (the two-writer clobber is pinned); `viewHref` preserves the comparison params.
- `EditorToolbar.tsx:44,86-91` link-scheme allowlist; `CodeMirrorEditor` heading-delete guard mirrors the server's H2 regex.
- `BranchBar` inert-when-unreadable state is pinned by a real test (`BranchBar.test.tsx:172-215`).
- `TiptapSelectionToolbar` ack-retry freezes the payload (pinned) and the non-unique-anchor refusal is real (`:80-84`); the server relocates anchors on the collab path (`turns/selection.py:40-80`), so stale `head`/`sectionSha` do not mis-target on that path.
- `useCollabDoc` bounds 4409 replacement loops (`MAX_DOCUMENT_REPLACEMENTS = 8`, `:5-7`) — prior whole-project Medium is fixed.
- `tsc --noEmit` clean at 8f44a35e — prior whole-project High (`base_branch` propagation) is fixed; `SaveVars` carries `base_branch` (`ArtifactSectionEditor.tsx:20-28`).
- `WorkspaceSettingsModal` delete invalidates `["workspaces"]` (`settings.ts:86`) and navigates.

##### 3. Prior findings touched
- third-pass §4.8 `synced` latch on SYNC_UPDATE — fixed (`collabProvider.ts:245`, `carriesContent`).
- third-pass §4.9 reconnect give-up with "reconnecting…" — fixed at the provider (`collabProvider.ts:294` sets `offline-permanent`); the PLACEHOLDER copy still lies (F6 above is the residual).
- third-pass §4.10 Save-a-version never invalidates Versions — fixed (`api/invalidateArtifact.ts`).
- whole-project High `base_branch` not wired through the editor — fixed (tsc clean).
- whole-project Medium 4409 reconnect unbounded — fixed (`useCollabDoc.ts:5-7`).
- sec-perf Medium `client.ts:54` server-provided path via `location.assign` — still as described; low (backend targets are two hard-coded local paths).
- sec-perf `useSettled` 200 ms poll, EditorToolbar per-transaction rerender, Problems 15 s poll — unchanged; not re-measured.

##### 4. Not read
`routes/Admin*.tsx` bodies, `routes/Settings.tsx`, `ProfileEdit/ProfileDetail` bodies, `Register.tsx`, `CallReadingPane`, `CallRow`, `DashboardHeader`, `cockpit/{ReviewPanel,ScorecardPanel,ProblemsPanel,ClaimsPanel,ActivityPanel,CitationsPanel,CitationComposer,ProposalReview,SelectionToolbar,SourceViewer,RubricEditor,WorkspaceSetup,BottomPanel,InboxPanel,MetadataModal}`, `panes/{Criteria,Members,References}Pane`, `VersionLane/versionLanes`, `diff.ts`, `collab/{schema,CitationNode,TableControls,selectionAnchor,collabColor}`, `collabProvider.ts:1-239`, `editor/{citations,InlineProposalReview}`, `components/*`, `dev-proxy`, the three Playwright specs.

##### 5. Leads (no proof)
- `HistoryPanel.tsx:150 + 175` and `:191 + 227` can render together: `useVersion` keeps default `staleTime` 20 s (`api/versions.ts:52-68`), and a failed BACKGROUND refetch leaves `data` set with `isError` true (TanStack v5) → both the `<pre>`/diff and "could not be read" + Clear button. Needs a failing refetch to reproduce.
- Collab mode Outline/jump mismatch: `panes/OutlinePane.tsx:14-20` indexes git-head sections while `useSectionJump.ts:98-117` counts H2 nodes in the LIVE Y.Doc — after an unsaved heading insert, clicking Outline item N scrolls to a different heading.
- `WorkspaceCreate.tsx:32-42` awaits `mutateAsync` without try/catch → an unhandled rejection in the console on failure (the `isError` banner still renders).
- `CallsFilters.tsx:86` derives the Profile `Select` value from `data.effective.profile_id` under `keepPreviousData`, so after a profile change the control shows the OLD profile until the new page lands.
- `Composer.tsx:131` prints `ApiError.message` = `"<code>: <message>"` raw (e.g. `disclosure_required: …`).
- `TiptapSelectionToolbar.tsx:55-70`: `claim` is recomputed only on `selectionUpdate`; a remote edit that changes the selected text without moving the selection leaves `claim.anchor` stale (server relocation covers the collab path; the client's `anchorUnique` gate still runs on stale text).


## Appendix A8 — mutation sweep over the twelve highest-stakes invariants

#### A8 — Mutation sweep over the 12 highest-stakes invariants

Worktree: `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-a14c99091349fb4f7` @ 8f44a35e.
Venv verified: `uv sync --python 3.12 --frozen` then `call_manager.__file__` printed a path
inside the worktree. Every mutation was applied with `sed -i '<N>s/…/…/'` by line number,
the line was re-read after `sed` (BEFORE/AFTER printed), `diff` confirmed exactly one hunk,
only the named test files ran (`uv run pytest <files> -q -p no:cacheprovider -x`), and the
file was restored byte-identically (sha256 before == after, printed) then `git checkout --`
and `git status --short` (empty) before the next batch. Runner:
`scratchpad/run_mut.sh`; per-mutation pytest output in `scratchpad/mutlogs/<id>.log`.

##### 1. Result table (all RAN)

Invariants as numbered in the task; 5, 8, 9, 11, 12 got two mutations each because
each has two distinct enforcement points.

| # | Invariant | Mutated line (`sed -i`) | Test file(s) named in (b) | Result | Test that went red |
|---|---|---|---|---|---|
| 1 | CSRF Origin mismatch | `src/call_manager/auth/csrf.py:114` — `114s/if _hostport(origin) != self._expected_host:/if False:/` | `tests/test_auth_csrf.py` | **RED** (1 failed, 2 passed, 4.1s) | `test_post_with_mismatched_origin_is_rejected` (named file) |
| 2 | `require_owner` on a write route | `src/call_manager/web/api/files.py:86` (upload) — `86s/require_owner(/require_member(/` | `tests/test_api_files.py` | **RED** (1 failed, 1 passed) | `test_api_files_upload_viewer_is_403` (named file) — discriminates viewer-vs-owner, not just stranger, so the owner→member downgrade is caught |
| 3 | `require_member` on a read route | `src/call_manager/web/api/files.py:60` (list) — `60s/require_member(workspace_id=workspace_id, user=user, session=session)/pass/` | `tests/test_api_files.py` | **RED** (1 failed, 5 passed; got 200) | `test_api_files_list_stranger_403` (named file) |
| 4 | Session-epoch check | `src/call_manager/auth/dependencies.py:44` (the brief said `web/dependencies.py`; it lives in `auth/`) — `44s/if user.session_epoch != data.session_epoch:/if False:/` | `tests/test_auth_dependencies.py` | **RED** (1 failed, 2 passed) | `test_stale_epoch_cookie_rejected` (named file) |
| 5a | SSRF: blocked-IP predicate | `src/call_manager/sources/types.py:99` (`_ip_is_blocked`) — `99s/return (/return False and (/` | `tests/test_sources_types.py` | **RED** (1 failed, 3 passed) | `test_is_safe_fetch_url_rejects_literal_internal_ips` (named file) |
| 5b | SSRF: adapter fails CLOSED on refusal | `src/call_manager/sources/eu_portal.py:181` — `181s/if api_transport is None:/if False:/` | `tests/test_adapter_ssrf_fail_closed.py` | **RED** (1 failed) | `test_a_refused_ssrf_target_aborts_instead_of_connecting_unpinned[eu_portal]` (named file) |
| 6 | Per-workspace daily token cap | `src/call_manager/proposal_assist/turns/caps.py:131` — `131s/if used_in >= cap_in:/if False:/` | `tests/proposal_assist/test_turns_caps.py` | **RED** (1 failed, 1 passed) | `test_check_daily_token_cap_over_input_limit_returns_not_allowed` (named file) |
| 7 | Global daily token cap | `src/call_manager/proposal_assist/turns/caps.py:186` — `186s/if used_in >= cap_in:/if False:/` | `tests/proposal_assist/test_global_cap.py` | **RED** (1 failed, 1 passed) | `test_submit_raises_global_cap_exceeded` (named file) — pinned at the `submit_user_turn` level, not only the predicate |
| 8a | `acquire_commit_mutex_at_generation` refuses stale generation | `src/call_manager/proposal_assist/fencing.py:168` — `168s/if row\[1\] != generation:/if False:/` | `tests/proposal_assist/test_fence_restore_hole.py` | **RED** (1 failed, 1 passed; `DID NOT RAISE WorkspaceGenerationStale`) | `test_generation_primitive_refuses_the_stale_writer_after_restore` (named file) |
| 8b | `live_at_generation` predicate | `src/call_manager/proposal_assist/fencing.py:111` — `111d` (drops the `lifecycle_generation == generation` clause) | `tests/proposal_assist/test_fencing.py` | **RED** (1 failed, 1 passed) | `test_does_not_match_a_different_generation` (named file) |
| 9a | `_MAX_FRAME_BYTES` WS frame cap | `src/call_manager/proposal_assist/web/collab_ws.py:251` — `251s/if len(message) > _MAX_FRAME_BYTES:/if False:/` | `tests/proposal_assist/collab/test_collab_ws_route.py` | **DETECTED BY HANG** (8 passed, then test 9 blocked >595s; killed, RC=143) | `test_oversized_frame_closes_the_connection` (named file) — see Finding 1 |
| 9b | Bounded varuint reader (10-byte cap) | `src/call_manager/proposal_assist/collab/protocol.py:76` — `76s/for _ in range(10):/while True:/` | `tests/proposal_assist/collab/test_collab_protocol.py` | **RED** (1 failed, 8 passed, 50.7s; `< 2.0s` bound failed at ~48s) | `test_read_awareness_client_ids_rejects_overlong_varuint_promptly` (named file) |
| 10 | `require_admin` | `src/call_manager/auth/dependencies.py:54` — `54s/if not user.is_admin:/if False:/` | `tests/test_auth_dependencies.py` | **RED** (1 failed, 3 passed) | `test_require_admin_rejects_non_admin` (named file) |
| 11 | `verify_password` mismatch → True | `src/call_manager/auth/passwords.py:45` — `45s/return False, False/return True, False/` (line 45 only; line 47 untouched) | `tests/test_auth_passwords.py`, `tests/test_api_auth.py` | **RED** twice: unit (1 failed) and, re-run against the route file alone, HTTP (1 failed, 6 passed) | `test_hash_and_verify_roundtrip`; `test_login_failures_identical_400_no_cookie` (both named) |
| 12a | OIDC `email_verified` | `src/call_manager/auth/oidc.py:140` — `140s/if claims.get("email_verified") is not True:/if False:/` | `tests/test_auth_oidc.py` | **RED** (1 failed, 7 passed) | `test_rejects_bad_claims[over4]` = `{"email_verified": False}` (named file) |
| 12b | Email-domain allowlist | `src/call_manager/auth/domain.py:52` — `52s/return domain in normalized/return True/` | `tests/test_auth_domain.py`, `tests/test_web_oauth_google.py` | **RED** twice: unit (1 failed, 10 passed) and route-only re-run (1 failed, 2 passed; landed on `/auth/pending` instead of `/auth/error`) | `test_domain_allowed[alice@umu.se-allowed2-False]`; `test_callback_disallowed_domain_rejected` (both named) |

**Score: 16/16 mutations detected — 15 by a red test, 1 (9a) by a hang. 0 hollow gates
among the 12 invariants.** Every detection came from a file I named in (b); no mutation
had to be rescued by a test in an unnamed file, so none of these pins is "weaker than
claimed" in the file-location sense.

Two notes on the shape of the pins (not defects):
- 5b only exercised `eu_portal`; `-x` stopped before the `vinnova` parametrization. I did
  not mutate `vinnova.py:145` separately.
- 9b's detection is a **wall-clock assertion** (`< 2.0s`): it is real (the mutated parser
  stalled ~48s on 900 kB of continuation bytes), but it is the kind of pin that can flake
  under heavy CI load in the *other* direction (a slow box could push the bounded parser
  past 2s? — not measured; the bounded run took microseconds here).

##### 2. Findings (new; ranked)

###### F1 — Medium: the `_MAX_FRAME_BYTES` regression test detects by hanging, not failing
`tests/proposal_assist/collab/test_collab_ws_route.py:279-281`. **RAN.** With line 251 of
`collab_ws.py` disarmed, the server accepted the 1,000,001-byte frame and never closed, so
`wsconn.receive_bytes()` (line 281, inside `pytest.raises(WebSocketDisconnect)`) blocked
indefinitely. Evidence: log shows 8 dots (tests 1–8 passed) then silence for >595s; the
pytest process had `etimes=595` when I killed it. `pytest-timeout` is **not installed**
(`importlib.util.find_spec('pytest_timeout')` → `None`) and `pyproject.toml` has no
`timeout` setting. Failure scenario: a future refactor of the receive loop drops or
inverts the cap; CI does not show a red test named `test_oversized_frame…` — it shows a
job that ran until the runner's global timeout with no failing test id. The pin is
load-bearing, but its signal is the worst kind. Fix shape: add `pytest-timeout` as a dev
dep with a suite-wide `timeout = 120` in `[tool.pytest.ini_options]` (cheap, protects every
blocking-receive test), or at minimum wrap this receive in a bounded wait. Prior: the cap
itself is the f92a0fe3 / third-pass §2 fix — that fix holds; this is about the pin's
failure mode, which no prior review recorded.

###### F2 — Low: `test_reset_password_has_no_status_gate` is status-only for the one case it exists to pin
`tests/test_api_admin_users.py:342-348`. **READ.** Asserts only `== 200` for a *disabled*
user. Sibling `test_reset_password_bumps_epoch` (`:318-331`) checks
`verify_password(u.password_hash, …)` and the epoch bump — for an **active** user only.
Failure scenario: an implementation that keeps a status gate but answers a disabled user
with a 200 no-op (early return before the hash write) passes both tests. Hollow shape 4
(proves a weaker property: "responds 200", not "resets the credential"). Fix: copy the two
sibling assertions into this test.

###### F3 — Low: `test_reenable_already_enabled_noop` asserts nothing about the no-op
`tests/test_api_admin_ops.py:127-138`. **READ.** Only `.status_code == 200`. The preceding
test (`:118-124`) checks body and `digest_disabled_at is None` for the disable→enable case.
Failure scenario: a "reenable" that unconditionally stamps `digest_disabled_at = now()` (or
any write) on an already-enabled user passes. Shape 4. Fix: one `s.get(User, tid).
digest_disabled_at is None` after the call.

###### F4 — Low: `test_build_scheduler_includes_turn_reaper_job` is presence-only
`tests/test_scheduler.py:176-200+`. **READ.** Asserts `sched.get_job("turn_reaper") is not
None`. A job registered under that id with the wrong callable or interval passes. The reaper
logic has its own file (`tests/proposal_assist/test_turn_reaper.py`, not opened) — this test
is the *wiring* pin, and wiring is exactly what an id-only check does not prove. Shape 4.
Fix: assert `job.func` identity and the trigger's interval.

###### F5 — Low: `test_claude_adapter_constructor_accepts_tools_factory` has a decorative assertion
`tests/proposal_assist/test_tools_claude_adapter_wiring.py:5-19`. **READ.** `adapter is not
None` on a freshly constructed instance is always true; the test can only fail by raising.
An adapter that accepts `tools_factory=` and silently ignores it passes. Shape 2 (asserts
something already true). The sibling at `:21` checks `_tools_factory is not None` — presence,
not invocation. Fix: either drop the assert (make the smoke honest) or assert the factory is
called when a turn runs.

##### 3. Second pass — cheap grep + 10-test sample

`grep -rn "assert True\|assert .* is not None$\|pytest.skip\|xfail" tests/ | wc -l` → **241**.
Breakdown (RAN): `assert True` **0**, `is not None$` **241**, `pytest.skip` **0**, `xfail`
**0**. Test functions: 4,156 (grep) / 4,155 (AST walk). (The one `skipped` CLAUDE.md
mentions is a `skipif` on `ANTHROPIC_API_KEY`, which the pattern does not match.)

AST scan (`scratchpad/weak_asserts.py`, full listing in `scratchpad/weak_asserts.out`):
tests whose *only* assertions are `status_code` comparisons or `is not None`, with no
`pytest.raises` / mock `assert_*`: **194 / 4,155 (4.7%)** — 178 status-only, 7
not-None-only, 9 mixed. Most status-only tests assert a **4xx** (authorization/validation
denials) where the code *is* the property — several of those went red in §1
(`upload_viewer_is_403`, `list_stranger_403`, `require_admin_rejects_non_admin`). The
suspicious subset — assert only a **2xx** or only `is not None` — is **18**. I sampled 10 of
those 18:

| # | Test | Only assertion | Could pass on a broken impl? |
|---|---|---|---|
| S1 | `tests/proposal_assist/collab/test_collab_save_severance.py:82` `test_draft_save_works_when_only_spec_enabled` | `== 200` | **No, for its invariant** (200-vs-409 severance; siblings at `:51`, `:67`, `:114` pin the 409s). Would not notice a save that returns 200 and writes nothing — that belongs to the save tests, not this file. |
| S2 | `tests/proposal_assist/test_submit_turn_selection.py:164` `test_member_can_submit_selection_ask` | `== 200` | **No, for its invariant** (allow-pin; `:186`/`:208` pin the member 403s). Enqueue side-effect not checked here. |
| S3 | `tests/proposal_assist/test_tools_claude_adapter_wiring.py:5` | `adapter is not None` ×2 | **Yes** — F5. |
| S4 | `tests/test_api_admin_users.py:342` `test_reset_password_has_no_status_gate` | `== 200` | **Yes** — F2. |
| S5 | `tests/test_api_workspaces.py:811` `test_citation_source_member_can_read` | `== 200` | **No** (allow-pin; the owner test at `:790-809` asserts the full body and highlight offsets). |
| S6 | `tests/test_auth_csrf.py:24` `test_post_without_origin_or_referer_is_allowed` | `== 200` | **No** — the status is the entire property. |
| S7 | `tests/test_api_admin_ops.py:127` `test_reenable_already_enabled_noop` | `== 200` | **Yes** — F3. |
| S8 | `tests/test_review_remediation.py:433` `test_create_app_accepts_default_embedding_dim` | `app is not None` | **No** — the property is "does not raise" and the sibling at `:425-431` pins the raise; the assertion is decorative, not misleading. |
| S9 | `tests/test_scheduler.py:176` `test_build_scheduler_includes_turn_reaper_job` | `get_job(...) is not None` | **Yes** — F4. |
| S10 | `tests/test_api_admin_golden_set.py:143` `test_delete_absent_is_204_idempotent` | `== 204` | **No** — 204-on-absent is the property; the delete-present sibling (`:96-98`) asserts the row is gone. |

Sample verdict: **4 of 10 could pass on a broken implementation** (S3, S4, S7, S9), all of
the "weaker property" or "already true" shapes, all Low; 6 are sound for what they claim.
Extrapolating 4/10 over the 18-test suspicious subset suggests ~7 weak tests total in the
2xx/not-None class; the 176 4xx-only tests are largely correct by construction.

##### 4. Checked and sound
All 12 invariants above (16 mutations): CSRF Origin mismatch; owner-only write and
member-only read on `files.py`; session-epoch revocation; both SSRF layers (predicate and
adapter fail-closed); per-workspace and global caps (the global one at the
`submit_user_turn` boundary); Primitive A and Primitive B generation refusal; 1 MB frame
cap (by hang) and 10-byte varuint bound; `require_admin`; argon2 mismatch at unit and
login-route level; OIDC `email_verified`; domain allowlist at unit and callback level.
Also: the `-x` short-circuit never masked a second pin — the two two-file runs (11, 12b)
were re-run against the route file alone and went red there too.

##### 5. Prior findings touched
- Third-pass §4.1 SSRF fail-open → fixed 8a297db1; **pin is real** (5b red).
- Third-pass §4.12–4.14 three hollow gates (`client_unauth` etc.) → fixed 5ab49d90;
  the member/owner pins are real now (2, 3 red).
- Third-pass §2 / f92a0fe3 `_MAX_FRAME_BYTES` + varuint P0 → fixed; **pins are real**
  (9a hang, 9b red), but see F1 for the hang.
- Security review 2026-09-01 "Auth, sessions, CSRF — exemplary" → consistent with 1, 4,
  10, 11, 12a, 12b all red.

##### 6. Not read
`tests/proposal_assist/test_turn_reaper.py`, `tests/test_editing_save.py`,
`tests/test_api_turns.py` (cited as "elsewhere" for S1/S2/S9 side-effects — not verified);
`src/call_manager/sources/vinnova.py:145` (5b's twin, not mutated); the three review
documents beyond a keyword grep.

##### 7. Leads
- 9b's `< 2.0s` wall-clock pin: worth a note in the test that the bounded path measures in
  microseconds, so a CI flake here means the bound is gone, not that the box is slow.
- 178 status-only tests: a one-line rule ("a 2xx-only assertion needs one side-effect
  assertion") would close F2/F3 and the ~7 like them; the 4xx ones are fine.

Closing state: `git status --short` in the worktree is empty (confirmed after every batch
and at the end).


## Appendix A9 — operations and deployment

#### A9 — Operations / deployment review (call-monitor, master @ 8f44a35e)

Lens: can a hospital IT operator run this safely, and what happens on the bad days.
Main tree was READ-ONLY; every probe ran from the scratchpad with `env -i` so the real
`.env` was never loaded (each probe prints `.env present=False`). Container probes used a
throwaway `pgvector/pgvector:pg16` testcontainer (same image as `tests/conftest.py`).
Probe scripts: `scratchpad/probes/{boot_probe.py,container_probe.py,accesslog_probe.py}`.

##### 1. Ranked findings

###### F1 — High — a second process is tolerated by design, but only the scheduler is single-instance-safe
`src/call_manager/main.py:235-251,261-286`; `scheduler.py:102-149`; in-process state at
`proposal_assist/collab/room.py:971` (`MANAGER = RoomManager(...)`), `passes.py:205`,
`lifecycle.py:114`, `main.py:800-803` (rate limiters), drafting registry via `main.py:185-190`.

Defect: when the advisory lock is not acquired the app deliberately keeps booting
(`start=False`, "so trigger_job_now / introspection still work"), logs ONE warning, and
serves traffic. Nothing refuses `uvicorn --workers 2`, gunicorn, or a second replica. The
design treats the scheduler as the only shared-state hazard; the collab room manager, the
drafting subprocess registry, the pass registry, the retirement tombstones and the login
rate limiters are all per-process module state.

Failure scenario: an operator adds `--workers 2` (or a second replica behind the proxy) to
"handle load". Two collaborators' WebSockets for the same proposal land on different
workers → two independent Yjs docs that never see each other's updates; each room appends
to `yjs_updates` and checkpoints to git on its own, so the last checkpoint wins and the other
worker's edits are lost. Login rate limiting is 2x and inconsistent. The admin "run now"
button on the non-lock worker returns `triggered=true` and runs nothing
(`scheduler.py:1135-1150`, prior #25). `/healthz` is 200 on both.

RAN (boot behaviour):
```
###### S4 uvicorn --workers 2 against one DB
healthz: 200 {"status":"ok"}
advisory locks granted in DB: 1
worker pids started: 2
lock_not_acquired lines: 1
```
READ (room/registry split): `room.py:971`, `main.py:198,204-213`.

Fix shape: make lock-not-acquired FATAL by default (exit non-zero) unless an explicit
`SCHEDULER_STANDBY_OK=true`; surface `scheduler_lock.acquired` in `/healthz`; README's
one-line prohibition (`README.md:31-33`) is the only guard today.

###### F2 — Medium — README step 4 (`uv run alembic upgrade head`) fails exactly as written
`alembic/env.py:29-34`; `README.md:76-110` (dev) and `README.md:196-203` (prod).

Defect: the README tells the operator to create `.env` and then run `uv run alembic upgrade
head`. pydantic-settings reads `.env`; alembic's `env.py` reads only `os.environ` and never
loads `.env`. Nothing in the README says to export the file.

Failure scenario: day one, fresh operator, follows the README verbatim → migration step
errors out; the natural workaround is `DATABASE_URL=postgresql+psycopg://user:PASSWORD@... uv
run alembic upgrade head` on the command line, which puts the production DB password into
shell history.

RAN (from the repo root, where `.env` exists, with `env -i`):
```
RuntimeError: No database URL: set DATABASE_URL in the environment or inject via cfg.set_main_option('sqlalchemy.url', ...)
```
Fix shape: `env.py` falls back to `Settings().database_url` (the app's own loader), or the
README adopts the `set -a && source .env && set +a` line that `scripts/seed_dev_data.py`'s
docstring already uses.

###### F3 — Medium — `.env.example` documents a break-glass admin password reset that the code refuses to do
`.env.example:51-59` vs `src/call_manager/admin_bootstrap.py:105-119`,
`tests/test_admin_bootstrap_password.py:15,51-62`, `README.md:103-104`.

Defect: `.env.example` says "While set, every configured INITIAL_ADMIN_EMAIL admin's
password is (re)set to this on each startup ... UNSET it once you've logged in" and
"re-activated + re-promoted on every startup". The code does the opposite: an existing user's
password hash is never touched (`test_second_run_does_not_overwrite_changed_password_or_bump_epoch`),
recovery on total lockout restores role/status only (`test_recovers_admin_on_total_lockout`
asserts `password_hash is None`), and re-promotion happens only when `active_admin_count == 0`.
README agrees with the code; `.env.example` contradicts both.

Failure scenario: the sole admin forgets their password. The operator does what
`.env.example` says — sets `INITIAL_ADMIN_PASSWORD`, restarts — and the password is
unchanged. The only in-app reset (`web/api/admin_users.py:253`) needs another admin. The
operator ends up hand-editing `users.password_hash` in Postgres. Conversely, an operator who
believed the example and left the env password set thinks a restart "resets" a compromised
admin credential; it does not.

READ (code + tests above; the tests are the authority).
Fix shape: rewrite the `.env.example` block to match `admin_bootstrap.py`; add an explicit
one-shot `INITIAL_ADMIN_PASSWORD_FORCE_RESET=true` if a break-glass reset is wanted.

###### F4 — Medium — a verbatim copy of `.env.example` yields a non-serving, off-document deployment; the day-one keys are missing
`.env.example` (whole file; last edited f6feeed2 2026-08-29); `config.py:688` (SPA_MODE default
`disabled`), `:680` (ADMIN_DASHBOARD_URL default `http://localhost:8000/`), `:139`
(`claude-opus-5`, since 09b7534f 2026-08-10), `:116-118` (DRAFTING_ENABLED default True);
`digest/templates/digest.html.jinja:14,27,34`; `scheduler.py:576-579`.

RAN — `.env.example` loaded as `.env` (+ a SESSION_SECRET), effective values:
```
spa_mode = 'disabled'                     -> no UI at all; "/" is 404 (README says prod MUST be 'prod')
admin_dashboard_url = 'http://localhost:8000/'   -> every link in the weekly digest points at localhost
proposal_assist_llm_model = 'claude-opus-4-7'    -> differs from the code default and from CLAUDE.md/README
drafting_enabled = True, anthropic key set? False -> README says leave it false until a key exists
```
Failure scenarios: (a) the first weekly digest sent from production carries
`http://localhost:8000/calls/<id>` links for every recipient (`digest.html.jinja:14,27,34`
build every href from `dashboard_url`); (b) drafting on with no key = `phase0 tick skipped:
ANTHROPIC_API_KEY not set` at INFO every 30 s (`scheduler.py:578-579`, `PHASE0_TICK_SECONDS=30`)
= 2,880 lines/day of noise while workspace setup jobs never run; (c) the example pins a
model id that the code, CLAUDE.md and README no longer name.

Of the 54 aliases absent from `.env.example` (measured: `cfg_aliases.txt` minus
`env_example_keys_incl_commented.txt`), the ones an operator must know on day one:
- `SPA_MODE` — no safe default (`disabled` = no product).
- `ADMIN_DASHBOARD_URL` — default is localhost; goes into every digest email.
- `EMBEDDING_MODEL` — README mentions it; default `bge-m3` is fine but it must match what Ollama has pulled.
- `SESSION_MAX_AGE_SECONDS` — 30-day cookies by default; a hospital policy decision.
- `COLLAB_KILL_SWITCH` — the only emergency brake for the collaborative editor; invisible unless you read config.py.
- `RETENTION_INSPECTION_ENABLED` — privacy decision (admin may read deleted proposals).
- `DRAFTING_MAX_LIVE_SUBPROCESSES` + `DRAFTING_DB_POOL_SIZE` — capacity; must move together (config.py:195-223).
- `REMOTE_RANK_MODEL`, `REVIEW_DOLLAR_CAP_DEFAULT`, `REVIEW_RUNS_PER_DAY_DEFAULT`, `REVIEW_PRICE_*` — hosted spend.
- `DIGEST_DRY_RUN` — the seed script's docstring assumes it is set in dev; it is not in the example.
- `STALE_SOURCE_ALERT_DAYS`, `TURN_MAX_SECONDS` — the operational alarms.
The remaining ~35 are tick cadences / timeouts with sane defaults.

Coverage: the only test touching `.env.example` (`tests/test_digest_smtp_only_deploy.py:94`)
asserts the presence of two keys; nothing loads the file.
Fix shape: a test that loads `.env.example` (+SESSION_SECRET) through `Settings()` and
asserts the prod-relevant keys; or generate the example from the `Settings` fields the way
`scripts/gen_architecture.py` generates the map.

###### F5 — Medium — shutdown can block for up to ~16 minutes; the documented launch has no stop timeout, so SIGKILL skips the subprocess teardown
`main.py:307` (`scheduler.shutdown(wait=True)`), `:314-333` (terminate drafting subprocesses
AFTER that); tick wait bounds `scheduler.py:731` (300 s), `:779` (`turn_max_seconds+60` =
660 s), `:815-816` (240 s), `:877-878` (240 s), `:927-928` (`review_job_timeout+60` = 960 s),
`:975-978` (240 s); ranking drain `config.py:76-84` (240 s budget + one ~240 s job); SSE loop
`web/api/turns.py:336-357` ends only when the turn is terminal (reaper TTL 15 min,
`turns/reaper.py:40`); `README.md:200-203` gives a bare `uvicorn` line, no unit file.

RAN — uvicorn 0.44.0 defaults: `timeout_graceful_shutdown=None` (wait forever for in-flight
responses), `timeout_keep_alive=5`, `limit_concurrency=None`, `workers=None`, `access_log=True`.

Failure scenario: `systemctl restart` during a mock-panel review run. SIGTERM → uvicorn waits
for the open SSE response (until the turn is terminal), then the lifespan waits for the
review tick (≤ 960 s). systemd's default `TimeoutStopSec=90s` fires → SIGKILL → the
`controller.terminate` loop at `main.py:320-333` never runs. Under `KillMode=control-group`
the Claude Agent SDK node subprocesses die with the cgroup; under a plain shell/`nohup`
launch (the README's shape) they are orphaned, and the running turn row stays `running`
until the 15-minute reaper. RAN sanity: an idle instance exits in 0.7 s, so this only bites
on the bad day.

Fix shape: `--timeout-graceful-shutdown 30`; bound the scheduler wait (`shutdown(wait=False)`
+ join with a deadline); terminate subprocesses BEFORE waiting on ticks; ship a systemd unit
with `TimeoutStopSec` and `KillMode=mixed` in the README.

###### F6 — Medium — the cutover script resets the production role's password to a public literal and cites a rotation pattern that does not exist
`scripts/cutover_rename_database.sh:53-56`.

Defect: step 3 runs `ALTER ROLE call_manager WITH PASSWORD 'call_manager'` unconditionally.
The comment says "production deployments rotate this via the env-var pattern documented in
.env.example" — RAN grep: `.env.example` and `README.md` contain no `PGPASSWORD`, `ROLE` or
`rotate` line. The script advertises itself as idempotent and safe to re-run.

Failure scenario: an operator re-runs the "idempotent" cutover on production (or runs it for
the first time with a real password already set): the role password silently becomes
`call_manager`; the app's `DATABASE_URL` stops authenticating (outage), and until the
operator notices, the production DB accepts a credential printed in a public repo.

READ. Fix shape: read the new password from `PG_APP_PASSWORD`, refuse when unset; or delete
the script now that the B2 cutover is done (no other file references it — RAN grep).

###### F7 — Medium — nothing states what must be backed up, nothing is ever pruned, and the filesystem is never reclaimed
`README.md:205-207` (the only statement: "Keep the database and WORKSPACES_DIR on persistent
storage"); `attachments/resolver.py:112,157` (WORKSPACES_DIR also holds `attachments/` and
`references/<id>/text.txt`, not just git repos); no purge anywhere (RAN grep for
`rmtree|hard_delete|purge` in admin retention / retirement: none).

RAN census: 44 ORM tables, 36 have no deleter anywhere in `src/` (grep for
`delete(<Model>)` / `session.delete(...)`). Append-per-event tables with no retention:
`llm_spend`, `citation_llm_usage`, `workspace_events`, `chat_turns` (compaction adds
`chat_summaries`, never deletes turns), `ranking_remote_attempts`, `ranking_jobs` (unique only
on the ACTIVE pair — done/failed rows accumulate per re-rank; migration 0057 indexes exactly
those), and every `*_jobs` / `review_runs` table keeps its terminal rows. Only `yjs_updates`
has compaction.

Failure scenarios: (a) backup taken of Postgres nightly and of WORKSPACES_DIR weekly (nobody
said they must be consistent): after a restore, `Attachment` rows whose files are gone raise
`AttachmentResolutionError` mid-turn (`resolver.py:114-117`), and `artifact_versions` rows
whose commit is gone 404 (`versions.py:214-218`) — degraded, not crashing, but the operator
has no document telling them this is expected; (b) disk fills: soft-delete frees zero bytes
(memory: retention A), no purge exists, so PDFs, extracted text and git repos of deleted
workspaces stay forever; ENOSPC surfaces as 500s via the `OperationalError` handler.

READ (+ RAN census). Fix shape: a "Backup and retention" README section (pg_dump +
WORKSPACES_DIR in the same window; what a mismatch looks like); a retention tick for
terminal job rows and `llm_spend`/`workspace_events` older than N days; an admin purge that
actually removes bytes.

###### F8 — Low — `scripts/seed_dev_data.py` has no environment guard and enqueues hosted-LLM work
`scripts/seed_dev_data.py:274-294` (only refuses when no admin exists), `:335-365`
(inserts 8 fabricated calls under the real `vinnova`/`eu_portal` source rows and
`enqueue_for_pair` at `:365`).

Failure scenario: an operator "checks the UI works" on production → 8 fake calls appear on
every dashboard, the first admin is subscribed to a fake profile, ranking jobs run (and may
escalate to the hosted model, i.e. real spend), and the next weekly digest advertises fake
calls. Idempotency means re-running does not remove them; there is no unseed.

READ. Fix shape: refuse when `APP_BASE_URL` is `https://` unless `--i-know-this-is-prod`;
tag seeded rows so an unseed can find them.

###### F9 — Low — uvicorn's access log (on by default, plaintext) records the OAuth callback's authorization code; app logs are JSON, uvicorn's are not
`web/routes/oauth_google.py:87` (`GET /auth/google/callback?code=…&state=…`);
`call_manager/logging.py:55-77` installs the JSON formatter on the root logger only; README
has no `--no-access-log` / log-config guidance (RAN grep).

RAN (trivial ASGI app under uvicorn 0.44.0, same defaults):
```
INFO:     127.0.0.1:43718 - "GET /auth/google/callback?code=4/0AX-SECRET-AUTH-CODE&state=abc HTTP/1.1" 200 OK
```
Failure scenario: logs shipped to a central collector; every Google sign-in's `code` is
stored there. Impact is bounded — the code is single-use and PKCE-bound (verifier lives in
the encrypted txn cookie, `auth/oauth_txn.py`) — so disclosure only. The mixed
plaintext/JSON stream also breaks a JSON-only collector.

Fix shape: `--no-access-log`, or a `--log-config` that JSON-formats `uvicorn.access` and
strips query strings.

###### F10 — Low — misspelled config keys are silently ignored
`config.py:24` (`extra="ignore"`).

RAN: `DRAFTING_ENABLE=false CITATION_LLM_PROVIDR=anthropic` → `drafting_enabled=True
citation_llm_provider='ollama'`, no warning.

Failure scenario: an operator intends to stop hosted spend (`DRAFTING_ENABLE=false`) or
believes they enabled something; the typo is accepted and the intended change never
happens, with no log line to find.

Fix shape: after `Settings()` construction, log at WARNING any `.env`/environment key that
matches `^[A-Z][A-Z0-9_]+$`, is not a Settings alias, and is within edit distance 2 of one.

##### 2. Asked-for measurements

**Resource ceilings.** Per app process: request engine `pool_size=10, max_overflow=20`
(`db/session.py:15-16`) + drafting engine `DRAFTING_DB_POOL_SIZE=8, max_overflow=1`
(`factory.py:48`, `config.py:212-223`) + 1 scheduler-lock connection = **40 max** — under
Postgres' default `max_connections=100`. The CLI tools (`ingestion/cli.py:121`,
`embeddings/cli.py:69`, `references/backfill.py:10`) each build their own 30-connection
engine if run alongside the app. Scheduler thread pool = 30 (`scheduler.py:46`) sharing the
30-connection request pool with web traffic (see lead L1). Body cap 64 MiB
(`config.py:676-678`) vs 50 MiB per reference upload (`references/uploads.py:33`), one file
per request (`web/api/references.py:96`) — consistent.

**Migrations.** Manual only (`README.md:106-110,201`); no boot-time revision check; 69 files,
single head `0068` (RAN `alembic heads`); the last 12 downgrades (0057–0068) are all real,
none `pass`-only (RAN AST census); 0065's is lossy (prior, accepted). RAN: an empty DB fails
loud at lifespan with `UndefinedTable relation "references" does not exist` (no "run alembic
upgrade head" hint); a DB at 0060 (8 behind head) boots and `/healthz` returns 200.

**CI (`.github/workflows/ci.yml`).** Runs: `uv lock --check`, wheels allowlist, route
artifacts, `ruff check` (`:44`), `pytest -v` serial (`:47`, real migrations from empty via
`conftest.py:124-126`), pip-audit, OSV ×2, vitest, OpenAPI drift, pnpm audit, Docker SPA build
(`tsc && vite build`, `frontend/package.json:11`), wheel-contains-SPA. Does NOT run: Playwright
e2e (`frontend/e2e/*.spec.ts`, only `scripts/collab_e2e_run.sh`), eslint (no config exists —
RAN ls), `ruff format --check`, `alembic check`/`compare_metadata` (prior), mypy/pyright
(none in pyproject), the `-n 16` mode (irrelevant to correctness), any `.env.example` load, any
README command. Green CI proves: deps locked and audited, migrations apply from empty, types
in sync, SPA builds and ships in the wheel. It does not prove the README launch sequence
works (F2), that a stale schema is refused (prior), that multi-worker is safe (F1), or that
shutdown is bounded (F5).

##### 3. Checked and sound (do not respend)
- Missing `SESSION_SECRET` / `APP_BASE_URL` / `DATABASE_URL` fail loud at `create_app` with the key named (RAN probe 1: pydantic `SESSION_SECRET Field required`). https + short/placeholder secret refused (`config.py:695-717`).
- Bogus `DATABASE_URL`: `create_app` succeeds (engine is lazy), lifespan fails loud; error text never echoes the password — RAN with `postgres://user:pw@…` (`NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres`) and an unresolvable host (`failed to resolve host 'db.internal'`).
- `SPA_MODE=prod` with the bundle missing raises `RuntimeError("SPA build missing at … (SPA_MODE=prod)")` at `create_app` (`spa_serving.py:33-37`, READ — the main tree has a bundle so the branch could not be RAN).
- Ollama unreachable / bogus `ANTHROPIC_API_KEY`: boot succeeds, failures surface per call (RAN probes 7-8) — by design; no boot-time probe, noted.
- `EMBEDDING_DIM` mismatch refused at boot (`main.py:384-392`); inverted embedding thresholds refused (`config.py:739-759`).
- uvicorn proxy headers: `forwarded_allow_ips=None` → only `127.0.0.1` trusted (`uvicorn/middleware/proxy_headers.py:23`), rightmost-untrusted XFF entry taken (`:48`), so a local proxy that appends XFF cannot be spoofed even with `TRUSTED_PROXY=false`; the app's hop logic (`web/client_ip.py:42-55`) is consistent with it.
- Anthropic key reaches the drafting subprocess explicitly (`claude_adapter.py:292-293`); `.env`-only keys never enter `os.environ`, so the scrub list (`:93-100`) matters only for `EnvironmentFile=` deployments.
- `api_key.txt` is read by no code (RAN grep: only a review mention); it is an operator artifact.
- Logger greps for `password|secret|cookie|token|api_key|Authorization` values: none (RAN); auth logs use `email_fp` fingerprints (`web/client_ip.py:13-16`); raw admin email appears only in `admin_bootstrap.py:57,95,115` WARNING lines; no log call emits prompts or reference text (RAN grep). `/healthz` logs the driver exception server-side only (`health.py:22-26`).
- Restore mismatch is tolerated at read time: missing attachment → `AttachmentResolutionError` (`resolver.py:114-117`); missing commit → 404 (`versions.py:214-218`); missing spec/proposal file → explicit checks (`web/api/workspaces.py:432,800,999`).
- `SESSION_SECRET` rotation: single key today (`session_cookie.py:33`, `oauth_txn.py:35`), so rotation logs everyone out; itsdangerous 2.2.0 accepts a key list (RAN) — a `SESSION_SECRET_PREVIOUS` is a small change.
- SIGTERM on an idle instance: exits in 0.7 s (RAN S5 teardown).

##### 4. Prior findings touched (status only)
- Lost scheduler lock is silent (third-pass §5; whole-project High): **still open** — RAN S5: killed the lock backend under a live instance; it kept serving 200 with no log line; a second instance then acquired the lock (no `lock_not_acquired` warning).
- `/healthz` is DB-only, no schema-revision check (third-pass §5): **still open** — RAN S2 (schema at 0060 boots, 200).
- `WORKSPACES_DIR` CWD-relative, no boot validation (third-pass §5 / prior #26): **still open** — RAN S3 (`/proc/nope` boots, 200).
- SMTP-only deploy loses the digest (§4.11): **fixed f6feeed2** (`main.py:413-434`; `EMAIL_TRANSPORT` now in `.env.example:33`).
- Drafting scrub list omits `INITIAL_ADMIN_PASSWORD`/`GOOGLE_CLIENT_SECRET`, pins `VINNOVA_API_KEY` (prior #19): **still open** — `claude_adapter.py:93-100` unchanged; `VINNOVA_API_KEY` is not a Settings alias (RAN: 0 hits in `cfg_aliases.txt`).
- `trigger_job_now` reports success on a never-started scheduler (prior #25): **still open** (`scheduler.py:1135-1150`, no `running` check) — and F1 makes it reachable on every non-lock worker.
- `api_key.txt` mode 0660 (whole-project review): **unchanged** (RAN `ls -la`).
- No `alembic check` / drift gate (third-pass §5): **still open** (CI section above).

##### 5. Not read
`digest/send.py` body, `ingestion/cli.py`, `embeddings/cli.py`, `auth/email.py` senders,
`email/factory.py`, `proposal_assist/drafting/registry.py`, `collab/room.py` beyond the
module-level `MANAGER`, `web/api/admin_retention.py` body, `web/middleware.py`,
`frontend/.dockerignore`, `frontend/playwright.config.ts`, `scripts/collab_e2e_run.sh` body,
`dependency-review.yml` semantics beyond the trigger.

##### 6. Leads (no proof)
- L1: 30 scheduler threads + web requests share one 30-connection pool; on exhaustion SQLAlchemy raises `sqlalchemy.exc.TimeoutError` (not `OperationalError`), so it would fall to the catch-all 500 (`main.py:760-767`) with no `db.operational_error` log line. Unmeasured.
- L2: with `DRAFTING_ENABLED=true` and no key (the `.env.example` state), `workspace_setup_jobs` stay queued forever; what the UI shows the user was not checked.
- L3: no `LOG_LEVEL` setting (`logging.py:55`); INFO forever, no way to quiet the 30-second skip lines without code.
- L4: migration 0068 prints a summary line to stdout during `alembic upgrade` (seen in the S2 probe output) — harmless, but a deploy script grepping alembic output may trip on it.


## Appendix A10 — epoch-fence spine and workspace lifecycle

#### A10 — Epoch-fence spine and workspace lifecycle (adversarial review)

Base: master @ 8f44a35e, own worktree `.claude/worktrees/agent-abf47bf0f3e684a9d`
(venv verified: `call_manager.__file__` inside the worktree). Every mutation was applied by
line number, run, and reverted; `git status --short` is empty at the end. Baseline before any
mutation: `uv run pytest tests/fencing -q` → **239 passed in 40.8s**.

Probe source (two runtime probes, both PASS = the hole/deadlock is real) is kept at
`scratchpad/findings_probe_a10.py`; the mutation script at `scratchpad/a10_mutations.sh`;
raw logs `scratchpad/a10_run{X,Y,Z}.log`.

---

##### 1. Ranked findings

###### F1 · Medium — A Phase-0 worker claimed at generation G writes M2/M3 into generation G+2 after delete → restore; the comment that says the liveness check catches it is false — **RAN**

- `src/call_manager/proposal_assist/phases/setup.py:473-487` (`_assert_live`: `soft_deleted_at IS NULL and status == 'running'`), `:512-514` (`_m2` persist: liveness only), `:592-599` (`_m3` persist: `job.status == 'running'` + liveness). Only `_m1` (`:397`) takes Primitive B. `claimed_at_generation` is threaded into `_m1` (`:344-348`) and then dropped.
- `src/call_manager/workspaces.py:53-60` (`_SETUP_RESTORE_MAP`) claims: *"The residual double-claim: accepted because the workspace WAS deleted, so any live worker has already failed its liveness check or will at its next milestone."* After a restore `soft_deleted_at` is NULL again, so the liveness check passes; and the re-queued row is re-claimed, so `status == 'running'` passes too.
- Scenario: worker A claims at gen 0, finishes `_m1`, enters its LLM phase (minutes). Owner deletes (gen 1); the jobs hook cancels the un-held row (`cancelled_from='running'`); admin restores (gen 2, row → `queued`); the next tick's worker B claims it (`claimed_at_generation=2`, `running`). A resumes: `_m2`/`_m3` pass every check and write `ws.section_extracts`, `ws.rubric_*`, the `rubric_initialized` audit event and set the job — B's claim — to `waiting_for_rubric_review`. B's own `_m3` then raises `WorkspaceGoneDuringSetup` and `_fail` (`:664`) treats `waiting_for_rubric_review` as already-yielded: B's two LLM calls are spent and recorded nowhere.
- RAN (probe A, `tests/proposal_assist/test_zz_probe_a10.py`, since removed):
  ```
  PROBE_A_LANDED {'ws.lifecycle_generation': 2, 'job.status': 'waiting_for_rubric_review',
   'job.claimed_at_generation': 2, 'ws.section_extracts_set': True,
   'ws.rubric_criteria_set': True, 'rubric_initialized_events': 1}
  2 passed in 4.60s
  ```
  The actor whose belief was generation 0 produced every M2/M3 semantic effect in generation 2 and terminalized another actor's claim.
- Why this is not merely "F5 is pending": the inventory row for F5 covers `_init_repo`/`claim_next_setup_job` (the git sink and the claim), and `_m1` is already converted. The two milestones that carry the LLM-derived writes are the unconverted ones, and the file's own comment asserts the opposite.
- Fix shape: `_m2`/`_m3` already hold the `Workspace` row `FOR UPDATE` — spec mechanism 3 applies: thread `claimed_generation` from `run_one` and refuse when `ws.lifecycle_generation != claimed_generation` (and in `_assert_live`). No new lock. The comment at `workspaces.py:56-60` must be rewritten.

###### F2 · Medium — The delete path and the turn-success finalize take the workspace row and the turn_jobs row in opposite orders: a real Postgres 40P01, and the finalize is the victim — **RAN**

- Delete: `src/call_manager/workspaces.py:180` sets `ws.soft_deleted_at`; the first `session.execute` at `:184` autoflushes `UPDATE workspaces` (row lock W, FOR NO KEY UPDATE), then the four job-table UPDATEs, then `record_terminal_outcome` (`:198-230`) `UPDATE turn_jobs … WHERE status='running'` (J). Measured statement order (probe B trace): `pg_advisory_xact_lock → SELECT soft_deleted_at → UPDATE workspaces SET updated_at, soft_deleted_at → UPDATE coherence_jobs → UPDATE evaluator_jobs → UPDATE review_runs → UPDATE uncited_claims_jobs → SELECT turn_jobs …`. So the delete is **M → W → J**.
- Finalize: `src/call_manager/proposal_assist/turns/processor.py:651-656` `UPDATE turn_jobs … status='running'` (J) then `:696-702` `UPDATE workspaces SET last_sdk_session_id` (W). **J → W.**
- RAN (probe B): with the finalize holding J and the delete holding W, both proceed and Postgres reports
  ```
  PROBE_B_ERRORS {'finalize': ('OperationalError', '40P01')}
  ```
  The finalize is the deadlock victim (first waiter past `deadlock_timeout`).
- Harm: `processor.py:494` calls `_finalize_success` under no `try`; `turns/drain.py:93-...` catches only `TimeoutError`, so the `OperationalError` propagates out of the tick. The finalize's transaction is rolled back: the assistant-row token stamp, the `spend_ledger` `chat_turn ok` row and the `cloud_turn_completed` event are all lost, and `stamp_post_terminal_usage` is never reached. The surviving delete then cancels the turn with `usage_unknown=True` (`workspaces.py:215-229`), i.e. the daily cap keeps the *submit-time estimate* for a turn whose real usage the SDK had just reported. `workspaces.py:198-205` documents this exact race as "closed durably" by same-row contention; it is closed by a deadlock.
- Lock-order corollary (READ): two "normative" orders are documented and they disagree in form — `fencing.py:33-38` / `phases/setup.py:610-616` say **M → J → W** (true for `workspace_setup_jobs`, which the delete transaction never touches), `analysis/queue.py:302` says **workspaces < claim_analysis_jobs** (W → J). The measured delete order is M → W → {coherence, evaluator, review, uncited, turn} — consistent with the four pass prefixes (W → own J) and with `mark_failed` (W → J under `no_autoflush`), inconsistent only with `_finalize_success`. A reader following `fencing.py`'s sentence for a non-setup job table writes J → W and inverts against the delete, which is what happened here. Memory's "lock order has NO test" stands: no test asserts an order across two paths (grep `40P01|deadlock` finds only per-path timeout tests).
- Fix shape: in `_finalize_success` take the workspace row before the `turn_jobs` UPDATE (move the `update(Workspace)` above it, or `SELECT … FOR NO KEY UPDATE` first) so it is W → J like every other writer; and make the documented order per job table ("M first; then W before every job table except `workspace_setup_jobs`, which is J then W").

###### F3 · Medium — `_insert_citation_locked`'s generation fence is invisible to the static gate *and* has no behavioural pin: a liveness-only regression passes 85/85 including every fencing gate — **RAN**

- `src/call_manager/proposal_assist/citations/core.py:575-577` takes Primitive B inside a `try:`; `tests/fencing/live_mutex_gate.py:69-77` pins it in `UNCONVERTED_LIVE_CALLERS` as "stays pinned with a §5.2 runtime proof". No such proof exists for the generation half: the only behavioural test, `tests/proposal_assist/test_tools_citations_deleted.py::test_insert_citation_refuses`, drives a *deleted* workspace (the weaker property).
- Mutation (Run Z): replace lines 575-577 with `acquire_commit_mutex(...)` + a fresh `soft_deleted_at` re-read raising `WorkspaceDeleted` — the pre-fence contract, no generation comparison. Ran `tests/fencing/test_live_mutex_gate.py tests/fencing/test_fence_coverage.py tests/proposal_assist/test_tools_citations_deleted.py tests/proposal_assist/test_f3d3a_relocation.py tests/proposal_assist/proposals/test_accepting.py tests/proposal_assist/collab/test_collab_save_severance.py tests/proposal_assist/test_fencing.py` → **all green** for this site. (The identical mutation applied to `review/pass_runner.py:476-478` in the same run reddened `review/test_pass_runner.py::test_finalize_refuses_to_commit_when_the_workspace_moved_on` and `::test_the_finalize_takes_the_mutex_BEFORE_the_review_run_row` — so the technique detects, and this site is the hole.)
- Failure scenario: an agent turn claimed at G calls `insert_citation` after delete → restore (G+2); with the regression above it commits `proposal.md` + `citations.json` into the restored repo. This is hollow-gate shape 4 (the checker proves a weaker property) sitting behind a pin that says the runtime proof exists.
- Same shape, READ only: `web/api/workspaces.py::post_init_spec._git` (`:596`, pinned at `live_mutex_gate.py:98-104` with a sentence — "the §5.2 runtime proof is that require_owner stamps the generation before the mutex" — not a test; `grep -rl "init_spec" tests/` finds no delete→restore case).
- Fix shape: one delete→restore→resume test per pinned site asserting `WorkspaceGenerationStale`, unchanged HEAD and a clean worktree (spec 2026-08-18 §9 already prescribes the assertions).

###### F4 · Low — Fence bookkeeping says the review finalize is unfenced; the code has been fenced and pinned since 2026-08-30 — READ

- `tests/fencing/fence_inventory.json` row `review/pass_runner.py::run_review_pass` is `fenced: false, slice: F4` (pending); `live_mutex_gate.py:91-94` pins it with no comment, unlike every neighbour. `inventory.py` `FENCING_IMPORT_FILES` (the "Review finalize (2026-08-30)" entry) and `review/test_pass_runner.py:324,426` say the opposite, and Run Z confirmed the pin is real (2 reds).
- Memory claim "2 git sites still take no mutex" is **refuted**: the only mutex-less sink is `citations/bibliography.py:627` behind `commit=True`, and both production callers pass `commit=False` (`export.py:88`, `tools/audit.py:221`). Memory claim "review runs can write to the wrong branch" is refuted for the git write (a switch bumps the generation at `branches.py:239`; the finalize is fenced at the claim's generation).
- `live_mutex_gate.py:35-42` docstring ("Seven of the eight are F3e1b's … `Phase0Worker._m1` reaches an F5 sink") describes an `ENCLOSING_ACQUISITIONS` mapping that is now empty (`:143-160`).
- Fix shape: regenerate the inventory row's slice/comment when the code moved; the `fenced` flag itself is measured and correctly false (acquisition inside `try:`), so this is documentation, not the gate.

---

##### 2. Semantic-effect site table (every git write, filesystem write, yjs append, artifact_versions insert, proposed_changes accept, and the DB-only effects)

Primitive legend: **A** `live_at_generation` in the write; **B** `acquire_commit_mutex_at_generation`; **C** `acquire_shared_lease`; **none** = liveness/status check only.

| Effect | path:line | Primitive | Generation claimed at | Status |
|---|---|---|---|---|
| `save_section` git commit + `artifact_versions` | `editing/save.py:219 → :334,:343` | B | route access check (threaded stamp) | fenced; removing `:219` reddens gate (`test_the_inventory_git_flags_match_the_tree`, `test_the_tree_has_no_unpinned_live_caller`) **and** `test_blocked_save_section_refuses_and_leaves_git_untouched` (RAN) |
| `insert_citation_for_user` git + AV | `citations/human_cite.py:116-117 → :239,:247` | B | `stamp_for_workspace(workspace)` on the caller-loaded row | fenced |
| `insert_from_annotation` git + AV | `analysis/insert.py:364-365 → :418,:431` | B | same | fenced |
| `checkpoint_artifact` git + AV (+ yjs watermark) | `collab/checkpoint.py:152 → :212,:218` | B | threaded stamp | fenced; but the evict/tail callers mint from a fresh read (`collab/permissions.py:64`, `checkpoint.py:291`) — see Lead L1 |
| `_insert_citation_locked` git + rows | `citations/core.py:575 → :631` | B (in `try`) | stamp param | fenced, generation half **unpinned** (F3) |
| `post_init_spec._git` git + AV | `web/api/workspaces.py:584,:596 → :615,:619` | B (in `try`) | access check | fenced; pinned-unprovable; no restore test (F3) |
| review finalize git (`fit_check`/`rubric_review`) | `review/pass_runner.py:476 → :515` | B | claim (`drain_review_queue_tick` FenceStamp) | fenced since 08-30; pinned (RAN); inventory stale (F4) |
| Phase-0 `_init_repo` git | `phases/setup.py:471` via `_m1:397` | B (enclosing) | `claimed_at_generation` (`:313`) | fenced transitively; F5 row pending |
| Phase-0 `_m2`/`_m3` DB writes (section_extracts, rubric, `rubric_initialized`, status) | `setup.py:512-551, :592-646` | **none** | — | **unfenced, reachable** (F1, RAN) |
| `build_bibliography(commit=True)` git | `citations/bibliography.py:627` | **none** | — | dormant: no prod caller passes `commit=True` |
| `submit_scorecard` / `submit_review` / `submit_uncited_claims` UPDATE | `port_impl/effects.py:112-120, :162-171, :207-215` | C + A | tool `Invocation.stamp` from the claim | fenced; removing `:120` reddens `test_case_b_lifecycle_first_the_effect_lands_nothing[submit_scorecard]` (RAN) |
| `propose_artifact_edit` / `propose_citation_insert` INSERT | `effects.py:271-312` | C + A (INSERT…SELECT…WHERE) | claim | fenced; case A/B tests |
| `reconcile_workspace_git_drift` AV INSERT | `reconcile.py:84,:87,:134` | C + A | read **under the lease** after the git-log belief | liveness-only by construction (its own comment says the equality term "can never fail"); removing `:84` reddens `test_reconcile_git_drift_waits_for_the_commit_mutex_key` (RAN) |
| `reconcile_workspace_citekeys` references UPDATE | `reconcile.py:246,:298` | C + A | same | same |
| `claim_turn_job` (stamps generation) | `turns/processor.py:189-196` | C + correlated subquery | at claim | fenced claim |
| turn success finalize (ChatTurn tokens, spend, `Workspace.last_sdk_session_id`, event) | `processor.py:616-720` | **none** (status CAS) | — | F6 pending; **J → W deadlock vs delete** (F2, RAN) |
| `proposed_changes` accept, flag-off | `proposals/accepting.py:116,:156,:256` via `web/proposals.py:98` stamp | B via the inner `save_section` / `_insert_citation_locked` | route access check | fenced through the inner write |
| `proposed_changes` accept, collab | `collab/kernel.py:217,:389 → :271-309,:419-425` | B | threaded stamp | fenced |
| yjs append inside kernel ops | `collab/kernel.py:176,:301,:354,:545` | B (enclosing op) | threaded stamp | fenced |
| yjs append, live socket per message | `web/collab_ws.py:140 → collab/store.py:35` | **none** (`room.evicted` / `is_retired`, in-process) | — | F7 pending; documented at `collab_ws.py:101-103` |
| hydrate seed append | `collab/hydrate.py:58,:75` | B | connect-time stamp (`collab_ws.py:105`) | fenced |
| enable-on-touch flip | `collab/flip.py:67` | B | stamp | fenced |
| branch switch/create/delete (git checkout/branch, bump) | `branches.py:181,:337,:439` | B | route stamp | fenced |
| six `workspace_write` routes | `web/_writes.py:88` | B | route stamp | fenced (straddle suite) |
| `workspaces.py` routes `:596,:711,:803` | B | `stamp_for_workspace(ws)` at access check | fenced |
| reference ingest: files under `WORKSPACES_DIR` + `Reference` rows + embeddings | `references/ingest.py:133,:330-`, `tools/references.py::_do_fetch_and_ingest` | **none** | — | F3e2 pending (`fenced:false` row); only subprocess teardown stops it |
| claim-analysis annotations + `done` | `analysis/pipeline.py:245-325`, `queue.py:232` | **none** (liveness probe per sentence, status CAS) | — | F4 pending; same class as F1 (needs a restore inside one job's runtime) |
| coherence/evaluator/uncited job → `completed`/`failed` | `coherence/pass_runner.py:243-270` and siblings | **none** (status CAS on the job row) | — | F4 pending; the content effects go through the ports (fenced) |
| soft-delete / restore | `workspaces.py:171,:277` | raw mutex + fresh liveness re-read; bump | — | correct by design |
| jobs hook cancel | `jobs_retirement.py:100-160` | `_still_at_generation` (no liveness, by design) | hook's allocated generation | pinned (`test_jobs_retirement_generation.py`) |
| `reconcile_cancelled_jobs` | `jobs_retirement.py:200-260` | `dead_ws` predicate in the write | — | pinned |
| admin inspection git read | `admin_retention.py:263` | raw mutex + fresh liveness | — | read only |

Not in the table because not opened: the attachments upload route (`attachments/storage.py:111` is the sink).

---

##### 3. Restore walk (delete → restore → stale writer fires), by writer class

| Writer class | What refuses it after restore | Verdict |
|---|---|---|
| Route writers (`workspace_write`, kernel, `save_section`, `workspaces.py` sites, branches) | Primitive B equality at the route's stamp | refused (`test_fence_restore_hole.py`, straddle suites) |
| Tool effects via ports (5) | C + A equality at the claim's stamp | refused (`test_f3b_lock_cases` case B) |
| Review run finalize | B at the claim | refused (RAN Run Z) |
| Coherence/evaluator/uncited finalize | delete's in-txn cancel flips the row (`cancel_*_jobs_for_workspace` are plain UPDATEs, so they wait for a held row rather than skip it) → `status != 'running'` | refused by compensation, not by fence (F4 pending) |
| Turn job | delete terminalizes running/queued turns; restore never resurrects | refused by compensation (F6 pending); finalize-vs-delete is a deadlock (F2) |
| **Phase-0 worker past `_m1`** | nothing | **NOT refused** (F1, RAN) |
| Claim-analysis worker holding its row | nothing (hook SKIP LOCKEDs it; `_should_abort` is liveness-only; `mark_done` is status-only) | NOT refused if the restore lands inside one job (READ; F4 pending) |
| Ingest mid-flight | nothing (F3e2) | NOT refused; documented `fenced:false` |
| Live collab socket | in-process eviction only | NOT fenced; documented F7 |

---

##### 4. Gate mutations (every one attributable to the mutated site)

Run X — add an unfenced site to three allowlists plus a late mint (`6 failed, 89 passed`):
- `export.py` + `def _probe_unfenced_sink(repo): return write_and_commit_files(...)` → `test_live_mutex_gate::test_the_tree_has_no_unpinned_live_caller` and `test_fence_coverage::test_the_inventory_matches_the_tree_exactly`, both naming `export.py::_probe_unfenced_sink`.
- `export.py` + `from call_manager.proposal_assist.fencing import live_at_generation` (file not in `FENCING_IMPORT_FILES`) → `test_only_f3a_identities_import_the_fencing_primitives` names it.
- `tools/scorecard.py:10` + `from sqlalchemy import select` (converted tool) → `test_import_gate::test_every_tool_module_is_either_clean_or_exempt`; `tools/references.py:9` + a fencing import (exempt tool, new violation) → `test_no_exempt_module_acquires_a_new_violation`.
- `web/proposals.py:98` `stamp_for_workspace(ws)` → `stamp_for_workspace(session.get(type(ws), ws.id))` → `test_stamp_gate::test_no_production_site_reads_its_generation_at_the_mutex` names `_accept_git_first`.

Run Y — remove three fences (`5 failed, 55 passed`): `save.py:219` → gate + behavioural red; `effects.py:120` → `test_case_b…[submit_scorecard]` red (behavioural, the gate cannot see a predicate term); `reconcile.py:84` → `test_reconcile_git_drift_waits_for_the_commit_mutex_key` red (behavioural).

Run Z — weaker-property (liveness-only) rewrite of two pinned Primitive-B sites: review finalize **red ×2**; `_insert_citation_locked` **green** (F3).

---

##### 5. Checked and sound

- Primitives A/B/C themselves (`fencing.py`): equality not `>=`, generation 0 real, deleted-before-stale precedence, under-mutex scalar SELECT not `session.get` — pinned by `test_fencing.py`, `test_fencing_mutex.py`.
- `port_impl/effects.py`: all five effects carry the predicate inside the single write statement; the two INSERTs are `INSERT … SELECT … WHERE`; `_explain_refusal` runs only after refusal. Case A/Case B and the no-lease regression exist and Case B goes red when the predicate is removed (RAN).
- `reconcile_cancelled_jobs` vs an in-flight job: it cannot terminalize a live job (`dead_ws` governs the write, one statement, `SKIP LOCKED` on held rows) and cannot resurrect a cancelled one (only `_CANCELLABLE` → `cancelled`). Restore's `FOR UPDATE` on setup-job rows (`workspaces.py:319`) is what serializes it against the hook/reconcile; claim-analysis rows have no restore-side lock but restore never resurrects them, so there is no resurrection path. Pinned by `test_jobs_retirement_generation.py:278,319` and `test_workspace_restore_acceptance.py:455,514`.
- `reconcile_workspace_git_drift`: lease first, generation read under it, liveness clause in the INSERT, whole-batch rollback on refusal, `on_conflict_do_nothing` for concurrent reconciles. Lease removal reddens its test (RAN).
- Delete path: raw mutex (not B — it bumps), fresh under-mutex liveness re-read, generation bumped in-DB, returned not re-read; restore idempotent path reads the generation under the mutex; `signal_workspace_restored` monotonic apply, never re-reads. Lifecycle `transition` holds the per-workspace lock on both paths and is `<=`-monotonic.
- The retained tool context is restamped per turn; `claim_turn_job` takes the shared lease so a branch switch linearizes against it.
- Lock order per path (READ, from every `with_for_update` / advisory call in `src/`): delete M→W→{5 job tables}; restore M→setup-J→W; rubric activate/retry M→setup-J→W; `_m1` M→W; `_m2` W→Call; `_m3` setup-J→W→Call; four pass prefixes W→own J; three pass finalizes J only (+ FK KEY SHARE on W, compatible); review finalize M→J; `turns/submit` M→W; `mark_failed` W→J (`no_autoflush`); `mark_done` J only; hook/reconcile J SKIP LOCKED only; ports C→row; kernel M→pc row. Exactly one path inverts against the delete: `_finalize_success` (F2).

##### 6. Prior findings touched

- Third-pass §4.5 (controller.launch blocks the loop on the reconcilers' 10 s shared lease): still open, `reconcile.py:84,:246` unchanged.
- Third-pass §5 "review pass runner commits with no commit mutex": **fixed** 2026-08-30 (`review/pass_runner.py:476`), pinned by `review/test_pass_runner.py:324,426`; inventory not updated (F4).
- Third-pass P2-2 (`SLICE_STATUS["F3f"]` tracks zero rows): still true.
- 2026-09-01 security review "three-primitive design holds up": consistent for converted sites; the unconverted milestones are where it does not (F1).
- Grok/Sol 2026-08-25: both identify the provenance problem ("a legitimate call to the legitimate public mint with a freshly-read workspace"); Lead L1 below is the concrete instance in the tree.

##### 7. Not read

`ports/accounting.py`, `ports/artifacts.py`, `ports/invocation.py`, `ports/queries.py`, `ports/scoring.py`, `ports/shas.py`; `port_impl/accounting.py`, `port_impl/queries.py`; `tests/fencing/sink_guard.py`, `spend_guard.py`, `regenerate_inventory.py`, `test_detectors.py`, `test_inventory_merge.py`, `test_f3e1a_slice.py`, `test_f3e1a2_slice.py` (ran, not read); `collab/room.py` and `branches.py` bodies (grep only); the attachments upload route; `tests/proposal_assist/test_lifecycle_races.py`, `test_restore_bridge.py`, `test_retirement_coordinator.py` (names only). No purge path exists yet (Slice E).

##### 8. Leads (no proof)

- **L1** — `Room` carries no captured generation (grep: none). The evict checkpoint (`collab/permissions.py:57-64`) and the tail checkpoint (`collab/checkpoint.py:283-291`) mint their stamp from a fresh `s.get(Workspace)` in the same session immediately before `checkpoint_artifact`, so Primitive B there proves "not bumped between this read and the mutex", never "same epoch as the room's content". Reachable only if the retirement signal fails silently (spec 2026-08-17 §1.1/§6.1): the room survives delete → restore in memory and its content is checkpointed into G+2 with a stamp that cannot fail. F7/F9 pending.
- **L2** — `soft_delete_workspace_db` never cancels claim-analysis rows in-transaction (only the post-commit hook does, with `SKIP LOCKED`); combined with `touch_running_job` holding the row for the whole per-job transaction, a running analysis job is *never* cancelled by the delete while it runs — only by its own liveness probe.
- **L3** — Spec mechanism 3 ("assert on the already-locked Workspace row") has zero users in the tree: every site holding W `FOR UPDATE` (four prefixes, `_m2`, `_m3`, the rubric routes) asserts liveness only. It is the cheapest conversion available for F1 and the F4 prefixes.
- **L4** — `WorkspaceLifecycle._state`/`_locks` grow one entry per workspace per process with no eviction (documented as deferred).


## Appendix A11 — the remainder: admin, me, profiles, calls routes; MCP tool handlers; subprocess lifecycle; attachments; claim analysis; auth; email; digest send

#### A11 — the remainder: admin/me/profiles/calls routes, MCP tool handlers, subprocess lifecycle, attachments, claim-analysis, auth, email, digest send

Base: master 8f44a35e, worktree `/home/parlar_ai/dev/call-monitor/.claude/worktrees/agent-ac5555ae37db11da3`
(venv verified: `call_manager.__file__` is inside the worktree). Evidence came from two temporary
probe files (`tests/_probe_a11_nodb.py`, `tests/_probe_a11_db.py`, deleted afterwards) and 31
line-numbered mutations (24 + 6 re-runs with corrected lines + 1), every one applied with
`sed -i 'Ns/…/…/'`, verified as a non-empty diff, run against the test files that should pin it,
and reverted with `git checkout --`. Logs: `scratchpad/a11_mutations.log`, `a11_mutations2.log`.
Six first-batch mutations were NO-OPs (line numbers read off a concatenated `cat -n`); the script
logged them as such and they were re-run at the correct lines — no result below rests on a no-op.
`git status --short` is empty.

##### 1. Ranked findings

###### F1 — Medium — `src/call_manager/web/api/auth.py:134-180` + `auth/accounts.py:38-39` + `web/routes/oauth_google.py:161-163` — an unauthenticated `POST /api/auth/register` squats any allowed-domain address and blocks that person's Google sign-in with no admin recovery surface
`register` needs no session and no proof of address: it inserts a `pending` row for any
`@umu.se` address (:162). `resolve_google_account` then refuses the *real* owner's verified
Google sign-in on `email_collision` (accounts.py:38-39 — "never auto-link"), regardless of the
row's status, and the callback answers `/auth/error` (oauth_google.py:161-163), the same page as
a bad token. The admin surface has approve/deny/disable/enable/role/reset-password and nothing
that deletes a user or releases an email (RAN, route dump below); `deny` maps to `disabled`,
which still collides. The only way back is: admin approves the squatted row with an
`initial_password`, hands it to the person out of band, and that person later runs
Connect-Google (`oauth_google.py:136-150`).
Scenario: anyone on the hospital network posts `{"email":"<colleague>@umu.se"}` (30 per IP per
15 min, `main.py:803` → ~2,880 addresses/day/IP) — every targeted colleague's Google sign-in,
the primary production login path (third-pass:715), fails with a generic error until an admin
does the password dance. The register endpoint's anti-enumeration design makes the squat
invisible to the victim.
**RAN** (probe A, FakeOIDC harness from `tests/test_web_oauth_google.py`):
```
POST /api/auth/register {"email":"victim@umu.se"} (no cookie) -> 200 {"ok":true}; row status=pending oidc_sub=None
victim Google sign-in (email_verified, sub=g-victim) while pending -> (303, '/auth/error')
after admin POST /api/admin/users/{id}/deny                     -> (303, '/auth/error')
after admin POST /api/admin/users/{id}/enable                   -> (303, '/auth/error')
admin user routes = [GET /api/admin/users, GET /api/admin/users/pending, POST …/approve, …/deny, …/disable, …/enable, …/reset-password, …/role]
```
Fix shape: on `email_collision` where the existing row is `pending` with no `oidc_sub` and no
`password_hash` (an unverified registration), attach the verified Google identity to that row
(Google's `email_verified` is exactly the proof `register` never had) and keep it pending; or
require an emailed confirmation for password registration; and give admins a delete/"release
address" action for pending rows.

###### F2 — Medium — `src/call_manager/proposal_assist/tools/audit.py:141-175` (`list_unsupported_claims`) and `:78-94` (`validate_citations`) — one synchronous LLM HTTP call per sentence / per ledger row, sequential, on the event loop, with no per-call cap; the web route for the same audit got a 25-call cap, the agent tool did not
`ctx.citation_service.complete_json` is a blocking `httpx` round-trip (`citations/llm.py:45`,
`ranking/ollama_client.py:63`). `list_unsupported_claims_impl` calls it once per uncited sentence
with no `await` anywhere in the loop (:141-175); `validate_citations_impl` calls the UNCAPPED
`validate_citations_semantic` (:86), which stops only on `cap_exceeded` (audit.py:701-715, the
daily token cap). The owner-facing route uses `validate_citations_semantic_capped(max_fresh_calls=25)`
(`web/api/citation_audit.py:178`); the tool the drafting agent can invoke any number of times per
turn does not. This impl constructs no Session, so it is outside the prior AST rule's
"sync Session in an async def" set (A1 item 1 / third-pass §6.3) — a different mechanism.
Scenario: the agent calls `list_unsupported_claims` on a 250-sentence draft with local Gemma
(~1-3 s per call): every HTTP request, SSE stream, collab WebSocket and scheduler future on the
single loop stalls for minutes; with the Anthropic provider the same call burns the daily
citation cap in one tool call.
**RAN** (probe D): 50-sentence `proposal.md`, stub service whose `complete_json` does
`time.sleep(0.02)`, a 5 ms ticker task alongside:
`llm_calls=50 total=1.005s max_loop_stall=1.010s candidates=50` — the ticker got zero turns
for the whole duration. Converts A4's lead on `tools/audit.py:86`.
Fix shape: run the loop body via `asyncio.to_thread` (the fence's stdlib rule, as `tools/history.py`
does), route the tool through `validate_citations_semantic_capped`, and bound sentences per call.

###### F3 — Low — `src/call_manager/web/api/calls.py:380` (`page: int = Query(ge=1)`, no `le`) via `web/dashboard.py:256`; `web/api/admin_users.py:50,61` and `:79,90` — an oversized `page` overflows int8 in the OFFSET and 500s
`offset = (page - 1) * limit` with `limit ≤ 200` exceeds Postgres int8 once `page > 4.6e16`;
`admin_users_list`/`admin_pending_list` clamp only the low end (`max(1, page)`). Any authenticated
user can do it on `/api/calls`; A3 listed the admin variant as an unrun lead.
**RAN** (probe B, `raise_server_exceptions=False`):
`GET /api/calls?page=1000000000000000000` → `500 {"error":{"code":"internal_error",…}}`;
`GET /api/admin/users?page=1e18` → 500; `GET /api/admin/users/pending?page=1e18` → 500;
`GET /api/calls?page=9223372036854775808` → 500 (four requests, four sanitized 500s; the
driver-level exception class was not extracted from the JSON log).
Fix shape: `Query(ge=1, le=100_000)` (→ 422) on all three, or clamp.

###### F4 — Low — hollow gate — `tests/test_api_admin_users.py:122-126` `test_approve_rejects_non_pending` never reaches the guard it names (`admin_users.py:151-152`)
The test approves the active admin itself, who has no `oidc_sub` and sends no
`initial_password`, so its 400 comes from `admin_users.py:153-154` ("initial password
required"), not from the status guard. Shape: a test asserting a status another guard already
produces.
**RAN** mutation `151s/if u.status != "pending":/if False:/`:
`tests/test_api_admin_users.py` → **27 passed**; widened to `test_api_admin_users.py
test_api_admin_ops.py test_auth_inactive_enforcement.py test_admin_bootstrap_status.py
test_api_session_revocation.py` → **50 passed**. Reverted.
Why it matters: `approve` rewrites `status`, `is_admin`, `password_hash` and the epoch
(:155-164) with none of the disable/enable/role guards — a regression of this one line turns
it into an un-guarded re-activate / promote / demote-the-last-admin path (approve yourself as
`member` skips `_lock_active_admin_count`). Today the guard is present; only the pin is missing.
Fix shape: approve an `active` user WITH an `initial_password` and assert 400 + unchanged row.

###### F5 — Low — hollow gate — `src/call_manager/web/api/files.py:96-103` (declared MIME must equal the sniffed format) has no pin
`test_api_files_upload_bad_mime_is_400` (`tests/test_api_files.py:71-75`) sends
`application/pdf`, rejected by the allowlist at :89; `test_api_files_upload_non_image_is_400`
sends non-image bytes, rejected by the PIL probe at :94. Neither sends a real PNG declared as
`image/jpeg`.
**RAN** mutation `96s/if detected_mime != content_type:/if False:/`:
`tests/test_api_files.py` → **12 passed**; widened to `+ tests/proposal_assist/
test_turns_submit_with_attachments.py tests/proposal_assist/attachments/test_resolver.py` →
**39 passed**. Reverted.
Why it matters: with the guard gone a PNG uploaded as `image/jpeg` is stored as `.jpg` with
`mime_type=image/jpeg` (:105,:124), served with that media type (:206), and resolved into the
model message as `media_type: image/jpeg` over PNG bytes (`attachments/resolver.py:129-131`),
which the API rejects — every turn carrying that attachment fails.
Fix shape: upload a valid PNG with `mime="image/jpeg"`, assert 400 and no row/file.

###### F6 — Low — `src/call_manager/proposal_assist/tools/registry.py:441, 540, 566, 612` — four handlers coerce `int(args.get(…))` OUTSIDE `_safe_tool`; the authors fixed exactly one (`list_citations`, :710-718)
The comment at :710-712 states the contract ("so a non-coercible value returns a tool error
envelope via _safe_tool rather than raising int(...) OUTSIDE the crash-guard") and applies it to
one of five handlers. For the other four the exception escapes the handler; the SDK's in-process
MCP dispatcher catches it at `claude_agent_sdk/_internal/query.py:716-721` and returns JSON-RPC
error `-32603` with `str(e)` — so the model gets a Python error string instead of the
`_mcp_error` envelope, and `_safe_tool`'s `logger.exception` never fires (the operator sees
nothing).
**RAN** (probe C, handlers called directly):
`get_artifact_history {"limit":"abc"}` → RAISED ValueError; `list_references {"limit":"abc"}` →
RAISED; `search_references {"top_k":"abc"}` → RAISED; `read_reference {"max_chars":"abc"}` →
RAISED; `list_citations {"limit":"abc"}` → envelope `is_error=False` (coerced inside the impl).
Fix shape: move each coercion inside its impl (as `list_citations_impl:93-97` does).

###### F7 — Low — hollow gate — `src/call_manager/proposal_assist/tools/citations.py:184-186` (`propose_citation` refuses a foreign-workspace reference) has no pin in any of 7 test files
**RAN** mutation `185s/if ref is None or ref.workspace_id != ctx.workspace_id:/if ref is None:/`:
`tests/proposal_assist/tools/test_propose_citation.py` → **9 passed**; widened to
`+ test_f3d3a_relocation.py test_f3d3b_conversion.py test_tools_citations_deleted.py
test_web_proposed_changes.py collab/test_collab_kernel_citation.py test_tools_end_to_end.py` →
**57 passed**. Reverted. Same class as A3-F6 (`human_cite.py:157`): consequence today is
bounded because the chunk lookup at :192-200 is workspace-scoped (a foreign reference becomes
"chunk not found" instead of "reference not found"), but the named guard is unpinned.
Fix shape: seed a reference in a second workspace and assert `{"error": "reference not found"}`.

##### 2. Checked and sound (do not respend)

Mutations that went RED (guard pinned; file → result):
- M1 `admin_users.py:203` last-admin disable → `test_api_admin_users.py test_api_admin_ops.py` **1 failed, 36 passed**; M2 `:229` last-admin demote → **1 failed, 36 passed**; M24 `:160` approve epoch bump → **1 failed, 26 passed**.
- M4 `me.py:172` current-password check → `test_api_me_password.py test_api_me.py` **2 failed, 7 passed**.
- M5 `web/api/auth.py:106` login status gate → **1 failed, 25 passed**; M22 `auth.py:153` register domain gate → **1 failed, 22 passed**.
- M6 `web/dashboard.py:208` `_ilike_escape` → `test_dashboard_readmodel.py test_api_calls_feed_behavior.py test_api_calls.py` **1 failed, 50 passed**.
- M7b `admin_retention.py:225` capability gate → **3 failed, 25 passed**; M8b `:170` audit commit→rollback → **5 failed, 23 passed**; M9b `:281` fresh liveness re-read under the mutex → **1 failed, 27 passed**.
- M11b `turns/processor.py:317` post-claim daily-cap recheck → `test_spend_chat_turn.py test_turns_processor.py` **1 failed, 21 passed** (pinned by `test_spend_chat_turn.py:360`; my first run against `test_turns_processor.py` alone was green — wrong file, not a hollow gate).
- M12 `digest/send.py:183` bounce threshold → **1 failed, 10 passed**; M23 `send.py:38` status filter → `+ test_digest_status_filter.py` **1 failed, 11 passed**.
- M13 `claude_adapter.py:274` `tools=[]` → **1 failed, 27 passed, 1 skipped**; M14 `:282` `setting_sources=[]` → **1 failed, 27 passed, 1 skipped**.
- M15 `profiles.py:342` update 403 → **2 failed, 9 passed**.
- M16b `attachments/resolver.py:107` cross-workspace attachment → **1 failed, 26 passed**.
- M17 `tools/references.py:36` `_load_workspace_reference` scope → **1 failed, 40 passed**.
- M19b `auth/rate_limit.py:48` window check → `test_auth_rate_limit.py test_api_auth.py` **4 failed, 19 passed**.
- M20 `web/api/calls.py:298` best-profile ownership → `test_api_call_detail.py` **1 failed, 7 passed** (first run against `test_api_calls.py` was green — wrong file).
- M21 `drafting/registry.py:385` `begin_in_flight_exclusive` → **1 failed, 29 passed**.

By reading / probe:
- A4's lead "broken limiter fails open" (`tools/references.py:506-512`) is a dead branch: the only wired limiter is `references/admission.py:19-27`, whose `try_acquire` is `threading.Semaphore.acquire(blocking=False)` and cannot raise. Refuted as a harm path.
- Last-admin guards: `_lock_active_admin_count` takes `FOR UPDATE` on the active-admin rows then re-counts; under READ COMMITTED the second of two concurrent disables re-evaluates the predicate after the first commits and sees one admin → 400. Approve-as-admin and enable add admins without the lock, which is safe.
- Subprocess teardown (`claude_adapter.py:627-677`): disconnect bounded at 3 s, cancellation-resistant disconnect is abandoned, `process.kill()` is SIGKILL (a subprocess ignoring SIGTERM is covered), reap bounded at 2 s, unconfirmed deaths stay in the inventory and count against the ceiling (`registry.py:148-190`); capacity eviction pops the victim under the lock and releases its advisory lock only after terminate, so a concurrent relaunch gets `LockNotAcquired` rather than two subprocesses.
- `drain.py:108-156`: `wait_for` cancellation reaches `processor.py:477` (`BaseException` → finalize failed → re-raise), then interrupt (2 s) then terminate; `record_terminal_outcome` stores `raw_error` only in the audit event, never in the viewer-facing chat row (`outcomes.py:49`).
- Attachments: suffix allowlist at path construction (`storage.py:47-50`), MIME/format equality (files.py:96 — unpinned, F5), 5 MB chunked cap, lazy `Image.open` + 64 Mpx cap on both probes, `resolver.py:99-107` collapses missing/foreign; upload caps are 5 MB (images) vs 50 MB (references) by design, not an inconsistency.
- Digest: week-claim is inserted+flushed before the send; bounce deletes the claim and counts; transport error releases it; `candidate_user_ids` filters status and bounce-disabled (both pinned above); `EmailMessage.__setitem__` refuses CR/LF in `To` (an injected local part fails per-user, logged, batch continues); `build_subject` strips CR/LF.
- `safe_next`, `TransactionCipher` (HKDF-separated Fernet with TTL), `domain_allowed` (IDNA canonicalization, strict `@` split), `get_current_user` (status gate before epoch), `turn_gating` (unknown `request_type` denies every tool).
- Retention inspection: capability gate first, no audit row on refusal, audit committed before any content is returned, `Cache-Control: private, no-store`, liveness checked before and after the mutex (all three pinned: M7b/M8b/M9b).
- `process_turn_once` rechecks the global daily cap after the claim (pinned, M11b) and reconciles usage when the finalize loses the row.

##### 3. Prior findings touched (status only)
- A1 item 1 / third-pass §6.3 (sync work on the loop inside async tool impls) — still open; F2 is a distinct instance that the Session-based AST rule does not count.
- A1 item 19 (`_SCRUBBED_ENV_KEYS` omits `INITIAL_ADMIN_PASSWORD`/`GOOGLE_CLIENT_SECRET`, `claude_adapter.py:93-100`) — still open.
- A1 item 15 (login limiter consumed on success, `auth.py:90`) and item 16 (logout accepts epoch-stale cookie) — still open.
- A3 lead "`admin_users_list` page overflow" — now RAN, and it is not admin-only (F3).
- A3 F4 (`add_member` registered-email oracle, accepts pending/disabled) — still open (not re-run).
- A4 lead `tools/audit.py:86` uncapped semantic validator — confirmed and measured (F2). A4 lead fail-open limiter — refuted (dead branch).
- A8 F2 (`test_reset_password_has_no_status_gate` status-only) and F3 (`test_reenable_already_enabled_noop`) — still open.
- Security review Medium (admin reset-password no per-route rate limit) — unchanged.
- A10 L2 (delete cannot cancel a running claim-analysis job except via its own probe) — consistent with `pipeline.py:438` / `audit.py:599`: the probe runs per sentence/row, so cancellation is cooperative and bounded by one LLM call.

##### 4. Not read
`web/api/export.py`, `citation_audit.py`, `annotations.py`, `rubric.py`, `references.py` bodies
(A3/A4 lens); `turns/submit.py`, `caps.py`, `compaction.py`, `selection.py`;
`drafting/live_clients.py`, `locking.py`, `factory.py`, `fake_adapter.py`, `contract.py`;
`analysis/queue.py`, `analysis/insert.py`; `auth/oidc.py` (excluded by brief) and
`web/routes/oauth_google.py` outside :125-186; `digest/assemble.py` and both digest templates;
`profiles/*` services, `ranking/golden_set.py`; `scheduler.py` beyond the grep hits;
`main.py` outside the handler block; the React admin/auth/profile pages; test bodies other than
the ones cited.

##### 5. Leads (no proof)
- `fetch_reference` (`tools/references.py:419-577`) is an agent-controlled outbound GET to any public host: the query string is preserved (`references/fetcher.py:180-181`), there is no host allowlist, only the private-IP refusal, three per turn — a reference that steers the model can carry draft text to an external host in the URL. The only mitigation is the system-prompt framing (`claude_adapter.py:110-120`); the third-pass refuted "no framing", not this channel.
- `web/dashboard.py:211-227` `_search_predicate`: a 200-char `q` splits into up to ~100 terms × 4 `ILIKE` legs including `full_text`; single-character terms defeat pg_trgm, so each row evaluates hundreds of pattern matches over the full call body. Unmeasured.
- `web/api/admin.py:396-408` `admin_reenable_user` clears `digest_disabled_at` but not `digest_bounce_count` (already ≥ 3), so the re-enabled user is re-disabled on the very next bounce instead of after three.
- `tools/references.py:71` `list_references_impl` interpolates the agent's `q` into `ILIKE` unescaped while `read_reference_section_impl:359` escapes; over-match only, agent-facing.
- `web/api/profiles.py:213-282` `create_profile` neither strips nor blank-checks `name` (an empty-string profile is creatable; uniqueness is exact-match). READ only.
- `admin_users.py:186` maps `deny` → `disabled`, so a denied registrant is indistinguishable from a disabled colleague in `/api/admin/users` and can be `enable`d into an active, credential-less account.
- `register` accepts a local part containing CR/LF (`auth/domain.py:33-36` only checks the `@` split); stored verbatim, it makes that user's digest fail every week on the SMTP path (`EmailMessage` refuses the header) — harmless but unvalidated.
- `turns/processor.py:353` falls back to the relative `Path("var/workspaces")` when `workspaces_dir` is omitted; the drain passes `settings.workspaces_dir` (`scheduler.py:757`), so this is unreachable in production but is a second copy of the CWD-relative default.

