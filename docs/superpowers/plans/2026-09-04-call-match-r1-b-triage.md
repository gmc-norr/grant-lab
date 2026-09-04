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
  | Backend | `uv run pytest -n 16 --dist loadfile` | `4915 passed, 1 skipped` |
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
  generating** — regenerate explicitly and check `git diff --stat`.
- **The `full` edition must keep working** at every commit: `pnpm -C frontend build`
  succeeds and `tests/architecture/full_edition_routes.txt` still matches (105
  routes). Release 1 is an addition, not a replacement. **One exception is
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
`CallRow.tsx:85-88` and `CallDetailBody.tsx:54-58`), recorded as ESCALATE **E1**.
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
is now **54–58**. `CallRow.tsx`'s `MatchBadge` render is at **:84**, not `:53`.
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

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/db/models.py` | Add `CallDecision`, delete `UserCallDismissal` (modify) |
| `alembic/versions/0070_call_decisions.py` | Create, backfill, drop (create) |
| `src/call_manager/web/api/calls.py` | Re-point hide; add save; grow `CallOut` (modify) |
| `src/call_manager/web/dashboard.py` | Filter per (user, profile) on `state` (modify) |
| `src/call_manager/digest/assemble.py` | Per-profile exclusion sets (modify) |
| `src/call_manager/ranking/golden_set.py` | Read `profile_id`; filter `state='hidden'` (modify) |
| `tests/migrations/test_0070_call_decisions.py` | The backfill-set gate (create) |
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
- Create: `alembic/versions/0070_call_decisions.py` — `down_revision = "0069"` (head measured: `0069_collab_doc_epoch.py`, 69 migrations)
- Create: `tests/migrations/test_0070_call_decisions.py`
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

    Replaces user_call_dismissals (migration 0070). The old key was
    (user_id, call_id): a dismissal was GLOBAL, so a researcher matching against
    two profiles who dismissed a call for one silently dismissed it for both.
    A decision is per profile because that is the entire point of the table.

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
    # Who pressed the key. Differs from user_id only on a shared profile, where
    # a colleague may triage on the group's behalf. NOT ON DELETE CASCADE: the
    # decision survives the deciding account being removed.
    decided_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
```

Then **delete** `class UserCallDismissal` (`db/models.py:442-454`). Deleting it is
what makes the four consumer edits in Task 2 fail loudly at import time instead of
silently reading a dropped table.

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

The `ON CONFLICT` is load-bearing: a user whose `default_profile_id` is also in
`subscriptions` matches the `OR` twice, and without it the migration aborts on the
unique index.

**Report the loss, do not hide it.** Before the drop, count and log the dismissals
that fanned out to nothing (see the open question above):

```python
orphans = conn.execute(sa.text("""
    SELECT count(*) FROM user_call_dismissals d
    WHERE NOT EXISTS (SELECT 1 FROM call_decisions c
                      WHERE c.user_id = d.user_id AND c.call_id = d.call_id)
""")).scalar_one()
if orphans:
    print(f"0070: {orphans} dismissal(s) had no active subscribed or default "
          f"profile and were NOT migrated")
```

Then `op.drop_table("user_call_dismissals")`.

`downgrade()` recreates `user_call_dismissals` and collapses `state='hidden'` rows
back to distinct `(user_id, call_id)` pairs. It **cannot** restore the profile
dimension — say so in the docstring rather than pretending it round-trips.

- [ ] **Step 3: The backfill gate — this is the task's real test**

`tests/migrations/test_0070_call_decisions.py`. Name what trivially passes first:
**a fixture with one user and one profile passes every wrong version of this
query.** So the fixture must make each wrong reading produce a *different* count.

Seed, for one user, four profiles and one dismissal:

| profile | active? | subscribed? | default? | must appear? |
| --- | --- | --- | --- | --- |
| P1 | yes | yes | no | **yes** |
| P2 | **no** | yes | no | no — inactive |
| P3 | yes | no | **yes** | **yes** |
| P4 | yes | no | no | no — another group's |

Expected: **exactly 2 rows**, on P1 and P3, both `state='hidden'`, `decided_at`
equal to `dismissed_at`, `decided_by = user_id`.

Add a second user, subscribed to P4 only, with no dismissal — proving the migration
does not fan one user's hide onto another's profile. And a third user with a
dismissal and **no** profiles at all, asserting the orphan count is 1 and no row
is written.

Also assert `user_call_dismissals` is **gone** (`inspector.has_table` is False) —
"the new table has rows" is true whether or not the old one was dropped.

- [ ] **Step 4: Mutation-test the gate (all four must go red)**

Run each inside one shell invocation, restoring with `cp` from a saved copy:

| # | Mutation in `0070`'s SQL | Must redden because |
| --- | --- | --- |
| B1 | drop `AND p.is_active = true` | P2 appears → 3 rows |
| B2 | drop `OR p.id = u.default_profile_id` | P3 missing → 1 row |
| B3 | replace the whole `JOIN profiles` predicate with `p.is_active = true` | P4 appears → 3 rows |
| B4 | change `'hidden'` to `'saved'` | state assertion |

A mutation that reddens nothing means the fixture is too small — fix the fixture,
not the expectation.

- [ ] **Step 5: Verify**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/migrations/test_0070_call_decisions.py -q
uv run alembic check
uv run python scripts/gen_architecture.py && git diff --stat docs/ARCHITECTURE.md
ruff check src tests alembic scripts
```

`alembic check` must report no pending autogenerate diff — that is what proves the
model and the migration agree. `docs/ARCHITECTURE.md` **must** change here (a new
table with a unique index and a CHECK); an unchanged file means the generator did
not run.

**Test delta from `4915 passed, 1 skipped`:** `+N` where N is the count this file
actually collects — measure it, do not predict it. Some existing
`user_call_dismissals` tests will move or be deleted in Task 2; count then.

---

### Task 2: Re-point every consumer at `call_decisions`

Spec §4.1. Five files read the old table (D-B4). All five change in this task, and
none may keep working by accident: Task 1 deleted the model, so a missed site is an
`ImportError`, not a wrong answer.

**Files:**
- Modify: `src/call_manager/web/api/calls.py` (hide / un-hide / hidden list)
- Modify: `src/call_manager/web/dashboard.py` (the list filter)
- Modify: `src/call_manager/digest/assemble.py` (the exclusion set)
- Modify: `src/call_manager/ranking/golden_set.py` (`candidates_dismissed_high_score`)
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

```python
# Only 'hidden' decisions suppress a call. A SAVED call still belongs in the
# main list — dropping the state predicate here would make Save behave as a
# second Hide, which is the bug this whole table exists to prevent.
decided_hidden_subq = (
    select(CallDecision.call_id)
    .where(
        CallDecision.user_id == user_id,
        CallDecision.profile_id == profile_id,
        CallDecision.state == "hidden",
    )
    .scalar_subquery()
)
```

**When `profile_id` is `None`** (no profile selected) there is nothing to scope to.
Suppress a call only if it is hidden on **every** profile the user has decided on —
i.e. fall back to the old global behaviour by dropping the `profile_id` predicate.
State this in a comment; it is the one place the answer is a judgement call rather
than a derivation.

- [ ] **Step 2: `digest/assemble.py` — one exclusion set per profile, not one per user**

`dismissed_ids` at `:101-107` is currently a single set for the whole user, applied
across every profile's candidate query. It becomes a `dict[int, set[int]]` keyed by
`profile_id`, and each profile's candidate query subtracts its own set.

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
3. **A saved call is not hidden:** save on P1, assert it is still in P1's list.
4. **Golden set ignores saved:** save a call ranked ≥ 60, assert it is not returned
   by `candidates_dismissed_high_score`.
5. **No consumer remains:** `grep -rn "UserCallDismissal\|user_call_dismissals" src/`
   returns nothing. Note `grep` exits 1 on no match, so write it as
   `! grep -rq ... src/` or the check inverts.

- [ ] **Step 6: Mutation-test each gate**

| # | Mutation | Reddens |
| --- | --- | --- |
| B5 | drop `CallDecision.profile_id == profile_id` in `dashboard.py` | gate 1 |
| B6 | drop `CallDecision.state == "hidden"` in `dashboard.py` | gate 3 |
| B7 | revert `assemble.py` to a single user-wide set | gate 2 |
| B8 | drop the `state` filter in `golden_set.py` | gate 4 |
| B9 | `ON CONFLICT DO NOTHING` instead of `DO UPDATE` in `hide_call` | a "hide a saved call" test |

B9 needs a test that does not exist yet — write it, or the mutation reddens nothing
and the row is a claim.

- [ ] **Step 7: Verify**

```bash
uv run pytest -n 16 --dist loadfile
! grep -rq "UserCallDismissal\|user_call_dismissals" src/ && echo "no consumers left"
ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py && git diff --stat docs/ARCHITECTURE.md
```

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
| `POST` | `/api/calls/{call_id}/save` | `profile_id` | `CallDecisionOut{state}` |
| `DELETE` | `/api/calls/{call_id}/save` | `profile_id` | `CallDecisionOut{state: null}` |
| `GET` | `/api/calls/saved` | `profile_id` | `HiddenListOut`-shaped |
| `GET` | `/api/calls/decisions/counts` | `profile_id` | `{inbox, saved, hidden}` |

`POST .../save` upserts with `DO UPDATE SET state='saved'`, mirroring Task 2's
hide, so `s` on a hidden call flips it. `DELETE` removes the row — back to
undecided, which is what undo means when undecided is the absence of a row.

**Route-order hazard, measured:** `/calls/hidden` is declared *after*
`/calls/{call_id}/hide` in the existing file and works only because `call_id: int`
fails to parse `"hidden"` and FastAPI falls through. `/calls/saved` and
`/calls/decisions/counts` inherit that fragility. **Declare all three literal
paths above `/calls/{call_id}`** and add a test that `GET /api/calls/saved`
returns 200, not 422 — the failure mode is a 422 that looks like a validation bug.

- [ ] **Step 2: The counts endpoint is where inbox-zero comes from**

`inbox` is a `NOT EXISTS` count over the same filters the list uses. It must be
computed from the **same** predicate as the list, not a second hand-written one, or
the rail will say "3" over an empty feed. Factor the predicate into one helper in
`dashboard.py` and call it from both. *That shared helper is the gate's target.*

- [ ] **Step 3: Gates**

1. `save` then `GET /calls/saved` returns it; `GET /calls/hidden` does not.
2. `hide` a saved call → it leaves saved, enters hidden, and there is exactly **one**
   row (`SELECT count(*)` = 1, not 2).
3. `DELETE .../save` on an undecided call is a 200, not a 404 (idempotent undo).
4. Counts agree with the lists: `inbox + saved + hidden == total matching calls`,
   asserted on a fixture with at least one of each. *What trivially passes:* an
   empty fixture — `0 + 0 + 0 == 0`. The fixture must be non-empty and asymmetric
   (e.g. 4 inbox / 2 saved / 1 hidden), or this gate is vacuous.
5. `GET /api/calls/saved` → 200 (the route-order gate).
6. Another user's decision is invisible: seed a second user's save on the same
   call/profile, assert it is not in the first user's saved list or counts.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B10 | `DO UPDATE` → `DO NOTHING` in `save_call` | gate 2 |
| B11 | move `/calls/saved` below `/calls/{call_id}` | gate 5 (expect 422) |
| B12 | counts helper drops the `state` predicate | gate 4 |
| B13 | drop `user_id` from the saved-list query | gate 6 |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_call_decisions_api.py -q
pnpm -C frontend gen:api
git diff --stat frontend/src/api/schema.gen.ts
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
`summary`, `keywords`, plus `criteria: list[CallListCriterionOut]` and
`applicant_note_count: int`.

**Safe on the raiseload contract, measured:** `dashboard.py:290-292` defers
`embedding`, `full_text` and `raw` with `raiseload=True` — and **only** those.
Every field added here is a plain column, so none of them raises. This is worth
stating because a `raiseload` violation surfaces as a 500 in production and as a
green test suite anywhere the fixture happens not to touch the row.

Payload does grow: `summary` and `full_text` share a trigram index and `summary`
can be long. If the list response becomes a problem, truncate `summary`
server-side — **do not** re-defer it, which would reintroduce an N+1.

- [ ] **Step 2: One batched criteria query — the part that can go quietly wrong**

`view.calls` yields `(call, ranking)` pairs, so `applicant_note_count` is
`len(ranking.applicant_notes or [])` with **zero** extra queries. Criteria need
exactly **one** more, after the page is known:

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
seed 1 call,  request the list -> record N1
seed 10 calls, request the list -> record N10
assert N10 == N1        # constant, not "small"
```

*What trivially passes:* `assert N10 < 60`. A ceiling passes a per-row loop on a
small page. **Equality against the 1-call run is the gate**; a ceiling is the
"weaker property than the one claimed" hollow-gate shape.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B14 | replace the batched query with a per-call `select(...)` inside the comprehension | the query-count gate (N10 = N1 + 9) |
| B15 | drop `CallRankingCriterion.profile_id == profile_id` | a cross-profile leak test — **write it**: two profiles ranked the same call differently, assert the list shows only the selected profile's chips |
| B16 | `applicant_note_count` from `len(ranking.applicant_notes)` without the `or []` | a `NULL applicant_notes` fixture (the local Gemma path always leaves it NULL — this is the common case, not the edge case) |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/web/test_calls_list_query_count.py -q
uv run pytest -n 16 --dist loadfile
pnpm -C frontend gen:api
git diff --stat frontend/src/api/schema.gen.ts   # must be non-empty
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
`web/filters.py::_CRITERION_LABELS` and the `criterion=` literals in
`ranking/worker.py` via `ast`, and asserts the frontend's `TONE` map plus its
criterion list cover every one. **This is the gate that stops D-B1 recurring:**
when someone adds an eighth criterion in `worker.py`, this goes red instead of the
UI silently rendering a blank chip.

*What trivially passes:* asserting the frontend is a **subset** of the backend.
That is the first hollow-gate shape, and it passes when the frontend knows nothing.
Assert the frontend **covers** the backend — every backend criterion appears in the
frontend list.

- [ ] **Step 3: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B17 | delete `"embedding"` from the frontend criterion list | the coverage gate |
| B18 | add `criterion="foo"` to `worker.py` | the coverage gate (proves it reads the real source, not a copy) |
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
4. `s` typed inside a focused `<input>` triggers **no** mutation.
5. Zero items renders the inbox-zero copy.
6. `?view=saved` renders the saved list and marks the rail item current.
7. Counts render from the API, not from `items.length`.

- [ ] **Step 6: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B23 | remove the input-focus guard from the key handler | gate 4 |
| B24 | `j` moves by 0 | gate 3 |
| B25 | render `items.length` in the rail instead of the counts response | gate 7 |
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
5. `POST /api/calls/1/hide` with a `profile_id` the user cannot use (inactive, or
   another group's) → 4xx, not a written row. *This is the one that matters:* a
   required parameter that is never validated is a required parameter in name only.

- [ ] **Step 4: Mutation-test**

| # | Mutation | Reddens |
| --- | --- | --- |
| B28 | render the feed **and** the onboarding screen | gate 1 |
| B29 | reuse the inbox-zero component for the no-profile branch | gate 2 |
| B30 | `profile_id: int \| None = Query(default=None)` on `hide` | gate 4 |
| B31 | drop the ownership check on `profile_id` | gate 5 |

B31 is the one to write the test for **first**. Gate 4 passes the moment the
parameter is declared required; gate 5 is the only one that proves it means anything.

- [ ] **Step 5: Verify**

```bash
pnpm -C frontend test src/routes/TriageEmptyProfile.test.tsx src/routes/TriageFeed.test.tsx
uv run pytest tests/web/test_call_decisions_api.py -q
uv run pytest tests/test_api_calls_feed_behavior.py -q -k unranked   # 3 passed, UNCHANGED
pnpm -C frontend build && pnpm -C frontend build:match && pnpm -C frontend check:bundle
pnpm -C frontend gen:api && git diff --stat frontend/src/api/schema.gen.ts
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
- [ ] **Step 2: Update the pins.** The scenario-title list is pinned by exact
  string, and the `expect(` floor is a measured number. **Measure both after
  writing the scenarios** — the Plan A run wrote `>= 30` commented "measured at 40"
  when the real count was 22, and the test failed on its first run. Write the
  number the command prints.
- [ ] **Step 3: Mutation-test.** B32: delete one new scenario — the title pin goes
  red. B33: strip the assertions from one scenario body but keep its title — the
  `expect(` floor goes red. B33 is the one that matters; a title pin alone passes
  over an empty test.
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
- **The pass total will be higher than `4915`.** State the number you measured and
  the per-task deltas that produce it. Do **not** copy a predicted total into this
  section — the Plan A brief's stale `3361` is why this line exists.
- `tests/architecture/full_edition_routes.txt` unchanged: **105 routes**. Plan B
  adds four routes to *both* editions, so this baseline **must be regenerated** and
  the diff must be exactly those four. Any fifth line is a finding.
- `docs/superpowers/specs/` byte-identical to `f0aaed0c`:
  `git diff --stat f0aaed0c -- docs/superpowers/specs/` is empty.
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
- **No swipe gesture.** §5's responsive strategy keeps one DOM; the phone gesture
  is deferred with the rest of the responsive work.
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
