# Call Match Release 1 — Plan B: triage decisions and match reasons

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `match` edition the other half of the triage pair. Today a user
can only push a call away, globally, for every profile at once. After this plan a
decision is **per profile**, has two directions (**Saved** / **Hidden**), and the
default view is an **inbox that ends**. The list stops showing a raw score and
starts showing named reasons quoted from the call itself.

**Architecture:** One new table, `call_decisions`, replaces `user_call_dismissals`
in a single migration. The three existing hide endpoints re-point at it; a Saved
half is added beside them. The list schema (`CallOut`) grows the fields the
expanded card needs, plus one batched criteria query — **not** a per-row query.
The match edition mounts a new `TriageFeed` at its index route; the full edition
keeps `Dashboard` and `MatchBadge` untouched, so the two editions diverge by
**which component the router mounts**, not by a runtime branch inside a shared one.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL 16;
React 19 + Vite + react-router-dom + Mantine; pytest (+ testcontainers Postgres);
vitest + jsdom; Playwright.

---

## Global Constraints

Copied from `CLAUDE.md`, from the spec, and from what the Plan A run measured.
Every task's requirements implicitly include this section.

- **Test command:** `uv run pytest -n 16 --dist loadfile` (~114 s). **Never
  `-n auto`** — `psutil` is absent, so xdist resolves it to 72 workers.
- **The `1 skipped` is the signal, not the total.** It is a real-SDK test gated on
  `ANTHROPIC_API_KEY`. `0 skipped` means a live Anthropic API call just ran — stop
  and record it. **Do not export `ANTHROPIC_API_KEY`.**
- **Measured baselines on `9365f123` (2026-09-04), not predictions:**

  | Suite | Command | Baseline |
  | --- | --- | --- |
  | Backend | `uv run pytest -n 16 --dist loadfile` | `5029 passed, 1 skipped` |
  | Architecture subset | `uv run pytest tests/architecture -q` | `73 passed` |
  | Frontend | `pnpm -C frontend test` | `135 files, 954 tests` |

  Each task states its own **delta** from these. Re-measure the base yourself
  before asserting a total. **Never adjust an expected number to make the
  arithmetic close** — find the term that changed and name it.
- **Frontend tests:** `pnpm -C frontend test` (whole suite) or
  `pnpm -C frontend test src/path/file.test.tsx` (one file). **Do NOT write
  `pnpm test run`** — `test` is already `vitest run`, so the extra `run` becomes a
  filename filter matching nothing (`No test files found, exiting with code 1`),
  which reads like a broken change. **Lint:** `pnpm -C frontend lint` (oxlint —
  there is no eslint config).
- **Never run `ruff format` on a directory.** Format by filename only. Count
  `git status` lines before and after; the delta must be 0 for untouched files.
  `ruff check src tests alembic scripts` is the gate.
- **`docs/ARCHITECTURE.md` is generated.** Regenerate in the same commit that moves
  code: `uv run python scripts/gen_architecture.py`. Never hand-edit. This plan
  adds a table, so it **will** change.
- **`.env.example` is generated** from `Settings` by
  `uv run python scripts/gen_env_example.py`, pinned by `tests/test_env_example.py`.
- **`frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py` are
  generated** from `frontend/routes.json` by `uv run python scripts/gen_routes.py`.
  `--check` fails when stale. This plan adds no route, so they should not move —
  **if they do, that is a finding, not a regeneration.**
- **OpenAPI TS types are generated.** Changing `CallOut` or adding an endpoint
  **requires** regenerating `frontend/src/api/schema.gen.ts` with
  `pnpm -C frontend gen:api` (which delegates to `scripts/gen_openapi_types.sh` —
  the one canonical generator, already pinning `EMBEDDING_MODEL=bge-m3` internally
  so local and CI agree regardless of `.env`). **The drift gate can pass without
  generating** — regenerate explicitly, and check with
  `test -n "$(git diff --name-only -- <file>)"`. **Not `git diff --stat`**: measured,
  it exits **0** with empty output on an unchanged file, so it can never fail.
- **The `full` edition must keep working** at every commit: `pnpm -C frontend build`
  succeeds and `tests/architecture/full_edition_routes.txt` grows by **exactly 2
  lines, 107 -> 109**, and by nothing else. (**Re-measured 2026-09-04 after the
  profile-visibility merge `ed1153a3`**, which added
  `/api/profiles/{profile_id}/viewers` and `/api/profiles/{profile_id}/visibility`
  and moved the base 105 -> 107. The review history below argues about 105 vs 107
  vs 109; all of it was against the OLD base and is left as written.) Release 1 is an addition, not a
  replacement. **Measured**, because three sources disagreed: I first wrote
  "still 105" and two outside reviewers both said 109. The file holds unique
  **paths**, not operations — `POST` and `DELETE /api/calls/{call_id}/hide` are
  one line (`grep -c hide` = 1) — so Task 3's four operations add three paths:
  `/api/calls/{call_id}/save` and `/api/calls/decisions/counts` — **two** paths, not
  three: `?view=` is a query parameter on the existing `/api/calls`, and
  `GET /calls/saved` was dropped in favour of it (Task 2 Step 1).
  `frontend/routes.json` and `routes.gen.ts` are the SPA pin and do **not** move;
  they are a different file and a different question. **One exception is
  deliberate and is called out in Task 2** — full's hide semantics change from
  global to per-profile, because that is the point of the new table.
- **`docs/superpowers/specs/` is INPUT, not output.** Byte-identical to `f0aaed0c`
  at every commit. A wrong spec is a `PLAN-DEFECTS.md` entry, not an edit. This
  plan already carries five (§ *Spec defects this plan corrects*).
- **Commit titles name the change, not the activity.**
- **Every gate must be mutation-tested.** Break the thing the gate protects and
  confirm it goes red, in the same task. *A verification that reddens nothing is a
  finding, not a pass.* Do the whole mutate → test → restore cycle **inside one
  shell invocation**, and restore with `cp` from a saved copy — **never
  `git checkout --`** while the fix under test is uncommitted.
- **The Plan A run recorded nine hollow gates, four of them found only by an
  outside review, and three of those were written the same morning to prevent
  hollow gates.** Before writing any gate here, name what trivially passes it.
  The five shapes: a subset check; an absence that is already true; a mutation
  that names a test which lacks the assertion; a weaker property than the one
  claimed; an expected output that is what the *failure* prints.

---

## Scope: this is Plan B of four

| Plan | Spec sections | Ships | State |
| --- | --- | --- | --- |
| A | §5, §9 gates 1/2/4/5, §11 steps 1-2 | the `match` edition | **MERGED `9365f123`** |
| **B (this one)** | **§4.1, §4.2, §5 card, §11 steps 3-4** | **`call_decisions`, save/hide, inbox, match reasons on the list** | this plan |
| C — Boards | §4.3, §6, §9 gate 3, §11 steps 5-6 | boards, approval queue, `/b/:slug` | not started |
| D — Extras | §7, §11 steps 7-8 | share by email; change detection | not started |

Plan B ships working software on its own: a release-1 build whose calls screen is
a working inbox with Saved and Hidden, per profile, showing why each call matched.

### Already done by Plan A — do not redo

**§11 step 3 says "remove the two `/cockpit/new` links".** Plan A did this, but
**gated them rather than deleting them** (`{!isMatchEdition && (…)}` in
`CallRow.tsx:85-88` and `CallDetailBody.tsx:54-60`), recorded as ESCALATE **E1**.
The spec's words are "**they must be removed, not disabled**"; deleting them
outright would have taken the buttons out of the **full** edition too, which
Global Constraint 3 forbids. The gate `frontend/src/noCockpitLinks.test.tsx`
proves no `/cockpit` href reaches the match bundle. **This is settled. Do not
re-open it, and do not delete those branches** — a later task here moves
`CallRow` out of the match graph entirely, which makes the branches vestigial in
`match` but still load-bearing for the gate and for `full`.

---

## Spec defects this plan corrects

Measured against merged master `9365f123` before this plan was written; the raw
probes are in `/home/parlar_ai/call-match-r1b-run/MEASUREMENTS.md`. The spec is
input — these are recorded, not edited into it.

**D-B1 — the criterion vocabulary is seven, not six.** §4.2 lists `topic_fit |
eligibility | exclusions | timeline | budget | trl`. `web/filters.py`'s
`_CRITERION_LABELS` carries a seventh, `embedding` → "Topic similarity", and
`ranking/worker.py:365-369` writes it as one of four gate rows. `db/models.py:508`'s
own comment is stale in the same way. There is **no `CheckConstraint`** on
`criterion` — it is a bare `String(20)`.

**D-B2 — the rating vocabulary is five shapes, not four.** §4.2 says `"5" |
"pass" | "fail" | "unclear"`. Measured:

| criterion | writer | ratings |
| --- | --- | --- |
| `budget`, `trl`, `timeline`, `embedding` | `worker.py` gates, `GateStatus.value` | `pass` \| `fail` \| **`unknown`** |
| `topic_fit` | LLM | `"1"` … `"5"` |
| `eligibility` | LLM (`prompt.py:329`) | `pass` \| `fail` \| `unclear` |
| `exclusions` | LLM (`prompt.py:38`) | `pass` \| `fail` |

`unknown` (`ranking/criteria.py:30`) is absent from the spec. A chip renderer built
to the spec's four falls through on every gate that could not judge.

**D-B3 — a card can carry one chip, not six.** `worker.py:383-390`: the gate loop
`return`s on the first `FAIL` (except `timeline`/`embedding` under
`must_include_override`). A call failing the budget gate has exactly one
`call_ranking_criteria` row. The chip row must render 1..7, and 0.

**D-B4 — §4.1 misses a fifth `user_call_dismissals` consumer.** It names three.
Measured, there are five files: `digest/assemble.py`, `web/api/calls.py`,
`web/dashboard.py`, `db/models.py` (the model) and **`ranking/golden_set.py:94-124`**,
whose `candidates_dismissed_high_score()` joins `Subscription` to *infer* which
profile a global dismissal belonged to — the same fan-out the migration performs.
Left unchanged it would treat a **saved** call as a "dismissed_high_score" golden-set
candidate. Task 2 fixes it.

**D-B5 — the spec's frontend line numbers are stale by exactly +10.** Plan A
inserted a 10-line edition gate into `CallDetailBody.tsx`. `:57-67` ("Aggregate
score") is now **63–93**; `:145-171` (criterion cards) is now **155–181**; `:48`
is now **54–60** (seven lines: the guard, `<Box>`, `<Button>`, its text, and the
three closers).
`CallRow.tsx`'s `MatchBadge` render is at **:84**, not `:53`.
**Cite by anchor string in this plan, never by the spec's line number.**

---

## Decided by the human, 2026-09-04

**1. No profile is an onboarding screen, not a firehose.** Measured first: without a
profile the list does **not** empty — `dashboard.py:296` falls back to "the unranked
sort (newest/deadline)" and returns **every open call in the database**, 50 a page,
newest-published first, with no score, no chips and no reason. Three existing tests
already pin that behaviour (`tests/test_api_calls_feed_behavior.py -k unranked`,
3 passed). There is **no** per-user or per-profile row filter — only hidden, closed,
`?source=` and `?q=`. And `hide_call` takes no profile argument today, which is how a
profile-less user can hide a call at all.

That screen is defensible in the **full** edition as a browsable archive. It is not
defensible as release 1, whose entire promise is *the calls that match you*; §5 says
a feed that never ends is what trains people to stop opening it. **Task 8 makes the
match edition show an onboarding screen instead.** The full edition keeps the unranked
fallback unchanged.

**2. A dismissal that fans out to zero rows is dropped, and the count is printed.**
The alternative — a `profile_id NULL` row — breaks the unique index and the
"a decision is per profile" rule. Two things make this cheap: after Task 8 no *new*
orphan can be created in the match edition, and the hides in question were made
against an unranked list that release 1 replaces. **Before running the migration on
real data, run this and put the number in the commit message:**

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

## Outside review, round 1 — 2026-09-04

Three model families reviewed the plan before any code existed (`gpt-5.6-sol`,
`grok-4.6`, `claude-sonnet-5`; 207k-char package, 39 findings). **26 changed this
plan.** Findings are recorded here rather than silently absorbed, because the
pattern they form is the point: of the 26, **eleven were gates that would have
stayed green while the thing they protect was broken** — the same failure mode that
produced nine hollow gates in Plan A.

Verified by running, not by reading:

| What was wrong | How it was settled |
| --- | --- |
| The inbox and the main list had two different rules on one query — a saved card would bounce back on refresh | All three found it. Fixed with `?view=` (Task 2 Step 1) |
| The route baseline said both "still 105" and "must be regenerated"; reviewers said 109 | **Neither**. Measured: the file is unique paths, and dropping `GET /calls/saved` makes it **107** |
| "Suppress only if hidden on **every** profile — i.e. drop the predicate" | Those are different queries. Rule is now **ANY**, stated as such |
| `git diff --stat` used as a "this file must change" check, 3 places | Ran it: exit **0**, empty output. Replaced with `test -n "$(git diff --name-only …)"` |
| `applicant_note_count = len(ranking.applicant_notes or [])` | `dashboard.py:52` types it `CallRanking \| None`. Guarded |
| Task 1 deleted the model while four files still imported it | The app could not start at that commit. Deletion moved into Task 2 |
| The "no consumers left" grep matched the new model's own docstring | Built the case, ran it: `GATE FAILS on a CORRECT Task 1`. Old name kept out of `src/` |
| `decided_by ON DELETE RESTRICT` "so the decision survives" | It does the opposite — it blocks the delete. Now nullable + `SET NULL` |
| "`ON CONFLICT` is load-bearing: the `OR` matches twice" | It does not; an `ON`-clause `OR` is evaluated once per row. Restated as defensive, with a fixture and a mutation that prove it is inert |
| The criterion gate scanned `criterion=` | Measured: that sees **3 of 7**. The four gate criteria are tuple literals. Gate now reads both |
| "Route-order hazard, **measured**: FastAPI falls through" | It does not. Two-route probe: declared second gives **422**. The word "measured" on an unmeasured claim is the process failure, recorded as such |
| Nothing updated the existing hide buttons | `api/calls.ts:34,44` + `CallDetail.tsx:16-17` send no profile. Added to Task 2 |
| `inbox + saved + hidden == total` as the counts gate | Passes with saved and hidden **swapped**. Three separate equalities now |
| `B10` (save's `DO UPDATE`) pointed at a gate that cannot test it | Gate 2's first save hits an empty row. Added the mirror gate 2b |
| The no-N+1 gate omitted `?profile=` | The batched query is guarded on it, so the gate ran over code it never reached |
| The `expect(` **floor** | A floor with spare assertions is spare assertions anyone can delete. Pinned to equality, plus per-scenario behavioural mutations |
| Keyboard guard tested `<input>` only | Parameterized over input, textarea and contenteditable |
| Nothing stopped the raw score reappearing on the new card | New gate 8 in Task 7 |
| The saved list returned `HiddenListOut` — five fields, cannot render the card | Dissolved by `?view=saved`; the endpoint is not added |
| Swipe and the phone rail had no owner in any plan | Assigned to Plan D as a recorded deviation |

**Refuted / narrowed:** the reviewers' `109` (measured **107**); a claim that the
backfill should widen to every active profile (the spec decided otherwise on purpose,
though the measurement behind it was right and is now written down).

**And one finding I generated myself, then had to withdraw.** Chasing the reviewer's
"selectable set" point I reported that *"there is no profile ownership model in the
app at all"* and flagged Task 8 as a possible stop-and-escalate. Measured afterwards,
that was wrong on both halves:

- `profiles.py:342-343` raises **403** unless `created_by == user.id or is_admin`.
  Spec §4.3 cites `profiles.py:151` as the canonical rule and builds boards on it.
- The rule Task 8 needs is not that one anyway. A decision is keyed
  `(user_id, profile_id, call_id)` and read only by its author, so requiring *edit*
  rights would break shared-profile triage — which is what `decided_by` exists for.

Gate 5 now asserts **exists-and-active**, the escalation is withdrawn, and the
correction is written in place beside the claim rather than deleted. What *is* real
and stays recorded: **profile reads are org-wide by design** — `list_profiles`
returns every active profile's name and full `description` to every logged-in user.

Raw findings: `.superpowers/review/findings-planB/planB-<model>.md`.
Settled points: `.superpowers/review/planB-settled.md`.

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/db/models.py` | Add `CallDecision`, delete `UserCallDismissal` (modify) |
| `alembic/versions/0071_call_decisions.py` | Create, backfill, drop (create) |
| `src/call_manager/web/api/calls.py` | Re-point hide; add save; grow `CallOut` (modify) |
| `src/call_manager/web/dashboard.py` | Filter per (user, profile) on `state` (modify) |
| `src/call_manager/digest/assemble.py` | Per-profile exclusion sets (modify) |
| `src/call_manager/ranking/golden_set.py` | Read `profile_id`; filter `state='hidden'` (modify) |
| `tests/test_migration_0071_call_decisions.py` | The backfill-set gate (create) |
| `tests/web/test_call_decisions_api.py` | Save/hide/inbox endpoints (create) |
| `tests/web/test_calls_list_query_count.py` | The no-N+1 gate (create) |
| `tests/architecture/test_criterion_vocabulary.py` | Pins the 7 criteria / 5 ratings (create) |

**Frontend**

| File | Responsibility |
| --- | --- |
| `frontend/src/routes/TriageFeed.tsx` | The match index: rail + feed + keys (create) |
| `frontend/src/routes/TriageFeed.module.css` | 240 px rail, ~680 px column (create) |
| `frontend/src/routes/TriageCard.tsx` | The expanded / collapsed card (create) |
| `frontend/src/routes/TriageEmptyProfile.tsx` | No profile yet: the onboarding screen (create) |
| `frontend/src/components/CriterionChips.tsx` | The chip row + expansion (create) |
| `frontend/src/components/CriterionChips.module.css` | Rating colours, Georgia quote (create) |
| `frontend/src/api/decisions.ts` | Save / hide / undo mutations + counts (create) |
| `frontend/src/router.simple.tsx` | Mount `TriageFeed` at index (modify) |
| `frontend/src/routes/CallDetailBody.tsx` | Gate out the "Aggregate score" block (modify) |
| `frontend/src/api/schema.gen.ts` | Regenerated from the new OpenAPI (regenerate) |
| `frontend/e2e/match.spec.ts` | Triage scenarios added to the walk (modify) |

**Unchanged on purpose:** `frontend/src/routes/Dashboard.tsx`,
`frontend/src/routes/CallRow.tsx`, `frontend/src/components/MatchBadge.tsx` and its
test. These are the **full** edition's list. `MatchBadge` has exactly one render
site (`CallRow.tsx:84`); the match edition stops importing `CallRow` at Task 7, so
neither file needs an edition branch and neither may be deleted.

---

### Task 1: `call_decisions` — the table and the migration

Spec §4.1. Replace `user_call_dismissals` with a per-profile, two-state decision
table, backfilling every existing row, in **one** migration. Leaving both tables is
how two sources of truth start.

**Files:**
- Modify: `src/call_manager/db/models.py` (add `CallDecision` after `Subscription`; delete `UserCallDismissal` at `:442`)
- Create: `alembic/versions/0071_call_decisions.py` — `down_revision = "0070"`
  (head **re-measured 2026-09-04** after the profile-visibility merge:
  `0070_profile_visibility.py`, **70** migrations. This plan was written against
  `0069` and would have created a SECOND `0070` claiming the same parent — a
  branched history alembic refuses to walk.)
- Create: `tests/test_migration_0071_call_decisions.py` (**not**
  `tests/migrations/…` — that directory does not exist; every migration test on
  master is `tests/test_migration_NNNN_*.py`, and a path under a missing
  directory makes pytest exit **4, "no tests ran"**, which reads like a pass)
- Regenerate: `docs/ARCHITECTURE.md`

**Interfaces:**
- Produces: `CallDecision` — consumed by Tasks 2, 3, 4.
- Consumes: nothing.

**Shape.** `state` is a `String(10)` + `CheckConstraint`, **not** a Postgres native
enum — matching `ck_ranking_remote_attempts_outcome` (`db/models.py`), and avoiding
alembic's enum-alteration pain when Plan C or D adds a third state.

- [ ] **Step 1: The model**

```python
class CallDecision(Base):
    """A user's triage decision about one call, for one profile.

    Supersedes the table migration 0071 drops. (Its NAME is deliberately not
    written here: Task 2's "no consumer remains" gate greps src/ for it, and a
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
        # The inbox query is NOT EXISTS (user_id, profile_id, call_id) — covered
        # by the unique index above. This second index serves the Saved and
        # Hidden LISTS, which filter (user_id, profile_id, state) and order by
        # decided_at DESC.
        Index("ix_call_decisions_user_profile_state", "user_id", "profile_id", "state"),
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
    # a String + CHECK is what ranking_remote_attempts.outcome uses, and it does
    # not force an ALTER TYPE when a later release adds a state.
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    # The user's own reasoning. NOTHING WRITES THIS IN RELEASE 1 — the column is
    # created now so the note UI is a frontend change later, not a migration.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Who pressed the key. Differs from user_id only on a shared profile, where a
    # colleague may triage on the group's behalf.
    #
    # NULLABLE + ON DELETE SET NULL. An earlier draft wrote ondelete="RESTRICT"
    # with the comment "the decision survives the deciding account being removed";
    # all three reviewers pointed out that RESTRICT does the OPPOSITE — it blocks
    # deleting the user. Worse, the backfill sets decided_by = user_id, so RESTRICT
    # would make deleting an ordinary account fail outright, even though user_id
    # itself is ON DELETE CASCADE. SET NULL is what "the row survives, we just lose
    # who" actually looks like. The decision still dies with its OWNER, via
    # user_id's cascade — that is correct: it is their decision.
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
```

**Do NOT delete `class UserCallDismissal` in this task.** An earlier draft did, on
the reasoning that a missing model makes Task 2's consumer edits "fail loudly at
import time". It does — but it fails loudly at *every* import, including the app's.
`calls.py:19`, `dashboard.py:35`, `assemble.py:17` and `golden_set.py:16` all import
it, so between Task 1 and Task 2 the application could not start, and Task 1's own
verify (which runs only the migration test) would not have noticed. That breaks
Global Constraint 3 at a commit boundary. **Task 2 deletes the model in the same
commit that removes its last importer.**

The loud failure is still available and still worth having — just take it inside
Task 2, where it is a compile-time to-do list rather than a broken tree.

- [ ] **Step 2: The migration**

`upgrade()` in this order — create, backfill, **then** drop:

```python
# The backfill set, named exactly (spec §4.1): every profile the user is
# SUBSCRIBED to, plus their users.default_profile_id, restricted to
# profiles.is_active = true.
#
# NOT "every active profile in the database". Profile.is_active
# (db/models.py:294) is a GLOBAL flag, not a per-user one, so that reading
# would write a hide onto other groups' profiles.
#
# NOT "the user's single default profile" either: that would silently UN-hide
# calls people had already dealt with on their other profiles.
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
```

**The `ON CONFLICT` is defensive, not load-bearing — and an earlier draft of this
plan said the opposite.** It claimed "a user whose `default_profile_id` is also in
`subscriptions` matches the `OR` twice, and without it the migration aborts". That
is false, and all three reviewers said so independently: the `OR` sits in a JOIN's
`ON` clause, where it is a boolean test evaluated **once per candidate `profiles`
row**. It decides inclusion; it does not duplicate. `profiles.id` is a plain PK and
`user_call_dismissals` is keyed `(user_id, call_id)`, so a doubly-matching profile
still contributes exactly one row — into a table this migration just created empty.

Keep the clause anyway, for re-run safety and because a plausible future refactor
(splitting the JOIN into a `UNION` of a subscribed branch and a default branch)
*would* produce duplicates. But **know that it is currently inert**, and that if the
JOIN shape ever changes, `DO NOTHING` becomes the thing that silently swallows a
real duplicate-row bug.

**A second, larger loss — named because it is real and was not obvious.** The
backfill set is subscriptions ∪ default. The **selectable** set is bigger: measured,
`resolve_profile_selection` validates only `Profile.is_active` with no user or
subscription check (`dashboard.py:127-131`), and the profiles facet is
`select(Profile).where(Profile.is_active.is_(True))` (`dashboard.py:416`) — **every
active profile in the database, offered to every user**. So a user could be browsing
an unsubscribed profile today, hide a call there, and after migration have no
decision row for it.

The spec chose this knowingly (*"Not 'every active profile in the database' — that
reading would write a hide onto other groups' profiles"*), and this plan keeps that
choice. It is written down here so the first person it surprises finds it.

**On profile ownership — a correction to an earlier draft of this plan.** That draft
said "there is no profile ownership model at all". That is **wrong**, and it is worth
correcting in place rather than quietly deleting, because the wrong version was
already reported once as an escalation.

There *is* one, and the spec names it as canonical:

- `profiles.py:342-343` — `update_profile` raises **403** unless
  `p.created_by == user.id or user.is_admin`. **Writes are protected.**
- `profiles.py:151` — `get_profile` returns `can_edit` computed the same way.
- Spec §4.3: *"'Can edit the profile' is not a new concept: `web/api/profiles.py:151`
  already defines it as `p.created_by == user.id or user.is_admin`. Boards reuse that
  rule exactly, so no second permission model appears."*

**SUPERSEDED 2026-09-04 by the profile-visibility merge `ed1153a3`.** What this
paragraph said was true when this plan was written and is now false, so it is
replaced rather than quietly edited:

> ~~What is genuinely org-wide is **reading**: `list_profiles` returns every
> active profile to any logged-in user, with no filter, and
> `resolve_profile_selection` accepts any active id ... that reads as deliberate,
> not as a hole.~~

Reading is **no longer org-wide**. A profile now has an owner and a `visibility`
of `'org'` or `'private'`, and `web/profile_access.py::readable_profile_ids` is
the single rule deciding who may read it. `list_profiles` filters by it and
`resolve_profile_selection` applies it on **both** arms. The observation that
prompted the change was raised from this very plan's review and became its own
slice; the "single-organization internal tool" reasoning above is exactly what
that slice decided was not good enough for a group's unpublished directions.

Day one is unchanged in practice — every existing profile defaults to `'org'` —
so nothing in this plan's behaviour shifts on merge. What shifts is the RULE the
gate below must name.

**So gate 5 changes.** An earlier draft asked Task 8 to reject "a `profile_id` the
user cannot use", which tests a rule that does not exist and **should not**: a
decision row is keyed `(user_id, profile_id, call_id)` and every read and write is
scoped `user_id == user.id`, so deciding against a profile you can see affects
nobody else's view of anything. Edit rights would be the *wrong* rule — a subscriber
who did not create a shared profile must still be able to triage for it, which is
exactly what §4.1's `decided_by` ("for shared profiles") is for.

The rule Task 8 must actually enforce is **"exists, is active, and is readable
by this user"** — still, exactly, *the same predicate `resolve_profile_selection`
already applies to reads*. That framing was right and survives the merge
unchanged; only its CONTENT grew a third arm. Concretely: `can_read_profile(session,
user, profile_id)` from `web/profile_access.py`. See Task 8 gate 5.

The paragraph above still holds on the part that matters: **edit rights remain the
wrong rule.** A colleague a profile is *shared with* — a viewer, not the owner —
must still be able to triage for it, which is what §4.1's `decided_by` is for. The
read rule is deliberately wider than the edit rule (`profiles.py::_profile_for_edit`
returns 404 to a non-reader and 403 to a reader who is not the owner), and it is
the READ side this gate wants.

**Report the orphan loss, do not hide it.** Before the drop, count and log the dismissals
that fanned out to nothing (see the open question above):

```python
orphans = conn.execute(sa.text("""
    SELECT count(*) FROM user_call_dismissals d
    WHERE NOT EXISTS (SELECT 1 FROM call_decisions c
                      WHERE c.user_id = d.user_id AND c.call_id = d.call_id)
""")).scalar_one()
if orphans:
    print(f"0071: {orphans} dismissal(s) had no active subscribed or default "
          f"profile and were NOT migrated")
```

Then `op.drop_table("user_call_dismissals")`.

`downgrade()` recreates `user_call_dismissals` and collapses `state='hidden'` rows
back to distinct `(user_id, call_id)` pairs. It **cannot** restore the profile
dimension — say so in the docstring rather than pretending it round-trips.

- [ ] **Step 3: The backfill gate — this is the task's real test**

`tests/test_migration_0071_call_decisions.py`. Name what trivially passes first:
**a fixture with one user and one profile passes every wrong version of this
query.** So the fixture must make each wrong reading produce a *different* count.

Seed, for one user, four profiles and one dismissal:

| profile | active? | subscribed? | default? | must appear? |
| --- | --- | --- | --- | --- |
| P1 | yes | yes | no | **yes** |
| P2 | **no** | yes | no | no — inactive |
| P3 | yes | no | **yes** | **yes** |
| P4 | yes | no | no | no — another group's |
| P5 | yes | **yes** | **yes** | **yes, once** — subscribed *and* default |

Expected: **exactly 3 rows**, on P1, P3 and P5, all `state='hidden'`, `decided_at`
equal to `dismissed_at`, `decided_by = user_id`.

**P5 is the row that tests the `ON CONFLICT` claim.** It is both subscribed and the
default, so it is the case the old rationale said would conflict. Assert it appears
**exactly once** (`count(*) WHERE profile_id = P5` is 1) — that is what proves the
`OR` does not duplicate, rather than proving `DO NOTHING` cleaned up after it.

Add a second user, subscribed to P4 only, with no dismissal — proving the migration
does not fan one user's hide onto another's profile. And a third user with a
dismissal and **no** profiles at all, asserting the orphan count is 1 and no row
is written.

Also assert `user_call_dismissals` is **gone** (`inspector.has_table` is False) —
"the new table has rows" is true whether or not the old one was dropped.

- [ ] **Step 4: Mutation-test the gate (all four must go red)**

Run each inside one shell invocation, restoring with `cp` from a saved copy:

| # | Mutation in `0071`'s SQL | Must redden because |
| --- | --- | --- |
| B1 | drop `AND p.is_active = true` | P2 appears → 4 rows |
| B2 | drop `OR p.id = u.default_profile_id` | P3 missing → 2 rows (P5 survives via its subscription) |
| B3 | replace the whole `JOIN profiles` predicate with `p.is_active = true` | P4 appears → 4 rows |
| B4 | change `'hidden'` to `'saved'` | the state assertion |
| B4b | delete `ON CONFLICT (…) DO NOTHING` | **nothing — and that is the point.** Run it and record that the migration still succeeds with 3 rows. That is the evidence the clause is inert today; a row claiming it is load-bearing without this run is the "expected output is what the failure prints" shape. |

A mutation that reddens nothing means the fixture is too small — fix the fixture,
not the expectation.

- [ ] **Step 5: Verify**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_migration_0071_call_decisions.py -q
uv run alembic check
uv run python scripts/gen_architecture.py
test -n "$(git diff --name-only -- docs/ARCHITECTURE.md)" \
  || { echo "ARCHITECTURE.md did not change — the generator did not run"; exit 1; }
ruff check src tests alembic scripts
```

**`git diff --stat` is not a check.** Measured: on an unchanged file it exits **0**
with empty output, so `&& git diff --stat <file>` passes whether or not anything
changed. Every "this file must change" step in this plan uses the
`test -n "$(git diff --name-only -- …)"` form instead.

`alembic check` must report no pending autogenerate diff — that is what proves the
model and the migration agree. `docs/ARCHITECTURE.md` **must** change here (a new
table with a unique index and a CHECK); an unchanged file means the generator did
not run.

**Test delta from `5029 passed, 1 skipped`:** `+N` where N is the count this file
actually collects — measure it, do not predict it. Some existing
`user_call_dismissals` tests will move or be deleted in Task 2; count then.

---

### Task 2: Re-point every consumer at `call_decisions`

Spec §4.1. **Five** files name the old table (D-B4) — four read it and the fifth is
`db/models.py`, which defines it. All five change here, in one commit, and none may
keep working by accident: deleting the model in the same commit turns a missed site
into an `ImportError` rather than a wrong answer. **The deletion is the last edit of
the task, not the first** — see Task 1 for why doing it a commit early leaves a tree
that cannot import.

**Two frontend callers change too, and an earlier draft missed them entirely.**
`useHideCall`/`useUnhideCall` (`frontend/src/api/calls.ts:30-45`) post to
`/api/calls/{call_id}/hide` with **no profile**, and `CallDetail.tsx:16-17` is their
only caller — in **both** editions. Adding a required `profile_id` server-side
without touching these ships a 422 on every hide in the full edition.

**Files:**
- Modify: `src/call_manager/db/models.py` — **delete `class UserCallDismissal` here**, in the same commit as its last importer (moved out of Task 1)
- Modify: `src/call_manager/web/api/calls.py` (hide / un-hide / hidden list)
- Modify: `src/call_manager/web/dashboard.py` (the list filter)
- Modify: `src/call_manager/digest/assemble.py` (the exclusion set)
- Modify: `src/call_manager/ranking/golden_set.py` (`candidates_dismissed_high_score`)
- Modify: **`frontend/src/api/calls.ts`** — `useHideCall`/`useUnhideCall` send no `profile_id` today (`calls.ts:34,44`)
- Modify: **`frontend/src/routes/CallDetail.tsx`** — the only caller of those two hooks (`CallDetail.tsx:16-17`)
- Regenerate: `frontend/src/api/schema.gen.ts`
- Modify: the existing tests that name `UserCallDismissal`

**Interfaces:**
- Consumes: `CallDecision` (Task 1).
- Produces: profile-scoped hiding — consumed by Tasks 3, 4, 7.

**This is where the `full` edition's behaviour deliberately changes.** Full's
dismissal is global today. After this task it is scoped to the profile the
dashboard is currently matching against. The spec calls this out as *"a deliberate
semantic change, and it is not lossless"*: a profile created next month starts
clean, and an old hide reappears there. That is the intended trade — per-profile
decisions are the entire point — and it is the **one** place Plan B alters the full
edition. It must be in the commit message, not discovered by the first user.

- [ ] **Step 1: `web/dashboard.py` — the filter gains a profile**

The scalar subquery at `:326` becomes profile-scoped and state-scoped:

**`GET /api/calls` gains a `view` parameter, and this is the fix for the plan's
worst defect.** An earlier draft said both *"a SAVED call still belongs in the main
list"* and *"inbox is a `NOT EXISTS` over the same filters the list uses"*. Those are
two different lists. All three reviewers found it independently, and it was rated
CRITICAL by two: a saved card would leave the feed and then bounce back on refresh,
because nothing removed it.

Four views over one query, and **`archive` is the existing behaviour under a name**:

| `view` | predicate | who asks |
| --- | --- | --- |
| `archive` (default, omitted) | `NOT EXISTS` a `hidden` decision | the **full** edition, byte-for-byte today's rule |
| `inbox` | `NOT EXISTS` **any** decision | the match feed's default |
| `saved` | `EXISTS` a `saved` decision | the match rail |
| `hidden` | `EXISTS` a `hidden` decision | the match rail |

```python
# The four views differ ONLY in this predicate. Everything else -- source, q,
# show_closed, the ranking join, ordering, pagination -- is shared, so a saved
# call renders with the SAME card as an inbox call and there is no second
# endpoint to keep in sync.
#
# 'archive' is the default so an omitted parameter is exactly today's behaviour:
# the full edition sends no `view` and cannot notice this change.
def _decision_predicate(view, *, user_id, profile_id):
    rows = select(CallDecision.call_id).where(CallDecision.user_id == user_id)
    if profile_id is not None:
        rows = rows.where(CallDecision.profile_id == profile_id)
    if view == "inbox":
        return Call.id.notin_(rows.scalar_subquery())
    if view in ("saved", "hidden"):
        return Call.id.in_(rows.where(CallDecision.state == view).scalar_subquery())
    return Call.id.notin_(rows.where(CallDecision.state == "hidden").scalar_subquery())
```

**This also deletes a second defect.** An earlier draft gave Saved its own endpoint
returning `HiddenListOut` — `id`, `title`, `deadline`, `deadline_local_date`,
`source_name` and nothing else. That cannot render the card §5 specifies, and fixing
it per-row would be the N+1 Task 4 forbids. With `?view=saved` the saved list *is*
the list, with every card field already on it. **`GET /calls/saved` is therefore not
added at all**; `GET /calls/hidden` stays exactly as it is for the full edition.

**When `profile_id` is `None`** there is nothing to scope to. An earlier draft said
"suppress only if it is hidden on **every** profile the user has decided on — i.e.
drop the `profile_id` predicate", and all three reviewers caught that those are two
different queries. Dropping the predicate gives **any**: one hide anywhere hides the
call in the unranked view.

**The rule is ANY, deliberately.** It is exactly today's global behaviour, which is
what the no-profile view *is* — the full edition's archive, unchanged. "Every" would
be a new rule, needs a quantified subquery, and would resurrect calls the user has
already dealt with. Write the comment as "any", not "every":

```python
# profile_id is None -> the full edition's unranked archive. Suppress a call
# hidden on ANY of the user's profiles. This is today's behaviour verbatim, not a
# new rule: it is the one view where a per-profile decision has no profile to
# attach to. NOT "hidden on every profile" -- that is a different query and it
# would re-surface calls the user has already dealt with.
```

After Task 8 the **match** edition never reaches this branch (no profile means the
onboarding screen), so this is the full edition's path only.

- [ ] **Step 2: `digest/assemble.py` — one exclusion set per profile, not one per user**

`dismissed_ids` at `:101-107` is currently a single set for the whole user, applied
across every profile's candidate query. It becomes a `dict[int, set[int]]` keyed by
`profile_id`, and each profile's candidate query subtracts its own set.

**Filter on `state == "hidden"`, not on "has a decision".** Putting every
`CallDecision` in the exclusion set would drop **saved** calls out of the digest —
the one place a user has said they want the call. The spec's wording is precise:
*"a hidden call never appears in a later digest for that profile."* Two reviewers
flagged that gate 2 below (hide on P1, still present under P2) stays **green** under
a mutation that drops the state predicate, because its fixture only ever hides.

- [ ] **Step 3: `ranking/golden_set.py` — read the column, do not infer it**

`candidates_dismissed_high_score()` (`:94-124`) joins `Subscription` to *guess*
which profile a global dismissal belonged to. With a real `profile_id` the
`EXISTS` collapses to a direct correlation — **and must gain the state filter**:

```python
exists().where(
    and_(
        CallDecision.call_id == CallRanking.call_id,
        CallDecision.profile_id == CallRanking.profile_id,
        CallDecision.state == "hidden",   # a SAVED call is not a negative label
    )
)
```

Without that last line a saved call becomes a `dismissed_high_score` golden-set
candidate — a silent data-quality bug, and the exact reason D-B4 matters.

- [ ] **Step 4: `web/api/calls.py` — the three endpoints keep their URLs**

`POST /calls/{id}/hide`, `DELETE /calls/{id}/hide`, `GET /calls/hidden` keep their
paths and response models (spec: *"Hidden is a re-skin, not a new feature"*), and
gain a `profile_id` query parameter. `POST` becomes an upsert on the unique index
with `DO UPDATE SET state='hidden'`, so hiding a **saved** call flips it rather
than silently doing nothing — `ON CONFLICT DO NOTHING` would be the wrong
idempotency now that the row has two states.

- [ ] **Step 5: Gates**

1. **Per-profile isolation** (`tests/web/test_call_decisions_api.py`): one user,
   two profiles, one call. Hide on P1. Assert the call is **absent** from the list
   for P1 and **present** for P2. *What trivially passes:* a single-profile
   fixture. Two profiles is the whole test.
2. **The digest respects it per profile:** hide on P1, assert the call still
   appears under P2 in the assembled digest.
2b. **The digest keeps SAVED calls:** save a call on P1, assert it is still in
   **P1's** digest. Gate 2 alone cannot catch a "treat every decision as an
   exclusion" regression — its fixture never saves anything.
3. **A saved call is not hidden:** save on P1, assert it is still in P1's list.
4. **Golden set ignores saved:** save a call ranked ≥ 60, assert it is not returned
   by `candidates_dismissed_high_score`.
5. **The hidden LIST is scoped too, not just the dashboard.** Two profiles, one
   call, opposite states: `GET /calls/hidden?profile_id=P1` returns it,
   `?profile_id=P2` does not. Every named mutation below touches `dashboard.py`
   only — without this gate, deleting the profile predicate from
   `list_hidden_calls` reddens nothing.
6. **Un-hide is scoped by profile *and* state.** `DELETE /calls/{id}/hide` on P1
   must not remove a **saved** row for the same call, and must not touch P2's row.
   A delete keyed on `(user_id, call_id)` alone passes every other gate here.
7. **No consumer remains.** The grep has two traps, both real:
   - `grep` exits **1** on no match, so it must be written `! grep -rq … src/` or
     the check inverts.
   - **The obvious pattern fails on correct code.** The new `CallDecision`
     docstring says *"Replaces user_call_dismissals (migration 0071)"*, which lives
     in `src/`. Verified by building the case and running it: the gate reported
     `GATE FAILS on a CORRECT Task 1`. Match the **identifier**, not the prose:

     ```bash
     ! grep -rnE '\bUserCallDismissal\b' src/ && \
     ! grep -rnE 'user_call_dismissals' src/ --include='*.py' \
         | grep -vE '^\S+: *#' | grep -q . && echo "no consumers left"
     ```

     Simplest alternative, and the one to prefer: **do not put the old table's name
     in the new model's docstring.** Put it in the migration, where a grep over
     `src/` never looks.

- [ ] **Step 6: Mutation-test each gate**

| # | Mutation | Reddens |
| --- | --- | --- |
| B5 | drop `CallDecision.profile_id == profile_id` in `dashboard.py` | gate 1 |
| B6 | drop `CallDecision.state == "hidden"` in `dashboard.py` | gate 3 |
| B7 | revert `assemble.py` to a single user-wide set | gate 2 |
| B7b | `assemble.py` excludes **every** decision, not just `hidden` | gate 2b (and **not** gate 2 — that is the point) |
| B8 | drop the `state` filter in `golden_set.py` | gate 4 |
| B8b | drop the profile predicate from `list_hidden_calls` | gate 5 |
| B8c | `unhide` deletes on `(user_id, call_id)` only | gate 6 |
| B9 | `ON CONFLICT DO NOTHING` instead of `DO UPDATE` in `hide_call` | a "hide a saved call" test |
| B9b | frontend `useHideCall` stops sending `profile_id` | a full-edition CallDetail hide test |

Run B7b **against gate 2 as well** and record that gate 2 stays green. A mutation
that reddens the gate you expected and *also* passes one you thought covered it is
the cheapest way to find out which of your gates was decorative.

B9 needs a test that does not exist yet — write it, or the mutation reddens nothing
and the row is a claim.

- [ ] **Step 7: Verify**

```bash
uv run pytest -n 16 --dist loadfile
! grep -rnE '\bUserCallDismissal\b' src/ && echo "no consumers left"
ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py
test -n "$(git diff --name-only -- docs/ARCHITECTURE.md)" || \
  { echo "ARCHITECTURE.md did not change"; exit 1; }
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || \
  { echo "schema.gen.ts did not change — hide gained a parameter, it must"; exit 1; }
pnpm -C frontend test && pnpm -C frontend build
```

`pnpm -C frontend build` and the full frontend suite run **here**, not only at the
end: this is the task that changes an endpoint both editions already call.

---

### Task 3: Save — the new half of the pair

Spec §4.1: *"**Saved** is the genuinely new half."* Hidden already exists and was
re-pointed in Task 2; this task adds the other direction, the counts the rail
needs, and the undo path a keypress-driven UI requires.

**Files:**
- Modify: `src/call_manager/web/api/calls.py`
- Modify: `tests/web/test_call_decisions_api.py`
- Regenerate: `frontend/src/api/schema.gen.ts`

**Interfaces:**
- Consumes: `CallDecision` (Task 1), the profile-scoped filter (Task 2).
- Produces: the endpoints Task 7's feed calls.

- [ ] **Step 1: Endpoints**

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/calls/{call_id}/save` | `profile_id` **required** | `CallDecisionOut{state}` |
| `DELETE` | `/api/calls/{call_id}/save` | `profile_id` **required** | `CallDecisionOut{state: null}` |
| `GET` | `/api/calls/decisions/counts` | `profile_id` **required** | `{inbox, saved, hidden}` |

**There is no `GET /calls/saved`.** The saved list is `GET /api/calls?view=saved`
(Task 2 Step 1), which returns the same `CallOut` cards as the inbox. Two paths are
added in total, not three.

`POST .../save` upserts with `DO UPDATE`, mirroring Task 2's hide, so `s` on a
hidden call flips it. `DELETE` removes the row — back to undecided, which is what
undo means when undecided is the absence of a row.

**A flip updates three columns, not one.** `SET state=…, decided_at=now(),
decided_by=:actor`. An earlier draft wrote only `state`, which leaves a flipped card
sorted by the time of the decision it *replaced*, and attributes it to whoever made
that earlier one — on a shared profile, the wrong colleague. Both Saved and Hidden
lists order by `decided_at`, so this is visible, not theoretical.
Gates 2 and 2b each assert all three columns moved.

**Route-order hazard — and my first version of this paragraph was wrong while
carrying the word "measured".** It claimed `/calls/hidden` survives because
`call_id: int` fails to parse `"hidden"` and *"FastAPI falls through"*. A reviewer
disputed it; I then actually measured it, twice.

The real registration order (`create_app()`, filtered to `/api/calls`):

```
['POST']    /api/calls/{call_id}/hide
['DELETE']  /api/calls/{call_id}/hide
['GET']     /api/calls/hidden
['GET']     /api/calls/{call_id}
['GET']     /api/calls
```

`/calls/{call_id}/hide` has three segments and never competes with a two-segment
path. The only competitor is `/calls/{call_id}`, and `/calls/hidden` is declared
**before** it. And there is **no fallthrough** — a two-route probe, both orders:

```
hidden declared FIRST:  200 {"who":"hidden"}
hidden declared SECOND: 422 {"detail":[{"type":"int_parsing","loc":["path","call_id"]…
```

So the rule is plain declaration order against `/calls/{call_id}`, and a wrong order
is a **422 that reads like a validation bug**, not a 404. **Declare
`/calls/decisions/counts` above `/calls/{call_id}`** and gate it with a 200
assertion. The mutation (B11) moves it below and must produce that 422.

*Recorded as a process failure, not just a fixed line:* I wrote "measured" on a
claim I had reasoned out. That is the exact thing `measure-dont-predict` exists to
stop, and the label made the error more expensive, not less — a reviewer had to
spend a finding on it.

- [ ] **Step 2: The counts endpoint is where inbox-zero comes from**

Each count is `_decision_predicate(view, …)` from Task 2 Step 1, applied to the same
call-matching filters the list uses, over one of the three views. **One helper,
three calls — not one shared "the list predicate" helper.** An earlier draft said the
latter, and a reviewer pointed out that the same helper cannot both exclude only
hidden (the full dashboard) and exclude every decision (the inbox).

Reusing the predicate function is what stops the rail saying "3" over an empty feed.

- [ ] **Step 3: Gates**

1. `save` then `GET /api/calls?view=saved` returns it; `?view=hidden` and
   `?view=inbox` do not.
2. `hide` a **saved** call → it leaves saved, enters hidden, exactly **one** row
   (`SELECT count(*)` = 1, not 2).
2b. `save` a **hidden** call → it leaves hidden, enters saved, exactly **one** row.
   *This mirror is not decorative.* A reviewer showed that gate 2 alone cannot test
   `save_call`'s upsert at all: its first `save` hits an empty row, where
   `DO UPDATE` and `DO NOTHING` are indistinguishable, and the flip to `hidden` is
   done by `hide_call`. Without 2b, a regression of `save_call` to `DO NOTHING`
   returns 200 while silently leaving the call hidden, and every gate stays green.
3. `DELETE .../save` on an undecided call is a 200, not a 404 (idempotent undo).
4. **Each count is asserted separately against its own list**, on an asymmetric
   fixture — 4 inbox / 2 saved / 1 hidden — and each is compared with
   `len(GET /api/calls?view=<that view>)`.

   *What trivially passes, and an earlier draft shipped both:*
   - `0 + 0 + 0 == 0` — an empty fixture satisfies any sum. Hence non-empty.
   - **`inbox + saved + hidden == total` passes with saved and hidden swapped**,
     even on 4/2/1. A reviewer found this one. A sum cannot detect a permutation,
     so the sum is not the gate — three separate equalities are. Also: if `total`
     is computed as the sum of the three, the equation is a tautology; compare
     against `pagination.total` of the matching view instead.
5. `GET /api/calls/decisions/counts` → 200, not 422 (the route-order gate — see
   Step 1; this is the literal path that competes with `/calls/{call_id}`).
6. Another user's decision is invisible: seed a second user's save on the same
   call/profile, assert it is absent from the first user's `?view=saved` **and**
   from their counts.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B10 | `DO UPDATE` → `DO NOTHING` in `save_call` | gate **2b** (**not** gate 2 — run it against gate 2 too and record that gate 2 stays green) |
| B11 | move `/calls/decisions/counts` below `/calls/{call_id}` | gate 5 — expect **422**, not 404 |
| B12 | `counts` computes `saved` with the `hidden` predicate and vice versa | gate 4. **Not** "drop the `state` predicate": `inbox` is a `NOT EXISTS` with no state predicate to drop, so that mutation is a no-op — a reviewer caught it. Swapping the two is the failure this gate must actually catch. |
| B13 | drop `user_id` from the `?view=saved` predicate | gate 6 |
| B13b | `_decision_predicate` returns the `archive` branch for `view="inbox"` | the inbox must stop excluding saved calls — if no gate reddens, Task 7's feed has no test |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_call_decisions_api.py -q
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || \
  { echo "schema.gen.ts did not change - the generator did not run"; exit 1; }
```

`schema.gen.ts` **must** change — four new operations. An unchanged file means the
generator did not run, and the drift gate can pass without generating.

---

### Task 4: The list schema grows the fields the card needs

Spec §5. Measured (`MEASUREMENTS.md` §1): `CallOut` (`calls.py:33-50`) carries only
`id`, `title`, `deadline`, `deadline_local_date`, `published_at`, `source_name`,
`source_slug`, `aggregate_score`. Everything else the card shows lives on
`CallDetailCallOut` (`calls.py:109-125`). **This is real backend work, not free.**

**Files:**
- Modify: `src/call_manager/web/api/calls.py` (`CallOut`, `get_calls` at `:404-416`)
- Create: `tests/web/test_calls_list_query_count.py`
- Regenerate: `frontend/src/api/schema.gen.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces: the card's data — consumed by Tasks 5 and 7.

- [ ] **Step 1: Add the fields**

To `CallOut`: `programme`, `budget_min`, `budget_max`, `currency`, `url`,
`summary`, `keywords`, plus `criteria: list[CallListCriterionOut]`,
`applicant_note_count: int` and `escalated: bool`.

**Safe on the raiseload contract, measured:** `dashboard.py:290-292` defers
`embedding`, `full_text` and `raw` with `raiseload=True` — and **only** those.
Every field added here is a plain column, so none of them raises. This is worth
stating because a `raiseload` violation surfaces as a 500 in production and as a
green test suite anywhere the fixture happens not to touch the row.

Payload does grow: `summary` and `full_text` share a trigram index and `summary`
can be long. If the list response becomes a problem, truncate `summary`
server-side — **do not** re-defer it, which would reintroduce an N+1.

- [ ] **Step 2: One batched criteria query — the part that can go quietly wrong**

`view.calls` is typed `list[tuple[Call, CallRanking | None]]` (`dashboard.py:52`,
measured) — the ranked query is an **outer** join and the unranked branch selects
`(Call, None)`, so `ranking` is `None` for a normal, common row:

```python
# `ranking` is Optional. An earlier draft wrote len(ranking.applicant_notes or [])
# and would have raised AttributeError on every unranked call -- which is the
# DEFAULT list state, not an edge case: the no-profile view has no rankings at all,
# and even a ranked view LEFT JOINs, so a newly ingested call has none.
applicant_note_count = 0 if ranking is None else len(ranking.applicant_notes or [])
escalated = bool(ranking is not None and ranking.escalated)
```

`escalated` is its own column and is **not** derivable from the note count: §5 wants
"N things to check" on escalated calls, and `CallRanking.escalated` can be true with
zero notes (analysis can fail or be capped after escalation). A reviewer caught that
the card's rule and the field the plan added were different things.

Criteria need exactly **one** more query, after the page is known:

```python
# ONE query for the whole page, not one per row. The obvious implementation —
# reading call.rankings[i].criteria inside the list comprehension — is a lazy
# load per row, which is invisible in a 1-row test fixture and is 50 round trips
# on a full page. tests/web/test_calls_list_query_count.py is the gate.
criteria_by_call: dict[int, list[CallRankingCriterion]] = defaultdict(list)
if profile_id is not None and call_ids:
    for c in session.execute(
        select(CallRankingCriterion).where(
            CallRankingCriterion.profile_id == profile_id,
            CallRankingCriterion.call_id.in_(call_ids),
        )
    ).scalars():
        criteria_by_call[c.call_id].append(c)
```

Reuse `criterion_label` (`web/filters.py:67`) and the `is_topic_fit` /
`is_reason_code` derivations from `calls.py:350-365` — do not re-implement them,
or the list and the detail page will disagree about what "Budget" is called.

- [ ] **Step 3: The no-N+1 gate**

Count statements with a SQLAlchemy `before_cursor_execute` event listener, then:

```
seed 1 call,  request the list WITH ?profile= -> record N1
seed 10 calls, request the list WITH ?profile= -> record N10
assert N10 == N1        # constant, not "small"
```

**The `?profile=` is load-bearing and an earlier draft omitted it.** Step 2's
batched query is guarded by `if profile_id is not None`, so a request without one
skips the criteria block **in both runs** — batched or mutated-to-per-row — and the
gate passes over code it never executed. A reviewer caught this; it is the
"zero rows satisfy every universal claim" shape.

Seed at least one **unranked** call in the same fixture, so the `ranking is None`
path (Step 2) is executed here too rather than only in a unit test.

*What trivially passes:* `assert N10 < 60`. A ceiling passes a per-row loop on a
small page. **Equality against the 1-call run is the gate**; a ceiling is the
"weaker property than the one claimed" hollow-gate shape.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B14 | replace the batched query with a per-call `select(...)` inside the comprehension | the query-count gate (N10 = N1 + 9) |
| B15 | drop `CallRankingCriterion.profile_id == profile_id` | a cross-profile leak test — **write it**: two profiles ranked the same call differently, assert the list shows only the selected profile's chips |
| B16 | `applicant_note_count` from `len(ranking.applicant_notes)` without the `or []` | a `NULL applicant_notes` fixture (the local Gemma path always leaves it NULL — the common case, not the edge case) |
| B16b | `applicant_note_count = len(ranking.applicant_notes or [])`, dropping the `ranking is None` guard | the **unranked-call** fixture — `AttributeError`. If nothing reddens, the fixture has no unranked row and the gate is decorative |
| B16c | `escalated = applicant_note_count > 0` | a fixture with `escalated=True` and zero notes |
| B16d | request the list **without** `?profile=` in the query-count gate | nothing — and that is the finding. Record that it passes, as proof the precondition above is real |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_calls_list_query_count.py -q
uv run pytest -n 16 --dist loadfile
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || \
  { echo "schema.gen.ts did not change - the generator did not run"; exit 1; }
```

---

### Task 5: Criterion chips

Spec §4.2, list view only. The detail page already renders one card per criterion
(`CallDetailBody.tsx:155-181`, measured) — **do not touch that**, except for the
removal in Task 6.

**Files:**
- Create: `frontend/src/components/CriterionChips.tsx`, `.module.css`, `.test.tsx`
- Create: `tests/architecture/test_criterion_vocabulary.py`

**Interfaces:**
- Consumes: `CallOut.criteria` (Task 4).
- Produces: the chip row — consumed by Task 7's card.

- [ ] **Step 1: Build to the measured vocabulary, not the spec's**

Seven criteria (D-B1) and five rating shapes (D-B2):

```tsx
// Rating -> tone. Measured from the source, NOT from spec §4.2, which lists six
// criteria and four ratings. ranking/worker.py:365-369 writes a seventh criterion
// ("embedding" -> "Topic similarity"), and ranking/criteria.py:30 gives the four
// GATE criteria a third state, "unknown", that the spec does not mention. A chip
// built to the spec falls through on every gate that could not judge.
const TONE: Record<string, "pass" | "fail" | "unsure"> = {
  pass: "pass", fail: "fail", unclear: "unsure", unknown: "unsure",
};
// topic_fit is numeric ("1".."5"), rendered as "Topic fit 4/5" and toned on >= 4.
```

An **unrecognised** rating renders as `unsure` with the raw string, never blank —
`criterion` is a bare `String(20)` with no CHECK, so a future model value reaches
this component unfiltered.

Render **1..7 chips, and handle 0** (D-B3: the gate loop returns on first FAIL, so
one chip is a normal card). Zero chips renders nothing, not an empty row.

Expansion shows `quoted_evidence` in Georgia (spec §5's serif-on-quotes), then
`rationale` **labelled as the model's reasoning**, never interleaved.

- [ ] **Step 2: Pin the vocabulary against the backend**

`tests/architecture/test_criterion_vocabulary.py` reads
`web/filters.py::_CRITERION_LABELS` **and both** criterion sources in
`ranking/worker.py` via `ast`, then asserts the frontend's criterion list covers
every one.

**Both sources, because a `criterion=` grep sees 3 of 7.** Measured:

```
376:  criterion=name,          <- a VARIABLE, not a literal
399:  criterion="topic_fit",
410:  criterion="eligibility",
421:  criterion="exclusions",
```

The four gate criteria are string literals in a **tuple list** several lines above:

```
366:  ("budget",    r.gates.budget),
367:  ("trl",       r.gates.trl),
368:  ("timeline",  r.gates.timeline),
369:  ("embedding", r.gates.embedding),
```

An earlier draft of this gate scanned only `criterion=` and would have pinned
**three** of seven while reporting success — a reviewer caught it, and my own D-B1
measurement had the tuple in front of me at the time. The gate must collect the
first element of every tuple in the `gate_results` assignment **and** the
`criterion=` literals, and union them.

**This is the gate that stops D-B1 recurring:** an eighth criterion added the way
the existing four were added goes red, instead of the UI rendering a blank chip.

*What trivially passes:* asserting the frontend is a **subset** of the backend.
That is the first hollow-gate shape, and it passes when the frontend knows nothing.
Assert the frontend **covers** the backend — every backend criterion appears in the
frontend list.

- [ ] **Step 3: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B17 | delete `"embedding"` from the frontend criterion list | the coverage gate |
| B18 | add `criterion="foo"` to `worker.py` | the coverage gate (proves it reads the real source, not a copy) |
| B18b | add `("foo", r.gates.foo)` to `gate_results` — **the way the real four are declared** | the coverage gate. B18 alone passes a gate that only scans `criterion=`; this is the mutation that proves the tuple scan exists |
| B19 | delete `unknown` from `TONE` | a chip test rendering `rating="unknown"` |
| B20 | render `criteria.slice(0, 6)` | a 7-chip test |

B18 is the important one: a gate that reads a hand-maintained list in a test file
instead of the actual source is the "expected output is what the failure prints"
shape wearing a disguise.

- [ ] **Step 4: Verify**

```bash
pnpm -C frontend test src/components/CriterionChips.test.tsx
uv run pytest tests/architecture/test_criterion_vocabulary.py -q
pnpm -C frontend lint
```

---

### Task 6: Delete the raw "Aggregate score" block from the detail page

Spec §4.2: *"the raw aggregate is not shown in the UI"*, and *"the only work there
is deleting its raw Aggregate score block"*.

**Files:**
- Modify: `frontend/src/routes/CallDetailBody.tsx`

Measured (D-B5): the block is at **63–93**, not the spec's `57-67` — Plan A moved
it by exactly 10 lines. **Anchor on the string `Aggregate score`, not the number.**

`CallDetail` is mounted by **both** routers, so this is a `{!isMatchEdition && (…)}`
gate, exactly like Plan A's E1 — not a deletion. Deleting it would strip the score
from the full edition, which Global Constraint 3 forbids.

- [ ] **Step 1: Gate it.** Keep `{ranking && (…)}`'s other contents (the reason
  label, the model attribution); gate only the `{ranking.aggregate_score}` figure
  and its "Aggregate score" heading.
- [ ] **Step 2: Test both editions.** The existing edition-aware mock pattern from
  `Dashboard.test.tsx` (mutable holder + getter mock) applies. Assert the string
  `Aggregate score` is **absent** in `match` and **present** in `full`.
  *What trivially passes:* asserting absence in `match` only — that is the
  "absence already true" shape if the component failed to render at all. Assert
  presence in `full` **in the same file**, and assert some other detail-page string
  is present in `match`, proving the component rendered.
- [ ] **Step 3: Mutate.** B21: remove the `!isMatchEdition &&` guard — the match
  assertion must go red. B22: make the whole `{ranking && …}` section unconditional
  `null` — the **full** assertion must go red. B22 is what proves the test is not
  passing on a component that renders nothing.
- [ ] **Step 4: Verify.** `pnpm -C frontend test src/routes/CallDetailBody.test.tsx`,
  `pnpm -C frontend build`, `pnpm -C frontend build:match && pnpm -C frontend check:bundle`.

---

### Task 7: The triage feed

Spec §5. The researcher's screen is the product: a single ~680 px expanding column,
a 240 px left rail for orientation only, `j`/`k`/`s`/`h`, and an inbox that ends.

**Files:**
- Create: `frontend/src/routes/TriageFeed.tsx`, `.module.css`, `.test.tsx`
- Create: `frontend/src/routes/TriageCard.tsx`, `.test.tsx`
- Create: `frontend/src/api/decisions.ts`
- Modify: `frontend/src/router.simple.tsx` (index route)

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces: the match edition's index screen.

**The edition split is the mount point, not a branch.** `router.simple.tsx:60`
currently mounts the same `Dashboard` as the full edition. It mounts `TriageFeed`
instead. `Dashboard.tsx`, `CallRow.tsx` and `MatchBadge.tsx` are then reachable
only from `router.tsx` and **leave the match bundle entirely** — which is the same
mechanism Plan A used, and is why no `isMatchEdition` branch is needed in any of
them. Leave Plan A's existing branches in `CallRow.tsx` in place: they still gate
correctly for `full`, and deleting them would redden `noCockpitLinks.test.tsx`.

**Expect gate 1's numbers to move.** `check_match_bundle.mjs` last printed
`384 modules, 0 under 14 excluded paths, 47 app modules all under 5 declared areas`.
Dropping `Dashboard`/`CallRow`/`MatchBadge` and adding four files changes both
counts. **Re-measure and update the expectation from the run — never adjust it to
make the arithmetic close.** The gate must still exit 0 and still report `0` under
the excluded paths; that zero is the guarantee, the totals are not.

- [ ] **Step 1: The three views and the rail**

Rail: Inbox / Saved / Hidden with counts from `GET /calls/decisions/counts`, plus
the profile being matched against. View selection lives in the URL
(`?view=saved`), so a reload and a back button both work.

- [ ] **Step 2: The card**

Field order from §5's table — *what is this, what does it fund, why is it in front
of me, can I apply*: title, funder, programme (omit the separator when absent),
deadline + days left, budget as a **range when min ≠ max**, `summary`, the matched
sentence, chips (Task 5), keywords, and "N things to check" for escalated calls.

**Every optional field is usually absent** — `CallRanking.summary` and
`applicant_notes` are written only on the remote path, capped at
`RANKING_REMOTE_DAILY_CAP`. Where `Call.summary` is missing, §5 requires a sentence
and a link out, **not** a collapsed section: an empty space reads as a bug.

- [ ] **Step 3: Keys**

`j`/`k` move focus, `s` saves, `h` hides. A decided card leaves the list. The
listener **must not fire while focus is in an `input`, `textarea` or
`contenteditable`** — otherwise typing "she" in a future note field hides three
calls. This guard is the single most likely thing to be written correctly and then
lost in a later refactor, so it gets its own gate and its own mutation.

- [ ] **Step 4: Inbox zero is a designed screen, not an empty div**

Spec §5: *"Reaching zero is the reward, and the inbox-zero state is a designed
screen, not an afterthought."* Assert on its actual copy.

- [ ] **Step 5: Gates**

1. Renders the feed; a card shows title, funder, deadline.
2. A card with `summary: null`, `keywords: []`, `criteria: []`, `programme: null`,
   all budget fields null still renders and shows the "no summary" sentence.
   *This is the common case, not the edge case.*
3. `j` then `s` saves the **second** card, not the first. *What trivially passes:*
   pressing `s` with one card. Use at least three.
4. `s` typed inside a focused editable element triggers **no** mutation —
   **parameterized over all three**: `<input>`, `<textarea>`, and a nested
   `contenteditable`. An earlier draft tested only `<input>`, which a reviewer
   pointed out leaves B23 green when only the `textarea` and `contenteditable`
   branches are deleted. One target is not a guard, it is one third of a guard.
5. Zero items renders the inbox-zero copy.
6. `?view=saved` renders the saved list and marks the rail item current.
7. Counts render from the API, not from `items.length`. **The stub must make the
   two differ** — e.g. `counts.inbox = 99` over 3 items. A reviewer showed that a
   stub where `counts.inbox === items.length` leaves B25 green, because both
   sources agree.
8. **No raw score on the card.** Assert the card renders criterion chips **and**
   that no `MatchBadge`/`data-band` element and no bare aggregate number is
   present. Task 6 removes the raw score from the *detail* page; without this gate,
   adding `{call.aggregate_score}` to `TriageCard` leaves every other assertion in
   Tasks 7 and 9 green — which would quietly undo §4.2's whole point on the one
   screen §4.2 is about.

- [ ] **Step 6: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B23 | remove the input-focus guard from the key handler | gate 4 |
| B24 | `j` moves by 0 | gate 3 |
| B25 | render `items.length` in the rail instead of the counts response | gate 7 (only with the differing stub) |
| B25b | render `{call.aggregate_score}` in `TriageCard` | gate 8 |
| B26 | return `null` from the empty branch | gate 5 |
| B27 | drop the `summary === null` branch | gate 2 |

- [ ] **Step 7: Verify**

```bash
pnpm -C frontend test src/routes/TriageFeed.test.tsx src/routes/TriageCard.test.tsx
pnpm -C frontend lint
pnpm -C frontend build          # the FULL edition must still build
pnpm -C frontend build:match && pnpm -C frontend check:bundle
uv run pytest tests/architecture -q
```

`pnpm -C frontend build` is not optional here. Moving the index route is exactly
the kind of change that can drop a module the full router still imports.

---

### Task 8: No profile is an onboarding screen, not a firehose

Decided by the human, 2026-09-04 (see *Decided by the human* above). Release 1's
first screen for a user with no profile must be an invitation to make one, not 50
unranked calls.

**Files:**
- Create: `frontend/src/routes/TriageEmptyProfile.tsx`, `.test.tsx`
- Modify: `frontend/src/routes/TriageFeed.tsx` (the branch)
- Modify: `src/call_manager/web/api/calls.py` (`hide`/`save` require a profile)
- Modify: `tests/web/test_call_decisions_api.py`

**Interfaces:**
- Consumes: `DashboardOut.effective.profile_id` (already on the wire — Task 7 reads it).
- Produces: the guarantee Task 1's migration leans on, that no new orphan decision
  can be created.

**This is a match-edition change only.** `dashboard.py`'s unranked fallback stays
exactly as it is: the full edition's archive view is not release 1's problem, and
touching it would break Global Constraint 3 for no gain. The split is the same one
Task 7 uses — `TriageFeed` is mounted only by `router.simple.tsx`, so the branch
lives there and `Dashboard.tsx` never sees it.

- [ ] **Step 1: The frontend branch**

`TriageFeed` reads `effective.profile_id` from the list response. When it is `null`,
render `TriageEmptyProfile` **instead of** the feed — not above it, not with an empty
list below it. The screen says what a profile is for in one sentence and links to
`/profiles/new`; if the user has profiles but none selected, it lists them instead
of sending them to create a duplicate.

Distinguish this from **inbox zero** (Task 7). They are different screens with
different copy: inbox zero is *"you're done"*, this is *"you haven't started"*.
Shipping one for the other is the likely bug, so the tests assert on the two copies
being different strings.

- [ ] **Step 2: Make the backend refuse too**

A screen is not a guarantee — the endpoint is. `POST/DELETE /calls/{id}/hide` and
`/save` gain a **required** `profile_id`, and return **422** without one. That is what
actually closes the orphan path, and it is what lets Task 1's migration say "no new
orphan can be created" rather than "the UI does not offer it".

**This is a breaking change to an existing endpoint** — `POST /calls/{id}/hide`
currently takes no profile. Nothing in-tree calls it without one after Task 3's
frontend work, but say so in the commit message; it is the second place (with Task 2)
where Plan B alters behaviour the full edition already had.

- [ ] **Step 3: Gates**

1. `profile_id = null` in the list response renders `TriageEmptyProfile`, and the
   feed's own list element is **absent**. *What trivially passes:* asserting the
   onboarding copy is present — a component rendering both would pass that. Assert
   the feed is gone in the same test.
2. Inbox-zero copy and no-profile copy are **different**, asserted as two distinct
   strings against the two states. This is the gate that catches shipping one screen
   for both.
3. A user with profiles but none selected sees the picker, not `/profiles/new` alone.
4. `POST /api/calls/1/hide` with no `profile_id` → **422**.
5. `POST /api/calls/1/hide` with a `profile_id` that **does not exist or is
   inactive** → 4xx, and **no row written**. *This is the one that matters:* a
   required parameter that is never validated is required in name only, and a
   `hidden` row pointing at a dead profile is invisible in every view.

   **Not "another group's profile".** An earlier draft asked for that, on a mistaken
   belief that no ownership model existed. It does (`profiles.py:342`, spec §4.3),
   but it governs **editing** a profile, which is the wrong rule here: a decision is
   keyed `(user_id, profile_id, call_id)` and read only by its own author, so
   deciding against a profile you can legitimately see affects no one. Requiring
   edit rights would break shared-profile triage — the thing `decided_by` exists
   for. Reject dead ids; do not invent an authorization rule this codebase does not
   have.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B28 | render the feed **and** the onboarding screen | gate 1 |
| B29 | reuse the inbox-zero component for the no-profile branch | gate 2 |
| B30 | `profile_id: int \| None = Query(default=None)` on `hide` | gate 4 |
| B31 | drop the exists-and-active check on `profile_id` | gate 5 — the fixture must use a **deleted** id *and* an **inactive** one; an id of `999999` alone passes a check that only tests `is_active` |

B31 is the one to write the test for **first**. Gate 4 passes the moment the
parameter is declared required; gate 5 is the only one that proves it means anything.

**Gate 5 is writable, so there is no escalation here.** An earlier draft flagged this
task as a possible stop-and-escalate on the grounds that the codebase had no
authorization model to express the rule. Measured, that was wrong twice over: the
model exists, and it is not the rule this gate needs.

- [ ] **Step 5: Verify**

```bash
pnpm -C frontend test src/routes/TriageEmptyProfile.test.tsx src/routes/TriageFeed.test.tsx
uv run pytest tests/web/test_call_decisions_api.py -q
uv run pytest tests/test_api_calls_feed_behavior.py -q -k unranked   # 3 passed, UNCHANGED
pnpm -C frontend build && pnpm -C frontend build:match && pnpm -C frontend check:bundle
pnpm -C frontend gen:api
# NB: on its own line. `A && B || C` would run C when A fails, reporting a codegen
# crash as "the generator did not run" -- true, but it hides the real error.
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || \
  { echo "schema.gen.ts did not change - the generator did not run"; exit 1; }
```

The unranked-feed tests must stay **green and untouched** — they are the full
edition's behaviour, and this task must not move them. If they go red, the branch
leaked out of the match edition.

---

### Task 9: Browser coverage and CI

Plan A wired `frontend/e2e/match.spec.ts` (10 scenarios, `gotoRendered()` walking
all 15 mounted routes) and `tests/architecture/test_match_gates_are_wired.py`
(pins the scenario titles and an `expect(` floor of 18 against a measured 22).
Both must grow with this plan, or CI keeps proving Plan A's product.

**Files:**
- Modify: `frontend/e2e/match.spec.ts`
- Modify: `tests/architecture/test_match_gates_are_wired.py`

- [ ] **Step 1: New scenarios** against `page.route` API mocks: the feed renders;
  `s` moves a card to Saved; `h` moves it to Hidden; un-hide returns it to the
  inbox; inbox-zero appears when the last card is decided; a chip expands to show
  its quoted evidence; **and a session with `profile_id: null` lands on the
  onboarding screen, not on a list of calls** (Task 8).
- [ ] **Step 2: Update the pins — and change the `expect(` count from a FLOOR to an
  EQUALITY.** Plan A pinned `>= 18` against a measured 22. Two reviewers pointed out
  that a floor with four spare assertions is four assertions anyone can delete, and
  that `expect(true).toBe(true)` keeps any count green. Pin `== <measured>`; a
  legitimate new assertion then updates the number deliberately, which is the point.

  **Measure both after writing the scenarios.** The Plan A run wrote `>= 30`
  commented "measured at 40" when the real count was 22, and it failed on its first
  run. Write the number the command prints, not the number you expect.
- [ ] **Step 3: Mutation-test.** B32: delete one new scenario — the title pin goes
  red. B33: strip the assertions from one scenario body but keep its title — the
  `expect(` count goes red. B34: replace one real assertion with
  `expect(true).toBe(true)` — **the count is unchanged, so this must be caught by a
  behavioural mutation instead**, not by the counter.

  **The counter is a smoke alarm, not a gate.** For each of the six scenarios, name
  the production behaviour it protects and mutate *that* (e.g. make `s` call the
  hide endpoint; make the rail counts static; make un-hide a no-op). A scenario
  whose behavioural mutation reddens nothing is a scenario that asserts nothing,
  whatever its `expect(` count says.
- [ ] **Step 4: Verify.**

```bash
pnpm -C frontend e2e:match
uv run pytest tests/architecture/test_match_gates_are_wired.py -q
```

---

## Definition of done for Plan B

From a clean tree, in this order:

```bash
cd /home/parlar_ai/dev/call-monitor
ruff check src tests alembic scripts
uv run alembic check
uv run pytest -n 16 --dist loadfile
uv run pytest tests/test_api_calls_feed_behavior.py -q -k unranked   # 3 passed, UNTOUCHED
uv run python scripts/gen_routes.py --check
uv run python scripts/gen_architecture.py && git diff --exit-code docs/ARCHITECTURE.md
uv run python scripts/gen_env_example.py && git diff --exit-code .env.example
pnpm -C frontend gen:api && git diff --exit-code frontend/src/api/schema.gen.ts
pnpm -C frontend test
pnpm -C frontend lint
pnpm -C frontend build
pnpm -C frontend build:match && pnpm -C frontend check:bundle
pnpm -C frontend e2e:match
```

Expectations:

- **`1 skipped` — exactly one.** `0 skipped` means a live Anthropic call ran.
- **The pass total will be higher than `5029`** (measured on master at
  `ed1153a3`, 2026-09-04; it was `4915` when this plan was first written). State the number you measured and
  the per-task deltas that produce it. Do **not** copy a predicted total into this
  section — the Plan A brief's stale `3361` is why this line exists.
- `tests/architecture/full_edition_routes.txt` goes **107 -> 109**, and the diff is
  exactly these two added lines:

  ```
  /api/calls/decisions/counts
  /api/calls/{call_id}/save
  ```

  Any third line is a finding — `?view=` is a query parameter on the existing
  `/api/calls`, not a route. (The file is unique paths; `POST`+`DELETE /save`
  is one line. The SPA pin `routes.gen.ts` does **not** move.)
- `docs/superpowers/specs/` byte-identical to `f0aaed0c`:
  `git diff --quiet f0aaed0c -- docs/superpowers/specs/` exits 0. (`--quiet`, not
  `--stat`: `--stat` exits 0 either way.)
- `.env` untouched. Never read, copy or back it up.
- `check_match_bundle.mjs` exits **0** and still reports **`0`** modules under the
  14 excluded paths. The other two numbers will have moved; record what they are.

Then a manual smoke, because no gate here proves the app is usable:

```bash
pnpm -C frontend build:match
pnpm -C frontend check:bundle
pnpm -C frontend package:match
APP_EDITION=match DRAFTING_ENABLED=false SPA_MODE=prod \
  uv run uvicorn call_manager.main:create_app --factory
```

Confirm by hand: the inbox lists calls with chips and no raw score; `j`/`k` move;
`s` empties one card into Saved; `h` into Hidden; un-hide brings it back; deciding
the last card shows the inbox-zero screen; switching profile changes which calls
are hidden; a chip expands to the funder's own sentence.

---

## What Plan B deliberately does not do

Named so a reviewer does not read them as omissions:

- **No boards, no approval queue, no `/b/:slug`.** Plan C.
- **No share-by-email, no change detection.** Plan D.
- **No note UI.** `call_decisions.note` is created and nothing writes it, so the
  note becomes a frontend change later rather than a second migration.
- **No swipe gesture and no phone rail.** §5's responsive strategy asks for swipe
  decisions and a bottom-tab rail on phones. Neither is here. **A reviewer pointed
  out that no later plan owns them either** — C is boards, D is §7 extras — so
  deferring them silently would drop them. They are hereby assigned to **Plan D**,
  recorded as a spec deviation until then: release 1 on a phone is the same single
  column with tap targets, not a gesture surface.
- **`MatchBadge` is not deleted**, and the full edition's `Dashboard`/`CallRow`
  keep it. §4.2 replaces the number *in release 1*; the full edition is not release 1.
- **`/cockpit` still returns HTTP 200 with the SPA's soft 404** in a match build —
  Plan A's recorded limitation, unchanged here.
- **The full edition's no-profile archive view is untouched.** Without a profile
  `dashboard.py` still returns every open call, newest first, 50 a page. Task 8
  replaces that screen in the **match** edition only. Changing it for `full` would
  break Global Constraint 3 and is not release 1's problem.
- **The old global-hide semantics are not preserved for the full edition.** This is
  the deliberate, spec-sanctioned loss described in Task 2, not an oversight.
