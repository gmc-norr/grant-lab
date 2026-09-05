# Call-Monitor Architecture Review — 2026-09-05

Tree: `master` @ `ed1153a3` (93 commits after the 2026-09-02 review of record, `8f44a35e`).
Every number below was produced by a command run on this tree today; the command is
given where it is not obvious. Nothing is carried over from an earlier review without
re-measurement. Method: `measure-dont-predict`.

## 0. Verdict in one paragraph

**Grade B+**, up from the third pass's B− and the 2026-09-02 current-tree B. The
defect backlog that drove those grades is closed: review §7 items 1–12 are merged, the
ports conversion that was "excellent and unused" on 2026-08-20 now has five production
callers, the async floor is on, the spend ledger exists, a schema-drift gate exists, and
the e2e specs run in CI. What is left is **structural choice, not defect**: the product
was just cut into two editions at the API and bundle surface but not at the process
boundary; the proposal document still lives in two peer stores; and the copy-paste
species the SOLID review named is unchanged. None of these harm a user today. The one
thing that has kept this project below A− for three reviews running is unchanged too:
**the clone has zero git remotes**, so CI has never executed and the only copy is one
disk.

## 1. Baseline (all run today)

| check | result |
|---|---|
| `uv run pytest -n 16 --dist loadfile -q` | **5029 passed, 1 skipped**, 122.9 s |
| `pnpm exec vitest run` | 136 files, **973 passed**, 12.2 s |
| `pnpm exec tsc --noEmit` | exit 0 |
| `pnpm run lint` (oxlint `--deny-warnings`) | clean |
| `uv run ruff check src tests alembic scripts` | clean |
| `uv run alembic heads` | one head, `0070` |
| `git remote \| wc -l` | **0** |

The 1 skipped is the `ANTHROPIC_API_KEY`-gated live-SDK test (CLAUDE.md). The base was
4387 on 2026-09-02 and 4857 on 2026-09-03; the profile-visibility and Call Match r1a
merges account for the rest.

## 2. Shape of the system

Lines are `wc -l` over `*.py` / `*.ts,*.tsx`, `__pycache__` excluded.

| area | LOC | files | share |
|---|---|---|---|
| `src/call_manager` total | 60,063 | 287 | |
| `proposal_assist/` | 35,551 | 179 | **59%** |
| `web/` | 9,027 | 33 | 15% |
| the Call Match half (`sources`+`ingestion`+`embeddings`+`ranking`+`profiles`+`digest`+`email`) | 8,419 | | **14%** |
| `db/` | 2,378 | 5 | |
| top-level `scheduler.py` / `config.py` / `main.py` / `workspaces.py` | 1,203 / 990 / 921 / 407 | | |
| `tests/` | 123,916 | 585 | 2.06× src |
| `frontend/src` | 47,541 | 294 | |
| of which `api/schema.gen.ts` (generated) | 15,565 | 1 | 33% |
| of which `cockpit/`+`editor/`+`collab/` (release 2 only) | 17,537 | | 37% |

Other counts: **45** mapped tables, **70** migration files, **115** `Field(...)` settings
in `config.py`, **27** `sched.add_job(...)` registrations, **12** files in
`tests/architecture/`, **6,646** lines in `tests/fencing/` guarding **1,431** lines of
fence/ports source.

Runtime shape: one FastAPI process that also hosts APScheduler, the collab
`RoomManager`, the drafting-subprocess registry and the rate limiters. A second
instance refuses to start (`025164a5`). Deployment is a wheel that carries the built
SPA; `docker-compose.yml` defines only the Postgres service (`pgvector/pgvector:pg16`);
there is no Dockerfile or unit file in the repo.

## 3. The package graph, measured

`scratchpad/importgraph.py` walks every module with `ast` and counts
`call_manager.*` import statements between top-level packages.

Heaviest edges: `web → proposal_assist` **98**, `proposal_assist → db` 82,
`main → proposal_assist` 25, `scheduler → proposal_assist` 19,
`workspaces → proposal_assist` 8. The Call Match packages import nothing from
`proposal_assist` — the split the spec wants is real at the package level.

**Four package-level cycles** (a lower layer importing a higher one):

| cycle | the lines | what it means |
|---|---|---|
| `workspaces` ↔ `proposal_assist` (8 / 1) | `workspaces.py:29-39` imports eight `proposal_assist.*` modules; `proposal_assist/branches.py:237` imports back lazily | Top-level `workspaces.py` is a proposal-assist lifecycle service (create → `workspace_setup_jobs`; delete → cancel five job families) wearing a top-level name. It belongs under `proposal_assist/`. |
| `digest` → `web` (2) | `digest/assemble.py:23` → `web.profile_access.readable_profile_ids`; `digest/render.py:9` → `web.filters` | `profile_access.py` imports only SQLAlchemy and `db.models` — it is the **domain read rule for profiles**, single-sourced by `tests/architecture/test_profile_access_single_source.py`, and it lives in the HTTP layer. A scheduler job imports `web` to get a domain rule. |
| `auth` → `web` (1) | `auth/csrf.py:41` → `web.api.errors` (FastAPI `JSONResponse`) | `auth` is imported by `proposal_assist` (12) and `ingestion` (2), so the ingestion path transitively imports the web error envelope. Harmless today. |
| `embeddings` ↔ `ranking` (2 / 1) | `embeddings/client.py:16`, `refresh.py:20` → `ranking.ollama_client.LLMError`; `ranking/rank_pair.py:400` → `embeddings.similarity` lazily | `LLMError` is the shared Ollama error and has no natural home. |

Inside `proposal_assist` there are **14** sub-package 2-cycles; `citations` sits in 8 of
them (`collab`, `analysis`, `proposals`, `editing`, `drafting`, `tools`, `references`,
`turns`). That is not a defect list — it says that "who owns a citation" is the one
question inside proposal-assist that has no single owner.

## 4. Findings

Ordered by what a reader should act on first, not by severity label.

### 4.1 Zero remotes; CI has never run (unchanged, third review in a row)

`git remote` prints nothing. `.github/workflows/ci.yml` now has two jobs (`test`, and
an `e2e` matrix over `dev` and `prod`) with 39 named steps, including the match-bundle
gate, the wheel-carries-SPA assertion and the Playwright specs — **none of which has
ever executed**. Local state that would be lost: 12
branches, of which 2 are unmerged (`feat/call-match-r1b`, `dry/call-match-r1b`) and 9
are merged and undeleted; 3 worktrees under `.worktrees/`. The only off-tree copy is
`~/db-backups` (2026-09-02 review). Every gate in §5 is a gate that has only ever been
run by hand.

### 4.2 The match edition is a surface cut, not a process cut

What the edition does: `main.py:892` skips 11 proposal-assist routers;
`web/api/__init__.py:82` composes the `/api` router per edition; `vite.config.ts:86`
aliases `virtual:app-router` to `router.simple.tsx` (15 `path:` entries vs 22);
`scripts/check_match_bundle.mjs` and `tests/architecture/test_edition_surface.py` pin
that no cockpit module reaches the match bundle; `config.py:895` refuses
`APP_EDITION=match` with drafting on. This is well built and well gated.

What it does not do, measured:

```
$ APP_EDITION=match DRAFTING_ENABLED=false uv run python -c "import sys, call_manager.main; ..."
claude_agent_sdk loaded: True
pycrdt loaded: True
proposal_assist modules loaded: 142
call_manager modules loaded: 237
```

A match-edition process imports the Claude Agent SDK (`tools/registry.py:30`,
`drafting/claude_adapter.py:49`, both top-level), pycrdt, and 142 of the 237
`call_manager` modules — 60% of the module graph, for the 14% of the code the customer
asked for. `scheduler.py` registers all 27 jobs in both editions; the six
proposal-assist ticks each early-return on `if not settings.drafting_enabled:`
(6 sites), and the two reconcile ticks deliberately do not (`scheduler.py:652,680`).

Consequences: the match deployment's dependency set, boot-time import graph and
scheduler job table equal the full edition's; an import-time failure anywhere in
`proposal_assist` takes the match edition down; and the release-1 customer's box needs
the SDK installed to run a product that never calls it.

**Recommendation.** Not a package split. Two lazy seams: `main.py` and `scheduler.py`
each get one `if settings.app_edition != "match": from call_manager.proposal_assist.boot
import register(app|sched)` and the 25 and 19 `proposal_assist` import statements the
graph counts in those two files move behind it. Then a gate
in `tests/architecture/`: import `call_manager.main` under `APP_EDITION=match` in a
subprocess and assert `claude_agent_sdk`, `pycrdt` and `call_manager.proposal_assist.*`
are absent from `sys.modules`. **What trivially passes it:** a subprocess that fails to
import at all and reports an empty `sys.modules`; assert `call_manager.main` *is*
present too. Against today's tree the probe above already returns True/True, so this is
a gate that can go red. Do this before release 1 ships, because it changes nothing the
customer sees and everything about what can break their box.

### 4.3 The proposal document has two peer stores, and no decision has been recorded

The per-workspace git repo and the Yjs update log are both written, and neither is
declared truth. The 2026-08-30 first-principles pass (memory, and
`notes/2026-06-17-editor-round-trip-spike-findings.md` before it) traced the commit
mutex, the epoch fence, the checkpoint layer, the HEAD-lags-the-log data-loss class and
the impossibility of auto-merge to that one fact. The workflow that was testing "log is
truth, files are a one-way projection" was displaced by the Call Match split on
2026-09-03 and its result is not in the tree. The cost of not deciding is visible in §2:
1,431 lines of fence source under 6,646 lines of fence tests, 59 source files that
touch `repo_dir`/`gitio`, and a `plans/2026-08-25-reconcile-fence.md` at revision 2,
unexecuted.

**Recommendation.** Write the decision down as *deferred until release 1 ships*, and
freeze fence slices until it is made. Do not execute the reconcile-fence plan in the
meantime; its own review concluded it is "a tidy-up, not a bug fix". Release 1 touches
none of this.

### 4.4 The DRY-drift species is unchanged

Re-measured against the 2026-08-20 SOLID review's three confirmed highs:

| 2026-08-20 finding | then | now |
|---|---|---|
| `ToolContext` fields (`tools/registry.py:46`) | 28 | **29** |
| `_collect_tools` (`registry.py:314` → EOF) | 657 lines of 1,021 | **674 lines of 987** |
| pass runners | 4 | **4** (`review` 576, `uncited_claims` 379, `evaluator` 365, `coherence` 365) + `ranking/worker.py` 427 + `profiles/bootstrap_worker.py` 171 |
| evict-or-not ceremony (`MANAGER.operate` sites) | 4 | **4** in 2 files |
| `db/models.py` | 2,037 lines | **2,264** lines, 9 `*Job` classes |
| `app.state` reached from `web/` | ~95 | **160** lines |

The pass-family unification decided on 2026-08-30 is the fix and is parked with §4.3.
Nothing in release 1 needs it: the match edition runs `ranking/worker.py` and
`bootstrap_worker.py` only. One vocabulary note for release 1's new tables (boards,
triage decisions, notify-on-change): the seven proposal job tables say `queued`,
`ranking_jobs` says `pending`, and `completed`/`done` split five–five
(`docs/ARCHITECTURE.md` "Status vocabularies"). Pick `queued`/`completed`.

### 4.5 The gate apparatus is a subsystem, with a subsystem's maintenance cost

Twelve architecture pins, the fencing gates, a generated `ARCHITECTURE.md` that a test
regenerates, an investigations index pinned for completeness, a route manifest, an
OpenAPI drift check, a bundle gate and an edition-surface gate. Its measured strength is
real (16/16 mutations caught on 2026-09-02). Its cost is that most of it is **file- or
name-keyed** — `FENCING_IMPORT_FILES`, the `spend_guard` pinned dict,
`test_profile_access_single_source.py`, `TOOLS_NOT_YET_CONVERTED` (5 files still
listed: `audit.py`, `citations.py`, `history.py`, `proposal.py`, `references.py`). Every
move in §3 has to re-pin a gate, and a gate re-pinned by editing a name can pass for the
wrong reason. The repo's own rule applies: re-prove each moved gate by mutation, not by
green.

### 4.6 The suite still writes into a live data file

```
$ wc -l data/golden_set_history.jsonl ; uv run pytest tests/test_golden_set_eval_db_mode.py -q ; wc -l ...
before=1088 after=1090 delta=2
```

`tests/test_golden_set_eval_db_mode.py:60,75` monkeypatch `_REPORT_PATH` only;
`test_golden_set_eval_history.py:61` shows the second patch that is missing. The
2026-09-03 second look recorded this for two files; one was fixed. The live file is
1,090 rows today, the same file the weekly golden job appends to, and the second look
already found it test-polluted. Two-line fix, or an autouse fixture in
`tests/conftest.py` for `eval_mod`.

### 4.7 Spend has one ledger and two read paths (known, acceptable)

`turns/caps.py:100` sums `chat_turns.tokens_in/out` for the per-workspace cap;
`caps.py:154`'s docstring says the global cap reads `llm_spend`. The ledger is
centralised storage, not the single read path. Recorded so nobody re-derives it; not
worth changing before the store decision in §4.3.

### 4.8 Ops residue on the one box this runs on

`app.log` in the repo root is 94,106,614 bytes, gitignored, unrotated — the `run`
skill's uvicorn log. `api_key.txt` in the root is gitignored (108 bytes; not read).
Neither is architecture; both are the kind of thing that matters when the whole system
is one process on one disk with no remote.

## 5. Release-1 structural watch list

From `specs/2026-09-03-call-match-release-1-design.md` read against the tree.

- **The public board is the first unauthenticated read of ranked data.**
  `build_dashboard_view(profile_id)` takes no user, so the profile read rule
  (`readable_profile_ids`) is not in its path by construction. The spec keys the board
  to admin-approved `board_items`, not a live profile; the gate to add when the router
  lands is a `tests/architecture/` pin that the public router imports nothing from
  `proposal_assist` and that every public path is in `test_auth_allowlist.py`'s list.
- **Notify-on-change has no substrate.** `Call` carries no content hash or
  `updated_at`, and ingestion upserts without comparing. Adding comparison touches the
  boundary that produced the SSRF fail-open and the poison-record outage; the spec
  already sequences it last. Keep it last.
- **Triage decisions migrate live rows.** `user_call_dismissals` is `(user_id, call_id)`
  with no profile; the spec's `call_decisions` is a migration over data that exists.
- **`APP_EDITION` must agree in three places** (backend setting, `VITE_APP_EDITION`,
  the packaged bundle's marker). `spa_serving.py:75-88` refuses a disagreement at boot,
  which is the right failure. §4.2's seam adds a fourth reader; put it behind the same
  setting.

## 6. What is closed since the last review of record — do not re-derive

Verified on this tree: `doc_epoch` (stale Y.Doc refused on reconnect); `published_at`
in the upsert; reference ingest caps (chunks 500, error body, deadline, `RLIMIT_AS`);
second-instance refusal + `/healthz` lock report; the three ORM-declared indexes and
`tests/test_schema_drift.py`; ruff `ASYNC` in `select`; `llm_spend`; oxlint as the JS
gate; `pytest-timeout`; Playwright specs in CI; `build_ports` with 5 callers
(`review/agent.py:48`, `drafting/factory.py:100`, `evaluator/agent.py:56`,
`coherence/agent.py:53`, `uncited_claims/agent.py:49`); profile read rule single-sourced.

## 7. Recommended order

1. **Add a remote and push, today.** Or `git bundle` into `~/db-backups` until one
   exists. Expect the first CI run to be red somewhere; that is the point.
2. **Fix §4.6** (two lines). Then `git checkout data/golden_set_history.jsonl` is no
   longer part of anyone's workflow.
3. **§4.2 before release 1 ships**: the two lazy seams and the boot-import gate.
4. **§3's three cheap moves** when a slice already touches them: `profile_access.py` →
   `profiles/`, `LLMError` → a shared module, top-level `workspaces.py` →
   `proposal_assist/`. Re-prove the file-keyed gates by mutation.
5. **Decide §4.3 before any release-2 slice.** Then pass-family unification, then
   Directions — the order already decided on 2026-08-30.
6. Delete the 9 merged branches; prune the worktrees whose branches are merged.
