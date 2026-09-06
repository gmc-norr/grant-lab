# Matching Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a stored embedding and a stored ranking say *which* profile content they describe, so the daily repair can tell "exists" from "current".

**Architecture:** Two derived fingerprints (`v1:` + sha256), recomputed at check time from the row rather than maintained at write time. `embedding_fingerprint` hashes the *output* of `build_profile_text`; `rank_fingerprint` hashes a canonical JSON payload of everything the ranker reads, including the stored embedding fingerprint. Two new nullable columns record what each stored artefact was built from. Both daily repair arms gain a staleness comparison, and the ranking worker defers a pair whose vector has not caught up.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16 + pgvector, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-09-06-matching-freshness-design.md` (rev 3, commit `1e7fae03`). The spec is INPUT. If the spec is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit to the spec.

## Global Constraints

- **Base commit:** `master` at `c08c9a69`, the commit that added this plan. (`1e7fae03` is the spec's commit, not the base — corrected after the dry run.) Branch `feat/matching-freshness`, worktree `.worktrees/freshness`.
- **Measured baseline: `5364 passed, 1 skipped`.** Run `uv run pytest -n 16 --dist loadfile`. Carry DELTAS against a baseline you measure yourself; never adjust an expected number to make arithmetic close.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic API call ran — stop and record it. Never export `ANTHROPIC_API_KEY`.
- **`docs/superpowers/specs/` is byte-identical at every commit.**
- **Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** It holds a real API key. Set any env var a test needs inline, via `monkeypatch.setenv` inside that test.
- **`docs/ARCHITECTURE.md` is generated.** Run `uv run python scripts/gen_architecture.py` in the same commit as any model change. Never hand-edit it.
- **Lint gate:** `uv run ruff check src tests alembic scripts` must be clean.
- **Migration number is `0075`, `down_revision = "0074"`.** Verified: `alembic/versions/0074_boards_settings_bounds.py` declares `revision = "0074"`.
- **Fast local runs:** `uv run pytest -n 16 --dist loadfile`. Never `-n auto` (resolves to 72 here), never `--dist loadgroup`.
- **False-pass signature.** A broken import surfaces as mass teardown ERRORs with **zero FAILED**, which reads like a clean run if you skim. Judge every mutation on FAILED, never on ERROR. A missing fixture is exactly this shape: `ERROR: fixture 'x' not found` at collection, so the test never runs and never "watches it fail".

## Where the tests go, and what fixtures exist

**This section is the correction the dry run paid for. Read it before writing any test.**

`tests/` has exactly five subdirectories — `fixtures/`, `architecture/`, `fencing/`, `proposal_assist/`, `web/`. There is **no `tests/embeddings/` and no `tests/ranking/`.** The repo convention is flat, prefixed files at `tests/` root (`tests/test_embeddings_refresh.py`, `tests/test_ranking_worker.py`, ...). An earlier revision of this plan named directories that do not exist; `uv run pytest tests/embeddings/ -v` exits 4 with `ERROR: file or directory not found`.

The eight files this plan creates, all flat:

| Task | File |
|---|---|
| 2 | `tests/freshness_helpers.py` (helpers, not a test file) |
| 1 | `tests/test_migration_0075_freshness_fingerprints.py` |
| 2 | `tests/test_profile_fingerprint.py` |
| 3 | `tests/test_embeddings_refresh_fingerprint.py` |
| 4 | `tests/test_embeddings_refresh_tick_staleness.py` |
| 5 | `tests/test_ranking_worker_fingerprint.py` |
| 6 | `tests/test_ranking_worker_defers_stale_vector.py` |
| 7 | `tests/test_ranking_reconcile_staleness.py` |
| 8 | `tests/test_api_profile_update_scheduling.py` |

Separate files rather than appending to the existing large ones is right: `--dist loadfile` keeps every file's tests on one xdist worker.

**The fixtures that actually exist.** Every fixture defined in any `conftest.py` in the tree:

```
_app, client_authed_outsider, client_authed_owner, client_unauth,
_close_helper_entered_lifespans, current_user, db_engine, fake_public_dns,
_isolate_golden_set_files, _isolate_settings_from_dotenv, make_workspace_repo,
migrated_engine, pg_container, pg_session_factory, pg_url, profile_factory,
_require_explicit_event_loops, _reset_collab_rooms, review_active_workspace,
review_run_with_item, review_workspace, _settings_env, test_client_factory,
_test_workspaces_root, _workspaces_dir_is_never_the_real_one
```

An earlier revision named twelve fixtures; **eleven of them do not exist.** The mapping:

| Named by the old plan | Reality |
|---|---|
| `session` | use `pg_session_factory` and open your own: `with pg_session_factory() as s:` |
| `session_factory` | `pg_session_factory` |
| `migrated_session` | `migrated_engine` (for `inspect`) + `pg_url` (to drive alembic) |
| `make_profile`, `make_call` | written by Task 2 into `tests/freshness_helpers.py` — plain helpers taking a session, NOT fixtures |
| `fake_embedder` | `FakeEmbedder` in `tests/freshness_helpers.py` (Task 2) |
| `fake_rank_client` | `ollama_clean()` in `tests/freshness_helpers.py` — a real `OllamaClient` over an `httpx.MockTransport` |
| `ranked_pair`, `queued_pair` | local `@pytest.fixture` in the file that needs them, built from the helpers (code given in Tasks 5, 6, 7) |
| `body_for`, `owner_profile` | local to `tests/test_api_profile_update_scheduling.py` (code given in Task 8) |
| `auth_headers` | **the concept does not apply** — auth is a signed **session cookie**, not a bearer header. Set `client.cookies.set("session", signer.make(user_id=uid))` |
| `client` | build a `TestClient(create_app())` locally, or use `client_authed_owner` |

Only `profile_factory` (returns `(profile_id, user_id)`, not a `Profile`), `pg_session_factory`, `pg_url` and `migrated_engine` are usable as-is.

## Task order is load-bearing

**Task 5 must land before Task 7.** Task 7 makes reconcile treat a NULL `profile_fingerprint` as stale (gate 13). Task 5 is what makes the worker write a non-NULL one. Landing 7 first gives every freshly-written ranking a NULL fingerprint, which reconcile immediately re-enqueues — an infinite re-rank loop.

**Task 1 before everything.** Tasks 2-8 read or write the two new columns.

**Task 2 before Tasks 3-8, for its *helper module* as much as its functions.** Task 2 creates `tests/freshness_helpers.py`; every later task's tests import `make_profile`, `make_call`, `FakeEmbedder`, `enqueue_job` or `ollama_clean` from it. Skipping it produces collection ERRORs, which in this suite read as a clean run (see the false-pass signature above).

**Two pre-existing tests change, and the task that changes each is named.** The dry run found both by running them; an earlier revision of this plan promised neither.

- **Task 4** amends `tests/test_embeddings_refresh.py::test_refresh_missing_tick_force_false_default_skips_already_embedded`. Widening the candidate set makes a NULL `embedding_fingerprint` stale by design, so the seeded profile is now re-embedded and `summary["profiles_embedded"]` is 1, not 0. Exact edit in Task 4 step 5.
- **Task 7** amends `tests/test_ranking_queue.py::test_reconcile_skips_already_ranked_pair`. It seeds a `CallRanking` with no `profile_fingerprint` and asserts no enqueue; gate 13 makes a NULL fingerprint stale. Exact edit in Task 7 step 4. **That failure is also the empirical proof of the Task-5-before-Task-7 rule** — it is the re-enqueue loop, reproduced in miniature.

Amend each in the task that breaks it, in the same commit. Do not carry a red test forward.

**Task 1's grandfathering gate drives alembic itself, and must restore `head`.** The shared testcontainers Postgres runs `upgrade head` once, session-scoped, against an **empty** database, so 0075's `UPDATE call_rankings SET profile_fingerprint = 'legacy'` touches zero rows there and cannot be observed. The real gate does `downgrade 0074` → seed a `call_rankings` row → `upgrade head`, and **must** put `command.upgrade(cfg, "head")` in a `finally` or it poisons every later file on that xdist worker. `--dist loadfile` keeps one file on one worker and files run sequentially within a worker, so this is safe — it is one more reason never to switch to `--dist loadgroup`.

**`alembic/` is not in most tasks' lint path.** Tasks 2-7 run `uv run ruff check src tests`; only Task 8 covers `alembic`. Task 1 writes the migration, so **Task 1 lints with `alembic` in the path** — otherwise an unsorted import block in `0075` hides for seven tasks.

## Three traps found while planning — do not "fix" these

1. **Do NOT add a paired-NULL CHECK constraint for the fingerprint.** `db/models/profiles.py:60-63` has `ck_profiles_embedding_model_paired` — `(embedding IS NULL) = (embedding_model IS NULL)`. An analogous constraint for `embedding_fingerprint` looks symmetrical and would **fail migration 0075**, because existing rows deliberately keep their vector while getting a NULL fingerprint.

2. **`expire_on_commit=False`** (`db/session.py:58`). Computing the new fingerprint *after* `session.commit()` does **not** reload the row from Postgres, so it does not solve the in-memory-versus-loaded asymmetry. That asymmetry is handled in the canonicalizer (Task 2), not by moving a line.

3. **Force mode never enters the drain loop.** `refresh_missing_embeddings_tick` returns early at `refresh.py:175-183` (`if force:` is line 175; the `return _refresh_missing_embeddings_batch(...)` spans 176-183) with a single `force=True` batch. The drain loop at `:200` is always `force=False`. Task 4's drain change therefore cannot affect force mode; the force protection belongs in the batch function.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `alembic/versions/0075_freshness_fingerprints.py` | two nullable columns + `'legacy'` stamp | 1 |
| `src/grant_lab/db/models/profiles.py` | `Profile.embedding_fingerprint` | 1 |
| `src/grant_lab/db/models/ranking.py` | `CallRanking.profile_fingerprint` | 1 |
| `src/grant_lab/profiles/fingerprint.py` | **new** — canonicalizer + both fingerprints | 2 |
| `tests/freshness_helpers.py` | **new** — the fixtures/helpers every later task's tests import | 2 |
| `src/grant_lab/embeddings/refresh.py` | write site + widened tick + drain fix | 3, 4 |
| `src/grant_lab/ranking/worker.py` | write site + retained-analysis guard + deferral | 5, 6 |
| `src/grant_lab/ranking/queue.py` | reconcile staleness arm | 7 |
| `src/grant_lab/web/api/profiles.py` | hole 1 scheduling guard | 8 |

---

### Task 1: The columns

**Files:**
- Create: `alembic/versions/0075_freshness_fingerprints.py`
- Modify: `src/grant_lab/db/models/profiles.py:114` (after `embedding_model`)
- Modify: `src/grant_lab/db/models/ranking.py:158` (after `analysis_model`)
- Modify: `docs/ARCHITECTURE.md` (generated)
- Test: `tests/test_migration_0075_freshness_fingerprints.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Profile.embedding_fingerprint: Mapped[str | None]`, `CallRanking.profile_fingerprint: Mapped[str | None]`. Every later task reads or writes these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0075_freshness_fingerprints.py`. There is **no `migrated_session` fixture** — use `migrated_engine` (for `inspect`) and `pg_url` (to drive alembic). The column-existence shape follows `tests/test_migration_0074_boards_settings_bounds.py`; the grandfathering test follows `tests/test_migration_0038_reference_tombstone.py`'s downgrade/seed/upgrade pattern.

**Why the grandfathering gate must drive alembic.** An earlier revision asserted
`row is None or row == "legacy"` against the shared container. That gate is
**vacuous and was proved so by mutation**: migrations run once, session-scoped,
against an empty database, and `pg_session_factory` TRUNCATEs every table at each
test boundary — so `call_rankings` is empty when 0075 runs, the UPDATE touches 0
rows, the SELECT returns `None`, and the assertion **also passes with the
`op.execute(...)` line deleted entirely**. Driving `0074 → seed → head` is what
makes it able to fail.

```python
"""Migration 0075: the two freshness-fingerprint columns."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_both_fingerprint_columns_exist(migrated_engine):
    insp = inspect(migrated_engine)
    assert "embedding_fingerprint" in {c["name"] for c in insp.get_columns("profiles")}
    assert "profile_fingerprint" in {c["name"] for c in insp.get_columns("call_rankings")}


def test_profiles_keep_their_vector_with_a_null_fingerprint(migrated_engine):
    """Deliberate asymmetry: profiles are re-embedded, rankings are not re-ranked.

    A paired-NULL CHECK constraint here would look symmetrical with
    ck_profiles_embedding_model_paired and would FAIL this migration.
    """
    names = {c["name"] for c in inspect(migrated_engine).get_check_constraints("profiles")}
    assert "ck_profiles_embedding_fingerprint_paired" not in names
    assert "ck_profiles_embedding_model_paired" in names


def test_existing_rankings_are_stamped_legacy(migrated_engine, pg_url):
    """The grandfathering gate: seed a ranking AT 0074, then upgrade.

    `finally` restores head unconditionally -- without it, a failure here
    leaves the shared container one revision behind and every later test
    file on this xdist worker fails on a missing column.
    """
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0074")
        with engine.begin() as conn:
            assert "profile_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("call_rankings")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0075','s0075') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0075@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            pid = conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('M0075','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, title, url) "
                    "VALUES (:s,'M0075','t','https://example.org/m0075') RETURNING id"
                ),
                {"s": src},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO call_rankings "
                    "(call_id, profile_id, aggregate_score, model, prompt_version, "
                    " raw_response) "
                    "VALUES (:c,:p,50,'gemma','v1','{}'::jsonb)"
                ),
                {"c": cid, "p": pid},
            )

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            got = conn.execute(
                text(
                    "SELECT profile_fingerprint FROM call_rankings "
                    "WHERE call_id=:c AND profile_id=:p"
                ),
                {"c": cid, "p": pid},
            ).scalar_one()
        assert got == "legacy", f"pre-0075 ranking was not grandfathered: {got!r}"
    finally:
        command.upgrade(_cfg(pg_url), "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM call_rankings"))
            conn.execute(text("DELETE FROM calls"))
            conn.execute(text("DELETE FROM profiles"))
            conn.execute(text("DELETE FROM users"))
            conn.execute(text("DELETE FROM sources WHERE slug='s0075'"))
        engine.dispose()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_migration_0075_freshness_fingerprints.py -v`
Expected: FAIL — `assert 'embedding_fingerprint' in cols` (the column does not exist).

- [ ] **Step 3: Write the migration**

**Import order is a hard gate.** `from alembic import op` first and `import sqlalchemy as sa` second — the natural reading order — is ruff `I001 Import block is un-sorted or un-formatted`, and `alembic/` is not in Tasks 2-7's lint path, so the error would hide until Task 8. Use the order below, which is the shape `alembic/versions/0074_boards_settings_bounds.py:35-37` uses.

```python
"""freshness fingerprints: record what a vector and a ranking describe

Revision ID: 0075
Revises: 0074
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("embedding_fingerprint", sa.Text(), nullable=True),
    )
    op.add_column(
        "call_rankings",
        sa.Column("profile_fingerprint", sa.Text(), nullable=True),
    )
    # Grandfather every ranking that predates this migration. 'legacy' means
    # "written before we tracked provenance, assumed current" -- reconcile
    # skips it only while cr.ranked_at >= p.updated_at, so an edit still
    # revokes it. A NULL AFTER this point means a bug and is treated as stale.
    #
    # Deliberately NOT done for profiles.embedding_fingerprint: NULL there
    # means stale, and every profile is re-embedded on the first tick. That
    # costs seconds against a local Ollama for tens of profiles, where a full
    # re-rank would be profile-count x 90 days of calls.
    op.execute("UPDATE call_rankings SET profile_fingerprint = 'legacy'")


def downgrade() -> None:
    op.drop_column("call_rankings", "profile_fingerprint")
    op.drop_column("profiles", "embedding_fingerprint")
```

- [ ] **Step 4: Add the ORM columns**

In `src/grant_lab/db/models/profiles.py`, immediately after the `embedding_model` line:

```python
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What `embedding` describes: "v1:" + sha256 of build_profile_text(self).
    # Moves with `embedding` and `embedding_model` in every branch of
    # refresh_profile_embedding. NULL means unknown -> the daily tick treats
    # the row as stale and re-embeds it.
    #
    # NOT covered by ck_profiles_embedding_model_paired, and must not be: on
    # deploy every existing row has a vector and a NULL fingerprint, so a
    # paired constraint would fail migration 0075.
    embedding_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
```

In `src/grant_lab/db/models/ranking.py`, immediately after the `analysis_model` line:

```python
    analysis_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which profile this ranking describes: "v1:" + sha256 of the canonical
    # rank payload, captured BEFORE rank_pair ran. 'legacy' marks a row that
    # predates migration 0075. NULL after 0075 means a bug, and reconcile
    # treats it as stale so the bug is loud rather than silent.
    profile_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 5: Regenerate the architecture map**

Run: `uv run python scripts/gen_architecture.py`
Expected: `docs/ARCHITECTURE.md` now lists both columns. Do not hand-edit it.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_migration_0075_freshness_fingerprints.py tests/architecture -v`
Expected: PASS, 3 new tests. The architecture tests confirm the generated map matches the source.

- [ ] **Step 6b: Prove the grandfathering gate can go red (mutation)**

Temporarily replace `op.execute("UPDATE call_rankings SET profile_fingerprint = 'legacy'")` with `pass`.

Run: `uv run pytest tests/test_migration_0075_freshness_fingerprints.py -v`
Expected: `test_existing_rankings_are_stamped_legacy` **FAILS** with `pre-0075 ranking was not grandfathered: None`. Judge on FAILED, not ERROR. If it stays green, the test is not driving alembic — fix the test, do not move on.

**Restore the `op.execute` line** and re-run to confirm green.

- [ ] **Step 6c: Lint, with `alembic` in the path**

Run: `uv run ruff check src tests alembic`
Expected: clean. `alembic` is NOT in Tasks 2-7's lint path, and this is the task that writes a migration — an unsorted import block here would otherwise surface seven tasks later.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0075_freshness_fingerprints.py \
        src/grant_lab/db/models/profiles.py \
        src/grant_lab/db/models/ranking.py \
        docs/ARCHITECTURE.md \
        tests/test_migration_0075_freshness_fingerprints.py
git commit -m "feat(freshness): two columns that say what a vector and a ranking describe"
```

---

### Task 2: The fingerprint module

**Files:**
- Create: `src/grant_lab/profiles/fingerprint.py`
- Create: `tests/freshness_helpers.py` — **every later task's tests import from this**
- Test: `tests/test_profile_fingerprint.py`

**Interfaces:**
- Consumes: `Profile.embedding_fingerprint` (Task 1); `build_profile_text` from `grant_lab.embeddings.similarity`.
- Produces:
  - `embedding_fingerprint(profile) -> str`
  - `rank_fingerprint(profile) -> str`
  - `FINGERPRINT_VERSION: str = "v1"`
  - `LEGACY_SENTINEL: str = "legacy"`

  Both return `"v1:" + <64 hex chars>`. Neither performs I/O or touches a session.

- [ ] **Step 0: Write the shared test helpers**

Create `tests/freshness_helpers.py`. **This is not optional scaffolding** — Tasks 3, 4, 5, 6 and 7 all import from it, and an earlier revision of this plan told each task to "reuse the existing helper", pointing at helpers that do not exist. Import style follows `tests/admin_retention_helpers.py`.

Three things this module gets right that a hand-rolled fake does not:

1. **`FakeEmbedder.session()`.** `_refresh_missing_embeddings_batch` opens the embed client as a context manager **twice per batch** (`refresh.py:299` for calls, `:335` for profiles). A fake without `session()` raises `AttributeError` the moment Task 4 calls the tick. Task 3's own tests do not catch this — they call `refresh_profile_embedding` directly, which never touches `session()`.
2. **`kind` must be `organizational_unit` or `individual`.** `db/models/profiles.py:69-72` enforces `CheckConstraint("kind IN ('organizational_unit','individual')")`. Anything else is an `IntegrityError` the moment a test commits.
3. **`ollama_clean()` instead of a `fake_rank_client` with a `produce_analysis` flag.** No such fake and no such attribute exists, and none is needed: the local Gemma path **never** produces analysis (`RankResult.summary` and `.applicant_notes` default to `None`, `ranking/rank_pair.py:54-55`), so every drain through this client already exercises the retained-analysis branch. Setting `produce_analysis = False` on an `OllamaClient` is a silent no-op.

```python
"""Shared helpers for the matching-freshness tests."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from grant_lab.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ranking.ollama_client import LLMError, OllamaClient

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(session, *, email: str | None = None) -> User:
    u = User(email=email or f"freshness-{_next()}@umu.se", status="active")
    session.add(u)
    session.flush()
    return u


def make_profile(session, *, description: str = "Liver disease", **kw) -> Profile:
    """A Profile owned by a fresh User.

    `kind` must be one of the two ck_profiles_kind allows -- an earlier
    revision of this plan used "research_group", which the CHECK rejects.
    """
    u = make_user(session)
    p = Profile(
        name=kw.pop("name", f"Group {_next()}"),
        kind=kw.pop("kind", ProfileKind.ORGANIZATIONAL_UNIT),
        description=description,
        structured=kw.pop("structured", {}),
        created_by=u.id,
        **kw,
    )
    session.add(p)
    session.flush()
    return p


def make_call(session, *, source_slug: str = "vinnova", **kw) -> Call:
    src = session.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    n = _next()
    c = Call(
        source_id=src.id,
        external_id=kw.pop("external_id", f"FRESH-{n}"),
        url=f"https://example.org/calls/fresh-{n}",
        title=kw.pop("title", "Precision medicine programme"),
        summary="Funding for AI-based diagnostics.",
        full_text="Body about eligibility, scope, budget.",
        # reconcile_missing_rankings filters on published_at >= now-90d and
        # excludes NULL published_at entirely.
        published_at=kw.pop("published_at", datetime.now(UTC)),
        **kw,
    )
    session.add(c)
    session.flush()
    return c


class FakeEmbedder:
    """Counts embed calls AND supplies session().

    _refresh_missing_embeddings_batch does `with embed_client.session() as http:`
    twice per batch. A fake without it raises AttributeError in Task 4.
    """

    def __init__(self, *, fail: bool = False, model: str = "bge-m3"):
        self.model = model
        self.fail = fail
        self.calls = 0

    def embed(self, text, _client=None):
        self.calls += 1
        if self.fail:
            raise LLMError("ollama down")
        return EmbedResult(vector=[0.1] * 1024, embedded_chars=len(text))

    @contextlib.contextmanager
    def session(self):
        yield None


def enqueue_job(session, *, call_id: int, profile_id: int) -> int:
    job = RankingJob(call_id=call_id, profile_id=profile_id, status=RankingJobStatus.PENDING)
    session.add(job)
    session.flush()
    return job.id


def ollama_clean() -> OllamaClient:
    """A real OllamaClient over an httpx.MockTransport returning a clean Gemma
    envelope -- the pattern tests/test_ranking_worker.py:63-113 uses."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "topic_fit": {
                                "rating": 4,
                                "rationale": "AI match",
                                "quoted_evidence": "AI",
                            },
                            "eligibility": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                            "exclusions": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                        }
                    ),
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "4",
                            "logprob": -0.1,
                            "top_logprobs": [
                                {"token": "4", "logprob": -0.1},
                                {"token": "3", "logprob": -2.0},
                            ],
                        },
                    ]
                },
            }
        ],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    return OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_profile_fingerprint.py`. The round-trip test needs a real Postgres session: there is **no `session` fixture** — open one from `pg_session_factory`. `Profile.created_by` is a real FK, so seed a `User` first.

```python
import json

from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.profiles.fingerprint import (
    FINGERPRINT_VERSION,
    embedding_fingerprint,
    rank_fingerprint,
)


def _profile(**kw):
    # kind MUST be one of the two ck_profiles_kind allows. "research_group"
    # passes the seven in-memory tests (no flush) and then dies with
    # IntegrityError in the one round-trip test that matters.
    base = dict(
        name="Hepatology",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="Liver disease",
        structured={},
        budget_currency="SEK",
        created_by=1,
    )
    base.update(kw)
    return Profile(**base)


def test_both_fingerprints_carry_the_version_tag():
    p = _profile()
    assert embedding_fingerprint(p).startswith(f"{FINGERPRINT_VERSION}:")
    assert rank_fingerprint(p).startswith(f"{FINGERPRINT_VERSION}:")


def test_embedding_fingerprint_tracks_the_text_builder_output():
    """Hashing build_profile_text's OUTPUT, not its inputs, is the point:
    editing that function must invalidate every vector automatically."""
    a = _profile(description="Liver disease")
    b = _profile(description="Liver  disease")  # different text, same fields
    assert embedding_fingerprint(a) != embedding_fingerprint(b)


def test_rank_fingerprint_covers_budget_and_trl():
    """Rev 1's enumeration omitted these; criteria.py:111 and :129 read them
    straight off `structured`. Hashing the whole blob is why this passes."""
    a = _profile(structured={"areas": ["liver"], "budget_min": 100})
    b = _profile(structured={"areas": ["liver"], "budget_min": 200})
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_rank_fingerprint_covers_the_embedding_fingerprint():
    """Catches a build_profile_text edit, which changes no text field."""
    a = _profile()
    a.embedding_fingerprint = "v1:aaa"
    b = _profile()
    b.embedding_fingerprint = "v1:bbb"
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_key_order_does_not_change_the_fingerprint():
    a = _profile(structured={"zebra": 1, "apple": 2})
    b = _profile(structured={"apple": 2, "zebra": 1})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_integral_floats_and_ints_hash_the_same():
    """json.dumps(1) != json.dumps(1.0). Postgres may hand back either.
    Without canonicalization this alone re-ranks the corpus daily."""
    a = _profile(structured={"budget_min": 1000})
    b = _profile(structured={"budget_min": 1000.0})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_empty_collections_are_omitted():
    a = _profile(structured={"areas": ["liver"], "must_include": []})
    b = _profile(structured={"areas": ["liver"]})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_survives_a_real_postgres_round_trip(pg_session_factory):
    """GATE 1. An unstable fingerprint marks every profile permanently stale
    and re-ranks the whole corpus on every daily tick, presenting as
    unexplained GPU load rather than as an error.

    `endowment` is load-bearing. `1000.0` does NOT change across the trip --
    measured: Postgres hands back exactly what json.dumps wrote, and
    `json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)`
    is True for a fixture without it. Only a float json.dumps writes in
    EXPONENT form flips: Postgres numeric normalizes 1e+16 to
    10000000000000000 and json.loads reads that back as an INT. Without
    `endowment` this gate exercises nothing but sort_keys=True, and stays
    GREEN with the whole numeric rule deleted.
    """
    structured = {
        "zebra": {"nested_z": 1, "nested_a": 2},
        "apple": [3, 2, 1],
        "budget_min": 1000.0,
        "endowment": 1e16,
        "must_include": [],
    }
    with pg_session_factory() as session:
        u = User(email="fp-roundtrip@umu.se", status="active")
        session.add(u)
        session.flush()
        p = _profile(structured=structured, created_by=u.id)
        before = rank_fingerprint(p)
        raw_before = json.dumps(p.structured, sort_keys=False)

        session.add(p)
        session.commit()
        pid = p.id
        session.expunge_all()

        loaded = session.get(Profile, pid)
        raw_after = json.dumps(loaded.structured, sort_keys=False)

        # PRECONDITION: if these are already identical, this test proves nothing.
        assert raw_before != raw_after, (
            "fixture too simple: the round trip changed nothing, so this test "
            "cannot detect an unstable canonicalizer"
        )

        assert rank_fingerprint(loaded) == before
```

**Which gate matters most.** An earlier revision called gate 1 "the one that matters
most" on the grounds that "Postgres hands numbers back in whatever representation it
stored". Measured, that is false for every realistic value: Postgres hands back what
`json.dumps` wrote. The asymmetry that genuinely needs the numeric rule is the *other*
one this plan names — `web/api/profiles.py:438` parses the incoming budget with
`bounded_int_or_400(...)`, which always yields an **`int`**, so a profile whose stored
`structured["budget_min"]` is a float `1000.0` compares DB-loaded-`1000.0` against
in-memory-`1000` on every PUT. **Task 8 gate 3 is the load-bearing one**; gate 1 above
is a useful stability guard that only bites once `endowment` is in the fixture. Keep
both; do not treat gate 1 passing as proof the canonicalizer works.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_profile_fingerprint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grant_lab.profiles.fingerprint'`.

- [ ] **Step 3: Write the module**

```python
"""Fingerprints that say what a stored embedding or ranking was built from.

DERIVED, never stored as a claim about "current". There are four profile-text
write sites (profiles/create.py:25, :51, the constructor at :68-75, and
web/api/profiles.py:481 + :518); a fifth that forgot to maintain a stored hash
would assert "fresh" about a stale row. Recomputing from the row cannot rot.

Both functions return "v1:<sha256 hex>". The version tag is not decoration:
any later change to a hashed payload makes EVERY stored fingerprint mismatch
at once and re-ranks the entire corpus on the next tick, with no operator
signal. Bump the version and ship an explicit decision -- either "yes, re-rank
everything" or a skip rule for the old version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from grant_lab.embeddings.similarity import build_profile_text

FINGERPRINT_VERSION = "v1"
LEGACY_SENTINEL = "legacy"


def _canonical(value: Any) -> Any:
    """Normalize a JSONB round-trip so equal content hashes equally.

    Postgres does not preserve key order, and hands numbers back in whatever
    representation it stored: json.dumps(1) is "1" and json.dumps(1.0) is
    "1.0". The profile-update endpoint compares a DB-loaded row against a
    freshly built Python dict (web/api/profiles.py:480 vs :520), so the two
    sides genuinely differ in representation. expire_on_commit=False
    (db/session.py:58) means committing does NOT reload the row, so this
    cannot be fixed by moving a line -- it is fixed here.
    """
    if isinstance(value, bool):
        return value  # bool before int: bool IS an int in Python
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            cv = _canonical(v)
            if cv is None or cv == [] or cv == {} or cv == "":
                continue  # omit-vs-null-vs-empty must not change the hash
            out[str(k)] = cv
        return out
    if isinstance(value, list):
        return [_canonical(v) for v in value]  # list ORDER is meaningful
    return value


def _digest(payload: Any) -> str:
    blob = json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def embedding_fingerprint(profile) -> str:
    """What the stored `embedding` describes.

    Hashes build_profile_text's OUTPUT, not its inputs. That function's own
    docstring says changing it changes every profile's vector and that callers
    "must re-embed profiles for the new text to take effect" -- a manual step
    performed by hand once already. Hashing the output makes it automatic.
    """
    return _digest(build_profile_text(profile))


def rank_fingerprint(profile) -> str:
    """What a stored ranking describes.

    The WHOLE `structured` blob, not an enumerated subset. An earlier revision
    enumerated the keys the ranker reads and was already wrong: it omitted
    budget_min/max and trl_min/max, which ranking/criteria.py:111 and :129 read
    straight off `structured`. An enumeration is a list maintained in step with
    a pipeline it does not live next to. It rots, and the symptom is a ranking
    that is stale forever.

    `embedding_fingerprint` is in the payload so that editing build_profile_text
    -- which changes no text field -- still invalidates rankings. It costs no
    extra re-rank round because the worker defers a pair whose vector is stale
    (ranking/worker.py), so a ranking is only ever written once the vector has
    settled.

    `name` and `kind` are insurance, not cost: they reach the LLM prompt at
    ranking/prompt.py:218-219, but ProfileUpdateIn (web/api/profiles.py:399-423)
    has 22 fields and neither is among them, and nothing assigns either after
    profiles/create.py:68-75.
    """
    return _digest(
        {
            "description": profile.description or "",
            "structured": profile.structured or {},
            "budget_currency": profile.budget_currency,
            "name": profile.name,
            "kind": getattr(profile.kind, "value", profile.kind),
            "embedding_fingerprint": profile.embedding_fingerprint,
        }
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_profile_fingerprint.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Prove gate 1 can go red (mutation)**

Temporarily remove numeric canonicalization — delete these two lines from `_canonical`:

```python
    if isinstance(value, float) and value.is_integer():
        return int(value)
```

Run: `uv run pytest tests/test_profile_fingerprint.py -v`
Expected: **TWO failures** — `test_integral_floats_and_ints_hash_the_same` AND `test_survives_a_real_postgres_round_trip`. The dry run measured `2 failed, 7 passed` against a 9-test file that also carried one evidence test this plan does not keep; here that is `2 failed, 6 passed`. **Measure it; do not adjust the number to close the arithmetic.**

If the round trip stays green, `endowment` is missing from the fixture and the gate is exercising nothing but `sort_keys=True`. That is a defect in the gate, not a reason to move on — add the key and re-run before continuing.

**Restore the two lines** and re-run to confirm green before committing.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/grant_lab/profiles/fingerprint.py tests/freshness_helpers.py tests/test_profile_fingerprint.py
git commit -m "feat(freshness): derived fingerprints, canonical across a JSONB round trip"
```

---

### Task 3: Write site — embeddings

**Files:**
- Modify: `src/grant_lab/embeddings/refresh.py` (`refresh_profile_embedding`, lines 108-141)
- Test: `tests/test_embeddings_refresh_fingerprint.py` (flat — there is no `tests/embeddings/`)

**Interfaces:**
- Consumes: `embedding_fingerprint` (Task 2); `Profile.embedding_fingerprint` (Task 1).
- Produces: after a successful refresh, `profile.embedding_fingerprint == embedding_fingerprint(profile)`. Tasks 4 and 6 rely on that invariant.

The fingerprint travels with the vector and the model in **every** branch:

| Branch | `embedding` | `embedding_model` | `embedding_fingerprint` |
|---|---|---|---|
| text blank -> invalidate | `None` | `None` | `None` |
| `LLMError`, `force=True` | `None` | `None` | `None` |
| `LLMError`, `force=False` | untouched | untouched | **untouched** |
| success | vector | model | `embedding_fingerprint(profile)` |

The `force=False` failure row is the whole point of the review's "normal failure case": the old vector is preserved and the fingerprint keeps describing it *truthfully*, so the row is now detectably stale. Leaving it untouched is the fix, not an omission.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embeddings_refresh_fingerprint.py`. Use `FakeEmbedder` and `make_profile` from `tests/freshness_helpers.py` (Task 2) — do **not** hand-roll a fake here. The one Task 3 could get away with (its tests call `refresh_profile_embedding` directly, which never touches `session()`) is exactly the one that breaks Task 4.

```python
"""Task 3 gates: the fingerprint travels with the vector in every branch."""

from __future__ import annotations

from grant_lab.db.models import Profile
from grant_lab.embeddings.refresh import refresh_profile_embedding
from grant_lab.profiles.fingerprint import embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_profile


def test_success_stamps_the_fingerprint(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_blank_text_clears_all_three(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}
        s.commit()
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_force_failure_clears_all_three(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(fail=True), pid, force=True)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_non_force_failure_leaves_all_three_untouched(pg_session_factory):
    """The review's 'normal failure case'. The old vector survives and the
    fingerprint keeps describing it truthfully, so the row is DETECTABLY
    stale rather than silently wrong."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        fp_before = p.embedding_fingerprint
        p.description = "Completely different subject"
        s.commit()
        refresh_profile_embedding(s, FakeEmbedder(fail=True), pid, force=False)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is not None
        assert p.embedding_model == "bge-m3"
        assert p.embedding_fingerprint == fp_before
        # ...and that is now provably stale:
        assert p.embedding_fingerprint != embedding_fingerprint(p)
```

**Two of these four are regression guards, not gates.** Measured on the pre-implementation run (`2 failed, 2 passed`): `test_blank_text_clears_all_three` and `test_force_failure_clears_all_three` pass **before** the implementation, because `embedding_fingerprint` is already NULL there and `assert ... is None` holds for free. Only `test_success_stamps_the_fingerprint` and `test_non_force_failure_leaves_all_three_untouched` drive the change. Keep all four — a regression guard is worth having — but do not read their green as evidence.

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_embeddings_refresh_fingerprint.py -v`
Expected: `2 failed, 2 passed` — `assert None == 'v1:...'` on the success test, and the non-force test. The other two pass for free (see above).

- [ ] **Step 3: Implement**

In `src/grant_lab/embeddings/refresh.py`, add the import at the top with the other `grant_lab` imports:

```python
from grant_lab.profiles.fingerprint import embedding_fingerprint
```

In the blank-text branch (currently lines 113-115), add the third assignment:

```python
        if (
            profile.embedding is not None
            or profile.embedding_model is not None
            or profile.embedding_fingerprint is not None
        ):
            profile.embedding = None
            profile.embedding_model = None
            profile.embedding_fingerprint = None
```

In the `LLMError` handler's force branch (currently lines 134-136):

```python
        if force:
            profile.embedding = None
            profile.embedding_model = None
            profile.embedding_fingerprint = None
        return False
```

In the success path (currently lines 138-140):

```python
    profile.embedding = result.vector
    profile.embedding_model = embed_client.model
    profile.embedding_fingerprint = embedding_fingerprint(profile)
    return True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_embeddings_refresh_fingerprint.py tests/test_embeddings_refresh.py tests/test_embeddings_similarity.py tests/test_embeddings_cli.py -v`
Expected: PASS, 4 new tests plus the pre-existing embedding tests unchanged. **Task 3 breaks nothing** — the pre-existing test that Task 4 breaks is still green here, because Task 3 only touches the write site.

No import cycle is introduced: `grant_lab/profiles/__init__.py` is **empty**, so `embeddings.refresh -> profiles.fingerprint -> embeddings.similarity` does not re-enter `embeddings/__init__`. `tests/architecture` stays green (192 passed).

- [ ] **Step 5: Prove the non-force gate bites (mutation)**

Temporarily add `profile.embedding_fingerprint = None` inside the `LLMError` handler *before* the `if force:` line, so it clears on both paths.

Run: `uv run pytest tests/test_embeddings_refresh_fingerprint.py -v`
Expected: `1 failed, 3 passed` — `test_non_force_failure_leaves_all_three_untouched` FAILS. Judge on FAILED, not ERROR.

**Revert the added line** and re-run to confirm `4 passed`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/embeddings/refresh.py tests/test_embeddings_refresh_fingerprint.py
git commit -m "feat(freshness): the fingerprint travels with the vector in every branch"
```

---

### Task 4: The embedding repair arm

**Files:**
- Modify: `src/grant_lab/embeddings/refresh.py` — `_refresh_missing_embeddings_batch` (profile phase, lines 318-345) and `refresh_missing_embeddings_tick` (drain loop, lines 200-232)
- Test: `tests/test_embeddings_refresh_tick_staleness.py` (flat)
- Amend: `tests/test_embeddings_refresh.py::test_refresh_missing_tick_force_false_default_skips_already_embedded` — see step 5

**Interfaces:**
- Consumes: `embedding_fingerprint` (Task 2); the write-site invariant (Task 3).
- Produces: `_refresh_missing_embeddings_batch` returns a summary dict with a new key **`profiles_seen: int`** — rows *examined*, not rows worked. The drain loop breaks on that.

**This is the change rev 1 got backwards.** The drain loop at `refresh.py:222-231` breaks on `processed == 0`, where `processed` counts rows *embedded or failed*, and the break happens **before** the cursor is carried forward:

```python
if processed == 0:
    break
cursor_profile_id = summary["max_profile_id"]   # never reached
```

Once the candidate set is widened, a page containing nothing stale yields `processed == 0`, stops the drain, and discards the advanced cursor. Every later tick restarts at `after_profile_id=0` and halts at the same wall, so a stale profile past the first page is never repaired. Rev 1 called the risk "preserve termination". The danger is stopping *too early*.

Three coupled changes: (a) widen, (b) fix the drain, (c) stop excluding blank-but-populated profiles.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embeddings_refresh_tick_staleness.py`. There is no `session`, `session_factory`, `make_profile` or `fake_embedder` fixture — use `pg_session_factory` directly and the Task 2 helpers. The sketches below use `pg_session_factory` in place of the old plan's `session_factory`/`session` pair; open your own sessions the way Task 3's tests do.

```python
"""Task 4 gates: the daily tick breaks on rows examined, not rows worked."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from grant_lab.db.models import Profile
from grant_lab.embeddings.refresh import (
    _refresh_missing_embeddings_batch,
    refresh_missing_embeddings_tick,
)
from grant_lab.profiles.fingerprint import embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_profile


@pytest.fixture()
def fake_embedder():
    """FakeEmbedder counts embed calls (gate 7) AND supplies session(), which
    _refresh_missing_embeddings_batch opens twice per batch."""
    return FakeEmbedder()


def test_a_changed_text_is_stale_even_with_a_current_vector_and_model(
    pg_session_factory, fake_embedder
):
    """GATE 4. Vector present, model current -- invisible to the old predicate."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint == embedding_fingerprint(p)
        p.description = "Cardiac imaging"
        s.commit()
        stale_fp = p.embedding_fingerprint

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint != stale_fp
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_stale_profile_behind_a_full_fresh_page_is_still_reached(
    pg_session_factory, fake_embedder
):
    """GATE 5 -- the one that would have caught rev 1's mistake.

    Uses batch_size=2 rather than a realistic corpus, so the loop's exit
    condition is what is under test, not the row count.
    """
    with pg_session_factory() as s:
        for i in range(4):
            make_profile(s, description=f"Fresh {i}")
        s.commit()
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )

    with pg_session_factory() as s:
        stale = make_profile(s, description="Originally this")
        s.commit()
        sid = stale.id
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        assert s.get(Profile, sid).embedding is not None
        # Now make the HIGHEST id stale, behind two full pages of fresh rows.
        s.get(Profile, sid).description = "Changed to something else"
        s.commit()

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        p = s.get(Profile, sid)
        assert p.embedding_fingerprint == embedding_fingerprint(p), (
            "the drain stopped at the first page with nothing to do and threw away its cursor"
        )


def test_a_profile_cleared_to_blank_is_invalidated_by_the_tick(pg_session_factory, fake_embedder):
    """GATE 8 -- hole 4, a defect in already-shipped code.

    refresh.py:328 excluded blank profiles from the tick, so the branch that
    wipes a cleared profile's vector could only ever fire from the request
    path -- the lossy one.
    """
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        assert s.get(Profile, pid).embedding is not None
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}
        s.commit()

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_an_already_blank_and_already_cleared_profile_is_not_rescanned_forever(
    pg_session_factory, fake_embedder
):
    """The other half of gate 8: including blank rows must not break termination."""
    with pg_session_factory() as s:
        p = make_profile(s, description="")
        p.structured = {}
        s.commit()
    summary = _refresh_missing_embeddings_batch(
        session_factory=pg_session_factory,
        embed_client=fake_embedder,
        batch_size=200,
        force=False,
        after_call_id=0,
        after_profile_id=0,
    )
    assert summary["profiles_embedded"] == 0
    assert summary["profiles_failed"] == 0


def test_force_mode_ignores_the_fingerprint(pg_session_factory, fake_embedder):
    """GATE 7. force=True must re-embed every embeddable row regardless."""
    with pg_session_factory() as s:
        make_profile(s, description="Liver disease")
        s.commit()
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    fake_embedder.calls = 0

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, force=True
    )
    assert fake_embedder.calls > 0, "force mode skipped a fingerprint-fresh row"


def test_the_staleness_decision_does_not_load_the_vector(pg_session_factory, fake_embedder):
    """GATE 6. Parsing 1024-dim vectors is the measured dominant cost of a
    profile load; loading them to decide whether to replace them is a
    regression. Assert on the emitted SQL of the CANDIDATE SELECT.
    """
    import re

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with pg_session_factory() as session:
        engine = session.bind
        event.listen(engine, "before_cursor_execute", _record)
        try:
            make_profile(session, description="Liver disease")
            session.commit()
            statements.clear()
            _refresh_missing_embeddings_batch(
                session_factory=pg_session_factory,
                embed_client=fake_embedder,
                batch_size=200,
                force=False,
                after_call_id=0,
                after_profile_id=0,
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    # Narrow to the paged CANDIDATE scan. A filter of
    # `"FROM profiles" in s and "SELECT" in s` also catches the
    # `session.get(Profile, pid)` that refresh_profile_embedding must issue
    # for the row it is about to embed -- which loads the vector by necessity.
    candidate = [s for s in statements if "ORDER BY profiles.id" in s and "LIMIT" in s]
    assert len(candidate) == 1, f"expected exactly one candidate scan, got {len(candidate)}"
    sql = candidate[0]

    # Look ONLY at the result columns. `re.split` on a FROM word boundary, not
    # `sql.split(" FROM ")`: the compiled SQL puts a newline before FROM, so
    # the space-delimited form silently returns the WHOLE statement and every
    # position-sensitive assertion below then reads the WHERE clause too.
    select_list = re.split(r"\bFROM\b", sql, maxsplit=1)[0]

    # The vector must not be a result column in ANY shape the ORM emits it:
    #   `profiles.embedding,`        mid-list
    #   `profiles.embedding`         last, no trailing comma
    #   `profiles.embedding AS ...`  aliased (what session.get emits)
    # The two lookaheads exclude the three things that legitimately start with
    # the same prefix: profiles.embedding_model, profiles.embedding_fingerprint,
    # and `profiles.embedding IS NULL AS embedding_is_null` -- the computed
    # flag, which is exactly what this gate wants to see instead.
    leaked = re.findall(r"profiles\.embedding(?!_)(?!\s+IS\s+NULL)", select_list)
    assert not leaked, f"the candidate SELECT loaded the 1024-dim vector:\n{sql}"
    assert "embedding_is_null" in select_list, (
        f"no computed NULL flag in the candidate scan:\n{sql}"
    )
```

**Gate 6 was rewritten because the earlier version could not discriminate.** Run verbatim
against this plan's own Step-3 implementation it FAILED, for two independent reasons:

1. `Profile.embedding.is_(None).label("embedding_is_null")` renders as
   `profiles.embedding IS NULL AS embedding_is_null`, which contains the substring
   `"profiles.embedding "` — the old gate's own third pattern. **The implementation
   tripped its own assertion.**
2. The filter `"FROM profiles" in s and "SELECT" in s` also caught
   `session.get(Profile, pid)` inside `refresh_profile_embedding`, which loads the
   vector by necessity — so the gate failed in every test where a profile is actually
   embedded.

Failing on both the correct and the wrong implementation is not a strict gate, it is a
broken one.

**And the first correction was still half-hollow.** Matching on `"profiles.embedding AS"`
looked right but that string **never appears in the candidate scan at all** — the ORM
only aliases columns when selecting a whole entity, which is what `session.get` does, not
what a column-limited `select(...)` does. Measured, the paged scan emits
`profiles.embedding,` bare. So that version caught `select(Profile)` only by accident,
via its *other* assertion (`embedding_is_null` is absent there), and stayed **green** on
the likelier regression — someone adding `Profile.embedding` back to the column list
beside the flag. The regex above was checked against all four shapes:

| Candidate scan | Old `"... AS"` gate | Gate above |
|---|---|---|
| correct (flag only) | green | **green** |
| `Profile.embedding` added mid-list | **green — missed it** | RED |
| `Profile.embedding` added last (no trailing comma) | **green — missed it** | RED |
| wholesale `select(Profile)` | RED (by accident, via the flag assertion) | RED |
| flag dropped entirely | RED | RED |

The `ORDER BY profiles.id` + `LIMIT` filter that narrows to the candidate scan is
load-bearing in its own right: `session.get(Profile, pid)` **does** emit
`profiles.embedding AS profiles_embedding`, so without the narrowing this gate would fail
on every test where a profile is actually embedded.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_embeddings_refresh_tick_staleness.py -v`
Expected: FAIL — gate 4 and gate 8 fail because the SQL predicate never selects those rows; gate 5 fails because the drain breaks early. Judge on FAILED. If you see ERRORs instead, `tests/freshness_helpers.py` is missing or misnamed.

- [ ] **Step 3: Widen the candidate query and decide in Python**

Replace the profile-candidate block in `_refresh_missing_embeddings_batch` (currently lines 318-333). The `if not force:` SQL narrowing goes away; a column-limited SELECT replaces `select(Profile)`.

```python
    with session_factory() as session:
        # The staleness decision moved into Python because build_profile_text
        # is Python over JSONB and cannot be evaluated in SQL.
        #
        # Do NOT select Profile wholesale: `p.embedding is None` on a loaded
        # Profile pulls the 1024-dim vector, which is the measured dominant
        # cost of a profile load. Select a computed boolean instead.
        profile_q = (
            select(
                Profile.id,
                Profile.description,
                Profile.structured,
                Profile.embedding_model,
                Profile.embedding_fingerprint,
                Profile.embedding.is_(None).label("embedding_is_null"),
            )
            .where(Profile.id > after_profile_id)
            .order_by(Profile.id)
            .limit(batch_size)
        )
        rows = session.execute(profile_q).all()
        summary["profiles_seen"] = len(rows)

        profile_ids = []
        for r in rows:
            text_now = build_profile_text(r)
            if force:
                # Force mode re-embeds every embeddable row, fingerprint or not.
                if text_now.strip():
                    profile_ids.append(r.id)
                continue
            if not text_now.strip():
                # Hole 4: a profile cleared to blank while it still carries
                # embedding state MUST reach refresh_profile_embedding, which
                # is the only thing that wipes the vector. Previously these
                # were filtered out, so the invalidation branch could only
                # ever fire from the lossy request path. An already-cleared
                # blank row is still skipped, so the scan still terminates.
                if (
                    not r.embedding_is_null
                    or r.embedding_model is not None
                    or r.embedding_fingerprint is not None
                ):
                    profile_ids.append(r.id)
                continue
            if (
                r.embedding_is_null
                or r.embedding_model != current_model
                or r.embedding_fingerprint != _digest_for_row(r)
            ):
                profile_ids.append(r.id)

        if rows and not profile_ids:
            # Paged past the end of the work set -- advance the cursor anyway
            # so the next iteration does not re-read the same rows.
            summary["max_profile_id"] = max(r.id for r in rows)
```

`build_profile_text` and `embedding_fingerprint` duck-type on attribute access — verified: `embeddings/similarity.py:86-99` reads **only** `profile.description` and `profile.structured`, both of which a SQLAlchemy `Row` exposes by name, and all six gates pass with `build_profile_text(r)` called on the `Row` directly. **`rank_fingerprint` does not duck-type onto this Row**: it also needs `name`, `kind` and `budget_currency`, none of which are selected here. Nothing in Task 4 calls it on a Row; Task 7 selects its own columns for that. Add a tiny helper beside the batch function so the intent is explicit:

```python
def _digest_for_row(row) -> str:
    """embedding_fingerprint() over a column-limited Row rather than a Profile.

    Both build_profile_text and embedding_fingerprint duck-type on
    `.description` and `.structured`, which the Row exposes. This exists so
    the call site reads as a deliberate choice rather than an accident.
    """
    return embedding_fingerprint(row)
```

- [ ] **Step 4: Fix the drain loop**

Initialise the new key where the other summary keys are initialised in `_refresh_missing_embeddings_batch`:

```python
        "profiles_seen": 0,
```

Then in `refresh_missing_embeddings_tick`, add `"profiles_seen"` to the accumulate tuple, and replace the break condition (currently lines 222-232):

```python
        # Break on rows EXAMINED, not rows worked. A page in which nothing was
        # stale is progress, not exhaustion: breaking on "processed == 0" here
        # would discard the advanced cursor two lines below, and every later
        # tick would restart at after_profile_id=0 and halt at the same wall.
        # Once the candidate set is widened, that page is the common case.
        examined = (
            summary["calls_embedded"]
            + summary["calls_failed"]
            + summary["profiles_seen"]
        )
        if examined == 0:
            break
        cursor_call_id = summary["max_call_id"]
        cursor_profile_id = summary["max_profile_id"]
```

- [ ] **Step 5: Run the tests — and amend the one pre-existing test this task breaks**

Run: `uv run pytest tests/test_embeddings_refresh_tick_staleness.py tests/test_embeddings_refresh.py -v`

Expected **before** the amendment below:

```
FAILED tests/test_embeddings_refresh.py::test_refresh_missing_tick_force_false_default_skips_already_embedded
E   assert 1 == 0            # summary["profiles_embedded"]
1 failed, 41 passed
```

That test (`tests/test_embeddings_refresh.py:298`) seeds a profile with a vector and a
matching `embedding_model` and asserts `summary["profiles_embedded"] == 0`, under the
docstring "default behavior (force=False) is **byte-identical to today**". This task
changes exactly that: a NULL `embedding_fingerprint` is stale by design, which is how
the first tick after deploy refills the corpus. **The new behaviour is correct**; the
old expectation is what must change. An earlier revision of this plan promised "PASS,
including the pre-existing embedding tests", so a worker following it literally would
treat a correct implementation as broken.

Amend it — one seeded field and a docstring, in this same commit:

```python
def test_refresh_missing_tick_force_false_default_skips_already_embedded(pg_session_factory):
    """Regression guard: default behavior (force=False) does not re-embed a row
    that is already current.

    Migration 0075 changed what "current" MEANS for a profile: a vector and a
    matching model are no longer enough, the embedding_fingerprint must also
    describe the row's present text. A NULL fingerprint is stale by design (it
    is how the first tick after deploy re-embeds the corpus), so this seed now
    stamps the fingerprint too. The CALL half is unchanged -- calls have no
    fingerprint column.
    """
    from grant_lab.profiles.fingerprint import embedding_fingerprint

    with pg_session_factory() as s:
        cid = _seed_call(s, title="ForceFalse1")
        s.flush()
        s.get(Call, cid).embedding = [0.9] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        pid = _seed_profile(s, name="ForceFalse2")
        s.flush()
        prof = s.get(Profile, pid)
        prof.embedding = [0.9] * 1024
        prof.embedding_model = "bge-m3"
        prof.embedding_fingerprint = embedding_fingerprint(prof)
        s.commit()
    # ...rest of the test unchanged
```

The assertion is not weakened: it still asserts "a CURRENT artefact is not redone",
under the new definition of current. **This is the only pre-existing embedding test that
changes.** If a second one goes red, that is a finding — record it in
`PLAN-DEFECTS.md`, do not quietly reseed it.

Re-run. Expected: PASS.

- [ ] **Step 6: Prove gate 5 bites (mutation)**

Temporarily restore the old break:

```python
        examined = (
            summary["calls_embedded"]
            + summary["calls_failed"]
            + summary["profiles_embedded"]
            + summary["profiles_failed"]
        )
```

Run: `uv run pytest tests/test_embeddings_refresh_tick_staleness.py -v`
Expected: `1 failed, 5 passed` — `test_a_stale_profile_behind_a_full_fresh_page_is_still_reached` FAILS. Verified by mutation in the dry run.

**Restore the fix** and re-run.

- [ ] **Step 7: Note the remaining bound in the plan's defects file, not in code**

`_TICK_MAX_BATCHES = 200` (`refresh.py:147`) still caps the drain, and a capped tick restarts at `after_profile_id=0` tomorrow rather than resuming. At 200 batches x 200 rows the headroom is 40,000 profiles, so at this project's scale (tens) it cannot bite. It is a real bound, not a bug today.

**What this task changes about that bound:** the widened candidate set means `profiles_seen` now counts *every* profile, not only stale ones, so the drain walks the whole table each tick. The headroom is consumed by total profile count rather than by backlog size. Still 40,000; still fine at this scale. Record both halves in `PLAN-DEFECTS.md` as a follow-up rather than growing this task.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/embeddings/refresh.py \
        tests/test_embeddings_refresh_tick_staleness.py \
        tests/test_embeddings_refresh.py
git commit -m "fix(freshness): the daily tick breaks on rows examined, not rows worked"
```

---

### Task 5: Write site — rankings

**MUST land before Task 7.** Task 7 treats a NULL `profile_fingerprint` as stale. This task is what makes the worker write a non-NULL one. Reversed, every freshly written ranking is immediately re-enqueued — an infinite re-rank loop.

**Files:**
- Modify: `src/grant_lab/ranking/worker.py` — capture immediately before `result = rank_pair(` (line 169), write inside the `CallRanking(...)` at the `session.merge(` call (line 245), retained-analysis guard at lines **238-243** (238 is `if new_summary is None and new_notes is None:`, 243 is `new_analysis_model = existing.analysis_model`; 244 is blank and 245 is `session.merge(`)
- Test: `tests/test_ranking_worker_fingerprint.py` (flat)

**Interfaces:**
- Consumes: `rank_fingerprint` (Task 2); `CallRanking.profile_fingerprint` (Task 1).
- Produces: every `CallRanking` the worker writes carries `profile_fingerprint = rank_fingerprint(profile)` as computed **before** `rank_pair` ran. Task 7 compares against exactly this value.

**The rationale is robustness, not a live bug.** An earlier revision claimed a concurrent edit during `rank_pair` would be mis-stamped. That is false here: `profile` is loaded at `worker.py:131` and the session is not committed, refreshed or expired before `:245` — the ledger write at `:203-211` uses a separate session on purpose — so hashing at either point returns the same value today. Capture early because it is unambiguous and survives a future refactor that introduces a commit or a `session.refresh` in between. The gate below is written against that real failure, not the imaginary one.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranking_worker_fingerprint.py`. There is no `session`, `session_factory`, `make_call`, `make_profile`, `fake_rank_client` or `ranked_pair` fixture — define the first three from `pg_session_factory` and the Task 2 helpers, at the top of the file:

```python
"""Task 5 gates: a ranking records the profile the ranker actually saw."""

from __future__ import annotations

import pytest

from grant_lab.db.models import CallRanking, Profile
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking.queue import enqueue_for_pair
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import enqueue_job, make_call, make_profile, ollama_clean


@pytest.fixture()
def session_factory(pg_session_factory):
    return pg_session_factory


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


@pytest.fixture()
def fake_rank_client():
    """A real OllamaClient over an httpx.MockTransport. There is NO fake with
    a `produce_analysis` knob, and none is needed -- the local Gemma path
    never produces summary/applicant_notes, so every drain through this
    client already exercises the retained-analysis branch."""
    return ollama_clean()


@pytest.fixture()
def ranked_pair(session, session_factory, fake_rank_client):
    """(call_id, profile_id, profile) with the pair already ranked once."""
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )
    return c.id, p.id, p
```

Tasks 6 and 7 need the same three `session`/`session_factory`/`fake_rank_client` fixtures; copy them into each file (they are four lines each, and a shared `conftest.py` would widen their blast radius beyond these three files).

**Verified names for every test in Tasks 5-7:** the worker entry point is
`drain_queue(*, session_factory, client, embedding_threshold, ...)` in
`ranking/worker.py:31` — there is no `run_ranking_batch`. `LLMError` lives in
`grant_lab.ranking.ollama_client:10`, not in an `llm.errors` module.
`RankingJobStatus` is exported from `grant_lab.db.models`. `enqueue_for_pair` is
`(session, *, call_id, profile_id)`. `embedding_threshold` has no default and must
be passed.

```python
def test_the_worker_stamps_the_fingerprint_it_ranked(session, ranked_pair):
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    assert cr.profile_fingerprint == rank_fingerprint(profile)
    assert cr.profile_fingerprint.startswith("v1:")


def test_the_stamp_is_captured_before_rank_pair_not_after(session, session_factory):
    """GATE 9, written so it can discriminate.

    The edit must land MID-FLIGHT -- between the worker's
    `session.get(Profile, ...)` at worker.py:131 and its merge -- so the rank
    client itself commits it from a separate session inside the
    httpx.MockTransport handler. Capture-before stamps the pre-call hash
    (correct); capture-after-a-refresh stamps the mid-flight hash (the bug).
    Nothing today refreshes, so this is a forward guard.
    """
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    pid, cid = p.id, c.id
    before = rank_fingerprint(p)

    client = ollama_clean()
    original = client._transport.handler

    def edit_then_answer(request):
        with session_factory() as mid:
            row = mid.get(Profile, pid)
            if row.description != "EDITED MID FLIGHT":
                row.description = "EDITED MID FLIGHT"
                mid.commit()
        return original(request)

    client._transport.handler = edit_then_answer

    drain_queue(
        session_factory=session_factory,
        client=client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        row = fresh.get(Profile, pid)
        assert row.description == "EDITED MID FLIGHT", "the mid-flight edit did not land"
        assert rank_fingerprint(row) != before
        cr = fresh.get(CallRanking, (cid, pid))
        assert cr.profile_fingerprint == before, (
            "the worker stamped a profile the ranker never saw"
        )


def test_retained_analysis_is_dropped_when_the_profile_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """GATE 10. worker.py:238-243 copies the previous run's summary, notes and
    analysis_model when this run produced none. After a profile edit that
    retained advice describes the OLD profile, and stamping it with the NEW
    fingerprint would make it permanently invisible to repair."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Old advice about liver disease"
    cr.applicant_notes = [{"note": "old"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    with session_factory() as other:
        other.get(Profile, profile_id).description = "Cardiac imaging"
        other.commit()

    # `ollama_clean()` already IS the gate-fail re-rank: the local Gemma path
    # never produces summary/applicant_notes (RankResult defaults them to
    # None, ranking/rank_pair.py:54-55). Do NOT set `produce_analysis = False`
    # -- that attribute does not exist on an OllamaClient and assigning it is
    # a silent no-op.
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary is None
        assert cr.applicant_notes is None
        assert cr.analysis_model is None


def test_retained_analysis_survives_when_the_profile_did_not_change(
    session, session_factory, ranked_pair, fake_rank_client
):
    """The discriminating control for gate 10: an unchanged profile must KEEP
    its analysis, otherwise the guard is just 'always clear'."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Advice that is still current"
    cr.applicant_notes = [{"note": "still good"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary == "Advice that is still current"
        assert cr.analysis_model == "claude-sonnet-4-6"
```

**Why gate 9 is the mid-flight construction and not the simpler one.** An earlier
revision captured `rank_fingerprint(p)`, edited the profile from an independent session
"while the job is queued", drained, and asserted the stamp equalled the pre-edit hash.
That edit lands **before the worker exists**. `worker.py:131` is
`profile = session.get(Profile, profile_id)` — the worker loads the profile fresh, at
drain time, i.e. *after* the edit — so it ranks and correctly stamps the post-edit
profile. Run verbatim it failed with the gate's own message:

```
E   AssertionError: the worker stamped a profile the ranker never saw
E   assert 'v1:cd6d7d7eb...' == 'v1:fefe07da9...'
```

Passing would have meant the bug. This plan's Task 5 preamble already states the
governing fact; the old test contradicted it. The mid-flight version is
mutation-verified: move the capture to after `rank_pair` **behind a
`session.refresh(profile)`** and it goes red (`1 failed, 4 passed`). The refresh is
needed for the mutation to bite — without it the worker's in-memory `profile` is
unchanged (`expire_on_commit=False`), which is precisely why this is a forward guard
rather than a live bug.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_ranking_worker_fingerprint.py -v`
Expected: FAIL — `assert None == 'v1:...'`. Judge on FAILED, not ERROR.

- [ ] **Step 3: Capture before `rank_pair`**

In `src/grant_lab/ranking/worker.py`, add the import:

```python
from grant_lab.profiles.fingerprint import rank_fingerprint
```

Immediately before the `result = rank_pair(` call (currently line 169):

```python
                # Capture BEFORE the LLM call. Today `profile` cannot change
                # between here and the merge below -- the session is not
                # committed, refreshed or expired, and the spend ledger uses a
                # separate session on purpose -- so this is equivalent to
                # hashing at write time. It is captured here anyway so that a
                # future refactor introducing a commit or a session.refresh in
                # between cannot silently stamp a profile the ranker never saw.
                captured_fingerprint = rank_fingerprint(profile)
```

- [ ] **Step 4: Guard the retained analysis**

Replace the retained-analysis block — **lines 238-243**, not 244-249. Line 238 is `if new_summary is None and new_notes is None:` and 243 is `new_analysis_model = existing.analysis_model`; **244 is blank and 245 is `session.merge(`**, so following a 244-249 range would delete the merge call.

```python
                if new_summary is None and new_notes is None:
                    existing = session.get(CallRanking, (call_id, profile_id))
                    # Only inherit advice that describes THIS profile. After an
                    # edit the retained triple describes the old one, and
                    # stamping it with the new fingerprint would make it
                    # permanently invisible to repair.
                    if (
                        existing is not None
                        and existing.profile_fingerprint == captured_fingerprint
                    ):
                        new_summary = existing.summary
                        new_notes = existing.applicant_notes
                        new_analysis_model = existing.analysis_model
```

- [ ] **Step 5: Write the fingerprint**

Add one keyword to the `CallRanking(...)` constructed at the `session.merge(` call:

```python
                        profile_fingerprint=captured_fingerprint,
                        ranked_at=datetime.now(UTC),
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_ranking_worker_fingerprint.py tests/test_ranking_worker.py tests/test_ranking_e2e.py tests/test_ranking_queue.py tests/test_ranking_backfill.py -v`
Expected: PASS, 4 new tests and no pre-existing ranking test broken. (Task 7, not Task 5, is what breaks `test_reconcile_skips_already_ranked_pair`.)

- [ ] **Step 7: Prove gate 10 bites (mutation)**

Temporarily drop the fingerprint condition, restoring `if existing is not None:`.

Run: `uv run pytest tests/test_ranking_worker_fingerprint.py -v`
Expected: `1 failed, 3 passed` — `test_retained_analysis_is_dropped_when_the_profile_changed` FAILS and `test_retained_analysis_survives_when_the_profile_did_not_change` still PASSES. Both matter: the second is what proves the guard is not simply "always clear". Dry-run-verified, control held.

**Restore** and re-run.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/worker.py tests/test_ranking_worker_fingerprint.py
git commit -m "feat(freshness): a ranking records the profile the ranker actually saw"
```

---

### Task 6: The worker defers a stale vector

**Files:**
- Modify: `src/grant_lab/ranking/worker.py` (after the `if not profile.is_active:` block at line 141, before the daily-cap gate)
- Test: `tests/test_ranking_worker_defers_stale_vector.py` (flat)

**Interfaces:**
- Consumes: `embedding_fingerprint` (Task 2); the write-site invariant (Task 3); `queue.revert_to_pending`.
- Produces: `_STALE_VECTOR_MAX_DEFER_HOURS: int = 24` in `worker.py`.

**The commit is the whole finding.** `revert_to_pending` (`queue.py:302-318`) issues an `UPDATE` and returns a rowcount; it does **not** commit. Every other early exit in the worker commits before `continue` (`worker.py:139-140`, `:149-159`). Without that commit the deferral is rolled back when the session closes, the job stays RUNNING on its previously committed claim, and `requeue_stuck_running` picks it up — bumping `attempts` and eventually dead-lettering the pair. The deferral would burn exactly the retries it exists to protect.

**A NULL fingerprint does not defer.** Found while planning, and not in the spec: on the first deploy every profile has a vector and a NULL fingerprint, so a naive `!=` would defer *every* pair until the 03:30 tick refills them. NULL means unknown provenance — the same as a NULL vector — so proceed. The ranking is stamped with `embedding_fingerprint: None` inside its payload, so once the tick fills the real value the rank fingerprint changes and the pair is re-ranked. Self-healing, no deploy-wide pause.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranking_worker_defers_stale_vector.py`. Copy the three
`session` / `session_factory` / `fake_rank_client` fixtures from Task 5's file, and
define `queued_pair` locally — there is no such fixture:

```python
"""Task 6 gates: the worker defers a pair whose vector has not caught up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from grant_lab.db.models import CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import embedding_fingerprint
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import enqueue_job, make_call, make_profile, ollama_clean


@pytest.fixture()
def session_factory(pg_session_factory):
    return pg_session_factory


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


@pytest.fixture()
def fake_rank_client():
    return ollama_clean()


@pytest.fixture()
def queued_pair(session):
    """(call_id, profile_id, profile, job_id) -- PENDING job, profile has a vector."""
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    p.embedding = [0.1] * 1024
    p.embedding_model = "bge-m3"
    p.embedding_fingerprint = embedding_fingerprint(p)
    c = make_call(session)
    job_id = enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    return c.id, p.id, p, job_id


def test_a_stale_vector_defers_without_burning_an_attempt(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 15. Assert from a FRESH session -- an uncommitted revert would
    look correct from the worker's own session and be rolled back on close."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        job = fresh.get(RankingJob, job_id)
        assert job.status == RankingJobStatus.PENDING
        assert job.attempts == 0
        assert fresh.get(CallRanking, (call_id, profile_id)) is None


def test_a_missing_vector_does_not_defer(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 17. A NULL vector is today's UNKNOWN-cosine path: the LLM
    adjudicates. Deferring here would stall ranking whenever Ollama is down."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding = None
    profile.embedding_model = None
    profile.embedding_fingerprint = None
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_a_null_fingerprint_with_a_present_vector_does_not_defer(
    session, session_factory, queued_pair, fake_rank_client
):
    """The deploy case. Every pre-0075 profile has a vector and a NULL
    fingerprint; deferring on that would pause ALL ranking until 03:30."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = None
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_deferral_is_bounded_by_job_age(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 16. force=False preserves the old vector on an embedder outage, so
    the mismatch persists and the pair would defer forever."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    job = session.get(RankingJob, job_id)
    job.created_at = datetime.now(UTC) - timedelta(hours=25)
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_a_current_vector_ranks_normally(
    session, session_factory, queued_pair, fake_rank_client
):
    """Discriminating control: without this, 'never defer' would pass gates
    16, 17 and the deploy case."""
    from grant_lab.profiles.fingerprint import embedding_fingerprint

    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = embedding_fingerprint(profile)
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None
```

`queued_pair` must yield `(call_id, profile_id, profile, job_id)` with a PENDING `RankingJob` and a profile whose `embedding` is set. Build it from the existing ranking fixtures.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_ranking_worker_defers_stale_vector.py -v`
Expected: FAIL — `test_a_stale_vector_defers_without_burning_an_attempt` fails because a `CallRanking` was written.

- [ ] **Step 3: Implement the deferral**

Add near the other module constants in `worker.py`:

```python
# A pair whose profile vector has not caught up is deferred rather than ranked
# against a stale cosine. Bounded because refresh_profile_embedding with
# force=False PRESERVES the old vector on an embedder outage, so the mismatch
# would otherwise persist forever. 24h is comfortably more than one 03:30
# embedding-tick cycle.
_STALE_VECTOR_MAX_DEFER_HOURS = 24
```

**Imports — exactly two edits, and `revert_to_pending` is NOT one of them.**

1. `worker.py:8` is `from grant_lab.db.models import Call, CallRanking, CallRankingCriterion, Profile`. **Add `RankingJob`** — the deferral calls `session.get(RankingJob, job_id)` and the name is not in this module's namespace. Without it the first deferral check is a `NameError`.
2. Add `from grant_lab.profiles.fingerprint import embedding_fingerprint` (Task 5 already added `rank_fingerprint` on this line — make it one import of both).
3. `from datetime import UTC, datetime` at line 4 must become `from datetime import UTC, datetime, timedelta`.

**Do NOT add `from grant_lab.ranking.queue import revert_to_pending`.** It is *already* imported at `worker.py:12-20`, inside the multi-name block:

```python
from grant_lab.ranking.queue import (
    MAX_ATTEMPTS, claim_pending_jobs, mark_done, mark_failed,
    requeue_stuck_running, revert_to_pending, touch_running_job,
)
```

A second import line is ruff `F811`, and `uv run ruff check` is a hard gate.

Insert immediately after the `if not profile.is_active:` block and before the daily-cap gate:

```python
                # Defer rather than rank against a vector that has not caught
                # up. post_edit.py:33-34 states the ordering the request path
                # honours ("the ranking jobs must see the post-edit vector");
                # the repair path has no equivalent, so a pair could be ranked
                # early, stamped with the current text fingerprint, and become
                # permanently invisible to repair.
                #
                # A NULL fingerprint is unknown provenance, NOT a mismatch: on
                # the first deploy every profile has a vector and a NULL
                # fingerprint, and deferring on that would pause all ranking
                # until the 03:30 tick. The pair is stamped with
                # embedding_fingerprint=None in its payload, so it re-ranks
                # itself once the tick fills the real value.
                job_row = session.get(RankingJob, job_id)
                too_old = job_row is not None and job_row.created_at < datetime.now(
                    UTC
                ) - timedelta(hours=_STALE_VECTOR_MAX_DEFER_HOURS)
                if (
                    profile.embedding is not None
                    and profile.embedding_fingerprint is not None
                    and profile.embedding_fingerprint != embedding_fingerprint(profile)
                    and not too_old
                ):
                    revert_to_pending(session, [job_id])
                    # revert_to_pending issues an UPDATE and does NOT commit.
                    # Without this the revert rolls back on session close, the
                    # job stays RUNNING on its committed claim, and
                    # requeue_stuck_running bumps attempts and eventually
                    # dead-letters the pair -- burning the very retry this
                    # deferral exists to protect.
                    session.commit()
                    _logger.info(
                        "ranking.job_deferred_stale_vector",
                        extra={
                            "event": "ranking_job_deferred_stale_vector",
                            "job_id": job_id,
                            "profile_id": profile_id,
                        },
                    )
                    continue
```

`timedelta` comes from the import edit in step 3.

**One known inefficiency, left in deliberately.** `session.get(RankingJob, job_id)` runs
on **every** job, not only stale ones — one extra
`SELECT ... FROM ranking_jobs WHERE id = ...` per job on the hot path, for a value used
only when the first three conditions already hold (the rare case). It sits oddly beside
Task 4 gate 6, whose whole subject is not issuing loads you do not need. The flat form
above is what the dry run built and verified end to end (5 tests green, both mutations
biting), so it is what this plan ships. The nested alternative —

```python
                if (
                    profile.embedding is not None
                    and profile.embedding_fingerprint is not None
                    and profile.embedding_fingerprint != embedding_fingerprint(profile)
                ):
                    job_row = session.get(RankingJob, job_id)
                    too_old = job_row is not None and job_row.created_at < datetime.now(
                        UTC
                    ) - timedelta(hours=_STALE_VECTOR_MAX_DEFER_HOURS)
                    if not too_old:
                        ...  # the revert / commit / log / continue block, re-indented
```

— is semantically identical and strictly cheaper, but requires re-indenting the tail and
was **not** exercised by the dry run. Take it only if the tests stay green afterwards;
it is a refinement, not a requirement, and at tens of profiles it cannot bite. Either
way, note the choice in `PLAN-DEFECTS.md`.

Also verified: `RankingJob.created_at` is `DateTime(timezone=True)`
(`db/models/ranking.py:205-206`), so `job_row.created_at < datetime.now(UTC) - ...` does
not raise the naive/aware `TypeError` it would with a naive column.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ranking_worker_defers_stale_vector.py tests/test_ranking_worker.py tests/test_ranking_e2e.py tests/test_ranking_queue.py tests/test_ranking_backfill.py -v`
Expected: PASS, 5 new tests and 49 pre-existing ranking tests unaffected (dry-run measured).

- [ ] **Step 5: Prove BOTH halves bite (mutation, twice)**

**5a — the deferral itself.** Change the condition to `if False:`.

Run: `uv run pytest tests/test_ranking_worker_defers_stale_vector.py -v`
Expected: `1 failed, 4 passed` — `test_a_stale_vector_defers_without_burning_an_attempt` FAILS. **Restore.**

**5b — the commit.** Delete the `session.commit()` line inside the deferral block.

Run: `uv run pytest tests/test_ranking_worker_defers_stale_vector.py -v`
Expected: `1 failed, 4 passed` — the same test FAILS, because the fresh session sees the job still RUNNING. This is the finding that made this task necessary. `revert_to_pending` (`queue.py:302-318`) really does return a rowcount without committing, and every other early exit in the loop commits before `continue` (`:139-140`, `:149-159`). If it stays green, the assertion is not actually reading from a new session — fix that before continuing.

**Restore the commit** and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/worker.py tests/test_ranking_worker_defers_stale_vector.py
git commit -m "feat(freshness): defer a pair whose vector has not caught up, and commit the deferral"
```

---

### Task 7: The ranking repair arm

**Task 5 must already be merged.** See "Task order is load-bearing".

**Files:**
- Modify: `src/grant_lab/ranking/queue.py` — `reconcile_missing_rankings`, lines 112-176, plus the import at line 10
- Test: `tests/test_ranking_reconcile_staleness.py` (flat)
- Amend: `tests/test_ranking_queue.py::test_reconcile_skips_already_ranked_pair` — see step 4

**Interfaces:**
- Consumes: `rank_fingerprint` (Task 2); `LEGACY_SENTINEL` (Task 2); the write-site invariant (Task 5).
- Produces: `reconcile_missing_rankings(session) -> int`, signature unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranking_reconcile_staleness.py`. Copy the `session` / `session_factory` fixtures from Task 5's file and define `ranked_pair` locally:

```python
"""Task 7 gates: reconcile asks whether a ranking is current, not whether it exists."""

from __future__ import annotations

import datetime as dt

import pytest

from grant_lab.db.models import CallRanking, Profile, RankingJob
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking.queue import reconcile_missing_rankings
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import enqueue_job, make_call, make_profile, ollama_clean


@pytest.fixture()
def session_factory(pg_session_factory):
    return pg_session_factory


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


@pytest.fixture()
def ranked_pair(session, session_factory):
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=ollama_clean(),
        embedding_threshold=0.0,
        batch_size=1,
    )
    return c.id, p.id, p


def _jobs(session, call_id, profile_id):
    return session.query(RankingJob).filter_by(call_id=call_id, profile_id=profile_id).count()


def test_a_matching_fingerprint_is_not_re_enqueued(session, ranked_pair):
    """Discriminating control: without this, 'always re-enqueue' passes
    every other test in this file."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = rank_fingerprint(profile)
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) == 0


def test_a_differing_fingerprint_is_re_enqueued(session, ranked_pair):
    """GATE 11."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "v1:something_else_entirely"
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1
    assert _jobs(session, call_id, profile_id) == 1


def test_a_legacy_pair_with_an_untouched_profile_is_skipped(session, ranked_pair):
    """GATE 12. The user's decision: grandfather the pre-0075 corpus rather
    than re-rank everything on deploy."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "legacy"
    # ranked_at must be at or after the profile's last edit.
    cr.ranked_at = profile.updated_at
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) == 0


def test_a_legacy_pair_whose_profile_was_edited_IS_re_enqueued(session, ranked_pair):
    """GATE 12b. An earlier revision's legacy arm was unconditional, so a
    pre-0075 pair stayed exempt FOREVER -- including after the edit whose
    background task was lost, which is the exact case this repair exists for."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "legacy"
    cr.ranked_at = profile.updated_at - dt.timedelta(days=1)
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1


def test_a_null_fingerprint_after_deploy_is_treated_as_stale(session, ranked_pair):
    """GATE 13. Every row present at migration time was stamped 'legacy', so a
    NULL here means a writer bug. Treat it as stale so the bug is loud."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = None
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1


def test_no_active_profiles_issues_no_insert(session, ranked_pair):
    """GATE 14. An empty VALUES list is a SYNTAX ERROR, so without the early
    return the daily tick raises on a fresh or fully-archived database --
    where today's CROSS JOIN simply degrades to zero rows.

    `ranked_pair` is requested so there IS a profile to deactivate -- without
    it the table may already be empty and the gate proves nothing.
    """
    session.query(Profile).update({Profile.is_active: False})
    session.commit()
    assert reconcile_missing_rankings(session) == 0
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_ranking_reconcile_staleness.py -v`
Expected: FAIL — gates 11, 12b and 13 return 0 because the current `NOT EXISTS` only asks whether a ranking exists.

- [ ] **Step 3: Implement**

**Two import edits, not one.** `queue.py:10` is
`from grant_lab.db.models import RankingJob, RankingJobStatus` — there is **no `Profile`
in this module's namespace**, and the new body calls `select(Profile)`. Without this the
step is a `NameError` at first call.

```python
from grant_lab.db.models import Profile, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import LEGACY_SENTINEL, rank_fingerprint
```

Replace the body of `reconcile_missing_rankings` from the `cutoff = ` line through the `session.execute(...)` call:

```python
    cutoff = datetime.now(UTC) - timedelta(days=_RECONCILE_DAYS)
    dead_letter_cutoff = datetime.now(UTC) - timedelta(days=_DEAD_LETTER_SUPPRESS_DAYS)

    # Fingerprints are Python over JSONB and cannot be computed in SQL, so the
    # active profiles come back first and their fingerprints ride in as a
    # VALUES list.
    # Column-limited, for the same reason Task 4 gives: `select(Profile)`
    # pulls the 1024-dim vector, and rank_fingerprint never reads it (it
    # reads description, structured, budget_currency, name, kind and
    # embedding_fingerprint). Task 4 asserts that principle in gate 6, whose
    # scope is the EMBEDDING arm only -- so it does not cover this file, and
    # an earlier revision of this plan reintroduced the wholesale load here
    # three tasks later. `.all()`, not `.scalars()`: these are Rows.
    #
    # Verified: the Rows produce byte-identical rank_fingerprints to loaded
    # Profile objects (`kind` is String + CHECK, not a native enum, so both
    # sides give the same str through `getattr(x, "value", x)`), and the
    # emitted SQL carries no `profiles.embedding AS` column.
    profiles = session.execute(
        select(
            Profile.id,
            Profile.name,
            Profile.kind,
            Profile.description,
            Profile.structured,
            Profile.budget_currency,
            Profile.embedding_fingerprint,
        ).where(Profile.is_active.is_(True))
    ).all()
    if not profiles:
        # An empty VALUES list is a SYNTAX ERROR. Today's CROSS JOIN degrades
        # to zero rows on an empty set; this early return preserves that.
        return 0

    values_sql = ", ".join(f"(:pid_{i}, :fp_{i})" for i in range(len(profiles)))
    params: dict[str, object] = {
        "cutoff": cutoff,
        "dead_letter_cutoff": dead_letter_cutoff,
        "legacy": LEGACY_SENTINEL,
    }
    for i, prof in enumerate(profiles):
        params[f"pid_{i}"] = prof.id
        params[f"fp_{i}"] = rank_fingerprint(prof)

    result = session.execute(
        text(
            f"""
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT c.id, p.id, 'pending'
            FROM calls c
            CROSS JOIN (VALUES {values_sql}) AS want(profile_id, fingerprint)
            JOIN profiles p ON p.id = want.profile_id
            WHERE p.is_active = true
              AND c.published_at >= :cutoff
              -- Currency, not existence. A ranking counts as current when its
              -- fingerprint matches, OR when it predates migration 0075 AND
              -- its profile has not been touched since it was written.
              --
              -- `cr.ranked_at >= p.updated_at` is what BOUNDS the legacy
              -- exemption. An unconditional 'legacy' arm would exempt a
              -- pre-0075 pair forever, including after the edit whose
              -- background task was lost -- the exact case this repair exists
              -- for. Profile.updated_at carries onupdate=func.now(), so any
              -- edit revokes the exemption for that profile's pairs alone,
              -- and the row gets a real fingerprint afterwards so it never
              -- recurs.
              --
              -- A NULL fingerprint is NOT exempt: migration 0075 stamped every
              -- pre-existing row 'legacy', so a NULL here means a writer bug.
              AND NOT EXISTS (
                SELECT 1 FROM call_rankings cr
                WHERE cr.call_id = c.id AND cr.profile_id = p.id
                  AND ((cr.profile_fingerprint = :legacy
                        AND cr.ranked_at >= p.updated_at)
                       OR cr.profile_fingerprint = want.fingerprint)
              )
              AND NOT EXISTS (
                SELECT 1 FROM ranking_jobs rj
                WHERE rj.call_id = c.id
                  AND rj.profile_id = p.id
                  AND rj.status = 'failed'
                  AND rj.updated_at > :dead_letter_cutoff
              )
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        params,
    )
```

The `values_sql` interpolation is safe: it is built from `range(len(profiles))`, so it contains only generated placeholder names. Every value stays a bound parameter.

**And the VALUES list types correctly with no casts — verified, not assumed.** Captured from the live `before_cursor_execute` hook with three active profiles:

```sql
CROSS JOIN (VALUES (%(pid_0)s, %(fp_0)s), (%(pid_1)s, %(fp_1)s), (%(pid_2)s, %(fp_2)s))
  AS want(profile_id, fingerprint)
```

`want.profile_id` joins `profiles.id` (`bigint`) and `want.fingerprint` compares against `call_rankings.profile_fingerprint` (`text`) with **no explicit casts**. The driver is `postgresql+psycopg` (psycopg 3), which sends typed parameters, so the untyped-VALUES hazard does not arise. 3 profiles x 2 calls enqueued exactly 6 jobs.

- [ ] **Step 4: Run the tests — and amend the one pre-existing test this task breaks**

Run: `uv run pytest tests/test_ranking_reconcile_staleness.py tests/test_ranking_queue.py tests/test_ranking_worker.py tests/test_ranking_e2e.py tests/test_ranking_backfill.py -v`

Expected **before** the amendment:

```
FAILED tests/test_ranking_queue.py::test_reconcile_skips_already_ranked_pair
```

That test (`tests/test_ranking_queue.py:237`) seeds a `CallRanking` with no
`profile_fingerprint` and asserts `reconcile_missing_rankings(s) == 0`, under the
docstring "**If a CallRanking already exists the pair is done — no enqueue**". This task
deliberately replaces existence with currency, and gate 13 makes a NULL fingerprint
stale, so the pair is now re-enqueued. Correct behaviour; the old expectation is what
must change.

**This failure is also the empirical proof of the "Task 5 must land before Task 7" rule.**
It is exactly the described loop — a ranking whose `profile_fingerprint` is NULL gets
re-enqueued on every tick — reproduced in miniature. If you see it and Task 5 is *not*
yet committed, stop: you have the ordering wrong.

Amend it in this same commit:

```python
def test_reconcile_skips_already_ranked_pair(pg_session_factory, profile_factory):
    """If a CURRENT CallRanking exists the pair is done — no enqueue.

    Migration 0075 changed what "done" means: reconcile now asks whether the
    ranking describes the profile's present content, not merely whether a row
    exists. A NULL `profile_fingerprint` after 0075 means a writer bug and is
    deliberately treated as stale, so this seed stamps the fingerprint the
    worker would have written.
    """
    from grant_lab.db.models import Profile
    from grant_lab.profiles.fingerprint import rank_fingerprint

    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(...)          # unchanged
        s.add(
            CallRanking(
                ...,                            # unchanged
                raw_response={},
                profile_fingerprint=rank_fingerprint(s.get(Profile, profile_id)),
            )
        )
        s.commit()
    # ...rest of the test unchanged
```

Re-run. Expected: PASS. **This is the only pre-existing ranking test that changes** — if
a second goes red, record it in `PLAN-DEFECTS.md` rather than reseeding it.

- [ ] **Step 5: Prove gate 12b bites (mutation)**

Temporarily drop `AND cr.ranked_at >= p.updated_at`, leaving `cr.profile_fingerprint = :legacy` unconditional.

Run: `uv run pytest tests/test_ranking_reconcile_staleness.py -v`
Expected: `1 failed, 5 passed` — `test_a_legacy_pair_whose_profile_was_edited_IS_re_enqueued` FAILS, and `test_a_legacy_pair_with_an_untouched_profile_is_skipped` still PASSES (the control held in the dry run).

**Second mutation, for the other arm.** Add `AND FALSE` to the `NOT EXISTS` so the tick always re-enqueues. Expected: `2 failed, 4 passed` — `test_a_matching_fingerprint_is_not_re_enqueued` and `test_a_legacy_pair_with_an_untouched_profile_is_skipped` both FAIL.

**Restore** after each and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/queue.py \
        tests/test_ranking_reconcile_staleness.py \
        tests/test_ranking_queue.py
git commit -m "feat(freshness): reconcile asks whether a ranking is current, not whether it exists"
```

---

### Task 8: Hole 1 — the scheduling guard

**Files:**
- Modify: `src/grant_lab/web/api/profiles.py:480, 520-531`
- Test: `tests/test_api_profile_update_scheduling.py`

**Interfaces:**
- Consumes: `rank_fingerprint` (Task 2).
- Produces: nothing later tasks depend on. This is the last task.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_profile_update_scheduling.py`. **None of `client`, `owner_profile`,
`auth_headers` or `body_for` exists**, and `auth_headers` is not merely missing — the
concept does not apply here: auth is a signed **session cookie**, not a bearer header.
The client pattern is `tests/test_api_profile_update.py:19`. A module-level `_BODY` dict
replaces `body_for`: it round-trips the profile unchanged because the `owner_profile`
fixture PUTs it once first.

```python
"""Task 8 gates: hole 1, the scheduling guard."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sqltext

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.main import create_app

_BODY = {
    "description": "Imaging research lab.", "areas": "oncology\nimaging",
    "budget_min": "1000", "budget_max": "2000", "budget_currency": "SEK",
    "trl_min": "3", "trl_max": "7", "must_include": "pediatric", "hard_exclusions": "",
    "level": "group", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "project_grant",
    "funder_exclusions": "", "country": "SE", "host_institution": "UmU",
}


@pytest.fixture()
def app(monkeypatch, pg_url):
    # Every env var set INLINE via monkeypatch. Never read the real .env.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


@pytest.fixture()
def owner_profile(app, pg_session_factory, profile_factory):
    """(client, profile_id), already normalized by one real PUT, so a second
    identical PUT is a genuine no-op rather than a first normalization."""
    pid, uid = profile_factory(name="Lab", description="Imaging research lab.")
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with (
        patch("grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"),
        client,
    ):
        r = client.put(f"/api/profiles/{pid}", json=_BODY)
    assert r.status_code == 200, r.text
    return client, pid


def test_an_areas_only_edit_schedules_the_refresh(owner_profile):
    """GATE 2. build_profile_text composes description OR areas OR org fields,
    so a profile with an empty description and non-empty areas has real
    embedding text -- but the guard read `body.description`, so editing one
    scheduled NOTHING and it stayed stale forever."""
    client, pid = owner_profile
    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(
            f"/api/profiles/{pid}",
            json={**_BODY, "description": "", "areas": "liver cancer\ntransplant rejection"},
        )
    assert r.status_code == 200, r.text
    assert task.called, "an areas-only edit scheduled no refresh and no backfill"


def test_an_identical_put_schedules_nothing(owner_profile, pg_session_factory):
    """GATE 3 -- the load-bearing one for the canonicalizer's numeric rule.

    Today an unchanged PUT enqueues 90 days of ranking jobs whenever the
    description happens to be non-empty. The endpoint writes budget_min with
    bounded_int_or_400 (an INT); a float 1000.0 in the row is what the
    canonicalizer has to absorb.
    """
    client, pid = owner_profile
    # Seeded with RAW SQL, not through the ORM. `p.structured = {**p.structured,
    # "budget_min": 1000.0}` is a SILENT NO-OP: Python's `==` on dicts holds
    # `{"budget_min": 1000} == {"budget_min": 1000.0}`, so SQLAlchemy's change
    # detection sees no difference and emits no UPDATE at all. The gate then
    # passes for the exact reason its own docstring warns about -- the value
    # was already normalized -- and passes with the canonicalizer DELETED.
    with pg_session_factory() as s:
        s.execute(
            sqltext(
                "UPDATE profiles SET structured = jsonb_set(structured, '{budget_min}', "
                "'1000.0'::jsonb) WHERE id = :i"
            ),
            {"i": pid},
        )
        s.commit()
        raw = s.execute(
            sqltext("SELECT structured->>'budget_min' FROM profiles WHERE id=:i"), {"i": pid}
        ).scalar_one()
    assert raw == "1000.0", f"seed did not land: {raw!r}"

    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(f"/api/profiles/{pid}", json=_BODY)
    assert r.status_code == 200, r.text
    assert not task.called, "an unchanged PUT enqueued a 90-day backfill"


def test_a_real_change_still_schedules(owner_profile):
    """Discriminating control: without this, 'never schedule' passes gate 3."""
    client, pid = owner_profile
    with (
        patch(
            "grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"
        ) as task,
        client,
    ):
        r = client.put(
            f"/api/profiles/{pid}",
            json={**_BODY, "description": "Something genuinely different"},
        )
    assert r.status_code == 200, r.text
    assert task.called
```

**Two notes on the patch target, both verified:**

- `web/api/profiles.py:22` is `from grant_lab.profiles import post_edit`, so the name in
  that module is the **module object itself** and the call site (`:526`) resolves the
  attribute at call time. Patching
  `"grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background"` sets it on
  the shared module and the endpoint picks it up. Measured: `a.post_edit is b` → True.
- `task.called` is True only because `TestClient` **runs** background tasks before
  returning. The assertion therefore proves "the task ran", not merely "the task was
  scheduled". That is stronger, and fine — but it depends on TestClient's background-task
  execution, not on `add_task` being reached.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_api_profile_update_scheduling.py -v`
Expected: `test_an_areas_only_edit_schedules_the_refresh` FAILS (`task.called` is False).

- [ ] **Step 3: Implement**

Add the import **in sorted position** — immediately after
`from grant_lab.profiles.fields import FieldValidationError, parse_structured` (line 24)
and before `from grant_lab.profiles.forms import (` (line 25):

```python
from grant_lab.profiles.fingerprint import rank_fingerprint
```

Placing it next to `from grant_lab.profiles import post_edit` (line 22), the natural
reading of "add the import", sorts **after** `grant_lab.profiles.create` and
`grant_lab.profiles.fields` and is ruff `I001`. Same class of defect as the migration
header in Task 1.

Beside the existing `old_embed_text` line (currently 480):

```python
        old_embed_text = build_profile_text(p)
        old_rank_fp = rank_fingerprint(p)
```

Beside the existing `new_embed_text` line (currently 520), before `session.commit()`:

```python
        new_embed_text = build_profile_text(p)
        embed_text_changed = old_embed_text.strip() != new_embed_text.strip()
        new_rank_fp = rank_fingerprint(p)
        rank_inputs_changed = old_rank_fp != new_rank_fp
        session.commit()
```

Replace the guard (currently line 524):

```python
    # Schedule on the RANK fingerprint, not on `body.description`. The embed
    # inputs are a subset of the rank inputs, so this schedules a superset of
    # the old behaviour and never less -- and an areas-only edit, which used to
    # schedule nothing at all, now schedules both.
    #
    # This comparison is in-memory (`new`) against DB-loaded (`old`):
    # expire_on_commit=False (db/session.py:58) means committing does NOT
    # reload the row, so moving these lines cannot make the representations
    # match. It is safe only because the canonicalizer in
    # profiles/fingerprint.py normalizes numbers and omits empties.
    if rank_inputs_changed:
        background.add_task(
            post_edit.refresh_and_backfill_in_background,
            session_factory=sf,
            embed_client=request.app.state.embed_client,
            profile_id=profile_id,
            refresh_embedding=embed_text_changed,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_api_profile_update_scheduling.py tests/test_api_profile_update.py tests/test_profiles_e2e.py tests/test_profiles_redesign_e2e.py tests/web -v`
Expected: PASS, 3 new tests. **No existing test breaks here** — the dry run measured `17 passed` across the three profile-update files, contrary to this plan's earlier hedge. The two tests that DO need amending are in Tasks 4 and 7. If something goes red here anyway, read it before touching it and record anything ambiguous in `PLAN-DEFECTS.md` rather than silently rewriting an assertion.

- [ ] **Step 5: Prove BOTH gates bite (mutation, twice)**

**5a — the guard.** Temporarily restore `if body.description.strip():`.

Run: `uv run pytest tests/test_api_profile_update_scheduling.py -v`
Expected: **`2 failed, 1 passed`** — `test_an_areas_only_edit_schedules_the_refresh` AND `test_an_identical_put_schedules_nothing`. The second fires because under the old guard a non-empty description schedules unconditionally, which is the very bug gate 3 exists for. Two failures where one was promised is the expected result, not a reason to stop and investigate. **Restore.**

**5b — the canonicalizer's numeric rule, which only this gate proves.** Delete `if isinstance(value, float) and value.is_integer(): return int(value)` from `_canonical` in `src/grant_lab/profiles/fingerprint.py`.

Run: `uv run pytest tests/test_api_profile_update_scheduling.py -v`
Expected: `1 failed, 2 passed` — `test_an_identical_put_schedules_nothing` FAILS. **If it stays green, the `jsonb_set` seed did not land** and gate 3 is testing an already-normalized int: fix the seed, do not move on. (With the ORM seed an earlier revision specified, this mutation left all 3 green.)

**Restore** and re-run.

**5c — the control.** Change the guard to `if True:` (always schedule). Expected: `1 failed, 2 passed` — only `test_an_identical_put_schedules_nothing`. **Restore.**

- [ ] **Step 6: Full suite and commit**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: **exactly 1 skipped**, and a pass total of **5364 + 39 = 5403**.

That 39 is the sum of the gate counts this plan asks for, per task:

```
baseline    5364   (measure it yourself at c08c9a69 before starting)
+ T1   3 -> 5367
+ T2   8 -> 5375
+ T3   4 -> 5379
+ T4   6 -> 5385
+ T5   4 -> 5389
+ T6   5 -> 5394
+ T7   6 -> 5400
+ T8   3 -> 5403
```

**The 39 is MEASURED, the 5403 is not.** Every python block in this plan was extracted
verbatim and run against a correct implementation: `39 passed`. So the delta is real. The
**base** 5364 is the number this plan was written against — **measure it yourself at
`c08c9a69` before you start**, carry the 39 against what you measured, and if your total
differs, find and name the term that changed. Never adjust an expected number to make the
arithmetic close.

`0 skipped` means a live Anthropic API call ran — stop and record it. Never export
`ANTHROPIC_API_KEY`.

```bash
uv run ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py   # no-op here unless a model changed
git add src/grant_lab/web/api/profiles.py tests/test_api_profile_update_scheduling.py
git commit -m "fix(freshness): an areas-only edit no longer schedules nothing"
```

`git add -A` is deliberately **not** used: a mutation left un-restored would be committed
silently. Add the two files by name and check `git -C <worktree> status --short` is
otherwise clean.

---

## Self-review notes

**Spec coverage.** Every change item in the spec maps to a task: changes 1-2 to Task 1 and 2, change 3 to Task 3, change 5 to Task 4, change 4 to Task 5, change 7 to Task 6, change 6 to Task 7, change 8 to Task 8. All 20 spec gates appear: 1 and 3 in Task 2 and 8, 2 in Task 8, 4-8 in Task 4, 9-10 in Task 5, 11-14 in Task 7, 15-17 in Task 6, 18-19 in Task 3.

**Two deliberate departures from the spec**, both recorded here rather than by editing the spec:

1. **A NULL `embedding_fingerprint` does not defer** (Task 6). The spec says a mismatch defers and only exempts a NULL *vector*. On the first deploy every profile has a vector and a NULL fingerprint, so the spec as written would pause all ranking until 03:30. Treating NULL as unknown-provenance is self-healing and avoids that.
2. **The `_TICK_MAX_BATCHES` bound is recorded, not fixed** (Task 4 step 7). It cannot bite at 40,000 profiles of headroom against tens of profiles.

**Discriminating controls.** Five gates would pass under a trivially wrong implementation without their paired control, so each has one: Task 5's "analysis survives when unchanged", Task 6's "a current vector ranks normally", Task 7's "a matching fingerprint is not re-enqueued", Task 8's "a real change still schedules", and Task 4's "already-blank rows do not rescan forever".

**Type consistency.** `embedding_fingerprint(profile) -> str` and `rank_fingerprint(profile) -> str` are used with those exact names in Tasks 3, 4, 5, 6, 7 and 8. `LEGACY_SENTINEL` is defined in Task 2 and consumed in Task 7. `profiles_seen` is produced in Task 4 step 3 and consumed in Task 4 step 4. `captured_fingerprint` is local to Task 5. Both fingerprint functions duck-type on attribute access, but on **different** column sets: `embedding_fingerprint` needs only `description` and `structured` (so it works on Task 4's Row); `rank_fingerprint` also needs `name`, `kind` and `budget_currency` (so Task 7 selects those too).

---

## What the dry run changed, and what it confirmed

This plan was executed once end to end in a throwaway worktree before the real build. That run found **23 defects — 8 blockers, 8 majors, 7 minors** — all folded into the text above. The blockers were not subtle: eleven of the twelve fixtures an earlier revision named do not exist, and two test directories it told you to create tests in do not exist either. Every one of those would have surfaced as a **collection ERROR with zero FAILED**, which in this suite reads like a clean run.

Four gates were rewritten because they could not fail, or failed on a correct implementation:

| Gate | What was wrong | Now |
|---|---|---|
| Task 1 legacy stamp | `call_rankings` is empty when 0075 runs, so the assertion also passed with the `op.execute` deleted | drives `downgrade 0074` → seed → `upgrade head` |
| Task 2 round trip | `1000.0` does not change across a JSONB trip; the gate stayed green with the numeric rule removed | `endowment: 1e16` added — an exponent-form float is the only value that flips |
| Task 4 gate 6 | tripped its own assertion (`profiles.embedding IS NULL AS ...` contains `"profiles.embedding "`) and caught an unrelated `session.get`; the dry run's own first correction was **still** hollow — `"profiles.embedding AS"` never appears in a column-limited scan at all | a regex over the result columns only, red on all three ways the vector can leak back in |
| Task 5 gate 9 | asserted an invariant that would be a **bug** if it held — the edit landed before the worker loaded the profile | the rank client commits the edit **mid-flight**, from inside the MockTransport handler |
| Task 8 gate 3 | the ORM seed was a silent no-op (`{"b": 1000} == {"b": 1000.0}`, so no UPDATE was emitted) | seeded with `jsonb_set` and asserted to have landed |

No gate was deleted. What the dry run **confirmed** and left alone: trap 1 (a paired CHECK really would fail 0075, driven against a real seeded row), traps 2 and 3, the `Row` duck-typing claim, the `VALUES` list needing no casts, the patch target in Task 8, the whole "Verified names" block in Task 5, and Task 6's headline finding that `revert_to_pending` needs an explicit `session.commit()` — mutation-proved twice.

**Every code block in this plan was then run.** They were extracted verbatim from the
markdown, wired to a correct implementation, and executed: **`39 passed`** — the exact
delta the arithmetic above predicts. Five gates were then mutated and each went red on
the mutation and green on restore:

| Mutation | Gate that reddened |
|---|---|
| `op.execute(...'legacy')` → `pass` in migration 0075 | `test_existing_rankings_are_stamped_legacy` |
| numeric rule deleted from `_canonical` | `test_survives_a_real_postgres_round_trip` **and** `test_an_identical_put_schedules_nothing` (plus its control) |
| `Profile.embedding` added mid-list to the candidate scan | `test_the_staleness_decision_does_not_load_the_vector` |
| `Profile.embedding` added last to the candidate scan | same |
| capture moved after `rank_pair`, behind `session.refresh(profile)` | `test_the_stamp_is_captured_before_rank_pair_not_after` |

Full ledger: `/home/parlar_ai/freshness-run/PLAN-DEFECTS.md` and `STATUS.md`.
