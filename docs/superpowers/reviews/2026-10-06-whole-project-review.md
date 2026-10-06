# Grant Lab whole-project review — 2026-10-06

Tree: `master` @ `c94f2de3` (228 commits after the 2026-09-05 architecture review's `4e6212e2`;
the checked-out branch `chore/ranking-sonnet-5-cap-150` is identical to `master`). Every
number below was produced by a command run on this tree today. Nothing is carried over from an
earlier review without re-measurement. Method: `measure-dont-predict`; nine worktree-isolated
reviewers, each required to break the code and paste the failing assertion rather than read.

## 0. Verdict in one paragraph

**Grade B+, unchanged from 2026-09-05 — but for different reasons.** The defect backlog that
three reviews carried is closed: of the **94** findings earlier reviews left open, **57 are
fixed, 7 partially, 4 no longer apply, and none of the prior Highs remains High** (§5). The
match edition is now a real process cut (0 proposal-assist modules at boot, gate reddens under
mutation), migrations round-trip with no model drift, the suite no longer writes into `data/`,
and a GitHub remote exists. The code itself is unusually well pinned: across nine subsystems
reviewers ran **~120 mutations and all but ~10 reddened the named test for the stated reason**
(§4). What keeps the grade at B+ is three things that are live today and none of which the
gates can see: **the backend test job has never executed on GitHub** (a bad action pin kills it
at "Set up job", on every push since June, and 63 commits are unpushed); **one hosted spender
bills Opus outside every cap and ledger**; and **every EU call closes in the feed about 15
hours early** because SEDIA encodes deadlines as midnight UTC and nothing bumps them to the
17:00 Brussels close — 670 of 676 stored EU deadlines sit at 00:00 UTC right now.

## 1. Baseline (all RAN today)

| check | result |
|---|---|
| `uv run pytest -n 16 --dist loadfile -q` | **5795 passed, 1 skipped**, 158 s (the skip is the `ANTHROPIC_API_KEY`-gated live test) |
| `uv run pytest --collect-only -q` | 5796 collected |
| `pnpm exec tsc --noEmit` / `pnpm run lint` | exit 0 / clean |
| `pnpm exec vitest run` | 149 files, **1229 passed** |
| `uv run ruff check src tests alembic scripts` | clean |
| `scripts/gen_architecture.py` → `git diff docs/ARCHITECTURE.md` | empty |
| `scripts/gen_no_build.py --check`, `uv lock --check` | in sync / ok |
| `uv run alembic heads` | one head, `0081` |
| `git remote -v` | `origin` = github.com/parlar/grant-lab |
| `git rev-list --left-right --count origin/master...master` | **0 / 63** — 63 local commits unpushed; last push `6c0772df` on 2026-09-09 |
| CI on `master`, latest run | `test` job **failed at "Set up job"**; `match-e2e` failed (1/31); `e2e (dev)`, `e2e (prod)` passed |
| `uvx pip-audit` on `uv export` | **25 advisories / 5 packages** (anyio, urllib3, httpcore2, httpx2, pyjwt) |
| `pnpm audit --prod` | 4 high (nanoid ×2 via `ics`, `@tiptap/core`, `prosemirror-view`), 1 moderate |

The base was 5029 on 2026-09-05 and 5530 on 2026-09-08; Plan C auto-publish, decision notes,
SEDIA, ranking freshness and the 10-05 spend fixes account for the rest.

## 2. Method

Nine reviewers, one subsystem each, every one in its own git worktree with its own `.venv`,
briefed with the claims to attack and forbidden from fixing anything. Each report carries
per-finding `RAN`/`READ` labels, and every `RAN` mutation had to redden a named test for the
claimed reason (a collection error or a hang proves nothing). The coordinator ran the baseline
above, re-verified the three Highs independently, and ran read-only probes against the live
database for the two findings whose severity depends on live data (§3 H2, §3 H3, §3 M1). Full
reports with every probe command are in the appendices.

One method finding worth recording: **the Agent tool's worktree isolation cut every reviewer's
worktree at `origin/master` (`6c0772df`), 63 commits behind the tree under review.** All nine
caught it because the brief stated the expected sha and made them print it first; each
fast-forwarded before measuring. A review package that does not pin the sha would have reviewed
a tree without ranking freshness, SEDIA, auto-publish or the spend fixes.

## 3. Findings — ordered by what to act on first

Severity: **HIGH** = money, data, or silently wrong results in production today; **MED** = a
user-visible defect or a gate that cannot fail; **LOW** = hygiene. Citations name the file and
the symbol; line numbers rot.

### H1 — [RAN] The backend test job has never run on GitHub, and 63 commits exist only on this disk

`.github/workflows/ci.yml`, both OSV steps: `uses: google/osv-scanner-action@764c9181…`. The
repository root `action.yml` at that sha has **no top-level `runs:`** (verified via the GitHub
API: 0 occurrences; the composite action lives at `osv-scanner-action/action.yml`, which has
`runs: using: docker`). GitHub therefore rejects the entire `test` job before step 1 — on
every push since the pin landed on 2026-06-16 (`8df52a35`). `gh run list --branch master
--workflow CI --status success` is empty: pytest, ruff, the lock/no-build/route/OpenAPI drift
gates, pip-audit, oxlint, vitest, the match-bundle gate, the wheel-carries-SPA gate and
`pnpm audit` have **never executed in CI**. The 2026-09-05 review graded "zero remotes, CI has
never run" as the one thing holding the project below A−; the remote now exists and the
sentence is still true. Meanwhile `origin/master` is `6c0772df` (2026-09-09): the SEDIA
rewrite, logprobs opt-in, ranking freshness, the 2026-09-08 review fixes and today's Sonnet 5.5
default are on one disk. The audits that job would have run are red: 25 pip advisories
(`pyjwt` 2.13.0 with 13 advisories is transitive via `claude-agent-sdk`→`mcp`, not used by
auth; `anyio`, `urllib3` are runtime) and 4 high pnpm advisories, of which only `nanoid` (via
`ics`) reaches the match bundle.

Fix: `uses: google/osv-scanner-action/osv-scanner-action@764c91816374ff2d8fc2095dab36eecd42d61638`
at both sites; `git push origin master`; expect the job to then go red on the audit steps and
bump. Two related items: `dependency-review.yml` can never pass on a private repo without
Dependency graph + GHAS enabled (every Dependabot PR is red by construction — enable it or
delete the workflow), and `match-e2e` is red on master because of M8 below.

### H2 — [RAN] Phase 0 workspace setup bills Opus twice per new call, outside every cap and the ledger

`scheduler.py` `build_scheduler._phase0_tick.make_client` builds
`AnthropicClient(model=settings.proposal_assist_llm_model)` (= `claude-opus-5`, default
`max_tokens=4096`, SDK `max_retries=2`); `phases/setup.py` `run_phase0_tick` → `Phase0Worker` →
`extractors/refresh.py` `compute_section_extracts` / `compute_rubric` →
`extractors/sections.py` `extract_sections` and `extractors/rubric.py` `extract_rubric`, each
one `client.chat(system + full call text)` that discards `envelope["usage"]`. `graft callers
record_spend` and `graft callers check_global_daily_token_cap` list **nothing** under `phases/`
or `extractors/` (coordinator re-ran both); `SpendKind` has no kind for it; `config.py` has no
setup cap. The reviewer drove the real tick with the global cap already tripped
(`tokens_in=10_000_000` seeded): `global cap tripped=True; hosted chat() calls made=2;
llm_spend rows before=1 after=1`. Bounded in practice by the rate of new workspaces on new
calls, which is why nobody noticed; unbounded in principle, and invisible on the spend view.

Fix: `check_global_daily_token_cap` before `client_factory()` in `run_phase0_tick` (requeue on
denial); `record_spend(kind="setup", …)` on every terminal path of both extractors;
`max_retries=0` as compaction already does.

### H3 — [RAN, confirmed on live data] Every EU call closes in the feed at 02:00 Stockholm on its last day, not 17:00 Brussels

`sources/eu_portal.py` `_parse_iso` / `_deadlines` / `_normalize`. SEDIA's `deadlineDate`
values are `…T00:00:00.000+0000` (the live-captured fixture
`tests/fixtures/eu_portal_search_page.json` has 2/2 at midnight; the EU tests' `_meta` helper
assumes `T17:00:00.000+0000`, which the fixture contradicts). The adapter stores that instant
unchanged and `skip_expired` compares `deadline <= now`; the dashboard (`open_expr`), digest,
ranking timeline gate and admin open-call count all read the same column. The Vinnova adapter
solved the identical shape with `end_of_day_if_midnight=True` (mutating it to False reddens
`test_midnight_deadline_today_is_treated_as_end_of_day`). **Live DB today (read-only):**
`eu_portal` deadlines at `00:00` UTC = **670 rows**, at `17:00` = 1, at `22:00` = 1; 11 EU
calls have a deadline within ±1 day right now. Vinnova rows sit at 12:00/13:00 as expected.
The deadline day is exactly when a researcher acts, and the call is gone from the feed from
02:00 that morning.

Fix: in `_deadlines`, bump an exactly-midnight instant to 17:00 Europe/Brussels before
comparing and storing; a one-off UPDATE for the 670 stored rows; pin with a test on the real
fixture. Related, same adapter (M2): expiry is filtered on the adapter, so a stored call whose
deadline is brought forward or that is withdrawn keeps its old deadline and text and stays
open.

### Medium

**M1 — Ranking [READ, measured today as not biting]. Sonnet 5.5 thinks inside the ranker's
4096-token cap with no `effort` set.** `ranking/anthropic_client.py` `AnthropicClient` sends
`max_tokens=4096` and no `output_config.effort`/`thinking`; a `max_tokens` stop becomes
`UnusableReplyError` → `parse_error` → three billed attempts → `escalation_state='gave_up'`,
which no API field or UI surface shows. **Coordinator measurement on the live ledger:** today's
44 `claude-sonnet-5-5` attempts are 44 `success`, 0 `parse_error`; `usage.output_tokens`
(which includes thinking) p50 **879**, max **1077** of 4096. The old `claude-sonnet-5` had 7
`parse_error` in 50 attempts over 10-01..10-03. So the risk is real in shape and not
materialising; treat as a watch item: set `effort` explicitly (the API reference recommends
`low` for extraction), record `error_class`/`usage` on the ledger row, and expose
`escalation_state` on `CallDetailRankingOut`.

**M2 — Sources [RAN]. Expiry is filtered on the adapter, so a stored call that moves into the
past, or is withdrawn, stays open with stale text.** `eu_portal.py` `_normalize` (`skip_expired`),
`vinnova.py` `_normalize` (`skip_closed`). Run 1 stores a +30 d deadline; run 2's feed carries
the same id with a past deadline and new text → the adapter yields nothing → the row keeps the
old deadline, title and body, counted open everywhere until the stale deadline passes. Probe:
`stored row kept the OLD deadline 2026-10-08 17:00:00+00:00 after the portal moved it to the
past`. Fix: yield expired rows and close them in the orchestrator, or a reaper for rows absent
from a full-set feed.

**M3 — Sources [RAN]. Every nightly ingest re-embeds every call it sees, changed or not.**
`ingestion/orchestrator.py` `run_ingestion` step 2 calls `refresh_call_embedding` for every
upserted id; `EUPortalAdapter.fetch_new_calls` yields the full open set (~623). Probe: three
identical runs → `embed.call_count: 3`; `ranking_jobs rows: 1` (ranking is fingerprint-gated,
so this is GPU time on the 2080 Ti Gemma shares, not Sonnet spend). The pinning test
`test_orchestrator_re_embeds_touched_calls_even_when_already_present` says "until we have a
content-hash mechanism" — `calls.embedding_fingerprint` (0080) is that mechanism. Fix: skip
when the vector is present, the model matches and the fingerprint matches; flip the test.

**M4 — Sources [RAN]. Nothing user-visible surfaces a failing source.** The streak is
persisted (`Source.consecutive_failures`), but its only reader is `ingestion/alerts.py`, gated
on `ADMIN_ALERT_EMAIL`, which defaults to None and appears in no README or deploy doc;
`SourceOut` exposes `name, slug, last_fetched_at, call_count` only; `/healthz` checks the DB
only. This already bit once (14 silent EU 404s). Fix: expose the streak on `SourceOut`, badge
the admin row, document the alert email.

**M5 — Spend [RAN]. `_is_cited` fails OPEN on a broken branch ref — its docstring promises the
opposite.** `references/deletion.py` `_branches_cite` catches `(GitCommitError, OSError)` from
`branch_tips` and returns False ("not a git repository"), but `gitio.branch_tips` deliberately
raises `GitCommitError("broken ref …")` on `for-each-ref`'s "ignoring broken ref". Probe on a
real repo: intact → `True`; garbage ref file → `False` → hard delete while `feature` still
cites the id; an unreadable refs dir → `False` too. Fix: return False only when the directory
is not a repository; any other error → True.

**M6 — Spend [RAN]. Two fence gates are blind to an honest bypass.** (a) The one-writer ledger
guard (`tests/fencing/spend_guard.py` `llm_spend_writers`) resolves bindings only to
`grant_lab.db.models[.spend].LlmSpend`; `proposal_assist/spend.py` re-binds `LlmSpend`, so a
module importing it from there is a working second writer and the gate stays green (a relative
import from `db.models` IS caught). (b) `tests/fencing/import_gate.py` allowlists
`proposal_assist.ports.*` and checks only `tools/`; nothing pins what `ports/` may import, so
`ports/x.py: from sqlalchemy import select` + `tools/y.py: from ...ports.x import select`
passes all 60 import-gate tests. Fix: resolve import chains transitively (what commit
`45c27d01`'s title promises), and one test running `check_source` over every `ports/` module.

**M7 — Spend [READ]. The citation client keeps SDK `max_retries=2`, and a timeout is a
zero-token row the citation caps never see.** `citations/llm.py` `build_citation_llm`:
`claude-sonnet-5-5`, `max_tokens=1024`, SDK retries 2; a read timeout → up to three billed
requests → `service.py` records `("timeout", 0, 0)`; `usage.check_citation_caps` sums tokens
only. Compaction pinned its client to `max_retries=0` for exactly this reason. Fix: the same,
plus count `status='timeout'` rows toward the cap.

**M8 — Frontend [RAN, known-open, worse than recorded]. The corridor wall renders no titles at
1366x768 and cuts every title mid-line at 1600x1000.** `BoardScreen.module.css` `.cardTitle`
(`flex: 1 1 auto; min-height: 0; overflow: hidden`) next to a fixed 170 px `.cardQr` while the
rest is vh-sized. Playwright against the real match bundle with the e2e `LONG_BOARD` fixture:
titles at h=0 — 0/8 at 1920x1080, **8/8 at 1366x768**, 0/8 but 8/8 clipped at 3.5 lines at
1600x1000. The memory note from 2026-09-23 said "3 titles at h=0"; it is all of them now. This
is also why `match-e2e` is red on master and on every Dependabot PR. Fix: a px floor on the
title band derived from line-height, let the QR band shrink below 1080p, add a third viewport
to the corridor gate.

**M9 — Frontend [RAN]. Re-opening a call within 20 s of deciding it shows the pre-decision
state and offers Save/Hide again.** `api/calls.ts` `useCallDetail` keys the page
`["call", id, profile]`; every decision hook (`api/decisions.ts` `invalidateDecisions`,
`useSetDecisionNote`) invalidates `["calls"]`, which does not prefix-match `"call"`
(coordinator confirmed with `graft grep`). `CallDetail.tsx` papers over it with local
`override` state that dies on navigation. Probe test: stale state shown, Save button enabled,
no refetch; invalidating `["call"]` is the passing control. Fix: invalidate `["call"]` in
`invalidateDecisions` and the note hook.

**M10 — Boards [RAN]. The board title is unbounded and reaches the unauthenticated route, every
org reader's list and the alert subject.** `web/api/boards.py` `BoardCreateIn.title` /
`BoardSettingsIn.title` are `min_length=1` with no `max_length`; `Board.title` is `Text`; the
only body bound is the 64 MiB request cap. Probe: owner PUTs a 900 KB title on a published
board → anonymous `GET /api/public/boards/{slug}` serves 900,145 bytes per poll at 60/15 min
per IP; `GET /api/boards` carries it to every reader. The 10 MiB payload gate varies only URL
bytes, so it cannot see this. Fix: `max_length=200` on both fields, a CHECK beside
`ck_boards_slug_length`, and let the payload gate build its titles at that bound.

**M11 — Profiles [RAN]. New profiles are org-readable at creation; "private by default" is not
what the code does.** `profiles/create.py` `create_profile_with_bootstrap` never sets
`visibility`; `ProfileCreateIn` has no field; the column's server default is `'org'`
(0070, chosen so *existing* rows would not change on migration day). Probe: `POST
/api/profiles` → `visibility: org`, stranger GET 200, listed to strangers. The 2026-09-04 scope
note, the 2026-09-06 spec and the recorded human decision ("all profiles should be personal")
say private. **Live DB: 1 profile (June), 5 active users** — no victim today, but the
decision has drifted. Fix: `visibility="private"` in `create_profile_with_bootstrap` plus a
real-endpoint test; or record that `org` at creation is the product choice.

**M12 — Ops [RAN]. `/healthz` still answers 200 after the scheduler lock is lost; a test pins
the 200.** `web/routes/health.py` `healthz` returns `{"status":"ok","scheduler_lock":false}`
with 200, and `tests/test_web_health.py::test_healthz_reports_a_missing_lock_without_failing`
asserts it. Since `93a5ea26` the watchdog stops the scheduler on loss (mutation-proved), so the
process serves HTTP with zero scheduled work while any status-code monitor stays green. Carried
from the 2026-09-05 runtime-ownership review. Fix: 503 unless `SCHEDULER_STANDBY_OK`, or a
`/readyz`.

**M13 — Ops [READ]. README quickstart `DATABASE_URL` names a role and database that compose
never creates.** README §3 says `grant_lab:grant_lab@…/grant_lab`; `docker compose config`
renders `POSTGRES_USER/DB: call_manager`; CLAUDE.md says the DB name stays `call_manager`
deliberately. Step 4 (`alembic upgrade head`) fails as written. Fix: one line.

**M14 — Architecture [RAN]. Three new package-level cycles since 09-05 (7 vs 4), and the only
cycle gate is module-level.** `boards/queue.py` imports the private
`web.dashboard._decision_predicate` (deliberately, to avoid a copy — its docstring says so, and
the coordinator confirmed the call sites); `profiles/fingerprint.py` ↔ `embeddings/refresh.py`;
`profiles/{bootstrap,post_edit}` ↔ `ranking/{freshness,worker}`. `test_no_import_cycles.py` is
a module-level Tarjan gate, correctly green. The 09-05 review's three "cheap moves"
(`profile_access` out of `web/`, `LLMError` out of `ranking/`, `workspaces.py` into
`proposal_assist/`) have zero delta. Fix: move `_decision_predicate` + `profile_access` into a
domain module, make `fingerprint` the shared leaf, and add a package-edge allowlist pinned at
today's 7 that may only shrink.

### Low (one line each; evidence in the appendices)

- **Ranking:** the ledger's Stockholm day boundary has no gate (UTC-midnight mutant survives 53 tests; a ±1 s probe catches it) · an `EMBEDDING_MODEL` swap bypasses the worker's vector deferral so each pair re-ranks twice, bounded by the shared daily cap and documented in spec D9 · a local Gemma `finish_reason=length` is classed `service` and retried forever (the remote twin has the dual-parent exception) · `gave_up`/`pending` reach no API or UI (spec says out of scope).
- **Sources:** `POST /admin/sources/{slug}/fetch` returns `triggered: true` while a run is in progress but APScheduler skips it (`max_instances=1`) · a non-dict element in SEDIA `results` aborts the run with an uncaught `AttributeError`.
- **Boards:** `last_reconcile_error` (may contain a DB host/role from a psycopg error) is served to every org reader via `BoardOut`, not "an admin screen" as the model comment says · the threshold sweep offset-pages a live ordering without dedup; a boundary repeat is a PK violation → 500.
- **Profiles:** a revoked viewer's hides keep suppressing their no-profile archive with no UI path to them · the three viewer-row writers disagree about non-active users (transfer inserts, add/replace refuse, replace silently drops).
- **Spend:** the `path_exists_at_commit` fail-closed branch in `_branches_cite` is untested (`continue` mutant: 17/17 green) · `citation_llm_usage.status` cannot tell a `max_tokens` truncation from a parse failure and failed rows lose `model`.
- **Ops:** an oversized chunked/multipart body without Content-Length → 500 + traceback, not 413 (memory stays bounded) · `index.html` has no `Cache-Control` · dotfiles under `/assets` are served immutable · `uvicorn --workers 2` crash-loops the second worker every ~5 s forever while worker 1 looks healthy (README should say `--workers 1`).
- **Frontend:** `TriageToolbar.change()` builds the next URL from render-time params, so Order-after-search can drop `q` (e2e flaky 1/31; ×6 isolated passes) · `TriageCard.DecisionNote`'s `keepCaret`/`onCommit` machinery is inert since `7d0f237c` and four caret tests pass vacuously · "a flip clears the old reason" (`CallDecisionActions.decide`) and "the queue says when it is truncated" (`BoardQueue`) have no pinning test (31/31 and 12/12 green under mutation) · `nanoid` reaches the match bundle via `ics` · the wall mixes Swedish and English strings · `?sort=foo` / `?page=abc` dead-end the index · the Saved view in Shortlist layout has no mouse control to un-save.
- **Architecture:** `scripts/gen_env_example.py --check` ignores the flag and overwrites · live `data/golden_set_history.jsonl` is 4 rows (was 1,090 on 09-05; the suite is no longer the writer — confirm the reset was deliberate) · migrations 0071 and 0078 have documented lossy downgrades (none is `pass`).
- **Hygiene:** untracked `.ignore` changes what ripgrep (and the Grep tool) searches in this checkout — commit it or move it to `.git/info/exclude`.

## 4. Checked and sound — do not respend this effort

All RAN; each line is a claim, the mutation or probe, the result. Full lists in the appendices.

- **Ranking/freshness (14 of 15 mutants killed):** `enqueue_stale_pairs` enqueues only on a changed call/profile/scorer fingerprint or an owed second reading (dropping the call-fingerprint conjunct → 4 red; `IS DISTINCT FROM` → `<>` → 2 red); the vector deferral is committed (commit removed → red from a fresh session); the 300 cap boundary (`>=` → `>` → red); refusal raises only on a schema request; unusable → `parse_error`; a reaped job keeps its kind; `gave_up` is retried when inputs change; `_instant` naive branch and `embedding_model` in `rank_fingerprint` both pinned; the call fingerprint is written with the vector (count-based tests); both backfill cursors carried; logprobs opt-in (forced key → red); 0075–0081 round-trip 0081→0074→head with seeded `gave_up`/`crash`/timeline rows.
- **Sources/ingestion:** all 13 `NormalizedCall` fields are in both the INSERT and the `ON CONFLICT … SET` (dropping `published_at` → red); a changed call re-embeds once, an embedder outage leaves the old vector and the tick repairs it; `_pick_deadline` earliest-open-round pinned; page-2 404 → 0 rows persisted, streak recorded; digest Jinja `autoescape=True` (forcing False → red); the calls API never exposes `full_text`; no `dangerouslySetInnerHTML` anywhere.
- **Boards (15 mutations, 17 red tests):** `MAX_BOARD_ITEMS` up → payload gate red at 11.4 MB, down → in-bound gate red; the `{board_id}` single-source permission gate (both halves); admin-only publish/unpublish; unpublished = the same 404 as nonexistent; 61st request → 429; queue hides keyed on the profile OWNER (both sides); a takedown outlives the sweep; pass-2 decides under the row lock (real `FOR UPDATE NOWAIT` probe); URL-moved re-encode; D9 dry-run, HTML escape, bounce count; 0078 downgrade restores NOT NULL; hand-back of an over-capacity auto board refused; six two-step capacity sequences all refused.
- **Profiles/access (13 mutations):** 409 scoped to the caller and the index owner-scoped (0076 drops the global one); the load-bearing-predicate gate reddens when the `.where` is deleted but the call left; the transfer race test uses real threads and a barrier and goes `[200, 200]` without `with_for_update`; the 404-before-403 ladder; self-transfer and viewer-row deletion; note scoping on write and on read; flip clears note; digest, subscription and board-queue filters; 33 stranger-facing surfaces + the digest probed with a marker — `LEAKS: {}`.
- **Spend/fence:** failed compaction records billed usage; only read timeouts are "possibly billed"; timeouts count toward `COMPACTION_MAX_BILLED_ATTEMPTS`; the attempt window restarts at the last summary; the citation failed row carries usage; every `record_spend` writer has a pre-check except H2; global cap checked before per-workspace; Stockholm day in every sum; primitives A/B/C each reddened their test; cross-branch cited-evidence protection (working-tree-only mutant → 2 red; the 3-branch orphan fixture exists).
- **Auth/ops:** logout epoch comparison (mutant → exactly one red); every cookie consumer checks the epoch (dependency, ws, SSE mid-stream, OAuth link); OIDC `alg=none`/HS256-confusion/`nbf`/`iat`/unknown-kid (1 refetch)/`aud`-without-`azp` all rejected, nonce and PKCE bound via the `oauth_txn` cookie; register squat fixed; watchdog wiring (lifespan call removed → exactly the wiring test red); second instance refuses to boot; `Origin: null` → 403, referrer-policy `same-origin`, ws `check_origin` before auth, `httponly`/`secure`/`lax`, max-age server-side; SPA traversal (`..`, `%2e%2e`, `..%2f`, `..\`, `//etc/passwd`, symlinks) → 404, `/api/*` never gets the shell, prod refuses to boot without a bundle; `X-Forwarded-For` honoured only with `TRUSTED_PROXY`, hourly limiter sweep covers the board limiter; compose binds Postgres to 127.0.0.1.
- **Frontend (12 of 13 claims):** note strip adopts only when clean (both directions red); editor keyed on `call.id`; click-open takes the caret; failed save shows an alert; decisions survive reload; corridor poll ordering, payload guard and abort-spans-body-read all red under mutation; boards preview answers for the form; `check:bundle` exit 1 and `router.simple.test` red on a planted cockpit import; `schema.gen.ts` byte-identical to a fresh regeneration; `unwrap` 401/429/empty-body paths; no redirect loop; React blocks `javascript:` hrefs.
- **Architecture:** process-cut gate 7 passed, module-level `import grant_lab.proposal_assist.fencing` in `main.py` → `assert 8 == 0`; match boot loads 0 proposal-assist modules, no `claude_agent_sdk`/`pycrdt`/`prosemirror` (full boot: 132 / true / true / true); golden-set isolation autouse fixture (removing the `_HISTORY_PATH` patch → red); `alembic check` → no new operations; 49 tables up → 1 down → 49 up on a throwaway container; all 28 `add_job` calls `max_instances=1, coalesce=True`; allowlist sizes equal the ARCHITECTURE.md table.

## 5. Disposition of the prior reviews' open findings

94 items re-verified from the 2026-09-02 thorough review (§2.7, §3, §6, appendices), the
2026-09-05 architecture review §5 watch list, the 2026-09-05 runtime-ownership review, and the
user's three recorded bridge data-loss reports. **57 FIXED, 7 PARTIAL, 26 STILL OPEN, 4 N/A.**
The full 95-row table with evidence is in Appendix A9. Headlines:

**Fixed, with a probe or test run today:** the collab corruption trio (stale Y.Doc after a
branch switch — `doc_epoch` built, 4409 before STEP1; mark mis-nesting; empty blockquote),
the fence-line-in-code-block and hard-break cases, `published_at`, all four reference
exhaustions (chunk cap 500, 4 KiB error snippet, deadline inside the body read, `RLIMIT_AS`),
second-process refusal, README migration step, `.env.example` coverage (114/117 aliases),
`LOG_LEVEL`, CJK `Content-Disposition`, delete-vs-finalize lock order, Phase-0 G+2 writes,
schema drift gate, nightly corpus re-enqueue, the forgeable ranking fence, blank-description
backfill, `list_turns` paging, SSE revocation mid-stream, logout epoch, the API error envelope,
six React findings, register squat + admin release, audit tools on the loop, the frame cap and
the four 2xx-only tests, silent lock loss, `trigger_job_now` on a replica, frontend lint gate,
e2e wiring, tracked `.orig` files, golden-set pollution, the match edition process cut, and
**the "present vs current" embedding repair** (compiled SQL: both halves now select
`embedding_fingerprint` and decide in Python). The three bridge data-loss reports (`<TP53>`,
`sample_id`, citation marks) round-trip byte-identical through both paths and are pinned.

**Still open at Medium, with RAN proof:** `NaN`/`Infinity` rubric weights accepted
(`_validate_criteria` → `ACCEPTED weight=nan`); `test_approve_rejects_non_pending` is hollow
(guard deleted → 30/30 green); `_is_private_ip("100.64.0.1") = False` while the docstring
promises CGNAT refused. **Still open, READ:** `create_workspace_branch` founds on the last
checkpoint; `reference_id` FKs unindexed; digest week-claim only flushed; near-miss Gemma reply
retried 5×; fetch failures rewritten to "internal server error"; SSRF refusal echoes the
resolved IP; `add_member` registration oracle and case-sensitive email; `get_spec` serves the
locker's email; unbounded `claim_hash`, `profile_ids`, `page`; activity feed discloses emails;
login limiter consumes a slot on success; `str(exc)` leaks in three places; reprocess re-keys
and de-approves a reference; deep citation check pins a connection across LLM calls; `text_error`
leaks the Ollama URL; declared-MIME-equals-sniffed has no pin; `base_branch` 422 has no test;
post-save optimistic write reverts canonicalization; Problems panel hides failed signals;
compaction input unbounded; `init_spec_via_kernel` untested; `WORKSPACES_DIR` unvalidated;
secret-scrub denylist incomplete; `_current_head` without timeout; WS TEXT frame → `KeyError`;
`seed_dev_data.py` unguarded; access log records the OAuth code; `extra="ignore"` config.
**Partial:** shutdown ordering; `_insert_citation_locked` pin (now in `live_mutex_gate`);
sync Sessions on the loop (29 coroutines, down from 31; both measured stall sites fixed);
boot-time revision check absent; a11y lows.

## 6. The numbers, re-measured (same methods as 2026-09-05)

| metric | 2026-09-05 | today | delta |
|---|---|---|---|
| `src/grant_lab` LOC / files | 60,063 / 287 | 66,964 / 309 | +6,901 / +22 |
| `proposal_assist/` | 35,551 (59%) | 35,727 (53%) | +176 |
| `web/` | 9,027 | 11,597 | +2,570 |
| Call Match half (7 pkgs) + `boards/` | 8,419 (14%) | 9,965 + 1,426 = 11,391 (17%) | +2,972 |
| `tests/` LOC / files | 123,916 / 585 | 144,930 / 643 | +21,014 / +58 |
| `frontend/src` excl. `schema.gen.ts` | 31,976 | 41,550 | +9,574 |
| mapped tables / migrations / settings / `add_job` | 45 / 70 / 115 / 27 | 48 / 81 / 117 / 28 | +3 / +11 / +2 / +1 |
| package-level 2-cycles | 4 | **7** | +3 |
| `web → proposal_assist` statements | 98 | 100 | +2 |
| Call Match half → `proposal_assist` | none | none | 0 |
| PA sub-package 2-cycles / `citations` in | 14 / 8 | 14 / 8 | 0 |
| pass runners / evict ceremonies / `ToolContext` fields | 4 / 4 / 29 | 4 (+ ranking worker 558 LOC) / 4 / 29 | 0 |
| match boot: PA modules / `claude_agent_sdk` / `pycrdt` | 142 / True / True | **0 / False / False** | fixed |
| `git remote` | 0 | 1 | fixed |
| backend tests | 5029 + 1 | 5795 + 1 | +766 |

## 7. Recommended order

1. **Today, ten minutes:** fix the OSV action path, push `master`, watch the `test` job run for
   the first time; it will go red on pip-audit/pnpm-audit — bump `anyio`, `urllib3`, and the
   dev `httpx2`, and decide `dependency-review.yml` (H1).
2. **This week, product-facing:** EU deadline bump + the 670-row backfill (H3); invalidate
   `["call"]` on decisions (M9); corridor title floor + third viewport, which also turns
   `match-e2e` green (M8); board title `max_length` (M10).
3. **This week, money:** Phase 0 cap check + ledger kind + `max_retries=0` (H2); citation
   client `max_retries=0` + timeouts toward the cap (M7); set Sonnet 5.5 `effort` and record
   `error_class` on the ledger (M1).
4. **Next:** adapter-side expiry → orchestrator-side (M2) and the dead-source badge (M4),
   ideally with the change-notification spec that was already going to touch ingestion;
   fingerprint-gated re-embed in ingest (M3); `_is_cited` fail-closed (M5); the two blind fence
   gates (M6); `/healthz` 503 on lock loss (M12); README `DATABASE_URL` (M13); decide
   `visibility` at creation and pin it (M11).
5. **Structural, when a slice is open anyway:** the package-edge allowlist and the three
   cheap moves (M14); the 26 still-open Mediums/Lows from §5, starting with the three that have
   RAN proof.

## 8. What this review did not do

No live Ollama or Anthropic calls; no live writes of any kind (three read-only SELECTs against
the live database, listed in §3); no Playwright on the full edition; no re-derivation of the
2026-09-01 SSRF/fencing items that have not changed since. Each appendix carries its own "Not
checked" list.

---

## Appendix A1 — ranking + freshness (reviewer report, verbatim)

## ranking + freshness — verdict in two sentences

The freshness machinery is sound: 14 of 15 mutants across the enqueue rule, the committed deferral, the cap boundary, the refusal/truncation classification, the reaper's kind rule, the fingerprint canonical forms, the call-side fingerprint stamp, both cursor rules and the logprobs opt-in were each killed by their gate for the stated reason, and migrations 0075–0081 round-trip 0081→0074→head with seeded data. The live risk is on the Sonnet 5.5 side, which went live today: the ranker sends no `effort`/`thinking` setting into a 4096-token cap that Sonnet 5.5's default adaptive thinking shares with the answer, and the code turns the resulting `max_tokens` stop into three billed attempts and a silent `gave_up` that no UI surface shows.

### Findings, most severe first

**[MED] [READ] Sonnet 5.5 thinks inside the ranker's 4096-token cap; a truncation is billed three times, then silently gives up**
`src/grant_lab/ranking/anthropic_client.py` `AnthropicClient.__init__`/`chat`; `src/grant_lab/scheduler.py` `build_scheduler` (`AnthropicClient(api_key=..., model=remote_rank_model)` — default `max_tokens=4096`, no `output_config.effort`, no `thinking`); `rank_pair` except-branch; `next_escalation_state`.
Scenario: per the claude-api skill, `claude-sonnet-5-5` with `thinking` omitted "runs **adaptive**" at default effort `high`, and "`max_tokens` is a hard cap on thinking *plus* response text … you may see a response that is almost entirely thinking followed by a truncated answer and `stop_reason: "max_tokens"`". The ranking reply (3 rationales + quotes + `summary` + `applicant_notes`) and the thinking share 4096. On `max_tokens` the client raises `UnusableReplyError` (both `LLMError` and `ParseError`); `rank_pair` maps it to `parse_error`; `next_escalation_state` counts it toward `ESCALATION_MAX_PARSE_FAILURES=3`; each of the three attempts is a billed Sonnet call (thinking output at $10/MTok) recorded only as `outcome='parse_error'`. The pair then ships Gemma-only with `escalation_state='gave_up'`, which nothing displays (next finding). The ledger cannot tell this from bad JSON: `ck_ranking_remote_attempts_outcome` allows only `success|llm_error|parse_error`, and the `usage` the client attaches via `_billed()` is dropped in `rank_pair` (only `str(e)` is logged). Spec §9 says adaptive thinking/effort are "a separate decision" — that decision is now live.
Evidence (READ): `anthropic_client.py` — `max_tokens: int = 4096` … `self._client.messages.create(model=…, max_tokens=self._max_tokens, system=…, messages=…, **({"output_config": output_config} if output_config else {}))` where `_output_config` returns only `{"format": …}`; `grep -n -i "effort|thinking|temperature" anthropic_client.py` → no setting. Not measurable here (no network, no live DB).
Fix: send `output_config={"format": …, "effort": "low"|"medium"}` (or `thinking={"type":"between_tools"}`) and raise ranker `max_tokens` to ≥16000; record `error_class`/`usage` on the ledger row. Measure first: `SELECT outcome, count(*) FROM ranking_remote_attempts WHERE created_at >= <today> GROUP BY 1` and grep the app log for `truncated at max_tokens`.

*Coordinator note (live ledger, read-only, 2026-10-06): claude-sonnet-5-5 today = 44 success / 0 parse_error; `usage.output_tokens` p50 879, max 1077 of 4096. Not biting today; downgraded to a watch item (M1).*

**[LOW] [RAN] A Gemma-only score is never marked for the user (`pending`/`gave_up` reach no API or UI)**
`src/grant_lab/web/api/calls.py` `CallDetailRankingOut`; `frontend/src/routes/CallDetailBody.tsx`, `TriageCard.tsx`.
Scenario: a pair denied by the cap (33 pairs ≥45 on 2026-09-09) or `gave_up` above shows the same card as a pair that never qualified.
Evidence: `grep -rn "gave_up|escalation_state|escalation_failures" src/grant_lab/web/ frontend/src/` → 0 hits for all three; the only escalation fields exposed are `escalated`/`escalated_from_score`, rendered only when `ranking.escalated` is true. Spec 2026-09-24 §9 lists the marker as out of scope, so this is known; reported because the question was asked.
Fix: expose `escalation_state` on `CallDetailRankingOut` and render "second reading pending/abandoned".

**[LOW] [RAN] The ledger's Europe/Stockholm day boundary is not pinned by any gate**
`src/grant_lab/ranking/remote_attempts.py` `count_today`.
Scenario: a refactor that inlines UTC midnight in `count_today` passes every test; attempts made 00:00–02:00 Stockholm then drop out of the day's count after 02:00 (the `timeutil` docstring's "up to 2x the configured hosted spend").
Evidence: mutant `day_start = datetime.now(UTC).replace(hour=0, …)` →
```
tests/test_ranking_remote_attempts.py tests/test_ranking_worker.py tests/test_ranking_escalation_state.py tests/test_stockholm_day_boundary.py
53 passed in 12.97s
```
(`test_count_today_ignores_yesterday` backdates by 2 days; `test_stockholm_day_boundary.py` tests the helper, not its use). My probe (rows at `stockholm_day_start_utc() ± 1 s`, assert `count_today == 1`) failed under the mutant at 13:19Z: `assert 0 == 1  where 0 = count_today(...)` and passes on HEAD.
Fix: add that probe to `tests/test_ranking_remote_attempts.py`.

**[LOW] [RAN] An `EMBEDDING_MODEL` swap bypasses the worker's vector deferral, so every pair re-ranks twice (Sonnet included)**
`src/grant_lab/ranking/worker.py` `drain_queue` deferral block (compares `profile.embedding_fingerprint != embedding_fingerprint(profile)` — text hashes, identical across a model swap).
Scenario: after a swap the 03:00/03:15 ingests re-embed calls, the worker ranks every touched pair with the cosine gate off, the 03:30 tick re-embeds profiles, `rank_fingerprint` (which hashes `embedding_model`) changes, and the 04:00 reconcile re-ranks the whole open feed again — both rounds escalating to Sonnet. Bounded: both rounds fall in one Stockholm day, so the 300 cap is shared, not doubled. Spec D9 (2026-10-05 amendment) documents it with a manual runbook.
Evidence: probe — profile `embedding_model="old-embedding-model"`, fingerprint current, call under `bge-m3`, `drain_queue(...)` → `CallRanking` written, job `DONE`, `embed_similarity is None`; then `rank_fingerprint` changes when `embedding_model` becomes `bge-m3` → PASSED.
Fix: also defer when `profile.embedding_model != embed_client.model` (hand `drain_queue` the configured model), under the same 24 h bound.

**[LOW] [READ] Every nightly ingest re-embeds every fetched call, changed or not**
`src/grant_lab/ingestion/orchestrator.py` `run_ingestion` step 2: `for cid in touched_call_ids: refresh_call_embedding(embed_session, embed_client, cid)` with no fingerprint/model check; pinned by `tests/test_ingestion_orchestrator_embeds.py::test_orchestrator_re_embeds_touched_calls_even_when_already_present` (`assert embed_client.embed.call_count == 2`).
Scenario: ~680 bge-m3 calls per night (623 EU + Vinnova) on the GPU Gemma shares. It does **not** reinstate the nightly re-rank: the fingerprint hashes text, so `call_rank_fingerprint` is unchanged — I confirmed the content fields are per-fetch stable (`published_at` from `startDate`/`Oppningsdatum`, EU `keywords` = `sorted(out)`, body scraped only when `needs_body`).
Fix: skip when the vector is present and `embedding_model == embed_client.model and embedding_fingerprint == call_embedding_fingerprint(call)`; update that test's expectation.

**[LOW] [READ] A deterministic local truncation is classed `service` and retried forever; the identical remote case is `parse`**
`src/grant_lab/ranking/ollama_client.py` `chat` (`finish_reason == "length"` → plain `LLMError`, whose own message says "retrying cannot help"); `drain_queue` → `failure_kind="service"` → never suppresses. Spec D7 amendment records this as a known limitation needing "a distinct exception type; not done" — the remote side has since gained exactly that type (`UnusableReplyError`). A pair that always hits `num_predict=2048` is re-enqueued by every ingest/reconcile and burns `MAX_ATTEMPTS` × up to 120 s of Gemma per night until the call closes.
Fix: raise an `LLMError`+`ParseError` dual-parent from `OllamaClient` on `length`, as `anthropic_client.py` does.

### Checked and sound (all RAN)
- (a) call fingerprint in the currency test: dropped `AND cr.call_fingerprint = wc.fingerprint` → 4 failed (`test_a_changed_summary_is_enqueued`, `test_a_trl_change_is_enqueued`, `test_null_stamps_are_stale[call_fingerprint]`, `test_a_changed_call_vector_is_stale`: `assert 0 == 1`), 24 passed.
- (a) the NULL trap: `IS DISTINCT FROM 'pending'` → `<> 'pending'` → `test_a_fresh_pair_is_not_enqueued_even_with_a_remote_configured` and `[None-True-0]` fail `assert 1 == 0`.
- (a) deferral is committed: removed `session.commit()` after `revert_to_pending` → gate 15 fails `assert <running> == <pending>` from a fresh session.
- (b) cap boundary: `>=` → `>` → `test_worker_cap_disables_remote_when_exhausted` fails (`_Remote ... is None`); below-cap sibling still passes. Only `drain_queue` calls `rank_pair` in `src/` (graft callers); `AnthropicClient` is built only by the scheduler ×3 and `build_citation_llm`; `validate_ev.py`/`eval.py`/`golden_set.py` never pass a remote client.
- (c) Unusable → `parse_error`: made the dual-parent map to `llm_error` → `test_an_unusable_remote_reply_counts_as_a_parse_failure` fails `'llm_error' == 'parse_error'`.
- (c) refusal only on a schema request: dropped `output_config and` → `test_a_refusal_without_a_schema_still_returns_its_text` fails with `UnusableReplyError: anthropic refused`. Note `max_tokens` raises for **all** callers (by design per the test docstring); citations' `StructuredLLM.complete_json` lets it propagate.
- (c) reaped job keeps its kind: `coalesce(failure_kind,'crash')` → `'crash'` → `test_a_reaped_job_that_failed_on_the_service_keeps_its_kind` fails `'crash' != 'service'`.
- (c) `gave_up` retried on input change: probe — stamped `gave_up`/3 → `enqueue == 0`; `call.summary` changed → `== 1`. G20 covers the worker side (count resets to 1).
- (d) `_instant` naive branch removed → `test_the_same_instant_in_another_zone…` and `test_a_naive_datetime_hashes_as_the_same_utc_instant[deadline]` fail (hash mismatch); `embedding_model` dropped from `rank_fingerprint` → `test_a_model_swap_changes_the_rank_fingerprint` fails (equal hashes). Scorer settings live in `ScoringConfig.fingerprint` (separate stamp), pinned by `test_ranking_scoring_config.py`; the scheduler builds `FreshnessContext` from the same locals it hands `drain_queue`.
- (f) call fingerprint written with the vector: removed the stamp on success → `test_refresh_call_embedding_writes_fingerprint`, `test_an_unchanged_call_does_not_re_embed` ("re-embedded 1 time(s)"), `test_a_changed_call_re_embeds_on_the_next_tick` fail — call-count based. Every `.embedding =` write site in `src/` is in `refresh.py` (graft find_all; the third hit is proposal-assist references).
- (f) CLI carries both cursors in non-force mode: `after_profile_id = ... if args.force else 0` → `test_cli_backfill_reaches_stale_calls_beyond_one_ticks_cap` fails ("did not terminate: exceeded 20 tick() calls"), 8 passed.
- (f) one capped half does not freeze the other's cursor: profile cursor keyed on joint `scan_exhausted` → gate 9c and its sequence sibling fail (`after_profile_id ... [0, 2, 2, 2, 2, 2]`).
- (g) logprobs opt-in: forced `{"logprobs": True, "top_logprobs": 10}` → `test_logprobs_are_not_requested_by_default` fails (`'logprobs' in payload`); `OllamaClient.chat` adds the key only when `logprobs=True`; the Settings→scheduler→drain→rank_pair hops are each pinned in `test_scheduler_ranking_logprobs.py`.
- (h) `max_tokens=4096` default confirmed; `UnusableReplyError` carries `.usage`; `build_citation_llm` falls back `settings.citation_llm_model or settings.remote_rank_model` with its own `citation_llm_max_output_tokens` and timeout.
- (i) each of 0075–0081 has a symmetric `downgrade()` (0076 deliberately can fail on duplicate names; 0078 deletes NULL-approver items); existing `test_round_trip_upgrade_downgrade_upgrade` (head→0005→head) PASSED; my seeded probe (0081→0074→head with `gave_up`/3, `crash` job, timeline criterion, both fingerprints) PASSED: columns gone at 0074, rows survive, re-upgrade gives `("legacy", None, None, None, 0)`, `failure_kind` NULL, timeline row deleted, and the NULL-kind job suppresses pair-wide for 7 days (arm c) until removed.
- (e) bounded by the cap: both re-rank rounds happen in one Stockholm day under one `count_today`; every attempt is a ledger row (M4 above).

### Not checked
Live ledger/`escalation_state` counts and Sonnet 5.5's actual truncation rate (no live DB, no network — the coordinator measured both, see M1). Stability of EU `full_text` across fetches (SEDIA body scrape needs network). Board tables were not seeded in the migration probe (DDL only for 0077–0079). Admin re-rank estimate read only (`active_profiles × open_calls`, matches D5). Compaction/citations handling of a `max_tokens` `UnusableReplyError` (outside scope).

### Numbers you measured
Baseline (14 ranking/embedding/fingerprint files, `-n 4 --dist loadfile`): 242 passed. Group-C mutant run (4 files): 53 passed (the finding). Probe file + existing round trip: first run 4 passed / 1 failed (my assertion, corrected), rerun of the corrected probe: 1 passed. 15 mutants: 14 killed for the claimed reason, 1 (UTC midnight in `count_today`) survived. Final HEAD `c94f2de3`. `git status --short`: empty.

## Appendix A2 — boards (reviewer report, verbatim)

## boards — verdict in two sentences

The Plan C / auto-publish code is tight: all 15 mutations I ran (cap up and down, permission gate, admin-only publish, unpublished-404, rate limit, owner-vs-creator hides, suppressions, row lock, URL re-encode, dry-run, HTML escape, bounce count, downgrade NOT NULL, the two-step manual hand-back) reddened their gate for exactly the reason claimed, and every two-step capacity sequence I tried was refused. The one real hole is a field nobody bounded: the board **title** is free text with no `max_length`, is editor-settable after publication, and lands on the unauthenticated route, every org reader's list, and the alert subject — a 900 KB title went straight through to a 900 KB public response.

### Findings, most severe first

**[MED] [RAN] Board title is unbounded and reaches the unauthenticated route, every reader's board list, and the alert subject**
`src/grant_lab/web/api/boards.py` — `BoardCreateIn.title` and `BoardSettingsIn.title` are `Field(min_length=1)` with no `max_length`; `Board.title` is `Text`; the only body bound is `max_request_body_bytes` (config.py, default **64 MiB**). `update_board` is editor-accessible and publication is not re-checked, so the profile owner of an already-published board (or anyone who pastes a document into the field) controls the size of `PublicBoardOut.title`. The 10 MiB payload gate (`test_the_max_board_items_ceiling_payload_at_the_cost_bound`) varies only URL bytes × item count with a ~40-char title, so it cannot see this.
Scenario: owner PUTs a 900 KB title on a published board → anonymous `GET /api/public/boards/{slug}` serves ~900 KB per poll, 60×/15 min/IP; `BoardScreen.tsx` renders it in the `<h1>` so every corridor screen on that board becomes unusable; `GET /api/boards` carries it to every reader of an org-visibility profile; and `alerts.py` puts `safe_title` into the weekly email `subject` (CR/LF stripped, length not) — a multi-hundred-KB subject will be refused by the transport and the alert logged as `recipient_failed`/transport error and lost every week, the same silent-forever class the newline fix closed (this last sink is inferred, not run).
Evidence (temporary probe, deleted): PUT `{"title": "T"*900_000}` as owner; anonymous GET public; GET `/api/boards` as an unrelated active user:
```
title 200 200 900145 200 900480
1 passed, 6 deselected in 7.28s
```
(PUT status, public status, public bytes, list status, list bytes.) Not recorded in INVESTIGATIONS.md, the reviews dir, or the decisions note.
Fix: `max_length=` on both title fields (200 is generous for a wall header), a `CHECK (char_length(title) <= N)` migration beside `ck_boards_slug_length`, and have the payload-budget gate build its titles at that bound.

**[LOW] [READ] `last_reconcile_error` is served to every reader of an org-visibility profile, not "an admin screen"**
`src/grant_lab/db/models/boards.py` `Board.last_reconcile_error` comment: "this is served on an admin screen". It is not: `_to_out` copies it into `BoardOut`, and `list_boards` returns `BoardOut` for every profile in `readable_profile_ids(user)`, which `profile_access.py`'s docstring says includes every `visibility == 'org'` profile — "the default, so this is most profiles today". `reconcile_auto_boards` stores `failure = str(e)[:500]`; a psycopg `OperationalError` string names the DB host, port and role. Fix: blank the field in `_to_out` unless `user.is_admin`, or move it to the admin dashboard.

**[LOW] [READ] The threshold sweep offset-pages a live ordering without deduplication; a boundary repeat is a PK violation → 500**
`src/grant_lab/web/api/boards.py` `_candidates_at_or_above` pages `board_queue(... limit=page_size, offset=offset)` then `offset += page_size`; in `approve_items` the `min_score` branch does `requested = _candidates_at_or_above(...)` with no `dict.fromkeys` (the `call_ids` branch has one), then `new_ids = [cid for cid in requested if cid not in already_approved_ids]` and one `session.add(BoardItem(...))` per entry. A ranking written between page 1 and page 2 (the drain runs every `RANKING_TICK_SECONDS`=60 s) shifts the `aggregate_score desc, Call.id desc` order by one, the 200th row repeats as the 201st, and a waiting call appears twice in `new_ids` → duplicate `(board_id, call_id)` → `IntegrityError` → 500, whole sweep rolled back. Precondition is ≥ 200 queue rows at/above the floor while `approved + waiting ≤ 192`, i.e. ≥ 8 suppressed rows padding the page — reachable on an auto board over time, not common. Fix: dedupe inside the helper (`list(dict.fromkeys(call_ids))`) on both paths.

### Checked and sound (all RAN)

Every mutation: `cp` backup → Edit → targeted pytest → `cp` restore in the same shell call. Command shape: `cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -q <file> -k <expr>`.

- `MAX_BOARD_ITEMS` 192→256: `test_the_max_board_items_ceiling_payload_at_the_cost_bound` red — `payload grew to 11374897 bytes at the 256-item / 800-byte-URL worst case -- over the re-derived 10 MiB bound`. The gate builds its board FROM the constant (`max_pages=-(-MAX_BOARD_ITEMS // 64)`).
- `MAX_BOARD_ITEMS` 192→100: `test_a_large_board_inside_the_cap_is_still_accepted` red — `a legitimate 144-item board was refused: ... 144 cards; a board holds at most 100 / assert 409 == 200`.
- Permission gate: `get_board_items` swapped to `session.get(Board, board_id)` → `tests/architecture/test_boards_permission_single_source.py` 2 failed / 24 passed — positive half `does not unconditionally call _board_for_edit`, negative half `looks the board up directly at line(s) [905]`.
- Admin-only publish: `if not user.is_admin: raise` removed → `test_an_editor_who_is_not_admin_CANNOT_publish` and `test_unpublishing_is_admin_only_too` red, `assert 200 == 403` (editor published AND unpublished).
- Unpublished = 404: `or not board.is_published` removed → `test_an_unpublished_board_is_404` and `test_a_nonexistent_slug_is_the_SAME_404` red, `assert 200 == 404`.
- Rate limit: `.allow(...) and False` → `test_the_rate_limiter_refuses_the_61st_request_in_a_window` red, `assert 200 == 429`. Key is `client_ip()`: `request.client.host` unless `TRUSTED_PROXY=true`, then the Nth-from-right XFF entry with `TRUSTED_PROXY_HOPS` (400 on too few entries); 60/15 min, own counter, in the hourly sweep list (`main.py:308`).
- Queue hides keyed off `board.created_by` instead of `Profile.created_by` → `test_a_call_the_owner_hid_is_absent_from_the_queue` and `test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue` both red (D2 pinned from both sides).
- `Call.id.notin_(suppressed)` removed from `board_selection` → `test_a_suppressed_call_is_not_republished` red — `the 05:00 job undid a takedown`. (Higher-score re-qualification is the same code path: the exclusion is by id, not score.)
- Pass-2 `with_for_update=False` → `test_pass_2_decides_under_the_board_row_lock` red — `pass 2 decided WITHOUT the board row lock`. That test and `test_the_encode_does_not_run_under_the_board_lock` are real second-connection `FOR UPDATE NOWAIT` probes, not `with TestClient(app)` thread tests.
- URL-moved re-encode disabled (`if False:`) → `test_a_changed_call_url_gets_a_fresh_qr_on_the_next_run` red — `the row still points at the old url`.
- D9 `digest_dry_run` ignored → `test_send_board_alerts_dry_run_sends_nothing` red — `messages_sent=1 ... assert 1 == 0`.
- D9 `_escape_html(title)` dropped → `test_send_board_alerts_escapes_the_title_in_html_not_text` red — `'<a href="https://evil.test"' is contained here`.
- D9 `if not result.ok:` → `if False:` → `test_send_board_alerts_counts_a_bounce_not_a_delivery` red — `messages_sent=1, bounces=0`.
- 0078 downgrade `alter_column(... nullable=False)` removed → `test_0078_round_trip` red — `downgrade must restore board_items.approved_by to NOT NULL` (it checks the inspector, not the absence of an exception).
- `or returning_to_manual` removed → `test_handing_an_over_capacity_auto_board_back_to_manual_is_refused` red — `a MANUAL board was left holding 24 cards in a cap of 8`.
- Two-step capacity probes (temporary test file, 6 passed): flip portrait w/ `items_per_page: 8` then `items_per_page: 4` → `[200, 409, 200, 409]`; auto+`1×1` then `{auto:false, max_pages:2}` → 409, grow then hand back → `[200,409,200,409,200]`; shrink an auto board, reorder all 24, empty approve, hand back → `[200,200,200,409]`; at-cap manual shrink `max_pages` or `items_per_page` → 409/409, rename → 200. Every end state had `cap >= count` for a manual board.
- Non-owner reads on a private profile: `ProfileViewer` → preview/queue/items/auto-preview all **403** (board visible in the list); stranger → all **404** (not in the list).
- Job idempotency: run twice on an auto board with 3 ranked calls → `rows_added/removed (3,0)` then `(0,0)`, positions `[(1,0),(2,1),(3,2)]` identical, public route shows 3.
- Deadline on the wall: public route calls the one shared `deadline_local_date` (Europe/Stockholm); the existing test pins both the helper and the literal `2027-01-01` for 23:30Z Dec 31 (baseline pass). NULL deadline is filtered by `Call.deadline.is_not(None)` in `public.py`, `alerts._renderable_counts` and the queue alike; a call that closed since the last rebuild is dropped per request by `Call.deadline > func.now()` (≤ 5-min poll), and `BoardScreen.isOpen` drops it from a stale copy by local date.
- Public shell: `/b/<slug>` is served by `_spa_fallback` with no auth dependency, `BoardLayout` is a third top-level pathless layout, the fetch uses `credentials: "omit"`; 429/5xx → `transport_error` → last-good copy kept ≤ 60 min then blank; 404/410 → blank. Payload is a closed list (no ids, score, profile) — existing tests, baseline pass.
- QR: the public route encodes nothing; `approve_items` encodes in a prefetch pass with no session open; the job encodes in pass 1 outside the lock; `_MAX_URL_BYTES=800`, `lru_cache(256)`, one global `_encode_lock` (baseline tests pass, not re-mutated).

### Not checked

- **Archived profiles**: no API path archives a profile — `is_active\s*=\s*False` matches nothing in `src/grant_lab/` or `scripts/`. So "the job skips archived profiles" is moot today; latent note: `reconcile_auto_boards` filters only `Board.auto_publish`, and `_profile_for_edit` 404s on an inactive profile, so if an archive path is ever added, a published board on that profile could not be unpublished or deleted through the API while the job keeps rebuilding it.
- **Transfer / disabled owner** (READ only): `transfer_profile_owner` inserts the new owner's `Subscription` row and `board_candidate_parts` reads `Profile.created_by` live, so the next run uses the new owner's hides and D9 addresses them; `delete_board` stays creator-or-admin (documented). A disabled owner escalates to deliverable admins (`test_a_DISABLED_owner_escalates_to_active_admins`, baseline pass); the job keeps using that owner's hides.
- approve-vs-PUT real-thread race not re-run; both call `_lock_board_row` before counting, lock order profile→board, and the job's lock was mutated red.
- Frontend vitest/Playwright not run. The alert-subject consequence of the title finding is inferred from `alerts.py`, not sent through an MTA.
- Deployment: `TRUSTED_PROXY` defaults false; README mandates a reverse proxy and says to set it — unset, every wall viewer shares the proxy's one 60/15-min bucket. Config, not code.

### Numbers you measured

- Baseline, 17 board test files (`-n 4 --dist loadfile`): **228 passed**, 0 failed.
- 15 mutations → 17 red test cases, each for the stated reason; all restored and re-grepped.
- Temporary probe file: 7 passed (6 sequence/read/idempotency + 1 title), then deleted.
- Worktree was at `6c0772df` on arrival; moved to `c94f2de3` (detached) before any work. `git status --short`: empty.

## Appendix A3 — profiles, access control, call decisions (reviewer report, verbatim)

## profiles + access + decisions — verdict in two sentences

The access model is sound where it is claimed to be: 33 stranger-facing surfaces plus the digest leaked nothing from a private profile, every 404 is byte-identical to a nonexistent id, and all 13 adverse mutations I ran (stale-owner transfer race, hollow predicate call, note author scoping on read and write, flip-clears-note, digest/subscription/board-queue filters, 403-oracle, self-transfer, viewer-row delete, 409 scope) reddened on the assertion that names them. The one real gap is at the front door: a profile created through `POST /api/profiles` is `org`-visible, listed to every user and readable by strangers, which contradicts the stated "private by default" rule and is pinned by no test.

### Findings, most severe first

**[MED] [RAN] New profiles are org-readable at creation; "private by default" is not what the code does**
`src/grant_lab/profiles/create.py::create_profile_with_bootstrap` never sets `visibility`; `web/api/profiles.py::ProfileCreateIn` has no `visibility` field; the column's `server_default` is `'org'` (migration 0070). Scenario: user A creates "Hepatology — Larsson group" with description, areas and must-include terms → every active user sees it in `GET /api/profiles`, the calls facet picker and `/me/settings`, can `GET` its full `_flatten_structured` payload and can subscribe to its digest, until A finds the visibility card. The 2026-09-04 note's title says "private by default, owner shares explicitly" and the 2026-09-06 spec says profiles are "personal and private by default"; the note's §3.3 chose `org` only so *existing* rows would not change on migration day, which does not extend to rows created afterwards. The scope note's gate 2 ("default asserted on a profile created through the real endpoint") exists nowhere (`grep visibility tests/test_api_profiles_create.py` → none).
Evidence (probe on clean source):
```
=== DEFAULT VISIBILITY via POST /api/profiles: org | stranger GET: 200 | stranger list has it: True
```
Fix: set `visibility="private"` in `create_profile_with_bootstrap` (keep the migration's server default), optionally expose `visibility` on `ProfileCreateIn`, and pin it with a real-endpoint test. If `org` at creation is the deliberate product choice, record that decision — three documents currently say otherwise.

*Coordinator note (live DB, read-only): 1 profile, `org`, created 2026-06-10; 5 active users.*

**[LOW] [READ] A revoked viewer's hides keep suppressing their no-profile feed with no way to see or undo them**
`web/dashboard.py::_decision_predicate` with `profile_id=None` (archive/inbox) excludes calls hidden on *any* of the user's profiles, including a profile they can no longer read; `?view=hidden&profile=P` degrades to `None` → empty (D8), so the UI has no path to those rows; only the legacy `GET /api/calls/hidden?profile_id=P` ("a surface neither edition calls") reaches them. Scenario: B hides 20 calls on shared P, A un-shares P, B's unranked archive silently lacks 20 calls forever. Only the full edition's no-profile archive is affected (match always sends a profile). Fix: scope the no-profile suppression to `readable_profile_ids`, or surface the stranded rows.

**[LOW] [READ] Viewer-row writers disagree about non-active users**
`transfer_profile_owner` with `keep_read_access=True` (admin acting for a disabled owner) inserts a `ProfileViewer` row for the outgoing user without checking `status`; `add_profile_viewer` and `replace_profile_viewers` refuse non-active users. `list_profile_viewers` shows viewers regardless of status, and `replace_profile_viewers` drops any non-active id silently — so an owner who echoes the list back (the reconcile shape the UI uses) silently removes a disabled-then-re-enabled colleague. Fix: one status rule in a helper used by all three writers, and either show status in the viewer list or do not drop non-active ids on reconcile.

### Checked and sound (all RAN)
- 409 scoped to caller AND index owner-scoped: migration 0076 drops `uq_profiles_active_name`, creates `uq_profiles_active_owner_name (created_by, name) WHERE is_active = true` (matches ARCHITECTURE.md); removing `Profile.created_by == user.id` from the pre-check → `test_a_name_held_by_an_unreadable_private_profile_does_not_409` FAILED `assert 409 != 409`.
- Predicate load-bearing gate: in `list_profiles` left `_r = readable_profile_ids(user)` and deleted the `.where` arm → `test_pinned_surface_calls_the_single_source[web/api/profiles.py::list_profiles]` FAILED "CALLS readable_profile_ids ... but does not USE the result". Same shape in `assemble_for_user` → both the structural gate and `test_gate12_revoking_a_share_removes_the_section` (`[1, 2] == [1]`) FAILED.
- Transfer race: removed `.with_for_update()` in `_profile_for_edit` → `test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner` FAILED 3/3 runs: `both transfers authorized against owner 1: statuses=[200, 200]`. The test uses plain `TestClient(app)` (no lifespan context), real threads, a timed barrier, and counts successes — it can fail.
- `_profile_for_edit` ladder order: swapped ownership-403 before read-404 → `sharing_ops_404_for_a_stranger` FAILED on GET/PUT/POST `/viewers` (`403 == 404`).
- Self-transfer guard removed → `test_a_self_transfer_with_keep_read_access_creates_no_viewer_row` FAILED (`ProfileViewer ... is None`); step-1 delete removed → `test_the_incoming_owners_viewer_row_is_deleted` FAILED likewise; each test red under its own mutation only.
- Lost-read set is computed against the locked row after `p.created_by = target.id` (autoflush) via `can_read_profile`; old owner's `subscriptions` row kept (digest filters it), `default_profile_id` cleared only when read is lost, `call_decisions` keyed on `user_id` so untouched; boards follow `profiles.created_by` (queue, alerts, `_board_for_edit`).
- Decision notes: `set_decision_note` minus `CallDecision.user_id == user.id` → `test_a_colleague_cannot_overwrite_your_note_on_a_shared_profile` FAILED `{1: 'THEIRS'} != {1: 'MINE'}`; `get_call_detail` minus the same → `test_detail_never_shows_another_users_note` FAILED `assert 'THEIRS' is None`; `build_dashboard_view` join minus `user_id` → `test_another_users_note_is_invisible` FAILED `2 == 1`; `hide_call` minus the `note` CASE → `test_hiding_a_saved_call_clears_its_note` FAILED while the save-side gate stayed green (independent).
- `call_decisions.user_id` vs `decided_by`: every writer sets both to `user.id`; nobody decides for anyone else; `decided_by` is SET NULL while `user_id` CASCADEs (informational only).
- Hidden calls excluded from feed (`_decision_predicate`), digest (`dismissed_ids` per profile, state hidden), and board queue: keying the queue off `board.created_by` → `test_a_call_the_owner_hid_is_absent_from_the_queue` FAILED and `..._BOARD_CREATOR_hid_is_still_in_the_queue` FAILED (`[] == [1]`).
- Subscriptions: `update_subscriptions` minus `Profile.id.in_(readable)` → `test_gate11_a_stranger_cannot_subscribe` FAILED `[1, 2, 3] == [1]`. Only three `Subscription(` writers exist (me.py, profiles/create.py, transfer) and all target a readable profile.
- Ten-plus read surfaces, probed with a stranger holding a pre-revocation subscription row and `default_profile_id = P`: list/get/update/viewers×3/visibility/transfer → 404 identical to id 999999; calls archive/inbox/saved/hidden/stored-default → 200 unranked, no marker; detail → no ranking/criteria; counts/hide/save/note → 404; subscriptions → `[]`; settings → no P; default-profile → 404; boards list → `[]`, board get/preview/queue/items/auto-preview/create → 404; workspace create → 400 (same shape as nonexistent); golden-set → 403; `assemble_for_user` → `None`. Marker grep over every body: `LEAKS: {}`. Viewer: read 200, edit/share/transfer/visibility 403, hide 200, board listed but detail 403, subscribe ok.
- Admin: `readable_profile_ids` short-circuits to all, `_profile_for_edit` admin arm edits/shares/transfers any; `admin_users.py` has no profile refs; `admin.py` only counts; golden-set names are `require_admin`-gated per spec D3. Disabled users cannot authenticate; viewer/transfer targets must be `active`.
- Known and accepted, not re-raised: transfer-into-name-collision 400 is still an observable (documented "blunted, not closed"); `update_profile` authorises against an unlocked read (documented); `add_profile_viewer`'s 200-vs-400 reveals whether an email has an active account (inherent to share-by-email).

### Not checked
- The structural gate `_predicate_is_load_bearing` tests *position* (an `if` test or filter argument), not effect: `if not can_read_profile(...): pass` would pass it. The behavioural suites cover that shape, so I did not mutate it.
- Frontend behaviour (TriageFeed note strip, D10/D11) and the golden-set note miner (`test_golden_set_notes.py` exists; not mutated).
- Live DB sizing of how many `org` profiles exist today (coordinator measured: 1).

### Numbers you measured
- HEAD: worktree was at `6c0772df` (63 commits behind); reset my own worktree branch to `c94f2de3`; `grant_lab.__file__` resolves under the worktree.
- Baseline, 8 files (`test_profile_access_single_source`, `test_api_profile_transfer_concurrency`, `test_api_calls_note`, `test_calls_decision_note_projection`, `test_api_call_detail`, `test_api_profile_name_privacy`, `test_api_profile_transfer`, `test_profile_transfer_effect`): **115 passed**, 75s.
- After all restores, those 8 plus `test_profile_access_digest` and `test_boards_queue`: **131 passed**, 83s.
- Probe (temporary `tests/_probe_review_surfaces.py`, deleted): 2 tests, each 1 passed on its final run; 13 mutations (M1–M13), each red for the claimed reason, M2 3/3. `git status --short`: empty.

## Appendix A4 — sources + ingestion (reviewer report, verbatim)

## sources + ingestion — verdict in two sentences

The persistence path is sound (every adapter field reaches both the INSERT and the ON CONFLICT SET, and every gate I mutated reddened for the stated reason), but the EU adapter closes every call roughly 15–17 hours early because SEDIA encodes deadlines as midnight UTC and nothing bumps them to the portal's 17:00 Brussels close. Around that sit three structural gaps: every daily ingest re-embeds every open call it sees (~620 EU calls) even though the fingerprint that could skip them already exists, expiry is filtered on the adapter so a stored call whose deadline moves into the past stays open with stale text, and a failing source is still invisible to anyone who has not configured an undocumented alert email.

### Findings, most severe first

**[MED] [RAN] EU deadlines arrive as midnight UTC and the adapter has no end-of-day bump — every EU call is treated as closed from 02:00 Stockholm on its final day**
`src/grant_lab/sources/eu_portal.py` — `_parse_iso`, `_deadlines`, `_normalize`. The live-captured fixture (`tests/fixtures/eu_portal_search_page.json`, 2026-09-08) encodes both records' `deadlineDate` as `T00:00:00.000+0000`; the portal's published close is 17:00 Brussels. The adapter stores that midnight instant unchanged and compares it with `deadline <= now`. Dashboard (`dashboard.py` `open_expr`: `Call.deadline > now`), digest (`assemble.py` lines 240/267), the ranking timeline gate (`criteria._timeline`: `deadline > now`) and the admin open-call count all read the same column, so the call is closed everywhere from 00:00 UTC on its last day, and the 03:00 ingest drops it from the feed. The Vinnova adapter solved the identical shape with `end_of_day_if_midnight=True`; the EU tests' `_meta` default assumes `T17:00:00.000+0000`, which the fixture contradicts.
```
$ uv run python - # json.load fixture
deadlineDate time-of-day suffixes: {'T00:00:00.000+0000': 2}
$ probe_nodb.py
closes today, T00:00+0000, ingest 01:00Z -> kept: 0
closes today, T17:00+0000, ingest 01:00Z -> kept: 1
Vinnova date-only today ->  2026-09-08 21:59:59+00:00
$ mutate vinnova end_of_day_if_midnight=True -> False
FAILED tests/test_sources_vinnova.py::test_midnight_deadline_today_is_treated_as_end_of_day
E       AssertionError: assert [] == ['CLOSES-TODAY']
```
Fix: in `_deadlines`, when a parsed instant is exactly midnight, bump to 17:00 Europe/Brussels (or 23:59:59) before comparing/storing; pin with a test on the real fixture.

*Coordinator note (live DB, read-only): stored `eu_portal` deadlines at 00:00 UTC = 670 rows; 17:00 = 1; 22:00 = 1. Promoted to H3.*

**[MED] [RAN] Every ingest re-embeds every call the adapter yields, changed or not — the fingerprint that could skip them is ignored on this path**
`src/grant_lab/ingestion/orchestrator.py` `run_ingestion` step 2 → `src/grant_lab/embeddings/refresh.py` `refresh_call_embedding`. `touched_call_ids` is every upserted row (the ON CONFLICT refreshes `fetched_at` on every encounter), and `refresh_call_embedding` embeds unconditionally. `EUPortalAdapter.fetch_new_calls` ignores `since` and yields the full open set (~623 live), so ~620 bge-m3 embeds run on the 2080 Ti every night at 03:00, interleaved with the 60 s `rank:drain` Gemma calls. Under a hung (not refused) Ollama each embed waits the client's 60 s default timeout before step 3 enqueues genuinely new calls. `tests/test_ingestion_orchestrator_embeds.py::test_orchestrator_re_embeds_touched_calls_even_when_already_present` pins this with "no 'unchanged call' optimization until we have a content-hash mechanism" — `embedding_fingerprint` (migration 0080) is that mechanism. Ranking is NOT re-enqueued (freshness is fingerprint-gated), so this is GPU time and contention, not Sonnet spend.
```
test_probe_db.py::test_b1  B1 embed.call_count after 3 identical runs: 3
E       AssertionError: unchanged call re-embedded on every run: call_count=3
test_probe_db2.py          Q embed.call_count: 3 | ranking_jobs rows: 1
```
Fix: in step 2 skip when `embedding is not None and embedding_model == embed_client.model and embedding_fingerprint == call_embedding_fingerprint(call)`; flip the pinning test to `call_count == 1`.

**[MED] [RAN] Expiry is filtered on the adapter, so a stored call whose deadline is brought forward (or that is withdrawn) stays open in the store with its old text**
`eu_portal.py` `_normalize` (`skip_expired`), `vinnova.py` `_normalize` (`skip_closed`). Run 1 stores a call with deadline +30 d; run 2's feed carries the same identifier with a past deadline and a rewritten title/body → the adapter yields nothing → the row keeps the old deadline, title and body and is still counted open, so it stays on the dashboard, in digests and boards, and ranking-eligible until the stale deadline passes. The spec's §6 TODO covers only "calls that vanish from the feed"; this is the same mechanism with no TODO.
```
C1 run2: IngestionResult(source_slug='eu_portal', inserted=0, updated=0, error=None)
C1 stored (deadline, title, full_text): (2026-10-08 17:00+00, 'Call X', 'original body') | counted open after the change: 1
E       AssertionError: stored row kept the OLD deadline 2026-10-08 17:00:00+00:00 after the portal moved it to the past
```
Fix: let the adapter yield expired rows with their real deadline and drop/close them in the orchestrator, or add a reaper that closes rows absent from a full-set feed.

**[MED] [RAN] Nothing user-visible surfaces a failing source; the only alerts are emails gated on `ADMIN_ALERT_EMAIL`, which defaults to None and is undocumented**
`src/grant_lab/web/api/admin.py` `SourceOut`/`admin_dashboard`, `frontend/src/routes/AdminDashboard.tsx`, `src/grant_lab/web/routes/health.py` `healthz`, `config.py` `admin_alert_email`. The streak IS persisted (probe D1: page-2 404 → `consecutive_failures=1`, `last_fetched_at=None`), but `graft find_all consecutive_failures|last_alerted_at` finds no reader outside `ingestion/alerts.py`; the admin table shows only a date or "never"; `/healthz` checks the DB only; `grep ADMIN_ALERT_EMAIL README.md docker-compose.yml docs/*.md` → no hits. Still open since spec 2026-09-08 §6.
```
D1 calls persisted: 0 | (consecutive_failures, last_fetched_at, last_alerted_at): (1, None, None)
D2 SourceOut fields: ['name', 'slug', 'last_fetched_at', 'call_count']
E       AssertionError: admin API does not expose the failure streak
```
Fix: add `consecutive_failures`/`last_alerted_at` to `SourceOut` and badge the row when streak ≥ 3 or `last_fetched_at` older than `stale_source_alert_days`; document `ADMIN_ALERT_EMAIL`.

**[LOW] [RAN] `POST /admin/sources/{slug}/fetch` reports `triggered: true` while a run is in progress, but APScheduler skips the triggered run**
`src/grant_lab/scheduler.py` `trigger_job_now` checks only `scheduler.running` and job existence; with `max_instances=1` the second fire is dropped and `next_run_time` reset to the next cron.
```
trigger 1 -> True
trigger 2 while running -> True
WARNING apscheduler.scheduler: ... skipped: maximum number of running instances reached (1)
runs executed: 1 | next_run_time: 2026-10-07 03:00:00+02:00
```
Fix: track a running flag per ingest job and return False / 409 while set.

**[LOW] [RAN] One non-dict element in SEDIA `results` aborts the whole run with an uncaught `AttributeError`; a dict-valued `identifier` ingests as junk identity**
`eu_portal.py` `_normalize` calls `item.get("metadata")` before any `isinstance(item, dict)` guard; `_first` stringifies anything.
```
results contains a string: RAISED AttributeError: 'str' object has no attribute 'get'
results contains null: RAISED AttributeError: 'NoneType' object has no attribute 'get'
identifier is a dict: yielded 1 ["{'a': 1}"]
```
The orchestrator's broad `except Exception` turns this into an ERROR log + failure count with 0 rows committed (same discard as D1). Fix: skip non-dict items; require `_first` results to be `str`.

**[LOW] [READ] Identifier reuse overwrites in place and drags attachments along**
`upsert.py` `upsert_call` keys on `uq_calls_source_external` and updates url/title/body in place, so rankings, decisions and board items (all FK `call_id`) follow whatever the identifier now denotes. Two sources carrying one call produce two rows by design (EU and Vinnova do not overlap). No mitigation needed beyond awareness.

### Checked and sound (all RAN)
- (a) All 13 `NormalizedCall` fields are in the INSERT and in `ON CONFLICT ... SET`; only `id`, `embedding`, `embedding_model`, `embedding_fingerprint` are untouched (probe A compiled SQL). Dropping `published_at` from the payload → `FAILED test_upsert_round_trips_published_at: assert None == datetime(2026, 4, 10)`.
- (b) Changed title → new fingerprint and one more embed (B2 pass). Embedder outage mid-ingest leaves the old vector AND old fingerprint while the title updates; the daily tick then re-embeds it: `calls_embedded: 1`, fingerprint changed (B3 pass).
- (c) EU `_pick_deadline` returns the earliest open round, last round when all closed; mutating to entry `[0]` → 2 FAILED (`a call with an open later round must not be dropped`, `the earliest open round`). No deadline → kept with `deadline=None`; all-unparseable deadlines → treated as no deadline → kept; an exactly-now deadline is dropped (`<=`). Vinnova drops no-deadline rounds (documented choice).
- (d) Page-2 404 → 0 of the 100 page-1 rows persisted, streak=1, `last_fetched_at` unmoved (D1 pass; `request_with_retry` retries only 5xx/transport). Removing `alerter.record_failure` → 2 FAILED (`Expected 'record_failure' to have been called once. Called 0 times`).
- (e) Vinnova: Swedish title + English body → `title='Svensk titel'`, body English; embedder/ranker text = title // bilingual summary // English body. Blank English anywhere → Swedish; partial English → Swedish wholesale.
- (f) Digest HTML env `autoescape=True` → `<script>` renders `&lt;script&gt;`; forcing `autoescape=False` → `FAILED test_html_escapes_xss_in_title`. Template has no `|safe`. The calls API never exposes `full_text` (`CallDetailCallOut` carries `summary` only); SPA renders title/summary through JSX/Mantine `<Text>`; no `dangerouslySetInnerHTML` or server-rendered HTML in `web/`.
- (g) Per-source dedup confirmed by `test_upsert_is_per_source`, in-place update by `test_upsert_replaces_existing_row_in_place` (baseline run).
- (h) Cron 03:00/03:15 Stockholm, `max_instances=1`, `coalesce=True`, `misfire_grace_time=6h`; no tick budget on ingest (EU: 30 s timeout × 3 attempts × ≤200 pages; Vinnova aborts at 5 consecutive round failures).

### Not checked
- Live SEDIA/Vinnova payloads (network forbidden) — the midnight finding rests on the 2-record captured fixture plus the portal's published 17:00 Brussels convention (and, per the coordinator, 670 live rows).
- Whether `ADMIN_ALERT_EMAIL` is set in the live `.env` (forbidden to read).
- SSRF/pinned-transport (`tests/test_adapter_ssrf_fail_closed.py`) — security reviewer's scope.
- `ingestion/cli.py` and the Vinnova `since`-date semantics of `/api/utlysningar/{date}` (needs live data).
- Board wall rendering beyond the `dangerouslySetInnerHTML` grep.

### Numbers you measured
- Baseline, 14 files (`test_sources_*`, `test_ingestion_*`): **170 passed** (19 s, `-n 4 --dist loadfile`).
- Mutation round 1 (upsert / eu_portal / vinnova / digest render): **5 failed, 73 passed** — all 5 the targeted gates.
- Mutation round 2 (orchestrator `record_failure` dropped): **2 failed, 14 passed**.
- Probes: `test_probe_db.py` 3 failed (B1, C1, D2 = findings) / 3 passed (B2, B3, D1); `test_probe_db2.py` 1 passed; `probe_a_fields.py`, `probe_nodb.py` ran clean.
- Worktree was handed at `6c0772df` (63 commits behind); reset to `c94f2de3` on a clean tree before any work. `git status --short`: empty.

## Appendix A5 — proposal-assist spend, compaction, citations, reference deletion, fence gates (reviewer report, verbatim)

## spend + citations + fence — verdict in two sentences

The ledger, compaction retry-bounding, timeout classification, cross-branch deletion protection and all three fence primitives are real and every mutation I ran against them reddened at the asserted line — but one hosted spender sits entirely outside the system: the Phase 0 setup tick bills `claude-opus-5` twice per new call with no cap pre-check and no ledger row (proven by driving the real tick past a tripped global cap). Two gates that pin central claims are blind to an honest bypass (`LlmSpend` via the `proposal_assist.spend` intermediary; SQLAlchemy via an allowlisted `ports/` module), and `_is_cited` fails open on a broken ref despite its docstring promising the opposite.

### Findings, most severe first

**[HIGH] [RAN] Phase 0 setup bills Opus with no cap pre-check and no ledger row**
`scheduler.py` `_phase0_tick`/`make_client` → `phases/setup.py` `run_phase0_tick` → `Phase0Worker` → `extractors/refresh.py` `compute_section_extracts`/`compute_rubric` → `extractors/sections.py` `extract_sections` and `extractors/rubric.py` `extract_rubric`, each `client.chat(system+full call text)` and discard `envelope["usage"]`. Client is `AnthropicClient(model=settings.proposal_assist_llm_model)` = `claude-opus-5`, default `max_tokens=4096`, SDK `max_retries=2`. `graft callers record_spend` and `graft callers check_global_daily_token_cap` list nothing under `phases/` or `extractors/`; `SpendKind` has no kind for it; `config.py` has no phase0/setup cap. Scenario: org already over the 5M global cap → a workspace on a new call still makes two Opus calls, invisible to every cap and to the spend view. Temporary probe test (deleted) seeded `tokens_in=10_000_000`, drove the real tick with a mocked `chat` reporting usage:
```
PROBE: global cap tripped=True; hosted chat() calls made=2; llm_spend rows before=1 after=1; kinds={'chat_turn'}
1 passed in 3.01s
```
Fix: `check_global_daily_token_cap` before `client_factory()` in `run_phase0_tick` (requeue the job on denial), `record_spend(kind="setup", ...)` on every terminal path of both extractors (add the kind), and `max_retries=0` like compaction.

**[MED] [RAN] `_is_cited` fails OPEN when a branch ref is broken — docstring says the opposite**
`references/deletion.py` `_branches_cite`: `except (GitCommitError, OSError): return False` around `branch_tips`, commented "not a git repository". But `gitio.py` `branch_tips` deliberately raises `GitCommitError("broken ref ...")` when `for-each-ref` prints "ignoring broken ref" (rc=0), and that raise is swallowed into "uncited". `_is_cited`'s own docstring: "A git repository whose refs cannot be read → True". Probe (scratchpad script, real repo: `main` checked out cites nothing, `feature` intact and CITES rid, `other` = garbage ref file):
```
control_intact      for-each-ref rc=0 stderr=''                                   -> _is_cited=True
garbage_ref         for-each-ref rc=0 stderr='warning: ignoring broken ref refs/heads/other' -> _is_cited=False
dangling_ref        for-each-ref rc=0 stderr=''                                   -> _is_cited=True
unreadable_refs_dir for-each-ref rc=0 stderr=''                                   -> _is_cited=False
```
Outcome: hard delete, bytes removed by `storage.delete_one`, while `feature` still cites the id. (`unreadable_refs_dir` is a second shape: git lists zero branches silently.) Fix: in `_branches_cite`, return False only for "not a repository" (e.g. `.git` absent / `rev-parse --git-dir` rc 128); any other `GitCommitError`/`OSError` from `branch_tips` → True.

**[MED] [RAN] One-writer guard is blind to the `proposal_assist.spend` intermediary**
`tests/fencing/spend_guard.py` `llm_spend_writers` resolves bindings only to `_LLM_SPEND_TARGETS` = `grant_lab.db.models[.spend].LlmSpend`. `proposal_assist/spend.py` binds `LlmSpend` at module level, so `from grant_lab.proposal_assist.spend import LlmSpend` is a working second writer (`uv run python -c` → `resolves to grant_lab.db.models.spend llm_spend`). With probe `proposal_assist/_probe_reexport.py` constructing `LlmSpend(**k)` through it, `test_the_ledger_has_exactly_one_writer` **passed** (group-1 run: `6 failed, 23 passed`, this test among the passes). Positive control: a relative `from ...db.models import LlmSpend` in `turns/_probe_rel.py` WAS caught (`Left contains one more item: 'grant_lab/proposal_assist/turns/_probe_rel.py'`); an `importlib.import_module("grant_lab.db.models").LlmSpend` writer was not (AST limit, LOW). Fix: resolve transitively — any module that itself resolves to a target becomes a target for its importers (that is what "whole import chains" in 45c27d01's title promises), or at least add `grant_lab.proposal_assist.spend.LlmSpend` to the targets.

**[MED] [RAN] Tools import boundary has no gate on `ports/`, so an indirect reach passes**
`tests/fencing/import_gate.py` `check_source` allowlists `grant_lab.proposal_assist.ports.*` and checks only files under `tools/`; nothing in `tests/fencing/` or `tests/architecture/` pins what `ports/` may import (grep: only `test_detectors.py` imports from it). Probes `ports/_probe_leak.py` (`from sqlalchemy import select`) + `tools/_probe_indirect.py` (`from grant_lab.proposal_assist.ports._probe_leak import select`) and `tools/_probe_importlib.py` (`importlib.import_module("grant_lab.db.models")`): `test_import_gate.py` all 60 green (group-2 run `3 failed, 53 passed`, none from this file). The gate's own docstring names this shape as the reason for the positive list. Fix: one test that runs `check_source` over every module in `ports/` (stdlib + `ports` only).

**[MED] [READ] Citation client keeps SDK `max_retries=2`; a timeout is a zero-token row the citation caps never see**
`citations/llm.py` `build_citation_llm` passes no `max_retries`; measured (`probe_citation_client.py`, no network): `model= claude-sonnet-5-5 | max_tokens= 1024 | sdk max_retries= 2 | timeout= 60.0`. Compaction's client was pinned to 0 precisely because "SDK retries on top made each billed attempt three" (`scheduler.py` `make_summarizer_client`). On the citation path a read timeout → up to 3 server-side-billed requests → `service.py` `complete_json` `except TimeoutError: _safe_record(..., "timeout", 0, 0, None)` → one row with 0 tokens. `usage.py` `_sum_today`/`check_citation_caps` sum tokens only, so timeout rows never move the per-op, global-citation or global ledger cap; the `count_billed_attempts` "timeout counts" rule exists for compaction only. Fix: `max_retries=0` in `build_citation_llm` (the analysis queue already retries jobs 3×) and count `status='timeout'` rows in `check_citation_caps`.

**[LOW] [RAN] The `path_exists_at_commit` fail-closed branch in `_branches_cite` is untested**
Mutated `return True  # fail closed: the repo moved or broke` → `continue`: `references/test_deletion.py` 17/17 green (group-3 run). The branch does work (the `dangling_ref` probe above → True via `ls-tree` rc=128). Fix: add a test with a well-formed ref to a missing object.

**[LOW] [READ] `citation_llm_usage.status` cannot tell a `max_tokens` truncation from a parse failure, and failed rows lose the model**
`anthropic_client.py` raises `UnusableReplyError` (deterministic, re-billing on retry) with `.usage`; `citations/llm.py` `StructuredLLM.complete_json` raises `CitationLLMOutputError`/`JSONDecodeError` with `.usage`; both reach `service.py`'s `except Exception` → `"failed"` with billed tokens and `model=None` (`_safe_record(..., None)` even when the envelope named the model). `audit.py` degrades per row (`audit_unavailable reason="transient"`), so a truncation is re-billed on every audit run. Fix: pass `result.model`/`envelope["model"]` on the failed row; raise `CITATION_LLM_MAX_OUTPUT_TOKENS` or map `UnusableReplyError` to a distinct reason.

**[LOW] [READ] Claim (b): the `summarized_through` CASCADE is unreachable from app code**
No deletion path for `chat_turns` exists (grep for `delete(ChatTurn)`, `session.delete(turn)`, raw `chat_turns` deletes across `src/` and `alembic/`: none; only the workspace CASCADE, which also drops summaries and spend). If a turn were deleted by hand, the summary vanishes, `compaction.py` `_summarized_through_position` falls back to the previous boundary, the next tick re-summarizes everything since it — one extra billed call, then correct; `latest_summary_text` re-injects the older summary once. Minor: the failure row's `model=(envelope or {}).get("model")` is None on a billed error whose usage rides on `exc.usage`.

### Checked and sound (all RAN)
- (a) Failed compaction records billed usage — `compaction.py` failure branch `tokens_in=0` → `test_a_compaction_cut_off_after_the_api_answered_records_the_billed_usage`: `assert ('failed', 0, 0) == ('failed', 900, 4096)`.
- (a) Only read timeouts are "possibly billed" — `anthropic_client.py` `if isinstance(e.__cause__, _UNSENT_TIMEOUTS)` → `if False`: `[connect-failed]` `assert 'timeout' == 'failed'`; `test_a_timeout_before_the_request_was_read_is_a_plain_connection_error[connect|pool|write]` `assert not isinstance(AnthropicTimeoutError(...), TimeoutError)`.
- (a) Timeouts count toward `COMPACTION_MAX_BILLED_ATTEMPTS` — `spend.py` `count_billed_attempts` without the `status == "timeout"` clause: `[rows4-None-False-False]` `assert True is False`.
- (a) Attempt window restarts at the last summary — `compaction.py` `_billed_attempts_since_last_summary` ignoring `last_summary`: `[rows6-after-False-True]` `assert False is True`. Note the brief's "not re-billed on the retry" is stronger than the code: a summary whose write fails IS re-billed, bounded to 3 billed attempts per 24 h, then resumes next day.
- (a) Citation failed row carries billed usage — `service.py` `tokens_from_usage(...)` → `0, 0`: `test_a_failure_after_a_billed_reply_records_its_usage` `assert (0, 0) == (55, 7)`.
- (c) Every `record_spend` writer (`graft callers`): `citations/usage.py` `record_usage`; coherence/evaluator/uncited_claims `pass_runner` success + `_fail`; review `_run_one_call`; `compaction.py`; `outcomes.py` `record_terminal_outcome` + `stamp_post_terminal_usage`; `processor.py` `_finalize_success`. Pre-checks (`graft callers check_global_daily_token_cap`): `submit_user_turn` (global BEFORE workspace, as `config.py` documents), `process_turn_once` re-check at claim, `compact_workspace`, all four pass runners + their `web/` start routes, `CitationLLMService.complete_json`. Per-workspace per-feature caps: `coherence_cap_exceeded`/`evaluator_cap_exceeded`/`uncited_claims_cap_exceeded` and `review_dollar_cap`. Day boundary: `stockholm_day_start_utc` in `sum_spend_today`, `_sum_today`, `check_daily_token_cap` and all three `_cap_exceeded`. `check_daily_token_cap` reads `ChatTurn` columns, chat-only by its docstring. Remote-rank escalation (`scheduler.py` first `AnthropicClient`) is outside `llm_spend` with its own count cap — ranking reviewer's scope.
- (e) Cross-branch protection — `_is_cited` → working tree only: `test_reference_cited_on_ANOTHER_branch_is_not_hard_deleted` and `test_corrupt_ledger_on_ANOTHER_branch_fails_closed` both `assert 'deleted' == 'tombstoned'`. The 3-branch real-repo fixture with an orphan branch lacking `citations.json` already exists (`test_uncited_on_EVERY_branch_still_hard_deletes`, hard-deletes at baseline); absent-file vs broken-tree separation via `ls-tree` confirmed by the `dangling_ref` probe → True.
- (g) Primitive A `live_at_generation` `==` → `>=`: `test_does_not_match_a_generation_below_the_current_one` `assert True is False`. Primitive B `acquire_commit_mutex_at_generation` check → `if False`: three `DID NOT RAISE WorkspaceGenerationStale` (incl. `test_the_generation_is_read_UNDER_the_mutex_not_before_it`). Primitive C `acquire_shared_lease` lock → `SELECT 1`: `assert 'acquired' == 'blocked'`, `DID NOT RAISE CommitMutexTimeout`. Production writers read: B → `branches.py` `switch_branch`, `collab/kernel.py` `kernel_accept_prose`; A+C → `port_impl/effects.py` `submit_scorecard` (lease first, predicate inside the UPDATE's WHERE, `landed is None` → `_explain_refusal`).
- (d) Relative import from a nested package IS resolved by `_resolve_from` (positive control above).
- (f) Measured: `claude-sonnet-5-5`, `max_tokens=1024`, 60 s, SDK retries 2.

### Not checked
- `tests/fencing/inventory.py` and `detectors.py` beyond a skeleton skim: they implement the fence-site inventory (tool/git commit sites); 45c27d01 and 14be7396 touched only `spend_guard.py`, so the "one-writer guard" the brief attributes to them is covered above.
- `ChatSummary.created_at` vs `LlmSpend.created_at` ordering under clock skew: both `server_default=func.now()` and the ok row commits in an earlier transaction, so spend < summary on one DB; not probed further.
- The drafting adapter's `_sum_input_tokens` (includes cache tokens — read only); the full suite; `tests/architecture/` with any probe present; live DB/Ollama/network untouched; `ANTHROPIC_API_KEY` never set (the client probe used a plain namespace object).

### Numbers you measured
- Worktree moved from `6c0772df` to `c94f2de3` by `git merge --ff-only` (was behind); `grant_lab` imports from the worktree.
- Baseline: `tests/fencing/test_spend_guard.py` + `tests/fencing/test_import_gate.py`: **93 passed**. Thirteen DB-backed files (`test_spend_compaction`, `test_ranking_anthropic_client`, `citations/test_service`, `citations/test_llm`, `citations/test_audit_usage_ledger`, `test_spend_citation`, `references/test_deletion`, `test_fencing`, `test_fencing_mutex`, `test_fencing_lease`, `test_turns_compaction`, `test_caps_read_the_ledger`, `test_global_cap`): **130 passed**.
- Mutation groups: G1 `6 failed, 23 passed` (one-writer test green = blind); G2 `3 failed, 53 passed` (import gate green = blind); G3 `6 failed, 49 passed` (deletion green under E2 = untested branch); G4a `1 failed, 9 passed`; G4b `1 failed, 9 passed`. Phase 0 probe `1 passed` (defect shape). Every mutation restored by `cp .bak`; every probe file removed. `git status --short`: empty.

## Appendix A6 — frontend (reviewer report, verbatim)

## frontend — verdict in two sentences

The frontend at `c94f2de3` is in good shape: tsc, oxlint and all 1229 vitest tests are green, `schema.gen.ts` is byte-identical to a fresh regeneration, the bundle gate reddens on a planted cockpit import, and 12 of the 13 behavioural claims under attack reddened their named test when mutated. Two real defects remain — the corridor wall renders **no titles at all** on any panel other than 1920x1080 (known-open, re-run, worse than recorded), and the call page shows the **pre-decision state** when re-opened within the 20 s staleTime because no decision invalidates its `["call", id, profile]` key — plus one flaky e2e that points at a narrow URL-write race in the triage toolbar.

(Worktree note: it was cut at `6c0772df`; I fast-forwarded my own branch to `c94f2de3` before starting, as the brief required.)

### Findings, most severe first

**[MED] [RAN] Corridor wall renders zero titles at 1366x768 and cuts every title mid-line at 1600x1000 — only 1920x1080 is clean**
`frontend/src/routes/BoardScreen.module.css` `.cardTitle` (`flex: 1 1 auto; min-height: 0; overflow: hidden`) against the fixed-px `.cardQr` (170px) while everything else is vh-sized. Scenario: the board is put on any non-1080p landscape panel → a passer-by sees funder, "Closes in 9 days", budget and a QR, with no call name. Previously known OPEN (memory note: "3 titles at h=0", user decided to leave it on 2026-09-23); re-ran against the real match bundle with the e2e `LONG_BOARD` fixture — **all 8** are at h=0 now:
```
=== 1920x1080: cards=8 pageScrollHeight=1080 titles h=0: 0  clipped: 0  sliced mid-line: 0
=== 1366x768:  cards=8 pageScrollHeight=768  titles h=0: 8  clipped: 8
  h=      0 sh= 116 lh=22.96 lines=0 cardBottom=385 "Utlysning: Kompetenscentrum fö"   (x8)
=== 1600x1000: cards=8 pageScrollHeight=1000 titles h=0: 0  clipped: 8  sliced mid-line: 8
  h= 104.73 sh= 181 lh=29.9 lines=3.5 cardBottom=502 "Utlysning: Kompetenscentrum fö"
=== 2560x1440: titles h=0: 0  clipped: 0   (fits: sh == h for all 8)
```
Screenshots at both sizes confirm it (1366x768: no title on any card; 1600x1000: "resurseffektiv" cut through the glyphs). Fix: give the title band a px floor derived from line-height × lines (e.g. `min-height: calc(5 * 1.15em)`) and let the QR band shrink below 1080p, or scale the QR in px with the viewport; add a third viewport to `e2e/match.spec.ts`'s corridor gate.

**[MED] [RAN] Re-opening a call within 20 s of deciding it shows the pre-decision state and offers Save/Hide again**
`frontend/src/api/calls.ts` `useCallDetail` keys the page `["call", id, profile]`; `frontend/src/api/decisions.ts` `invalidateDecisions` (used by every save/hide/unsave/unhide hook) invalidates `["calls"]` and `["decision-counts"]`, which does not prefix-match `"call"`. `CallDetail.tsx`'s own comments acknowledge this and paper over it with local `override` state — which dies on navigation. Scenario: open a call → Save (status reads "Saved to this profile") → "← All calls" → click the same title again (or `s` in the feed then open the call) → the page offers **Save** and **Hide**, no status line, and the note just written is gone; pressing Hide now flips the server row. Probe test (temporary, deleted) on the clean tree:
```
qc.setQueryData(["call", 5, 7], UNDECIDED); GET mocked to return SAVED; await invalidateDecisions(qc); mount CallDetail
expect(get).not.toHaveBeenCalled()                                   // passed: no refetch
expect(screen.queryByText("Saved to this profile")).toBeNull()      // passed: stale state shown
expect(screen.getByRole("button", { name: /^Save$/ })).toBeEnabled() // passed
await qc.invalidateQueries({ queryKey: ["call"] }); findByText("Saved to this profile") // passed: control
      Tests  1 passed (1)
```
Fix: add `qc.invalidateQueries({ queryKey: ["call"] })` to `invalidateDecisions` and to `useSetDecisionNote`'s `onSuccess` (or re-key the detail query under `["calls", "detail", …]`).

**[LOW] [RAN] e2e "search and deadline order survive the detail round trip" is flaky under load, and the toolbar's URL write is the reason**
`frontend/src/routes/TriageToolbar.tsx` `change()` builds the next URL from the render-time `params` (`new URLSearchParams(params)`). If the Order change fires before the re-render that follows the search navigation commits, the second write is built from the pre-search params and `q` is dropped. Full-suite run: `1 failed / 30 passed` with `Expected: "genomics" Received: ""` at `match.spec.ts:909`; the same test isolated: `--repeat-each=6` → `6 passed (18.8s)`. A human cannot easily hit the window, but the gate is red one run in a while. Fix: read the current search at event time (`new URLSearchParams(window.location.search)` or `useLocation().search`) instead of the render-time `params`.

**[LOW] [RAN] The caret bookkeeping in `TriageCard.DecisionNote` is inert, and four caret tests cannot fail for their stated reason**
`frontend/src/routes/TriageCard.tsx` `keepCaret` / `onCommit` and `DecisionNoteStrip`'s `onCommit` prop exist to stop the `[autoFocus]` effect re-running on "the remount a save causes" — but since `7d0f237c` the strip is keyed on `call.id` only, so no remount happens and the effect never re-fires. Hard-coding `autoFocus={true}` (keepCaret ignored): `TriageCard.test.tsx` + `TriageFeed.test.tsx` → **87 passed (87)**. Making a blur-commit keep the caret: `TriageFeed.test.tsx` → **57 passed (57)**. The tests "does not pull the caret back after a BLUR commits", "leaves the caret in the SECOND row's box", "KEEPS the caret when committed with Enter" and "keeps the caret across the remount a successful save causes" therefore pass vacuously. Behaviour is correct; the dead mechanism and its 60 lines of load-bearing comments will mislead the next editor. Fix: delete `keepCaret`/`onCommit`, pass `autoFocus` on click-open only, and drop or rewrite the four tests.

**[LOW] [RAN] Two shipped claims have no pinning test**
(a) `frontend/src/routes/CallDecisionActions.tsx` `decide()` — "a flip clears the old reason in local state" (`note: next === state ? note : null`): mutated to keep the note → `CallDetail.test.tsx` **31 passed (31)**. (b) `frontend/src/routes/BoardQueue.tsx` — "the approval queue says when it is truncated" (`items.length >= QUEUE_PAGE_SIZE`): mutated to never show → `BoardQueue.test.tsx` **12 passed (12)**; nothing in that file mentions the message. Both code paths are correct by reading; add one assertion each.

**[LOW] [READ] `nanoid` (two high advisories) ships in the match bundle through `ics`**
`frontend/src/routes/callCalendar.ts` imports `createEvent` from `ics`; `dist/module-manifest.json` lists 45 `ics`/`nanoid` modules. `pnpm audit --prod`: 4 high + 1 moderate, but the tiptap/prosemirror ones are full-edition only (0 matches in the match manifest). Practical exposure is low (`ics` uses nanoid's default generator for UIDs). Fix: bump `ics`/`nanoid` when the 7-day cooldown allows, or generate the one-line UID locally.

**[LOW] [READ] The public wall is bilingual, and commit `cfeb5324`'s title overstates it**
`BoardCard.tsx`/`BoardScreen.tsx`: Swedish title, "Sida N av M" and sv-SE number format sit beside English "Closes in N days" / "Closes today" / "From" / "Up to" / "No calls are currently posted" / "QR code linking to …" (visible in both screenshots). "speak Swedish on the wall" changed only `toLocaleString("sv-SE")`; the English strings are pinned as-is by `BoardScreen.test.tsx` ("Closes in 5 days") and `BoardCard.test.tsx`. Decide one language for the wall and update those two assertions.

**[LOW] [READ] A malformed `?sort=` or `?page=` deep link dead-ends the index**
`frontend/src/api/dashboard.ts` `dashboardParamsFrom` sanitises `view` and `min_score` but passes `sort` through (`as "newest" | "deadline"`) and `Number("abc")` → `NaN` for `page`, so `/?sort=foo` is a 422 → "Couldn't load calls." with no way back except editing the URL. Degrade both like `view`.

**[LOW] [READ] Saved view in the "Shortlist" layout has no mouse control to un-save**
`TriageFeed.tsx` passes `expanded={i === focused && (view !== "saved" || layout === "expanded")}` and `TriageCard.tsx` renders "Return to inbox" only inside `expanded` — so in Saved/Shortlist no card carries the button; `u` still works, mouse users must switch to "Details" first.

### Checked and sound (all RAN)
- Strip adopts the server value only when the box is clean: unconditional adopt → `TriageCard.test.tsx:629` "does not discard typing…" reddened (value lost); never adopt → `:878` "re-seeds the open editor…" reddened (`Expected "padded reason" Received "   padded reason   "`).
- Editor key is `call.id` (no remount on refetch): keyed on the note → `:629` reddened.
- Click-opened note box takes the caret: `autoFocus={false}` → `:753` "puts the caret in the box it just opened" reddened (`activeElement` was `<body>`).
- Failed note save is visible: `setError` removed → `DecisionNoteStrip.test.tsx:104` reddened (`Unable to find role="alert"`), under `<StrictMode>`.
- Decisions survive a reload: `initialState={null}` → `CallDetail.test.tsx:82` and `:121` reddened; Saved/Hidden views show the note (`TriageCard.test` "prints the note under the title in Hidden/Saved" in the green suite).
- Corridor poll ordering: superseded-poll guard removed → `publicBoard.test.ts:235` reddened (`expected {…} to be null`). Payload guard: per-element check removed → `:254` reddened. Abort spans the body read: timer cleared before `res.json()` → "settles a headers-then-stall response" **timed out in 5000ms**.
- Dark-edition card edge: white card + black `box-shadow` ring reads on both `#1a1b1e` and `#faf9f7` grounds (READ of the CSS; e2e paints at 1920x1080).
- Boards admin: preview answers for the FORM → `query: {}` reddened `BoardDetail.test.tsx:566` and `:605`; items_per_page reconciled after a save → effect disabled reddened `:675`; settings bounds mirror the backend (`ge/le` 1–64, 0–100, 1–3600, 1–64; the 192-card total is server-side and surfaced verbatim via the 409 test).
- Match-edition bundle gate: `import { Cockpit }` + a route in `router.simple.tsx` → `pnpm run check:bundle` **exit 1** ("36 excluded module(s)", `src/cockpit/…`), `router.simple.test.tsx` 2 failures (`['/cockpit-leak']` ≠ `[]`); `checkMatchBundle.test.ts` and `noCockpitLinks.test.tsx` stay green **by design** (they pin the script's lists and link literals, not the router graph — their headers say so). Clean build: `522 modules, 0 under 13 excluded paths, 71 app modules`. Built `<title data-edition-title>Grant Lab — Call Match</title>`; `resolveEdition` exact-match pinned by `edition.test.ts`.
- `api/client.ts`: probe (deleted) — 401 → `location.assign("/login?next=%2Fcalls%2F5%3Fprofile%3D7%26view%3Dsaved")`, also with an empty body; 429 JSON envelope → its message, text 429 → "request failed"; empty-body 503 → `HTTP 503` — 4/4 passed. No redirect loop: bad credentials are **400** (`_denied()` in `web/api/auth.py`), `/login` makes only the public `/api/auth/config` call, `/b/:slug` bypasses `unwrap` entirely. CSRF is `OriginCSRFMiddleware` (Origin/Referer), so no header is needed on same-origin fetches.
- `schema.gen.ts` in sync: `scripts/gen_openapi_types.sh` run (no server, `create_app().openapi()`) → regenerated file byte-identical to HEAD (`DIFF_LINES=0`).
- XSS: `grep dangerouslySetInnerHTML src/` → 0 uses (comments only); QR path rendered as an attribute; React 19.2.7's production build blocks `javascript:` hrefs (`"React has blocked a javascript: URL as a security precaution"`), so unvalidated `call.url` is not a vector; markdown-it only in `scripts/gen_collab_goldens.mjs`, not in any bundle.
- Sort labels are right: with a profile the inbox orders by `aggregate_score desc` ("Best match"); Saved/Hidden by `decided_at desc` ("Recently decided") — `web/dashboard.py` ordering block.
- e2e match suite on the restored tree: 30/31 (the flaky one above); both corridor gates (1920x1080, 1080x1920) pass.

### Not checked
- Full-edition surfaces (cockpit, collab, editor, AppShell) — outside the match-edition scope; `ActivityBar`/`TitleBar` only via the bundle exclusion list.
- Accessibility beyond a READ smoke (icon buttons carry `aria-label`s — Search, Close preview, Move/Remove; account menu is label-in-name; no `Modal`/`Drawer` in match routes; strip label uses `htmlFor`; all triage actions are real `<button>`s) plus axe "critical" in the e2e — no screen-reader run.
- The live app, Ollama, the dev proxy, and backend behaviour beyond reading `auth.py`, `boards.py`, `dashboard.py`, `middleware.py`.
- The corridor probe used the e2e `LONG_BOARD` fixture (synthetic QR path); real QR geometry is the same 170px box.

### Numbers you measured
- `pnpm exec tsc --noEmit`: exit 0. `pnpm run lint` (oxlint `--deny-warnings src e2e`): exit 0.
- `pnpm exec vitest run`: **149 files, 1229 tests passed, 0 failed**, 21.75 s (full suite, pristine tree).
- `pnpm audit --prod`: 5 vulnerabilities — 4 high (nanoid ×2 via `ics`; `@tiptap/core` ReDoS; `prosemirror-view` XSS), 1 moderate (`@tiptap/core` mergeAttributes). Only `ics`/`nanoid` reach the match bundle.
- `check:bundle` clean: 522 modules / 71 app modules / 0 leaks; with the planted import: exit 1, 36 leaked.
- `pnpm run e2e:match`: 30 passed, 1 failed (46 s); the failing test alone ×6: 6 passed.
- Corridor probe: titles at h=0 — 0/8 (1920x1080), **8/8 (1366x768)**, 0/8 (1600x1000, but 8/8 clipped at 3.5 lines), 0/8 (2560x1440). `git status --short`: empty.

## Appendix A7 — auth, session, middleware, scheduler lock, SPA serving, CI/ops (reviewer report, verbatim)

## auth + ops — verdict in two sentences

The auth/session/CSRF/CSWSH/SPA-serving/scheduler-lock code holds up: every claim I attacked was pinned by a test that reddened under a mutant for the stated reason, and the OIDC/traversal/body-cap probes were all rejected. The ops side is broken: **master's CI has never been green, the Python `test` job has never executed on GitHub** (a bad action pin kills it at "Set up job"), 63 commits are unpushed, and the unrun gates are hiding 25 pip-audit advisories and 4 high pnpm advisories.

(Setup note: my worktree was handed to me at `6c0772df`, 63 commits behind; I fast-forwarded my own worktree branch to `c94f2de3` before starting. Python resolved to the worktree.)

### Findings, most severe first

**[HIGH] [RAN] CI's `test` job has never run on GitHub — the pinned `google/osv-scanner-action` SHA is a metadata-only stub**
`.github/workflows/ci.yml`, steps "OSV-Scanner (uv.lock)" and "OSV-Scanner (pnpm-lock.yaml)" — `uses: google/osv-scanner-action@764c918…`. The root `action.yml` at that SHA has `name/description/branding` and no `runs:` (the real composite action is in the `osv-scanner-action/` subdirectory, which does have `runs: using: docker`). GitHub rejects the whole job before step 1, so pytest, ruff, `uv lock --check`, `gen_no_build --check`, `gen_routes --check`, pip-audit, oxlint, vitest, the match-bundle gate, the OpenAPI drift check, `pnpm audit`, the SPA container build and the wheel-contains-SPA gate have **never** executed in CI. The pin entered on 2026-06-16 (`8df52a35`); master has 0 successful CI runs in its history (12/12 latest = failure).
```
annotations job 102432675238: "Top level 'runs:' section is required for
  google/osv-scanner-action/764c91816374ff2d8fc2095dab36eecd42d61638/action.yml"
JOB: test => failure   FAILED STEP: Set up job   (started 10:39:20, completed 10:39:21)
upstream osv-scanner-action/action.yml @764c918:  26:runs:  27:  using: "docker"
gh run list --branch master --workflow CI --status success --limit 3   -> (empty)
```
Fix: `uses: google/osv-scanner-action/osv-scanner-action@764c91816374ff2d8fc2095dab36eecd42d61638` at both sites; expect the job to then go red on the audit steps below.

**[HIGH] [RAN] 63 commits unpushed; the remote is 27 days stale**
```
git rev-list --left-right --count origin/master...master -> 0	63
git log -1 --format=%ci origin/master -> 2026-09-09 12:26:24 +0200 (6c0772df)
master tip                           -> 2026-10-06 14:33:11 +0200 (c94f2de3)
```
The eu_portal SEDIA rewrite, logprobs opt-in, ranking freshness, Sonnet 5.5 default and the 2026-09-08 review fixes exist only on this box. Fix: `git push origin master`.

**[HIGH] [RAN] pip-audit: 25 advisories in 5 packages; pnpm audit --prod: 4 high + 1 moderate — and both blocking CI steps have never run**
`uvx pip-audit -r <uv export>`:
```
anyio     4.13.0  PYSEC-2026-4024/4025      -> 4.14.2   (runtime: anthropic, claude-agent-sdk)
urllib3   2.7.0   PYSEC-2026-4175/4176/4177 -> 2.8.0    (runtime via trafilatura->courlan)
httpcore2 2.9.1   PYSEC-2026-3844           -> 2.10.0   (dev group, via httpx2)
httpx2    2.9.1   5x PYSEC-2026-384x        -> 2.12.0   (dev group; pyproject "httpx2>=2.9.1")
pyjwt     2.13.0  13x PYSEC + CVE-2026-102275 -> 2.15.0 (runtime, transitive claude-agent-sdk->mcp[crypto]; NOT used by auth — OIDC is joserfc)
pnpm audit --prod: high nanoid x2, high @tiptap/core ReDoS, high prosemirror-view XSS, moderate @tiptap/core __proto__
```
Fix: bump; CI's `pnpm audit --prod --audit-level=high` and `uv run pip-audit` are blocking once the job runs.

**[MED] [RAN] Dependency Review can never pass on this repo — every Dependabot PR is red by construction**
`.github/workflows/dependency-review.yml` uses `actions/dependency-review-action`, which requires the Dependency graph + GHAS on a **private** repo (`gh repo view` → PRIVATE).
```
annotation job 105856955716: "Dependency review is not supported on this repository.
  Please ensure that Dependency graph is enabled along with GitHub Advanced Security on private repositories"
```
Fix: enable Dependency graph (and GHAS if licensed) or remove the workflow and rely on pip-audit/OSV/pnpm-audit in the `test` job.

**[MED] [RAN] `match-e2e` fails on master and every Dependabot PR on the known corridor-board truncation — not a lockfile or cooldown problem**
master job 102432675122 and Dependabot job 105856955803 both: `Error: 3 of 8 titles are truncated on the wall` / `1 failed, 30 passed`. The `e2e (dev)`/`e2e (prod)` jobs on the same PRs installed with `--frozen-lockfile` and passed, so the lockfile matches and the 7-day `minimumReleaseAge` (pnpm-workspace.yaml: 10080) is not the cause. This is the OPEN 1366x768 CSS bug. Fix: the corridor-board CSS, or mark that spec's viewport case as known-failing until then.

**[MED] [READ] README quickstart `DATABASE_URL` names a role and database that `docker-compose.yml` never creates — step 4 (`alembic upgrade head`) fails as written**
`README.md` §3: `DATABASE_URL=postgresql+psycopg://grant_lab:grant_lab@localhost:5432/grant_lab`. `docker compose config` renders `POSTGRES_USER: call_manager`, `POSTGRES_DB: call_manager` (password `call_manager`); `.env.example` line 16 says `call_manager:CHANGE_ME@localhost:5432/call_manager`. CLAUDE.md states the role/DB stay `call_manager` deliberately. Fix: README line 85 → `call_manager:call_manager@localhost:5432/call_manager`.

**[MED] [RAN] `/healthz` still answers 200 after the scheduler lock is lost — the 2026-09-05 finding is not fixed; a test pins the 200**
`web/routes/health.py::healthz` returns `HealthOut(status="ok", scheduler_lock=False)` with 200. `tests/test_web_health.py::test_healthz_reports_a_missing_lock_without_failing` asserts `r.status_code == 200` and `r.json() == {"status": "ok", "scheduler_lock": False}` (ran, passed). Since 93a5ea26 the watchdog **stops** the scheduler on loss (proved below), so the process serves HTTP with zero scheduled work while any status-code monitor (systemd, uptime check — the endpoint's own docstring says "treat any non-200 as page operator") stays green. Fix: 503 when `scheduler_lock is False and not settings.scheduler_standby_ok`, or a separate `/readyz` that encodes it.

**[LOW] [RAN] Oversized chunked/multipart body without Content-Length → 500 "internal server error" plus an ASGI traceback, not 413 (memory stays bounded)**
`web/middleware.py::MaxBodySizeMiddleware.limited_receive` returns a synthetic `http.disconnect` when the running total passes the cap; Starlette raises `ClientDisconnect` inside the handler → catch-all `Exception` handler → 500 → ServerErrorMiddleware re-raises → uvicorn logs a traceback per request, unauthenticated. Probe (bare middleware, cap=1000):
```
1. Content-Length 2000 ->  413
2. chunked 5000 (no CL) ->  500 Internal Server Error   max body the handler ever saw: 0
3. chunked 900 under cap ->  200 {"received":900}
4. chunked multipart 5000 ->  500 Internal Server Error
5. two conflicting CL headers ->  400
```
Fix: wrap `send` to track `response_started`; when the cap trips before it, emit the 413 envelope via `_send_error` instead of a disconnect.

**[LOW] [RAN] `index.html` is served with no `Cache-Control` (ETag + Last-Modified only)**
`web/spa_serving.py::_spa_fallback` returns bare `FileResponse(index)` for every SPA route except `/b/*` and `/boards/N?preview=1` (`private, no-store`). Browsers heuristically cache it; after a deploy a cached shell references hashed assets that no longer exist. `test_index_html_does_not_have_immutable_cache_control` only asserts "not immutable". Probe: `GET / -> 200 cc='-'  etag/last-modified present: True True`. Fix: `headers={"Cache-Control": "no-cache"}` on the shell.

**[LOW] [RAN] Dotfiles under `/assets` are served, with the immutable header**
`GET /assets/.hidden -> 200 cc='public, max-age=31536000, immutable' body='DOTFILE-SECRET'`. Only exploitable if `package_spa.sh` ever copies a dotfile; Vite emits only hashed names. Fix: reject a leading `.` path segment in `_ImmutableStaticFiles.get_response`.

**[LOW] [READ] `uvicorn --workers 2` neither boots healthy nor refuses — the second worker crash-loops every ~5 s forever while the first serves**
uvicorn 0.44.0 `supervisors/multiprocess.py::Multiprocess.keep_subprocess_alive` (polled every 0.5 s, `is_alive(timeout=timeout_worker_healthcheck=5)`) restarts any dead child; `lifespan/on.py` sets `should_exit` on "Application startup failed" so the worker exits; the restart re-runs `probe_scheduler_lock` → `SingleInstanceViolation` → exit → restart. Each loop opens a DB connection and logs `scheduler_lock_held_elsewhere_fatal` at ERROR; the parent and `/healthz` (worker 1) look healthy. README's deploy command omits `--workers` (default 1) but never says it must stay 1. Fix: state `--workers 1` as a hard requirement in README; optionally sleep before raising to avoid a hot loop.

### Checked and sound (all RAN)
- **(a) logout epoch**: mutant `if user is not None:` in `web/api/auth.py::logout` → only `tests/test_api_auth.py::test_a_stale_cookie_cannot_log_out_a_newer_session` reddened: `AssertionError: a revoked cookie revoked the session that replaced it / assert 2 == 1` (26 others passed). Every cookie consumer checks the epoch: `auth/dependencies.py::get_current_user`, `collab/ws_auth.py::user_from_cookie` (`test_user_from_cookie_rejects_inactive_and_stale_epoch` passed), SSE turn stream via `get_current_user` + `turns.py::_identity_still_valid` mid-stream, OAuth link via `link_epoch` in `google_callback`. Grep: the only `signer.verify(` callers are those three plus `logout` — no signature-only path.
- **(b) OIDC**: scratchpad probe against `OIDCClient` with a mock JWKS — `alg=none` rejected ("Algorithm of 'none' is not allowed"), HS256 signed with the RSA public PEM rejected ("Algorithm of 'HS256' is not allowed"), `nbf`+1h rejected ("not yet valid"), `iat`+1h rejected, foreign-key/unknown-kid rejected with exactly **1** JWKS refetch, `aud` list without `azp` rejected; iss pinned to discovery issuer, nonce compared; `state` and PKCE verifier bound via the Fernet `oauth_txn` cookie and compared in `google_callback`. `at_hash` not validated — acceptable in code flow (token from the token endpoint over TLS, access token never used). Register squat: fixed in `auth/accounts.py::resolve_google_account` (pending + no `oidc_sub` + no `password_hash` → attach identity, stays pending); `test_web_oauth_google.py` A11-F1 section passed. Bootstrap: `admin_bootstrap.py::bootstrap_admins` creates only when no row exists and restores only when zero active admins remain, never touching password/epoch — a squatted pending row in that lockout case still has no credential (Google sign-in then collides). Residual is a lockout, not a hijack.
- **(c) watchdog wiring**: mutant replacing `start_scheduler_lock_watchdog(...)` with `None, None` in `main.py::lifespan` → only `tests/test_scheduler_single_instance.py::test_the_lifespan_actually_starts_the_watchdog` reddened: `the lifespan did not start the lock watchdog / assert 0 == 1` (18 others passed). **Second instance**: mutant `if False:` on the `held_elsewhere` branch → only `tests/test_lifespan_single_instance.py::test_a_second_instance_refuses_to_boot_by_default` reddened: `DID NOT RAISE SingleInstanceViolation` (4 others passed). Lock loss stops the scheduler: `test_a_lost_lock_stops_the_scheduler`, `test_the_watchdog_stops_the_scheduler_when_the_lock_dies` passed.
- **(d) CSRF/CSWSH/cookies**: `Origin: null` → 403 (`test_post_with_null_origin_is_rejected`); referrer-policy `same-origin`, not `no-referrer` (`test_security_headers_present_on_response`); `collab_ws` calls `check_origin` as step 0 before auth (`test_check_origin_matrix` passed); login/callback set `httponly=True, secure=settings.cookie_secure` (https `APP_BASE_URL`), `samesite="lax"`; `session_max_age_seconds` enforced server-side by `URLSafeTimedSerializer.loads(max_age=)` (`test_expired_cookie_rejected` passed).
- **(e) SPA prod**: probes `/assets/../index.html`, `%2e%2e/`, `..%2f`, `..\`, `//etc/passwd`, `../../../../etc/passwd` → all 404; symlinks to files outside the dir → 404; `/api/*`, `/api`, `/index.html`, `/edition.txt`, `/server-secret.txt` never receive the shell; hashed assets `public, max-age=31536000, immutable`; `POST /healthz` → 405 with Allow. Prod gate: mutant `if False:` on `index.exists()` in `mount_spa` → only `test_prod_mode_missing_build_fails_fast` reddened: `DID NOT RAISE RuntimeError`.
- **(f) rate limiters**: `web/client_ip.py::client_ip` honours `X-Forwarded-For` only when `TRUSTED_PROXY=true`, takes the Nth-from-right with `TRUSTED_PROXY_HOPS` (`ge=1`), 400 on too few entries (`test_trusted_proxy_*` passed); startup WARNING when https without a trusted proxy; eviction via hourly `_rate_limit_sweep` over email/ip/board limiters (`tests/test_scheduler.py -k rate_limit_sweep` → 3 passed, incl. `test_main_pys_rate_limit_sweep_covers_the_board_limiter`); denied requests never insert a key; email keys capped at 320 chars; `public_boards.py` uses `board_limiter` with `client_ip`.
- **(g) body cap**: Content-Length 2000 > 1000 → 413 carrying the full security-header set (`test_oversized_body_rejected_with_413`, `test_413_early_reject_carries_csp_and_hsts_over_https`); conflicting Content-Length → 400; chunked under cap → 200; chunked over cap → handler never sees more than the cap (finding above is about the status code only).
- **(j) compose/deploy docs**: Postgres published on `127.0.0.1:${HOST_DB_PORT:-5432}` only (confirmed by `docker compose config`); backups documented (README "Backup and retention": `pg_dump` + `WORKSPACES_DIR` + `.env`); `SESSION_SECRET` ≥16 (http) / ≥32 + no placeholder (https) matches README.

### Not checked
- `uvicorn --workers 2` live (needs a DB-backed uvicorn boot; READ of uvicorn 0.44.0 source only).
- `pnpm audit` ran from the lockfile without installing `node_modules` (absent; no install per instructions) — registry-based, complete for the lockfile.
- The Playwright `e2e` jobs locally; GZip/BREACH (no reflected secret in compressible bodies, not pursued); the 2026-09-01 review's SSRF/fencing items (unchanged since, not re-derived); internals of the 2026-08-31 joserfc swap beyond the probes above.

### Numbers you measured
- Baseline (clean tree): 16 files — `test_api_auth, test_scheduler_single_instance, test_lifespan_single_instance, test_spa_serving, test_web_health, test_auth_oidc, test_auth_csrf, collab/test_collab_ws_auth, test_client_ip, test_auth_rate_limit, test_security_headers, test_auth_session_cookie, test_web_oauth_google, test_auth_dependencies, test_api_session_revocation, test_auth_oauth_txn` → **142 passed, 0 failed** (32.65 s, `-n 4 --dist loadfile`).
- Mutants A+D: `test_api_auth.py` + `test_spa_serving.py` → 2 failed (exactly the two named), 26 passed. Mutant B: 3 files → 1 failed (watchdog wiring), 18 passed. Mutant C: `test_lifespan_single_instance.py` → 1 failed (second instance), 4 passed. Post-restore `test_scheduler.py -k rate_limit_sweep` → 3 passed.
- CI: master — 0 successful CI runs ever; 12/12 latest failed (`test` dies at Set up job; `match-e2e` 1/31 fails; `e2e dev/prod` pass). Dependabot 2026-09-19: 5 PRs × (CI fail + Dependency Review fail), same causes.
- Unpushed: **63** commits (`0	63`); last pushed commit **2026-09-09 12:26:24 +0200** (`6c0772df`).
- pip-audit: 25 advisories / 5 packages (anyio, urllib3, httpcore2, httpx2, pyjwt). pnpm audit --prod: 5 (4 high, 1 moderate).
- Worktree at `c94f2de3`. `git status --short`: empty.

## Appendix A8 — architecture, migrations, gate apparatus (reviewer report, verbatim)

## architecture + migrations — verdict in two sentences

The structural backlog the 2026-09-05 review named is half-closed and half-untouched: the match-edition **process cut is real and proven** (0 proposal-assist modules, no SDK/pycrdt at boot; the gate reddens to `8 == 0` under mutation), the suite no longer writes into `data/` (gate reddens when the patch is removed), a GitHub remote exists, and migrations round-trip cleanly with no model drift. But **three new package-level cycles entered green since 09-05** (7 now vs 4) because the only cycle gate is module-level, and the review's three "cheap moves" (`profile_access` out of `web/`, `LLMError` out of `ranking/`, `workspaces.py` into `proposal_assist/`) have zero delta.

**Note for the coordinator:** this worktree was created at `6c0772df`, 63 commits *behind* the required `c94f2de3`. I fast-forwarded it (`git merge --ff-only c94f2de3`, clean ancestor, clean tree) before measuring anything. Every number below is from `c94f2de3`.

### Findings, most severe first

**[MED] [RAN] Three new package-level cycles since 09-05; nothing gates the package layer**
`src/grant_lab/boards/queue.py` line 31 — `from grant_lab.web.dashboard import _decision_predicate`; `profiles/fingerprint.py:35` ↔ `embeddings/refresh.py:20`; `profiles/{bootstrap,bootstrap_worker,post_edit}.py` ↔ `ranking/{freshness,worker}.py:19,9`. Consequence: a scheduler-driven Call Match package (`boards`) imports a *private* helper from the HTTP layer, which itself imports `web.profile_access` — the same inversion §3 flagged for `digest→web`, now with an underscore name; and the match half's three domain packages are mutually entangled. `tests/architecture/test_no_import_cycles.py` is a **module-level** Tarjan gate (7 passed) and is correctly green — there is no package-layer gate, so all three entered unnoticed. Evidence (ast walk, statements, same method as 09-05):
```
TOTAL 2-cycles: 7     (09-05: 4)
  boards <-> web  (1 / 6)      boards/queue.py:31 -> grant_lab.web.dashboard
  embeddings <-> profiles (1/3) embeddings/refresh.py:20 -> profiles.fingerprint ; profiles/fingerprint.py:35 -> embeddings.similarity
  profiles <-> ranking (3/2)   profiles/bootstrap.py:12 -> ranking.ollama_client ; ranking/freshness.py:19 -> profiles.fingerprint
Cross-package imports of _private names (whole tree): 2
  boards/queue.py:31  boards <- web: ['_decision_predicate']
  web/api/annotations.py:32  web <- proposal_assist: ['_citation_snapshot']
```
Fix: move `_decision_predicate` + `profile_access` into a domain module (`profiles/`), make `fingerprint` the shared leaf both `embeddings` and `ranking` import, and add a `tests/architecture/` package-edge allowlist pinned at today's 7 (may only shrink).

*Coordinator note: the `boards/queue.py` import is deliberate — its module docstring says "This CALLS dashboard._decision_predicate. It does not copy it." — so the fix is a move, not a copy.*

**[MED] [RAN] The 09-05 §7-item-4 "three cheap moves" have zero delta**
`digest/assemble.py:23 → web.profile_access`, `digest/render.py:9 → web.filters`, `auth/csrf.py:41 → web.api.errors`, `embeddings/client.py:16` + `refresh.py:21 → ranking.ollama_client` (`LLMError`), `workspaces.py:29-39` (8 `proposal_assist` imports, back-edge `proposal_assist/branches.py:237` lazy). All four original cycles are byte-for-byte where the review left them. Consequence: Finding 1 is this backlog growing, not a new species. Fix: do them with Finding 1 in one slice; re-prove `test_profile_access_single_source.py` by mutation after the move.

**[LOW] [RAN] `scripts/gen_env_example.py --check` is not a check — it overwrites**
`scripts/gen_env_example.py` `__main__` block: no argv handling at all; `--check` is ignored and `.env.example` is rewritten. Harmless today (output identical; `git status` clean after the run), and the real staleness gate is `tests/test_env_example.py` (5 passed). But a CI step written as `--check` would pass while mutating the checkout. Evidence:
```
$ uv run python scripts/gen_env_example.py --check
wrote .env.example (117 settings)
```
Fix: copy `gen_no_build.py`'s `--check` branch or stop documenting the flag.

**[LOW] [RAN] Live `data/golden_set_history.jsonl` is 4 rows; the review recorded 1,090**
Main tree (read-only `ls`/`wc`): 500 bytes, 4 lines, mtime `2026-10-05 06:00:00` — the Monday `golden_eval:weekly` tick. The suite is no longer the writer (§4.6 is fixed, see below), so something else reset the eval history between 09-05 and 10-05. Outside my scope to explain; flagging for the golden-set owner. Fix: none from me — confirm it was deliberate.

**[LOW] [READ] Interval jobs rely on APScheduler's 1-second default misfire grace; no stacking is possible**
`src/grant_lab/scheduler.py` `build_scheduler`: no `job_defaults`. All **28** `add_job` calls set `max_instances=1, coalesce=True`; the 8 cron jobs set `misfire_grace_time` (6h; `digest:weekly` 24h); the 20 interval jobs set none. Consequence after a long pause: the coalesced missed run is *discarded*, not stacked, and the next interval fires on schedule — one tick of latency on `rank:drain`/`turn_drain`, zero overlap anywhere. Not a defect; recorded so no one re-derives it. `drafting_idle_sweep` is absent from the match scheduler (gate test `test_the_match_scheduler_registers_no_drafting_jobs` passed).

**[LOW] [READ] Two migrations since 0070 have documented lossy downgrades (none is `pass`)**
`alembic/versions/0071_call_decisions.py` `downgrade()` rebuilds `user_call_dismissals` from `hidden` rows only (drops `saved`, collapses the profile dimension — docstring says "NOT LOSSLESS"); `0078_board_auto_publish.py` `downgrade()` runs `DELETE FROM board_items WHERE approved_by IS NULL` and drops `board_suppressions` (header says "DOWNGRADE HAZARD"). All 12 downgrades 0070–0081 are real ops (summarised by ast: every `down` mirrors its `up`). Fix: a rollback runbook naming both; nothing in code.

### The numbers, re-measured

| metric | 2026-09-05 | today (`c94f2de3`) | delta |
|---|---|---|---|
| `src/grant_lab` LOC / files | 60,063 / 287 | 66,964 / 309 | +6,901 / +22 |
| `proposal_assist/` | 35,551 / 179 (59%) | 35,727 / 179 (53%) | +176 / 0 |
| `web/` | 9,027 / 33 | 11,597 / 35 | +2,570 / +2 |
| Call Match half (same 7 pkgs) | 8,419 (14%) | 9,965 (15%) | +1,546 |
| `boards/` (new) | — | 1,426 / 6 | — |
| Call Match half + boards | — | 11,391 (17%) | — |
| `db/` | 2,378 / 5 | 3,142 / 16 (`db/models/` split: 2,985 in 11) | +764 / +11 |
| `scheduler.py` / `config.py` / `main.py` / `workspaces.py` | 1,203 / 990 / 921 / 407 | 1,466 / 1,023 / 997 / 407 | +263 / +33 / +76 / 0 |
| `tests/` LOC / files | 123,916 / 585 | 144,930 / 643 | +21,014 / +58 |
| `frontend/src` (ts+tsx) | 47,541 / 294 | 60,199 / 329 | +12,658 / +35 |
| of which `schema.gen.ts` | 15,565 | 18,649 | +3,084 |
| `frontend/src` excl `schema.gen.ts` | 31,976 | 41,550 | +9,574 |
| `cockpit/`+`editor/`+`collab/` | 17,537 | 17,538 | +1 |
| `tests/fencing/` | 6,646 | 6,813 | +167 |
| fence/ports source (`fencing.py`+`ports/`+`port_impl/`) | 1,431 | 1,450 (912 + 538) | +19 |
| mapped tables | 45 | 48 | +3 |
| migration files / head | 70 / `0070` | 81 / `0081` (single) | +11 |
| `Field(` in config.py | 115 | 117 | +2 |
| `sched.add_job(` | 27 | 28 | +1 |
| `tests/architecture/` files | 12 | 16 `.py` (+1 `.txt`) | +4 |
| package-level 2-cycles | 4 | **7** | **+3** |
| `web → proposal_assist` edge (statements) | 98 | 100 | +2 |
| `proposal_assist → db` / `main → PA` / `scheduler → PA` / `workspaces → PA` | 82 / 25 / 19 / 8 | 82 / 25 / 19 / 8 | 0 |
| Call Match half → `proposal_assist` | none | **none** | 0 |
| PA sub-package 2-cycles / `citations` in | 14 / 8 | 14 / 8 | 0 |
| `ToolContext` fields | 29 | 29 | 0 |
| `_collect_tools` | 674 of 987 | 674 of 988 | 0 |
| pass runners | 4 (+ worker 427, bootstrap 171) | 4 (583/379/365/365) + `ranking/worker.py` **558** + 171 | worker +131 |
| evict ceremonies (`MANAGER.operate`) | 4 in 2 files | 4 in 2 files | 0 |
| `*Job` model classes | 9 | 9 | 0 |
| `app.state` lines from `web/` | 160 | 181 | +21 |
| match boot: PA modules / grant_lab modules | 142 / 237 | **0 / 105** | −142 / −132 |
| match boot: `claude_agent_sdk` / `pycrdt` / `prosemirror` | True / True / — | **False / False / False** | fixed |
| full boot: PA / grant_lab / routes | — | 132 / 250 / 142 (match 63) | — |
| armed allowlists (`CATEGORIES_BY_KIND`…`_SLICE_FOR_KIND`) | 4/3/2/1/16/5/2 (ARCH.md) | 4/3/2/1/16/5/2 | 0 |
| `git remote` | 0 | 1 (`origin` github.com/parlar/grant-lab) | closed |
| live `golden_set_history.jsonl` rows | 1,090 | 4 | see finding |
| test base | 5,029 passed + 1 skipped (run) | **5,796 collected** (`--collect-only`) | +766 |

### Checked and sound (all RAN)

- **Process-cut gate**: `tests/architecture/test_match_edition_process_cut.py` 7 passed (30.5s) unmutated; with `import grant_lab.proposal_assist.fencing` at module level in `main.py` → `2 failed, 5 passed`: `AssertionError: a match boot imported 8 proposal_assist modules … assert 8 == 0` (create_app) and `{'at_construction': 8, … 'proposal_assist': 8}` (through the lifespan). Restored via `cp`, status clean.
- **Boot probe** (same env as the gate): match → `{"proposal_assist": 0, "grant_lab": 105, "pycrdt": false, "claude_agent_sdk": false, "prosemirror": false, "routes": 63}`; full → `132 / 250 / true / true / true / 142`.
- **§4.6 golden-set write**: autouse `_isolate_golden_set_files` in `tests/conftest.py` patches both `_REPORT_PATH` and `_HISTORY_PATH`; 30 golden-set tests passed with no `data/` created in the worktree; removing the `_HISTORY_PATH` line → `tests/test_golden_set_isolation.py` FAILED: `_HISTORY_PATH resolves to …/data/golden_set_history.jsonl, inside the live data dir`. Live main-tree file byte-identical before/after (500 B, 4 lines, same mtime).
- **Alembic**: `alembic heads` → `0081 (head)` only. Throwaway `pgvector/pgvector:pg16` container: `upgrade head` → 49 tables/`0081`; `downgrade base` → 1 table/`None`; `upgrade head` → 49/`0081`; `alembic check` → "No new upgrade operations detected". `tests/test_schema_drift.py` 1 passed. Tests apply migrations via `command.upgrade(cfg, "head")` in `conftest.py::migrated_engine`.
- `uv run python scripts/gen_architecture.py` → `git diff --stat docs/ARCHITECTURE.md` empty. `tests/architecture/test_investigations_index.py` 3 passed. `uv lock --check` exit 0. `gen_no_build.py --check` "in sync". `ruff check src tests alembic scripts` clean. `tests/test_env_example.py` 5 passed. `test_no_import_cycles.py` 7 passed.
- Allowlist sizes in `tests/fencing/inventory.py` equal the ARCHITECTURE.md table exactly; `TOOLS_NOT_YET_CONVERTED` still lists the same 5 files (`audit.py`, …).
- All 28 scheduler jobs carry `max_instances=1, coalesce=True`.

### Not checked

- Full suite (forbidden); the coordinator has the run to compare against 5,796 collected (5795 passed + 1 skipped = 5796).
- Frontend gates (vitest/tsc/oxlint) and the e2e specs — other reviewers' scope.
- Why the live golden history dropped from 1,090 to 4 rows (read-only observation only; I did not touch the main tree).
- A mutation of the package-cycle count — there is no package-layer gate to mutate (that absence is Finding 1).
- The `web/api/annotations.py:32 ← proposal_assist._citation_snapshot` private import is inside the full edition's own web→PA edge and I did not assess it beyond listing it.

`git status --short`: empty.

## Appendix A9 — re-verification of every prior open finding (reviewer report, verbatim)

## prior-open re-verification — verdict in two sentences

Of the 94 prior-open items I re-verified on `c94f2de3` (worktree `agent-ac02f05ff46b15888`, moved from `6c0772df` to the required HEAD), **57 are FIXED, 7 PARTIAL, 26 STILL OPEN, 4 N/A** — and every one of the 09-02 review's Highs (the collab corruption trio, `published_at`, the four reference exhaustions, the ops safety net, CJK `Content-Disposition`, the register squat, SSE revocation, the forgeable fence, the 09-05 "present vs current" repair, and the user's three bridge data-loss reports) is closed by a probe or a test I ran today. What remains open is Medium and below: rubric `NaN` still accepted, three hollow gates (approve status guard proved by mutation, files MIME sniff, `base_branch` 422), the SSRF IP echo, the digest week-claim, a handful of `str(exc)` leaks and unbounded params, plus bookkeeping; the one carried High (sync Sessions on the loop) is down from 31 to 29 coroutines by the same AST rule, with both measured stall sites fixed.

Test runs today (all green, all testcontainers): collab/bridge/ws/branch-switch 457; references 32 + chunk-cap 3; web API/upsert/db-url/trigger/golden-set/schema-drift 85; single-instance 16; phase-0 restore + citation insert 22; admin-users 30 (under mutation); auth/profile-update/lifespan subset 5.

### The table

`RAN` = probe or test executed today; `READ` = code inspected only.

| # | finding (short) | reported in | prior | TODAY | label | evidence |
|---|---|---|---|---|---|---|
| 1 | Stale Y.Doc reconnect after branch switch merges old doc (`doc_epoch`) | 09-02 §2.1 / A6 F1 | OPEN | **FIXED** | RAN | `bbcde9ae` + mig `0069_collab_doc_epoch`; `collab_ws.py` refuses 4409 *before* STEP1 (`if client_epoch is not None and client_epoch != room.doc_epoch`); provider sends `?epoch=`. Tests ran: `test_collab_ws_route.py::test_every_accepted_connect_learns_the_epoch_before_step1`, `::test_a_socket_presenting_the_current_epoch_is_accepted_and_can_persist`, `::test_a_malformed_epoch_closes_1008_before_any_frame`, `test_branch_switch.py::test_switch_moves_the_document_epoch_of_BOTH_artifacts` |
| 2 | Mark mis-nesting widens bold/italic/link on checkpoint | §2.2 / A6 F2 | OPEN | **FIXED** | RAN | md→pm→ydoc→md probe: `**_Aim 1_: text**` → `***Aim 1*: text**` (em stays inside strong; was `***Aim 1: text***`); `**[Aim 1](https://x.se): text**` → byte-identical. `test_nested_marks_do_not_widen_through_yjs`, `test_every_two_mark_layout_survives_the_checkpoint_round_trip` |
| 3 | Empty blockquote serializes to `>` and bricks the artifact | §2.3 / A6 F3 | OPEN | **FIXED** | RAN | probe: `'## Aims\n\nintro\n\n>\n\nafter\n'` round-trips SAME on both paths, no raise; `test_empty_blockquote_is_readable_again` |
| 4 | Code block containing a ``` line swallows later sections | A6 F4 | OPEN | **FIXED** | RAN | probe SAME; `_longest_backtick_run`; `test_code_block_containing_a_fence_line_does_not_swallow_the_document` |
| 5 | Trailing hard break → literal `\`; hard break in heading splits section | A6 F5 | OPEN | **FIXED** | RAN | editor-shaped PM probe: trailing `hard_break` → `'## Aims\n\nline one\n\nnext para\n'`; heading → `'## Aim 1 subtitle\n\nbody\n'`, rehydrated heading text `['Aim 1 subtitle']`; tests at `test_markdown_bridge.py:764,789` |
| 6 | `create_workspace_branch` founds the branch on the last checkpoint, not the live doc | A6 F6 | OPEN | STILL OPEN | READ | `branches.py`: `checkpoint_artifact(` is called only inside `switch_branch`; create uses `start = "HEAD"` |
| 7 | Checkpoint commits with no round-trip self-check | A6 F7 | OPEN | **FIXED** | READ | `checkpoint.py` `_refuse_unreadable_projection` dry-runs `markdown_to_pm_or_empty(md)` before `write_and_commit_files`, raises `CheckpointProjectionUnreadable` |
| 8 | `published_at` dropped from upsert payload | §2.4 | OPEN | **FIXED** | RAN | `ingestion/upsert.py` `"published_at": _as_datetime(call.published_at)`; `test_ingestion_e2e.py` asserts the value read back |
| 9 | No cap on chunks before embedding | §2.5a / A4 F1 | OPEN | **FIXED** | RAN | `_MAX_REFERENCE_CHUNKS = 500` + `_cap_chunks` with a visible `[truncated]` chunk; `test_ingest_chunk_cap.py` 3 passed |
| 10 | Fetcher reads 4xx/5xx body uncapped | §2.5b / A4 F2 | OPEN | **FIXED** | RAN | `_ERROR_SNIPPET_BYTES = 4096`, `next(response.iter_bytes(_ERROR_SNIPPET_BYTES), b"")`; `test_error_status_reads_at_most_a_snippet_of_the_body` |
| 11 | `timeout_s` not a deadline (slow drip) | §2.5c / A4 F3 | OPEN (+hollow) | **FIXED** | RAN | deadline re-checked inside `for chunk in response.iter_bytes()`; `test_deadline_is_enforced_inside_the_body_read`, `test_per_read_timeout_is_capped_even_with_a_long_budget` (not re-mutated) |
| 12 | Sandbox bounds CPU not memory | §2.5d / A4 F6 | OPEN | **FIXED** | RAN | `extract_sandbox.py` `setrlimit(RLIMIT_AS…)`; `test_the_limit_bites_in_a_real_spawned_child` |
| 13 | No git remote; CI never ran | §2.6a / 09-05 §4.1 | OPEN | **FIXED** (remote) | RAN | `git remote -v` → `origin https://github.com/parlar/grant-lab.git`; `origin/master` = `6c0772df` (c94f2de3 itself not pushed). Whether a run went green: see A7 — never |
| 14 | Second process tolerated; only scheduler single-instance-safe | §2.6b / A9 F1 | OPEN | **FIXED** | RAN | `main.py` `scheduler_lock_held_elsewhere_fatal` unless `SCHEDULER_STANDBY_OK`; `test_lifespan_single_instance.py::test_a_second_instance_refuses_to_boot_by_default` passed |
| 15 | README `alembic upgrade head` fails as written | §2.6c / A9 F2 | OPEN | **FIXED** (mechanism; see A7 for the wrong role name) | RAN | throwaway `.env` (fake URL → port 1), `env -i … uv run alembic current` → `psycopg.OperationalError: connection to server at "127.0.0.1", port 1 failed: Connection refused` — URL came from `.env` via `db/url.py resolve_database_url → Settings().database_url` |
| 16 | Cutover script sets password to a literal | §2.6d | OPEN | **FIXED** | READ | `scripts/cutover_rename_database.sh` no longer exists |
| 17 | `.env.example` contradicts code, 57 aliases missing | §2.6e / A2 #11 / A9 F3-F4 | OPEN | **FIXED** | RAN | 117 aliases, 114 named, **3 missing** (`reference_ingest_concurrency`, `reference_max_batch_bytes`, `reference_max_batch_files` — alias-less fields); `SPA_MODE`, `ADMIN_DASHBOARD_URL`, `SESSION_MAX_AGE_SECONDS`, `COLLAB_KILL_SWITCH`, `SCHEDULER_STANDBY_OK` present; `INITIAL_ADMIN_PASSWORD` now "for a configured admin that does NOT have one yet" |
| 18 | Shutdown can block ~16 min | §2.6f / A9 F5 | OPEN | PARTIAL | READ | README `--timeout-graceful-shutdown 30`; watchdog stopped before `await to_thread(scheduler.shutdown, wait=True)`; subprocess-teardown-before-tick-wait ordering not verified |
| 19 | Nothing says what to back up | §2.6g / A9 F7 | OPEN | **FIXED** (docs) | READ | README `### Backup and retention` names the three state holders; pruning remains manual by design |
| 20 | `app.log` 32 MB/day, no `LOG_LEVEL` | §2.6h | OPEN | **FIXED** | READ | `config.py` `log_level` + validator; `logging.py` `QUIET_LOGGERS = ("apscheduler",)` |
| 21 | 31 `async def`s build a sync Session on the loop; embed + controller pre-await | §2.7 / A1 #1 / 09-05 ro #4 | OPEN (High) | PARTIAL | RAN | same AST rule today: **49 call sites / 29 distinct coroutines** (was 31). The two measured stall sites are gone: `tools/references.py` `await asyncio.to_thread(embedder.embed, query)`; `drafting/controller.py` lock-session via `to_thread` (comment cites §2.7). Remainder = pass runners/tool impls doing short DB calls |
| 22 | `Content-Disposition` 500 on CJK/en-dash/emoji | §2.7 / A1 #8 | OPEN | **FIXED** | RAN | probe through `Response.raw_headers`: `研究` → `attachment; filename="__.md"; filename*=UTF-8''%E7%A0%94%E7%A9%B6.md`; `Aim – 1`, `Study 🚀`, `研究.pdf`, `rapport–v2.pdf` all OK (`web/http_headers.py content_disposition`) |
| 23 | Delete vs finalize lock order deadlock (40P01) | §3 / A10 F2 | OPEN | **FIXED** | READ | `turns/processor.py _finalize_success` takes `Workspace.id … with_for_update(key_share=True)` first; `workspaces.py` comment: "finalize takes the WORKSPACE row first … (W then J): before review 2026-09-02 A10 F2 it took J then W" |
| 24 | Phase-0 worker writes `_m2/_m3` into generation G+2 | §3 / A10 F1 | OPEN | **FIXED** | RAN | `phases/setup.py` `_m2(…, claimed_generation=)`, `_m3(…, claimed_generation=)`, `_refuse_stale_claim`; `test_phase0_restore_fence.py` passed |
| 25 | `_insert_citation_locked` fence has no behavioural pin | §3 / A10 F3 | OPEN | PARTIAL | READ | now listed in `tests/fencing/live_mutex_gate.py` ("stays pinned with a §5.2 runtime proof"); not re-mutated today |
| 26 | `fence_inventory.json` says `run_review_pass` unfenced | §3 / A10 F4 | OPEN | STILL OPEN (bookkeeping) | READ | inventory still `"symbol": "run_review_pass", "fenced": false`; runtime-pinned by `live_mutex_gate.py` |
| 27 | 3 live indexes absent from ORM; no drift gate | §3 / A2 #7 | OPEN | **FIXED** | RAN | `tests/test_schema_drift.py` passed |
| 28 | `reference_id` FKs unindexed | §3 / A1 #28 | OPEN | STILL OPEN | READ | no `ix_*_reference_id` in `db/models/` or `alembic/versions` |
| 29 | Every EU tick re-enqueues corpus × profiles | §3 / A5 F3 | OPEN | **FIXED** | READ | orchestrator Step 3 enqueues only STALE pairs via `ranking/freshness.py` fingerprints (mig `0081`); `eu_portal.py` documents `since` as informational |
| 30 | Digest week-claim only flushed; post-accept failure re-sends | §3 / A5 F4 | OPEN | STILL OPEN | READ | `digest/send.py`: `DigestRun` flushed, send, commit; `EmailTransportError` (incl. `client.py` "resend 200 with non-JSON body") → `session.rollback()` releases the claim. Test contract states it |
| 31 | Ranking fence forgeable (`CALL_BODY>>>`) | §3 / A1 #4 | OPEN | **FIXED** | RAN | probe: `opening markers=1 closing markers=1`, injected text inside fence `True`, `_neutralise_fence` → `CALL-BODY` |
| 32 | Near-miss Gemma reply retried 5× then dead-lettered | A5 F5 (Low) | OPEN | STILL OPEN (Low) | READ | `parse_response` → `json.loads(content)` with no fence stripping; mitigated by `response_format json_object` |
| 33 | Blank-description edit skips backfill | A5 F6 / gate-audit | OPEN | **FIXED** | RAN | `refresh.py` "Hole 4"; `test_update_areas_only_change_triggers_reembed` passed |
| 34 | Fetch failures reach user as "internal server error" | A3 F1 | OPEN | STILL OPEN | READ | `references.py` `HTTPException(502, f"fetch failed: {e}")`; `main.py` rewrites every ≥500 to `"internal server error"` |
| 35 | `NaN`/`Infinity` rubric weights pass validation | A3 F2 | OPEN | STILL OPEN | RAN | `_validate_criteria([CriterionIn(name='c1', description='d', weight=nan, scale_max=5)])` → `ACCEPTED weight=nan`; `inf` → `ACCEPTED`; no `allow_inf_nan`/`isfinite` in `rubric.py` |
| 36 | SSRF refusal echoes resolved private IP | A3 F3 | OPEN | STILL OPEN | READ | `fetcher.py SSRFError(f"refused to connect to {host!r} → private IP {addr!r}")` → `references.py` 400 `str(e)` |
| 37 | `add_member`: registration oracle, admits pending/disabled, case-sensitive | A3 F4 / A1 #13 | OPEN | STILL OPEN | READ | `settings.py` `User.email == body.email` (no `.lower()`), no status check, generic 400 |
| 38 | `get_spec` serves locker's email to every member | A3 F7 | OPEN | STILL OPEN | READ | `_locker_email` returns `u.email` into `locked_by` |
| 39 | `create_workspace_api` accepts an archived profile | A3 F8 | OPEN | **N/A** | READ | `grep archived db/models/profiles.py` → nothing; profiles no longer carry an archived state |
| 40 | `dismiss_uncited_claim` stores arbitrary path segment | A3 F9 | OPEN | STILL OPEN | READ | `claim_hash: str` path param, unbounded, stored in `WorkspaceEvent.payload` |
| 41 | `profile_ids` unbounded → 500 | A3 F5 | OPEN | STILL OPEN | READ | `me.py` `profile_ids: list[int]` no `max_length` |
| 42 | `list_turns` unbounded | A1 #9 | OPEN | **FIXED** | RAN | `turns.py` `limit = Query(_TURNS_PAGE_DEFAULT, ge=1, le=_TURNS_PAGE_MAX)`, `has_more`/`next_before`; `test_api_turns.py` |
| 43 | SSE stream outlives logout/disable/removal | A1 #5 | OPEN | **FIXED** | RAN | `_identity_still_valid` (status/epoch/membership) re-checked in `_json_turn_stream`; `test_turn_stream_ends_when_the_session_is_revoked_mid_stream`, `…account_is_disabled_mid_stream` |
| 44 | Activity feed discloses emails | A1 #14 | OPEN | STILL OPEN | READ | `activity.py` maps actor ids → `User.email` |
| 45 | Login limiter consumes a slot on success | A1 #15 | OPEN | STILL OPEN | READ | `auth.py` both `allow()` before `verify_password`; no refund |
| 46 | Logout accepts epoch-stale cookie | A1 #16 | OPEN | **FIXED** | RAN | `test_api_auth.py::test_a_stale_cookie_cannot_log_out_a_newer_session` passed |
| 47 | `str(exc)` in `last_error`, export 409, references 400 | A1 #6/#11 | OPEN | STILL OPEN | READ | `pipeline.py` `error=str(exc)`; `export.py` `f"…: {exc}"`; `references.py` `str(e)` ×3 |
| 48 | 24/114 `/api` ops outside the error envelope | A1 #23 | OPEN | **FIXED** | READ | `web/api/__init__.py` builds one `APIRouter(responses={code: ApiError…})` and `include_router`s both `_ALWAYS` and `_full_only_routers()` under it |
| 49 | Reprocess re-keys and de-approves a reference | A4 F4 | OPEN | STILL OPEN | READ | `grep -c approved ingest.py` → **0**; Phase 3 always `ref3.citekey = generate_citekey(...)` |
| 50 | Deep citation check pins a connection idle-in-transaction across LLM calls | A4 F5 | OPEN | STILL OPEN | READ | `citations/audit.py validate_citations_semantic(session, session_factory, …)`: `_load_chunk(session…)`, `get_verdict(session…)` then `citation_service.complete_json(...)` in the same loop with `session` open |
| 51 | `text_error` leaks internal Ollama URL | A4 F8 | OPEN | STILL OPEN | READ | `ingest.py msg = f"{type(e).__name__}: {e}"` persisted unscrubbed |
| 52 | Docstring promises CGNAT refused; code does not | A4 F7 | OPEN | STILL OPEN | RAN | `_is_private_ip("100.64.0.1") = False`, `("100.127.255.254") = False`; docstring still lists "CGNAT" |
| 53 | Another member's branch switch leaves cockpit caches on old branch | A7 F1 | OPEN | **FIXED** | READ | `useCollabDoc.ts` invalidates `branches/draft/spec/versions` on 4409 |
| 54 | SSE 401 classification thrown away | A7 F2 | OPEN | **FIXED** | READ | `cockpit/chat/CockpitChat.tsx` passes `onExpired`/`onNoAccess` |
| 55 | Cockpit "Loading…" forever on meta error | A7 F3 | OPEN | **FIXED** | READ | `routes/Cockpit.tsx` `if (meta.isError)` arm |
| 56 | Create workspace/profile never invalidates lists | A7 F4 | OPEN | **FIXED** | READ | `api/profiles.ts` (`profiles`,`calls`), `api/workspace.ts` (`workspaces`) |
| 57 | BranchBar swallows switch/delete failures | A7 F5 | OPEN | **FIXED** | READ | `BranchBar.tsx` `{switchBranch.isError && …}` |
| 58 | Placeholder "retrying" / "Read-only" lies | A7 F6 | OPEN | **FIXED** | READ | `CollabEditor.tsx` splits `offline` (retrying) from `offline-permanent` |
| 59 | A7 lows: dead key, `onSave` raw buffer, a11y, bubble test | A7 F7-F10 | OPEN | STILL OPEN (dead key `annotations.ts` `["proposals", id]`; `ArtifactSectionEditor.tsx` `text: content`) / PARTIAL (9 `aria-pressed`) / bubble not checked | READ | — |
| 60 | Register squat blocks Google sign-in; no release | A11 F1 | OPEN | **FIXED** | RAN | `auth/accounts.py` attaches the verified Google identity to a credential-less pending row; `POST /admin/users/{id}/release`. Tests: `test_verified_google_identity_attaches_to_an_unverified_pending_row`, `test_a_pending_row_that_already_has_a_credential_is_still_a_collision`, `test_release_deletes_a_pending_row`, `test_release_refuses_anything_but_a_pending_row`, `test_release_is_admin_only` |
| 61 | Audit tools: per-sentence sync LLM calls on the loop, uncapped | A11 F2 | OPEN | **FIXED** | READ | `tools/audit.py` uses `validate_citations_semantic_capped(max_fresh_calls=_MAX_FRESH_VALIDATION_CALLS)` and `asyncio.to_thread` |
| 62 | Oversized `page` overflows int8 OFFSET | A11 F3 | OPEN | STILL OPEN | READ | `calls.py` `page: int = Query(default=1, ge=1)` (no `le`); `admin_users.py` `page: int = 1` |
| 63 | `int(args.get(…))` outside `_safe_tool` | A11 F6 | OPEN | STILL OPEN | READ | `registry.py` ×4 — the `int()` is an argument expression evaluated before `_safe_tool` runs |
| 64 | Hollow: `test_approve_rejects_non_pending` never reaches its guard | A11 F4 | OPEN | STILL OPEN | RAN | mutation: removed approve's `if u.status != "pending": raise 400` → `tests/test_api_admin_users.py` **30 passed**; restored, tree clean |
| 65 | Hollow: declared-MIME-equals-sniffed has no pin | A11 F5 | OPEN | STILL OPEN | READ | only `test_api_files_upload_bad_mime_is_400` (`application/pdf`), refused by the `_ALLOWED_MIMES` allowlist before the sniff compare; no jpeg/gif-mismatch test |
| 66 | Hollow: `propose_citation` foreign-workspace refusal unpinned | A11 F7 / A3 F6 | OPEN | **FIXED** | RAN | `tools/test_citations_insert.py::test_reference_in_another_workspace_not_found` passed |
| 67 | Disarmed frame cap shows as a hang | A8 F1 | OPEN | **FIXED** | READ | `pyproject.toml` `pytest-timeout`, `timeout = 120` (comment cites A6 F1) |
| 68 | Four 2xx-only / not-None tests | A8 F2-F5 | OPEN | **FIXED** | READ | `test_reset_password_has_no_status_gate` (A6 F2 side-effects), `test_enable_only_disabled` asserts status+epoch, `test_build_scheduler_includes_turn_reaper_job` (A6 F4), adapter wiring (A6 F5) |
| 69 | `PytestUnraisableExceptionWarning` bypasses the filter | A1 #7 | OPEN | STILL OPEN | READ | `filterwarnings = ["error::ResourceWarning"]` only |
| 70 | Silent scheduler-lock loss | §6 / A1 #2 / A2 #9 | OPEN | **FIXED** | RAN | `main.py` `start_scheduler_lock_watchdog`; `test_scheduler_single_instance.py` passed. (`/healthz`-after-loss: see A7, still open) |
| 71 | `WORKSPACES_DIR` unvalidated, CWD-relative | §6 / A1 #26 / A2 #10 | OPEN | STILL OPEN | READ | `config.py` default `var/workspaces`, no validator; `.env.example` ships the relative value |
| 72 | `trigger_job_now` lies on a lockless replica | §6 / A1 #25 | OPEN | **FIXED** | RAN | never-started `BackgroundScheduler`: `running=False trigger_job_now -> False job ran: False`; `test_trigger_job_now_false_when_scheduler_is_not_running` |
| 73 | Secret-scrub list incomplete | §6 / A1 #19 | OPEN | STILL OPEN | READ | `_SCRUBBED_ENV_KEYS` = 6 keys, no `INITIAL_ADMIN_PASSWORD`/`GOOGLE_CLIENT_SECRET`; still a denylist merged over inherited env (`claude_adapter.py`) |
| 74 | `_current_head` without timeout | §6 / A1 #22 | OPEN | STILL OPEN | READ | `citations/core.py` `subprocess.run(["git","rev-parse","HEAD"], …)` no `timeout=` |
| 75 | WS TEXT frame → `KeyError('bytes')` | §6 / A1 #27 | OPEN | STILL OPEN | READ | `collab_ws.py` `async for message in websocket.iter_bytes()`; Starlette 1.3.1 `receive_bytes` → `message["bytes"]`; route catches only `WebSocketDisconnect`/`PersistError` |
| 76 | No frontend lint gate | §6 / A2 #5 | OPEN | **FIXED** | READ | `package.json "lint": "oxlint --deny-warnings src e2e"` (oxlint 1.80.0); CI step "Frontend lint (oxlint)" |
| 77 | No `alembic check`; no boot revision check | §6 / A2 #8 | OPEN | PARTIAL | RAN | `tests/test_schema_drift.py` passed; boot-time revision check still absent (`grep alembic_version\|ScriptDirectory src/` → none) |
| 78 | e2e specs unwired; cockpit.spec inert; no password user | A2 #6 | OPEN | **FIXED** | READ | `ci.yml` playwright steps; `frontend/e2e/` = `collab, dashboard, match` (cockpit stub deleted); `collab_e2e_seed.py` seeds the password user |
| 79 | `base_branch` 422 path has no test | A2 #1 / §6 | PARTIAL | STILL OPEN | READ | no test posts a section save without `base_branch` and asserts 422 (`grep 422` in the save/spec/workspaces tests → citation cases only) |
| 80 | Post-save optimistic write reverts canonicalization | A2 #3 | OPEN | STILL OPEN | READ | `ArtifactSectionEditor.tsx` `setQueryData(… text: content, sha: result.section_sha)` |
| 81 | Problems panel hides failed signals | A2 #13 | PARTIAL | STILL OPEN | READ | `problems/aggregate.py` `logger.exception` + swallow; no failed-signal field |
| 82 | Compaction input unbounded | A1 #18 | PARTIAL | STILL OPEN | READ | no truncation/cap in `turns/compaction.py` (only the daily token cap) |
| 83 | `init_spec_via_kernel` untested | A1 #24 | OPEN | STILL OPEN | READ | `grep -rln init_spec_via_kernel tests` → none |
| 84 | Tracked `.orig` files | A2 #12 / A6 F8 | OPEN | **FIXED** | RAN | `git ls-files \| grep -c '\.orig$'` → 0 |
| 85 | Suite pollutes `data/golden_set_history.jsonl` (disposition only) | 09-05 §4.6 / notes 09-03 | OPEN | **FIXED** | RAN | `tests/conftest.py` autouse monkeypatch of `eval_mod._HISTORY_PATH`; `tests/test_golden_set_isolation.py` passed |
| 86 | `seed_dev_data.py` no environment guard | A9 F8 | OPEN | STILL OPEN | READ | no `APP_ENV`/guard in the script |
| 87 | uvicorn access log records OAuth code | A9 F9 | OPEN | STILL OPEN | READ | no `access-log` mention in README or `main.py` |
| 88 | Misspelled config keys silently ignored | A9 F10 | OPEN | STILL OPEN | READ | `config.py` `extra="ignore"` |
| 89 | Match edition is a surface cut | 09-05 §4.2 / ro #3 | OPEN | **FIXED** | READ | `tests/architecture/test_match_edition_process_cut.py` (`57b8ecfd`); `web/api/__init__.py` defers `_full_only_routers()` — mutation-proved in A8 |
| 90 | Public board: first unauthenticated read of ranked data | 09-05 §5 | watch | ADDRESSED | READ | `test_auth_allowlist.py` `("GET", "/api/public/boards/{slug}")` is an explicit entry; `public_boards.router` in `_ALWAYS` under the "imports nothing from proposal_assist" invariant the process-cut gate measures |
| 91 | Notify-on-change has no substrate | 09-05 §5 | watch | N/A (deferred by design) | READ | `db/models/calls.py` carries `embedding_fingerprint` only — no `updated_at`/content hash; spec sequences it last |
| 92 | Triage decisions migrate live rows | 09-05 §5 | watch | DONE | READ | `alembic/versions/0071_call_decisions.py` |
| 93 | `APP_EDITION` must agree in three places | 09-05 §5 | watch | HOLDS | READ | 4 readers (`main.py`, `config.py`, `spa_serving.py`, `web/api/__init__.py`), all via `settings.app_edition`; `spa_serving.py` refuses disagreement |
| 94 | Embedding repair cannot tell present from current | 09-05 runtime-ownership #2 | CONFIRMED open | **FIXED** (profiles 0075 + calls 0080) | RAN | compiled SQL below; both halves select `embedding_fingerprint` and decide in Python with `… or r.embedding_fingerprint != _digest_for_call_row(r)` / `!= _digest_for_row(r)` |
| 95 | Bridge data loss: `<TP53>` deleted, `sample_id`→`sample\_id`, citation marks lost | user notes | live DATA LOSS | **FIXED** | RAN | round-trip probe (direct and via Y.Doc, plus 2nd pass): `<TP53> and <BRCA1>` SAME/STABLE; `sample_id, patient_age and n_reads` SAME/STABLE; `[@smith2021]` plain, inside bold, and adjacent `[@a2020][@b2021]` all SAME/STABLE. Pinned by `test_markdown_bridge.py` (the two literals), `test_citation_marker_survives_roundtrip`, `test_adjacent_citations_survive`, `test_a_bold_citation_does_not_corrupt_its_own_paragraph` |

Compiled repair-tick SQL (item 94), captured from `_refresh_missing_embeddings_batch` through a fake session:
```
SELECT calls.id, calls.title, calls.summary, calls.full_text, calls.embedding_fingerprint, calls.embedding_model, calls.embedding IS NULL AS embedding_is_null FROM calls WHERE calls.id > 0 ORDER BY calls.id LIMIT 200
SELECT profiles.id, profiles.description, profiles.structured, profiles.embedding_model, profiles.embedding_fingerprint, profiles.embedding IS NULL AS embedding_is_null FROM profiles WHERE profiles.id > 0 ORDER BY profiles.id LIMIT 200
```
followed in Python by `if (r.embedding_is_null or r.embedding_model != current_model or r.embedding_fingerprint != _digest_for_call_row(r))` and the profile twin — "present but stale" rows are now selected.

### Still-open items that are HIGH, with the evidence pasted in full

**None of the prior Highs remains open at High.** The closest is item 21, which the 09-02 review carried as High by *count*; the two sites whose stall it measured are fixed, and what the rule still catches is short DB work. Evidence (same rule as A1 #1, run today):

```
$ uv run python probe_async_sessions.py $W
call sites: 49; distinct async defs: 29
   src/grant_lab/main.py:165 lifespan
   src/grant_lab/proposal_assist/citations/core.py:331 insert_citation_impl
   src/grant_lab/proposal_assist/coherence/pass_runner.py:{52,67,123,153,238,352}
   src/grant_lab/proposal_assist/evaluator/pass_runner.py:{81,96,161,191,274,352}
   src/grant_lab/proposal_assist/review/pass_runner.py:{96,156,195,336,365,434,451,465,557}
   src/grant_lab/proposal_assist/tools/audit.py:242 build_bibliography_impl
   src/grant_lab/proposal_assist/tools/citations.py:182 propose_citation_impl
   src/grant_lab/proposal_assist/tools/proposal.py:78 _read_artifact_impl
   src/grant_lab/proposal_assist/tools/references.py:{57,129,184,248,363}
   src/grant_lab/proposal_assist/turns/drain.py:57 ; turns/processor.py:{225,271,315,336}
   src/grant_lab/proposal_assist/uncited_claims/pass_runner.py:{116,131,186,213,294,366}
   src/grant_lab/web/api/files.py:{86,118} ; web/api/references.py:{99,212,548} ; web/api/turns.py:{354,367}
```
The two named harms: `tools/references.py` `query_vec = (await asyncio.to_thread(embedder.embed, query)).vector` and `drafting/controller.py` `lock_session, resume_session_id = await asyncio.to_thread(…)` (comment: "await (review 2026-09-02 §2.7), freezing every other request").

The Medium-with-RAN-proof items worth lifting out: **#35** (NaN/Inf weights accepted) and **#64** (approve status guard: 30/30 green with the guard deleted).

### New things I noticed on the way

**[LOW] [RAN] `resolve_database_url` reports every `Settings()` failure as "No database URL"** — `src/grant_lab/db/url.py` `resolve_database_url`. Scenario: an operator's `.env` has `DATABASE_URL` but lacks another required key; the README migration step tells them to set a URL they already set. Evidence (throwaway `.env` with only `DATABASE_URL` + `SESSION_SECRET`, nothing exported):
```
APP_BASE_URL
  Field required [type=missing, input_value={'DATABASE_URL': 'postgre...'}, input_type=dict]
The above exception was the direct cause of the following exception:
RuntimeError: No database URL: set DATABASE_URL in the environment, put it in .env (...)
```
then the same with `ALLOWED_EMAIL_DOMAINS`; only with all three present did it reach the DB (`connection refused`, port 1). The `except Exception … raise RuntimeError("No database URL…") from exc` in `url.py` is the site.

**[LOW] [READ] `safe_title` strips every non-ASCII-alnum to `_`, so the RFC 5987 fallback name is `__.md` for a pure-CJK title** — `proposal_assist/export.py safe_title` + `web/http_headers.py content_disposition`. Not a defect (the `filename*=` carries the real name) but old clients that ignore `filename*` get an unreadable name.

**[INFO] [READ] `fence_inventory.json` and `live_mutex_gate.py` disagree on purpose** — item 26: the inventory records the static checker's verdict (`fenced:false` because the acquisition sits in a `try:`), the live gate records the runtime proof. A reader of only the inventory still concludes `run_review_pass` is unfenced.

### Not checked

- `/healthz` after advisory-lock loss (A7 owns it).
- The golden-set pollution *measurement* (only its disposition recorded, item 85; A8 measured it).
- Mutation re-proof of items 11 (`timeout=None` shape-4) and 25 (`_insert_citation_locked` raw-mutex swap) — both now have named pins; not re-mutated today.
- Item 18's subprocess-teardown-vs-tick-wait ordering; item 59's bubble-menu `shouldShow` test; the `cockpit` optimistic-write vitest; whether a CI run on `origin` has ever gone green (A7: never).
- The §3 "24/114 envelope" exact count — I verified the mounting structure (all routers under the one `responses=` router), not a re-count.
- Live DB counts of any kind (hard rule).

Scratch files lived only in the session scratchpad; the throwaway `.env` was removed; the one mutation (`admin_users.py`) was restored from its `.bak`. `git status --short`: empty; HEAD `c94f2de3`.

