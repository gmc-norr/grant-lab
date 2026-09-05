# Call Match Release 1 — Plan B: triage decisions and match reasons

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 3 — Phase 2, 2026-09-05.** Phase 1 executed revision 2 of this plan in a
throwaway worktree (`.worktrees/call-match-r1b-dry`, branch `dry/call-match-r1b`, tip
`49969f63`, 24 commits) purely to find where the plan was wrong, and recorded **113
defects** in `/home/parlar_ai/call-match-r1b-run/PLAN-DEFECTS.md` (7 Critical, 50
Important, 47 Minor, 10 Decision). This revision folds them in. Every defect has a
one-line disposition in that file's *Phase 2 disposition* section. **Structural
changes:** Tasks 1 and 2 are ONE task (the two-commit split produced a fully red
commit, F-8); a *Decided unattended in Phase 2* section records nineteen product and
design calls the plan had left to nobody; every gate the dry run found hollow is
rewritten with the exact assertion and the real red output beside it; every verify
block that could report success over a red tree now runs the suite that goes red.
Where a gate is new and its green side could not be run (the code does not exist
yet), the green side is labelled `PREDICTED (not run)` and its red side was run
against the dry tree, which is the known-bad input.

**Goal:** Give the `match` edition the other half of the triage pair. Today a user
can only push a call away, globally, for every profile at once. After this plan a
decision is **per profile**, has two directions (**Saved** / **Hidden**), and the
default view is an **inbox that ends**. The list stops showing a raw score and
starts showing named reasons quoted from the call itself.

**Architecture:** One new table, `call_decisions`, replaces `user_call_dismissals`
in a single migration, in a single commit with every consumer. The three existing
hide endpoints re-point at it; a Saved half is added beside them. The list schema
(`CallOut`) grows the fields the expanded card needs, plus one batched criteria query
— **not** a per-row query. The match edition mounts a new `TriageFeed` at its index
route; the full edition keeps `Dashboard` and `MatchBadge` untouched, so the two
editions diverge by **which component the router mounts**, not by a runtime branch
inside a shared one.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL 16;
React 19 + Vite + react-router-dom + Mantine; pytest (+ testcontainers Postgres);
vitest + jsdom; Playwright.

---

## Global Constraints

Copied from `CLAUDE.md`, from the spec, and from what the Plan A run and the Phase 1
dry run measured. Every task's requirements implicitly include this section.

- **Test command:** `uv run pytest -n 16 --dist loadfile` (~120–130 s measured in the
  dry worktree). **Never `-n auto`** — `psutil` is absent, so xdist resolves it to
  72 workers.
- **The `1 skipped` is the signal, not the total.** It is a real-SDK test gated on
  `ANTHROPIC_API_KEY`. `0 skipped` means a live Anthropic API call just ran — stop
  and record it. **Do not export `ANTHROPIC_API_KEY`.**
- **Measured baselines on the branch base `dcd4ddbd` (= master `ed1153a3` merged),
  not predictions.** Phase 1 measured the first row; Phase 2 re-measured the second
  on 2026-09-05; the frontend rows are from the dry run's baseline logs
  (`/home/parlar_ai/call-match-r1b-run/dry-baseline-vitest.log`, `dry-baseline-bundle.log`).

  | Suite | Command | Baseline at `dcd4ddbd` |
  | --- | --- | --- |
  | Backend | `uv run pytest -n 16 --dist loadfile` | `5029 passed, 1 skipped` |
  | Architecture subset | `uv run pytest tests/architecture -q` | `112 passed` |
  | Frontend | `pnpm -C frontend test` | `Test Files 136 passed`, `Tests 973 passed` |
  | Match bundle | `pnpm -C frontend build:match && pnpm -C frontend check:bundle` | `396 modules, 0 under 14 excluded paths, 48 app modules all under 5 declared areas` |
  | Browser smoke | `pnpm -C frontend e2e:match` | `15 passed` (after Task 8's fixture fix in the dry run; **12 passed / 2 failed at the dry run's Task 7 tip** — see Task 7) |
  | Route manifest | `tests/architecture/full_edition_routes.txt` | 107 lines |
  | Migrations | `ls alembic/versions \| wc -l` | 70, head `0070_profile_visibility` |

  For order of magnitude only, the dry run's **final** tree `49969f63` measured
  `5107 passed, 1 skipped`; architecture `132 passed`; vitest `140 files / 1034
  tests`; bundle `397 / 0 / 49`; e2e `23 passed`; manifest 109. **This revision's
  gate set is not the dry run's** (Tasks 1+2 merged, six gates added, the dedup
  gates removed), so those totals are a shape, not an expectation. Each task states
  its own **delta**. Re-measure the base yourself before asserting a total. **Never
  adjust an expected number to make the arithmetic close** — find the term that
  changed and name it.
- **Cite by anchor string, never by line number.** Revision 2 cited ~13 line
  numbers that had rotted after `ed1153a3`, and the dry run's own corrections
  rotted by 2–4 lines within a day (PF-4, T4-7). Every citation in this revision is
  a function name, a string literal, or a comment fragment. `grep -n` it yourself.
- **Frontend tests:** `pnpm -C frontend test` (whole suite) or
  `pnpm -C frontend test src/path/file.test.tsx` (one file). **Do NOT write
  `pnpm test run`** — `test` is already `vitest run`, so the extra `run` becomes a
  filename filter matching nothing. **Lint:** `pnpm -C frontend lint` (oxlint —
  there is no eslint config). **vitest does not typecheck** (esbuild transpile) and
  **oxlint has no types**: the dry run twice had green tests over `tsc` errors
  (T5-3: 14 green, `tsc` exit 2; T7-11: 34 green, 10 `tsc` errors). Every frontend
  task's verify block therefore runs `pnpm -C frontend build`, which runs `tsc`.
  `frontend/e2e/` is **outside** `tsconfig.json`'s `include` and typechecks
  **nowhere** (T9-5); see Task 9 for what replaces that.
- **Every frontend task runs `pnpm -C frontend e2e:match`** in its verify block
  (~30 s). Revision 2 ran it only in Task 9; the dry run's Task 7 broke two browser
  scenarios and a third latently, and nothing between Task 7 and Task 9 noticed
  (T8-1). Any task that edits `frontend/e2e/match.spec.ts` also edits
  `tests/architecture/test_match_gates_are_wired.py` **in the same commit** — the
  dry run spent a commit red because a fixture fix added a scenario without
  touching the pin (T9-3).
- **Never run `ruff format` on a directory.** Format by filename only. Count
  `git status` lines before and after; the delta must be 0 for untouched files.
  `uv run ruff check src tests alembic scripts` is the gate (write `uv run ruff`,
  not bare `ruff` — T2-6). The pasted model block in Task 1 is **not** format-clean
  as pasted (T1-6); format `src/call_manager/db/models.py` by name after editing it.
- **`docs/ARCHITECTURE.md` is generated.** Regenerate in the same commit that moves
  code: `uv run python scripts/gen_architecture.py`. Never hand-edit. Task 1 adds a
  table, so it **will** change there (the dry run measured `+3/-1` table lines, 46 →
  45 tables + 1). Note the generator keys "status vocabularies" on the substring
  `status`, so `ck_call_decisions_state` will **not** appear in that section (T1-8,
  pre-existing; not this plan's code).
- **`.env.example` is generated** from `Settings` by
  `uv run python scripts/gen_env_example.py`, pinned by `tests/test_env_example.py`.
  This plan adds no setting; it must not move.
- **`frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py` are
  generated** from `frontend/routes.json` by `uv run python scripts/gen_routes.py`.
  This plan adds no SPA route, so they should not move — **if they do, that is a
  finding, not a regeneration.**
- **OpenAPI TS types are generated.** Changing `CallOut` or adding an endpoint
  **requires** regenerating `frontend/src/api/schema.gen.ts` with
  `pnpm -C frontend gen:api`. The drift gate can pass without generating, so where
  a task genuinely changes the schema it checks `test -n "$(git diff --name-only --
  frontend/src/api/schema.gen.ts)"`. **Not `git diff --stat`** (exits 0 on an
  unchanged file). **And not in a task where the schema does not change** —
  revision 2's Task 8 demanded the file change on a step that changes nothing on
  the backend, which is a gate that is red on correct code (T8-3). Where the
  guarantee is a *shape* (a required parameter), assert the shape — see Task 8.
- **`uv run alembic check` is gone from this plan.** In a worktree there is no
  `.env`, so it raises `RuntimeError: No database URL`; in the main tree it would
  connect to the LIVE database named in the `.env` this run must never read
  (PF-5, T1-3). The same guarantee is a first-class suite test the plan never
  mentioned: `tests/test_schema_drift.py::test_orm_metadata_matches_the_migrated_database`
  runs `compare_metadata(ctx, Base.metadata)` against the migrated testcontainer
  (T1-11). It is what goes red when model and migration disagree, and it is in
  every full-suite run. (For the record: the dry run ran `alembic check` against a
  throwaway `pgvector/pgvector:pg16` container with `DATABASE_URL` set inline —
  exit 0 on the final tree. That is the human's recipe if they want it; it is not a
  step here.)
- **The `full` edition must keep working** at every commit: `pnpm -C frontend build`
  succeeds and `tests/architecture/full_edition_routes.txt` grows by **exactly 2
  lines, 107 → 109** — `/api/calls/decisions/counts` and `/api/calls/{call_id}/save`
  (the file holds unique **paths**; `POST`+`DELETE /save` are one line; `?view=` is
  a query parameter on the existing `/api/calls`). **The literal `== 107` in
  `tests/architecture/test_edition_surface.py::test_the_baseline_is_not_empty`
  must be bumped to 109 by hand in the same commit** (T3-5) — regenerating the
  manifest alone leaves that test red. `frontend/routes.json` and `routes.gen.ts`
  do **not** move. **Two deliberate exceptions are called out in Task 1**: full's
  hide semantics change from global to per-profile (the point of the table), and
  full's no-profile archive can no longer hide at all (a decision needs a profile —
  D3 below).
- **`docs/superpowers/specs/` is INPUT, not output.** Byte-identical to
  **`ed1153a3`** at every commit (`git diff --quiet ed1153a3 -- docs/superpowers/specs/`
  exits 0; `--quiet`, not `--stat`). Revision 2 named two different pin commits
  (`f0aaed0c` and `ed1153a3`) for the same rule; both are currently identical on
  this directory, and `ed1153a3` is the one the brief names (PF-1). A wrong spec is
  a `PLAN-DEFECTS.md` entry, not an edit. This plan carries eight (§ *Spec defects
  this plan corrects*).
- **Commit titles name the change, not the activity.** Task 6's revision-2 title
  ("Delete the raw Aggregate score block") contradicted its own body — the title is
  what survives into progress notes and commit subjects (T6-4).
- **Every gate must be mutation-tested.** Break the thing the gate protects and
  confirm it goes red, in the same task. *A verification that reddens nothing is a
  finding, not a pass.* Do the whole mutate → test → restore cycle **inside one
  shell invocation**, with a `.bak` copy OUTSIDE the repo, an `EXIT`/`INT`/`TERM`
  trap that restores, a `__pycache__` purge after the restore, and `git diff
  --quiet` afterwards. A mutation script that aborts on a no-op edit (the anchor
  did not match) is mandatory — a mutation that silently did not apply "passes".
  **A crash mutation flatters a suite** (T2-2: deleting a `group_by` reddened four
  tests with an SQL error, which proves nothing about any of them). **A mutation
  that does not compile is not evidence about the tests** (T9-5: `e2e:match` runs
  `tsc` first, so a mutant that fails typecheck yields "webServer was not able to
  start", a red that says nothing about any scenario).
- **Name what trivially passes before writing any gate.** The Plan A run recorded
  nine hollow gates; the dry run of revision 2 found the plan's own *wording*
  produced hollow or red-on-correct gates in four places (T7-1, T6-2, T8-3, T4-1)
  and a surviving mutation under six more (F-5, F-12). The shapes, now eleven:
  a subset check; an absence that is already true; a mutation naming a test that
  lacks the assertion; a weaker property than the one claimed; an expected output
  that is what the *failure* prints; **"asserts no mutation fires" without an
  awaited flush** (TanStack defers `mutate()` to a microtask, so a synchronous
  `not.toHaveBeenCalled()` runs before the request would have been made — T7-1);
  **a presence assertion outside the gated section** ("some other string is
  present" was satisfied by the page title with the whole card gone — T6-2);
  **"this file must change" on a step that changes nothing** (T8-3); **an `ORDER
  BY` gate on a table small enough for the index scan to return rows already
  sorted** (removing the `ORDER BY` changed nothing until the test forced a
  sequential scan — T4-11); **a stub whose wrong source agrees with the right one**
  (`counts.inbox === items.length` — T7-7); **an exact-set pin that can only see
  the names someone remembered to write down** (a brand-new function that
  correctly calls `can_read_profile` is invisible to every test in
  `test_profile_access_single_source.py` — T2-13; new surfaces are pinned by hand).

---

## Scope: this is Plan B of four

| Plan | Spec sections | Ships | State |
| --- | --- | --- | --- |
| A | §5, §9 gates 1/2/4/5, §11 steps 1-2 | the `match` edition | **MERGED `9365f123`** |
| **B (this one)** | **§4.1, §4.2, §5 card, §11 steps 3-4** | **`call_decisions`, save/hide, inbox, match reasons on the list** | this plan |
| C — Boards | §4.3, §6, §9 gate 3, §11 steps 5-6 | boards, approval queue, `/b/:slug` | not started — see *Handed to Plan C* |
| D — Extras | §7, §11 steps 7-8 | share by email; change detection; swipe + phone rail | not started |

Plan B ships working software on its own: a release-1 build whose calls screen is
a working inbox with Saved and Hidden, per profile, showing why each call matched,
with a way back out of both.

### Already done by Plan A — do not redo

**§11 step 3 says "remove the two `/cockpit/new` links".** Plan A did this, but
**gated them rather than deleting them** (`{!isMatchEdition && (…)}` around the
`Start drafting` button in `CallRow.tsx` and `CallDetailBody.tsx`), recorded as
ESCALATE **E1**. The spec's words are "**they must be removed, not disabled**";
deleting them outright would have taken the buttons out of the **full** edition
too, which the full-edition constraint forbids. The gate
`frontend/src/noCockpitLinks.test.tsx` is a **source scan over all of `src/`**, so
it covers `TriageFeed` too (T7-14's correction to T7-9); only
`Dashboard.test.tsx`'s self-description ("the MATCH dashboard … emits no /cockpit
link") is stale after Task 7 moves `Dashboard` out of the match bundle — Task 9
fixes that string. **This is settled. Do not re-open it, and do not delete those
branches.**

---

## Spec defects this plan corrects

Measured against merged master before this plan was written; the raw probes are in
`/home/parlar_ai/call-match-r1b-run/PLAN-AUTHORING-MEASUREMENTS.md` (revision 2
cited a `MEASUREMENTS.md` that does not exist — PF-2). The spec is input — these
are recorded, not edited into it.

**D-B1 — the criterion vocabulary is seven, not six.** §4.2 lists `topic_fit |
eligibility | exclusions | timeline | budget | trl`. `web/filters.py`'s
`_CRITERION_LABELS` carries a seventh, `embedding` → "Topic similarity", and
`ranking/worker.py` writes it as one of four gate rows in the `gate_results` tuple
list. There is **no `CheckConstraint`** on `criterion` — it is a bare `String(20)`.

**D-B2 — the rating vocabulary is five shapes, not four.** §4.2 says `"5" |
"pass" | "fail" | "unclear"`. Measured:

| criterion | writer | ratings |
| --- | --- | --- |
| `budget`, `trl`, `timeline`, `embedding` | `worker.py` gates, `GateStatus.value` | `pass` \| `fail` \| **`unknown`** |
| `topic_fit` | LLM | `"1"` … `"5"` |
| `eligibility` | LLM (`prompt.py`, the `not in ("pass", "fail", "unclear")` validator) | `pass` \| `fail` \| `unclear` |
| `exclusions` | LLM (`prompt.py`, the `not in ("pass", "fail")` validator) | `pass` \| `fail` |

`unknown` (`ranking/criteria.py::GateStatus`) is absent from the spec. A chip
renderer built to the spec's four falls through on every gate that could not judge.
**Task 5 pins both halves of this table against the source** (revision 2 pinned only
the criterion half — F-5).

**D-B3 — a card can carry one chip, not six.** `worker.py`'s gate loop `return`s on
the first `FAIL` (except `timeline`/`embedding` under `must_include_override`). A
call failing the budget gate has exactly one `call_ranking_criteria` row. The chip
row must render 1..7, and 0.

**D-B4 — §4.1 misses a fifth `user_call_dismissals` consumer.** It names three.
Measured, there are five files: `digest/assemble.py`, `web/api/calls.py`,
`web/dashboard.py`, `db/models.py` (the model) and **`ranking/golden_set.py`**,
whose `candidates_dismissed_high_score()` joins `Subscription` to *infer* which
profile a global dismissal belonged to. Left unchanged it would treat a **saved**
call as a "dismissed_high_score" golden-set candidate. Task 1 fixes it.

**D-B5 — the spec's frontend line numbers are stale.** Plan A inserted a 10-line
edition gate into `CallDetailBody.tsx`, and every later merge moves them again.
**Anchor on strings**: the `Aggregate score` heading, the `Analyzed by` attribution,
the `Escalated from` note, the `Start drafting` button, and `MatchBadge`'s single
render site in `CallRow.tsx`.

**D-B6 (new, T3-9) — §4.1's `decided_by` "for shared profiles" is unreachable under
the plan's own key.** The unique key is `(user_id, profile_id, call_id)` and every
endpoint writes `user_id = decided_by = user.id`, so no endpoint can produce a row
where `decided_by != user_id`. Either the key should be `(profile_id, call_id)` (a
decision per PROFILE — a product change this plan does not make) or the column is
dead weight in release 1. **Kept, with an honest docstring** (D4 below).

**D-B7 (new, F-1) — §4.1 is silent on what a closed deadline means for Saved and
Hidden.** "Saved means I am interested in this" argues that a saved call must not
vanish when its deadline passes; the list's default `show_closed=False` says the
opposite. Decided in D1 below.

**D-B8 (new, T9-1, F-3) — §5 describes deciding and never describes undeciding
from the feed, nor what the URL carries across a detail round trip.** Revision 2
inherited both silences, and the dry run shipped a product where a hidden call could
never be restored except by hiding it a second time from its own page. Decided in
D2 and D11 below.

---

## Decided by the human, 2026-09-04

**1. No profile is an onboarding screen, not a firehose.** Measured first: without a
profile the list does **not** empty — `build_dashboard_view` falls back to the
unranked sort and returns **every open call in the database**, 50 a page,
newest-published first, with no score, no chips and no reason. Three existing tests
pin that behaviour (`tests/test_api_calls_feed_behavior.py -k unranked`, 3 passed).
That screen is defensible in the **full** edition as a browsable archive. It is not
defensible as release 1. **Task 8 makes the match edition show an onboarding screen
instead.** The full edition keeps the unranked fallback unchanged.

**2. A dismissal that fans out to zero rows is dropped, and the count is printed.**
The alternative — a `profile_id NULL` row — breaks the unique index and the
"a decision is per profile" rule. **Before running the migration on real data, run
this and put the number in the commit message:**

```sql
SELECT count(*) FROM user_call_dismissals d
JOIN users u ON u.id = d.user_id
WHERE NOT EXISTS (SELECT 1 FROM subscriptions s
                  JOIN profiles p ON p.id = s.profile_id AND p.is_active
                  WHERE s.user_id = d.user_id)
  AND (u.default_profile_id IS NULL
       OR NOT EXISTS (SELECT 1 FROM profiles p
                      WHERE p.id = u.default_profile_id AND p.is_active));
```

It needs the DB URL from `.env`, which this run must never read — so it is the
human's query to run, not the agent's.

## Decided unattended in Phase 2, 2026-09-05

Phase 1 recorded ten *Decision* entries and the final review added more. Nobody was
there to answer, so each is decided here, with the reason, so the human can reverse
any of them with one line. **Every one of these is implemented and gated in the task
named; none is a note.**

**D1 (F-1, T3-2, D-B7). Saved and Hidden list DECISIONS, so a closed call stays in
them; the inbox and the archive still hide closed calls by default.** `open_expr`
is not applied to `view in ("saved", "hidden")` — in the list, in `pagination.total`,
and in the counts. `show_closed` therefore affects only `inbox`/`archive`. A
researcher who saved a call because they intend to apply must not watch it vanish
exactly when they go looking for it; a hidden call that closed is still a hidden
call. The card already prints "Closed" for a past deadline. → Task 1 gate 12, Task
3 gate 4. **Measured:** the gate is red on the dry tree (which drops them) and green
under a two-line patch (see Task 1 Step 6).

**D2 (T9-1, D-B8). Release 1 ships an un-decide control in the feed: the `u` key and
a "Return to inbox" button on the card, in the Saved and Hidden views.** `u` on the
focused card issues `DELETE …/save` in Saved and `DELETE …/hide` in Hidden; in the
Inbox it does nothing. The button exists because keys are undiscoverable and the
spec's own scenario ("un-hide returns it to the inbox") was unperformable without
one. `s` in Hidden and `h` in Saved keep their flip semantics. → Task 7 gate 9.

**D3 (T2-3). A decision needs a profile — so the full edition's no-profile archive
can no longer hide.** `call_decisions.profile_id` is NOT NULL; there is no row to
write. `profile_id` is REQUIRED on hide/un-hide from Task 1 (not Task 8 — T8-2), and
`CallDetail.tsx` renders the hide button disabled with "Pick a profile to hide
calls." when `?profile=` is absent, and issues no POST. This is the second
deliberate change to the full edition (with per-profile hide semantics), named in
the full-edition constraint above and in Task 1's commit message.

**D4 (T3-9, D-B6). `decided_by` stays, documented honestly.** The spec asks for it;
the spec is input. The model comment says: *"Equal to user_id for every row an
endpoint can write in release 1 — the key is per user, so a colleague's decision is
their own row. Kept so a per-profile key later does not need a migration for it."*
The revision-2 comment ("differs from user_id only on a shared profile") described a
state nothing can produce and is deleted. The flip gates seed the pre-existing row
with a **different** `decided_by` and a 2020 `decided_at` via raw SQL so all three
columns are observably rewritten (T3-3).

**D5 (T4-8, T4-11, T5-10). Criterion order is deterministic: both the list's batched
query and the detail's query `ORDER BY criterion`, in Task 4; the chip row then
re-sorts into the fixed reading order (`CRITERIA` in `CriterionChips.tsx`) in
Task 5.** List and detail therefore agree on the SET and the server order; the list
chip row shows the reading order and the detail page shows alphabetical — a known,
recorded cosmetic difference, not release-1 scope. The gate forces a sequential
scan (`SET enable_indexscan = off; SET enable_bitmapscan = off`), because on a
small table the composite-PK index scan returns rows already sorted and a missing
`ORDER BY` is unobservable (T4-11). **Measured** (Task 4 mutation B16e).

**D6 (T5-4). `topic_fit` tone: `>= 4` → `pass`, otherwise `unsure`.** `fail` is
reserved for hard gates the model or a rule actually failed; a 2/5 topic fit is
"not convinced", which is what the unsure tone already says. Reversible in one line
of `CriterionChips.tsx` and one test.

**D7 (T1-9). The backfill does NOT apply the read rule.** A user whose stored
default profile was later made private and un-shared still gets a `hidden` row on
it. That row is harmless (never surfaced while unreadable), and correct the moment
the share is restored — revocation is a filter, not a delete, which is also what
`resolve_profile_selection` does with the stored default. Adding the visibility
predicate to a migration would put a second copy of `can_read_profile` in SQL.

**D8 (F-7, T2-8). A decision view with no effective profile is EMPTY, never the
cross-profile union.** `?view=saved` or `?view=hidden` with no `?profile=` and no
readable default returns `items: []`, `pagination.total: 0`, `effective.profile_id:
null`. Revision 2 reused the archive's "degrade to unranked = ANY profile" rule
without re-deriving it; for `saved`/`hidden` that rule WIDENS. The match frontend
still needs a 200 with `effective.profile_id: null` from the list to branch to the
onboarding screen (Task 8), which is why this is an empty page and not a 422.
`inbox` and `archive` with no profile keep today's user-wide behaviour (the full
edition's archive; the match edition never renders an inbox without a profile).
→ Task 1 gate 11b. **Measured:** red on the dry tree (`?view=saved with no profile
widened to the ANY-union: ['DecisionCall0']`), green under a two-line patch.

**D9 (F-6, T2-2). `GET /api/calls/hidden` takes a REQUIRED `profile_id`.** The
endpoint has no client in either edition (`git grep 'calls/hidden'` over
`frontend/src` + `frontend/e2e`, excluding `schema.gen.ts`, is empty at
`dcd4ddbd`). Revision 2 gave it "no `profile_id` = ANY, DISTINCT by call"
semantics, which cost a dedup subquery and three tests for a surface nobody calls,
and then cited it as precedent. Required is the simplest rule and matches every
other decision route. It stays user-scoped and applies **no** read check (same
reason as un-hide: a user must be able to see and undo their own rows after a share
is revoked — T2-10). → Task 1 gate 5b. **Measured:** red on the dry tree
(`GET /calls/hidden without profile_id answered 200`).

**D10 (F-2, T4-5, T4-9). The full edition pays for the card's payload, and the cost
is measured, not guessed.** `CallOut` grows for every view. The only unbounded part
is `criteria` (up to 7 chips × `rationale` + `quoted_evidence`), and it is populated
**only when a profile is selected** — the full edition's no-profile archive pays
nothing; the full edition WITH a profile pays for chips `CallRow` does not render.
`quoted_evidence` stays on the LIST schema because Task 5's expansion renders it.
Task 4 prints `len(response.content)` for a 50-row ranked page with 7 chips per row,
before and after, and puts both numbers in the commit message. If the number is
ugly, the lever is `?fields=` in a later slice — not re-deferring `summary`, which
would reintroduce an N+1.

**D11 (F-3). The URL is the state, and every link preserves it.** The card's title
link is `/calls/{id}?profile={p}&view={v}`; `CallDetail`'s back link returns to
`/` + the search it arrived with; and the rail carries a **profile picker** — every
readable profile (from `facets.profiles`) as a link to `/?profile={id}&view={v}`,
the current one marked — because the match index otherwise has no profile switcher
at all (it left with `DashboardHeader` in Task 7) and the DoD smoke "switching
profile changes which calls are hidden" was unperformable. → Task 7 gates 12–13,
CallDetail gate. **Measured:** all three red on the dry tree (card link
`/calls/1?profile=7`, back link `/`, no picker link).

**D12 (F-9). Saved and Hidden are ordered by `decided_at DESC` (most recent
decision first), then `Call.id DESC`; the inbox and the archive keep the score
order.** This is the ordering Task 1's index was sized for and the reason a flip
rewrites `decided_at`; revision 2 built both lists on the inbox's score order and
left the index and the three-column write justified by nothing. A decision view is
a log of what you just did. → Task 1 gate 13. **Measured:** red on the dry tree
(`['DecisionCall1', 'DecisionCall0']` — the inbox order).

**D13 (T2-9, T2-11). The golden-set widening is intended and pinned.** The old
query required the decider to be SUBSCRIBED to the profile; the new one correlates
on `(call_id, profile_id, state='hidden')` only. A hide on profile P by any READER
of P (Task 1's write rule already requires readability) is a negative label for P —
that is what "a decision is per profile" means. → Task 1 gate 4c. **Measured:** the
pin goes red when a subscription predicate is re-added (`a reader who is not
subscribed hid this call on the profile; the golden set must see it`).

**D14 (T3-6, T3-7). The `?profile=` / `?profile_id=` naming split and the two
response shapes are kept.** The list's `?profile=<str>` (degrades) predates this
plan and is the full edition's; every decision route takes `?profile_id=<int>`
(404s). `POST /hide` keeps `CallHideOut{hidden}` ("Hidden is a re-skin"); `/save`
returns `CallDecisionOut{state}`. The frontend has two hooks anyway. `decisions.ts`
carries a comment saying this is a wire fact, not a choice — do not "harmonise"
client-side.

**D15 (T7-4). Saved and Hidden get a "Next page" link, owned by Task 7.** The feed
renders `data.items` (server default `limit=50`); the inbox self-heals (decided
cards leave, page 1 refills) but Saved/Hidden do not. When `pagination.has_next`
is true the column ends with a link to `page + 1` (URL-carried, like `view`). No
new module: it lives in `TriageFeed.tsx`. → Task 7 gate 14 (`PREDICTED`).

**D16 (PF-7). `tests/web/` is created deliberately.** The dry run proved pytest's
default import mode collects it (`tests/__init__.py` exists, subdirs have none).
Existing API tests are `tests/test_api_*.py`; the two new files are the start of a
directory, not a mistake. Keep it.

**D17 (T9-5, F-12 #2). `frontend/e2e/` is NOT added to `tsc`.** Phase 2 probed it:
a `tsconfig` that `include`s `e2e` fails on two `playwright-core` versions in the
lockfile (`Type 'Page' is missing the following properties from type 'Page':
localStorage, sessionStorage`; `1.60.0` vs `1.61.0-alpha`) plus `dashboard.spec.ts`
— a dependency problem, not this plan's. The floating-promise hazard (`delete one
await` keeps the `expect(` count and can never fail) is closed by a **text gate**
instead: every async Playwright matcher in `match.spec.ts` must be preceded by
`await`. → Task 9 pin 3. **Measured** both ways.

**D18 (T2-8, T3 note, T7-10, F-4). `min_score` and the summary badges are
view-scoped as a side effect, and the counts endpoint ignores `min_score`.** The
match edition has no `min_score` control and renders no badges, and the full
edition sends no `view`, so no release-1 user can observe either. Recorded in the
counts route's docstring and in `decisions.ts`, not gated.

**D19 (F-11). A closed finding closes its code comment in the same commit.** The
dry run's `match.spec.ts` still described the 20-second stale-rail bug as live
after `7e6b49d5` fixed it. Task 9's scenario asserts the correct rail count after an
un-decide, and the comment goes.

**D20 (F-11). Two cheap indexes and one guard.** `call_decisions` gets plain
indexes on `call_id` and on `profile_id` (both FKs `CASCADE`; without them deleting
a `Call` or a `Profile` sequentially scans the largest per-user table). And
`TriageFeed` resets the focus index on a view change (it already does) and ignores
`s`/`h`/`u` while the list query is fetching (`isFetching`), so a key cannot decide
a card from the view just left under `keepPreviousData`.

## Outside review, round 1 — 2026-09-04

Three model families reviewed revision 1 before any code existed (`gpt-5.6-sol`,
`grok-4.6`, `claude-sonnet-5`; 207k-char package, 39 findings). **26 changed the
plan.** Of the 26, **eleven were gates that would have stayed green while the thing
they protect was broken.** The settled points are in
`.superpowers/review/planB-settled.md`; raw findings in
`.superpowers/review/findings-planB/planB-<model>.md`. Two of that round's
"measured" claims were later refuted by the dry run and are corrected in place
here: the route-order hazard (Task 3 — the probe was real but run on a two-segment
path and generalised to a three-segment one), and the profile-ownership paragraph
(Task 1 — superseded by the visibility merge).

## Phase 1 dry run — what it found, in one paragraph

The dry run built all nine tasks, ran ~90 mutations, and reviewed every commit. The
three findings that reshape this revision: **(1)** Tasks 1 and 2 as written cannot
both be green — the model kept while its table is dropped fails
`test_schema_drift.py` and every `pg_session_factory` teardown (`1386 failed, 3257
passed, 2885 errors` at `98bb03b3`), so they are one task now; **(2)** the plan's
gate WORDING produced hollow or red-on-correct gates in four places, each now
rewritten with its exact assertion and measured red output; **(3)** the assembled
product had gaps no task owned — a saved call silently lost when its deadline
passed, no way to un-hide from the feed, a detail round trip that dropped both URL
parameters, Saved/Hidden truncating at 50 — each now decided above and owned by a
task. Everything else is in `PLAN-DEFECTS.md`, each entry with its disposition.

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/db/models.py` | Add `CallDecision`, delete `UserCallDismissal` — same commit (Task 1) |
| `alembic/versions/0071_call_decisions.py` | Create, backfill, drop (Task 1) |
| `src/call_manager/web/api/calls.py` | Re-point hide (T1); add save + counts (T3); grow `CallOut` + batched criteria (T4) |
| `src/call_manager/web/dashboard.py` | `_decision_predicate` + `view`, decision-view ordering and closed rule (T1); `_base_call_filters` + `count_decision_views` (T3) |
| `src/call_manager/digest/assemble.py` | Per-profile exclusion sets (T1) |
| `src/call_manager/ranking/golden_set.py` | Read `profile_id`; filter `state='hidden'`; drop the `Subscription` import (T1) |
| `tests/test_migration_0071_call_decisions.py` | The backfill-set gate (T1, create) |
| `tests/web/test_call_decisions_api.py` | Hide/un-hide/hidden list + views (T1, create); save/counts (T3) |
| `tests/web/test_calls_list_query_count.py` | No-N+1, row-count, ordering gates (T4, create) |
| `tests/architecture/test_criterion_vocabulary.py` | Pins the 7 criteria AND the 5 rating words (T5, create) |
| `tests/architecture/test_profile_access_single_source.py` | Pin every new `can_read_profile` surface by hand (T1, T3) |
| `tests/architecture/test_edition_surface.py` + `full_edition_routes.txt` | 107 → 109 and the `== 109` literal (T3) |
| `tests/architecture/test_match_gates_are_wired.py` | Scenario titles, assertion-site count, `await` scan (T7 fixture, T8, T9) |
| `tests/test_api_calls.py` | Key-set pin on `CallOut` grows (T4) |
| existing tests naming `UserCallDismissal` | `test_api_calls_hide.py`, `test_models_dismissal_digestrun_bounce.py`, digest and golden-set tests (T1) |

**Frontend**

| File | Responsibility |
| --- | --- |
| `frontend/src/api/calls.ts` | `useHideCall`/`useUnhideCall` take `{ id, profileId }` (T1) |
| `frontend/src/routes/CallDetail.tsx` | Hide needs a profile; back link keeps the search; undo invalidates counts (T1, T7) |
| `frontend/src/routes/CallDetailBody.tsx` | Gate the three raw aggregates out of match (T6) |
| `frontend/src/components/CriterionChips.tsx` + `.module.css` + `.test.tsx` | The chip row + expansion (T5, create) |
| `frontend/src/api/decisions.ts` | Save / un-save / counts + `invalidateDecisions` (T7, create) |
| `frontend/src/api/dashboard.ts` | `view` sanitised to the four Literal values, once (T7) |
| `frontend/src/routes/TriageFeed.tsx` + `.module.css` + `.test.tsx` | Rail (views, counts, profile picker), feed, keys `j k s h u`, next-page link (T7, create) |
| `frontend/src/routes/TriageCard.tsx` + `.test.tsx` | The expanded / collapsed card, "Return to inbox" button (T7, create) |
| `frontend/src/components/Deadline.tsx` | Additive: days-left text shares the Stockholm calendar-day rule (T7) |
| `frontend/src/routes/TriageEmptyProfile.tsx` + `.test.tsx` | No profile yet: the onboarding screen; reuses `TriageFeed.module.css` (T8, create) |
| `frontend/src/router.simple.tsx` | Mount `TriageFeed` at index (T7) |
| `frontend/src/api/schema.gen.ts` | Regenerated in T1, T3, T4 — and NOT in T8 |
| `frontend/e2e/match.spec.ts` | Fixture update (T7), onboarding scenario (T8), triage scenarios (T9) |

**Unchanged on purpose:** `frontend/src/routes/Dashboard.tsx`,
`frontend/src/routes/CallRow.tsx`, `frontend/src/components/MatchBadge.tsx` and its
test. These are the **full** edition's list. `MatchBadge` has exactly one render
site (`CallRow.tsx`); the match edition stops importing `CallRow` at Task 7, so
neither file needs an edition branch and neither may be deleted. **Note (T7-5):
`CriterionChips` is not in the match bundle until Task 7 mounts `TriageFeed`** —
nothing reachable from `router.simple.tsx` imports it before then, so Task 5's
bundle check proves nothing about the chips. Task 5 says so in its verify block.

---

### Task 1: `call_decisions` — the table, the migration, and every consumer, in ONE commit

Spec §4.1. Replace `user_call_dismissals` with a per-profile, two-state decision
table, backfilling every existing row, in **one** migration — and re-point all five
consumers, delete the old model, and update the two frontend callers **in the same
commit**. Revision 2 split this across two tasks with the reasoning "keep the model
so the tree can import between the tasks". The dry run built that commit
(`98bb03b3`) and measured it:

```
$ uv run pytest tests/test_migration_0071_call_decisions.py::test_the_old_dismissals_table_is_dropped -q
E  psycopg.errors.UndefinedTable: relation "user_call_dismissals" does not exist
$ uv run pytest -n 16 --dist loadfile -q
1386 failed, 3257 passed, 1 skipped, 2 warnings, 2885 errors in 142.47s
```

`tests/conftest.py` builds its teardown `TRUNCATE` from `Base.metadata.sorted_tables`
(the comment "Truncate every table Base knows about"), which still names the dropped
table, so every test requesting `pg_session_factory` errors in teardown;
`test_schema_drift.py` fails outright; and the app 500s at request time because
`dashboard.py`, `calls.py`, `assemble.py` and `golden_set.py` all query the dropped
table. A kept model IS a model/migration disagreement. There is no green ordering
of two commits; there is one commit (F-8).

**Files:**
- Modify: `src/call_manager/db/models.py` — add `CallDecision` after `class Subscription`; delete `class UserCallDismissal` (anchor: the class name)
- Create: `alembic/versions/0071_call_decisions.py` — `down_revision = "0070"` (head
  re-measured: `0070_profile_visibility.py`, **70** migrations)
- Modify: `src/call_manager/web/dashboard.py` — `_decision_predicate`, `View`, the closed rule and ordering for decision views
- Modify: `src/call_manager/web/api/calls.py` — `hide_call`, `unhide_call`, `list_hidden_calls`, `_readable_active_profile_or_404`, the `view` parameter on `get_calls`
- Modify: `src/call_manager/digest/assemble.py` — the exclusion set
- Modify: `src/call_manager/ranking/golden_set.py` — `candidates_dismissed_high_score`, and **delete the now-unused `Subscription` import** (T2-1: as pasted in revision 2, `uv run ruff check` fails with `F401 'Subscription' imported but unused` on the task's own verify step)
- Modify: `frontend/src/api/calls.ts` — `useHideCall`/`useUnhideCall`
- Modify: `frontend/src/routes/CallDetail.tsx` — their only caller, in **both** editions
- Regenerate: `frontend/src/api/schema.gen.ts`, `docs/ARCHITECTURE.md`
- Create: `tests/test_migration_0071_call_decisions.py` (**not** `tests/migrations/…` — that directory does not exist; a path under it makes pytest exit **4, "no tests ran"**)
- Create: `tests/web/test_call_decisions_api.py`
- Modify: `tests/architecture/test_profile_access_single_source.py` — pin the two new surfaces (Step 8)
- Modify: every existing test that names `UserCallDismissal` (`grep -rln UserCallDismissal tests/`)

**Interfaces:**
- Produces: `CallDecision`, profile-scoped hiding, the four `view`s — consumed by Tasks 3, 4, 7.
- Consumes: nothing.

**This is where the `full` edition's behaviour deliberately changes, twice.** Full's
dismissal is global today; after this task it is scoped to the profile the dashboard
is matching against (spec: *"a deliberate semantic change, and it is not lossless"*).
And full's no-profile archive can no longer hide at all (D3). Both go in the commit
message.

- [ ] **Step 1: The model**

```python
class CallDecision(Base):
    """A user's triage decision about one call, for one profile.

    Supersedes the table migration 0071 drops. (Its NAME is deliberately not
    written here: Step 9's "no consumer remains" gate greps src/ for it, and a
    docstring mention makes that gate fail on correct code. The old name lives in
    the migration, where the grep never looks.) The old key was (user_id, call_id):
    a dismissal was GLOBAL, so a researcher matching against two profiles who
    dismissed a call for one silently dismissed it for both. A decision is per
    profile because that is the entire point of the table.

    THERE IS NO 'undecided' STATE. Undecided is the ABSENCE of a row, so the
    inbox query is a NOT EXISTS and a newly ingested call arrives undecided with
    no backfill. Adding an 'undecided' value here would make every ingest write
    a row per (user, profile, call) and break that.
    """

    __tablename__ = "call_decisions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "profile_id", "call_id", name="uq_call_decisions_user_profile_call"
        ),
        CheckConstraint("state IN ('saved','hidden')", name="ck_call_decisions_state"),
        # The inbox query is NOT EXISTS (user_id, profile_id, call_id) -- covered
        # by the unique index above. This second index serves the Saved and
        # Hidden LISTS, which filter (user_id, profile_id, state) and order by
        # decided_at DESC (Phase 2 decision D12).
        Index("ix_call_decisions_user_profile_state", "user_id", "profile_id", "state"),
        # Both FKs CASCADE; without these, deleting a Call or a Profile
        # sequentially scans the largest per-user table (D20).
        Index("ix_call_decisions_call_id", "call_id"),
        Index("ix_call_decisions_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    # 'saved' | 'hidden'. Enforced by ck_call_decisions_state, not a native enum:
    # String + CHECK is what profiles.visibility uses (the closest precedent --
    # ranking_remote_attempts.outcome is Text + CHECK), and it does not force an
    # ALTER TYPE when a later release adds a state.
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    # The user's own reasoning. NOTHING WRITES THIS IN RELEASE 1 -- the column is
    # created now so the note UI is a frontend change later, not a migration.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Who pressed the key. EQUAL TO user_id FOR EVERY ROW AN ENDPOINT CAN WRITE
    # IN RELEASE 1: the key is per user, so a colleague's decision on a shared
    # profile is their own row, not a rewrite of this one. Kept because spec 4.1
    # asks for it, and so that a per-PROFILE key later does not need a migration
    # for the column. NULLABLE + ON DELETE SET NULL: the backfill sets it to
    # user_id, so RESTRICT would block deleting an ordinary account even though
    # user_id itself CASCADEs. The decision still dies with its OWNER.
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
```

Then **delete `class UserCallDismissal`** in the same edit, and run
`uv run ruff format src/call_manager/db/models.py` (by filename — the pasted block is
not format-clean, T1-6).

- [ ] **Step 2: The migration**

`upgrade()` in this order — create (table, unique, check, three indexes), backfill,
count the orphans, **then** drop:

```python
# The backfill set, named exactly (spec 4.1): every profile the user is
# SUBSCRIBED to, plus their users.default_profile_id, restricted to
# profiles.is_active = true. NOT "every active profile in the database"
# (Profile.is_active is a GLOBAL flag, so that reading writes a hide onto other
# groups' profiles); NOT "the user's single default profile" (that silently
# UN-hides calls people had dealt with on their other profiles); and NOT
# filtered by the read rule (Phase 2 decision D7 -- a row on a profile the user
# can no longer read is harmless and becomes correct again when re-shared).
op.execute("""
    INSERT INTO call_decisions
        (user_id, profile_id, call_id, state, decided_at, decided_by)
    SELECT d.user_id, p.id, d.call_id, 'hidden', d.dismissed_at, d.user_id
    FROM user_call_dismissals d
    JOIN users u ON u.id = d.user_id
    JOIN profiles p
      ON p.is_active = true
     AND (p.id IN (SELECT s.profile_id FROM subscriptions s WHERE s.user_id = d.user_id)
          OR p.id = u.default_profile_id)
    ON CONFLICT (user_id, profile_id, call_id) DO NOTHING
""")
orphans = conn.execute(sa.text("""
    SELECT count(*) FROM user_call_dismissals d
    WHERE NOT EXISTS (SELECT 1 FROM call_decisions c
                      WHERE c.user_id = d.user_id AND c.call_id = d.call_id)
""")).scalar_one()
if orphans:
    print(f"0071: {orphans} dismissal(s) had no active subscribed or default "
          f"profile and were NOT migrated")
op.drop_table("user_call_dismissals")
```

**The `ON CONFLICT` is defensive, not load-bearing.** The `OR` sits in a JOIN's `ON`
clause and is evaluated once per candidate `profiles` row; a doubly-matching profile
contributes one row. Mutation B4b below proves it inert (dry run: `7 passed` with the
clause deleted). Keep it for re-run safety, and know that if the JOIN is ever split
into a `UNION`, `DO NOTHING` becomes the thing that swallows a real duplicate.

`downgrade()` recreates `user_call_dismissals` and collapses `state='hidden'` rows
back to distinct `(user_id, call_id)` pairs. It **cannot** restore the profile
dimension and it cannot resurrect the orphans — say so in the docstring.

**On the read rule, for the record.** Profiles became personal in `ed1153a3`: a
profile has an owner and a `visibility` of `'org'` or `'private'`, and
`web/profile_access.py::readable_profile_ids` / `can_read_profile` is the single
rule deciding who may read it. `list_profiles` filters by it, and
`resolve_profile_selection` applies it on **both** arms (the stored default and an
explicit `?profile=`), and the profiles facet is `Profile.id.in_(readable_profile_ids(viewer))`.
Revision 2's paragraph claiming "every active profile in the database, offered to
every user" described the tree before that merge (T1-5). What survives of it: the
backfill set (subscriptions ∪ default) is still narrower than the readable set, so a
user browsing a readable-but-unsubscribed profile who hid a call there has no
decision row after migration. The spec chose this; it is written down so the first
person it surprises finds it.

- [ ] **Step 3: `web/dashboard.py` — four views over one query**

`GET /api/calls` gains `view: Literal["archive", "inbox", "saved", "hidden"]`
(inline `Literal`, **not** the `View` alias — ruff 0.15.10's B008 exempts `Query()`
under an inline Literal but flags it under a module-level alias, exactly why the
existing `sort` parameter duplicates its Literal inline — T2-4). `archive` is the
default, so an omitted parameter is exactly today's behaviour.

| `view` | predicate | closed calls | order | who asks |
| --- | --- | --- | --- | --- |
| `archive` (default) | `NOT EXISTS` a `hidden` decision | hidden unless `show_closed` | score / newest | the **full** edition, byte-for-byte today's rule |
| `inbox` | `NOT EXISTS` **any** decision | hidden unless `show_closed` | score / newest | the match feed's default |
| `saved` | `EXISTS` a `saved` decision | **always listed** (D1) | `decided_at DESC, id DESC` (D12) | the match rail |
| `hidden` | `EXISTS` a `hidden` decision | **always listed** (D1) | `decided_at DESC, id DESC` (D12) | the match rail |

```python
def _decision_predicate(view, *, user_id, profile_id):
    """The ONLY thing that differs between the four views' row sets.

    profile_id is None -> archive/inbox: the full edition's unranked archive,
    suppressing a call hidden on ANY of the user's profiles (today's behaviour
    verbatim; NOT "hidden on every profile", a different query that would
    re-surface calls the user has dealt with). For saved/hidden with no profile
    there is nothing to scope a decision to, and the ANY rule would WIDEN --
    so those views are EMPTY (Phase 2 decision D8).
    """
    rows = select(CallDecision.call_id).where(CallDecision.user_id == user_id)
    if profile_id is not None:
        rows = rows.where(CallDecision.profile_id == profile_id)
    if view == "inbox":
        return Call.id.notin_(rows.scalar_subquery())
    if view in ("saved", "hidden"):
        if profile_id is None:
            return Call.id.in_([])          # empty, never the union
        return Call.id.in_(rows.where(CallDecision.state == view).scalar_subquery())
    if view != "archive":
        raise ValueError(f"unknown view {view!r}")   # the HTTP Literal is the guard; this is the tripwire (T2-12)
    return Call.id.notin_(rows.where(CallDecision.state == "hidden").scalar_subquery())
```

Two more edits in `build_dashboard_view`, both anchored on the comment "The list
(and its pagination total) hide closed calls by default": the `open_expr` append
becomes `if not show_closed and view not in ("saved", "hidden")` (D1); and for
`view in ("saved", "hidden")` the page query joins `CallDecision` (one row per call
under the unique key, so a join is the `EXISTS`) and orders by
`CallDecision.decided_at.desc(), Call.id.desc()` instead of the score order (D12).
The `profile_id` used for both is the **effective** one — the id
`build_dashboard_view` re-validated (an unknown/inactive id degrades to `None`
above the join), so the decision scope and the ranking join always agree (T4-3).

`GET /calls/saved` is **not** added: the saved list is `?view=saved`, with every
card field already on it.

- [ ] **Step 4: `digest/assemble.py` — one exclusion set per profile**

`dismissed_ids` (anchor: the local name) becomes a `dict[int, set[int]]` keyed by
`profile_id`, and each profile's candidate query subtracts its own set. **Filter on
`state == "hidden"`**, not "has a decision" — a saved call is the one place the
user has said they want the call in the digest.

- [ ] **Step 5: `ranking/golden_set.py` — read the column, do not infer it**

`candidates_dismissed_high_score()` correlates directly:

```python
exists().where(
    and_(
        CallDecision.call_id == CallRanking.call_id,
        CallDecision.profile_id == CallRanking.profile_id,
        CallDecision.state == "hidden",   # a SAVED call is not a negative label
    )
)
```

and the `Subscription` import goes (T2-1). This **widens** who can contribute a
negative label from subscribers to readers — intended and pinned (D13, gate 4c).

- [ ] **Step 6: `web/api/calls.py` — the three endpoints keep their URLs, gain a REQUIRED `profile_id`**

`POST /calls/{id}/hide`, `DELETE /calls/{id}/hide`, `GET /calls/hidden` keep their
paths and response models (spec: *"Hidden is a re-skin"*), and each takes
`profile_id: int = Query(...)` (required — D3, D9; 422 without).

- **`hide_call`** validates through one helper, `_readable_active_profile_or_404(session, user, profile_id)`:
  exists AND `is_active` AND `can_read_profile` → else **404** (not 403: a profile
  the caller may not read must not be distinguishable from one that does not
  exist — that is `can_read_profile`'s contract). Then an upsert on the unique
  index with `ON CONFLICT DO UPDATE SET state='hidden', decided_at=now(),
  decided_by=:actor` — all **three** columns, because `decided_at` is the Saved /
  Hidden sort key (D12) and `DO NOTHING` would leave a saved call saved.
- **`unhide_call`** deletes `WHERE user_id = :me AND profile_id = :p AND call_id = :c AND state = 'hidden'`
  and does **NOT** validate the profile (T2-10): validating it makes every call
  hidden on a later-deactivated or un-shared profile permanently un-un-hideable.
  The `user_id` clause is the endpoint's only authorization control, and the
  docstring says so.
- **`list_hidden_calls`** filters `WHERE user_id = :me AND profile_id = :p AND state = 'hidden'`,
  ordered `decided_at DESC`; no read check, for un-hide's reason. No dedup, no
  union (D9).

- [ ] **Step 7: The two frontend callers**

`useHideCall`/`useUnhideCall` take `{ id, profileId }` and send `?profile_id=`.
`CallDetail.tsx` derives `profile` from `?profile=`; with none, the hide button
renders **disabled** with the text `Pick a profile to hide calls.` and issues no
POST (D3). The comment there names the reason: a decision belongs to a profile.

- [ ] **Step 8: Pin the two new read-rule surfaces BY HAND**

`tests/architecture/test_profile_access_single_source.py` claims an EXACT set of
surfaces that resolve a caller-supplied `profile_id`. Its `test_no_module_builds_a_second_read_predicate`
scans for a second HAND-ROLLED predicate; the pin tests are parametrized over the
pin tuples; so **a brand-new function that correctly calls `can_read_profile` is
invisible to every test in the file** (T2-13). Add `web/api/calls.py::_readable_active_profile_or_404`
to the predicate pins and `web/api/calls.py::hide_call` → `_readable_active_profile_or_404`
to the delegation pins, with a comment explaining why `unhide_call` and
`list_hidden_calls` are deliberately absent. Update the `test_pin_set_has_not_shrunk`
docstring's counts to what the sets now hold (the dry run found it stale at 11/1
against real sets of 13/6). Each pin is two parametrized cases (+4 tests for two
pins — the dry run measured exactly that).

- [ ] **Step 9: Gates** — `tests/test_migration_0071_call_decisions.py` and `tests/web/test_call_decisions_api.py`

**Migration gates.** Name what trivially passes first: **a fixture with one user and
one profile passes every wrong version of the backfill query.** Revision 2's fixture
asked one user to hold BOTH P3 and P5 as their default — impossible,
`users.default_profile_id` is one nullable scalar (T1-2). The fixture is:

| profile | active? | subscribed by | default of | must appear? |
| --- | --- | --- | --- | --- |
| P1 | yes | U1 | – | **yes** |
| P2 | **no** | U1 | – | no — inactive |
| P3 | yes | – | **U2** | **yes** — the default-only row |
| P4 | yes | U3 | – | no — another group's |
| P5 | yes | **U1** | **U1** | **yes, once** — subscribed *and* default |

U1 and U2 each have one dismissal; U3 subscribes to P4 and has none (proves the
migration does not fan one user's hide onto another's profile); U4 has a dismissal
and **no** profiles (the orphan). `profiles.created_by` is NOT NULL, so a fifth
"owner" user owns the five profiles. Seed with raw SQL at revision 0070 (the ORM's
`call_decisions` mapping has no table there). Expected: **exactly 3 rows** —
(U1,P1), (U1,P5), (U2,P3) — all `state='hidden'`, `decided_at` = `dismissed_at`,
`decided_by = user_id`; P5 appears **once**; the orphan count printed on stdout is
**1** and no row exists for U4; `inspector.has_table("user_call_dismissals")` is
False; the unique key rejects a duplicate `(user, profile, call)`; deleting
`decided_by`'s user `SET NULL`s the column while deleting `user_id`'s user cascades
the row; downgrade rebuilds the old table with distinct `(user_id, call_id)` pairs
and the orphan does not come back. Wrap every downgrade in `try/finally` that
re-upgrades — a test that leaves the shared container below head breaks every later
test on that xdist worker. (The dry run's file: 9 tests.)

**API gates**, all on "one user, two profiles, one call" unless stated. `saved`
rows are written straight through the session in this task (the save endpoint is
Task 3) — a gate that can only reach the states its own endpoint writes cannot see
the state predicate.

1. **Per-profile isolation.** Hide on P1: absent from the list for P1, present for P2.
2. **The digest respects it per profile:** hide on P1, the call still appears under P2's digest.
2b. **The digest keeps SAVED calls:** save on P1, still in P1's digest. The fixture
   must hold **a second candidate call**, and the test asserts `payload is not None`
   before indexing — with one call `assemble_for_user` returns `None` and the gate
   dies with an `AttributeError` for the wrong reason (T2-7).
3. **A saved call is not hidden:** save on P1, still in P1's archive list.
4. **Golden set ignores saved:** save a call ranked ≥ 60, not returned.
4b. **Golden set is scoped to the decision's profile:** hidden on PA only, ranked
   77 on PA and PB → the candidate set for that call is exactly `{(call, PA)}`.
4c. **A non-subscribed READER's hide is a candidate (D13):** profile owned by A, org
   visibility; user B (no subscription) hides; ranked 80 → `{(call, P)}` is returned.
5. **The hidden LIST is scoped by profile and state:** opposite states on P1/P2;
   `?profile_id=P1` lists it, `?profile_id=P2` does not.
5b. **The hidden list requires a profile (D9):** `GET /api/calls/hidden` → **422**;
   with `?profile_id=` → 200 and the row.
5c. **The hidden list does not leak a stranger's row:** user B's hidden row on the
   same call and profile is absent from A's list.
6. **Un-hide is scoped by profile and state:** `DELETE …/hide?profile_id=P1` leaves
   a `saved` row for the same call and P2's row untouched.
6b. **Un-hide does not touch a stranger's row:** two users, same call, same
   profile, both hidden; A's DELETE → 200, A's row gone, **B's row survives**
   (assert on B's rows directly — a `_states()` helper that filters on the caller's
   `user_id` cannot see the foreign row, which is how the dry run's first version
   was hollow, T2-14).
6c. **Un-hide after the profile is deactivated / un-shared still removes the row**
   (T2-10). The un-shared variant first asserts the POST now 404s, so the premise
   is self-checking.
7. **No consumer remains.** `! grep -rnE '\bUserCallDismissal\b' src/` — note
   `grep` exits 1 on no match, so the check must be written with `!`, and the old
   table name must not appear in the new model's docstring.
8. **Hide flips a saved call and rewrites three columns:** seed the saved row via
   raw SQL with `decided_by` = another user and `decided_at = 2020-01-01`; POST hide;
   exactly **one** row, `state='hidden'`, `decided_by` = the actor, `decided_at` > 2020.
8b. **Hide is idempotent within one profile** (second POST → 200, still one row).
9. **The write rule.** Hide **without** `profile_id` → 422 and no row; on an
   **unreadable** (private, un-shared) profile → 404 and no row; on an **inactive**
   profile → 404; on an **unknown id** (`999999`) → 404. Both dead-id fixtures are
   needed, and here is the real reason (T8-4 corrected revision 2's inversion): a
   single `SELECT … WHERE id = :id AND is_active` rejects both, but a check that
   OMITS `is_active` passes the inactive one while still rejecting the unknown one —
   the dry run's B31b (drop only `is_active`) reddened exactly the inactive-flavoured
   tests and nothing else.
10. **The four views partition the decisions:** on P1 with one hidden, one saved,
   one undecided call: omitted view == `archive` == {saved, undecided}; `inbox` ==
   {undecided}; `saved`; `hidden`; and `?view=nonsense` → 422.
11. **No-profile archive suppresses a call hidden on ANY profile** (today's rule).
11b. **No-profile Saved/Hidden are EMPTY (D8):** user with no default; save on P1,
   hide on P2; `?view=saved` and `?view=hidden` with no `?profile=` → 200,
   `effective.profile_id is None` (self-check), `items == []`, `pagination.total == 0`;
   positive control: the same views WITH the profile return their row.
12. **Saved and Hidden keep closed calls; the inbox does not (D1):** four calls, three
   with a 2020 deadline: open+saved, closed+saved, closed+hidden, closed+undecided.
   `?view=saved` lists both saved; `?view=hidden` lists the hidden; `?view=inbox`
   is empty by default and lists the closed undecided call under `show_closed=true`.
13. **Saved is ordered most-recent-decision-first (D12):** two calls, the LATER-published
   one saved with the EARLIER `decided_at` (raw SQL) → `?view=saved` returns the
   other first. The fixture must make the score/newest order and the decision order
   disagree, or the gate is the "absence already true" shape.
14. **Frontend (vitest, `CallDetail.test.tsx`):** in the full edition with `?profile=7`
   the hide POST carries `profile_id: 7`; without `?profile=` the button is disabled,
   the hint text is present, and after an awaited flush no POST was made.

**What the dry run measured about red phases (T2-5):** gates 3 and 6 pass GREEN
against the pre-change code (it reads the old table and never sees a decision row).
Only the mutation runs prove them; do not spend time hunting for a red phase on
those two.

- [ ] **Step 10: Mutation-test each gate**

Each row's *Measured* column is the dry run's ledger (`PLAN-DEFECTS.md`) or a Phase
2 re-run against `49969f63` on 2026-09-05; `PREDICTED (not run)` means the gate is
new to this revision and its red side could not be produced on the dry tree.

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B1 | migration: drop `AND p.is_active = true` | P2 appears — 4 rows | `2 failed, 5 passed` — got 4 rows |
| B2 | drop `OR p.id = u.default_profile_id` | P3 missing — 2 rows | Phase 2 re-run: `Got [(2, 1, 1), (2, 5, 1)], expected [(2, 1, 1), (2, 5, 1), (3, 3, 1)]`; `expected the 3 backfilled rows, got 2`; orphan-count and downgrade gates also red |
| B3 | replace the JOIN predicate with `p.is_active = true` | the query stops being per-user: `active profiles × dismissals` rows (12 in this fixture; U4 gains rows) | `4 failed, 3 passed` — `u4 must have no decision row; got 4` |
| B4 | `'hidden'` → `'saved'` | the state assertion | `1 failed, 6 passed` |
| B4b | delete `ON CONFLICT … DO NOTHING` | **nothing** — record that it passes; that is the evidence the clause is inert | `7 passed` |
| M1 | unique key → `(user_id, call_id)` | the uniqueness gate | `4 failed, 5 passed` |
| M2 | `decided_by` `SET NULL` → `CASCADE` | the SET NULL gate | `1 failed, 8 passed` |
| B5 | dashboard: drop `CallDecision.profile_id == profile_id` | gate 1 | `1 failed, 12 passed` |
| B6 | archive branch drops the state predicate | gate 3 + gate 10 | `2 failed, 11 passed` |
| B7 | assemble: one user-wide set | gate 2 | `1 failed, 29 passed` |
| B7b | assemble: drop `state == "hidden"` | gate 2b **and not gate 2** — run it against gate 2 too and record that gate 2 stays green | `1 failed, 29 passed`; gate 2 green |
| B8 | golden set: drop the state filter | gate 4 | `1 failed, 5 passed` |
| B8b | hidden list: drop the profile predicate | gate 5 | `1 failed, 12 passed` |
| B8c | un-hide keyed on `(user_id, call_id)` | gate 6 | `2 failed, 11 passed` |
| B8d | un-hide: drop `CallDecision.user_id == user.id` | gate 6b | dry fix `ea07a172` F1a: `1 failed` |
| B8e | hidden list: drop `user_id` | gate 5c | dry F1c: `1 failed` |
| B8f | re-add the profile validation to `unhide_call` | gate 6c | dry `93927bdd`: `2 failed` |
| B8g | golden set: re-add a subscription predicate (`CallDecision.user_id.in_(select(Subscription.user_id).where(Subscription.profile_id == CallRanking.profile_id))`) | gate 4c | Phase 2: `AssertionError: a reader who is not subscribed hid this call on the profile; the golden set must see it` — `1 failed` |
| B9 | hide: `DO UPDATE` → `DO NOTHING` | gate 8 | `1 failed, 12 passed` |
| B9b | `useHideCall` stops sending `profile_id` | gate 14 | CallDetail.test (full) + calls.test fail |
| B9c | `hide_call` stops calling `_readable_active_profile_or_404` | gate 9 (unreadable, inactive, unknown) **and** the Step 8 delegation pin | Phase 2 on the sibling `save_call`: `test_pinned_delegate_still_delegates[web/api/calls.py::save_call]` — `1 failed, 46 passed` |
| B9d | `list_hidden_calls`: `profile_id` back to `Query(default=None)` | gate 5b | dry tree IS this mutant: `GET /calls/hidden without profile_id answered 200` — `1 failed` |
| B9e | `_decision_predicate`: for saved/hidden with `profile_id is None`, return the user-wide `in_` (the union) | gate 11b | dry tree IS this mutant: `?view=saved with no profile widened to the ANY-union: ['DecisionCall0']` — `1 failed` |
| B9f | keep the `open_expr` append for saved/hidden | gate 12 | dry tree IS this mutant: `Saved dropped a call because its deadline passed: ['DecisionCall0']` — `1 failed` |
| B9g | order saved/hidden by the inbox's score order | gate 13 | dry tree IS this mutant: `Saved is in the inbox's order, not most-recent-decision-first: ['DecisionCall1', 'DecisionCall0']` — `1 failed` |
| B9h | drop `if view != "archive": raise` | nothing observable through HTTP (the Literal guards it) — record it; the tripwire is for callers inside the package | PREDICTED (not run): green |

**Green side of B9d–B9g:** Phase 2 applied the two-line D8 + D1 patch to
`dashboard.py` on the dry tree (`if profile_id is None: return Call.id.in_([])`
under the saved/hidden branch; `and view not in ("saved", "hidden")` on both
`open_expr` appends) and ran gates 11b and 12 together with the dry tree's own
four-views, no-profile-archive and counts-parity gates: `6 passed, 33 deselected`.
The D12 ordering patch was **not** built in Phase 2; gate 13's green is
`PREDICTED (not run)`.

- [ ] **Step 11: Verify**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/call-match-r1b
uv run pytest tests/test_migration_0071_call_decisions.py tests/web/test_call_decisions_api.py tests/test_schema_drift.py -q
uv run pytest -n 16 --dist loadfile
uv run pytest tests/architecture -q
! grep -rnE '\bUserCallDismissal\b' src/ && echo "no consumers left"
uv run ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py
test -n "$(git diff --name-only -- docs/ARCHITECTURE.md)" || { echo "ARCHITECTURE.md did not change — the generator did not run"; exit 1; }
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || { echo "schema.gen.ts did not change — hide gained a parameter, it must"; exit 1; }
python3 - <<'EOF'   # the required-parameter SHAPE, not just "the file moved" (T8-3)
import re, sys
src = open("frontend/src/api/schema.gen.ts").read()
bad = [op for op in ("hide_call_api_calls__call_id__hide_post", "unhide_call_api_calls__call_id__hide_delete", "list_hidden_calls_api_calls_hidden_get")
       if not re.search(re.escape(op) + r": \{\s*parameters: \{\s*query: \{\s*profile_id: number;", src)]
sys.exit(f"profile_id is not REQUIRED on {bad}" if bad else 0)
EOF
pnpm -C frontend test && pnpm -C frontend lint && pnpm -C frontend build
pnpm -C frontend e2e:match
git diff --quiet ed1153a3 -- docs/superpowers/specs/ && echo "specs untouched"
```

`test_schema_drift.py` is the model/migration agreement gate (it replaces `alembic
check`). The required-shape script was run against known-bad input in Phase 2. With
hide's `query: {` edited to `query?: {` and `profile_id?: number;` it prints
`hide_call_api_calls__call_id__hide_post: NOT REQUIRED` and exits 1. And run
exactly as written above against the dry tree's own schema it prints
`profile_id is not REQUIRED on ['list_hidden_calls_api_calls_hidden_get']` and
exits 1 — the dry tree never applied D9, so that is the B9d mutant, caught.

**Test delta from `5029 passed, 1 skipped`:** measure it. The dry run's Tasks 1+2
together added +32 (9 migration + 13 API + 2 digest + 2 golden set + 2 F1 gates + 4
pin cases) with a gate set that differs from this one by about ten tests. Write the
number the run prints.

---

### Task 2: (merged into Task 1)

Revision 2's Task 2 — "re-point every consumer" — is Task 1 Steps 3–7 above. There
is no separate commit. This heading stays so `PLAN-DEFECTS.md`'s `T2-*` entries
still resolve to something.

---

### Task 3: Save — the new half of the pair

Spec §4.1: *"**Saved** is the genuinely new half."* Hidden was re-pointed in Task 1;
this task adds the other direction, the counts the rail needs, and the undo path a
keypress-driven UI requires.

**Files:**
- Modify: `src/call_manager/web/api/calls.py` — `save_call`, `unsave_call`, `get_decision_counts`, `CallDecisionOut`, `CallDecisionCountsOut`
- Modify: `src/call_manager/web/dashboard.py` — `_base_call_filters`, `count_decision_views`
- Modify: `tests/web/test_call_decisions_api.py`
- Modify: `tests/architecture/test_profile_access_single_source.py` — pin `save_call` and `get_decision_counts` (delegation pins, by hand — T2-13)
- Modify: `tests/architecture/full_edition_routes.txt` (regenerate, 107 → 109) **and** the `== 107` literal in `tests/architecture/test_edition_surface.py` (T3-5)
- Regenerate: `frontend/src/api/schema.gen.ts` — **three** new operations (`save … post`, `unsave … delete`, `get_decision_counts … get`; revision 2 said four, a leftover from the dropped `GET /calls/saved` — T3-4)

**Interfaces:**
- Consumes: `CallDecision`, the four views, `_decision_predicate` (Task 1).
- Produces: the endpoints Task 7's feed calls.

- [ ] **Step 1: Endpoints**

| Method | Path | Query | Validates the profile? | Returns |
| --- | --- | --- | --- | --- |
| `POST` | `/api/calls/{call_id}/save` | `profile_id` **required** | yes — `_readable_active_profile_or_404` | `CallDecisionOut{state: "saved"}` |
| `DELETE` | `/api/calls/{call_id}/save` | `profile_id` **required** | **no** (T2-10; user-scoped delete) | `CallDecisionOut{state: null}` |
| `GET` | `/api/calls/decisions/counts` | `profile_id` **required**, plus `source`, `q`, `show_closed` | yes | `{inbox, saved, hidden}` |

`POST …/save` upserts with `DO UPDATE SET state='saved', decided_at=now(),
decided_by=:actor` (all three columns — D4, D12). `DELETE` removes the `saved` row
only — back to undecided. Two paths are added in total.

**Route order — the measured fact replaces revision 2's paragraph.** Starlette
compiles `/api/calls/{call_id}` to `^/api/calls/(?P<call_id>[^/]+)$`; `[^/]+`
cannot cross a slash. Measured in Phase 2:

```
/api/calls/decisions/counts False     <- can never be captured by /calls/{call_id}
/api/calls/hidden           True      <- the one path whose correctness depends on order
/api/calls/counts           True
```

So `/calls/decisions/counts` (three segments) is safe **wherever it is declared**,
and revision 2's B11 ("move it below `/calls/{call_id}` → 422") was an impossible
mutation with a gate behind it that asserted nothing (T3-1: the dry run moved it and
got `28 passed`, request `200`). The path that genuinely depends on declaration
order is Task 1's `/calls/hidden` (two segments), declared above `/calls/{call_id}`
today; Task 1's gate 5 asserts a 200 there, and a reorder turns that into a 422
(`int_parsing`). Revision 2's own "measured" label on the wrong path is the process
failure `measure-dont-predict` exists to stop; it is recorded, not deleted.

- [ ] **Step 2: The counts endpoint is where inbox-zero comes from**

`count_decision_views` calls `_base_call_filters(view, …)` **three times, once per
view** (no single "the list predicate" call can both exclude only hidden and
exclude every decision), over the same filters the list applies with the **same
names** — `source`, `q`, `show_closed` — and applies `open_expr` exactly as the list
does: to `inbox` unless `show_closed`, and **never** to `saved`/`hidden` (D1). It
does **not** accept `min_score` (a score floor needs the ranking join and
`score_floor_active`'s three-way rule; duplicating it here is how the rail and the
feed drift — D18; the docstring says so). `_base_call_filters` is the ONE
definition of the call-matching filters; its docstring forbids a second copy.

- [ ] **Step 3: Gates**

1. `save` then `?view=saved` returns it; `?view=hidden` and `?view=inbox` do not.
2. `hide` a **saved** call → leaves saved, enters hidden, exactly **one** row
   (`len(rows) == 1` on a LIST, not a dict keyed by profile — a dict collapses a
   duplicate), all three columns rewritten (seed via raw SQL with a foreign
   `decided_by` and `decided_at = 2020`).
2b. `save` a **hidden** call → the mirror. *Not decorative:* gate 2's first `save`
   hits an empty row where `DO UPDATE` and `DO NOTHING` are indistinguishable, and
   the flip to hidden is done by `hide_call`; without 2b, `save_call` regressing to
   `DO NOTHING` returns 200 while leaving the call hidden.
3. `DELETE …/save` on an undecided call → 200 (idempotent undo).
3b. `DELETE …/save` does not touch a `hidden` row.
3c. **`DELETE …/save` does not touch a stranger's saved row** on the same call and
   profile (two users; assert on the stranger's rows directly).
3d. `DELETE …/save` after the profile is deactivated still removes the row.
4. **Each count equals `pagination.total` of its own view** — `pagination.total`,
   never `len(items)` (page-capped at `limit`; revision 2 offered both — T3-12) —
   on an asymmetric fixture (4 inbox / 2 saved / 1 hidden) that includes **a
   past-deadline call in each of the three states**, a call under a **second
   source**, and a call with a **unique title word**; asserted under the default,
   `show_closed=true`, `source=`, and `q=`. Under D1 the fixture must show:
   `inbox` differs between default and `show_closed=true`; `saved` and `hidden` do
   **not**. *What trivially passes:* `0+0+0 == 0`; `inbox+saved+hidden == total`
   (passes with saved and hidden swapped); a fixture with `deadline=None`
   everywhere and one source (the dry run's first version — T3-2, T3-14 — stayed
   green with `open_expr` deleted from counts and `source_id` hard-coded `None`).
5. `GET /api/calls/decisions/counts` → 200 with the three keys. (No order claim.)
6. Another user's decision is invisible: seed a second user's save on the same
   call/profile; absent from the first user's `?view=saved` **and** their counts.
7. Counts / save on an **unreadable**, **inactive**, or **unknown** `profile_id` →
   404 and no row (the save-on-inactive case is the one the dry run's first pass
   missed — T8-6); save **without** `profile_id` → 422 and no row.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B10 | `save_call`: `DO UPDATE` → `DO NOTHING` | gate 2b — **and record that gate 2 stays green** | `1 failed, 27 passed`; gate 2 green |
| B11 | move `/calls/hidden` (Task 1's route) below `/calls/{call_id}` | Task 1 gate 5 — a **422** `int_parsing`, not a 404 | PREDICTED (not run) — the dry reviewer read it, nobody ran it |
| B12 | counts: `saved` computed with the `hidden` predicate and vice versa | gate 4 + gate 6 | `2 failed` |
| B13 | drop `user_id` from `_decision_predicate` | gate 6 | `1 failed` |
| B13b | `_decision_predicate` returns the `archive` branch for `inbox` | Task 1 gate 10; later Task 7's feed tests | `4 failed` |
| B13c | `unsave_call`: drop `CallDecision.user_id == user.id` | gate 3c | Phase 2 re-run: `un-save must be scoped by user_id - a stranger's saved row on the SAME call and profile must survive another user's un-save` — `assert {} == {1: 'saved'}` |
| B13d | counts: delete the `open_expr` append for `inbox` | gate 4 (default vs `show_closed`) | dry F2a: `1 failed` |
| B13e | counts: hard-code `source_id = None` / `q = None` | gate 4 (`source=`, `q=`) | dry F2b: `1 failed` |
| B13f | counts: apply `open_expr` to `saved`/`hidden` too | gate 4 (the D1 half) | Phase 2, via the list: `the rail disagrees with the views` — under the dry tree's counts, `{"inbox": 0, "saved": 1, "hidden": 1}` vs expected `saved: 2` |
| B13g | `save_call` stops calling `_readable_active_profile_or_404` | gate 7 **and** the delegation pin | Phase 2: `web/api/calls.py::save_call no longer calls _readable_active_profile_or_404()` — `1 failed, 46 passed`; dry F4: `2 failed` on gate 7 |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_call_decisions_api.py -q
uv run pytest tests/architecture -q        # manifest, the == 109 literal, the pin sets — this is the suite that goes red on this task (T3-13)
uv run pytest -n 16 --dist loadfile
git diff dcd4ddbd -- tests/architecture/full_edition_routes.txt   # exactly +/api/calls/decisions/counts and +/api/calls/{call_id}/save
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || { echo "schema.gen.ts did not change - three operations were added, it must"; exit 1; }
# the required-shape script from Task 1 Step 11, with the two save ops and the counts op added to its list
uv run ruff check src tests alembic scripts
```

**Test delta:** measure. The dry run's Task 3 added +17 (13 tests + 4 pin cases) and
its fix commit +4; `tests/architecture` moved 116 → 120 there.

---

### Task 4: The list schema grows the fields the card needs

Spec §5. `CallOut` (anchor: `class CallOut`) carries `id`, `title`, `deadline`,
`deadline_local_date`, `published_at`, `source_name`, `source_slug`,
`aggregate_score`. Everything else the card shows lives on `CallDetailCallOut`.
**This is real backend work, not free.**

**Files:**
- Modify: `src/call_manager/web/api/calls.py` — `CallOut`, `CallListCriterionOut`, `get_calls`, `get_call_detail` (the `ORDER BY`), the shared criterion helper
- Create: `tests/web/test_calls_list_query_count.py`
- Modify: `tests/test_api_calls.py` — `test_api_calls_returns_seeded_call_shape` asserts exact key-set equality on `CallOut` and **necessarily** goes red here (T4-2)
- Regenerate: `frontend/src/api/schema.gen.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces: the card's data — consumed by Tasks 5 and 7.

- [ ] **Step 1: Add the fields**

To `CallOut`: `programme`, `budget_min`, `budget_max`, `currency`, `url`, `summary`,
`keywords`, `criteria: list[CallListCriterionOut]`, `applicant_note_count: int`,
`escalated: bool`.

**`CallListCriterionOut` carries** `criterion`, `label`, `rating`, `is_topic_fit`,
`is_reason_code`, `rationale`, `quoted_evidence` — everything the detail's criterion
type has minus `detail` (T4-9). `quoted_evidence` and `rationale` are on the list
because Task 5's chip expansion renders both. One helper (`_criterion_common_fields`
in the dry run) builds these fields for the list AND the detail page, so "Budget" is
called the same thing in both.

**Safe on the raiseload contract:** `build_dashboard_view` defers `embedding`,
`full_text` and `raw` with `raiseload=True` — and **only** those. Every field added
here is a plain column.

**Payload (D10):** the growth risk is `criteria`, not `summary`. Populated only when
a profile is selected. Print `len(resp.content)` for a 50-row ranked page with 7
chips per row, before and after this task, and put both in the commit message.

- [ ] **Step 2: One batched criteria query, ordered — the part that can go quietly wrong**

`view.calls` is `list[tuple[Call, CallRanking | None]]`: the ranked query is an
outer join and the unranked branch selects `(Call, None)`, so `ranking` is `None`
for a normal, common row:

```python
applicant_note_count = 0 if ranking is None else len(ranking.applicant_notes or [])
escalated = bool(ranking is not None and ranking.escalated)
```

`escalated` is its own column and is **not** derivable from the note count
(`CallRanking.escalated` can be true with zero notes).

Criteria need exactly **one** more query, after the page is known, **inside the same
`with sf() as session` block** that produced `dashboard.calls` (detached instances
otherwise), keyed on the **effective** profile id (`dashboard.effective["profile_id"]`):

```python
criteria_by_call: dict[int, list[CallRankingCriterion]] = defaultdict(list)
if criteria_profile_id is not None and call_ids:
    for crit in session.execute(
        select(CallRankingCriterion)
        .where(
            CallRankingCriterion.profile_id == criteria_profile_id,
            CallRankingCriterion.call_id.in_(call_ids),      # the PAGE, not the profile
        )
        .order_by(CallRankingCriterion.criterion)            # D5: deterministic; the detail query gets the same ORDER BY
    ).scalars():
        criteria_by_call[crit.call_id].append(crit)
```

Add the same `.order_by(CallRankingCriterion.criterion)` to `get_call_detail`'s
criteria query. Pin **all four** members of `_REASON_CODE_CRITERIA` (the dry run
found only `budget` and `embedding` pinned; dropping `timeline` or `trl` stayed
green — T4-10).

- [ ] **Step 3: The gates**

1. **No-N+1, as an equality with a positive control.** Count statements with a
   `before_cursor_execute` listener on **the app's own engine** (`create_app()` builds
   its own; seeding on `pg_session_factory`'s engine never lands in the count). Burn
   the first request (dialect init and pool warm-up are cursor executes too). Seed 1
   ranked call → N1; seed 9 more with three criteria each → N10; **assert N10 == N1**,
   and **in the same test assert the ranked row came back WITH its three chips**.
   Revision 2 said "record that B16d (drop `?profile=`) passes, as proof the
   precondition is real" — true only for the weaker gate it specified: the dry run
   ran that gate verbatim with the per-row defect planted and it **passed**
   (`1 passed` — the defect escapes silently, T4-1). The positive chip assertion is
   what makes B16d red. Seed at least one **unranked** call too.
1b. **The precondition, pinned:** without `?profile=` the row has `criteria == []`;
   with it, chips. (The vacuity test, as its own fact in the suite.)
2. **Chips come from the selected profile only:** two profiles rank the same call
   differently; each `?profile=` shows only its own chips, and never the other's
   `rationale`.
3. **Labels and flags match the detail page** field by field.
4. **NULL `applicant_notes`** (the local Gemma path always leaves it NULL — the
   common case) counts 0; **unranked** call counts 0 and `escalated=False`;
   `escalated=True` with zero notes is still escalated.
5. **Criteria are ordered identically on list and detail (D5) — with a forced
   sequential scan.** Seed rows OUT of order; wrap the requests in
   `SET enable_indexscan = off; SET enable_bitmapscan = off` on the app's engine;
   assert list order == detail order == sorted. Without the forced scan the
   composite-PK index returns rows already sorted and removing `ORDER BY` is
   unobservable (T4-11: the dry run's first version passed with the `order_by`
   removed).
6. **The criteria query reads only the page (F-12 #3).** Ten ranked calls with one
   criterion each; `?profile=&limit=2`; an `after_cursor_execute` listener records
   `cursor.rowcount` for every statement mentioning `call_ranking_criteria`; assert
   `rows == [2]`. The statement-count gate cannot see this: dropping the
   `call_id.in_(call_ids)` clause is still one statement, `criteria_by_call` is
   keyed by `call_id` so off-page rows are never read, and the response is
   byte-identical — an unbounded scan of the profile's whole criteria table under a
   green no-N+1 gate.
7. `tests/test_api_calls.py`'s key-set pin updated to the new `CallOut` keys.

Use a **disposing `make_app` fixture** (`engine.dispose()` in teardown): the
`create_app()`-per-test recipe otherwise emits `ResourceWarning: <psycopg.Connection …>
was deleted while still open` via the unraisable hook, ×10 on a green run (T4-4).

- [ ] **Step 4: Mutation-test**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B14 | per-call `select(...)` inside the comprehension | gate 1 — N10 = N1 + 9 | `1 failed, 10 passed` — N1 = 16, N10 = 25 |
| B15 | drop `CallRankingCriterion.profile_id == …` | gate 2 | `1 failed` |
| B16 | `len(ranking.applicant_notes)` without `or []` | gate 4 — and most of the file, NULL notes are on nearly every row | `8 failed, 3 passed` |
| B16b | drop the `ranking is None` guard | gate 4 (unranked), gate 1 | `3 failed, 8 passed` |
| B16c | `escalated = note_count > 0` | gate 4 (both directions) | `2 failed` |
| B16d | gate 1's URL without `?profile=` | gate 1's positive chip assertion | `1 failed` — `assert [] == ['budget', 'eligibility', 'topic_fit']`; **the same mutation against revision 2's count-only gate with B14 planted: `1 passed`** |
| B16e | drop the LIST's `.order_by(CallRankingCriterion.criterion)` | gate 5 | Phase 2 re-run: `assert ['trl', 'budget', 'topic_fit'] == ['budget', 'topic_fit', 'trl']` — `1 failed` |
| B16f | drop `CallRankingCriterion.call_id.in_(call_ids)` | gate 6 | Phase 2: `the criteria statement returned [10] row(s) for a 2-row page over 10 ranked calls` — `1 failed` |
| B16g | drop `"timeline"` from `_REASON_CODE_CRITERIA` | gate 3 | dry `a9c26407`: `1 failed` |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_calls_list_query_count.py tests/test_api_calls.py -q
uv run pytest -n 16 --dist loadfile
uv run pytest tests/architecture -q          # ARCHITECTURE.md must NOT move here (no schema change); the drift gate says so
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || { echo "schema.gen.ts did not change - CallOut grew, it must"; exit 1; }
pnpm -C frontend build                        # the types moved; tsc is the only thing that checks the consumers
pnpm -C frontend test && pnpm -C frontend e2e:match
```

**Test delta:** measure. The dry run's Task 4 added +11 (+1 in its fix commit).

---

### Task 5: Criterion chips

Spec §4.2, list view only. The detail page already renders one card per criterion
(anchor: the `criteria` map in `CallDetailBody.tsx`) — **do not touch that**.

**Files:**
- Create: `frontend/src/components/CriterionChips.tsx`, `.module.css`, `.test.tsx`
- Create: `tests/architecture/test_criterion_vocabulary.py`

**Interfaces:**
- Consumes: `CallOut.criteria` (Task 4).
- Produces: the chip row — consumed by Task 7's card. **Not in the match bundle until Task 7** (T7-5).

- [ ] **Step 1: Build to the measured vocabulary, not the spec's**

```tsx
// The reading order of the chip row, and the only place in the SPA that
// enumerates the vocabulary. tests/architecture/test_criterion_vocabulary.py
// pins it against ranking/worker.py -- both sources.
export const CRITERIA = ["topic_fit", "eligibility", "exclusions", "timeline", "budget", "trl", "embedding"] as const;
type Tone = "pass" | "fail" | "unsure";
// Rating -> tone. Measured from ranking/criteria.py::GateStatus and prompt.py's
// validators, NOT from spec 4.2. Pinned by the same architecture file.
const TONE: Record<string, Tone> = { pass: "pass", fail: "fail", unclear: "unsure", unknown: "unsure" };
// topic_fit is "1".."5": rendered `${label} ${n}/5`, toned pass at >= 4, else unsure (D6).
```

- `label` is **server-computed** (`web/filters.py::criterion_label`, set on every
  list row by the shared helper); the component renders `${label}` and never
  derives it. A criterion this build has never heard of renders at the **end** of
  the row with its server label (`criterion_label` Title-cases an unknown key —
  `co_funding` renders as `Co Funding ✓`, fully legible; "a blank chip" was never
  possible — T5-2). An **unrecognised rating** renders as `unsure` with the raw
  string, never blank.
- Render **1..7 chips, and handle 0** (D-B3). Zero chips renders nothing.
- Chips carry `data-criterion` and `data-tone` — `getByText("Timeline ✗")` does
  not match a chip whose symbol is in a child `<span>`; use `toHaveTextContent` or
  the data attributes (T5-7).
- **Expansion:** `quoted_evidence` in a `<blockquote className={styles.quote}>`
  (Georgia via the `.quote` rule), then `rationale` in a **sibling** `<p>` after it,
  labelled "The model's reasoning:" — or "Reason code:" with the value in `<code>`
  for a reason-code criterion. Never interleaved, never nested.

- [ ] **Step 2: Pin BOTH halves of the vocabulary against the backend**

`tests/architecture/test_criterion_vocabulary.py` parses (via `ast` / regex, from
the REAL files — never a hand copy):

- **criteria:** `web/filters.py::_CRITERION_LABELS` keys; `ranking/worker.py`'s
  `gate_results` tuple first elements **and** its `criterion="…"` keyword literals
  (a `criterion=` scan alone sees **3 of 7** — `criterion=name` in the gate loop is a
  variable); `CriterionChips.tsx`'s `CRITERIA` with comments stripped. Assert
  **coverage** (every backend criterion is in the frontend list), the reverse
  (the frontend invents none), and the label map covers the backend. Plus positive
  controls pinning what each scanner finds (`{"budget","trl","timeline","embedding"}`;
  `{"topic_fit","eligibility","exclusions"}`; disjoint; 7 labels; a real list with
  `topic_fit`), a commented-out-entry control, a renamed-list refusal, and two
  proof-by-breaking controls on doctored sources.
- **ratings (F-5 — revision 2 promised this half and never specified it):**
  `ranking/criteria.py::GateStatus` string members ∪ every string in a
  `<name> not in ("pass", "fail", …)` validator tuple in `ranking/prompt.py`, versus
  the keys of `CriterionChips.tsx`'s `TONE` block. Assert coverage; pin the scanners
  (`{"pass","fail","unknown"}` and `{"pass","fail","unclear"}`).

Phase 2 wrote and ran the rating half against the dry tree:

```
=== good input:  2 passed in 0.68s
=== PARTIAL = "partial" added to GateStatus:
E  AssertionError: the backend can write rating ['partial'], which CriterionChips.tsx's TONE map does not know
   -- the chip falls through to the 'unsure' tone and prints the bare word where a symbol belongs
```

**Known blind spot, written down (T5-6):** the criterion union sees a name only as
a string literal in the first element of a `gate_results` tuple or as a `criterion=`
keyword; `crit = "bar"; criterion=crit` is invisible (dry run B18c: `12 passed`,
MISSED as expected). It fails **closed** on a renamed `gate_results` (B22: `6
failed`, "no assignment to 'gate_results'"). A legitimate eighth criterion reddens
**four** exact-count controls at once — deliberate; equality, not a floor.

- [ ] **Step 3: Mutation-test**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B17 | delete `"embedding"` from `CRITERIA` | coverage gate | `2 failed, 10 passed` |
| B18 | add `criterion="foo"` to `worker.py` | coverage gate | `5 failed, 7 passed` |
| B18b | add `("foo", r.gates.foo)` to `gate_results` — the way the real four are declared | coverage gate (proves the tuple scan exists) | `5 failed, 7 passed` |
| B18c | `crit = "bar"; criterion=crit` | **nothing** — record the miss | `12 passed` |
| B19 | delete `unknown` from `TONE` | a chip test asserting the rendered **TEXT** for `rating="unknown"` (`Technology Readiness Level ?` → `… unknown`). A tone-only assertion stays GREEN: the fallthrough tone IS `unsure` (T5-1) | Phase 2 re-run: `× gives BOTH not-judged ratings the unsure tone and the '?' symbol` — `1 failed, 13 passed` |
| B20 | `criteria.slice(0, 6)` | the 7-chip test | `1 failed, 13 passed` |
| B20b | move `styles.quote` off the blockquote (`className={styles.reasoning}`) | the expansion test that asserts the blockquote's class contains `quote` | Phase 2 re-run: `× shows the funder's quote FIRST and the model's reasoning after it, labelled` — `1 failed, 13 passed` |
| B20c | nest the reasoning `<p>` INSIDE the `<blockquote>` | the sibling assertion (`not.toContainElement` + same parent — **not** a `compareDocumentPosition` bitmask, which is also truthy for containment, T5-9) | Phase 2 re-run: `2 failed, 12 passed` |
| B20d | `PARTIAL = "partial"` on `GateStatus` | the rating coverage gate | Phase 2: `1 failed` (above) |
| B21 | a broken prop signature | **vitest and oxlint stay green**; only `pnpm -C frontend build` (`tsc`) goes red | `14 passed`, oxlint exit 0, `tsc` exit 2 |

- [ ] **Step 4: Verify**

```bash
pnpm -C frontend test src/components/CriterionChips.test.tsx
uv run pytest tests/architecture/test_criterion_vocabulary.py -q
uv run pytest tests/architecture -q
pnpm -C frontend lint
pnpm -C frontend build                                   # tsc — the only typecheck (T5-3)
pnpm -C frontend build:match && pnpm -C frontend check:bundle   # numbers UNCHANGED here: the chips are not in the match graph until Task 7
pnpm -C frontend e2e:match
```

vitest gotchas the dry run hit (T5-7): `import css from "./x.module.css?raw"`
returns the CSS-modules proxy under `css: false` (TypeError);
`readFileSync(new URL(..., import.meta.url))` fails under jsdom (`http:` base); the
working form is `readFileSync("src/components/CriterionChips.module.css")` (vitest
root is `frontend/`). The Mantine wrapper is `renderWithProviders` in
`frontend/src/test-utils.tsx` (T5-8).

**Delta:** the dry run's Task 5 added +12 backend (the architecture file) and +14
vitest; with the rating half, expect more — measure.

---

### Task 6: Gate every raw aggregate out of the match detail page

Spec §4.2: *"the raw aggregate is not shown in the UI"*. `CallDetail` is mounted by
**both** routers, so this is a `{!isMatchEdition && (…)}` gate — **not** a deletion,
whatever the spec's verb; deleting would strip the score from the full edition.

**Files:**
- Modify: `frontend/src/routes/CallDetailBody.tsx`, `CallDetailBody.test.tsx`

- [ ] **Step 1: Three guards, not one.** Anchor on strings: the
  `{ranking.aggregate_score}` figure (and its band colour, which goes with it), the
  `Aggregate score` heading, and the `Escalated from {ranking.escalated_from_score}`
  note — `escalated_from_score` IS an aggregate (`rank_pair.py`:
  `escalated_from_score=local_agg.score`); a build to revision 2's enumeration ships
  "Escalated from 55" on every escalated call in match (T6-1). Keep the reason
  badge (`aggregate_reason_label` — a reason, not a number), the model
  attribution (`Analyzed by …`), the rationale, the summary and the notes. **If
  everything left inside the ranking card is null in match, do not render the
  bordered card at all** (F-11's empty "Ranking" card; gate 4, `PREDICTED`).
- [ ] **Step 2: Test both editions, with the presence assertion INSIDE the gated section.**
  Mutable holder + getter mock (`Dashboard.test.tsx` pattern). In `match`: `Aggregate
  score` absent, `/Escalated from/` absent, the figure absent — **and**
  `Analyzed by Sonnet 4.6` **and** the rationale text present. Revision 2 said
  "assert some other detail-page string is present, proving the component
  rendered"; the dry run took the page title, gated the whole section out of match,
  and got `7 passed` (T6-2). The presence string must be rendered by the same
  `{ranking && …}` section the guards live in. In `full`: all three present.
- [ ] **Step 3: The reason badge needs a fixture that renders it.** Every fixture in
  the repo has `aggregate_reason_label: null`, so the badge branch never rendered in
  any test in either edition and a future `!isMatchEdition &&` on it reddens nothing
  (T6-5). Add a fixture with a non-null label and assert the badge in `match`.
- [ ] **Step 4: Mutate.**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B21a | remove the figure's guard | the match figure assertion | `1 failed` |
| B21b | remove the heading's guard | the match heading assertion | `1 failed` |
| B21c | remove the escalation note's guard | the match `/Escalated from/` assertion | `1 failed` |
| B22 | the whole `{ranking && …}` section → `null` | the **full** assertions — and two pre-existing `data-band` tests. A run reddening only the full assertion means the pre-existing tests were removed (T6-3) | `4 failed` |
| B22b | the whole section under `!isMatchEdition &&` | the in-card presence assertion in match, and the reason-badge test | Phase 2 re-run: `× the MATCH edition keeps the reason badge`, `× the MATCH edition shows no raw aggregate, and still renders the rest of the ranking card — Unable to find an element with the text: Analyzed by Sonnet 4.6` — `2 failed`. **With the presence assertion on the page title instead (revision 2's wording): `7 passed`, survived.** |
| B22c | `!isMatchEdition &&` on the reason Badge | the match badge test | dry `b2832856`: `1 failed` — match assertion only |
| B22d | render the empty bordered card in match when everything inside is gated/null | gate 4 | PREDICTED (not run) |

- [ ] **Step 5: Verify.** `pnpm -C frontend test src/routes/CallDetailBody.test.tsx`,
  `pnpm -C frontend lint`, `pnpm -C frontend build`,
  `pnpm -C frontend build:match && pnpm -C frontend check:bundle` (a runtime gate —
  numbers unchanged), `pnpm -C frontend e2e:match`.

---

### Task 7: The triage feed

Spec §5. The researcher's screen is the product: a single ~680 px expanding column,
a 240 px left rail for orientation, `j`/`k`/`s`/`h`/`u`, and an inbox that ends.

**Files:**
- Create: `frontend/src/routes/TriageFeed.tsx`, `.module.css`, `.test.tsx`
- Create: `frontend/src/routes/TriageCard.tsx`, `.test.tsx`
- Create: `frontend/src/api/decisions.ts`
- Modify: `frontend/src/api/dashboard.ts` — `view` sanitised to the four Literal values in **one** place (`DashboardParams`), so a mistyped deep link `?view=inbo` is a plain inbox, not a FastAPI 422 that dead-ends the index (T7-8; the dry run wrote two sanitisers, one unreachable — T7-14)
- Modify: `frontend/src/api/calls.ts` — nothing structural; the feed passes `invalidateDecisions` as a per-call `onSuccess` to `useHideCall`
- Modify: `frontend/src/routes/CallDetail.tsx` — hide and undo also invalidate `["decision-counts"]` (T9-2, measured 20-s stale rail); back link keeps the search (D11)
- Modify: `frontend/src/components/Deadline.tsx` — additive: `daysLeftText` shares the Stockholm calendar-day rule (T7-8)
- Modify: `frontend/src/router.simple.tsx` — index route
- Modify: `frontend/e2e/match.spec.ts` **and** `tests/architecture/test_match_gates_are_wired.py` — the dashboard fixture (see Step 8), in this commit

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces: the match edition's index screen.

**The edition split is the mount point, not a branch.** `router.simple.tsx` mounts
`TriageFeed` at index instead of `Dashboard`. `Dashboard.tsx`, `CallRow.tsx`,
`DashboardHeader.tsx`, `Pagination.tsx` and `MatchBadge.tsx` then leave the match
bundle; `TriageFeed`, `TriageCard`, `decisions.ts` and `CriterionChips` enter it.
The dry run measured **no net movement**: `396 / 0 / 48` before and after (6 out, 6
in — two of the six entering being Task 5's chips, T7-5). Re-measure; the guarantee
is the `0`, not the totals. Leave Plan A's `CallRow.tsx` branches in place.

- [ ] **Step 1: The rail**

Inbox / Saved / Hidden with counts from `GET /calls/decisions/counts` (`enabled`
only with a profile — the route is a guaranteed 422 without one); view selection in
the URL (`?view=`), a view change resets `page`; **the profile picker (D11)** —
every profile in `facets.profiles` as a link to `/?profile={id}&view={current}`,
the current one marked `aria-current`. `data-count="{view}"` on each count.

- [ ] **Step 2: The card**

Field order from §5's table: title (a `Link` to `/calls/{id}?profile={p}&view={v}`
— **both** parameters, D11), funder, programme (omit the separator when absent),
deadline + days left, budget as a **range when min ≠ max**, `summary` (or the
"no summary" sentence with a link out — never a collapsed section), the matched
sentence, chips (Task 5), keywords, "N things to check" for escalated calls **with
notes**. In the Saved and Hidden views the expanded card also renders a **"Return
to inbox"** button (D2) that issues the view's `DELETE`. **The card reads no
`aggregate_score`** — its file says so in a comment, and gate 8 pins both halves.
`tabIndex={-1}` and `data-call-id` on the `<article>`.

- [ ] **Step 3: Keys, and what happens after one**

`j`/`k` move **DOM focus** (not just a highlight — `cardEls.current[focused]?.focus()`;
a screen-reader user pressing `j` must hear something move); `s` saves, `h` hides,
`u` un-decides in Saved/Hidden and does nothing in Inbox (D2). Chords (`meta`,
`ctrl`, `alt`) belong to the OS. The listener **must not fire while focus is in an
`input`, `textarea` or `contenteditable`** — one `matches("input, textarea,
[contenteditable]")` guard, but the mutation table breaks it **per branch**. Keys
are ignored while the list is fetching (D20). **A decided card leaves the list, and
the rail moves with it:** every decision mutation's `onSuccess` calls
`invalidateDecisions(qc)` — `["calls"]` AND `["decision-counts"]`, in one function in
`decisions.ts` — because `useHideCall` predates the rail and invalidates only
`["calls"]` (T7-3), and `staleTime` is 20 s (`App.tsx`), so a forgotten second key is
a rail that sits one triage behind for the rest of the session.

- [ ] **Step 4: Inbox zero is a designed screen.** Its copy is asserted on.
  `data-empty="{view}"` on the empty-state element; Saved's and Hidden's empty
  copies are different strings from the inbox's.

- [ ] **Step 5: Pagination (D15).** When `pagination.has_next`, the column ends with
  a "Next page" link to `?page={page+1}` carrying `view` and `profile`.

- [ ] **Step 6: Gates** (`TriageFeed.test.tsx`, `TriageCard.test.tsx`, `CallDetail.test.tsx`)

The stub dispatches by **path**: `/api/calls/decisions/counts` returns counts
`{ inbox: 99, saved: 4, hidden: 12 }` over **three** items — all three differ from
`items.length` and from each other (T7-7; `counts.inbox = 99` alone half-coincides on
saved/hidden); everything else returns the dashboard body. Request queries are read
with a helper that asserts the request EXISTS before indexing (a `find(...)?.[1]`
that short-circuits turns "never requested" into a pass). Spies are typed
structurally (`{ mock: { calls: unknown[][] } }`), not `ReturnType<typeof vi.spyOn>`
— that is a `tsc` error with 34 green tests (T7-11).

1. Renders a card per call: title, funder, deadline.
2. A card with `summary: null`, `keywords: []`, `criteria: []`, `programme: null`,
   all budget fields null renders and shows the "no summary" sentence. *The common
   case.*
3. `s` alone → card 1; `j` then `s` → card 2; `j j k s` → card 2; six `j` then `s`
   then six `k` then `s` → cards 3 then 1 (T7-6: "`s` always decides the LAST card"
   and "`j` moves by 2 and clamps" both pass the two-case version).
3b. `j` moves DOM focus to the second `<article>` (`document.activeElement`), `k`
   back (T7-13).
4. **`s` inside a focused editable element fires no mutation — parameterised over
   `<input>`, `<textarea>`, and a nested `contenteditable` — with an AWAITED FLUSH
   and a positive control in the same render.** The assertion is
   `await act(async () => { await new Promise(r => setTimeout(r, 0)); }); expect(post.mock.calls).toEqual([]);`
   then blur, `s` on `body`, and `await waitFor(() => expect(decisions(post)).toEqual(["/api/calls/1/save"]))`.
   Revision 2's wording ("triggers no mutation") written the obvious way passes
   with the ENTIRE guard deleted: TanStack defers `mutate()` to a microtask, so a
   synchronous `not.toHaveBeenCalled()` runs first (T7-1 — measured: under B23 the
   three cases failed ONLY at the positive control the implementer had added on
   their own). **Any gate anywhere in this plan phrased "no request is made" has
   this shape unless it flushes first.**
5. Zero items renders the inbox-zero copy.
6. `?view=saved` requests `view=saved`, marks the rail item current; the default
   requests `inbox`.
7. Counts render from the API — `99 / 4 / 12` — not from `items.length`.
8. **No raw score on the card.** Fixture: `aggregate_score: 87` with a deadline 12
   days out, budget 100 000–250 000, `topic_fit` 5/5, 3 notes — `87` appears nowhere
   by accident. Assert (a) chips render, (b) no `MatchBadge` / `[data-band]`, (c)
   `card.textContent` does not contain `87` — **scoped to the `<article>`**, not the
   render container: `MantineProvider` injects ~8 kB of CSS custom properties into
   a `<style>` in the container, containing `87` (rgba components, the `87.99375em`
   breakpoint), so a container-wide `not.toContain("87")` is RED ON CORRECT CODE
   (T7-2); and (d) a comment-stripped source scan of `TriageCard.tsx` not naming
   `aggregate_score` — a smoke, not the gate: `["aggregate" + "_score"]` walks past
   it, and (c) is what catches that (F-12 #4).
9. **`u` (D2):** in `?view=hidden`, `u` issues `DELETE /api/calls/{call_id}/hide`
   with `profile_id`; in `?view=saved`, `DELETE …/save`; in `?view=inbox`, after an
   awaited flush, no DELETE and no POST. The "Return to inbox" button does the same
   as `u`.
10. **A decision refetches both caches (T7-12):** after `s` (and after `h`), a
    second `GET /api/calls` and a second `GET /api/calls/decisions/counts` are
    requested. Deleting either `onSuccess` must redden this.
11. `?view=inbo` is requested as `inbox`.
12. The card's title link is `/calls/1?profile=7&view=hidden` when rendered at
    `/?view=hidden&profile=7` (D11).
13. The rail lists `Other group` as a link to `/?profile=8&view=saved` when rendered
    at `/?view=saved&profile=7` with two profiles in `facets.profiles` (D11).
14. With `has_next: true` the column ends with a link whose `href` carries `page=2`,
    `view` and `profile`; with `has_next: false` there is none (D15).
15. `CallDetail`: the undo and the hide both call `invalidateQueries` for
    `["decision-counts"]` (spy on the same `QueryClient` the component uses); the
    back link's `href` is `/?profile=7&view=hidden` when mounted at
    `/calls/5?profile=7&view=hidden` (D11).

- [ ] **Step 7: Mutation-test**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B23a/b/c | delete the `input` / `textarea` / `[contenteditable]` branch of the guard, **one at a time** | exactly the matching gate-4 case | each `1 failed` on its own case; the whole guard deleted: Phase 2 re-run `3 failed, 21 passed` — all three gate-4 cases. **Before the awaited-flush fix: green** (T7-1) |
| B24 | `j` moves by 0 | gate 3 (four tests) | `4 failed` |
| B25 | render `items.length` in the rail | gate 7 | ✓ with the 99/4/12 stub |
| B25b | render `{call.aggregate_score}` in `TriageCard` | gate 8 (c) and (d) | ✓ both halves |
| B25c | render `String((call as unknown as Record<string, unknown>)["aggregate" + "_score"])` | gate 8 (c) — **(d) stays green**, which is why (c) is the gate | Phase 2: `× renders the criterion chips instead — and no band, no badge, no raw number → expected '…' not to contain '87'`; `1 failed, 12 passed` |
| B26 | `null` from the empty branch | gate 5 | ✓ |
| B27 | drop the `summary === null` branch | gate 2 | ✓ |
| B28 | delete the focus effect (`cardEls.current[focused]?.focus()`) | gate 3b | Phase 2 re-run: `× j moves DOM focus to the next card, k moves it back` — `1 failed, 23 passed` |
| B29a | delete `onSuccess: () => invalidateDecisions(qc)` from `useSaveCall` | gate 10 | dry `7e6b49d5` F1a: `1 failed` |
| B29b | delete the per-call `onSuccess` on `hide.mutate` | gate 10 | dry F1b: `1 failed` |
| B29c | `CallDetail`'s undo: `mutateAsync(hidden)` without the `onSuccess` | gate 15 | Phase 2 re-run: `× Undo also invalidates the rail's decision-counts, not only the calls list` — `1 failed, 10 passed` |
| B30a | `u` in Hidden issues `POST …/hide` (a re-hide) | gate 9 | PREDICTED (not run). The gate itself is red on the dry tree, which has no `u`: `expected [] to deeply equal [ '/api/calls/{call_id}/hide' ]` |
| B30b | `u` in Saved issues `DELETE …/hide` | gate 9 (saved case) | PREDICTED (not run); red on the dry tree: `expected [] to deeply equal [ '/api/calls/{call_id}/save' ]` |
| B31 | drop `view` from the card link | gate 12 | PREDICTED (not run); red on the dry tree: `Expected href="/calls/1?profile=7&view=hidden", Received href="/calls/1?profile=7"` |
| B31b | drop `view` from the picker links | gate 13 | PREDICTED (not run); red on the dry tree: `Unable to find an accessible element with the role "link" and name "Other group"` |
| B31c | back link `to="/"` | gate 15 | red on the dry tree (which IS this mutant): `Expected href="/?profile=7&view=hidden", Received href="/"` |
| B32 | no next-page link | gate 14 | PREDICTED (not run) |
| B33 | `view` sanitiser passes the raw string through | gate 11 | ✓ (dry) |

- [ ] **Step 8: The browser walk — this task breaks it, so this task fixes it**

`frontend/e2e/match.spec.ts`'s `CALLS` fixture has `effective.profile_id: null`
(which after this task selects the onboarding SCREEN, not "no filter"), and its
items lack `criteria`/`keywords`, which `TriageCard` dereferences — so `/` crashes
into react-router's error boundary and five scenarios go down at once (T8-1: the
dry run measured `2 failed, 12 passed` at its Task 7 tip and a third failure
latent). Update the fixture **in this commit**: a resolved `profile_id`, every
field `TriageCard` reads, and `frontend/e2e/match.spec.ts`'s mock recording the
query string on every decision request. Update `test_match_gates_are_wired.py`'s
title list and assertion-site count **in the same commit** (T9-3).

- [ ] **Step 9: Verify**

```bash
pnpm -C frontend test src/routes/TriageFeed.test.tsx src/routes/TriageCard.test.tsx src/routes/CallDetail.test.tsx
pnpm -C frontend test
pnpm -C frontend lint
pnpm -C frontend build                       # the FULL edition must still build; and tsc is the typecheck
pnpm -C frontend build:match && pnpm -C frontend check:bundle    # re-measure; the dry run saw 396/0/48 -> 396/0/48
pnpm -C frontend e2e:match                   # green, in THIS commit
uv run pytest tests/architecture -q          # the e2e pins, gen_routes --check
uv run python scripts/gen_routes.py --check
```

**Delta:** the dry run's Task 7 added +2 vitest files, +34 tests (13 card + 21 feed);
with gates 9, 12–15 expect more — measure.

---

### Task 8: No profile is an onboarding screen, not a firehose

Decided by the human, 2026-09-04. Release 1's first screen for a user with no
profile must be an invitation to make one, not 50 unranked calls.

**Files:**
- Create: `frontend/src/routes/TriageEmptyProfile.tsx`, `.test.tsx` — **reuses `TriageFeed.module.css`** (a new module would move the pinned bundle count for nothing — T8-5)
- Modify: `frontend/src/routes/TriageFeed.tsx` (the branch), `.test.tsx`
- Modify: `frontend/e2e/match.spec.ts` (the null-profile scenario) **and** `tests/architecture/test_match_gates_are_wired.py`
- **Not** `src/call_manager/web/api/calls.py`, **not** `tests/web/test_call_decisions_api.py`, **not** `schema.gen.ts` — revision 2's "Step 2: make the backend refuse too" is UNREACHABLE as a step: `call_decisions.profile_id` is NOT NULL, so the parameter had to be required from Task 1, and every gate the step asked for exists by name there (T8-2). Step 2 below is a **confirmation**, not work.

**Interfaces:**
- Consumes: `DashboardOut.effective.profile_id` (Task 7 reads it) and `facets.profiles`.
- Produces: the guarantee that no new orphan decision can be created **from the match UI** — the endpoint guarantee is Task 1's.

**Match-edition only.** `dashboard.py`'s unranked fallback stays exactly as it is.

- [ ] **Step 1: The frontend branch**

`TriageFeed` reads `effective.profile_id` from the list response (D8 is what makes
that a 200 with `null` even for a deep link into `?view=saved`). When it is `null`,
render `TriageEmptyProfile` **instead of** the feed — its own `<main>`, no rail
(so Saved/Hidden are unreachable until a profile is picked — recorded). The screen
says what a profile is for in one sentence and links to `/profiles/new`; if
`facets.profiles` is non-empty, it lists them as links to `/?profile={id}` instead
of sending the user to create a duplicate. The pick is **not persisted** (a deep
link never changes the stored default — `persist=False`; making it persist is a
product question, recorded). Inbox zero and this screen share no string.

- [ ] **Step 2: Confirm the backend guarantee (no code)**

`uv run pytest tests/web/test_call_decisions_api.py --collect-only -q | grep -E 'without_profile_id|unreadable_profile|inactive_profile'`
must list the hide AND save flavours. Then re-run Task 1's B9c and Task 3's B13g
mutations as confirmation. Paste the `--collect-only` output in the task report.

- [ ] **Step 3: Gates**

1. `profile_id: null` renders `TriageEmptyProfile` **and the feed's list element is
   absent** (asserting the onboarding copy alone passes a component that renders
   both).
2. Inbox-zero copy and no-profile copy are **different strings**, each asserted
   ABSENT in the other's state.
3. Profiles but none selected → the picker (`data-profile-pick`), not
   `/profiles/new` alone; no profiles → `/profiles/new` and no picker.
4. **The schema shape (replaces revision 2's "schema.gen.ts must change", which
   exits 1 on a CORRECT run of this task — nothing on the backend changes, T8-3):**
   run the required-shape script from Task 1 Step 11, extended with the two save
   operations and the counts operation (all six decision operations `REQUIRED`). Do **not** demand a diff on `schema.gen.ts` here.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Must redden | Measured |
| --- | --- | --- | --- |
| B34 | render the feed **and** the onboarding screen | gate 1 | `1 failed` — gate 1 only |
| B35 | reuse the inbox-zero component for the no-profile branch | gate 2 (both directions) + gate 1 | `4 failed` |
| B36 | `profile_id: int \| None = Query(default=None)` on `hide` (Task 1's route) | Task 1 gate 9 (`assert 404 == 422`) and the required-shape script (`NOT REQUIRED`) | `1 failed`; script: measured in Phase 2 on a doctored schema |
| B37 | drop `is_active` only from `_readable_active_profile_or_404` | exactly the inactive-flavoured tests — proves the two dead-id fixtures are not redundant | `2 failed` |

- [ ] **Step 5: Verify**

```bash
pnpm -C frontend test src/routes/TriageEmptyProfile.test.tsx src/routes/TriageFeed.test.tsx
pnpm -C frontend test
uv run pytest tests/test_api_calls_feed_behavior.py -q -k unranked   # 3 passed, UNCHANGED — the full edition's behaviour
pnpm -C frontend lint && pnpm -C frontend build
pnpm -C frontend build:match && pnpm -C frontend check:bundle       # +1 app module; the dry run measured 396/0/48 -> 397/0/49
pnpm -C frontend e2e:match
uv run pytest tests/architecture -q
```

**Delta:** the dry run's Task 8 added +3 (EmptyProfile) +2 (feed) vitest, +0 backend.

---

### Task 9: Browser coverage and CI

Plan A wired `frontend/e2e/match.spec.ts` and `tests/architecture/test_match_gates_are_wired.py`.
**On the branch base the pin is already an EQUALITY** — `assert expects == 34`, with
a docstring saying "EQUALITY, not a floor. The previous version asserted `>= 18`
against a real 22" (PF-6, T9-4: revision 2's "change the floor to an equality" was
done two merges ago; the numbers 18/22 describe a version that no longer exists).
After Tasks 7 and 8 the numbers are whatever they are — the dry run measured **91**
assertion sites and **23** titles at its tip. Measure, do not copy.

**Files:**
- Modify: `frontend/e2e/match.spec.ts`
- Modify: `tests/architecture/test_match_gates_are_wired.py`
- Modify: `frontend/src/routes/Dashboard.test.tsx` — the stale self-description "the MATCH dashboard … emits no /cockpit link" (T7-9)

- [ ] **Step 1: Scenarios** against `page.route` mocks. The mock records the method,
  path **and query string** of every decision request — `profile_id` is WHOSE
  decision this is — **and** records `?profile=`/`?view=` on `GET /api/calls` and
  `profile_id` on the counts request (T9-7: the dry run's mock never inspected
  `profile_id` on the READ path, so a bug dropping it on the post-decision refetch
  survived every scenario). Scenarios: the feed renders rail, counts and the focused
  card; `s` → Saved; `h` → Hidden; **`u` in Hidden returns the call to the inbox,
  and the rail count is correct immediately** (D2, D19 — replaces the dry run's
  hide-twice-to-reveal-Undo walk); **the detail round trip lands back in the view
  it left** (D11: open a card from Hidden, click the back link, still in Hidden —
  the dry run's scenario asserted "back in the INBOX" and passed only BECAUSE
  `view` was lost, F-3); **the rail's profile picker switches profile and the
  hidden set changes** (D11, the DoD smoke made performable); inbox-zero when the
  last card is decided; a chip expands to the quoted sentence and the reasoning;
  **a closed saved call is still in Saved** (D1); `profile_id: null` → onboarding
  (Task 8 wrote it); profiles but none picked → the picker.
- [ ] **Step 2: Three pins, all measured after writing.**
  1. **Titles**: the `REQUIRED_SCENARIOS` list == every `test("…")` title, by equality.
  2. **Assertion sites**: `len(re.findall(r"\bexpect(\.soft|\.poll)?\(", src))` pinned
     by equality — `expect.poll(` and `expect.soft(` are invisible to the old
     `\bexpect\(` (T9-6, T9-7; measured 0 of each today, which is why the old count
     and the new one both say 91 on the dry tree).
  3. **Every async matcher is awaited (D17, F-12 #2).** For every `expect(…)`
     site, find its matching `)`, read the matcher name after `.`/`.not.`, and if it
     is a Playwright async matcher (`toBeVisible`, `toHaveText`, `toHaveCount`,
     `toHaveAttribute`, `toHaveURL`, `toBeFocused`, …) or the site is `expect.poll(`,
     require the text before `expect` to end in `await`. Phase 2 wrote this as
     ~30 lines of Python and ran it on the dry tree's spec:

     ```
     === good input (dry HEAD match.spec.ts)
     assertion sites (expect( + expect.soft( + expect.poll(): 91      exit=0
     === delete the await before one toBeVisible
     UNAWAITED async matcher at line 783: expect(page.locator('article[data-call-id="1"]')).toBeVisible(   exit=1
     ```

     Without it, deleting one `await` keeps the count at 91, typechecks nowhere
     (`frontend/e2e/` is outside `tsconfig` `include`), lints nowhere (oxlint has no
     types), and becomes a floating promise that can never fail.
- [ ] **Step 3: Mutation-test.** B38: delete one scenario → pin 1. B39: strip a
  scenario's assertions, keep its title → pin 2. B40: `expect(true).toBe(true)` in
  place of a real assertion → **the count is unchanged**; caught by that scenario's
  behavioural mutation. B41: delete one `await` → pin 3. **And one behavioural
  mutation per scenario**, in app source (make `u` POST instead of DELETE; make the
  back link `to="/"`; drop `view` from the picker; make the rail counts static —
  **check `tsc` first**: `counts` becoming unused trips `noUnusedLocals` and the
  run fails at `webServer` start, a red that says nothing (T9-5)). The dry run ran
  eight behavioural mutations, each reddening its named scenario.
- [ ] **Step 4: Verify.**

```bash
pnpm -C frontend e2e:match
uv run pytest tests/architecture/test_match_gates_are_wired.py -q
uv run pytest tests/architecture -q
pnpm -C frontend lint && pnpm -C frontend build && pnpm -C frontend test
```

---

## Definition of done for Plan B

From a clean tree, in this order:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/call-match-r1b
uv run ruff check src tests alembic scripts
uv run pytest -n 16 --dist loadfile                                     # includes tests/test_schema_drift.py, the model/migration agreement gate
uv run pytest tests/test_api_calls_feed_behavior.py -q -k unranked      # 3 passed, UNTOUCHED
uv run pytest tests/architecture -q
uv run python scripts/gen_routes.py --check
uv run python scripts/gen_architecture.py && git diff --exit-code docs/ARCHITECTURE.md
uv run python scripts/gen_env_example.py && git diff --exit-code .env.example
pnpm -C frontend gen:api && git diff --exit-code frontend/src/api/schema.gen.ts
python3 <the required-shape script from Task 1 Step 11, all six decision operations: hide, unhide, hidden list, save, unsave, counts>
pnpm -C frontend test
pnpm -C frontend lint
pnpm -C frontend build
pnpm -C frontend build:match && pnpm -C frontend check:bundle
pnpm -C frontend e2e:match
git diff --quiet ed1153a3 -- docs/superpowers/specs/ && echo "specs untouched"
git status --short | wc -l                                              # 0
```

Expectations:

- **`1 skipped` — exactly one.** `0 skipped` means a live Anthropic call ran.
- **The pass total will be higher than `5029`.** State the number you measured and
  the per-task deltas that produce it. The dry run reached `5107` with a different
  gate set; do **not** copy it.
- `tests/architecture/full_edition_routes.txt` goes **107 → 109**, and the diff is
  exactly `/api/calls/decisions/counts` and `/api/calls/{call_id}/save`. The SPA pin
  `routes.gen.ts` does **not** move.
- `docs/superpowers/specs/` byte-identical to `ed1153a3`.
- `.env` untouched. Never read, copy or back it up.
- `check_match_bundle.mjs` exits **0** and still reports **`0`** under the 14
  excluded paths. The other two numbers moved (the dry run: `396/0/48 → 397/0/49`);
  record what they are.
- `master` is still `ed1153a3`.

Then a manual smoke, because no gate here proves the app is usable:

```bash
pnpm -C frontend build:match && pnpm -C frontend check:bundle && pnpm -C frontend package:match
APP_EDITION=match DRAFTING_ENABLED=false SPA_MODE=prod uv run uvicorn call_manager.main:create_app --factory
```

Confirm by hand: the inbox lists calls with chips and no raw score; `j`/`k` move;
`s` empties one card into Saved; `h` into Hidden; **`u` in Hidden brings it back and
the rail count moves at once**; deciding the last card shows the inbox-zero screen;
**the rail's profile picker** changes which calls are hidden; a saved call whose
deadline has passed is still in Saved, marked closed; opening a card from Hidden
and pressing "← All calls" lands in Hidden; a chip expands to the funder's own
sentence.

---

## What Plan B deliberately does not do

- **No boards, no approval queue, no `/b/:slug`.** Plan C.
- **No share-by-email, no change detection.** Plan D.
- **No note UI.** `call_decisions.note` is created and nothing writes it.
- **No swipe gesture and no phone rail.** Assigned to **Plan D**, recorded as a
  spec deviation until then.
- **`MatchBadge` is not deleted**, and the full edition's `Dashboard`/`CallRow` keep it.
- **`/cockpit` still returns HTTP 200 with the SPA's soft 404** in a match build.
- **The full edition's no-profile archive view is untouched** — except that it can
  no longer hide (D3).
- **The old global-hide semantics are not preserved for the full edition.**
- **`GET /api/calls/hidden` keeps existing with no client** (D9). It is the spec's
  "re-skin"; deleting it moves the manifest and is a later decision.
- **`frontend/e2e/` is not typechecked** (D17). The `await` scan is the gate.
- **No `?fields=` on the list.** D10 measures the payload; the lever is named, not pulled.
- **The route manifest still pins PATHS, not operations** (F-12 #1). A new
  `@router.patch("/calls/{call_id}/hide")` would move no manifest line. The rule this
  plan follows instead: **every write endpoint on `call_decisions` has a two-user
  gate** (Task 1 gates 6b/5c, Task 3 gate 3c) and a mutation that drops its
  `user_id` clause. Turning the manifest into operations is Plan C's or a review's.

## Handed to Plan C

**F-10.** `_base_call_filters` / `_decision_predicate` require a `user_id` (the
docstring forbids growing separate copies), and §4.3's board is profile-scoped with
no user in scope. Plan C must answer *"whose hides suppress a call on a shared
board?"* before its Task 1 — fork the predicate deliberately, or define the board's
decision set as the profile's decisions. The per-user key (D4) is the other half of
that question.
