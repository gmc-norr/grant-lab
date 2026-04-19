# Ranking v2 — Plan E: Simplification

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Depends on:** Plans A + B + C + D of v2 (all merged to master).
**Goal:** Remove ~1,300 LOC of code that no longer earns its keep — v1 ranking machinery, setwise tie-break, G-Eval logprob walker, embedding-threshold caching dance, freestanding quoted-evidence verification. After this plan the codebase is approximately the right size for what it does.

**Why now:** Nothing is in production yet. Plan D made v2 strictly stronger than v1 on every axis the user cares about. The dual-scheme machinery and post-LLM refinement layer were defensible when each was added; with v2 mature, they're now overhead.

**Architecture after this plan:**
- One ranking pipeline: `evaluate_gates → rank_pair (was rank_pair_v2) → aggregate → CallRanking row + criterion rows`
- One displayed score: `aggregate_score` (no setwise offset, no `display_score` indirection)
- One source of truth for the embedding threshold (Pydantic Settings, threaded through)
- LLM rating handled as integer × 20 (no logprob walker, no `topic_expected` field)
- One queue, no scheme discriminator anywhere

**Tech Stack:** Same as Plans A–D. Python 3.12 · FastAPI + Jinja2 + HTMX · PostgreSQL 16 + SQLAlchemy 2.x + Alembic · APScheduler · pgvector · `pytest` + `testcontainers[postgres]`

---

## Design decisions (fix-now; push back if you disagree)

- **Hard delete v1, no `if` flag.** Nothing is in production — there are no v1 CallRanking rows that matter, no operators with v1 dashboards. A `--keep-v1-for-compatibility` flag would just be the same dead code with extra branches.
- **Drop the `scheme` column from `call_rankings`, `ranking_jobs`, `profiles`, AND `call_ranking_criteria`** (Codex H1: the criteria table also has scheme in its PK; missing this would leave one scheme-bearing table behind and the worker still writing `scheme="v2"`). Update the PK / unique-index accordingly. The data migration sets all v2 rows in place; v1 rows get deleted (there are none in dev, but the migration handles it safely).
- **Drop `setwise_offset` column** + the entire `display_score` hybrid_property + SQL CASE-around-NULL expression. Read paths use bare `aggregate_score`.
- **Replace G-Eval logprob walker with `topic_argmax × 20`.** Drop the `topic_expected` field, drop the `logprobs=True, top_logprobs=6` kwargs from the chat call, drop `logprob_fallback`. Save ~100 LOC of fragile token-walking that refines an integer rating in a way that's invisible in the UI. **Also drop `CallRankingCriterion.rating_expected` column** (it's the persisted form of `topic_expected`, written by the worker and rendered on call_detail.html line ~39); migration handles it.
- **Inline the embedding threshold from Settings.** Drop `_get_threshold()` lru_cache; thread `settings.embedding_pregate_threshold` from the worker → `rank_pair` directly. Single source of truth.
- **Inline quoted-evidence verification into the parser.** The standalone helper `_verify_quoted_evidence` becomes a 6-line filter inside `parse_v2_response`. No behavior change.
- **Keep `must_include` plumbing** — the runtime ranking-bypass code stays. Operator hasn't asked to remove it; it's small enough.
- **Keep `ProfileKind` enum** — it's used in form validation and prompt context; removing it would touch more files than it saves.
- **Rename `rank_pair_v2` → `rank_pair`** and `RankResultV2 → RankResult` after v1 is deleted. Same for `_v2_summary`, `prompt_v2.py` constants, etc. v2 is just the ranking now.
- **Schema migration is a single new revision (0008).** No "intermediate state" — drop everything in one go.

---

## File structure changes

```
DELETED:
  src/call_monitor/ranking/setwise.py                     (~241 LOC)
  src/call_monitor/web/templates/ranking_compare.html
  tests/test_ranking_setwise.py                           (~425 LOC)
  tests/test_web_admin_compare.py                         (~215 LOC)

NEW:
  alembic/versions/0008_drop_v1_and_setwise.py            (single migration)

RENAMED:
  src/call_monitor/ranking/rank_pair.py                   (rank_pair_v2 → rank_pair)
                                                           (RankResultV2 → RankResult)

MODIFIED (substantive):
  src/call_monitor/db/models.py                           (drop columns, drop hybrid_property)
  src/call_monitor/ranking/aggregate.py                   (drop logprob_expected, drop topic_expected)
  src/call_monitor/ranking/criteria.py                    (drop EMBEDDING_PREGATE_THRESHOLD module const,
                                                            accept threshold as required arg)
  src/call_monitor/ranking/prompt.py                      (drop v1 template; rename v2 → v1 = the only one)
  src/call_monitor/ranking/worker.py                      (drop scheme dispatch; drop _v2_ prefix on
                                                            symbols; drop setwise propagation)
  src/call_monitor/ranking/eval.py                        (drop v1 column from GoldenSetReport,
                                                            single tau computation)
  src/call_monitor/scheduler.py                           (drop setwise:daily job)
  src/call_monitor/web/routes/admin.py                    (drop ranking_compare route + per-scheme metrics)
  src/call_monitor/web/routes/profiles.py                 (drop ranking_scheme form handling)
  src/call_monitor/web/templates/admin.html               (drop scheme columns + compare link)
  src/call_monitor/web/templates/profile_edit.html        (drop scheme radio)
  src/call_monitor/web/templates/calls_index.html         (display_score → aggregate_score)
  src/call_monitor/web/templates/call_detail.html         (display_score → aggregate_score)
  src/call_monitor/digest/templates/digest.html.jinja
  src/call_monitor/digest/templates/digest.txt.jinja
  src/call_monitor/digest/assemble.py                     (drop scheme branching; drop display_score)
  src/call_monitor/ranking/backfill.py                    (drop scheme parameter; always v2-style)

DELETED tests:
  tests/test_ranking_setwise.py
  tests/test_web_admin_compare.py
  tests/test_web_admin_v2_metrics.py                      (per-scheme metrics gone)

MODIFIED tests (extensive — fixtures/seeds drop scheme="v2", tests drop scheme assertions):
  most tests/test_ranking_*.py
  most tests/test_web_*.py
  tests/test_alembic_v2_round_trip.py                     (assert v1 columns dropped)
```

---

## Task 1: Migration + drop v1 ranking + drop setwise schema

**Files:**
- Create: `alembic/versions/0008_drop_v1_and_setwise.py`
- Modify: `src/call_monitor/db/models.py` (drop scheme/setwise/display_score)
- Modify: `tests/test_alembic_v2_round_trip.py` (assert columns gone)

This task is purely schema — no code outside `models.py` should depend on `ranking_scheme`, `scheme`, `setwise_offset`, or `display_score` after this migration runs. Subsequent tasks update the application code.

- [ ] **Step 1: Inventory current scheme usage**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "ranking_scheme\|RankingJob.scheme\|CallRanking.scheme\|setwise_offset\|display_score" src/ tests/ | wc -l
```

Expected output: hundreds of hits. Don't try to fix them yet — just confirm the surface area.

- [ ] **Step 2: Write migration `alembic/versions/0008_drop_v1_and_setwise.py`**

```python
"""Drop v1 ranking + setwise machinery

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-18

After Plan D, v2 is strictly stronger than v1 on every dimension. Plan E
removes the dual-scheme machinery (no production v1 data to preserve),
the setwise tie-break (point in time when borderline noise was worth
refining; the embedding pre-filter from Plan D made it superfluous),
and the persisted G-Eval logprob expected value (refines an integer
rating invisibly in the UI; not worth the parser fragility).

Schema deltas:
- profiles: drop ranking_scheme
- ranking_jobs: drop scheme; rebuild uq_ranking_jobs_active_pair without it
- call_rankings: drop scheme from PK (pk_call_rankings becomes
    (call_id, profile_id)); rebuild ix_call_rankings_profile_scheme_score as
    ix_call_rankings_profile_score; drop setwise_offset
- call_ranking_criteria: drop scheme from PK; drop rating_expected
- delete any rows where scheme='v1' (defensive — should be 0 in dev)

All constraint/index names checked against alembic/versions/0006_ranking_v2_schema.py.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -------- ranking_jobs --------
    # Drop existing index that includes scheme; delete v1 rows; drop column;
    # rebuild index without scheme.
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.execute("DELETE FROM ranking_jobs WHERE scheme = 'v1'")
    op.drop_column("ranking_jobs", "scheme")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs", ["call_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # -------- call_rankings --------
    # Drop the score-with-scheme index; delete v1 rows; drop scheme from PK;
    # drop scheme + setwise_offset; rebuild the score index without scheme.
    op.drop_index("ix_call_rankings_profile_scheme_score", table_name="call_rankings")
    op.execute("DELETE FROM call_rankings WHERE scheme = 'v1'")
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings",
                          ["call_id", "profile_id"])
    op.drop_column("call_rankings", "scheme")
    op.drop_column("call_rankings", "setwise_offset")
    op.create_index(
        "ix_call_rankings_profile_score",
        "call_rankings",
        ["profile_id", sa.text("aggregate_score DESC")],
    )

    # -------- call_ranking_criteria (Codex H1 — was missed) --------
    # The criteria table has scheme in its PK and a rating_expected column
    # that's the persisted form of the logprob-walker output. Drop both.
    op.execute("DELETE FROM call_ranking_criteria WHERE scheme = 'v1'")
    # PK was (call_id, profile_id, scheme, name) — verify and reduce to
    # (call_id, profile_id, name). PK constraint name comes from existing
    # migration; introspect or read 0006_ranking_v2_schema.py to find it.
    op.drop_constraint(
        "pk_call_ranking_criteria", "call_ranking_criteria", type_="primary"
    )
    op.create_primary_key(
        "pk_call_ranking_criteria", "call_ranking_criteria",
        ["call_id", "profile_id", "criterion"],
    )
    op.drop_column("call_ranking_criteria", "scheme")
    op.drop_column("call_ranking_criteria", "rating_expected")

    # -------- profiles --------
    op.drop_column("profiles", "ranking_scheme")


def downgrade() -> None:
    # Best-effort downgrade — restores columns but cannot recreate v1 data.
    op.add_column(
        "profiles",
        sa.Column("ranking_scheme", sa.String(8), nullable=False, server_default="v2"),
    )
    op.alter_column("profiles", "ranking_scheme", server_default=None)

    # call_ranking_criteria
    op.add_column(
        "call_ranking_criteria",
        sa.Column("rating_expected", sa.Float(), nullable=True),
    )
    op.add_column(
        "call_ranking_criteria",
        sa.Column("scheme", sa.String(8), nullable=False, server_default="v2"),
    )
    op.alter_column("call_ranking_criteria", "scheme", server_default=None)
    op.drop_constraint(
        "pk_call_ranking_criteria", "call_ranking_criteria", type_="primary"
    )
    op.create_primary_key(
        "pk_call_ranking_criteria", "call_ranking_criteria",
        ["call_id", "profile_id", "scheme", "criterion"],
    )

    # call_rankings
    op.drop_index("ix_call_rankings_profile_score", table_name="call_rankings")
    op.add_column("call_rankings", sa.Column("setwise_offset", sa.Integer(),
                                              nullable=False, server_default="0"))
    op.alter_column("call_rankings", "setwise_offset", server_default=None)
    op.add_column("call_rankings", sa.Column("scheme", sa.String(8), nullable=False,
                                              server_default="v2"))
    op.alter_column("call_rankings", "scheme", server_default=None)
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings",
                          ["call_id", "profile_id", "scheme"])
    op.create_index(
        "ix_call_rankings_profile_scheme_score",
        "call_rankings",
        ["profile_id", "scheme", sa.text("aggregate_score DESC")],
    )

    # ranking_jobs
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.add_column("ranking_jobs", sa.Column("scheme", sa.String(8), nullable=False,
                                             server_default="v2"))
    op.alter_column("ranking_jobs", "scheme", server_default=None)
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs", ["call_id", "profile_id", "scheme"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
```

**Constraint names verified against `alembic/versions/0006_ranking_v2_schema.py`:**
- PK is `pk_call_rankings` (not `call_rankings_pkey`)
- Active-job index is `uq_ranking_jobs_active_pair` (not `ux_*`)
- Score index is `ix_call_rankings_profile_scheme_score` → reverts to `ix_call_rankings_profile_score`
- Criteria PK name should be verified against `0006_ranking_v2_schema.py` before running — likely `pk_call_ranking_criteria`. If different, fix the constraint name in this migration.

- [ ] **Step 3: Update `src/call_monitor/db/models.py`**

Remove:
- `Profile.ranking_scheme` column
- `RankingJob.scheme` column
- `CallRanking.scheme` from PK (the composite PK becomes `(call_id, profile_id)`)
- `CallRanking.setwise_offset` column
- The `display_score` `@hybrid_property` AND its `@display_score.expression` SQL form

Read the file first; the column locations are clearly identifiable. Update the partial unique index on RankingJob to drop scheme.

- [ ] **Step 4: Test migration**

**Use `migrated_engine` (not bare `pg_url`)** so the upgrade-to-head fixture chain runs first. Bare `pg_url` is just the container URL; without `migrated_engine` (or `pg_session_factory`) you'd be inspecting an unmigrated DB and the test would only pass by accident if other tests ordered before it left the DB at head (Codex M2 finding).

In `tests/test_alembic_v2_round_trip.py`, append:

```python
def test_0008_drops_v1_and_setwise_machinery(migrated_engine):
    """After 0008 (which is now head), dual-scheme + setwise columns are gone."""
    from sqlalchemy import inspect

    insp = inspect(migrated_engine)

    cols_calls = {c["name"] for c in insp.get_columns("call_rankings")}
    cols_profiles = {c["name"] for c in insp.get_columns("profiles")}
    cols_jobs = {c["name"] for c in insp.get_columns("ranking_jobs")}
    cols_criteria = {c["name"] for c in insp.get_columns("call_ranking_criteria")}

    assert "scheme" not in cols_calls
    assert "scheme" not in cols_jobs
    assert "scheme" not in cols_criteria        # Codex H1
    assert "ranking_scheme" not in cols_profiles
    assert "setwise_offset" not in cols_calls
    assert "rating_expected" not in cols_criteria  # G-Eval column gone

    # PKs reduced to (call_id, profile_id) and (call_id, profile_id, name) respectively
    pk_rankings = insp.get_pk_constraint("call_rankings")["constrained_columns"]
    assert pk_rankings == ["call_id", "profile_id"]

    pk_criteria = insp.get_pk_constraint("call_ranking_criteria")["constrained_columns"]
    assert pk_criteria == ["call_id", "profile_id", "criterion"]

    # Score index renamed back to its pre-Plan-A form
    idx_names = {ix["name"] for ix in insp.get_indexes("call_rankings")}
    assert "ix_call_rankings_profile_score" in idx_names
    assert "ix_call_rankings_profile_scheme_score" not in idx_names
```

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_alembic_v2_round_trip.py -v
# Many other tests will break — that's Task 2+. Don't try to make them green here.
git add alembic/versions/0008_drop_v1_and_setwise.py src/call_monitor/db/models.py tests/test_alembic_v2_round_trip.py
git commit -m "refactor: drop v1 + setwise schema columns (migration 0008)"
```

---

## Task 2: Drop v1 ranking code + rename v2 → canonical

**Files:**
- Delete: `src/call_monitor/web/templates/ranking_compare.html`
- Modify: `src/call_monitor/ranking/prompt.py` (drop v1 template + helpers)
- Modify: `src/call_monitor/ranking/rank_pair.py` (delete `rank_pair`; rename `rank_pair_v2 → rank_pair`, `RankResultV2 → RankResult`)
- Modify: `src/call_monitor/ranking/worker.py` (drop scheme dispatch; rename `_v2_summary → _rank_summary`, `_write_v2_criteria_rows → _write_criteria_rows`)
- Modify: `src/call_monitor/ranking/queue.py` (drop scheme parameter from `enqueue_for_pair` and `claim_pending_jobs`)
- Modify: `src/call_monitor/ranking/backfill.py` (drop `profile.ranking_scheme` reading; always enqueue)
- Modify: `src/call_monitor/ingestion/orchestrator.py` (drop `pscheme` from active-profiles loop)
- Delete: `tests/test_web_admin_compare.py`
- Delete: `tests/test_web_admin_v2_metrics.py`
- Delete: any other test files exclusively about scheme dispatch
- Modify: most other test files in the ranking + ingestion + web suites

This task is the bulk of the refactor. Approach: delete first, then fix what fails to compile, then make tests pass.

- [ ] **Step 1: Delete dead code wholesale**

```bash
rm src/call_monitor/web/templates/ranking_compare.html
rm tests/test_web_admin_compare.py
rm tests/test_web_admin_v2_metrics.py
```

- [ ] **Step 2: Refactor `prompt.py`**

Read the file. Identify what's v1 (the original LLM-only prompt builder + parser) and what's v2 (the criterion-rubric prompt builder + parser).

Delete:
- `PROMPT_VERSION_V1`, `build_messages_v1`, `parse_v1_response`, `ParsedV1`, `ParseV1Error`
- The v1 system prompt template + helpers
- `CALL_TEXT_LIMIT_V1` if separate from v2

Rename (drop the `_v2`/`V2` suffix):
- `PROMPT_VERSION_V2 → PROMPT_VERSION` (keep the value `"v2.0"` so existing rationales still parse — or bump to `"v3.0"` if you want to mark the simplification; recommend keep `"v2.0"`)
- `build_messages_v2 → build_messages`
- `parse_v2_response → parse_response`
- `ParsedV2 → Parsed`
- `ParseV2Error → ParseError`
- `CALL_TEXT_LIMIT_V2 → CALL_TEXT_LIMIT`

Update all imports across the codebase. Use sed/ripgrep to find references:

```bash
grep -rn 'PROMPT_VERSION_V2\|build_messages_v2\|parse_v2_response\|ParsedV2\|ParseV2Error\|CALL_TEXT_LIMIT_V2\|PROMPT_VERSION_V1\|build_messages_v1\|parse_v1_response\|ParsedV1\|ParseV1Error' src/ tests/
```

- [ ] **Step 3: Refactor `rank_pair.py`**

Delete `rank_pair` (the v1 monolithic version).
Rename `rank_pair_v2 → rank_pair`.
Rename `RankResultV2 → RankResult`.
Update all imports.

```bash
grep -rn 'rank_pair_v2\|RankResultV2' src/ tests/
```

- [ ] **Step 4: Refactor `worker.py` — drop scheme dispatch + criteria scheme + rating_expected**

Today the worker reads `job.scheme` and dispatches to v1 or v2. After the migration, all jobs are uniform. The dispatch becomes a single direct call. Delete:
- The `if job.scheme == 'v1': ...` branch + the v1 path entirely (lines around 76-80, 195-199)
- The unpacking of `scheme` from claimed jobs (line 70, 76)
- Rename `_v2_summary → _rank_summary` (v1 summary disappears)
- Rename `_write_v2_criteria_rows → _write_criteria_rows`

In `_write_criteria_rows` (was `_write_v2_criteria_rows`) — Codex H1:
- Drop the `CallRankingCriterion.scheme == "v2"` filter from the wipe-existing query (line ~301)
- Drop `scheme="v2"` from the gate-row INSERT (~line 323)
- Drop `scheme="v2"` from the LLM-criterion INSERT (~line 344)
- Drop `rating_expected=None` and `rating_expected=r.topic_expected` from both INSERTs (column is gone after Task 4 / migration)

The CallRanking write no longer has a `scheme=...` kwarg either.

- [ ] **Step 5: Refactor `queue.py` and `backfill.py`**

In `queue.py`, drop the `scheme: str` parameter from `enqueue_for_pair`. The function becomes:
```python
def enqueue_for_pair(session, *, call_id: int, profile_id: int) -> None:
    ...
```

In `backfill.py`, drop the `profile.ranking_scheme` read. The bulk INSERT no longer includes a scheme column.

In `ingestion/orchestrator.py`, the active-profiles query no longer needs to select `ranking_scheme`:
```python
active_profile_ids = session.execute(
    select(Profile.id).where(Profile.is_active.is_(True))
).scalars().all()
for cid in touched_call_ids:
    for pid in active_profile_ids:
        enqueue_for_pair(session, call_id=cid, profile_id=pid)
```

- [ ] **Step 6: Refactor `eval.py` — single tau, no v1/v2 split**

Read `src/call_monitor/ranking/eval.py`. The current `GoldenSetReport` has `tau_v1`, `n_v1`, `tau_v2`, `n_v2`. Collapse to:

```python
@dataclass(frozen=True)
class GoldenSetReport:
    n_fixture: int
    n_compared: int
    tau: float | None
```

The CLI / route / template that displays this report needs the corresponding update.

- [ ] **Step 7: Refactor admin routes + templates**

In `src/call_monitor/web/routes/admin.py`:
- Delete the `ranking_compare` route entirely
- Delete `_v2_queue_depth` (just `_queue_depth` now)
- Delete `_per_profile_scheme` (every profile is v2; meaningless)
- Update the admin index template context accordingly

In `src/call_monitor/web/templates/admin.html`:
- Delete the per-scheme queue depth section
- Delete the per-profile scheme + last-rank table
- Delete the link to `/admin/ranking-compare`
- Update the golden-set tau section to render single `tau` field

In `src/call_monitor/web/routes/profiles.py`:
- Delete the `ranking_scheme: str = Form("")` parameter
- Delete the admin-gated `if user.is_admin and requested in {"v1", "v2"}: p.ranking_scheme = requested` block

In `src/call_monitor/web/templates/profile_edit.html`:
- Delete the `{% if user.is_admin %}` radio block

- [ ] **Step 8: Refactor read paths — drop scheme filtering**

In `src/call_monitor/web/routes/calls.py`:
- The calls index query joins `CallRanking` filtered by scheme. Drop the scheme filter (the JOIN now only needs `(call_ranking.profile_id == profile.id)`).
- The call_detail query loads `CallRankingCriterion` rows filtered by `scheme` (line ~158 — Codex H1). Drop that filter too.

In `src/call_monitor/web/templates/calls_index.html`, no template change needed — query already returns the right rows.

In `src/call_monitor/web/templates/call_detail.html`:
- Delete the `{% if ranking.scheme == 'v2' and criteria %}` guard around the per-criterion table — every ranking has criteria now (no scheme dispatch)
- Drop `ranking.scheme` references

In `src/call_monitor/digest/assemble.py`:
- Drop the per-scheme branching (every profile is v2)
- The query joins call_ranking on profile_id only

In `src/call_monitor/digest/templates/digest.html.jinja` and `digest.txt.jinja`:
- Drop any `?scheme=` query-string parameters from links
- Drop scheme conditionals

- [ ] **Step 9: Update test fixtures + tests**

Find every test that:
- Constructs a `CallRanking` with `scheme="v2"` → drop the kwarg
- Constructs a `RankingJob` with `scheme="v2"` → drop the kwarg
- Sets `profile.ranking_scheme = "v2"` → drop that line
- Asserts on `ranking.scheme == "v2"` → drop the assertion (always true → tautology)
- Asserts `ranking.scheme == "v1"` → delete the test (v1 is gone)
- Imports `rank_pair_v2` → import `rank_pair` instead

Use ripgrep to find:
```bash
grep -rn 'scheme="v2"\|scheme="v1"\|ranking_scheme' tests/
```

Delete tests that exclusively exercised:
- v1 LLM ranking
- v1/v2 dispatch in the worker
- The admin ranking-scheme flip + radio
- `/admin/ranking-compare` (test file already deleted)
- per-scheme queue-depth metrics (test file already deleted)
- `Profile.ranking_scheme` (it's gone)

- [ ] **Step 10: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add -A
git commit -m "refactor: drop v1 ranking; rename v2 → canonical (rank_pair, RankResult, build_messages, etc.)"
```

Expected: ~50–100 tests removed (v1 tests + scheme-dispatch tests). Suite count drops significantly but coverage of the actual ranking pipeline is unchanged.

---

## Task 3: Drop setwise pass

**Files:**
- Delete: `src/call_monitor/ranking/setwise.py`
- Delete: `tests/test_ranking_setwise.py`
- Modify: `src/call_monitor/scheduler.py` (drop `setwise:daily` job + the helper)
- Modify: `src/call_monitor/web/templates/calls_index.html` (display_score → aggregate_score — already done in Task 2 if the template was using `display_score` directly; otherwise drop here)
- Modify: `src/call_monitor/web/templates/call_detail.html` (same)
- Modify: `src/call_monitor/digest/assemble.py` (band cutoffs use aggregate_score, not display_score — already done in Task 2 if applicable)
- Modify: `tests/test_scheduler*.py` (remove setwise:daily from expected job sets)

Schema work already done in Task 1 (`setwise_offset` column dropped, `display_score` hybrid_property removed from models).

- [ ] **Step 1: Delete `setwise.py` and its test**

```bash
rm src/call_monitor/ranking/setwise.py
rm tests/test_ranking_setwise.py
```

- [ ] **Step 2: Update `scheduler.py`**

Delete:
- `from call_monitor.ranking.setwise import run_setwise_for_profile`
- The `_setwise_tick` function
- The `sched.add_job(_setwise_tick, ...)` call

In `tests/test_scheduler*.py`, find any expectation that `"setwise:daily"` is registered and remove it. Update the "expected job set" assertion if it's a strict superset check (it should already use issubset per Plan C's review).

- [ ] **Step 3: Update read paths to use aggregate_score**

If Task 2 already replaced `display_score` references with `aggregate_score`, this step is a no-op. Otherwise:

```bash
grep -rn 'display_score' src/ tests/
```

Replace each occurrence:
- ORM access: `ranking.display_score` → `ranking.aggregate_score`
- SQL access: `CallRanking.display_score` → `CallRanking.aggregate_score`
- Template: `{{ ranking.display_score }}` → `{{ ranking.aggregate_score }}`

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add -A
git commit -m "refactor: drop setwise pass (display_score = aggregate_score)"
```

---

## Task 4: Replace G-Eval logprob walker with `argmax × 20`

**Files:**
- Modify: `src/call_monitor/ranking/aggregate.py` (delete `logprob_expected`; simplify `aggregate`)
- Modify: `src/call_monitor/ranking/rank_pair.py` (drop `logprobs=True, top_logprobs=6` from chat call; drop `topic_expected`/`logprob_fallback` from `RankResult`)
- Modify: `src/call_monitor/ranking/prompt.py` (drop `topic_expected` field from `Parsed`)
- Modify: existing tests that assert on `topic_expected` or `logprob_fallback`
- Migration to drop `topic_expected` column from `call_rankings` (if it's a separate column)

Verify first what's stored where. The `topic_expected` may live on `CallRanking` directly OR on the criteria row OR be transient (computed inside `aggregate`).

- [ ] **Step 1: Inventory `topic_expected` usage**

```bash
grep -rn 'topic_expected\|logprob_expected\|logprob_fallback' src/ tests/ alembic/
```

- [ ] **Step 2: Delete `logprob_expected` from `aggregate.py`**

Read the file. Delete the function entirely. In `aggregate()`:
- Replace `int(round(llm.topic_expected * 20))` with `llm.topic_argmax * 20`
- The `eligibility == "unclear"` cap: `min(50, llm.topic_argmax * 20)` (was `min(50, base)` where base was the float-derived value; equivalent at integer granularity)
- The exclusion-fail floor: `min(10, int(llm.topic_expected * 2))` becomes `min(10, llm.topic_argmax * 2)`

Drop the `topic_expected` field from `LLMResult`:
```python
@dataclass(frozen=True)
class LLMResult:
    topic_argmax: int        # was: topic_expected: float, topic_argmax: int
    eligibility: str
    exclusions: str
```

- [ ] **Step 3: Update `rank_pair.py`**

In the chat call, drop `logprobs=True, top_logprobs=6`:
```python
envelope = client.chat(messages=messages, response_format={"type": "json_object"})
```

In `RankResult` dataclass, drop:
- `topic_expected: float | None`
- `logprob_fallback: bool`

Drop the post-parse code that computed `topic_expected = logprob_expected(...) or float(parsed.topic_argmax)` and the `logprob_fallback = topic_expected is None` flag.

- [ ] **Step 4: Update `prompt.py`**

In `Parsed` dataclass, drop `topic_expected` (was carried for the logprob-walker path). Keep `topic_argmax`.

- [ ] **Step 5: Persisted form is `CallRankingCriterion.rating_expected`** (Codex M1)

The transient `topic_expected` is persisted on the `topic_fit` criterion row as `rating_expected`. The migration in Task 1 already drops the column. This task just needs to:

1. **Confirm Task 2's worker refactor** dropped both `rating_expected=None` and `rating_expected=r.topic_expected` from `_write_criteria_rows` INSERTs (already covered by Task 2 Step 4).

2. **Update `src/call_monitor/web/templates/call_detail.html`** (~line 39) — find the rendering of `criterion.rating_expected` (probably inside the `topic_fit` row) and remove it. The template should render only `criterion.rating` (the integer 0-5 argmax) without the parenthesized expected-value annotation. Read the current template + its test to understand the exact change.

3. **Update tests** that asserted on `rating_expected` (templates or DB rows) — drop those assertions.

```bash
grep -rn 'rating_expected' src/ tests/
```

After this step + Task 1 migration, `rating_expected` is gone from schema, code, templates, and tests.

- [ ] **Step 6: Update tests**

Find tests that:
- Assert on `topic_expected` (drop assertions; refactor to assert on `topic_argmax`)
- Set `logprobs=True` in chat mock (drop kwarg)
- Construct `LLMResult(topic_expected=X, topic_argmax=Y, ...)` (use only `topic_argmax`)
- Construct `RankResult(topic_expected=X, ..., logprob_fallback=False, ...)` (drop both fields)

```bash
grep -rn 'topic_expected\|logprob_fallback' tests/
```

Tests that exclusively exercise the logprob walker (`test_logprob_expected_*` etc.) should be deleted entirely.

- [ ] **Step 7: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add -A
git commit -m "refactor: drop G-Eval logprob walker (use argmax * 20)"
```

---

## Task 5: Inline embedding threshold (drop lru_cache)

**Files:**
- Modify: `src/call_monitor/ranking/criteria.py` (drop `EMBEDDING_PREGATE_THRESHOLD` module constant; require threshold as arg)
- Modify: `src/call_monitor/ranking/rank_pair.py` (drop `_get_threshold()` lru_cache + fallback)
- Modify: `src/call_monitor/ranking/worker.py` (read `settings.embedding_pregate_threshold` once per tick, pass to rank_pair)
- Modify: tests

Right now there are TWO sources of truth: `criteria.EMBEDDING_PREGATE_THRESHOLD = 0.30` (module constant, used by `_embedding`) AND `Settings.embedding_pregate_threshold = 0.30` (Pydantic field, read via lru_cache). Pick one: Settings.

- [ ] **Step 1: Update `criteria.py`**

Drop the module-level `EMBEDDING_PREGATE_THRESHOLD` constant and the `from call_monitor.config import settings` import (or whatever Task 4's implementer used).

`_embedding` becomes:
```python
def _embedding(sim: float | None, *, threshold: float) -> GateResult:
    if sim is None:
        return GateResult(GateStatus.UNKNOWN)
    if sim < threshold:
        return GateResult(GateStatus.FAIL, reason="topic_dissimilar")
    return GateResult(GateStatus.PASS)
```

`evaluate_gates` makes `embedding_threshold` a required keyword arg:
```python
def evaluate_gates(
    profile, call, *,
    now: datetime,
    embed_similarity: float | None = None,
    embedding_threshold: float,
) -> Gates:
    ...
```

(Keyword-only, no default. Caller MUST pass it.)

- [ ] **Step 2: Update `rank_pair.py`**

Drop:
- `from functools import lru_cache`
- The `_get_threshold()` function and the `@lru_cache(maxsize=1)` decorator
- The `try/except Exception → fallback` pattern
- The `EMBEDDING_PREGATE_THRESHOLD` module-level reference

Make `rank_pair` accept `embedding_threshold` as a required kwarg:
```python
def rank_pair(*, client, profile, call, now, embedding_threshold: float) -> RankResult:
    embed_sim = _safe_cosine(profile, call)
    must_include_match = _call_matches_must_include(profile, call)
    gates = evaluate_gates(
        profile, call, now=now,
        embed_similarity=embed_sim,
        embedding_threshold=embedding_threshold,
    )
    # ... rest unchanged
```

- [ ] **Step 3: Update `worker.py`**

The worker constructs `Settings()` once per tick (or reads from `app.state.settings` if accessible). Pass `settings.embedding_pregate_threshold` into each `rank_pair` call:

```python
def drain_queue(*, session_factory, ollama_client, settings, ...):
    threshold = settings.embedding_pregate_threshold
    # ... in the per-job loop ...
    result = rank_pair(
        client=ollama_client,
        profile=profile, call=call, now=now,
        embedding_threshold=threshold,
    )
```

The drain-tick wiring in `scheduler.py` also needs to pass `settings` through. Trace how `settings` reaches the drain function today and extend it.

- [ ] **Step 4: Update tests**

Tests that called `rank_pair_v2` directly (now `rank_pair`) need to add `embedding_threshold=0.30`:

```bash
grep -rn 'rank_pair(' tests/
```

For each call site, add the kwarg explicitly. Tests that exercise threshold tuning should pass the value they want to test (e.g., `embedding_threshold=0.5`).

The env-override test from Plan D (`test_rank_pair_v2_embedding_threshold_respects_env_override`) becomes simpler — just call `rank_pair(... embedding_threshold=Settings().embedding_pregate_threshold)` and assert.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add -A
git commit -m "refactor: drop lru_cache; thread embedding_threshold from Settings → rank_pair"
```

---

## Task 6: Inline `_verify_quoted_evidence` into the parser

**Files:**
- Modify: `src/call_monitor/ranking/prompt.py` (parse function gains the substring check)
- Modify: `src/call_monitor/ranking/rank_pair.py` (drop the standalone helper + its callers)
- Modify: tests

The `_verify_quoted_evidence` helper substring-checks each `quoted_evidence` field against the call text and nullifies the field on miss. It's ~30 LOC of standalone code that does a 6-line job. Inline it into `parse_response` (was `parse_v2_response`).

- [ ] **Step 1: Move the substring check into the parser**

In `parse_response`, validate quoted_evidence against an optional `verification_text` parameter:

```python
def parse_response(content: str, *, verification_text: str | None = None) -> Parsed:
    # ... existing JSON parse ...

    def _verified_quote(quote: str | None) -> str | None:
        if quote is None or verification_text is None:
            return quote
        # Truncate verbatim quote to 200 chars first.
        truncated = quote[:200]
        if not truncated.strip():
            return None
        # Case-insensitive substring against the call body.
        if truncated.lower() in verification_text.lower():
            return truncated
        return None

    parsed = Parsed(
        topic_quoted=_verified_quote(data["topic_fit"].get("quoted_evidence")),
        eligibility_quoted=_verified_quote(data["eligibility"].get("quoted_evidence")),
        exclusions_quoted=_verified_quote(data["exclusions"].get("quoted_evidence")),
        # ... other fields ...
    )
    return parsed
```

- [ ] **Step 2: Drop the standalone helper from `rank_pair.py`**

Delete `_verify_quoted_evidence` and its call site. The parser now does this work inline. Update `rank_pair`:

**Codex M3 — use the FULL call body, not `build_call_text`.** The current `_verify_quoted_evidence` in `rank_pair.py:241,316` checks against `title + summary + full_text` (no truncation). `build_call_text` (the embedding-input helper) truncates `full_text` to 8000 chars. If we use `build_call_text`, valid quotes from later in the body would start getting falsely nulled — a real regression.

Build the verification text inline (or factor into a small helper kept in `rank_pair.py`):

```python
def _build_verification_text(call) -> str:
    """Title + summary + FULL full_text (no truncation). Quoted evidence
    can come from anywhere in the call body, including past 8000 chars
    that the embedding helper truncates."""
    parts = [call.title or ""]
    if call.summary:
        parts.append(call.summary)
    if call.full_text:
        parts.append(call.full_text)
    return "\n".join(parts)


# Usage in rank_pair:
parsed = parse_response(content, verification_text=_build_verification_text(call))
```

(Don't call this `build_call_text` — that name is taken by the embedding helper which DOES truncate. Keep the two functions distinct.)

- [ ] **Step 3: Update tests**

Tests that called `_verify_quoted_evidence` directly should call `parse_response` with the appropriate `call_text` and assert on the parsed quote field.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add -A
git commit -m "refactor: inline quoted_evidence verification into parse_response"
```

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -q` → all green (expect ~350 tests after deletions; was 511)
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected"
4. `DATABASE_URL=... uv run alembic upgrade head` → applies 0008 cleanly
5. `DATABASE_URL=... uv run alembic downgrade -1 && uv run alembic upgrade head` → round-trips cleanly
6. Manual smoke (with Ollama running):
   - Visit `/admin` → no per-scheme widgets, no compare-link, just queue depth + tau
   - Visit `/profiles/<id>/edit` → no scheme radio
   - Hand-fire a rank → CallRanking row written without `scheme` column, displayed score = `aggregate_score`
   - Confirm `data/golden_set_last.json` schema is single-tau

7. **Code metrics — before/after**:
   ```bash
   find src/call_monitor -name '*.py' | xargs wc -l | tail -1
   find tests -name '*.py' | xargs wc -l | tail -1
   ```
   Expected: production drops from ~6,093 → ~5,100 LOC; tests from ~10,365 → ~8,000 LOC.

## After this plan ships

The codebase is approximately the right size for the use case. Future enhancements (reranker, profile drift detection, per-criterion stats) plug in against the simpler v2-only ranking pipeline without scheme branching to navigate.

## Out of scope (intentionally kept)

- `must_include` keyword bypass — small, has plausible operator request
- `ProfileKind` enum — used in form validation + prompt context
- Per-criterion storage in `call_ranking_criteria` table — the call detail UI uses it; alternative (JSONB on rationale) saves little
- Quoted-evidence verification (kept, just moved)
- Three-step orchestrator transaction split — necessary for embedding-before-enqueue ordering
