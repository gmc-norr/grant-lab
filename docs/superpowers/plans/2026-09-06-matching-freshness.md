# Matching Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a stored embedding and a stored ranking say *which* profile content they describe, so the daily repair can tell "exists" from "current".

**Architecture:** Two derived fingerprints (`v1:` + sha256), recomputed at check time from the row rather than maintained at write time. `embedding_fingerprint` hashes the *output* of `build_profile_text`; `rank_fingerprint` hashes a canonical JSON payload of everything the ranker reads, including the stored embedding fingerprint. Two new nullable columns record what each stored artefact was built from. Both daily repair arms gain a staleness comparison, and the ranking worker defers a pair whose vector has not caught up.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16 + pgvector, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-09-06-matching-freshness-design.md` (rev 3, commit `1e7fae03`). The spec is INPUT. If the spec is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit to the spec.

## Global Constraints

- **Base commit:** `master` at `1e7fae03`. Branch `feat/matching-freshness`, worktree `.worktrees/freshness`.
- **Measured baseline: `5364 passed, 1 skipped`.** Run `uv run pytest -n 16 --dist loadfile`. Carry DELTAS against a baseline you measure yourself; never adjust an expected number to make arithmetic close.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic API call ran — stop and record it. Never export `ANTHROPIC_API_KEY`.
- **`docs/superpowers/specs/` is byte-identical at every commit.**
- **Never read, write, copy or back up `/home/parlar_ai/.env`.** Set any env var inline in the command.
- **`docs/ARCHITECTURE.md` is generated.** Run `uv run python scripts/gen_architecture.py` in the same commit as any model change. Never hand-edit it.
- **Lint gate:** `uv run ruff check src tests alembic scripts` must be clean.
- **Migration number is `0075`, `down_revision = "0074"`.** Verified: `alembic/versions/0074_boards_settings_bounds.py` declares `revision = "0074"`.
- **Fast local runs:** `uv run pytest -n 16 --dist loadfile`. Never `-n auto` (resolves to 72 here).

## Task order is load-bearing

**Task 5 must land before Task 7.** Task 7 makes reconcile treat a NULL `profile_fingerprint` as stale (gate 13). Task 5 is what makes the worker write a non-NULL one. Landing 7 first gives every freshly-written ranking a NULL fingerprint, which reconcile immediately re-enqueues — an infinite re-rank loop.

**Task 1 before everything.** Tasks 2-8 read or write the two new columns.

## Three traps found while planning — do not "fix" these

1. **Do NOT add a paired-NULL CHECK constraint for the fingerprint.** `db/models/profiles.py:60-63` has `ck_profiles_embedding_model_paired` — `(embedding IS NULL) = (embedding_model IS NULL)`. An analogous constraint for `embedding_fingerprint` looks symmetrical and would **fail migration 0075**, because existing rows deliberately keep their vector while getting a NULL fingerprint.

2. **`expire_on_commit=False`** (`db/session.py:58`). Computing the new fingerprint *after* `session.commit()` does **not** reload the row from Postgres, so it does not solve the in-memory-versus-loaded asymmetry. That asymmetry is handled in the canonicalizer (Task 2), not by moving a line.

3. **Force mode never enters the drain loop.** `refresh_missing_embeddings_tick` returns early at `refresh.py:176-184` with a single `force=True` batch. The drain loop at `:200` is always `force=False`. Task 4's drain change therefore cannot affect force mode; the force protection belongs in the batch function.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `alembic/versions/0075_freshness_fingerprints.py` | two nullable columns + `'legacy'` stamp | 1 |
| `src/grant_lab/db/models/profiles.py` | `Profile.embedding_fingerprint` | 1 |
| `src/grant_lab/db/models/ranking.py` | `CallRanking.profile_fingerprint` | 1 |
| `src/grant_lab/profiles/fingerprint.py` | **new** — canonicalizer + both fingerprints | 2 |
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

Create `tests/test_migration_0075_freshness_fingerprints.py`. Follow the shape of the existing `tests/test_migration_0074_boards_settings_bounds.py` for fixtures and helpers — read it first and reuse its migration-running fixture rather than inventing one.

```python
from sqlalchemy import inspect, text


def test_both_fingerprint_columns_exist(migrated_session):
    cols = {c["name"] for c in inspect(migrated_session.bind).get_columns("profiles")}
    assert "embedding_fingerprint" in cols
    cols = {c["name"] for c in inspect(migrated_session.bind).get_columns("call_rankings")}
    assert "profile_fingerprint" in cols


def test_existing_rankings_are_stamped_legacy(migrated_session):
    """A ranking that predates 0075 must be grandfathered, not re-ranked on deploy."""
    # Insert a ranking with the column NULL, as a pre-0075 row would be,
    # then assert the migration's UPDATE would have caught it.
    row = migrated_session.execute(
        text("SELECT profile_fingerprint FROM call_rankings LIMIT 1")
    ).scalar_one_or_none()
    # Every row present at migration time carries the sentinel.
    assert row is None or row == "legacy"


def test_profiles_keep_their_vector_with_a_null_fingerprint(migrated_session):
    """Deliberate asymmetry: profiles are re-embedded, rankings are not re-ranked.

    A paired-NULL CHECK constraint here would look symmetrical with
    ck_profiles_embedding_model_paired and would FAIL this migration.
    """
    constraints = {
        c["name"]
        for c in inspect(migrated_session.bind).get_check_constraints("profiles")
    }
    assert "ck_profiles_embedding_fingerprint_paired" not in constraints
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_migration_0075_freshness_fingerprints.py -v`
Expected: FAIL — `assert 'embedding_fingerprint' in cols` (the column does not exist).

- [ ] **Step 3: Write the migration**

```python
"""freshness fingerprints: record what a vector and a ranking describe

Revision ID: 0075
Revises: 0074
"""

from alembic import op
import sqlalchemy as sa

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
Expected: PASS. The architecture tests confirm the generated map matches the source.

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
- Test: `tests/test_profile_fingerprint.py`

**Interfaces:**
- Consumes: `Profile.embedding_fingerprint` (Task 1); `build_profile_text` from `grant_lab.embeddings.similarity`.
- Produces:
  - `embedding_fingerprint(profile) -> str`
  - `rank_fingerprint(profile) -> str`
  - `FINGERPRINT_VERSION: str = "v1"`
  - `LEGACY_SENTINEL: str = "legacy"`

  Both return `"v1:" + <64 hex chars>`. Neither performs I/O or touches a session.

- [ ] **Step 1: Write the failing test**

Create `tests/test_profile_fingerprint.py`. The round-trip tests need a real Postgres session; use the project's existing session fixture (read `tests/conftest.py` and copy the fixture name other DB tests use).

```python
import json

from grant_lab.db.models import Profile
from grant_lab.profiles.fingerprint import (
    FINGERPRINT_VERSION,
    embedding_fingerprint,
    rank_fingerprint,
)


def _profile(**kw):
    base = dict(
        name="Hepatology",
        kind="research_group",
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


def test_survives_a_real_postgres_round_trip(session):
    """GATE 1 -- the one that matters most.

    An unstable fingerprint marks every profile permanently stale and
    re-ranks the whole corpus on every daily tick, presenting as
    unexplained GPU load rather than as an error.

    The fixture deliberately contains nested non-sorted keys and a number
    whose repr changes across the trip. The precondition assert below is
    load-bearing: without it this test can pass on a fixture too simple to
    exercise the canonicalizer.
    """
    p = _profile(
        structured={
            "zebra": {"nested_z": 1, "nested_a": 2},
            "apple": [3, 2, 1],
            "budget_min": 1000.0,
            "must_include": [],
        }
    )
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
Expected: PASS, 9 tests.

- [ ] **Step 5: Prove gate 1 can go red (mutation)**

Temporarily remove numeric canonicalization — delete these two lines from `_canonical`:

```python
    if isinstance(value, float) and value.is_integer():
        return int(value)
```

Run: `uv run pytest tests/test_profile_fingerprint.py -v`
Expected: `test_integral_floats_and_ints_hash_the_same` FAILS. If `test_survives_a_real_postgres_round_trip` stays green, that is fine — its precondition assert tells you whether the fixture exercised this path.

**Restore the two lines** and re-run to confirm green before committing.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/grant_lab/profiles/fingerprint.py tests/test_profile_fingerprint.py
git commit -m "feat(freshness): derived fingerprints, canonical across a JSONB round trip"
```

---

### Task 3: Write site — embeddings

**Files:**
- Modify: `src/grant_lab/embeddings/refresh.py` (`refresh_profile_embedding`, lines 108-141)
- Test: `tests/embeddings/test_refresh_profile_fingerprint.py`

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

Create `tests/embeddings/test_refresh_profile_fingerprint.py`. Read `tests/embeddings/` first and reuse the existing fake embed client rather than writing a new one.

```python
import pytest

from grant_lab.embeddings.refresh import refresh_profile_embedding
from grant_lab.ranking.ollama_client import LLMError
from grant_lab.profiles.fingerprint import embedding_fingerprint


class _FakeEmbedder:
    model = "bge-m3"

    def __init__(self, *, fail=False):
        self.fail = fail

    def embed(self, text, _client=None):
        if self.fail:
            raise LLMError("ollama down")

        class _R:
            vector = [0.1] * 1024

        return _R()


def test_success_stamps_the_fingerprint(session, make_profile):
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_profile_embedding(session, _FakeEmbedder(), p.id)
    session.commit()
    assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_blank_text_clears_all_three(session, make_profile):
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_profile_embedding(session, _FakeEmbedder(), p.id)
    session.commit()
    p.description = ""
    p.structured = {}
    refresh_profile_embedding(session, _FakeEmbedder(), p.id)
    session.commit()
    assert p.embedding is None
    assert p.embedding_model is None
    assert p.embedding_fingerprint is None


def test_force_failure_clears_all_three(session, make_profile):
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_profile_embedding(session, _FakeEmbedder(), p.id)
    session.commit()
    refresh_profile_embedding(session, _FakeEmbedder(fail=True), p.id, force=True)
    session.commit()
    assert p.embedding is None
    assert p.embedding_model is None
    assert p.embedding_fingerprint is None


def test_non_force_failure_leaves_all_three_untouched(session, make_profile):
    """The review's 'normal failure case'. The old vector survives and the
    fingerprint keeps describing it truthfully, so the row is DETECTABLY
    stale rather than silently wrong."""
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_profile_embedding(session, _FakeEmbedder(), p.id)
    session.commit()
    fp_before = p.embedding_fingerprint

    p.description = "Completely different subject"
    refresh_profile_embedding(session, _FakeEmbedder(fail=True), p.id, force=False)
    session.commit()

    assert p.embedding is not None
    assert p.embedding_model == "bge-m3"
    assert p.embedding_fingerprint == fp_before
    # ...and that is now provably stale:
    assert p.embedding_fingerprint != embedding_fingerprint(p)
```

If `make_profile` does not exist as a fixture, build the `Profile` inline the way the neighbouring embedding tests do.

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/embeddings/test_refresh_profile_fingerprint.py -v`
Expected: FAIL — `assert None == 'v1:...'` on the success test.

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

Run: `uv run pytest tests/embeddings/ -v`
Expected: PASS, including the pre-existing embedding tests.

- [ ] **Step 5: Prove the non-force gate bites (mutation)**

Temporarily add `profile.embedding_fingerprint = None` inside the `LLMError` handler *before* the `if force:` line, so it clears on both paths.

Run: `uv run pytest tests/embeddings/test_refresh_profile_fingerprint.py -v`
Expected: `test_non_force_failure_leaves_all_three_untouched` FAILS.

**Revert the added line** and re-run to confirm green.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/embeddings/refresh.py tests/embeddings/test_refresh_profile_fingerprint.py
git commit -m "feat(freshness): the fingerprint travels with the vector in every branch"
```

---

### Task 4: The embedding repair arm

**Files:**
- Modify: `src/grant_lab/embeddings/refresh.py` — `_refresh_missing_embeddings_batch` (profile phase, lines 318-345) and `refresh_missing_embeddings_tick` (drain loop, lines 200-232)
- Test: `tests/embeddings/test_refresh_tick_staleness.py`

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

Create `tests/embeddings/test_refresh_tick_staleness.py`.

```python
from grant_lab.embeddings.refresh import (
    _refresh_missing_embeddings_batch,
    refresh_missing_embeddings_tick,
)
from grant_lab.profiles.fingerprint import embedding_fingerprint


def test_a_changed_text_is_stale_even_with_a_current_vector_and_model(
    session_factory, session, make_profile, fake_embedder
):
    """GATE 4. Vector present, model current -- invisible to the old predicate."""
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=fake_embedder)
    session.refresh(p)
    assert p.embedding_fingerprint == embedding_fingerprint(p)

    p.description = "Cardiac imaging"
    session.commit()
    stale_fp = p.embedding_fingerprint

    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=fake_embedder)
    session.refresh(p)
    assert p.embedding_fingerprint != stale_fp
    assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_stale_profile_behind_a_full_fresh_page_is_still_reached(
    session_factory, session, make_profile, fake_embedder
):
    """GATE 5 -- the one that would have caught rev 1's mistake.

    Uses batch_size=2 rather than a realistic corpus, so the loop's exit
    condition is what is under test, not the row count.
    """
    fresh = [make_profile(description=f"Fresh {i}") for i in range(4)]
    session.commit()
    refresh_missing_embeddings_tick(
        session_factory=session_factory, embed_client=fake_embedder, batch_size=2
    )

    stale = make_profile(description="Originally this")
    session.commit()
    refresh_missing_embeddings_tick(
        session_factory=session_factory, embed_client=fake_embedder, batch_size=2
    )
    session.refresh(stale)
    assert stale.embedding is not None

    # Now make the HIGHEST id stale, behind two full pages of fresh rows.
    stale.description = "Changed to something else"
    session.commit()

    refresh_missing_embeddings_tick(
        session_factory=session_factory, embed_client=fake_embedder, batch_size=2
    )
    session.refresh(stale)
    assert stale.embedding_fingerprint == embedding_fingerprint(stale), (
        "the drain stopped at the first page with nothing to do and threw "
        "away its cursor"
    )


def test_a_profile_cleared_to_blank_is_invalidated_by_the_tick(
    session_factory, session, make_profile, fake_embedder
):
    """GATE 8 -- hole 4, a defect in already-shipped code.

    refresh.py:328 excluded blank profiles from the tick, so the branch that
    wipes a cleared profile's vector could only ever fire from the request
    path -- the lossy one.
    """
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=fake_embedder)
    session.refresh(p)
    assert p.embedding is not None

    p.description = ""
    p.structured = {}
    session.commit()

    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=fake_embedder)
    session.refresh(p)
    assert p.embedding is None
    assert p.embedding_model is None
    assert p.embedding_fingerprint is None


def test_an_already_blank_and_already_cleared_profile_is_not_rescanned_forever(
    session_factory, session, make_profile, fake_embedder
):
    """The other half of gate 8: including blank rows must not break termination."""
    p = make_profile(description="")
    p.structured = {}
    session.commit()
    s = _refresh_missing_embeddings_batch(
        session_factory=session_factory,
        embed_client=fake_embedder,
        batch_size=200,
        force=False,
        after_call_id=0,
        after_profile_id=0,
    )
    assert s["profiles_embedded"] == 0
    assert s["profiles_failed"] == 0


def test_force_mode_ignores_the_fingerprint(session_factory, session, make_profile, fake_embedder):
    """GATE 7. force=True must re-embed every embeddable row regardless."""
    p = make_profile(description="Liver disease")
    session.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=fake_embedder)
    session.refresh(p)
    fake_embedder.calls = 0

    refresh_missing_embeddings_tick(
        session_factory=session_factory, embed_client=fake_embedder, force=True
    )
    assert fake_embedder.calls > 0, "force mode skipped a fingerprint-fresh row"


def test_the_staleness_decision_does_not_load_the_vector(
    session_factory, session, make_profile, fake_embedder
):
    """GATE 6. Parsing 1024-dim vectors is the measured dominant cost of a
    profile load; loading them to decide whether to replace them is a
    regression. Assert on the emitted SQL of the candidate SELECT."""
    statements = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    from sqlalchemy import event

    engine = session.bind
    event.listen(engine, "before_cursor_execute", _record)
    try:
        make_profile(description="Liver disease")
        session.commit()
        statements.clear()
        _refresh_missing_embeddings_batch(
            session_factory=session_factory,
            embed_client=fake_embedder,
            batch_size=200,
            force=False,
            after_call_id=0,
            after_profile_id=0,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    candidate_selects = [s for s in statements if "FROM profiles" in s and "SELECT" in s]
    assert candidate_selects, "no candidate SELECT was emitted"
    assert not any(
        "profiles.embedding \n" in s or "profiles.embedding," in s or "profiles.embedding " in s
        for s in candidate_selects
    ), "the candidate SELECT loaded the 1024-dim vector"
```

`fake_embedder` must count `embed` calls. If no such fixture exists, add one to this file that wraps the Task 3 fake and increments `self.calls`.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/embeddings/test_refresh_tick_staleness.py -v`
Expected: FAIL — gate 4 and gate 8 fail because the SQL predicate never selects those rows; gate 5 fails because the drain breaks early.

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

`build_profile_text` and `embedding_fingerprint` both duck-type on attribute access, and a SQLAlchemy `Row` exposes `.description` and `.structured`, so both work on `r` directly. Add a tiny helper beside the batch function so the intent is explicit:

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

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/embeddings/ -v`
Expected: PASS.

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

Run: `uv run pytest tests/embeddings/test_refresh_tick_staleness.py -v`
Expected: `test_a_stale_profile_behind_a_full_fresh_page_is_still_reached` FAILS.

**Restore the fix** and re-run.

- [ ] **Step 7: Note the remaining bound in the plan's defects file, not in code**

`_TICK_MAX_BATCHES = 200` (`refresh.py:147`) still caps the drain, and a capped tick restarts at `after_profile_id=0` tomorrow. At 200 batches x 200 rows the headroom is 40,000 profiles, so at this project's scale (tens) it cannot bite. It is a real bound, not a bug today. Record it in `PLAN-DEFECTS.md` as a follow-up rather than growing this task.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/embeddings/refresh.py tests/embeddings/test_refresh_tick_staleness.py
git commit -m "fix(freshness): the daily tick breaks on rows examined, not rows worked"
```

---

### Task 5: Write site — rankings

**MUST land before Task 7.** Task 7 treats a NULL `profile_fingerprint` as stale. This task is what makes the worker write a non-NULL one. Reversed, every freshly written ranking is immediately re-enqueued — an infinite re-rank loop.

**Files:**
- Modify: `src/grant_lab/ranking/worker.py` — capture at line 169, write at line 245, retained-analysis guard at lines 238-243
- Test: `tests/ranking/test_worker_fingerprint.py`

**Interfaces:**
- Consumes: `rank_fingerprint` (Task 2); `CallRanking.profile_fingerprint` (Task 1).
- Produces: every `CallRanking` the worker writes carries `profile_fingerprint = rank_fingerprint(profile)` as computed **before** `rank_pair` ran. Task 7 compares against exactly this value.

**The rationale is robustness, not a live bug.** An earlier revision claimed a concurrent edit during `rank_pair` would be mis-stamped. That is false here: `profile` is loaded at `worker.py:131` and the session is not committed, refreshed or expired before `:245` — the ledger write at `:203-211` uses a separate session on purpose — so hashing at either point returns the same value today. Capture early because it is unambiguous and survives a future refactor that introduces a commit or a `session.refresh` in between. The gate below is written against that real failure, not the imaginary one.

- [ ] **Step 1: Write the failing tests**

Create `tests/ranking/test_worker_fingerprint.py`. Read the existing worker tests first and reuse their job/fixture helpers.

**Verified names for every test in Tasks 5-7:** the worker entry point is
`drain_queue(*, session_factory, client, embedding_threshold, ...)` in
`ranking/worker.py:31` — there is no `run_ranking_batch`. `LLMError` lives in
`grant_lab.ranking.ollama_client:10`, not in an `llm.errors` module.
`RankingJobStatus` is exported from `grant_lab.db.models`. `enqueue_for_pair` is
`(session, *, call_id, profile_id)`. `embedding_threshold` has no default and must
be passed.

```python
from grant_lab.db.models import CallRanking
from grant_lab.profiles.fingerprint import rank_fingerprint


def test_the_worker_stamps_the_fingerprint_it_ranked(session, ranked_pair):
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    assert cr.profile_fingerprint == rank_fingerprint(profile)
    assert cr.profile_fingerprint.startswith("v1:")


def test_persistence_keeps_the_captured_value_across_a_refresh(
    session, session_factory, make_call, make_profile, fake_rank_client
):
    """GATE 9. The mutation this must catch is moving the hash to AFTER a
    session.refresh that follows rank_pair -- not inserting a refresh before
    it, which would leave the invariant intact because the ranker would then
    see the refreshed profile too."""
    from grant_lab.ranking.worker import drain_queue

    p = make_profile(description="Liver disease")
    c = make_call()
    session.commit()
    captured = rank_fingerprint(p)

    # An independent session edits the profile while the job is queued.
    with session_factory() as other:
        from grant_lab.db.models import Profile

        other.get(Profile, p.id).description = "Cardiac imaging"
        other.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    cr = session.get(CallRanking, (c.id, p.id))
    assert cr.profile_fingerprint == captured, (
        "the worker stamped a profile the ranker never saw"
    )


def test_retained_analysis_is_dropped_when_the_profile_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """GATE 10. worker.py:238-243 copies the previous run's summary, notes and
    analysis_model when this run produced none. After a profile edit that
    retained advice describes the OLD profile, and stamping it with the NEW
    fingerprint would make it permanently invisible to repair."""
    from grant_lab.db.models import Profile
    from grant_lab.ranking.worker import drain_queue

    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Old advice about liver disease"
    cr.applicant_notes = [{"note": "old"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    with session_factory() as other:
        other.get(Profile, profile_id).description = "Cardiac imaging"
        other.commit()

    # A gate-fail re-rank: produces a score but no summary and no notes.
    fake_rank_client.produce_analysis = False
    from grant_lab.ranking.queue import enqueue_for_pair

    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    session.expunge_all()
    cr = session.get(CallRanking, (call_id, profile_id))
    assert cr.summary is None
    assert cr.applicant_notes is None
    assert cr.analysis_model is None


def test_retained_analysis_survives_when_the_profile_did_not_change(
    session, session_factory, ranked_pair, fake_rank_client
):
    """The discriminating control for gate 10: an unchanged profile must KEEP
    its analysis, otherwise the guard is just 'always clear'."""
    from grant_lab.ranking.queue import enqueue_for_pair
    from grant_lab.ranking.worker import drain_queue

    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Advice that is still current"
    cr.applicant_notes = [{"note": "still good"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    fake_rank_client.produce_analysis = False
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    session.expunge_all()
    cr = session.get(CallRanking, (call_id, profile_id))
    assert cr.summary == "Advice that is still current"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/ranking/test_worker_fingerprint.py -v`
Expected: FAIL — `assert None == 'v1:...'`.

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

Replace the retained-analysis block (currently lines 244-249, inside `if new_summary is None and new_notes is None:`):

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

Run: `uv run pytest tests/ranking/ -v`
Expected: PASS.

- [ ] **Step 7: Prove gate 10 bites (mutation)**

Temporarily drop the fingerprint condition, restoring `if existing is not None:`.

Run: `uv run pytest tests/ranking/test_worker_fingerprint.py -v`
Expected: `test_retained_analysis_is_dropped_when_the_profile_changed` FAILS and `test_retained_analysis_survives_when_the_profile_did_not_change` still PASSES. Both matter — the second is what proves the guard is not simply "always clear".

**Restore** and re-run.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/worker.py tests/ranking/test_worker_fingerprint.py
git commit -m "feat(freshness): a ranking records the profile the ranker actually saw"
```

---

### Task 6: The worker defers a stale vector

**Files:**
- Modify: `src/grant_lab/ranking/worker.py` (after the `is_active` check, before `rank_pair`)
- Test: `tests/ranking/test_worker_defers_stale_vector.py`

**Interfaces:**
- Consumes: `embedding_fingerprint` (Task 2); the write-site invariant (Task 3); `queue.revert_to_pending`.
- Produces: `_STALE_VECTOR_MAX_DEFER_HOURS: int = 24` in `worker.py`.

**The commit is the whole finding.** `revert_to_pending` (`queue.py:302-318`) issues an `UPDATE` and returns a rowcount; it does **not** commit. Every other early exit in the worker commits before `continue` (`worker.py:139-140`, `:149-159`). Without that commit the deferral is rolled back when the session closes, the job stays RUNNING on its previously committed claim, and `requeue_stuck_running` picks it up — bumping `attempts` and eventually dead-lettering the pair. The deferral would burn exactly the retries it exists to protect.

**A NULL fingerprint does not defer.** Found while planning, and not in the spec: on the first deploy every profile has a vector and a NULL fingerprint, so a naive `!=` would defer *every* pair until the 03:30 tick refills them. NULL means unknown provenance — the same as a NULL vector — so proceed. The ranking is stamped with `embedding_fingerprint: None` inside its payload, so once the tick fills the real value the rank fingerprint changes and the pair is re-ranked. Self-healing, no deploy-wide pause.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime, timedelta

from grant_lab.db.models import CallRanking, RankingJob, RankingJobStatus
from grant_lab.ranking.worker import drain_queue


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

Run: `uv run pytest tests/ranking/test_worker_defers_stale_vector.py -v`
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

Add the imports:

```python
from grant_lab.profiles.fingerprint import embedding_fingerprint
from grant_lab.ranking.queue import revert_to_pending
```

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

Ensure `timedelta` is imported from `datetime` at the top of the file.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/ranking/ -v`
Expected: PASS.

- [ ] **Step 5: Prove the commit is load-bearing (mutation)**

Delete the `session.commit()` line inside the deferral block.

Run: `uv run pytest tests/ranking/test_worker_defers_stale_vector.py -v`
Expected: `test_a_stale_vector_defers_without_burning_an_attempt` FAILS — the fresh session sees the job still RUNNING. This is the finding that made this task necessary; if the test stays green, the fresh-session assertion is not actually reading from a new session and must be fixed before continuing.

**Restore the commit** and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/worker.py tests/ranking/test_worker_defers_stale_vector.py
git commit -m "feat(freshness): defer a pair whose vector has not caught up, and commit the deferral"
```

---

### Task 7: The ranking repair arm

**Task 5 must already be merged.** See "Task order is load-bearing".

**Files:**
- Modify: `src/grant_lab/ranking/queue.py` — `reconcile_missing_rankings`, lines 112-176
- Test: `tests/ranking/test_reconcile_staleness.py`

**Interfaces:**
- Consumes: `rank_fingerprint` (Task 2); `LEGACY_SENTINEL` (Task 2); the write-site invariant (Task 5).
- Produces: `reconcile_missing_rankings(session) -> int`, signature unchanged.

- [ ] **Step 1: Write the failing tests**

```python
from grant_lab.db.models import CallRanking, RankingJob
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking.queue import reconcile_missing_rankings


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
    import datetime as dt

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


def test_no_active_profiles_issues_no_insert(session):
    """GATE 14. An empty VALUES list is a SYNTAX ERROR, so without the early
    return the daily tick raises on a fresh or fully-archived database --
    where today's CROSS JOIN simply degrades to zero rows."""
    from grant_lab.db.models import Profile

    session.query(Profile).update({Profile.is_active: False})
    session.commit()
    assert reconcile_missing_rankings(session) == 0
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/ranking/test_reconcile_staleness.py -v`
Expected: FAIL — gates 11, 12b and 13 return 0 because the current `NOT EXISTS` only asks whether a ranking exists.

- [ ] **Step 3: Implement**

Add the import:

```python
from grant_lab.profiles.fingerprint import LEGACY_SENTINEL, rank_fingerprint
```

Replace the body of `reconcile_missing_rankings` from the `cutoff = ` line through the `session.execute(...)` call:

```python
    cutoff = datetime.now(UTC) - timedelta(days=_RECONCILE_DAYS)
    dead_letter_cutoff = datetime.now(UTC) - timedelta(days=_DEAD_LETTER_SUPPRESS_DAYS)

    # Fingerprints are Python over JSONB and cannot be computed in SQL, so the
    # active profiles come back first and their fingerprints ride in as a
    # VALUES list.
    profiles = session.execute(select(Profile).where(Profile.is_active.is_(True))).scalars().all()
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

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/ranking/ -v`
Expected: PASS.

- [ ] **Step 5: Prove gate 12b bites (mutation)**

Temporarily drop `AND cr.ranked_at >= p.updated_at`, leaving `cr.profile_fingerprint = :legacy` unconditional.

Run: `uv run pytest tests/ranking/test_reconcile_staleness.py -v`
Expected: `test_a_legacy_pair_whose_profile_was_edited_IS_re_enqueued` FAILS, and `test_a_legacy_pair_with_an_untouched_profile_is_skipped` still PASSES.

**Restore** and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/ranking/queue.py tests/ranking/test_reconcile_staleness.py
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

```python
from unittest.mock import patch


def test_an_areas_only_edit_schedules_the_refresh(client, owner_profile, auth_headers):
    """GATE 2. build_profile_text composes description OR areas OR org fields,
    so a profile with an empty description and non-empty areas has real
    embedding text -- but the guard read `body.description`, so editing one
    scheduled NOTHING and it stayed stale forever."""
    with patch("grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background") as task:
        r = client.put(
            f"/api/profiles/{owner_profile.id}",
            json={"description": "", "areas": "liver cancer\ntransplant rejection"},
            headers=auth_headers,
        )
    assert r.status_code == 200
    assert task.called, "an areas-only edit scheduled no refresh and no backfill"


def test_an_identical_put_schedules_nothing(client, owner_profile, auth_headers, body_for):
    """GATE 3. Today an unchanged PUT enqueues 90 days of ranking jobs
    whenever the description happens to be non-empty.

    The differing-representation number is seeded DIRECTLY below, not via a
    previous PUT that would already have normalized it -- otherwise this gate
    passes on already-normalized values and proves nothing."""
    owner_profile.structured = {**owner_profile.structured, "budget_min": 1000.0}
    body = body_for(owner_profile)

    with patch("grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background") as task:
        r = client.put(f"/api/profiles/{owner_profile.id}", json=body, headers=auth_headers)
    assert r.status_code == 200
    assert not task.called, "an unchanged PUT enqueued a 90-day backfill"


def test_a_real_change_still_schedules(client, owner_profile, auth_headers, body_for):
    """Discriminating control: without this, 'never schedule' passes gate 3."""
    body = {**body_for(owner_profile), "description": "Something genuinely different"}
    with patch("grant_lab.web.api.profiles.post_edit.refresh_and_backfill_in_background") as task:
        r = client.put(f"/api/profiles/{owner_profile.id}", json=body, headers=auth_headers)
    assert r.status_code == 200
    assert task.called
```

`body_for(profile)` must build a `ProfileUpdateIn` payload that round-trips the profile unchanged. If no such helper exists, write one in this file from the model's current values.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_api_profile_update_scheduling.py -v`
Expected: `test_an_areas_only_edit_schedules_the_refresh` FAILS (`task.called` is False).

- [ ] **Step 3: Implement**

Add the import:

```python
from grant_lab.profiles.fingerprint import rank_fingerprint
```

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

Run: `uv run pytest tests/test_api_profile_update_scheduling.py tests/web -v`
Expected: PASS. Some existing profile-update tests may assert the old scheduling behaviour; read each failure and update the test only where the new behaviour is deliberate — record anything ambiguous in `PLAN-DEFECTS.md` rather than silently rewriting an assertion.

- [ ] **Step 5: Prove gate 2 bites (mutation)**

Temporarily restore `if body.description.strip():`.

Run: `uv run pytest tests/test_api_profile_update_scheduling.py -v`
Expected: `test_an_areas_only_edit_schedules_the_refresh` FAILS.

**Restore** and re-run.

- [ ] **Step 6: Full suite and commit**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: **more than 5364 passed, exactly 1 skipped.** `0 skipped` means a live Anthropic API call ran — stop and record it. Measure the new total; do not predict it.

```bash
uv run ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py
git add -A
git commit -m "fix(freshness): an areas-only edit no longer schedules nothing"
```

---

## Self-review notes

**Spec coverage.** Every change item in the spec maps to a task: changes 1-2 to Task 1 and 2, change 3 to Task 3, change 5 to Task 4, change 4 to Task 5, change 7 to Task 6, change 6 to Task 7, change 8 to Task 8. All 20 spec gates appear: 1 and 3 in Task 2 and 8, 2 in Task 8, 4-8 in Task 4, 9-10 in Task 5, 11-14 in Task 7, 15-17 in Task 6, 18-19 in Task 3.

**Two deliberate departures from the spec**, both recorded here rather than by editing the spec:

1. **A NULL `embedding_fingerprint` does not defer** (Task 6). The spec says a mismatch defers and only exempts a NULL *vector*. On the first deploy every profile has a vector and a NULL fingerprint, so the spec as written would pause all ranking until 03:30. Treating NULL as unknown-provenance is self-healing and avoids that.
2. **The `_TICK_MAX_BATCHES` bound is recorded, not fixed** (Task 4 step 7). It cannot bite at 40,000 profiles of headroom against tens of profiles.

**Discriminating controls.** Five gates would pass under a trivially wrong implementation without their paired control, so each has one: Task 5's "analysis survives when unchanged", Task 6's "a current vector ranks normally", Task 7's "a matching fingerprint is not re-enqueued", Task 8's "a real change still schedules", and Task 4's "already-blank rows do not rescan forever".

**Type consistency.** `embedding_fingerprint(profile) -> str` and `rank_fingerprint(profile) -> str` are used with those exact names in Tasks 3, 4, 5, 6, 7 and 8. `LEGACY_SENTINEL` is defined in Task 2 and consumed in Task 7. `profiles_seen` is produced in Task 4 step 3 and consumed in Task 4 step 4. `captured_fingerprint` is local to Task 5.
