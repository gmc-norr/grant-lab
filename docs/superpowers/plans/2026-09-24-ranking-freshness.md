# Ranking Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank each (call, profile) pair once per input revision and drive that ranking to
completion, instead of re-ranking the whole open feed every night.

**Architecture:** Every ranking is stamped with three fingerprints: profile (exists), call
(new), and scoring settings (new). It also carries an escalation state (new). One module,
`ranking/freshness.py`, owns the rule for "is this pair current?", the open-call window,
and dead-letter suppression keyed on inputs. The ingest and 04:00 reconcile paths enqueue
only stale pairs. Backfill (a human act) enqueues every open pair of its profile. The
timeline gate fails only on expiry.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic on PostgreSQL 16, pytest +
testcontainers, React 19 + Vitest, `openapi-typescript`.

**Spec (the one home for every number and every decision):**
`docs/superpowers/specs/2026-09-24-ranking-freshness-design.md`, **rev 3**. This plan does
not restate its measurements. Where a task says "D4" or "G11", it means that section or
gate of the spec.

**Dry run (2026-09-24, base `c431d5cc`).** The code blocks of Tasks 1–4 were extracted
into a throwaway worktree with a column-only stand-in for 0080 and run against the
testcontainers Postgres: **66 passed**. Every mutation those tasks name was applied and
gave exactly its "Expected" red set: Task 1 Step 7 (both), Tasks 2–3 (six), Task 4
Step 6 (all four; the fourth fails on its `metadata` assertion, as written). The dry run
caught two plan bugs, both fixed in this text: a duplicate `call_fingerprint` kwarg in
`_stamp`, and a CHECK test that passed vacuously on empty tables. **Not run:** Task 4's
row-parity retarget (prose only), and all of Tasks 5–9. Their counts and red sets are
`PREDICTED (not run)` until you run them.

## Global Constraints

- **Precondition:** the 2026-09-08 call-embedding-freshness plan
  (`docs/superpowers/plans/2026-09-08-call-embedding-freshness.md`) is built and merged
  into the base branch. This plan uses `calls.embedding_fingerprint` and
  `call_embedding_fingerprint()` from it, and chains migration `0081` onto its `0080`.
  Task 0 stops the run if either is missing.
- Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`. Set any env var
  inline in the one command that needs it.
- Never export `ANTHROPIC_API_KEY`. The backend suite's `1 skipped` stays exactly 1;
  `0 skipped` means a live paid API call ran — stop.
- Never `pkill` anything. Use `tmux kill-session`.
- Never `ruff format` a directory. Format the files you touched, by path.
- Full backend suite: `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto`.
- `docs/ARCHITECTURE.md` is generated: `uv run python scripts/gen_architecture.py`, in the
  same commit that changes a model or a migration. Never hand-edit it.
- OpenAPI types: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the `.env`
  model logs a warning to stdout that corrupts the JSON). Needs Node 22:
  `. "$HOME/.nvm/nvm.sh" && nvm use 22`.
- A fresh worktree needs `pnpm install` in `frontend/` before any frontend command.
- Your shell's cwd resets between tool calls: start **every** Bash call with `cd $W &&`,
  and use `git -C $W`. `W=/home/parlar_ai/dev/grant-lab/.worktrees/ranking-freshness`.
- Commit titles name the change, not the activity. End every commit message with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Cite code by **name**, not line number. An earlier task that adds lines moves every
  later line.

## The mutation protocol

A gate is accepted only after it has been run against something it must reject (spec §7).
That means editing source, running, and restoring. Use exactly this shape:

```bash
cd $W && set -o pipefail
S="$W/.mutation-backups"; mkdir -p "$S"
F=$W/src/grant_lab/<file>.py
cp "$F" "$S/$(basename "$F").bak"         # a COPY; never `mv`, never `trap ... EXIT`
python3 - <<'PY'
from pathlib import Path
p = Path("<absolute path of F>")
s = p.read_text()
a = "<the exact anchor>"
assert s.count(a) == 1, f"ANCHOR MISS {s.count(a)}"   # abort BEFORE writing
p.write_text(s.replace(a, "<the mutant>"))
PY
timeout 900 uv run pytest <target> -q -p no:cacheprovider 2>&1 | tail -8
cp "$S/$(basename "$F").bak" "$F"         # explicit absolute restore
find "$W/src" -name '__pycache__' -path '*grant_lab*' -prune -exec rm -rf {} +   # stale .pyc after a same-size restore
git -C $W diff --stat -- "$F"             # must print nothing
```

- Judge on **FAILED**, never ERROR. A broken import reddens a file as collection ERRORs
  with zero FAILED, which reads like a pass.
- Record the **observed** red set next to the gate in your task report, not the expected
  one. Every flip set in this plan is `PREDICTED (not run)`.
- A mutation that reddens everything means coupled tests; one that reddens nothing
  means a hollow gate. Either is a finding. Report it; do not "fix" the test to match.

## File structure

| File | Responsibility |
|---|---|
| `alembic/versions/0081_ranking_freshness.py` (new) | the columns, the CHECK, the timeline-row delete |
| `src/grant_lab/db/models/ranking.py` | ORM mirror of 0081 |
| `src/grant_lab/profiles/fingerprint.py` | `RANKED_RAW_KEYS`, `call_rank_fingerprint` (beside 0080's `call_embedding_fingerprint`) |
| `src/grant_lab/ranking/freshness.py` (new) | `SCORING_VERSION`, `ScoringConfig`, `FreshnessContext`, the open window, `enqueue_stale_pairs`, `next_escalation_state` |
| `src/grant_lab/ranking/queue.py` | `bulk_enqueue_for_calls` / `reconcile_missing_rankings` become thin wrappers; `mark_failed` takes stamps |
| `src/grant_lab/ranking/backfill.py` | open window, no freshness, no suppression |
| `src/grant_lab/ranking/criteria.py`, `aggregate.py`, `rank_pair.py` | timeline = expired only; `escalation_wanted` |
| `src/grant_lab/ranking/worker.py` | stamps, escalation state, advice preservation, criteria rows |
| `src/grant_lab/ingestion/orchestrator.py`, `ingestion/cli.py`, `scheduler.py` | thread the `FreshnessContext` |
| `src/grant_lab/web/api/admin.py`, `web/call_detail_view.py` | estimate and label; timeline text |
| `frontend/src/components/CriterionChips.tsx`, `routes/AdminDashboard.tsx`, `api/schema.gen.ts` | chip list; admin label |

---

## Task 0: Preconditions, worktree, baseline

**Files:** none changed.

- [ ] **Step 1: Confirm 0080 is built on the base you will branch from.**

```bash
cd /home/parlar_ai/dev/grant-lab && R=$PWD && BASE=<the branch or sha that contains 0080>
git -C $R ls-tree --name-only $BASE alembic/versions/ | grep -c '^alembic/versions/0080_call_embedding_fingerprint.py$'
git -C $R show $BASE:src/grant_lab/profiles/fingerprint.py | grep -c '^def call_embedding_fingerprint'
git -C $R show $BASE:src/grant_lab/db/models/calls.py | grep -c 'embedding_fingerprint'
```

Expected: `1`, `1`, and at least `1`. **If any prints 0, STOP.** Build the 2026-09-08 plan
first; this plan cannot start without it.

Record the base SHA in your report: `git -C $R rev-parse $BASE`. Later steps call it
`$BASE`. Your shell does not keep variables between calls, so set it again each time
you need it.

- [ ] **Step 2: Create the worktree and install.**

```bash
cd /home/parlar_ai/dev/grant-lab && git worktree add .worktrees/ranking-freshness -b feat/ranking-freshness $BASE
W=/home/parlar_ai/dev/grant-lab/.worktrees/ranking-freshness
cd $W && uv sync && git -C $W branch --show-current
cd $W/frontend && pnpm install --frozen-lockfile
```

Expected: `feat/ranking-freshness`.

- [ ] **Step 3: Confirm the migration head is 0080.**

```bash
cd $W && uv run alembic heads
```

Expected: `0080 (head)`. If it is anything else, STOP and report. `0081`'s
`down_revision` below assumes 0080.

- [ ] **Step 4: Measure the baseline. Do not copy a number from anywhere.**

```bash
cd $W && timeout 1800 uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -3
cd $W/frontend && pnpm exec vitest run 2>&1 | tail -5
```

Record both totals in your report. The backend line must show `1 skipped`. Every later
"suite green" claim is a delta against these numbers.

---

## Task 1: Schema — migration 0081 and the ORM

**Files:**
- Create: `alembic/versions/0081_ranking_freshness.py`
- Modify: `src/grant_lab/db/models/ranking.py` (classes `CallRanking`, `RankingJob`)
- Create: `tests/test_migration_0081_ranking_freshness.py`
- Regenerate: `docs/ARCHITECTURE.md`

**Interfaces:**
- Produces: `CallRanking.call_fingerprint: str | None`,
  `CallRanking.scoring_fingerprint: str | None`,
  `CallRanking.escalation_state: str | None` (`'pending' | 'gave_up' | None`),
  `CallRanking.escalation_failures: int` (default 0),
  `RankingJob.profile_fingerprint / call_fingerprint / scoring_fingerprint: str | None`.

- [ ] **Step 1: Write the failing test.**

`tests/test_migration_0081_ranking_freshness.py`:

```python
"""Migration 0081: ranking-freshness columns (spec D1) and the timeline-row delete (G16)."""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from grant_lab.db.models.ranking import CallRanking


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_the_new_columns_exist(migrated_engine):
    insp = inspect(migrated_engine)
    cr = {c["name"] for c in insp.get_columns("call_rankings")}
    rj = {c["name"] for c in insp.get_columns("ranking_jobs")}
    assert {"call_fingerprint", "scoring_fingerprint", "escalation_state",
            "escalation_failures"} <= cr
    assert {"profile_fingerprint", "call_fingerprint", "scoring_fingerprint"} <= rj


def test_escalation_state_is_checked_live_and_in_the_orm(migrated_engine):
    live = {c["name"] for c in inspect(migrated_engine).get_check_constraints("call_rankings")}
    assert "ck_call_rankings_escalation_state" in live
    orm = {c.name for c in CallRanking.__table__.constraints}
    assert "ck_call_rankings_escalation_state" in orm


def test_escalation_state_accepts_pending_and_rejects_an_unknown_value(pg_session_factory):
    """The CHECK is real, not just named. Seeded on purpose: an INSERT ... SELECT
    over empty tables writes no row and raises nothing, which passes vacuously
    (it did, in the dry run of this plan)."""
    from tests.freshness_helpers import make_call, make_profile

    with pg_session_factory() as s:
        p, c = make_profile(s), make_call(s)
        cr = CallRanking(call_id=c.id, profile_id=p.id, aggregate_score=0, rationale="",
                         model="m", prompt_version="v", raw_response={},
                         escalation_state="pending")
        s.add(cr)
        s.flush()  # a known state is accepted
        cr.escalation_state = "bogus"
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_0081_deletes_timeline_rows_only_and_leaves_stamps_null(migrated_engine, pg_url):
    """G16. Seed AT 0080, upgrade, check. `migrated_engine` is requested so head
    has run before this test's own downgrade (see the 0075 test's docstring)."""
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0080")
        with engine.begin() as conn:
            assert "call_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("call_rankings")
            }
            src = conn.execute(text(
                "INSERT INTO sources (name, slug) VALUES ('S0081','s0081') RETURNING id"
            )).scalar_one()
            uid = conn.execute(text(
                "INSERT INTO users (email, status) VALUES ('m0081@umu.se','active') RETURNING id"
            )).scalar_one()
            pid = conn.execute(text(
                "INSERT INTO profiles (name, kind, description, structured, created_by) "
                "VALUES ('M0081','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
            ), {"u": uid}).scalar_one()
            cid = conn.execute(text(
                "INSERT INTO calls (source_id, external_id, title, url) "
                "VALUES (:s,'M0081','t','https://example.org/m0081') RETURNING id"
            ), {"s": src}).scalar_one()
            conn.execute(text(
                "INSERT INTO call_rankings (call_id, profile_id, aggregate_score, model, "
                "prompt_version, raw_response) VALUES (:c,:p,40,'gemma','v5.0','{}'::jsonb)"
            ), {"c": cid, "p": pid})
            for crit in ("timeline", "budget"):
                conn.execute(text(
                    "INSERT INTO call_ranking_criteria (call_id, profile_id, criterion, rating) "
                    "VALUES (:c,:p,:k,'pass')"
                ), {"c": cid, "p": pid, "k": crit})

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            crits = set(conn.execute(text(
                "SELECT criterion FROM call_ranking_criteria WHERE call_id=:c AND profile_id=:p"
            ), {"c": cid, "p": pid}).scalars())
            row = conn.execute(text(
                "SELECT call_fingerprint, scoring_fingerprint, escalation_state, "
                "escalation_failures FROM call_rankings WHERE call_id=:c AND profile_id=:p"
            ), {"c": cid, "p": pid}).one()
        assert crits == {"budget"}, crits
        assert tuple(row) == (None, None, None, 0), row
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM call_ranking_criteria WHERE profile_id IN "
                    "(SELECT id FROM profiles WHERE name='M0081')"
                ))
                conn.execute(text(
                    "DELETE FROM call_rankings WHERE profile_id IN "
                    "(SELECT id FROM profiles WHERE name='M0081')"
                ))
                conn.execute(text("DELETE FROM calls WHERE external_id='M0081'"))
                conn.execute(text("DELETE FROM profiles WHERE name='M0081'"))
                conn.execute(text("DELETE FROM users WHERE email='m0081@umu.se'"))
                conn.execute(text("DELETE FROM sources WHERE slug='s0081'"))
        finally:
            engine.dispose()
```

- [ ] **Step 2: Run it and watch it fail.**

```bash
cd $W && timeout 900 uv run pytest tests/test_migration_0081_ranking_freshness.py -q -p no:cacheprovider 2>&1 | tail -8
```

Expected: FAILED on missing columns / constraint, and an alembic error on `downgrade
0080`… `upgrade head` finding nothing new. Judge by FAILED lines, not ERROR.

- [ ] **Step 3: Write the migration.**

`alembic/versions/0081_ranking_freshness.py`:

```python
"""ranking freshness: stamp what a ranking describes, track its second reading

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Spec D1. NULL means stale for both fingerprints: no grandfathering,
    # because one re-rank of the open feed is one ordinary night of work.
    op.add_column("call_rankings", sa.Column("call_fingerprint", sa.Text(), nullable=True))
    op.add_column("call_rankings", sa.Column("scoring_fingerprint", sa.Text(), nullable=True))
    # Spec D6. NULL = nothing outstanding.
    op.add_column("call_rankings", sa.Column("escalation_state", sa.Text(), nullable=True))
    op.add_column(
        "call_rankings",
        sa.Column(
            "escalation_failures", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.create_check_constraint(
        "ck_call_rankings_escalation_state",
        "call_rankings",
        "escalation_state IN ('pending','gave_up')",
    )
    # Spec D7: what a failed job failed ON. NULL on every pre-0081 row, which
    # keeps today's pair-wide 7-day suppression for those rows.
    for col in ("profile_fingerprint", "call_fingerprint", "scoring_fingerprint"):
        op.add_column("ranking_jobs", sa.Column(col, sa.Text(), nullable=True))
    # Spec D8: the writer no longer produces a timeline criterion row. Closed
    # pairs are never re-ranked, so without this their stale chip would stay.
    op.execute("DELETE FROM call_ranking_criteria WHERE criterion = 'timeline'")


def downgrade() -> None:
    # The deleted timeline rows are not restored: they were derived data, and
    # the next re-rank on the old code writes them again.
    for col in ("scoring_fingerprint", "call_fingerprint", "profile_fingerprint"):
        op.drop_column("ranking_jobs", col)
    op.drop_constraint("ck_call_rankings_escalation_state", "call_rankings", type_="check")
    op.drop_column("call_rankings", "escalation_failures")
    op.drop_column("call_rankings", "escalation_state")
    op.drop_column("call_rankings", "scoring_fingerprint")
    op.drop_column("call_rankings", "call_fingerprint")
```

- [ ] **Step 4: Mirror it in the ORM.** In `src/grant_lab/db/models/ranking.py`:

In `CallRanking.__table_args__`, after the `Index("ix_call_rankings_profile_score", …)` entry, add:

```python
        CheckConstraint(
            "escalation_state IN ('pending','gave_up')",
            name="ck_call_rankings_escalation_state",
        ),
```

In `CallRanking`, directly after the `profile_fingerprint` column, add:

```python
    # Which call content this ranking describes: call_rank_fingerprint(), taken
    # BEFORE rank_pair ran. NULL = stale (migration 0081 leaves every old row NULL).
    call_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which scorer settings produced it: ScoringConfig.fingerprint() (spec D3).
    scoring_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether its second reading is still owed (spec D6). NULL = nothing outstanding.
    escalation_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # parse_error escalation failures within ONE input revision (spec D6).
    escalation_failures: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), default=0
    )
```

In `RankingJob`, directly after `last_error`, add:

```python
    # The inputs a terminally-failed job failed ON (spec D7). Written by
    # mark_failed(stamps=...); NULL on rows from before migration 0081.
    profile_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    scoring_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
```

`CheckConstraint`, `SmallInteger`, `Text` and `text` are already imported in that module.
Check with `grep -nE "^\s+(CheckConstraint|SmallInteger|Text|text)," src/grant_lab/db/models/ranking.py`.

- [ ] **Step 5: Run the test, then the drift and round-trip tests.**

```bash
cd $W && timeout 900 uv run pytest tests/test_migration_0081_ranking_freshness.py tests/test_alembic_v2_round_trip.py tests/test_models_ranking.py -q -p no:cacheprovider 2>&1 | tail -5
cd $W && ls tests | grep -i drift && timeout 900 uv run pytest tests/test_schema_drift.py -q -p no:cacheprovider 2>&1 | tail -3
```

Expected: all PASS.

- [ ] **Step 6: Regenerate the architecture map and check its gate.**

```bash
cd $W && uv run python scripts/gen_architecture.py && git -C $W diff --stat -- docs/ARCHITECTURE.md
cd $W && timeout 600 uv run pytest tests/architecture/test_architecture_map_is_current.py -q -p no:cacheprovider 2>&1 | tail -3
```

Expected: `docs/ARCHITECTURE.md` changes (new columns, the new CHECK vocabulary). The gate
PASSES.

- [ ] **Step 7: Prove G16 bites.** Mutation: in the migration, change
  `WHERE criterion = 'timeline'` to `WHERE criterion IN ('timeline','budget')`. Run the
  migration test. Expected: `test_0081_deletes_timeline_rows_only…` FAILED (`budget` gone).
  Restore. Then change it to `WHERE false`. Expected: the same test FAILED (`timeline`
  kept). Restore.

- [ ] **Step 8: Commit.**

```bash
cd $W && uv run ruff check alembic/versions/0081_ranking_freshness.py src/grant_lab/db/models/ranking.py tests/test_migration_0081_ranking_freshness.py && uv run ruff format alembic/versions/0081_ranking_freshness.py src/grant_lab/db/models/ranking.py tests/test_migration_0081_ranking_freshness.py
git -C $W add alembic/versions/0081_ranking_freshness.py src/grant_lab/db/models/ranking.py tests/test_migration_0081_ranking_freshness.py docs/ARCHITECTURE.md
git -C $W commit -m "feat(ranking): stamp what a ranking describes and whether its second reading is owed" -m "Migration 0081 (spec D1). Also drops the timeline criterion rows the writer stops producing in a later commit." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 2: The call fingerprint

**Files:**
- Modify: `src/grant_lab/profiles/fingerprint.py` (add `RANKED_RAW_KEYS`, `_instant`, `call_rank_fingerprint`)
- Create: `tests/test_call_rank_fingerprint.py`

**Interfaces:**
- Consumes: `_digest`, and `call.embedding_fingerprint` / `call.embedding_model` (0080).
- Produces: `RANKED_RAW_KEYS: tuple[str, ...] == ("trl_min", "trl_max")`;
  `call_rank_fingerprint(call) -> str` (prefix `f"{FINGERPRINT_VERSION}:"`). `call` is a
  `Call` **or** any row with the attributes `url, title, summary, full_text, deadline,
  budget_min, budget_max, currency, programme, keywords, published_at, raw,
  embedding_fingerprint, embedding_model`.

- [ ] **Step 1: Write the failing tests.**

`tests/test_call_rank_fingerprint.py`:

```python
"""Spec D2: what the call contributed to a ranking. Gates G3, G4, G5, G15 (unit level)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grant_lab.profiles.fingerprint import (
    FINGERPRINT_VERSION,
    RANKED_RAW_KEYS,
    call_rank_fingerprint,
)
from grant_lab.ranking.rank_pair import rank_pair
from tests.freshness_helpers import ollama_clean

_DEADLINE = datetime(2099, 3, 1, 12, 0, tzinfo=UTC)


def _call(**over):
    base = dict(
        url="https://example.org/c/1",
        title="AI in pathology",
        summary="Funding for AI diagnostics.",
        full_text="Body text.",
        deadline=_DEADLINE,
        budget_min=100_000,
        budget_max=500_000,
        currency="EUR",
        programme="Digital Europe",
        keywords=["ai", "health"],
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        raw={"trl_min": 3, "trl_max": 6, "metadata": {"fetched": "2026-09-24"}},
        embedding_fingerprint="v2:" + "a" * 64,
        embedding_model="bge-m3",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_it_carries_the_version_tag():
    assert call_rank_fingerprint(_call()).startswith(f"{FINGERPRINT_VERSION}:")


def test_ranked_raw_keys_are_the_two_trl_bounds():
    assert RANKED_RAW_KEYS == ("trl_min", "trl_max")


@pytest.mark.parametrize(
    "field, value",
    [
        ("url", "https://example.org/c/2"),
        ("title", "Something else"),
        ("summary", "Changed summary."),
        ("full_text", "Changed body."),
        ("deadline", _DEADLINE + timedelta(days=30)),
        ("budget_min", 1),
        ("budget_max", 2),
        ("currency", "SEK"),
        ("programme", "Horizon Europe"),
        ("keywords", ["ai"]),
        ("published_at", datetime(2026, 9, 2, tzinfo=UTC)),
        ("embedding_fingerprint", "v2:" + "b" * 64),  # G15, unit level
        ("embedding_model", "mxbai-embed-large"),
    ],
)
def test_every_ranked_field_changes_it(field, value):
    assert call_rank_fingerprint(_call(**{field: value})) != call_rank_fingerprint(_call())


def test_a_raw_change_outside_the_ranked_keys_does_not_change_it():
    """G3, unit level. Raw churns between fetches of an identical call."""
    churned = _call(raw={"trl_min": 3, "trl_max": 6, "metadata": {"fetched": "2026-09-25"}})
    assert call_rank_fingerprint(churned) == call_rank_fingerprint(_call())


def test_a_trl_change_does_change_it():
    """G4, unit level. Without this, 'ignore raw entirely' passes G3."""
    moved = _call(raw={"trl_min": 4, "trl_max": 6, "metadata": {"fetched": "2026-09-24"}})
    assert call_rank_fingerprint(moved) != call_rank_fingerprint(_call())


def test_the_same_instant_in_another_zone_is_the_same_fingerprint():
    """The DB hands back timestamptz in the connection's zone; a Python-built
    Call holds UTC. Equal instants must hash equally, or every pair a test
    builds in Python looks stale to a row read back from Postgres."""
    cet = timezone(timedelta(hours=1))
    shifted = _call(deadline=_DEADLINE.astimezone(cet))
    assert call_rank_fingerprint(shifted) == call_rank_fingerprint(_call())


class _Recording(dict):
    """A dict that remembers every key it was asked for."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


def test_ranking_reads_only_the_ranked_raw_keys():
    """G5. Keys read from call.structured during a real rank_pair run must be a
    subset of RANKED_RAW_KEYS. A key read and not hashed is a ranking input the
    fingerprint cannot see."""
    rec = _Recording({"trl_min": 3, "trl_max": 6, "metadata": {"x": 1}})
    call = _call(structured=rec, raw=rec)
    profile = SimpleNamespace(
        name="P",
        kind="organizational_unit",
        description="AI in pathology",
        structured={"areas": ["AI"], "trl_min": 2, "trl_max": 7},
        budget_currency=None,
        embedding=None,
        embedding_model=None,
    )
    rank_pair(
        client=ollama_clean(),
        profile=profile,
        call=call,
        now=datetime(2026, 9, 24, tzinfo=UTC),
        embedding_threshold=0.0,
    )
    assert rec.read, "ranking read nothing from call.structured -- the gate is hollow"
    assert rec.read <= set(RANKED_RAW_KEYS), rec.read - set(RANKED_RAW_KEYS)
```

- [ ] **Step 2: Run them and watch them fail.**

```bash
cd $W && timeout 600 uv run pytest tests/test_call_rank_fingerprint.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: collection ERROR on `ImportError: cannot import name 'RANKED_RAW_KEYS'`. This is
the one place an ERROR is the expected red, because the names do not exist yet.

- [ ] **Step 3: Implement.** Append to `src/grant_lab/profiles/fingerprint.py`, after
  `call_embedding_fingerprint` (added by the 0080 plan):

```python
# The raw keys ranking reads from Call.structured (which IS Call.raw). The one
# enumeration ranking spec D2 cannot avoid: raw carries source metadata that
# changes between fetches of an unchanged call, so hashing it whole would
# re-rank the feed nightly. tests/test_call_rank_fingerprint.py records the
# keys rank_pair actually reads and fails if one is missing here.
RANKED_RAW_KEYS: tuple[str, ...] = ("trl_min", "trl_max")


def _instant(value):
    """A datetime as one canonical UTC string, whatever zone it arrived in."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def call_rank_fingerprint(call) -> str:
    """What a stored ranking describes on the call side (ranking spec D2).

    Every NormalizedCall content field except external_id (identity) and raw,
    plus the RANKED_RAW_KEYS projection of raw, plus the call's own vector
    identity (embedding_fingerprint + embedding_model), because the cosine gate
    reads the vector.

    `call` may be a loaded Call or a narrowed row from ranking/freshness.py
    whose `raw` is already the projection. Both give the same bytes;
    tests/test_ranking_freshness_enqueue.py (G27) pins that.
    """
    raw = call.raw or {}
    return _digest(
        {
            "url": call.url,
            "title": call.title,
            "summary": call.summary,
            "full_text": call.full_text,
            "deadline": _instant(call.deadline),
            "budget_min": call.budget_min,
            "budget_max": call.budget_max,
            "currency": call.currency,
            "programme": call.programme,
            "keywords": list(call.keywords or []),
            "published_at": _instant(call.published_at),
            "raw": {k: raw.get(k) for k in RANKED_RAW_KEYS},
            "embedding_fingerprint": call.embedding_fingerprint,
            "embedding_model": call.embedding_model,
        }
    )
```

And at the top of the file, extend the imports:

```python
from datetime import UTC
```

- [ ] **Step 4: Run the tests.**

```bash
cd $W && timeout 600 uv run pytest tests/test_call_rank_fingerprint.py tests/test_profile_fingerprint.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 5: Prove G5 and G3/G4 bite.**
  1. `src/grant_lab/ranking/criteria.py`, anchor `c_min, c_max = c.get("trl_min"), c.get("trl_max")`,
     mutant `c_min, c_max = c.get("trl_min"), c.get("trl_max"); c.get("trl_note")`.
     Expected: `test_ranking_reads_only_the_ranked_raw_keys` FAILED. Restore.
  2. `src/grant_lab/profiles/fingerprint.py`, anchor
     `"raw": {k: raw.get(k) for k in RANKED_RAW_KEYS},`, mutant `"raw": raw,`.
     Expected: `test_a_raw_change_outside_the_ranked_keys_does_not_change_it` FAILED,
     `test_a_trl_change_does_change_it` still PASSES. Restore.
  3. Same anchor, mutant `"raw": {},`. Expected: `test_a_trl_change_does_change_it`
     FAILED. Restore.

- [ ] **Step 6: Commit.**

```bash
cd $W && uv run ruff check src/grant_lab/profiles/fingerprint.py tests/test_call_rank_fingerprint.py && uv run ruff format src/grant_lab/profiles/fingerprint.py tests/test_call_rank_fingerprint.py
git -C $W add src/grant_lab/profiles/fingerprint.py tests/test_call_rank_fingerprint.py
git -C $W commit -m "feat(ranking): fingerprint the call content a ranking describes" -m "Hashes the typed call fields, the two raw keys ranking reads, and the call's vector identity (spec D2). G5 records the raw keys rank_pair actually reads." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3: The scoring config and its classification gate

**Files:**
- Create: `src/grant_lab/ranking/freshness.py` (first part: constants, `ScoringConfig`, `FreshnessContext`, `next_escalation_state`)
- Create: `tests/test_ranking_scoring_config.py`
- Modify: `tests/freshness_helpers.py` (add `scoring_for`, `ctx_for`)

**Interfaces:**
- Produces:
  - `SCORING_VERSION: str`
  - `DEAD_LETTER_SUPPRESS_DAYS: int == 7`
  - `ESCALATION_MAX_PARSE_FAILURES: int == 3`
  - `ScoringConfig` (frozen), with fields `local_model: str | None`,
    `embedding_threshold: float`, `embedding_hard_fail_threshold: float`,
    `escalation_min_score: int`, `logprobs_enabled: bool`. Methods:
    - `ScoringConfig.for_drain(*, client, embedding_threshold, embedding_hard_fail_threshold, escalation_min_score, logprobs_enabled) -> ScoringConfig`
    - `ScoringConfig.from_settings(settings) -> ScoringConfig`
    - `.fingerprint() -> str`
  - `FreshnessContext` (frozen), with fields `scoring: ScoringConfig` and
    `remote_enabled: bool`, plus `FreshnessContext.from_settings(settings)`.
  - `next_escalation_state(*, wanted: bool, escalated: bool, remote_configured: bool, remote_withheld: bool, outcome: str | None, prev_failures: int) -> tuple[str | None, int]`
  - Test helpers `scoring_for(client=None, **over) -> ScoringConfig` and
    `ctx_for(client=None, *, remote_enabled=False, **over) -> FreshnessContext`.

- [ ] **Step 1: Write the failing tests.**

`tests/test_ranking_scoring_config.py`:

```python
"""Spec D3 and D6 (pure parts). Gates G8 (unit), G24 (unit), G25."""

from __future__ import annotations

import dataclasses
import inspect
from types import SimpleNamespace

import pytest

from grant_lab.profiles.fingerprint import FINGERPRINT_VERSION
from grant_lab.ranking import freshness as F
from grant_lab.ranking.freshness import ScoringConfig, next_escalation_state
from grant_lab.ranking.worker import drain_queue

# G25: every drain_queue parameter in exactly one class (spec D3's table).
SCORING_PARAMS = {
    "client",
    "embedding_threshold",
    "embedding_hard_fail_threshold",
    "escalation_min_score",
    "logprobs_enabled",
}
ESCALATION_PARAMS = {"remote_client", "remote_daily_cap"}
NO_EFFECT_PARAMS = {"session_factory", "batch_size", "tick_budget_seconds", "_clock"}


def _cfg(**over):
    base = dict(
        client=SimpleNamespace(model="gemma4:e4b-it-q4_K_M"),
        embedding_threshold=0.30,
        embedding_hard_fail_threshold=0.20,
        escalation_min_score=45,
        logprobs_enabled=False,
    )
    base.update(over)
    return ScoringConfig.for_drain(**base)


def test_every_drain_queue_parameter_is_classified_exactly_once():
    params = set(inspect.signature(drain_queue).parameters)
    classes = (SCORING_PARAMS, ESCALATION_PARAMS, NO_EFFECT_PARAMS)
    assert all(classes), "an empty class makes this gate pass for free"
    assert SCORING_PARAMS | ESCALATION_PARAMS | NO_EFFECT_PARAMS == params, (
        params ^ (SCORING_PARAMS | ESCALATION_PARAMS | NO_EFFECT_PARAMS)
    )
    assert sum(len(c) for c in classes) == len(params), "a parameter is in two classes"


def test_the_payload_class_is_exactly_what_for_drain_hashes():
    assert set(inspect.signature(ScoringConfig.for_drain).parameters) == SCORING_PARAMS


def test_it_carries_the_version_tag():
    assert _cfg().fingerprint().startswith(f"{FINGERPRINT_VERSION}:")


@pytest.mark.parametrize(
    "over",
    [
        {"client": SimpleNamespace(model="gemma4:e4b-q8")},
        {"embedding_threshold": 0.31},
        {"embedding_hard_fail_threshold": 0.10},  # G24, unit level
        {"escalation_min_score": 50},
        {"logprobs_enabled": True},
    ],
)
def test_every_scoring_setting_changes_it(over):
    assert _cfg(**over).fingerprint() != _cfg().fingerprint()


def test_every_dataclass_field_reaches_the_fingerprint():
    """A field added to ScoringConfig but left out of fingerprint() is a
    setting that changes nothing. Perturb each field directly."""
    base = _cfg()
    for f in dataclasses.fields(ScoringConfig):
        v = getattr(base, f.name)
        bumped = (not v) if isinstance(v, bool) else (v + 1 if isinstance(v, (int, float)) else f"{v}-x")
        assert dataclasses.replace(base, **{f.name: bumped}).fingerprint() != base.fingerprint(), f.name


def test_a_prompt_version_bump_changes_it(monkeypatch):
    """G8, unit level."""
    before = _cfg().fingerprint()
    monkeypatch.setattr(F, "PROMPT_VERSION", "v999")
    assert _cfg().fingerprint() != before


def test_a_scoring_version_bump_changes_it(monkeypatch):
    before = _cfg().fingerprint()
    monkeypatch.setattr(F, "SCORING_VERSION", "999")
    assert _cfg().fingerprint() != before


def test_a_missing_hard_fail_threshold_means_the_single_cut():
    """drain_queue's None collapses to the pregate threshold (criteria.py);
    the fingerprint must say the same thing either way."""
    assert (
        _cfg(embedding_threshold=0.3, embedding_hard_fail_threshold=None).fingerprint()
        == _cfg(embedding_threshold=0.3, embedding_hard_fail_threshold=0.3).fingerprint()
    )


def test_from_settings_matches_the_mapping_main_py_uses():
    """The ingest CLI has no scheduler, so it builds from Settings. main.py maps
    these Settings fields one-to-one into build_scheduler."""
    s = SimpleNamespace(
        gemma_model="gemma4:e4b-it-q4_K_M",
        embedding_pregate_threshold=0.30,
        embedding_hard_fail_threshold=0.20,
        ranking_escalation_min_score=45,
        ranking_logprobs_enabled=False,
    )
    assert ScoringConfig.from_settings(s) == _cfg()


@pytest.mark.parametrize(
    "kw, expected",
    [
        # not wanted / escalated / no remote configured -> nothing outstanding
        (dict(wanted=False, escalated=False, remote_configured=True, remote_withheld=False, outcome=None, prev_failures=2), (None, 0)),
        (dict(wanted=True, escalated=True, remote_configured=True, remote_withheld=False, outcome="success", prev_failures=2), (None, 0)),
        (dict(wanted=True, escalated=False, remote_configured=False, remote_withheld=False, outcome=None, prev_failures=0), (None, 0)),  # G21
        # withheld by the cap -> pending, failures unchanged (G17)
        (dict(wanted=True, escalated=False, remote_configured=True, remote_withheld=True, outcome=None, prev_failures=1), ("pending", 1)),
        # service failure -> pending, failures unchanged (G18)
        (dict(wanted=True, escalated=False, remote_configured=True, remote_withheld=False, outcome="llm_error", prev_failures=2), ("pending", 2)),
        # this pair's response failed -> +1, gave_up at 3 (G19)
        (dict(wanted=True, escalated=False, remote_configured=True, remote_withheld=False, outcome="parse_error", prev_failures=0), ("pending", 1)),
        (dict(wanted=True, escalated=False, remote_configured=True, remote_withheld=False, outcome="parse_error", prev_failures=2), ("gave_up", 3)),
    ],
)
def test_next_escalation_state(kw, expected):
    assert next_escalation_state(**kw) == expected
```

- [ ] **Step 2: Run and watch it fail.**

```bash
cd $W && timeout 600 uv run pytest tests/test_ranking_scoring_config.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: collection ERROR, `ModuleNotFoundError: grant_lab.ranking.freshness`.

- [ ] **Step 3: Create `src/grant_lab/ranking/freshness.py`.**

```python
"""Which (call, profile) pairs need ranking work.

Ranking spec: docs/superpowers/specs/2026-09-24-ranking-freshness-design.md.
A pair is ranked once per INPUT REVISION -- profile, call and scoring settings,
each fingerprinted -- and that ranking is driven to completion, including its
second reading (escalation_state). This module owns the rule for "current",
the open-call window, and dead-letter suppression keyed on inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from grant_lab.profiles.fingerprint import _digest
from grant_lab.ranking.prompt import PROMPT_VERSION

_logger = logging.getLogger(__name__)

# Bump when a change to ranking/criteria.py, ranking/aggregate.py or
# prompt.parse_response changes what a stored ranking would say for the same
# inputs. No gate can see a semantic code change, so this is a convention
# (spec D3); the admin "Re-rank everything" button is the fallback. A bump
# re-ranks every open pair once, on the next ingest or 04:00 reconcile.
SCORING_VERSION = "1"

# How long a terminally-failed job suppresses a retry of the SAME inputs (spec D7).
DEAD_LETTER_SUPPRESS_DAYS = 7

# parse_error escalation failures allowed per input revision (spec D6).
ESCALATION_MAX_PARSE_FAILURES = 3


@dataclass(frozen=True)
class ScoringConfig:
    """The scorer settings that can change a stored result (spec D3).

    Build it from the SAME values drain_queue receives -- for_drain() -- never
    from Settings on one side and parameters on the other: tests run the worker
    with non-default values, and a mismatch makes every pair look stale forever.
    The remote model id is deliberately absent (spec D3, rev 3).
    """

    local_model: str | None
    embedding_threshold: float
    embedding_hard_fail_threshold: float
    escalation_min_score: int
    logprobs_enabled: bool

    @classmethod
    def for_drain(
        cls,
        *,
        client,
        embedding_threshold,
        embedding_hard_fail_threshold,
        escalation_min_score,
        logprobs_enabled,
    ) -> ScoringConfig:
        # criteria.evaluate_gates collapses a None hard-fail to the pregate
        # threshold; normalise here so both spellings hash the same.
        hard = embedding_threshold if embedding_hard_fail_threshold is None else embedding_hard_fail_threshold
        return cls(
            local_model=getattr(client, "model", None),
            embedding_threshold=float(embedding_threshold),
            embedding_hard_fail_threshold=float(hard),
            escalation_min_score=int(escalation_min_score),
            logprobs_enabled=bool(logprobs_enabled),
        )

    @classmethod
    def from_settings(cls, settings) -> ScoringConfig:
        """For the ingest CLI only, which has no scheduler. Mirrors the one-to-one
        mapping main.py hands build_scheduler."""
        return cls.for_drain(
            client=_Named(settings.gemma_model),
            embedding_threshold=settings.embedding_pregate_threshold,
            embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold,
            escalation_min_score=settings.ranking_escalation_min_score,
            logprobs_enabled=settings.ranking_logprobs_enabled,
        )

    def fingerprint(self) -> str:
        return _digest(
            {
                "prompt_version": PROMPT_VERSION,
                "scoring_version": SCORING_VERSION,
                "local_model": self.local_model,
                "embedding_threshold": self.embedding_threshold,
                "embedding_hard_fail_threshold": self.embedding_hard_fail_threshold,
                "escalation_min_score": self.escalation_min_score,
                "logprobs_enabled": self.logprobs_enabled,
            }
        )


@dataclass(frozen=True)
class _Named:
    model: str


@dataclass(frozen=True)
class FreshnessContext:
    """What the enqueue paths need beyond the database (spec D4).

    `remote_enabled` decides whether a `pending` escalation makes a pair due.
    It is not hashed: adding or removing an API key must not re-rank anything.
    """

    scoring: ScoringConfig
    remote_enabled: bool

    @classmethod
    def from_settings(cls, settings) -> FreshnessContext:
        return cls(
            scoring=ScoringConfig.from_settings(settings),
            remote_enabled=bool(settings.anthropic_api_key),
        )


def next_escalation_state(
    *,
    wanted: bool,
    escalated: bool,
    remote_configured: bool,
    remote_withheld: bool,
    outcome: str | None,
    prev_failures: int,
) -> tuple[str | None, int]:
    """The (escalation_state, escalation_failures) a ranking is stored with (spec D6).

    `prev_failures` is the existing row's count when the three stamps match
    (same input revision), else 0 -- the caller decides that.
    """
    if not wanted or escalated or not remote_configured:
        return None, 0
    if outcome == "parse_error":
        failures = prev_failures + 1
        return ("gave_up" if failures >= ESCALATION_MAX_PARSE_FAILURES else "pending"), failures
    # Withheld by the daily cap, llm_error (the SERVICE failed), or no attempt
    # recorded: owed, and not this pair's fault.
    return "pending", prev_failures
```

The `remote_withheld` argument is not read on its own path: a withheld escalation reaches
the final `return "pending", prev_failures` because its `outcome` is `None`. It stays in
the signature so the worker's call site says *why* the pair is pending. G17 pins the
behaviour.

- [ ] **Step 4: Add the test helpers.** In `tests/freshness_helpers.py`, add to the imports:

```python
from grant_lab.ranking.freshness import FreshnessContext, ScoringConfig
```

and append:

```python
def scoring_for(client=None, **over) -> ScoringConfig:
    """The ScoringConfig a drain_queue(client=client, embedding_threshold=0.0,
    ...defaults) run stamps. Keep in step with how the tests below call drain."""
    kw = dict(
        client=client,
        embedding_threshold=0.0,
        embedding_hard_fail_threshold=None,
        escalation_min_score=45,
        logprobs_enabled=False,
    )
    kw.update(over)
    return ScoringConfig.for_drain(**kw)


def ctx_for(client=None, *, remote_enabled: bool = False, **over) -> FreshnessContext:
    return FreshnessContext(scoring=scoring_for(client, **over), remote_enabled=remote_enabled)
```

Also let `make_call` take `summary` and `full_text` overrides. Replace its two lines
`summary="Funding for AI-based diagnostics.",` and
`full_text="Body about eligibility, scope, budget.",` with:

```python
        summary=kw.pop("summary", "Funding for AI-based diagnostics."),
        full_text=kw.pop("full_text", "Body about eligibility, scope, budget."),
```

- [ ] **Step 5: Run.**

```bash
cd $W && timeout 600 uv run pytest tests/test_ranking_scoring_config.py tests/test_ranking_reconcile_staleness.py tests/test_ranking_worker_fingerprint.py -q -p no:cacheprovider 2>&1 | tail -5
cd $W && timeout 600 uv run pytest tests/architecture/test_no_import_cycles.py -q -p no:cacheprovider 2>&1 | tail -3
```

Expected: all PASS. The import-cycle gate must stay green. `freshness` imports
`profiles.fingerprint` and `ranking.prompt`; `ranking.queue` already imports
`profiles.fingerprint`, so no new package edge is added.

- [ ] **Step 6: Prove G25 bites.**
  1. Mutation: add a keyword parameter `foo: int = 0` to `drain_queue`'s signature (anchor
     `    logprobs_enabled: bool = False,\n    _clock=None,`, mutant
     `    logprobs_enabled: bool = False,\n    foo: int = 0,\n    _clock=None,`).
     Expected: `test_every_drain_queue_parameter_is_classified_exactly_once` FAILED.
     Restore.
  2. Mutation: in `fingerprint()`, delete the `"logprobs_enabled": self.logprobs_enabled,`
     line. Expected: `test_every_scoring_setting_changes_it[over4]` and
     `test_every_dataclass_field_reaches_the_fingerprint` FAILED. Restore.

- [ ] **Step 7: Commit.**

```bash
cd $W && uv run ruff check src/grant_lab/ranking/freshness.py tests/test_ranking_scoring_config.py tests/freshness_helpers.py && uv run ruff format src/grant_lab/ranking/freshness.py tests/test_ranking_scoring_config.py tests/freshness_helpers.py
git -C $W add src/grant_lab/ranking/freshness.py tests/test_ranking_scoring_config.py tests/freshness_helpers.py
git -C $W commit -m "feat(ranking): fingerprint the scorer settings a ranking was made with" -m "ScoringConfig over drain_queue's result-affecting parameters (spec D3), G25 classifies every parameter, and the pure escalation-state rule (spec D6)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 4: The one freshness rule — `enqueue_stale_pairs`

**Files:**
- Modify: `src/grant_lab/ranking/freshness.py` (add `_profile_rows`, `_call_rows`, `enqueue_stale_pairs`)
- Create: `tests/test_ranking_freshness_enqueue.py`
- Modify: `tests/test_ranking_queue_row_parity.py` (retarget to `_profile_rows`)

**Interfaces:**
- Consumes: `call_rank_fingerprint`, `RANKED_RAW_KEYS`, `rank_fingerprint`,
  `LEGACY_SENTINEL`, `FreshnessContext`, `DEAD_LETTER_SUPPRESS_DAYS`.
- Produces:
  - `enqueue_stale_pairs(session, *, ctx: FreshnessContext, call_ids: list[int] | None = None) -> int`.
    It inserts pending jobs for stale, unsuppressed (open call × active profile) pairs.
    `call_ids=None` means all open calls; `[]` returns 0. It does not commit.
  - `_profile_rows(session) -> list[Row]`
  - `_call_rows(session, call_ids: list[int] | None) -> list[Row]`

- [ ] **Step 1: Write the failing tests.**

`tests/test_ranking_freshness_enqueue.py`:

```python
"""Spec D4, D6 (enqueue half), D7. Gates G1-G4, G6, G7, G11, G13, G15, G22, G24, G27."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from grant_lab.db.models import Call, CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking import freshness as F
from grant_lab.ranking.freshness import enqueue_stale_pairs
from tests.freshness_helpers import ctx_for, make_call, make_profile

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


def _stamp(session, call, profile, ctx, **fields) -> CallRanking:
    """A ranking whose three stamps are CURRENT for (call, profile, ctx).
    `fields` override any column, stamps included (e.g. call_fingerprint=None)."""
    values = dict(
        call_id=call.id,
        profile_id=profile.id,
        aggregate_score=60,
        rationale="",
        model="m",
        prompt_version="v5.0",
        raw_response={},
        profile_fingerprint=rank_fingerprint(profile),
        call_fingerprint=call_rank_fingerprint(call),
        scoring_fingerprint=ctx.scoring.fingerprint(),
    )
    values.update(fields)
    cr = CallRanking(**values)
    session.add(cr)
    session.flush()
    return cr


def _pending(session) -> list[tuple[int, int]]:
    return sorted(
        (j.call_id, j.profile_id)
        for j in session.execute(
            select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING)
        ).scalars()
    )


def _failed_job(session, call, profile, *, days_ago: float, stamps=None) -> None:
    job = RankingJob(
        call_id=call.id,
        profile_id=profile.id,
        status=RankingJobStatus.FAILED,
        attempts=5,
        last_error="dead-lettered",
    )
    if stamps is not None:
        job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint = stamps
    session.add(job)
    session.flush()
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=days_ago))
    )


def _stamps(call, profile, ctx):
    return (rank_fingerprint(profile), call_rank_fingerprint(call), ctx.scoring.fingerprint())


# --- freshness -------------------------------------------------------------


def test_a_fresh_pair_is_not_enqueued_even_with_a_remote_configured(session):
    """G1 at the rule level, and the NULL trap (spec D4): escalation_state is
    NULL on a normal fresh row. `NOT (state = 'pending' AND ...)` is NULL there
    and would make every fresh pair stale."""
    ctx = ctx_for(remote_enabled=True)
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 0
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_a_missing_ranking_is_enqueued(session):
    """The population control for every '== 0' above."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1
    assert _pending(session) == [(c.id, p.id)]


def test_a_changed_summary_is_enqueued(session):
    """G2."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    c.summary = "Rewritten scope."
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_raw_change_outside_the_ranked_keys_is_not_enqueued(session):
    """G3."""
    ctx = ctx_for()
    p = make_profile(session)
    c = make_call(session, deadline=FUTURE, raw={"trl_min": 3, "metadata": {"t": 1}})
    _stamp(session, c, p, ctx)
    c.raw = {"trl_min": 3, "metadata": {"t": 2}}
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 0


def test_a_trl_change_is_enqueued(session):
    """G4."""
    ctx = ctx_for()
    p = make_profile(session)
    c = make_call(session, deadline=FUTURE, raw={"trl_min": 3})
    _stamp(session, c, p, ctx)
    c.raw = {"trl_min": 4}
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_an_open_call_published_long_ago_is_enqueued(session):
    """G6 (F3): openness, not publication date."""
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=FUTURE, published_at=datetime.now(UTC) - timedelta(days=120))
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


def test_a_closed_call_is_not_enqueued(session):
    """G7."""
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=datetime.now(UTC) - timedelta(days=1))
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_null_stamps_are_stale(session):
    """G13: every pre-0081 row has NULL call/scoring stamps."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx, call_fingerprint=None)
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_changed_call_vector_is_stale(session):
    """G15."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    c.embedding_fingerprint = "v2:" + "c" * 64
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_changed_scoring_setting_is_stale(session):
    """G24 at the rule level."""
    old, new = ctx_for(embedding_hard_fail_threshold=0.20), ctx_for(embedding_hard_fail_threshold=0.10)
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, old)
    assert enqueue_stale_pairs(session, ctx=old) == 0
    assert enqueue_stale_pairs(session, ctx=new) == 1


def test_a_prompt_version_bump_is_stale(session, monkeypatch):
    """G8 at the rule level."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    monkeypatch.setattr(F, "PROMPT_VERSION", "v999")
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


# --- escalation state (spec D6, enqueue half) --------------------------------


@pytest.mark.parametrize(
    "state, remote_enabled, expected",
    [
        ("pending", True, 1),   # G17: owed and possible -> due
        ("pending", False, 0),  # no key -> nothing to do (G21)
        ("gave_up", True, 0),   # G19: given up for this revision
        (None, True, 0),        # the NULL trap again, explicitly
    ],
)
def test_escalation_state_decides_whether_a_fresh_pair_is_due(session, state, remote_enabled, expected):
    ctx = ctx_for(remote_enabled=remote_enabled)
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx, escalation_state=state)
    assert enqueue_stale_pairs(session, ctx=ctx) == expected


# --- dead-letter suppression by input (spec D7) ------------------------------


def test_a_recent_failure_on_the_same_inputs_suppresses(session):
    """G11."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, stamps=_stamps(c, p, ctx))
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_a_recent_failure_on_other_inputs_does_not_suppress(session):
    """G22 (gap 2): the corrected call is retried at once."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, stamps=_stamps(c, p, ctx))
    c.summary = "Corrected text."
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


def test_a_legacy_failure_with_null_stamps_suppresses_pair_wide(session):
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, stamps=None)
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_an_old_failure_no_longer_suppresses(session):
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=F.DEAD_LETTER_SUPPRESS_DAYS + 1, stamps=_stamps(c, p, ctx))
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


# --- plumbing ----------------------------------------------------------------


def test_an_empty_call_list_inserts_nothing(session):
    make_profile(session)
    make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx_for(), call_ids=[]) == 0


def test_an_active_job_is_not_duplicated(session):
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx) == 1
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_the_narrowed_call_row_and_the_loaded_call_agree(session):
    """G27. Real values on every hashed field: with NULLs, _canonical drops the
    key from both payloads and the two agree about nothing."""
    c = make_call(
        session,
        deadline=FUTURE,
        raw={"trl_min": 3, "trl_max": 6, "metadata": {"big": "x" * 1000}},
        keywords=["ai", "health"],
        budget_min=1,
        budget_max=2,
        currency="EUR",
        programme="Digital Europe",
    )
    c.embedding_fingerprint = "v2:" + "d" * 64
    c.embedding_model = "bge-m3"
    c.embedding = [0.0] * 1024  # ck: embedding and embedding_model are paired
    session.commit()
    loaded = session.get(Call, c.id)
    (row,) = F._call_rows(session, [c.id])
    assert call_rank_fingerprint(row) == call_rank_fingerprint(loaded)
    assert "metadata" not in (row.raw or {}), "the select loaded raw whole"
```

- [ ] **Step 2: Run and watch them fail.**

```bash
cd $W && timeout 600 uv run pytest tests/test_ranking_freshness_enqueue.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: collection ERROR, `ImportError: cannot import name 'enqueue_stale_pairs'`.

- [ ] **Step 3: Implement.** Append to `src/grant_lab/ranking/freshness.py`:

```python
def _profile_rows(session: Session):
    """Active profiles, column-limited: rank_fingerprint never reads the vector.
    tests/test_ranking_queue_row_parity.py pins that these rows hash like
    loaded Profiles."""
    return session.execute(
        select(
            Profile.id,
            Profile.name,
            Profile.kind,
            Profile.description,
            Profile.structured,
            Profile.budget_currency,
            Profile.embedding_fingerprint,
            Profile.embedding_model,
        ).where(Profile.is_active.is_(True))
    ).all()


def _open_calls():
    return or_(Call.deadline.is_(None), Call.deadline > func.now())


def _call_rows(session: Session, call_ids: list[int] | None):
    """Open calls, column-limited: never Call.embedding, and never raw whole
    (13 MB across the open feed, spec D4). `raw` here is the RANKED_RAW_KEYS
    projection, so call_rank_fingerprint reads it exactly as it reads a loaded
    Call's raw. G27 pins that."""
    projection = func.jsonb_build_object(
        *[part for k in RANKED_RAW_KEYS for part in (literal(k, type_=Text), Call.raw[k])]
    ).label("raw")
    stmt = select(
        Call.id,
        Call.url,
        Call.title,
        Call.summary,
        Call.full_text,
        Call.deadline,
        Call.budget_min,
        Call.budget_max,
        Call.currency,
        Call.programme,
        Call.keywords,
        Call.published_at,
        Call.embedding_fingerprint,
        Call.embedding_model,
        projection,
    ).where(_open_calls())
    if call_ids is not None:
        stmt = stmt.where(Call.id.in_(call_ids))
    return session.execute(stmt).all()


def enqueue_stale_pairs(
    session: Session, *, ctx: FreshnessContext, call_ids: list[int] | None = None
) -> int:
    """Enqueue every stale, unsuppressed (open call x active profile) pair.

    `call_ids=None` means every open call (the 04:00 reconcile); a list limits
    the candidates (ingest). Returns the number of jobs inserted. Does not commit.

    Current (spec D4) = the profile arm (fingerprint, or the bounded 'legacy'
    arm) AND call_fingerprint AND scoring_fingerprint match, AND no escalation
    is owed while a remote client can pay it. Suppressed (spec D7) = a failed
    job in the last DEAD_LETTER_SUPPRESS_DAYS on the SAME inputs, or with NULL
    stamps (pre-0081 rows keep the old pair-wide rule).
    """
    if call_ids is not None and not call_ids:
        return 0
    profiles = _profile_rows(session)
    if not profiles:
        # An empty VALUES list is a syntax error.
        return 0
    calls = _call_rows(session, call_ids)
    if not calls:
        return 0

    values_sql = ", ".join(f"(:pid_{i}, :pfp_{i})" for i in range(len(profiles)))
    params: dict[str, object] = {
        "call_ids": [c.id for c in calls],
        "call_fps": [call_rank_fingerprint(c) for c in calls],
        "scoring_fp": ctx.scoring.fingerprint(),
        "remote_enabled": ctx.remote_enabled,
        "legacy": LEGACY_SENTINEL,
        "dead_letter_cutoff": datetime.now(UTC) - timedelta(days=DEAD_LETTER_SUPPRESS_DAYS),
    }
    for i, prof in enumerate(profiles):
        params[f"pid_{i}"] = prof.id
        params[f"pfp_{i}"] = rank_fingerprint(prof)

    result = session.execute(
        text(
            f"""
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT wc.call_id, p.id, 'pending'
            FROM unnest(CAST(:call_ids AS bigint[]), CAST(:call_fps AS text[]))
                 AS wc(call_id, fingerprint)
            CROSS JOIN (VALUES {values_sql}) AS want(profile_id, fingerprint)
            JOIN profiles p ON p.id = want.profile_id
            WHERE p.is_active = true
              AND NOT EXISTS (
                SELECT 1 FROM call_rankings cr
                WHERE cr.call_id = wc.call_id AND cr.profile_id = p.id
                  AND ((cr.profile_fingerprint = :legacy AND cr.ranked_at >= p.updated_at)
                       OR cr.profile_fingerprint = want.fingerprint)
                  AND cr.call_fingerprint = wc.fingerprint
                  AND cr.scoring_fingerprint = :scoring_fp
                  -- IS DISTINCT FROM, never `NOT (state = 'pending' AND ...)`:
                  -- a NULL state (the normal case) would make this whole test
                  -- NULL and every fresh pair stale (spec D4, "the NULL trap").
                  AND (cr.escalation_state IS DISTINCT FROM 'pending'
                       OR NOT CAST(:remote_enabled AS boolean))
              )
              AND NOT EXISTS (
                SELECT 1 FROM ranking_jobs rj
                WHERE rj.call_id = wc.call_id AND rj.profile_id = p.id
                  AND rj.status = 'failed'
                  AND rj.updated_at > :dead_letter_cutoff
                  AND (rj.profile_fingerprint IS NULL
                       OR (rj.profile_fingerprint = want.fingerprint
                           AND rj.call_fingerprint = wc.fingerprint
                           AND rj.scoring_fingerprint = :scoring_fp))
              )
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        params,
    )
    return result.rowcount or 0
```

Extend the module's imports to:

```python
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, func, literal, or_, select, text
from sqlalchemy.orm import Session

from grant_lab.db.models import Call, Profile
from grant_lab.profiles.fingerprint import (
    LEGACY_SENTINEL,
    RANKED_RAW_KEYS,
    _digest,
    call_rank_fingerprint,
    rank_fingerprint,
)
from grant_lab.ranking.prompt import PROMPT_VERSION
```

- [ ] **Step 4: Retarget the profile row-parity gate.** In
  `tests/test_ranking_queue_row_parity.py`, the column list moves out of
  `reconcile_missing_rankings`. Replace the three lines from
  `src = inspect.getsource(Q.reconcile_missing_rankings)` through
  `assert cols, "could not read queue.py's column list"`, and the `row = …` statement
  after them, with:

```python
    # The rows reconcile/ingest actually hash, from the function that builds them.
    rows = [r for r in F._profile_rows(session) if r.id == p.id]
    assert len(rows) == 1, "the active-profile select did not return the seeded profile"
    row = rows[0]
```

  Change the module imports: drop `import inspect`, `import re`,
  `from sqlalchemy import select`, and `from grant_lab.ranking import queue as Q`. Add
  `from grant_lab.ranking import freshness as F`. Update the final assertion message
  from `queue.py's narrowed SELECT {cols}` to `freshness._profile_rows`. Keep everything
  else, including the docstring's reason for setting the embedding columns. Update the
  module docstring's first line to name `ranking/freshness.py::_profile_rows`.

- [ ] **Step 5: Run.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_freshness_enqueue.py tests/test_ranking_queue_row_parity.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 6: Prove the rule bites in both directions.**
  1. **The NULL trap.** Anchor `AND (cr.escalation_state IS DISTINCT FROM 'pending'\n                       OR NOT CAST(:remote_enabled AS boolean))`.
     Mutant `AND NOT (cr.escalation_state = 'pending' AND CAST(:remote_enabled AS boolean))`.
     Expected: `test_a_fresh_pair_is_not_enqueued_even_with_a_remote_configured` and
     `test_escalation_state_decides_whether_a_fresh_pair_is_due[None-True-0]` FAILED.
     Restore.
  2. Anchor `                  AND cr.call_fingerprint = wc.fingerprint\n`, mutant: delete it.
     Expected: `test_a_changed_summary_is_enqueued`, `test_a_trl_change_is_enqueued`,
     `test_a_changed_call_vector_is_stale` and `test_null_stamps_are_stale` FAILED.
     Restore.
  3. Anchor `                  AND rj.updated_at > :dead_letter_cutoff\n                  AND (rj.profile_fingerprint IS NULL`.
     Mutant `                  AND rj.updated_at > :dead_letter_cutoff\n                  AND (true`.
     Expected: `test_a_recent_failure_on_other_inputs_does_not_suppress` FAILED. Restore.
  4. In `_call_rows`, replace `projection,` with `Call.raw,`. Expected:
     `test_the_narrowed_call_row_and_the_loaded_call_agree` FAILED on its `metadata`
     assertion. Restore.

- [ ] **Step 7: Commit.**

```bash
cd $W && uv run ruff check src/grant_lab/ranking/freshness.py tests/test_ranking_freshness_enqueue.py tests/test_ranking_queue_row_parity.py && uv run ruff format src/grant_lab/ranking/freshness.py tests/test_ranking_freshness_enqueue.py tests/test_ranking_queue_row_parity.py
git -C $W add src/grant_lab/ranking/freshness.py tests/test_ranking_freshness_enqueue.py tests/test_ranking_queue_row_parity.py
git -C $W commit -m "feat(ranking): enqueue a pair only when its inputs changed or its second reading is owed" -m "One freshness rule over profile, call and scoring stamps, the open-call window, and dead-letter suppression keyed on the failed inputs (spec D4, D6, D7). Not wired in yet." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 5: `rank_pair` — timeline on expiry only, and `escalation_wanted`

**Files:**
- Modify: `src/grant_lab/ranking/criteria.py` (`TIMELINE_MIN_DAYS`, `_timeline`, the `GateResult.reason` comment)
- Modify: `src/grant_lab/ranking/aggregate.py` (`aggregate`)
- Modify: `src/grant_lab/ranking/rank_pair.py` (`RankResult`, `rank_pair`, `_local_with_failed_escalation`)
- Modify: `tests/test_ranking_criteria.py`, `tests/test_ranking_aggregate.py`, `tests/test_ranking_rank_pair.py`

**Interfaces:**
- Produces: `RankResult.escalation_wanted: bool = False`, meaning the local outcome
  qualifies for escalation, whether or not a remote client ran. Timeline gate reasons
  are only `'expired'` or `None`.

- [ ] **Step 1: Rewrite the reversed tests, then add the new ones.**

In `tests/test_ranking_criteria.py`, replace `test_timeline_fails_when_deadline_is_13_days_away` with:

```python
def test_timeline_passes_when_deadline_is_13_days_away():
    """Spec §2: urgency is not suitability. The 14-day zeroing is gone."""
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(days=13)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS
    assert g.timeline.reason is None


def test_timeline_passes_one_minute_before_the_deadline():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(minutes=1)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


def test_timeline_fails_exactly_at_the_deadline():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW), now=_NOW)
    assert g.timeline.status == GateStatus.FAIL
    assert g.timeline.reason == "expired"
```

In `tests/test_ranking_aggregate.py`:
- In `test_aggregate_timeline_fail_short_circuits`, change `reason="too_soon"` to
  `reason="expired"`.
- Replace `test_aggregate_timeline_fail_ignored_with_must_include_override` with:

```python
def test_aggregate_timeline_fail_is_not_bypassed_by_must_include():
    """Spec §2 / D8: a closed call has nothing to rescue. must_include still
    bypasses the EMBEDDING gate only."""
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="expired"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5), must_include_override=True)
    assert r.score == 0
    assert r.reason == "timeline"
```

In `tests/test_ranking_rank_pair.py`, replace `test_rank_pair_must_include_bypasses_timeline_gate` with:

```python
def test_rank_pair_scores_a_call_closing_tomorrow():
    """G12: deadline tomorrow is a real score, not a timeline zero."""
    from datetime import timedelta

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(title="Pathology funding", summary="x", deadline=_NOW + timedelta(days=1))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 80
    assert r.reason is None
    assert r.gates.timeline.status.value == "pass"


def test_rank_pair_expired_call_fails_even_with_must_include():
    """G12: must_include no longer rescues a closed call, and the LLM never runs."""
    from datetime import timedelta

    profile = _Profile(
        structured={"areas": [], "hard_exclusions": [], "must_include": ["pathology"]}
    )
    call = _Call(title="Pathology funding", summary="x", deadline=_NOW - timedelta(days=1))
    client = _ollama_mock(content={"topic_fit": {"rating": 5, "rationale": "x"},
                                   "eligibility": {"rating": "pass"},
                                   "exclusions": {"rating": "pass"}})
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.parsed is None
    assert r.must_include_override is True
```

Append to `tests/test_ranking_rank_pair.py`:

```python
def _content(topic, elig="pass"):
    return {
        "topic_fit": {"rating": topic, "rationale": "x"},
        "eligibility": {"rating": elig},
        "exclusions": {"rating": "pass"},
    }


def test_escalation_is_wanted_without_a_remote_client():
    """Spec D6: `escalation_wanted` is the local outcome, not remote availability.
    Without it the worker cannot tell a cap-withheld escalation from none."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(client=_ollama_mock(content=_content(4)), profile=profile, call=call,
                  now=_NOW, remote_client=None, escalation_min_score=45)
    assert r.score == 80
    assert r.escalation_wanted is True
    assert r.escalation_attempted is False


def test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(client=_ollama_mock(content=_content(3, "fail")), profile=profile,
                  call=call, now=_NOW, remote_client=None, escalation_min_score=45)
    assert r.score == 0
    assert r.escalation_wanted is True


def test_escalation_is_not_wanted_below_the_threshold():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(client=_ollama_mock(content=_content(2)), profile=profile, call=call,
                  now=_NOW, remote_client=None, escalation_min_score=45)
    assert r.score == 40
    assert r.escalation_wanted is False


def test_a_successful_escalation_is_also_wanted():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(client=_ollama_mock(content=_content(4)), profile=profile, call=call,
                  now=_NOW, remote_client=_remote_mock(), escalation_min_score=45)
    assert r.escalated is True
    assert r.escalation_wanted is True


def test_a_failed_escalation_is_still_wanted():
    from grant_lab.ranking.ollama_client import LLMError

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    remote = _remote_mock()
    remote.chat.side_effect = LLMError("credit")
    r = rank_pair(client=_ollama_mock(content=_content(4)), profile=profile, call=call,
                  now=_NOW, remote_client=remote, escalation_min_score=45)
    assert r.escalation_outcome == "llm_error"
    assert r.escalation_wanted is True


def test_lowering_the_hard_fail_threshold_moves_a_pair_from_zero_to_scored():
    """G24, the reviewer's probe as a test. Same pair, same model output; only
    the hard-fail threshold moves (0.20 -> 0.10), and the score moves from a
    gate-fail 0 to an LLM score. No call or profile fingerprint input changed,
    which is why the scoring fingerprint exists (spec gap 3)."""
    import math

    p_vec = [1.0] + [0.0] * 1023
    c_vec = [0.15, math.sqrt(1 - 0.15**2)] + [0.0] * 1022  # cosine 0.15
    profile = _make_profile(embedding=p_vec, embedding_model="bge-m3")
    call = _make_call(embedding=c_vec, embedding_model="bge-m3", deadline_days=400)
    client = _ollama_mock(content=_content(4))
    strict = rank_pair(client=client, profile=profile, call=call, now=_NOW,
                       embedding_hard_fail_threshold=0.20)
    loose = rank_pair(client=client, profile=profile, call=call, now=_NOW,
                      embedding_hard_fail_threshold=0.10)
    assert (strict.score, strict.reason) == (0, "embedding")
    assert (loose.score, loose.reason) == (80, None)
```

- [ ] **Step 2: Run and watch them fail.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_criteria.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py -q -p no:cacheprovider 2>&1 | tail -12
```

Expected FAILED, not ERROR:
- `test_timeline_passes_when_deadline_is_13_days_away`
- `test_aggregate_timeline_fail_is_not_bypassed_by_must_include`
- `test_rank_pair_scores_a_call_closing_tomorrow`
- `test_rank_pair_expired_call_fails_even_with_must_include`
- the five `escalation_wanted` tests (`AttributeError` inside the test body counts as FAILED)

- [ ] **Step 3: Implement.**

`src/grant_lab/ranking/criteria.py`: delete `TIMELINE_MIN_DAYS = 14`. Replace `_timeline`
with the version below, and remove `timedelta` from the `datetime` import (it becomes
unused):

```python
def _timeline(deadline: datetime | None, now: datetime) -> GateResult:
    """FAIL only once the call has closed (ranking spec §2).

    Urgency is not suitability: a call closing in five days is scored like any
    other, and the dashboard's live deadline colour carries the urgency. A
    clock-dependent verdict also cannot survive re-ranking on input change --
    nothing would revisit a pair when it crossed a day threshold.
    """
    if deadline is None or deadline > now:
        return GateResult(GateStatus.PASS)
    return GateResult(GateStatus.FAIL, reason="expired")
```

In `GateResult`, change the comment to
`# 'expired' | 'currency_mismatch' | 'topic_dissimilar' | 'topic_borderline' | None`.
Update the module docstring's "Four deterministic checks" paragraph only if it mentions
14 days. Check with `grep -n "14" src/grant_lab/ranking/criteria.py`, which should
print nothing after the edit.

`src/grant_lab/ranking/aggregate.py`: replace
`    if not must_include_override and gates.timeline.status == GateStatus.FAIL:` with
`    if gates.timeline.status == GateStatus.FAIL:`. In the docstring, replace the sentence
beginning `` `must_include_override=True` skips the timeline AND embedding short-circuits ``
with:

```
    `must_include_override=True` skips the embedding short-circuit only (a
    profile keyword forces the TOPIC in). The timeline gate now fails only on a
    closed call, and nothing rescues a closed call (ranking spec §2).
```

`src/grant_lab/ranking/rank_pair.py`:
1. In `RankResult`, after `analysis_model: str | None = None`, add:

```python
    # The local outcome qualifies for a second reading -- score >=
    # escalation_min_score, or eligibility 'fail' with topic >= 3 -- whether or
    # not a remote client was available (ranking spec D6). The worker uses it to
    # tell "owed" from "not needed".
    escalation_wanted: bool = False
```

2. Replace
   `    if gates.timeline.status == GateStatus.FAIL and not must_include_match:` with
   `    if gates.timeline.status == GateStatus.FAIL:`, and update its comment line to
   `    # Timeline FAIL (a closed call) short-circuits unconditionally (ranking spec §2).`
3. In the docstring's "Rule-based-gate handling" bullets, replace the Timeline bullet
   with `- Timeline FAIL (the call has closed) → short-circuit, no LLM call.` and add
   `- Embedding FAIL → short-circuit UNLESS the call matches a must_include keyword.`
4. Replace the `should_escalate = …` statement with:

```python
    escalation_wanted = local_agg.score >= escalation_min_score or (
        local_parsed.eligibility_rating == "fail" and local_parsed.topic_fit_rating >= 3
    )
    should_escalate = remote_client is not None and escalation_wanted
```

5. In the `if not should_escalate:` return, add `escalation_wanted=escalation_wanted,`.
6. In the remote-success return, add `escalation_wanted=True,`.
7. `_local_with_failed_escalation`: add a keyword parameter `escalation_wanted: bool`
   after `outcome: str`, pass `escalation_wanted=escalation_wanted` into its
   `RankResult(...)`, and pass `escalation_wanted=escalation_wanted` at its one call site.

- [ ] **Step 4: Run.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_criteria.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py tests/test_call_rank_fingerprint.py -q -p no:cacheprovider 2>&1 | tail -5
```

Expected: all PASS. `tests/test_ranking_worker.py` goes red here on the two timeline tests
Task 6 rewrites (`test_worker_must_include_with_failing_timeline_persists_llm_score`,
`test_worker_must_include_override_appears_in_summary`). That is expected. Do not
commit a fix for them in this task.

- [ ] **Step 5: Prove the reversal bites.** Two replacements in one mutant script, both
  asserted `count == 1` first: `from datetime import datetime\n` →
  `from datetime import datetime, timedelta\n`, and
  `    if deadline is None or deadline > now:` →
  `    if deadline is None or deadline > now + timedelta(days=14):`. Expected:
  `test_timeline_passes_when_deadline_is_13_days_away` and
  `test_rank_pair_scores_a_call_closing_tomorrow` FAILED. Restore.

- [ ] **Step 6: Commit.**

```bash
cd $W && uv run ruff check src/grant_lab/ranking/criteria.py src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/rank_pair.py tests/test_ranking_criteria.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py && uv run ruff format src/grant_lab/ranking/criteria.py src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/rank_pair.py tests/test_ranking_criteria.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py
git -C $W add src/grant_lab/ranking/criteria.py src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/rank_pair.py tests/test_ranking_criteria.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py
git -C $W commit -m "fix(ranking): score calls that close within 14 days instead of zeroing them" -m "The timeline gate fails only on a closed call, and must_include no longer bypasses it (spec §2, D8). rank_pair reports escalation_wanted independently of remote availability (spec D6)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 6: The worker — stamps, escalation state, advice, criteria rows

**Files:**
- Modify: `src/grant_lab/ranking/queue.py` (`mark_failed` gains `stamps`)
- Modify: `src/grant_lab/ranking/worker.py` (`drain_queue`, `_rank_summary`, `_write_criteria_rows`)
- Modify: `src/grant_lab/web/call_detail_view.py` (the timeline text)
- Modify: `tests/test_ranking_worker.py` (two reversed tests)
- Modify: `tests/architecture/test_criterion_vocabulary.py` (four gates → three; seven → six)
- Create: `tests/test_ranking_escalation_state.py`
- Modify: `tests/test_ranking_worker_fingerprint.py` (call-side and scoring-side advice gates)

**Interfaces:**
- Consumes: `call_rank_fingerprint` (Task 2), `ScoringConfig`, `next_escalation_state`
  (Task 3), `RankResult.escalation_wanted` (Task 5).
- Produces: `mark_failed(session, job_id, *, error, terminal, stamps: tuple[str, str, str] | None = None)`.
  Every `CallRanking` the worker writes carries `call_fingerprint`,
  `scoring_fingerprint`, `escalation_state` and `escalation_failures`.

- [ ] **Step 1: Write the failing tests.**

`tests/test_ranking_escalation_state.py`:

```python
"""Spec D6 at the worker, and D7's stamps. Gates G17-G21."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking import worker as worker_mod
from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
from grant_lab.ranking.prompt import Parsed, ParseError
from grant_lab.ranking.queue import enqueue_for_pair
from grant_lab.ranking.rank_pair import RankResult
from grant_lab.ranking.remote_attempts import record_attempt
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import make_call, make_profile, ollama_clean, scoring_for


class _Remote:
    model = "claude-sonnet-5"


def _stub(*, wanted=True, escalated=False, attempted=False, outcome=None, score=60):
    parsed = Parsed(
        topic_fit_rating=3, topic_fit_rationale="ok", topic_fit_quoted=None,
        eligibility_rating="pass", eligibility_rationale="ok", eligibility_quoted=None,
        exclusions_rating="pass", exclusions_rationale="ok", exclusions_quoted=None,
    )
    return RankResult(
        score=score, reason=None,
        gates=Gates(timeline=GateResult(GateStatus.PASS), budget=GateResult(GateStatus.PASS),
                    trl=GateResult(GateStatus.PASS), embedding=GateResult(GateStatus.PASS)),
        parsed=parsed, must_include_override=False, model="m", prompt_version="v5.0",
        raw_response={}, embed_similarity=None,
        escalated=escalated, escalated_from_score=score if escalated else None,
        escalation_attempted=attempted, escalation_outcome=outcome,
        escalation_wanted=wanted,
    )


@pytest.fixture()
def pair(pg_session_factory):
    with pg_session_factory() as s:
        p, c = make_profile(s), make_call(s)
        enqueue_for_pair(s, call_id=c.id, profile_id=p.id)
        s.commit()
        return c.id, p.id


def _drain(sf, monkeypatch, stub, *, remote=_Remote, cap=150):
    monkeypatch.setattr(worker_mod, "rank_pair", lambda **kw: stub)
    drain_queue(session_factory=sf, client=None, embedding_threshold=0.0,
                remote_client=remote() if remote else None, remote_daily_cap=cap,
                batch_size=10)


def _row(sf, pair) -> CallRanking:
    with sf() as s:
        return s.get(CallRanking, pair)


def _requeue(sf, pair):
    with sf() as s:
        enqueue_for_pair(s, call_id=pair[0], profile_id=pair[1])
        s.commit()


def test_a_cap_withheld_escalation_is_pending_then_completes(pg_session_factory, monkeypatch, pair):
    """G17."""
    with pg_session_factory() as s:
        record_attempt(s, call_id=pair[0], profile_id=pair[1], model="m", outcome="success")
        s.commit()
    _drain(pg_session_factory, monkeypatch, _stub(), cap=1)
    assert (_row(pg_session_factory, pair).escalation_state, _row(pg_session_factory, pair).escalation_failures) == ("pending", 0)

    _requeue(pg_session_factory, pair)
    _drain(pg_session_factory, monkeypatch, _stub(escalated=True, attempted=True, outcome="success"), cap=150)
    assert _row(pg_session_factory, pair).escalation_state is None


def test_llm_errors_never_exhaust_the_pair(pg_session_factory, monkeypatch, pair):
    """G18."""
    for _ in range(5):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="llm_error"))
        _requeue(pg_session_factory, pair)
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == ("pending", 0)


def test_three_parse_errors_in_one_revision_give_up(pg_session_factory, monkeypatch, pair):
    """G19."""
    seen = []
    for _ in range(3):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
        r = _row(pg_session_factory, pair)
        seen.append((r.escalation_state, r.escalation_failures))
        _requeue(pg_session_factory, pair)
    assert seen == [("pending", 1), ("pending", 2), ("gave_up", 3)]


def test_a_new_revision_resets_the_count(pg_session_factory, monkeypatch, pair):
    """G20."""
    for _ in range(3):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
        _requeue(pg_session_factory, pair)
    assert _row(pg_session_factory, pair).escalation_state == "gave_up"
    with pg_session_factory() as s:
        s.get(Call, pair[0]).summary = "A corrected call text."
        s.commit()
    _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == ("pending", 1)


def test_no_remote_configured_owes_nothing(pg_session_factory, monkeypatch, pair):
    """G21."""
    _drain(pg_session_factory, monkeypatch, _stub(), remote=None)
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == (None, 0)


def test_the_worker_stamps_all_three_fingerprints(pg_session_factory, pair):
    drain_queue(session_factory=pg_session_factory, client=ollama_clean(),
                embedding_threshold=0.0, batch_size=10)
    with pg_session_factory() as s:
        cr = s.get(CallRanking, pair)
        call = s.get(Call, pair[0])
        assert cr.call_fingerprint == call_rank_fingerprint(call)
        assert cr.scoring_fingerprint == scoring_for(ollama_clean()).fingerprint()
        assert cr.profile_fingerprint is not None


def test_a_terminal_failure_is_stamped_with_its_inputs(pg_session_factory, monkeypatch, pair):
    """Spec D7: suppression needs to know WHAT failed."""
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        job.attempts = 4  # MAX_ATTEMPTS is 5: the next failure is terminal
        s.commit()

    def _raise(**kw):
        raise ParseError("exclusions missing or not an object")

    monkeypatch.setattr(worker_mod, "rank_pair", _raise)
    drain_queue(session_factory=pg_session_factory, client=None, embedding_threshold=0.0,
                batch_size=10)
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        call = s.get(Call, pair[0])
        from grant_lab.db.models import Profile
        prof = s.get(Profile, pair[1])
        assert job.status == RankingJobStatus.FAILED
        assert (job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint) == (
            rank_fingerprint(prof),
            call_rank_fingerprint(call),
            scoring_for(None).fingerprint(),
        )
```

Append to `tests/test_ranking_worker_fingerprint.py`:

```python
def test_retained_analysis_is_dropped_when_the_call_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """G9 (Astra #3): advice about the old call text must not survive a call edit."""
    from grant_lab.db.models import Call

    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary, cr.applicant_notes, cr.analysis_model = "Old advice", [{"note": "old"}], "claude-sonnet-5"
    session.commit()
    with session_factory() as other:
        other.get(Call, call_id).summary = "The funder rewrote the scope."
        other.commit()
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(session_factory=session_factory, client=fake_rank_client,
                embedding_threshold=0.0, batch_size=1)
    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert (cr.summary, cr.applicant_notes, cr.analysis_model) == (None, None, None)


def test_retained_analysis_is_dropped_when_the_scoring_settings_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """G9, the scoring side."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary, cr.applicant_notes, cr.analysis_model = "Old advice", [{"note": "old"}], "claude-sonnet-5"
    session.commit()
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(session_factory=session_factory, client=fake_rank_client,
                embedding_threshold=0.05, batch_size=1)
    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary is None
```

The existing `test_retained_analysis_survives_when_the_profile_did_not_change` is G10's
control. It must stay green: its two drains use identical scoring parameters, so all
three stamps match.

In `tests/test_ranking_worker.py`, replace
`test_worker_must_include_with_failing_timeline_persists_llm_score` and
`test_worker_must_include_override_appears_in_summary` with:

```python
def test_worker_scores_a_call_closing_tomorrow_and_writes_no_timeline_row(pg_session_factory):
    """G12 at the worker: a real score, and no timeline criterion row (spec D8)."""
    from datetime import timedelta

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.get(Call, cid).deadline = datetime.now(UTC) + timedelta(days=1)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        names = {c.criterion for c in s.execute(select(CallRankingCriterion)).scalars()}
    assert ranking.aggregate_score >= 70
    assert "timeline" not in names
    assert {"budget", "trl", "embedding", "topic_fit", "eligibility", "exclusions"} <= names


def test_worker_expired_call_scores_zero_even_with_must_include(pg_session_factory):
    """G12: must_include no longer rescues a closed call."""
    from datetime import timedelta

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.get(Profile, pid).structured = {"areas": ["AI"], "hard_exclusions": [],
                                          "must_include": ["funding"]}
        s.get(Call, cid).deadline = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert (ranking.aggregate_score, ranking.aggregate_reason) == (0, "timeline")
```

Also search the same file for any other test that expects a `timeline` criterion row.
Run `grep -n '"timeline"' tests/test_ranking_worker.py`. `test_worker_writes_sparse_criterion_rows_clean_path`
and `test_worker_short_circuit_writes_up_to_failing_gate_row` are the likely ones. For
each hit, remove `timeline` from the expected set of criterion names, and change
nothing else. Record every test you changed in your report.

In `tests/architecture/test_criterion_vocabulary.py`:
- `test_the_gate_tuple_scan_finds_the_four_gates`: rename to
  `test_the_gate_tuple_scan_finds_the_three_gates` and expect
  `{"budget", "trl", "embedding"}`.
- `test_neither_criterion_scan_alone_is_the_whole_vocabulary`: `== 7` → `== 6`.
- Leave `test_the_label_map_scan_finds_the_seven_labels` at 7. `timeline` stays in
  `web/filters.py::_CRITERION_LABELS` because `aggregate_reason='timeline'` still exists
  (spec D8).
- Leave `test_the_frontend_criteria_scan_finds_a_real_list` at 7. Task 8 changes it,
  together with the frontend list.

- [ ] **Step 2: Run and watch them fail.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_escalation_state.py tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/architecture/test_criterion_vocabulary.py -q -p no:cacheprovider 2>&1 | tail -15
```

Expected FAILED (not ERROR):
- every test in `test_ranking_escalation_state.py`
- the two new `test_ranking_worker_fingerprint.py` tests
- `test_worker_scores_a_call_closing_tomorrow_and_writes_no_timeline_row`
- `test_the_gate_tuple_scan_finds_the_three_gates`
- `test_neither_criterion_scan_alone_is_the_whole_vocabulary`

- [ ] **Step 3: Implement `mark_failed` stamps.** In `src/grant_lab/ranking/queue.py`,
  replace `mark_failed` with:

```python
def mark_failed(
    session: Session,
    job_id: int,
    *,
    error: str,
    terminal: bool,
    stamps: tuple[str, str, str] | None = None,
) -> None:
    """Mark a job failed. If terminal=True, transition to the FAILED
    terminal status (dead-letter). Otherwise revert to PENDING with
    incremented attempts so the queue retries it on the next tick.

    `stamps` = (profile, call, scoring) fingerprints the job failed ON. Dead-
    letter suppression (ranking/freshness.py, spec D7) compares them with the
    pair's current inputs, so a corrected call is retried at once. None when
    the failure happened before they were captured.

    `error` is truncated to `_LAST_ERROR_MAX` chars so a stray traceback
    doesn't blow up row size (the column is unbounded TEXT)."""
    new_status = RankingJobStatus.FAILED if terminal else RankingJobStatus.PENDING
    values: dict[str, object] = {
        "status": new_status,
        "attempts": RankingJob.attempts + 1,
        "last_error": (error or "")[:_LAST_ERROR_MAX],
        "updated_at": datetime.now(UTC),
    }
    if stamps is not None:
        values["profile_fingerprint"], values["call_fingerprint"], values["scoring_fingerprint"] = stamps
    session.execute(update(RankingJob).where(RankingJob.id == job_id).values(**values))
```

- [ ] **Step 4: Implement the worker.** In `src/grant_lab/ranking/worker.py`:

1. Imports: change the fingerprint import to
   `from grant_lab.profiles.fingerprint import call_rank_fingerprint, embedding_fingerprint, rank_fingerprint`
   and add `from grant_lab.ranking.freshness import ScoringConfig, next_escalation_state`.
2. Directly after `tick_start = _clock()`, add:

```python
    # The scorer's settings, hashed once per tick (ranking spec D3). Built from
    # THIS function's parameters, never from Settings, so the stamp says what
    # this worker actually ran with. scheduler.py hands ingest and reconcile a
    # ScoringConfig built from the same values (gate G26).
    captured_scoring = ScoringConfig.for_drain(
        client=client,
        embedding_threshold=embedding_threshold,
        embedding_hard_fail_threshold=embedding_hard_fail_threshold,
        escalation_min_score=escalation_min_score,
        logprobs_enabled=logprobs_enabled,
    ).fingerprint()
```

3. Directly after `captured_fingerprint = rank_fingerprint(profile)`, add:

```python
                captured_call_fingerprint = call_rank_fingerprint(call)
                stamps = (captured_fingerprint, captured_call_fingerprint, captured_scoring)
```

4. In the `except (LLMError, ParseError) as e:` block right after the `rank_pair(...)`
   call, change the `mark_failed(...)` call to pass `stamps=stamps` as well.
5. Replace the whole preservation block. It runs from the comment line
   `# Preserve a previously-recorded analysis when this run` down to and including
   `new_analysis_model = existing.analysis_model`, and the block is replaced with:

```python
                # One input revision = all three stamps equal (ranking spec D9).
                # Advice and escalation failures carry over only within one.
                existing = session.get(CallRanking, (call_id, profile_id))
                same_revision = (
                    existing is not None
                    and existing.profile_fingerprint == captured_fingerprint
                    and existing.call_fingerprint == captured_call_fingerprint
                    and existing.scoring_fingerprint == captured_scoring
                )

                # Preserve a previously-recorded analysis when this run produced
                # none (a local-only run, or a gate-fail re-rank), and only when it
                # still describes THESE inputs. The analysis is ONE attributable
                # unit (summary + applicant_notes + analysis_model): restore the
                # triple together, never graft one run's summary onto another's
                # notes. Advice about an old call text or an old profile must not
                # be stamped as current (Astra #3).
                new_summary = result.summary
                new_notes = result.applicant_notes
                new_analysis_model = result.analysis_model
                if new_summary is None and new_notes is None and same_revision:
                    new_summary = existing.summary
                    new_notes = existing.applicant_notes
                    new_analysis_model = existing.analysis_model

                escalation_state, escalation_failures = next_escalation_state(
                    wanted=result.escalation_wanted,
                    escalated=result.escalated,
                    remote_configured=remote_client is not None,
                    remote_withheld=remote_client is not None and effective_remote is None,
                    outcome=result.escalation_outcome,
                    prev_failures=existing.escalation_failures if same_revision else 0,
                )
```

6. In the `session.merge(CallRanking(...))`, after `profile_fingerprint=captured_fingerprint,`,
   add:

```python
                        call_fingerprint=captured_call_fingerprint,
                        scoring_fingerprint=captured_scoring,
                        escalation_state=escalation_state,
                        escalation_failures=escalation_failures,
```

7. `_rank_summary`: delete the two lines starting
   `    if r.must_include_override and r.gates.timeline.status == GateStatus.FAIL:`. A
   timeline FAIL now always short-circuits, so that branch is dead. Remove the second
   docstring paragraph ("When must_include overrode a failing timeline gate…").
8. `_write_criteria_rows`: in `gate_results`, delete the line
   `        ("timeline", r.gates.timeline),`. Replace
   `            if name in ("timeline", "embedding") and r.must_include_override:` with
   `            if name == "embedding" and r.must_include_override:`. Update the two comments
   that say `budget → trl → timeline → embedding` to
   `budget → trl → embedding (the timeline gate is not written as a row: on an open call it
   could only say "pass"; ranking spec D8)`. Update the docstring's "timeline OR
   embedding" to "embedding".
9. If `GateStatus` is no longer used anywhere in `worker.py` after step 7, remove its
   import. Check with `grep -n GateStatus src/grant_lab/ranking/worker.py`.

- [ ] **Step 5: The call page text.** In `src/grant_lab/web/call_detail_view.py`, replace
  `return f"Deadline {call.deadline.date()} — {days} day(s) away (minimum 14 day window)."` with
  `return f"Deadline {call.deadline.date()} — {days} day(s) away."`

- [ ] **Step 6: Run.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_escalation_state.py tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/test_ranking_worker_defers_stale_vector.py tests/test_ranking_queue.py tests/architecture/test_criterion_vocabulary.py -q -p no:cacheprovider 2>&1 | tail -6
```

Expected: all PASS, except the `tests/test_ranking_queue.py` reconcile tests. They are
still on the old signature and old window until Task 7, so leave them. Only
`mark_failed` changed in `queue.py` here.

- [ ] **Step 7: Prove the worker gates bite.**
  1. Anchor `                    and existing.call_fingerprint == captured_call_fingerprint\n`,
     mutant: delete it. Expected: `test_retained_analysis_is_dropped_when_the_call_changed`
     and `test_a_new_revision_resets_the_count` FAILED. Restore.
  2. In `freshness.next_escalation_state`, anchor `    if outcome == "parse_error":`, mutant
     `    if outcome in ("parse_error", "llm_error"):`. Expected:
     `test_llm_errors_never_exhaust_the_pair` and the `llm_error` row of
     `test_next_escalation_state` FAILED. Restore.
  3. Anchor `                    remote_configured=remote_client is not None,`, mutant
     `                    remote_configured=True,`. Expected:
     `test_no_remote_configured_owes_nothing` FAILED. Restore.

- [ ] **Step 8: Commit.**

```bash
cd $W && uv run ruff check src/grant_lab/ranking/queue.py src/grant_lab/ranking/worker.py src/grant_lab/web/call_detail_view.py tests/test_ranking_escalation_state.py tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/architecture/test_criterion_vocabulary.py && uv run ruff format src/grant_lab/ranking/queue.py src/grant_lab/ranking/worker.py src/grant_lab/web/call_detail_view.py tests/test_ranking_escalation_state.py tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/architecture/test_criterion_vocabulary.py
cd $W && uv run python scripts/gen_architecture.py && git -C $W status --short docs/ARCHITECTURE.md
git -C $W add src/grant_lab/ranking/queue.py src/grant_lab/ranking/worker.py src/grant_lab/web/call_detail_view.py tests/test_ranking_escalation_state.py tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/architecture/test_criterion_vocabulary.py docs/ARCHITECTURE.md
git -C $W commit -m "feat(ranking): record whether a ranking's second reading is still owed" -m "The worker stamps call and scoring fingerprints, keeps advice only within one input revision, tracks escalation_state (pending on a cap or llm_error, gave_up after 3 parse errors), and stamps a failed job with its inputs (spec D6, D7, D9). No timeline criterion row is written (spec D8)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Wire it in — ingest, reconcile, backfill, scheduler, admin

**Files:**
- Modify: `src/grant_lab/ranking/queue.py` (`bulk_enqueue_for_calls`, `reconcile_missing_rankings`; delete `_RECONCILE_DAYS`, `_DEAD_LETTER_SUPPRESS_DAYS`)
- Modify: `src/grant_lab/ranking/backfill.py`
- Modify: `src/grant_lab/ingestion/orchestrator.py` (`run_ingestion`)
- Modify: `src/grant_lab/ingestion/cli.py`
- Modify: `src/grant_lab/scheduler.py` (`build_scheduler`)
- Modify: `src/grant_lab/web/api/admin.py` (`AdminDashboardOut`, the dashboard handler)
- Modify tests: `tests/test_ranking_queue.py`, `tests/test_ranking_reconcile_staleness.py`, `tests/test_ranking_backfill.py`, `tests/test_api_admin_dashboard.py`, `tests/test_api_admin_ops.py`, and every file that calls `run_ingestion(` (`tests/test_ingestion_enqueues_rankings.py`, `tests/test_ingestion_orchestrator.py`, `tests/test_ingestion_orchestrator_embeds.py`, `tests/test_ingestion_e2e.py`, `tests/test_ranking_e2e.py`)
- Create: `tests/test_ranking_freshness_wiring.py`

**Interfaces:**
- Consumes: `enqueue_stale_pairs`, `FreshnessContext`, `ScoringConfig` (Tasks 3–4).
- Produces:
  - `bulk_enqueue_for_calls(session, call_ids, *, ctx: FreshnessContext) -> int`
  - `reconcile_missing_rankings(session, *, ctx: FreshnessContext) -> int`
  - `run_ingestion(*, session_factory, adapter, alerter, embed_client, freshness: FreshnessContext)`
  - `backfill_profile(*, session_factory, profile_id) -> int`: every open call for the
    profile; no freshness, no suppression.
  - `AdminDashboardOut.open_calls_count: int` replaces `recent_calls_count` and
    `backfill_days`.

- [ ] **Step 1: Write the failing wiring tests.**

`tests/test_ranking_freshness_wiring.py`:

```python
"""The rule, wired in. Gates G1 (end to end), G6, G23, G26."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

import grant_lab.scheduler as scheduler_mod
from grant_lab.auth.email import InMemoryEmailSender
from grant_lab.config import Settings
from grant_lab.db.models import RankingJob, RankingJobStatus
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
from grant_lab.ingestion.orchestrator import run_ingestion
from grant_lab.ranking.freshness import ScoringConfig
from grant_lab.ranking.worker import drain_queue
from grant_lab.sources.types import NormalizedCall
from tests.freshness_helpers import ctx_for, ollama_clean

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def _embed_client():
    c = MagicMock()
    c.model = "bge-m3"
    c.embed.return_value = EmbedResult(vector=[0.1] * 1024, embedded_chars=1)
    return c


class _StaticAdapter:
    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


def _pending(sf) -> int:
    with sf() as s:
        return len(s.execute(
            select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING)
        ).scalars().all())


def _ingest(sf, summary, ctx):
    run_ingestion(
        session_factory=sf,
        adapter=_StaticAdapter([NormalizedCall(
            external_id="U-1", url="https://x/u1", title="Unchanged call",
            summary=summary, deadline=FUTURE,
        )]),
        alerter=ConsecutiveFailureTracker(threshold=3, sender=InMemoryEmailSender(), admin_email=None),
        embed_client=_embed_client(),
        freshness=ctx,
    )


def test_an_unchanged_call_reingested_is_not_re_ranked(pg_session_factory, profile_factory):
    """G1 end to end (F1). The drain uses the same scoring values as `ctx`, with
    a remote configured on the enqueue side (the NULL trap)."""
    profile_factory()
    ctx = ctx_for(ollama_clean(), remote_enabled=True)
    _ingest(pg_session_factory, "Same text.", ctx)
    assert _pending(pg_session_factory) == 1, "population control: the first ingest enqueues"
    drain_queue(session_factory=pg_session_factory, client=ollama_clean(),
                embedding_threshold=0.0, batch_size=10)
    assert _pending(pg_session_factory) == 0

    _ingest(pg_session_factory, "Same text.", ctx)
    assert _pending(pg_session_factory) == 0, "an unchanged call was re-enqueued"

    _ingest(pg_session_factory, "The funder changed the scope.", ctx)
    assert _pending(pg_session_factory) == 1, "a changed call was NOT re-enqueued"


# --- G26: the real scheduler hands ingest and reconcile the drain's own values ---


class _NullAlerter:
    def record_failure(self, *a, **kw):
        return None

    def record_success(self, *a, **kw):
        return None


class _FakeEmbedder:
    model = "fake-embed"

    def embed(self, text: str):
        return [0.1] * 1024


@pytest.fixture()
def _settings_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


SCORING_PARAMS = ("client", "embedding_threshold", "embedding_hard_fail_threshold",
                  "escalation_min_score", "logprobs_enabled")


@pytest.mark.parametrize("api_key", [None, "sk-ant-test-not-a-real-key"])
def test_the_scheduler_hands_enqueue_the_drain_values(
    monkeypatch, pg_session_factory, _settings_env, api_key
):
    """G26. No network: every job body below is monkeypatched before it runs,
    and AnthropicClient() makes no request on construction."""
    seen: dict = {}
    monkeypatch.setattr(scheduler_mod, "drain_queue", lambda **kw: seen.setdefault("drain", kw) and 0)
    monkeypatch.setattr(scheduler_mod, "run_ingestion", lambda **kw: seen.setdefault("ingest", kw))
    monkeypatch.setattr(scheduler_mod, "reconcile_missing_rankings",
                        lambda session, **kw: seen.setdefault("reconcile", kw) and 0)
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory, settings=Settings(), alerter=_NullAlerter(),
        ollama_url="http://localhost:11434", gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60, ranking_queue_batch=10,
        embedding_pregate_threshold=0.27, embedding_hard_fail_threshold=0.18,
        anthropic_api_key=api_key, ranking_escalation_min_score=50,
        embed_client=_FakeEmbedder(), start=False,
    )
    try:
        for job_id in ("rank:drain", "ingest:vinnova", "ranking:reconcile"):
            job = sched.get_job(job_id)
            assert job is not None, job_id
            job.func()
    finally:
        sched.shutdown(wait=False)

    assert {"drain", "ingest", "reconcile"} <= set(seen), seen.keys()
    d = seen["drain"]
    want = ScoringConfig.for_drain(**{k: d[k] for k in SCORING_PARAMS})
    for ctx in (seen["ingest"]["freshness"], seen["reconcile"]["ctx"]):
        assert ctx.scoring == want
        assert ctx.remote_enabled is (d["remote_client"] is not None)
    assert want.local_model == "gemma4:e4b-q8" and want.embedding_threshold == 0.27
```

- [ ] **Step 2: Run and watch it fail.**

```bash
cd $W && timeout 900 uv run pytest tests/test_ranking_freshness_wiring.py -q -p no:cacheprovider 2>&1 | tail -6
```

Expected FAILED: `run_ingestion() got an unexpected keyword argument 'freshness'`, and the
scheduler test's `KeyError: 'freshness'`.

- [ ] **Step 3: The queue wrappers.** In `src/grant_lab/ranking/queue.py`:
  - Delete `_RECONCILE_DAYS` and `_DEAD_LETTER_SUPPRESS_DAYS` and their comments. They
    live in `freshness` now.
  - Replace `bulk_enqueue_for_calls` and `reconcile_missing_rankings` with:

```python
def bulk_enqueue_for_calls(session: Session, call_ids: list[int], *, ctx: FreshnessContext) -> int:
    """Enqueue the STALE (touched call x active profile) pairs after an ingest.

    Before ranking spec 2026-09-24 this enqueued every touched pair, and
    "touched" is every call the adapter returned -- so the whole open feed was
    re-ranked, and re-paid on Sonnet, every night. Now only a pair whose inputs
    changed, or whose second reading is owed, is enqueued (ranking/freshness.py).
    """
    if not call_ids:
        return 0
    return enqueue_stale_pairs(session, ctx=ctx, call_ids=call_ids)


def reconcile_missing_rankings(session: Session, *, ctx: FreshnessContext) -> int:
    """The daily 04:00 repair: enqueue every stale (open call x active profile)
    pair (ranking/freshness.py). Closes the orchestrator's step-1-succeeded-
    but-step-3-failed gap, catches pairs whose call vector caught up at 03:30,
    and drains owed escalations after the midnight cap reset."""
    n = enqueue_stale_pairs(session, ctx=ctx)
    if n:
        _logger.info(
            "ranking.reconcile_enqueued",
            extra={"event": "ranking_reconcile_enqueued", "count": n},
        )
    return n
```

  - Imports: add `from grant_lab.ranking.freshness import FreshnessContext, enqueue_stale_pairs`.
    Remove the imports that became unused (`Profile`, `LEGACY_SENTINEL`,
    `rank_fingerprint`, `text` if no longer used, `timedelta` if no longer used). Let
    `uv run ruff check src/grant_lab/ranking/queue.py` tell you. Keep `_STUCK_AFTER`,
    which `requeue_stuck_running` uses.

- [ ] **Step 4: Backfill.** Replace the body of `src/grant_lab/ranking/backfill.py` from
  `_logger = …` down, keeping the module imports that remain used:

```python
_logger = logging.getLogger(__name__)


def backfill_profile(*, session_factory: sessionmaker, profile_id: int) -> int:
    """Enqueue every OPEN call (deadline NULL or in the future) for this profile.

    No freshness check and no dead-letter suppression (ranking spec D4): backfill
    runs only after a human act. post_edit fires only when the profile's rank
    inputs changed, so every pair of this profile is stale already;
    bootstrap_worker runs for a profile with no rankings; and the admin
    "Re-rank everything" button is the override by definition (spec D5).

    Idempotent against the partial unique index per (call, profile).
    """
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or not profile.is_active:
            return 0
        result = session.execute(
            text(
                """
                INSERT INTO ranking_jobs (call_id, profile_id, status)
                SELECT c.id, :profile_id, 'pending'
                FROM calls c
                WHERE c.deadline IS NULL OR c.deadline > now()
                ON CONFLICT (call_id, profile_id)
                  WHERE status IN ('pending', 'running') DO NOTHING
                """
            ),
            {"profile_id": profile_id},
        )
        session.commit()
        enqueued = result.rowcount or 0
    _logger.info(
        "ranking.backfill_done",
        extra={"event": "ranking_backfill_done", "profile_id": profile_id, "enqueued": enqueued},
    )
    return enqueued
```

  Remove `BACKFILL_DAYS`, `_DEAD_LETTER_SUPPRESS_DAYS`, and the `datetime`/`timedelta`
  imports if they become unused.

- [ ] **Step 5: Orchestrator and CLI.**
  - `src/grant_lab/ingestion/orchestrator.py`: add the keyword parameter
    `freshness: FreshnessContext,` to `run_ingestion` after `embed_client`, with the
    import `from grant_lab.ranking.freshness import FreshnessContext`. Change
    `bulk_enqueue_for_calls(session, touched_call_ids)` to
    `bulk_enqueue_for_calls(session, touched_call_ids, ctx=freshness)`. In the step-3
    comment, replace "enqueue ranking jobs for touched calls" wording with "enqueue the
    touched calls' STALE pairs (ranking/freshness.py)".
  - `src/grant_lab/ingestion/cli.py`: in the `run_ingestion(...)` call, add
    `freshness=FreshnessContext.from_settings(settings),` and import `FreshnessContext`
    from `grant_lab.ranking.freshness`.

- [ ] **Step 6: Scheduler.** In `build_scheduler` in `src/grant_lab/scheduler.py`:
  1. **Move** the block that builds `ollama = OllamaClient(...)` and
     `anthropic_client` (from `ollama = OllamaClient(base_url=ollama_url, model=gemma_model)`
     through the `if anthropic_api_key:` assignment) so it sits **above**
     `for cfg in SOURCES:`. Move it; do not duplicate it.
  2. Directly after the moved block, add:

```python
    # Ranking freshness (spec D3/D4): ingest and reconcile judge "current" with
    # the SAME scorer values the rank:drain job below hands drain_queue. Built
    # from these locals, never from Settings, so the two cannot drift (gate G26).
    freshness = FreshnessContext(
        scoring=ScoringConfig.for_drain(
            client=ollama,
            embedding_threshold=embedding_pregate_threshold,
            embedding_hard_fail_threshold=embedding_hard_fail_threshold,
            escalation_min_score=ranking_escalation_min_score,
            logprobs_enabled=settings.ranking_logprobs_enabled,
        ),
        remote_enabled=anthropic_client is not None,
    )
```

  3. In `_ingest_job`, add `freshness=freshness,` to the `run_ingestion(...)` call.
  4. In `_reconcile_rankings_tick`, change `reconcile_missing_rankings(session)` to
     `reconcile_missing_rankings(session, ctx=freshness)`, and change "(recent call ×
     active profile)" in its comment to "(open call × active profile)".
  5. Import `FreshnessContext, ScoringConfig` from `grant_lab.ranking.freshness`.

- [ ] **Step 7: Admin.** In `src/grant_lab/web/api/admin.py`:
  - Remove `BACKFILL_DAYS` from the `grant_lab.ranking.backfill` import.
  - In `AdminDashboardOut`, replace `recent_calls_count: int` and `backfill_days: int`
    with `open_calls_count: int`.
  - Replace the `recent_cutoff … recent_calls_count = …` statements with:

```python
        open_calls_count = session.execute(
            select(func.count(Call.id)).where(
                or_(Call.deadline.is_(None), Call.deadline > func.now())
            )
        ).scalar_one()
```

  - In the `AdminDashboardOut(...)` return, use
    `rerank_estimate=active_profiles_count * open_calls_count` and
    `open_calls_count=open_calls_count`, and delete the `recent_calls_count=` and
    `backfill_days=` arguments.
  - Add `or_` to the `sqlalchemy` import if it is missing. Remove `timedelta` if it
    became unused.

- [ ] **Step 8: Update the existing tests to the new signatures and contracts.**
  1. Every `run_ingestion(` call site. Run this, then read the diff:

```bash
cd $W && python3 - <<'PY'
import re
from pathlib import Path
files = ["tests/test_ingestion_enqueues_rankings.py", "tests/test_ingestion_orchestrator.py",
         "tests/test_ingestion_orchestrator_embeds.py", "tests/test_ingestion_e2e.py",
         "tests/test_ranking_e2e.py"]
for f in files:
    p = Path(f); lines = p.read_text().splitlines(keepends=True); out = []; n = 0
    for line in lines:
        out.append(line)
        m = re.match(r"^(\s*)(?:\w+\s*=\s*)?run_ingestion\($", line.rstrip("\n"))
        if m:
            out.append(f"{m.group(1)}    freshness=ctx_for(),\n"); n += 1
    s = "".join(out)
    if n and "from tests.freshness_helpers import" not in s:
        s = s.replace("\nfrom grant_lab.", "\nfrom tests.freshness_helpers import ctx_for\nfrom grant_lab.", 1)
    p.write_text(s)
    print(f, "calls:", len(re.findall(r"run_ingestion\(", s)) - s.count("def run_ingestion("), "patched:", n)
PY
git -C $W diff --stat -- tests/
```

     For every file, `calls` must equal `patched`. If `ctx_for` was already imported
     from `tests.freshness_helpers` under another import line, merge the imports by hand.
     `uv run ruff check --select I --fix` on those five files sorts them.
  2. `tests/test_ranking_queue.py`:
     - Add `from tests.freshness_helpers import ctx_for`, and a module constant
       `CTX = ctx_for()` below `_PAST_STUCK`.
     - Replace every `reconcile_missing_rankings(s)` with
       `reconcile_missing_rankings(s, ctx=CTX)`.
     - In `test_reconcile_skips_already_ranked_pair`, add these to the seeded
       `CallRanking`:

```python
                call_fingerprint=call_rank_fingerprint(s.get(Call, call_id)),
                scoring_fingerprint=CTX.scoring.fingerprint(),
```

       Import `call_rank_fingerprint` from `grant_lab.profiles.fingerprint`.
     - Replace `test_reconcile_ignores_old_calls` with
       `test_reconcile_includes_an_open_call_published_long_ago`. Seed with
       `published=datetime.now(UTC) - timedelta(days=365)` and no deadline, and assert
       `n == 1`. The docstring should say the window is openness, not publication date
       (spec F3).
     - Replace `test_reconcile_ignores_calls_with_null_published_at` with
       `test_reconcile_includes_a_call_with_null_published_at` (`n == 1`).
     - Add:

```python
def test_reconcile_ignores_a_closed_call(pg_session_factory, profile_factory):
    """G7 through the wrapper."""
    profile_factory()
    with pg_session_factory() as s:
        cid = _seed_call_with(s, external_id="RC-CLOSED", published=datetime.now(UTC))
        s.get(Call, cid).deadline = datetime.now(UTC) - timedelta(days=1)
        s.commit()
    with pg_session_factory() as s:
        assert reconcile_missing_rankings(s, ctx=CTX) == 0
```

  3. `tests/test_ranking_reconcile_staleness.py`: import `ctx_for`, and replace each
     `reconcile_missing_rankings(session)` with
     `reconcile_missing_rankings(session, ctx=ctx_for(ollama_clean()))`. `ranked_pair`
     drains with `ollama_clean()` and `embedding_threshold=0.0`, which is exactly what
     `ctx_for(ollama_clean())` hashes. So `test_a_matching_fingerprint_is_not_re_enqueued`
     must stay green. If it goes red, the scoring stamps disagree. Find out why; do not
     loosen the test.
  4. `tests/test_ranking_backfill.py`: drop the `BACKFILL_DAYS` import. Replace
     `test_backfill_enqueues_jobs_for_recent_calls` with:

```python
def test_backfill_enqueues_every_open_call_whatever_its_age(pg_session_factory, profile_factory):
    """Spec D4: the window is openness. Published 1 day or 400 days ago is the same."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_calls(s, ages_days=[1, 30, 400])
        s.execute(update(Call).where(Call.external_id == "BF-2").values(
            deadline=datetime.now(UTC) - timedelta(days=1)))  # closed
        s.commit()
    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)
    with pg_session_factory() as s:
        assert len(s.execute(select(RankingJob)).scalars().all()) == 2
```

     Replace `test_backfill_skips_pair_with_recent_dead_lettered_job` with
     `test_backfill_bypasses_dead_letter_suppression`. Keep the same seed, and assert
     that **both** calls get a pending job (`len(pending) == 2`). The docstring should
     cite spec D4: backfill runs only after a human act.
  5. `tests/test_api_admin_dashboard.py::test_dashboard_rerank_estimate_is_product`:
     seed the three calls with `deadline=datetime(2099, 1, 1, tzinfo=UTC)` and add a
     fourth with `deadline=datetime.now(UTC) - timedelta(days=1)`. Assert
     `open_calls_count == 3`, `rerank_estimate == 6`, and that `"backfill_days"` and
     `"recent_calls_count"` are **not** keys of the body.
  6. `tests/test_api_admin_ops.py`: add G23.

```python
def test_rerank_enqueues_fresh_and_suppressed_open_pairs_but_not_closed(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    """G23 (gap 2, D5): the override ignores freshness AND suppression."""
    from grant_lab.db.models import CallRanking, RankingJobStatus
    from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
    from grant_lab.db.models import Profile
    from tests.freshness_helpers import ctx_for

    pid, _ = profile_factory()
    ctx = ctx_for()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        fresh, suppressed, closed = (
            Call(source_id=src.id, external_id=e, url=f"https://x/{e}", title=e,
                 deadline=d)
            for e, d in (("FRESH", datetime(2099, 1, 1, tzinfo=UTC)),
                         ("SUPP", datetime(2099, 1, 1, tzinfo=UTC)),
                         ("CLOSED", datetime.now(UTC) - timedelta(days=1)))
        )
        s.add_all([fresh, suppressed, closed])
        s.flush()
        s.add(CallRanking(call_id=fresh.id, profile_id=pid, aggregate_score=60, rationale="",
                          model="m", prompt_version="v5.0", raw_response={},
                          profile_fingerprint=rank_fingerprint(s.get(Profile, pid)),
                          call_fingerprint=call_rank_fingerprint(fresh),
                          scoring_fingerprint=ctx.scoring.fingerprint()))
        s.add(RankingJob(call_id=suppressed.id, profile_id=pid,
                         status=RankingJobStatus.FAILED, attempts=5, last_error="x"))
        s.commit()
        want = {fresh.id, suppressed.id}
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    assert c.post("/api/admin/rerank").status_code == 200
    with pg_session_factory() as s:
        pending = {j.call_id for j in s.execute(
            select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING)).scalars()}
    assert pending == want
```

     In `test_rerank_enqueues_for_active_profiles`, give the seeded call
     `deadline=datetime(2099, 1, 1, tzinfo=UTC)`. It passes either way (NULL is open),
     but this makes the intent explicit.

- [ ] **Step 9: Run the touched tests, then grep for stragglers.**

```bash
cd $W && timeout 1200 uv run pytest tests/test_ranking_freshness_wiring.py tests/test_ranking_queue.py tests/test_ranking_reconcile_staleness.py tests/test_ranking_backfill.py tests/test_api_admin_dashboard.py tests/test_api_admin_ops.py tests/test_ingestion_enqueues_rankings.py tests/test_ingestion_orchestrator.py tests/test_ingestion_orchestrator_embeds.py tests/test_ingestion_e2e.py tests/test_ranking_e2e.py tests/test_scheduler_ranking_logprobs.py -q -p no:cacheprovider 2>&1 | tail -6
cd $W && grep -rnE "BACKFILL_DAYS|_RECONCILE_DAYS|recent_calls_count|backfill_days" src tests --include=*.py
```

Expected: all PASS. The grep prints nothing.

- [ ] **Step 10: Prove the wiring gates bite.**
  1. `scheduler.py`, anchor `        remote_enabled=anthropic_client is not None,`, mutant
     `        remote_enabled=False,`. Expected:
     `test_the_scheduler_hands_enqueue_the_drain_values[sk-ant-test-not-a-real-key]`
     FAILED. The `None` case still passes, which shows the split. Restore.
  2. Same file, anchor `            embedding_threshold=embedding_pregate_threshold,\n            embedding_hard_fail_threshold=embedding_hard_fail_threshold,\n            escalation_min_score=`
     (the one inside `FreshnessContext`, not the drain job's; check `count == 1`), mutant
     with `embedding_threshold=settings.embedding_pregate_threshold,`. Expected: both
     parametrizations FAILED (the test passes 0.27, Settings defaults to 0.30). Restore.
  3. `queue.bulk_enqueue_for_calls`, anchor
     `    return enqueue_stale_pairs(session, ctx=ctx, call_ids=call_ids)`, mutant: the old
     unconditional `INSERT … CROSS JOIN profiles` body (paste it from
     `git -C $W show $BASE:src/grant_lab/ranking/queue.py`, with `$BASE` the SHA from Task 0). Expected:
     `test_an_unchanged_call_reingested_is_not_re_ranked` FAILED at "an unchanged call
     was re-enqueued". Restore. This is F1 itself, reproduced.

- [ ] **Step 11: Commit.**

```bash
cd $W && F="src/grant_lab/ranking/queue.py src/grant_lab/ranking/backfill.py src/grant_lab/ingestion/orchestrator.py src/grant_lab/ingestion/cli.py src/grant_lab/scheduler.py src/grant_lab/web/api/admin.py tests/test_ranking_freshness_wiring.py tests/test_ranking_queue.py tests/test_ranking_reconcile_staleness.py tests/test_ranking_backfill.py tests/test_api_admin_dashboard.py tests/test_api_admin_ops.py tests/test_ingestion_enqueues_rankings.py tests/test_ingestion_orchestrator.py tests/test_ingestion_orchestrator_embeds.py tests/test_ingestion_e2e.py tests/test_ranking_e2e.py" && uv run ruff check $F && uv run ruff format $F
cd $W && uv run python scripts/gen_architecture.py && git -C $W add $F docs/ARCHITECTURE.md
git -C $W commit -m "fix(ranking): stop re-ranking the whole open feed every night" -m "Ingest and the 04:00 reconcile enqueue only stale pairs over open calls, with the scheduler handing both the drain's own scoring values (spec D3, D4, G26). Backfill enqueues every open pair of its profile. The admin estimate counts open calls (spec D5)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — the chip list and the admin label

**Files:**
- Modify: `frontend/src/components/CriterionChips.tsx` (`CRITERIA`)
- Modify: `frontend/src/components/CriterionChips.test.tsx`
- Modify: `frontend/src/routes/AdminDashboard.tsx`, `frontend/src/routes/AdminDashboard.test.tsx`
- Regenerate: `frontend/src/api/schema.gen.ts`
- Modify: `tests/architecture/test_criterion_vocabulary.py` (`test_the_frontend_criteria_scan_finds_a_real_list`: 7 → 6)

- [ ] **Step 1: Regenerate the API types.**

```bash
cd $W && . "$HOME/.nvm/nvm.sh" && nvm use 22 >/dev/null && EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh && git -C $W diff --stat -- frontend/src/api/schema.gen.ts
cd $W && grep -cE "open_calls_count" frontend/src/api/schema.gen.ts; grep -cE "recent_calls_count|backfill_days" frontend/src/api/schema.gen.ts
```

Expected: the first grep prints at least `1`, the second prints `0`.

- [ ] **Step 2: Watch the frontend fail.**

```bash
cd $W/frontend && pnpm exec tsc --noEmit -p . 2>&1 | tail -5
```

Expected: type errors in `AdminDashboard.tsx` and `AdminDashboard.test.tsx` on
`backfill_days` / `recent_calls_count`.

- [ ] **Step 3: Update the components and tests.**
  - `CriterionChips.tsx`: delete `"timeline",` from `CRITERIA`. If a nearby comment
    counts "seven" criteria or lists the four gates, update it to six / three.
  - `CriterionChips.test.tsx`:
    - In `ALL`, delete the `timeline` row.
    - Rename "renders one chip per criterion for a full seven-criterion row, in reading
      order" to say "six-criterion", and change `toHaveLength(7)` to `toHaveLength(6)`.
    - In "renders a single chip, because a gate FAIL short-circuits the rest", use
      `crit({ criterion: "budget", rating: "fail", label: "Budget" })` and expect
      `"Budget ✗"`.
    - In "labels a reason-code rationale as a code, not as reasoning", use
      `crit({ criterion: "budget", rating: "unknown", label: "Budget", rationale: "currency_mismatch" })`,
      click `{ name: /Budget/ }`, and expect `screen.getByText("currency_mismatch").tagName`
      to be `"CODE"`.
  - `AdminDashboard.tsx`:
    - Button text: `Re-rank everything (open calls × active profiles)`.
    - Help text: `Enqueues {d.rerank_estimate} ranking jobs ({d.active_profiles_count} active profiles × {d.open_calls_count} open calls).`
    - Confirm text: `` `This enqueues ${d.rerank_estimate} ranking jobs and may take a while. Continue?` ``
      The estimate is now exact, so "up to ~" goes.
  - `AdminDashboard.test.tsx`: in `DASH`, replace `recent_calls_count: 3,` and
    `backfill_days: 90,` with `open_calls_count: 3,`.
  - `tests/architecture/test_criterion_vocabulary.py::test_the_frontend_criteria_scan_finds_a_real_list`:
    `== 7` → `== 6`.

- [ ] **Step 4: Run the frontend gates and the vocabulary gate.**

```bash
cd $W/frontend && pnpm exec tsc --noEmit -p . 2>&1 | tail -3 && pnpm exec vitest run src/components/CriterionChips.test.tsx src/routes/AdminDashboard.test.tsx 2>&1 | tail -6
cd $W/frontend && pnpm exec oxlint src/components/CriterionChips.tsx src/routes/AdminDashboard.tsx 2>&1 | tail -3
cd $W && timeout 600 uv run pytest tests/architecture/test_criterion_vocabulary.py -q -p no:cacheprovider 2>&1 | tail -3
```

Expected: clean, PASS, clean, PASS.

- [ ] **Step 5: Prove the vocabulary gate still bites.** Put `"timeline",` back into
  `CRITERIA` in `CriterionChips.tsx`. Expected:
  `test_the_chip_row_invents_no_criterion_of_its_own` stays green (the label map still
  has `timeline`), but `test_the_frontend_criteria_scan_finds_a_real_list` FAILED.
  Restore. Record that `invents_no_criterion` cannot see this regression on its own,
  and that the count assertion is what does.

- [ ] **Step 6: Commit.**

```bash
cd $W && git -C $W add frontend/src/components/CriterionChips.tsx frontend/src/components/CriterionChips.test.tsx frontend/src/routes/AdminDashboard.tsx frontend/src/routes/AdminDashboard.test.tsx frontend/src/api/schema.gen.ts tests/architecture/test_criterion_vocabulary.py
git -C $W commit -m "feat(ui): drop the timeline chip and count open calls in the re-rank estimate" -m "The chip row loses timeline (spec D8). The admin re-rank button says 'open calls' and shows an exact job count (spec D5)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 9: Full verification

**Files:** none changed, unless a check fails.

- [ ] **Step 1: The whole backend suite.**

```bash
cd $W && timeout 2400 uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -4
```

Expected: `N passed, 1 skipped`, where N is Task 0's baseline plus the tests this plan
added, minus the ones it replaced. Count the delta with `git -C $W diff --stat $BASE --
tests/` and the `def test_` changes. **Do not adjust an expected number to make it close.**
If it does not close, find the term that moved and name it in your report. `0 skipped`
means STOP.

- [ ] **Step 2: The whole frontend suite, and lint.**

```bash
cd $W/frontend && pnpm exec vitest run 2>&1 | tail -5 && pnpm exec tsc --noEmit -p . && pnpm exec oxlint . 2>&1 | tail -3
cd $W && uv run ruff check src tests && uv run ruff format --check $(git -C $W diff --name-only $BASE -- '*.py')
```

Expected: all clean. `ruff format --check` is given the touched files only, never a
directory.

- [ ] **Step 3: Generated artefacts are current.**

```bash
cd $W && uv run python scripts/gen_architecture.py && git -C $W status --short docs/ARCHITECTURE.md
cd $W && . "$HOME/.nvm/nvm.sh" && nvm use 22 >/dev/null && EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh && git -C $W status --short frontend/src/api/schema.gen.ts
```

Expected: both `status` lines print nothing.

- [ ] **Step 4: Spec coverage.** For each gate G1–G27 in spec §7 (G14 is unused), write
  down in your report the test that implements it and the mutation that reddened it,
  with the observed red set. Any gate without a recorded reject run is not done.

- [ ] **Step 5: Report the deploy notes (spec §6).** Include them verbatim in your final
  report:
  1. Deploy only after 0080 has run one 03:30 tick with zero NULL open-call
     `embedding_fingerprint`s.
  2. The first night after deploy re-ranks every open pair once. Escalations beyond the
     daily cap go `pending` and drain over the following days.
  3. On that first re-rank, stamps are NULL, so no Before-you-apply notes carry over. A
     pair that re-escalates that day gets fresh notes; the others get them as their
     pending escalation drains.
  4. For the first week, run spec §5's measurements daily.
