# Profile Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-kind (org/group/user) profile model with a two-kind model — `organizational_unit` (with a `level`) and `individual` — and add structured eligibility/org/funding/geo "headline" fields that enrich LLM ranking, with kind-aware creation, bootstrap, and editing.

**Architecture:** Single `profiles` table (Approach A): `kind` becomes Text+Check, new `level` and reserved `parent_profile_id` columns, and the new fields live in the existing `structured` JSONB under namespaces (`eligibility.*`, `org.*`, `funding.*`, `geo.*`). A new `profiles/fields.py` module is the single source of truth for the field vocabularies, validation caps, and parse helpers, shared by the route layer, the bootstrap parser, and the ranking prompt. New fields are **prompt-only** (the embedding stays `description + areas`); no new deterministic gate.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.x + Alembic, PostgreSQL 16, pytest + testcontainers, `uv`, `ruff`.

**Spec:** [docs/superpowers/specs/2026-06-05-profile-redesign-design.md](../specs/2026-06-05-profile-redesign-design.md)

**Pre-flight (run once before Task 1):**
```bash
cd /home/parlar_ai/dev/call-monitor
git switch -c feature/profile-redesign
uv run pytest tests/test_profiles_bootstrap.py tests/test_web_profiles.py -q   # baseline green
```

---

## Task 1: Field vocabulary + validation module

Single source of truth for the new field allow-lists, caps, and parsing. Pure functions, no framework imports — easy to unit-test and reused by routes, bootstrap, and prompt.

**Files:**
- Create: `src/call_manager/profiles/fields.py`
- Test: `tests/test_profiles_fields.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_fields.py
import pytest

from call_manager.profiles.fields import (
    APPLICANT_TYPES,
    CAREER_STAGES,
    FUNDING_INSTRUMENTS,
    LEVELS,
    MAX_SHORT_TEXT,
    MAX_TAG_LEN,
    ORG_TYPES,
    FieldValidationError,
    parse_structured,
)


def test_allowlists_are_frozen_and_nonempty():
    for s in (APPLICANT_TYPES, CAREER_STAGES, ORG_TYPES, FUNDING_INSTRUMENTS, LEVELS):
        assert isinstance(s, frozenset) and s


def test_parse_structured_individual_happy_path():
    out = parse_structured(
        kind="individual",
        raw={
            "areas": "oncology\nimaging",
            "career_stage": "postdoc",
            "applicant_types": "solo_pi\nconsortium_partner",
            "role": "staff scientist",
            "years_since_phd": "4",
            "funding_instruments": "fellowship",
        },
    )
    assert out["areas"] == ["oncology", "imaging"]
    assert out["eligibility"]["career_stage"] == "postdoc"
    assert out["eligibility"]["applicant_types"] == ["solo_pi", "consortium_partner"]
    assert out["eligibility"]["role"] == "staff scientist"
    assert out["eligibility"]["years_since_phd"] == 4
    assert out["funding"]["instruments"] == ["fellowship"]


def test_parse_structured_rejects_unknown_enum():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="individual", raw={"career_stage": "wizard"})
    assert e.value.status_code == 400


def test_parse_structured_rejects_overlong_tag():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="organizational_unit", raw={"capabilities": "x" * (MAX_TAG_LEN + 1)})
    assert e.value.status_code == 400


def test_parse_structured_rejects_overlong_short_text():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="organizational_unit", raw={"scale": "y" * (MAX_SHORT_TEXT + 1)})
    assert e.value.status_code == 413


def test_parse_structured_rejects_years_out_of_range():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="individual", raw={"years_since_phd": "999"})
    assert e.value.status_code == 400


def test_parse_structured_individual_drops_org_namespace():
    out = parse_structured(kind="individual", raw={"capabilities": "biobank"})
    assert "org" not in out or not out["org"].get("capabilities")


def test_parse_structured_rejects_over_100_items():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(
            kind="organizational_unit",
            raw={"capabilities": "\n".join(str(i) for i in range(101))},
        )
    assert e.value.status_code == 400


def test_sanitize_structured_drops_bad_llm_values():
    from call_manager.profiles.fields import sanitize_structured

    out = sanitize_structured(
        kind="individual",
        structured={
            "eligibility": {"career_stage": "wizard", "applicant_types": ["solo_pi", "bogus"]},
            "funding": {"instruments": ["fellowship", "nope"]},
        },
    )
    assert out["eligibility"]["career_stage"] is None
    assert out["eligibility"]["applicant_types"] == ["solo_pi"]
    assert out["funding"]["instruments"] == ["fellowship"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_profiles_fields.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'call_manager.profiles.fields'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/call_manager/profiles/fields.py
"""Single source of truth for profile structured-field vocabularies, caps, and
parsing. Framework-agnostic: raises FieldValidationError (with a status hint)
which the route layer maps to HTTPException. Shared by the create/edit routes,
the bootstrap response parser, and the ranking prompt."""

from __future__ import annotations

# Allow-lists (enum-style fields). frozenset so they can't be mutated at runtime.
APPLICANT_TYPES = frozenset(
    {"solo_pi", "consortium_coordinator", "consortium_partner", "institution"}
)
CAREER_STAGES = frozenset(
    {"phd_student", "postdoc", "junior_pi", "senior_pi", "group_leader"}
)
ORG_TYPES = frozenset(
    {"research_organisation", "university_hospital", "public_body", "sme", "other"}
)
FUNDING_INSTRUMENTS = frozenset(
    {"project_grant", "fellowship", "mobility", "infrastructure", "programme"}
)
LEVELS = frozenset({"lab", "group", "department", "clinic", "institution", "other"})

# Caps (see spec "Validation caps").
MAX_LIST_ENTRIES = 100
MAX_TAG_LEN = 80
MAX_SHORT_TEXT = 200
YEARS_SINCE_PHD_MIN = 0
YEARS_SINCE_PHD_MAX = 80


class FieldValidationError(Exception):
    """Validation failure with an HTTP status hint (400 bad value, 413 too long)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _tags(raw, *, field: str) -> list[str]:
    """Split a textarea (one entry per line) into a count/length-checked list.

    Rejects (does not silently truncate) over-count or over-length input — the
    spec requires a 400, not silent data loss."""
    if raw is None:
        return []
    items = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    if len(items) > MAX_LIST_ENTRIES:
        raise FieldValidationError(
            f"{field}: too many items (max {MAX_LIST_ENTRIES})", status_code=400
        )
    for item in items:
        if len(item) > MAX_TAG_LEN:
            raise FieldValidationError(
                f"{field}: item exceeds {MAX_TAG_LEN} chars", status_code=400
            )
    return items


def _enum_list(raw, *, allowed: frozenset[str], field: str) -> list[str]:
    out = _tags(raw, field=field)
    for v in out:
        if v not in allowed:
            raise FieldValidationError(f"{field}: unknown value {v!r}", status_code=400)
    return out


def _enum_scalar(raw, *, allowed: frozenset[str], field: str) -> str | None:
    v = (raw or "").strip()
    if not v:
        return None
    if v not in allowed:
        raise FieldValidationError(f"{field}: unknown value {v!r}", status_code=400)
    return v


def _short_text(raw, *, field: str) -> str | None:
    v = (raw or "").strip()
    if not v:
        return None
    if len(v) > MAX_SHORT_TEXT:
        raise FieldValidationError(
            f"{field}: exceeds {MAX_SHORT_TEXT} chars", status_code=413
        )
    return v


def _bounded_int(raw, *, low: int, high: int, field: str) -> int | None:
    v = (raw or "").strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        return None  # optional numeric box: "n/a" → None (matches existing UX)
    if n < low or n > high:
        raise FieldValidationError(f"{field}: out of range [{low},{high}]", status_code=400)
    return n


def parse_structured(*, kind: str, raw: dict) -> dict:
    """Parse + validate the new + existing structured inputs for a profile.

    `raw` is a flat dict of string form values (textarea/inputs). Returns the
    namespaced `structured` dict. Fields not relevant to `kind` are dropped.
    Existing rubric fields (areas/budget/trl/must_include/hard_exclusions) are
    parsed by the caller and merged in — this function owns only the new
    namespaces plus `areas` (shared). Raises FieldValidationError on bad input.
    """
    out: dict = {"areas": _tags(raw.get("areas"), field="areas")}

    eligibility = {
        "applicant_types": _enum_list(
            raw.get("applicant_types"), allowed=APPLICANT_TYPES, field="applicant_types"
        ),
        "role": _short_text(raw.get("role"), field="role"),
        "years_since_phd": _bounded_int(
            raw.get("years_since_phd"),
            low=YEARS_SINCE_PHD_MIN,
            high=YEARS_SINCE_PHD_MAX,
            field="years_since_phd",
        ),
    }
    if kind == "individual":
        eligibility["career_stage"] = _enum_scalar(
            raw.get("career_stage"), allowed=CAREER_STAGES, field="career_stage"
        )
    out["eligibility"] = eligibility

    if kind == "organizational_unit":
        out["org"] = {
            "org_type": _enum_scalar(raw.get("org_type"), allowed=ORG_TYPES, field="org_type"),
            "capabilities": _tags(raw.get("capabilities"), field="capabilities"),
            "patient_populations": _tags(
                raw.get("patient_populations"), field="patient_populations"
            ),
            "scale": _short_text(raw.get("scale"), field="scale"),
        }

    out["funding"] = {
        "instruments": _enum_list(
            raw.get("funding_instruments"), allowed=FUNDING_INSTRUMENTS, field="funding_instruments"
        ),
        "funder_exclusions": _tags(raw.get("funder_exclusions"), field="funder_exclusions"),
    }
    out["geo"] = {
        "country": _short_text(raw.get("country"), field="country"),
        "host_institution": _short_text(raw.get("host_institution"), field="host_institution"),
    }
    return out


def sanitize_structured(*, kind: str, structured: dict) -> dict:
    """Coerce an LLM-suggested (nested) structured dict to a valid shape.

    Unlike parse_structured (strict — raises on bad USER form input), this is
    LENIENT: it silently drops unknown enum values, over-length items, and
    over-count lists, because the input is machine-generated bootstrap output,
    not a user form. Used on create so LLM suggestions are never persisted
    unvalidated. Returns only the namespaces (caller keeps description/areas/
    rubric from the draft)."""
    structured = structured or {}

    def _clean_tags(v):
        if not isinstance(v, list):
            return []
        out = [str(x).strip() for x in v if str(x).strip()][:MAX_LIST_ENTRIES]
        return [x for x in out if len(x) <= MAX_TAG_LEN]

    def _clean_enum_list(v, allowed):
        return [x for x in _clean_tags(v) if x in allowed]

    def _clean_enum(v, allowed):
        v = str(v).strip() if v is not None else ""
        return v if v in allowed else None

    def _clean_text(v):
        v = str(v).strip() if v is not None else ""
        return v[:MAX_SHORT_TEXT] or None

    def _clean_years(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return v if YEARS_SINCE_PHD_MIN <= v <= YEARS_SINCE_PHD_MAX else None

    elig_in = structured.get("eligibility") or {}
    org_in = structured.get("org") or {}
    fund_in = structured.get("funding") or {}
    geo_in = structured.get("geo") or {}

    eligibility = {
        "applicant_types": _clean_enum_list(elig_in.get("applicant_types"), APPLICANT_TYPES),
        "role": _clean_text(elig_in.get("role")),
        "years_since_phd": _clean_years(elig_in.get("years_since_phd")),
    }
    if kind == "individual":
        eligibility["career_stage"] = _clean_enum(elig_in.get("career_stage"), CAREER_STAGES)

    out: dict = {"eligibility": eligibility}
    if kind == "organizational_unit":
        out["org"] = {
            "org_type": _clean_enum(org_in.get("org_type"), ORG_TYPES),
            "capabilities": _clean_tags(org_in.get("capabilities")),
            "patient_populations": _clean_tags(org_in.get("patient_populations")),
            "scale": _clean_text(org_in.get("scale")),
        }
    out["funding"] = {
        "instruments": _clean_enum_list(fund_in.get("instruments"), FUNDING_INSTRUMENTS),
        "funder_exclusions": _clean_tags(fund_in.get("funder_exclusions")),
    }
    out["geo"] = {
        "country": _clean_text(geo_in.get("country")),
        "host_institution": _clean_text(geo_in.get("host_institution")),
    }
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_profiles_fields.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/profiles/fields.py tests/test_profiles_fields.py
git commit -m "feat(profiles): central field vocabulary + validation module"
```

---

## Task 2: Model changes — kind Text+Check, level, parent_profile_id, enums

**Files:**
- Modify: `src/call_manager/db/models.py` (ProfileKind enum at line 190-193; Profile `__table_args__` and columns at line ~210-247)
- Test: `tests/test_models_profile_redesign.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_profile_redesign.py
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Profile, ProfileKind, ProfileLevel, User


def _user(s):
    u = User(email="owner@ki.se")
    s.add(u)
    s.flush()
    return u.id


def test_kind_enum_has_two_members():
    assert {k.value for k in ProfileKind} == {"organizational_unit", "individual"}


def test_create_unit_with_level_and_parent(pg_session_factory):
    with pg_session_factory() as s:
        uid = _user(s)
        parent = Profile(
            name="Hospital", kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level=ProfileLevel.INSTITUTION.value, created_by=uid,
        )
        s.add(parent)
        s.flush()
        child = Profile(
            name="Path Lab", kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level=ProfileLevel.LAB.value, parent_profile_id=parent.id, created_by=uid,
        )
        s.add(child)
        s.commit()
        got = s.execute(select(Profile).where(Profile.name == "Path Lab")).scalar_one()
        assert got.parent_profile_id == parent.id
        assert got.level == "lab"


def test_individual_allows_null_level(pg_session_factory):
    with pg_session_factory() as s:
        uid = _user(s)
        s.add(Profile(name="Dr A", kind="individual", level=None, created_by=uid))
        s.commit()  # no cross-field NOT NULL constraint


def test_bad_kind_rejected_by_check(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        uid = _user(s)
        s.add(Profile(name="X", kind="banana", created_by=uid))
        s.commit()


def test_bad_level_rejected_by_check(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        uid = _user(s)
        s.add(Profile(name="X", kind="organizational_unit", level="galaxy", created_by=uid))
        s.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models_profile_redesign.py -q`
Expected: FAIL (ImportError: `ProfileLevel`, and/or kind enum has 3 members)

- [ ] **Step 3: Write minimal implementation**

Replace the `ProfileKind` enum (models.py:190-193):

```python
class ProfileKind(enum.StrEnum):
    ORGANIZATIONAL_UNIT = "organizational_unit"
    INDIVIDUAL = "individual"


class ProfileLevel(enum.StrEnum):
    LAB = "lab"
    GROUP = "group"
    DEPARTMENT = "department"
    CLINIC = "clinic"
    INSTITUTION = "institution"
    OTHER = "other"
```

In the `Profile` class, change the `kind` column (models.py:231-233) from the PG enum to Text+Check, and add `level` + `parent_profile_id`. The `kind` mapped type becomes `str`:

```python
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
```

Add the two CheckConstraints to `Profile.__table_args__` (alongside the existing `uq_profiles_active_name` index and embedding constraints):

```python
        CheckConstraint(
            "kind IN ('organizational_unit','individual')",
            name="ck_profiles_kind",
        ),
        CheckConstraint(
            "level IS NULL OR level IN "
            "('lab','group','department','clinic','institution','other')",
            name="ck_profiles_level",
        ),
```

Note: `enum` and `CheckConstraint`/`Text`/`BigInteger`/`ForeignKey` are already imported in models.py.

- [ ] **Step 4: Run test to verify it fails on schema (migration not yet written)**

Run: `uv run pytest tests/test_models_profile_redesign.py -q`
Expected: FAIL — the migrated test DB still has the old `profile_kind` enum column / no `level` column. This is expected; Task 3 adds the migration. (If it errors on `column "level" does not exist`, proceed to Task 3.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/db/models.py tests/test_models_profile_redesign.py
git commit -m "feat(profiles): two-kind model + level + parent_profile_id (model layer)"
```

---

## Task 3: Migration 0034 (kind→Text+Check, level, parent_profile_id)

**Files:**
- Create: `alembic/versions/0034_profile_redesign.py`
- Test: `tests/test_migration_0034_profile_redesign.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0034_profile_redesign.py
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0034_maps_kinds_and_adds_columns(pg_url):
    # NOTE: pg_url is the SHARED session container. This test mutates the schema
    # (downgrade→upgrade) and seeds rows, so the finally MUST (a) delete the
    # seeded rows and (b) leave the schema at head, or it poisons later tests
    # that assume a clean head schema (pg_session_factory truncates rows but not
    # schema version).
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0033")  # back to the pre-redesign enum schema
    eng = create_engine(pg_url)
    try:
        # Seed legacy rows under the 0033 schema (profile_kind enum: org/group/user).
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('m@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='m@ki.se'")).scalar_one()
            for nm, k in [("Org A", "org"), ("Grp B", "group"), ("Dr C", "user")]:
                c.execute(
                    text(
                        "INSERT INTO profiles (name, kind, description, structured, "
                        "is_active, created_by) VALUES (:n, :k, '', '{}'::jsonb, true, :u)"
                    ),
                    {"n": nm, "k": k, "u": uid},
                )
        command.upgrade(cfg, "head")  # apply 0034
        with eng.connect() as c:
            rows = dict(
                c.execute(text("SELECT name, kind FROM profiles ORDER BY name")).all()
            )
            assert rows["Org A"] == "organizational_unit"
            assert rows["Grp B"] == "organizational_unit"
            assert rows["Dr C"] == "individual"
            levels = dict(
                c.execute(text("SELECT name, level FROM profiles ORDER BY name")).all()
            )
            assert levels["Grp B"] == "group"
            assert levels["Org A"] is None  # owner fills in later
            assert levels["Dr C"] is None
            # parent_profile_id column exists and is nullable
            assert c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='profiles' AND column_name='parent_profile_id'"
                )
            ).scalar_one() == "YES"
    finally:
        # Hermetic cleanup: drop the rows we seeded and guarantee head schema.
        with eng.begin() as c:
            c.execute(text("DELETE FROM profiles WHERE name IN ('Org A','Grp B','Dr C')"))
            c.execute(text("DELETE FROM users WHERE email='m@ki.se'"))
        eng.dispose()
        command.upgrade(cfg, "head")  # restore for subsequent tests


def test_0034_downgrade_runs_clean(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0033")  # documented-lossy; must not error
    command.upgrade(cfg, "head")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migration_0034_profile_redesign.py -q`
Expected: FAIL (revision 0034 not found / column `level` missing)

- [ ] **Step 3: Write minimal implementation**

```python
# alembic/versions/0034_profile_redesign.py
"""Profile redesign: two-kind model + level + parent_profile_id

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-05

Upgrade: convert profiles.kind from the profile_kind enum to Text+Check
(org/group -> organizational_unit, user -> individual); add level (group ->
'group', org -> NULL) and a reserved parent_profile_id FK. New structured.*
keys need no migration (read via .get()).

Downgrade is EXPLICITLY LOSSY: organizational_unit -> group, individual ->
user; the original org/group split is unrecoverable and level/parent_profile_id
are dropped. See the design spec.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Convert kind enum -> text (USING cast), remap values, add check.
    op.execute("ALTER TABLE profiles ALTER COLUMN kind TYPE text USING kind::text")
    op.execute("DROP TYPE profile_kind")
    op.execute(
        "UPDATE profiles SET kind = CASE "
        "WHEN kind IN ('org','group') THEN 'organizational_unit' "
        "WHEN kind = 'user' THEN 'individual' ELSE kind END"
    )
    op.create_check_constraint(
        "ck_profiles_kind", "profiles", "kind IN ('organizational_unit','individual')"
    )

    # 2. level (nullable for all kinds).
    op.add_column("profiles", sa.Column("level", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_profiles_level",
        "profiles",
        "level IS NULL OR level IN "
        "('lab','group','department','clinic','institution','other')",
    )
    # Back-map: old 'group' rows are the 'group' level; old 'org' rows -> NULL.
    # We can only tell them apart via the *pre-remap* kind, so derive from the
    # name set we just remapped is impossible — instead set level='group' for
    # rows that were 'group'. Do this BEFORE step 1's UPDATE in practice; see
    # ordering note: we capture it via a temp flag.
    # (Implemented by reordering below.)

    # 3. parent_profile_id (reserved).
    op.add_column(
        "profiles",
        sa.Column("parent_profile_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_profiles_parent_profile_id",
        "profiles",
        "profiles",
        ["parent_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_profiles_parent_profile_id", "profiles", ["parent_profile_id"])


def downgrade() -> None:
    op.drop_index("ix_profiles_parent_profile_id", table_name="profiles")
    op.drop_constraint("fk_profiles_parent_profile_id", "profiles", type_="foreignkey")
    op.drop_column("profiles", "parent_profile_id")
    op.drop_constraint("ck_profiles_level", "profiles", type_="check")
    op.drop_column("profiles", "level")
    # Lossy: collapse back to the legacy enum domain.
    op.drop_constraint("ck_profiles_kind", "profiles", type_="check")
    op.execute(
        "UPDATE profiles SET kind = CASE "
        "WHEN kind = 'organizational_unit' THEN 'group' "
        "WHEN kind = 'individual' THEN 'user' ELSE kind END"
    )
    op.execute("CREATE TYPE profile_kind AS ENUM ('org','group','user')")
    op.execute(
        "ALTER TABLE profiles ALTER COLUMN kind TYPE profile_kind USING kind::profile_kind"
    )
```

**Ordering fix (apply this exact `upgrade()` instead — captures `level='group'` before remapping `kind`):**

```python
def upgrade() -> None:
    # Add level first, populate from the *legacy* kind, THEN remap kind.
    op.add_column("profiles", sa.Column("level", sa.Text(), nullable=True))
    op.execute("UPDATE profiles SET level = 'group' WHERE kind = 'group'")

    op.execute("ALTER TABLE profiles ALTER COLUMN kind TYPE text USING kind::text")
    op.execute("DROP TYPE profile_kind")
    op.execute(
        "UPDATE profiles SET kind = CASE "
        "WHEN kind IN ('org','group') THEN 'organizational_unit' "
        "WHEN kind = 'user' THEN 'individual' ELSE kind END"
    )
    op.create_check_constraint(
        "ck_profiles_kind", "profiles", "kind IN ('organizational_unit','individual')"
    )
    op.create_check_constraint(
        "ck_profiles_level",
        "profiles",
        "level IS NULL OR level IN "
        "('lab','group','department','clinic','institution','other')",
    )
    op.add_column("profiles", sa.Column("parent_profile_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_profiles_parent_profile_id", "profiles", "profiles",
        ["parent_profile_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_profiles_parent_profile_id", "profiles", ["parent_profile_id"])
```

Use the ordering-fixed `upgrade()` and delete the first draft + its inline notes.

- [ ] **Step 4: Run tests (migration + the Task 2 model tests)**

Run: `uv run pytest tests/test_migration_0034_profile_redesign.py tests/test_models_profile_redesign.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0034_profile_redesign.py tests/test_migration_0034_profile_redesign.py
git commit -m "feat(profiles): migration 0034 — two-kind + level + parent_profile_id"
```

---

## Task 4: Seed inputs (`SeedInput` / `DraftProfile`)

**Files:**
- Modify: `src/call_manager/profiles/seed.py` (`SeedInput` line 37-42; `DraftProfile` line 45-53)
- Test: `tests/test_profiles_seed.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_seed.py
from call_manager.profiles.seed import SeedInput


def test_seedinput_carries_level_and_mission():
    s = SeedInput(
        name="Path Lab",
        kind="organizational_unit",
        level="lab",
        mission_text="We run a biobank and digital pathology pipeline.",
    )
    assert s.level == "lab"
    assert "biobank" in s.mission_text
    assert s.members == [] and s.pasted_grants == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_profiles_seed.py -q`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'level'`)

- [ ] **Step 3: Write minimal implementation**

Replace `SeedInput` (seed.py:37-42):

```python
@dataclass
class SeedInput:
    name: str  # profile name, user-visible
    kind: str  # 'organizational_unit' | 'individual'
    level: str | None = None  # organizational unit level; None for individuals
    members: list[Member] = field(default_factory=list)
    pasted_grants: str = ""  # free text — titles + abstracts pasted in by user
    mission_text: str = ""  # org units: mission/strategy/capabilities free text
```

`DraftProfile` (seed.py:45-53) is unchanged — `structured` is already an open dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_profiles_seed.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/profiles/seed.py tests/test_profiles_seed.py
git commit -m "feat(profiles): SeedInput gains level + mission_text"
```

---

## Task 5: Kind-aware bootstrap prompt

**Files:**
- Modify: `src/call_manager/profiles/bootstrap_prompt.py` (`BOOTSTRAP_PROMPT_VERSION` line 7; `_SYSTEM_TEMPLATE`; `build_bootstrap_messages`; `parse_bootstrap_response`)
- Test: `tests/test_profiles_bootstrap_prompt.py` (extend)

- [ ] **Step 1: Write the failing test (append to the existing file)**

```python
# append to tests/test_profiles_bootstrap_prompt.py
from call_manager.profiles.bootstrap_prompt import (
    BOOTSTRAP_PROMPT_VERSION,
    build_bootstrap_messages,
    parse_bootstrap_response,
)


def test_org_unit_prompt_mentions_capabilities_not_members():
    msgs = build_bootstrap_messages(
        profile_name="Path Lab",
        kind="organizational_unit",
        level="lab",
        publications=[],
        pasted_grants="",
        mission_text="We operate a biobank and an imaging core.",
    )
    system = msgs[0]["content"]
    user = msgs[1]["content"]
    assert "organizational_unit" in system or "organizational unit" in system.lower()
    assert "capabilities" in system.lower()
    assert "biobank" in user  # mission text is included


def test_individual_prompt_mentions_career_stage():
    msgs = build_bootstrap_messages(
        profile_name="Dr A",
        kind="individual",
        level=None,
        publications=[],
        pasted_grants="",
        mission_text="",
    )
    assert "career" in msgs[0]["content"].lower()


def test_parse_bootstrap_keeps_new_namespaces_when_present():
    content = (
        '{"description":"d","areas":["x"],'
        '"org":{"capabilities":["biobank"]},'
        '"funding":{"instruments":["infrastructure"]}}'
    )
    draft = parse_bootstrap_response(content)
    assert draft.structured["areas"] == ["x"]
    assert draft.structured.get("org", {}).get("capabilities") == ["biobank"]
    assert draft.structured.get("funding", {}).get("instruments") == ["infrastructure"]


def test_bootstrap_prompt_version_bumped():
    assert BOOTSTRAP_PROMPT_VERSION == "v2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap_prompt.py -q`
Expected: FAIL (`build_bootstrap_messages() got an unexpected keyword 'level'`; version still "v1")

- [ ] **Step 3: Write minimal implementation**

In `bootstrap_prompt.py`: bump version, make the system template kind-aware, thread `level`/`mission_text`, and preserve new namespaces when parsing.

```python
BOOTSTRAP_PROMPT_VERSION = "v2"

_SYSTEM_UNIT = """\
You are a research-funding analyst for an academic hospital. Draft a profile for
an ORGANIZATIONAL UNIT (a {level} — e.g. lab, group, department, clinic, or
institution) from its mission/capabilities text, any member publications, and
pasted past grants. The profile scores incoming funding calls for relevance.

PROFILE:
  Name: {name}
  Kind: organizational_unit
  Level: {level}

Output a single JSON object (all keys optional — omit if unclear, don't fabricate):
  {{
    "description": "<1-2 paragraph summary of what this unit does>",
    "areas": ["<short tag>", ...],
    "budget_min": <int or null>, "budget_max": <int or null>,
    "trl_min": <int 1-9 or null>, "trl_max": <int 1-9 or null>,
    "must_include": ["<keyword>", ...],
    "hard_exclusions": ["<topic the unit won't work on>", ...],
    "org": {{
      "org_type": "research_organisation|university_hospital|public_body|sme|other",
      "capabilities": ["biobank","clinical_trials_unit","imaging_core","registry", ...],
      "patient_populations": ["<tag>", ...],
      "scale": "<free text, e.g. ~400 researchers>"
    }},
    "funding": {{"instruments": ["project_grant|fellowship|mobility|infrastructure|programme", ...]}}
  }}
"""

_SYSTEM_INDIVIDUAL = """\
You are a research-funding analyst for an academic hospital. Draft a profile for
an INDIVIDUAL researcher from their recent publications and pasted past grants.
The profile scores incoming funding calls for relevance, including eligibility
(career stage matters for many calls).

PROFILE:
  Name: {name}
  Kind: individual

Output a single JSON object (all keys optional — omit if unclear, don't fabricate):
  {{
    "description": "<1-2 paragraph summary of this researcher's focus>",
    "areas": ["<short tag>", ...],
    "budget_min": <int or null>, "budget_max": <int or null>,
    "trl_min": <int 1-9 or null>, "trl_max": <int 1-9 or null>,
    "must_include": ["<keyword>", ...],
    "hard_exclusions": ["<topic>", ...],
    "eligibility": {{
      "career_stage": "phd_student|postdoc|junior_pi|senior_pi|group_leader",
      "applicant_types": ["solo_pi","consortium_partner", ...]
    }},
    "funding": {{"instruments": ["project_grant|fellowship|mobility|infrastructure|programme", ...]}}
  }}
"""

_USER_TEMPLATE = """\
MISSION / CAPABILITIES:
{mission}

PUBLICATIONS ({n_pubs}):
{pubs_block}

PASTED GRANTS:
{grants}
"""


def build_bootstrap_messages(
    *,
    profile_name: str,
    kind: str,
    level: str | None,
    publications: list[Publication],
    pasted_grants: str,
    mission_text: str = "",
) -> list[dict]:
    if kind == "individual":
        system = _SYSTEM_INDIVIDUAL.format(name=profile_name)
    else:
        system = _SYSTEM_UNIT.format(name=profile_name, level=level or "unit")
    user = _USER_TEMPLATE.format(
        mission=mission_text or "(none)",
        n_pubs=len(publications),
        pubs_block=_pubs_block(publications),
        grants=pasted_grants or "(none)",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
```

Update `parse_bootstrap_response` to preserve the new namespaces verbatim when present (still defaulting the existing keys):

```python
    structured = {
        "areas": _str_list(data.get("areas")),
        "budget_min": _int_or_none(data.get("budget_min")),
        "budget_max": _int_or_none(data.get("budget_max")),
        "trl_min": _int_or_none(data.get("trl_min")),
        "trl_max": _int_or_none(data.get("trl_max")),
        "must_include": _str_list(data.get("must_include")),
        "hard_exclusions": _str_list(data.get("hard_exclusions")),
    }
    # Carry through the new namespaces if the model supplied them. These are
    # SUGGESTIONS shown on the edit screen, re-validated on save via
    # profiles.fields.parse_structured — so a loose shape here is acceptable.
    for ns in ("eligibility", "org", "funding", "geo"):
        if isinstance(data.get(ns), dict):
            structured[ns] = data[ns]
```

(`Publication` is already imported in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_profiles_bootstrap_prompt.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/profiles/bootstrap_prompt.py tests/test_profiles_bootstrap_prompt.py
git commit -m "feat(profiles): kind-aware bootstrap prompt + new-namespace passthrough"
```

---

## Task 6: Wire kind/level/mission through `bootstrap_profile`

**Files:**
- Modify: `src/call_manager/profiles/bootstrap.py` (`build_bootstrap_messages` call at line 52-57)
- Test: `tests/test_profiles_bootstrap.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_profiles_bootstrap.py
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.seed import SeedInput


class _FakeOllama:
    def __init__(self, content):
        self._content = content

    def chat(self, *, messages):
        # Assert the org-unit mission text reached the prompt.
        assert any("biobank" in m["content"] for m in messages)
        return {"choices": [{"message": {"content": self._content}}]}


class _FakePubMed:
    def fetch_publications(self, members):
        return []


def test_bootstrap_org_unit_uses_mission_text():
    seed = SeedInput(
        name="Path Lab", kind="organizational_unit", level="lab",
        mission_text="We run a biobank.",
    )
    draft = bootstrap_profile(
        seed=seed, pubmed=_FakePubMed(),
        ollama=_FakeOllama('{"description":"d","org":{"capabilities":["biobank"]}}'),
    )
    assert draft.structured.get("org", {}).get("capabilities") == ["biobank"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap.py -q`
Expected: FAIL (`build_bootstrap_messages()` missing `level`/`mission_text` → TypeError)

- [ ] **Step 3: Write minimal implementation**

Update the call in `bootstrap.py` (line 52-57):

```python
    messages = build_bootstrap_messages(
        profile_name=seed.name,
        kind=seed.kind,
        level=seed.level,
        publications=publications,
        pasted_grants=seed.pasted_grants,
        mission_text=seed.mission_text,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_profiles_bootstrap.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/profiles/bootstrap.py tests/test_profiles_bootstrap.py
git commit -m "feat(profiles): thread level + mission_text through bootstrap"
```

---

## Task 7: Create route — kind-branched form + validation

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (`profile_new_submit` line 60-128; `_parse_members`; constants)
- Test: `tests/test_web_profiles.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_web_profiles.py — uses the existing app/client helpers in this file
def test_create_individual_persists_eligibility(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)  # existing helper in this test module
    _patch_bootstrap(monkeypatch)    # existing helper that fakes pubmed+ollama draft
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Dr A", "kind": "individual",
                "orcids": "0000-0001-2345-6789", "career_stage": "postdoc",
                "applicant_types": "solo_pi", "host_institution": "Karolinska",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from sqlalchemy import select
    from call_manager.db.models import Profile
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Dr A")).scalar_one()
        assert p.kind == "individual"
        assert "solo_pi" in p.structured["eligibility"]["applicant_types"]
        assert p.structured["eligibility"]["career_stage"] == "postdoc"
        assert p.structured["geo"]["host_institution"] == "Karolinska"


def test_create_rejects_unknown_career_stage(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _patch_bootstrap(monkeypatch)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={"name": "Dr B", "kind": "individual", "career_stage": "wizard"},
            follow_redirects=False,
        )
    assert r.status_code == 400
```

> If `_app` / `_patch_bootstrap` / `_logged_in` helpers don't already exist in `tests/test_web_profiles.py`, mirror the setup used by the existing create test in that file (read it first). Keep the bootstrap patched so no real PubMed/Ollama call is made.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_profiles.py -q -k "individual or career_stage"`
Expected: FAIL (route ignores new fields / accepts `wizard`)

- [ ] **Step 3: Write minimal implementation**

Extend `profile_new_submit` to accept the new form fields, validate via `parse_structured`, branch member-parsing by kind, and merge the bootstrap draft with the validated user inputs. Add imports at top:

```python
from fastapi import HTTPException
from call_manager.profiles.fields import (
    FieldValidationError,
    parse_structured,
    sanitize_structured,
)
from call_manager.db.models import ProfileKind, ProfileLevel
```

Replace the signature + body of `profile_new_submit` (line 60-128). Key additions:

```python
@router.post("/profiles/new")
def profile_new_submit(
    request: Request,
    background: BackgroundTasks,
    name: str = Form(...),
    kind: str = Form("organizational_unit"),
    level: str = Form(""),
    orcids: str = Form(""),
    names_affiliations: str = Form(""),
    pasted_grants: str = Form(""),
    mission_text: str = Form(""),
    # new structured inputs (validated via parse_structured)
    areas: str = Form(""),
    career_stage: str = Form(""),
    applicant_types: str = Form(""),
    role: str = Form(""),
    years_since_phd: str = Form(""),
    org_type: str = Form(""),
    capabilities: str = Form(""),
    patient_populations: str = Form(""),
    scale: str = Form(""),
    funding_instruments: str = Form(""),
    funder_exclusions: str = Form(""),
    country: str = Form(""),
    host_institution: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="name too long (max 200 chars)")
    if len(pasted_grants) > 100_000:
        raise HTTPException(status_code=413, detail="pasted grants too large (max 100 KB)")
    if kind not in {k.value for k in ProfileKind}:
        kind = ProfileKind.ORGANIZATIONAL_UNIT.value
    level_val: str | None = None
    if kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
        level_val = level.strip() or None
        if level_val is not None and level_val not in {x.value for x in ProfileLevel}:
            raise HTTPException(status_code=400, detail=f"unknown level: {level!r}")

    # Validate the new structured inputs up-front (before the LLM round-trip).
    try:
        user_structured = parse_structured(
            kind=kind,
            raw={
                "areas": areas, "career_stage": career_stage,
                "applicant_types": applicant_types, "role": role,
                "years_since_phd": years_since_phd, "org_type": org_type,
                "capabilities": capabilities, "patient_populations": patient_populations,
                "scale": scale, "funding_instruments": funding_instruments,
                "funder_exclusions": funder_exclusions, "country": country,
                "host_institution": host_institution,
            },
        )
    except FieldValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e

    # ... existing duplicate-name pre-check (unchanged) ...

    members = _parse_members(orcids, names_affiliations)  # individuals enter their own ORCID here
    seed = SeedInput(
        name=name, kind=kind, level=level_val, members=members,
        pasted_grants=pasted_grants, mission_text=mission_text,
    )
    # ... existing pubmed/ollama bootstrap call (unchanged) ...

    # Merge: bootstrap draft is the base, but its LLM-suggested namespaces are
    # SANITIZED before they are ever persisted (parse_structured guards user
    # input; sanitize_structured guards machine input). User-entered values then
    # win over the sanitized suggestions.
    draft_clean = sanitize_structured(kind=kind, structured=draft.structured)
    merged = dict(draft.structured)
    merged["areas"] = user_structured["areas"] or draft.structured.get("areas", [])
    for ns in ("eligibility", "org", "funding", "geo"):
        base_ns = draft_clean.get(ns, {})
        user_ns = {
            k: v for k, v in user_structured.get(ns, {}).items() if v not in (None, [], "")
        }
        if base_ns or user_ns:
            merged[ns] = {**base_ns, **user_ns}

    profile = Profile(
        name=name, kind=kind, level=level_val,
        description=draft.description, structured=merged, created_by=user.id,
    )
    # ... existing add/commit/IntegrityError + background embed (unchanged) ...
```

> Read the current `profile_new_submit` and preserve every existing line not shown here (duplicate-name pre-check, IntegrityError handling, background embed task, redirect).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_profiles.py -q`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/routes/profiles.py tests/test_web_profiles.py
git commit -m "feat(profiles): kind-branched create route + new-field validation"
```

---

## Task 8: Edit route — kind-aware fields + re-rank-on-edit test

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (`profile_edit_submit` line 169-257)
- Test: `tests/test_web_profiles.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_web_profiles.py
def test_edit_persists_new_fields_and_schedules_backfill(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    scheduled = {"backfill": 0}
    import call_manager.web.routes.profiles as profiles_mod
    monkeypatch.setattr(
        profiles_mod, "backfill_profile",
        lambda **kw: scheduled.__setitem__("backfill", scheduled["backfill"] + 1),
    )
    # create an org-unit profile directly
    from call_manager.db.models import Profile
    with pg_session_factory() as s:
        from call_manager.db.models import User
        u = User(email="o@ki.se"); s.add(u); s.flush()
        p = Profile(name="Lab", kind="organizational_unit", level="lab",
                    description="d", structured={"areas": []}, created_by=u.id)
        s.add(p); s.commit(); pid, uid = p.id, u.id
    client = _logged_in(app, pg_session_factory, user_id=uid)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={"description": "d", "level": "clinic", "org_type": "university_hospital",
                  "capabilities": "biobank\nimaging_core", "budget_currency": ""},
            follow_redirects=False,
        )
    assert r.status_code == 303
    # background task ran the re-rank even though description didn't change
    assert scheduled["backfill"] == 1
    with pg_session_factory() as s:
        got = s.get(Profile, pid)
        assert got.level == "clinic"  # editable level persisted (was NULL for migrated orgs)
        assert got.structured["org"]["org_type"] == "university_hospital"
        assert "biobank" in got.structured["org"]["capabilities"]


def test_edit_rejects_unknown_level(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    from call_manager.db.models import Profile, User
    with pg_session_factory() as s:
        u = User(email="o2@ki.se"); s.add(u); s.flush()
        p = Profile(name="Lab2", kind="organizational_unit", level=None,
                    description="d", structured={"areas": []}, created_by=u.id)
        s.add(p); s.commit(); pid, uid = p.id, u.id
    client = _logged_in(app, pg_session_factory, user_id=uid)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={"description": "d", "level": "galaxy", "budget_currency": ""},
            follow_redirects=False,
        )
    assert r.status_code == 400
```

> Adapt `_logged_in(... user_id=)` to whatever the existing helper supports; the point is to POST as the profile owner.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_profiles.py -q -k "new_fields"`
Expected: FAIL (edit route ignores org_type/capabilities)

- [ ] **Step 3: Write minimal implementation**

Extend `profile_edit_submit` to accept the new fields **including `level`**, validate via `parse_structured` (kind read from the loaded profile), assign `p.level` for organizational units, and merge into `structured` alongside the existing rubric keys. The existing background `_refresh_and_backfill_in_background` task already always runs `backfill_profile` (re-rank) and only re-embeds when `description` changed — keep that. Add the new `Form(...)` params — same names as Task 7 **plus `level: str = Form("")`** — and replace the `p.structured = {...}` assignment:

```python
    # Level is editable for organizational units (migrated org rows start NULL,
    # and the spec promises owners can fill it in here). Ignored for individuals.
    if p.kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
        lv = (level or "").strip() or None
        if lv is not None and lv not in {x.value for x in ProfileLevel}:
            raise HTTPException(status_code=400, detail=f"unknown level: {level!r}")
        p.level = lv

    # existing rubric keys (kept) ...
    base = {
        "areas": _split_lines(areas),
        "budget_min": parsed_budget_min,
        "budget_max": parsed_budget_max,
        "trl_min": parsed_trl_min,
        "trl_max": parsed_trl_max,
        "must_include": _split_lines(must_include),
        "hard_exclusions": _split_lines(hard_exclusions),
    }
    try:
        new_ns = parse_structured(
            kind=p.kind,
            raw={
                "areas": areas, "career_stage": career_stage,
                "applicant_types": applicant_types, "role": role,
                "years_since_phd": years_since_phd, "org_type": org_type,
                "capabilities": capabilities, "patient_populations": patient_populations,
                "scale": scale, "funding_instruments": funding_instruments,
                "funder_exclusions": funder_exclusions, "country": country,
                "host_institution": host_institution,
            },
        )
    except FieldValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    # base owns areas/budget/trl/must_include/hard_exclusions; new_ns owns the
    # namespaces. parse_structured also returns "areas" — base wins (it parsed
    # the same input), so drop new_ns["areas"].
    new_ns.pop("areas", None)
    p.structured = {**base, **new_ns}
```

> `p.kind` is now a plain string (e.g. `"organizational_unit"`). For an individual, `parse_structured` omits the `org` namespace automatically, so an org_type accidentally posted to an individual is simply ignored.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_profiles.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/routes/profiles.py tests/test_web_profiles.py
git commit -m "feat(profiles): kind-aware edit route persists new namespaces"
```

---

## Task 9: Templates — kind-branched new/edit/detail

**Files:**
- Modify: `src/call_manager/web/templates/profile_new.html`
- Modify: `src/call_manager/web/templates/profile_edit.html`
- Modify: `src/call_manager/web/templates/profile_detail.html`
- Test: `tests/test_web_profiles.py` (extend — render assertions)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_web_profiles.py
def test_new_form_has_kind_and_unit_and_individual_fields(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles/new")
    assert r.status_code == 200
    body = r.text
    assert 'value="organizational_unit"' in body and 'value="individual"' in body
    assert 'name="career_stage"' in body       # individual field
    assert 'name="capabilities"' in body        # unit field
    assert 'name="funding_instruments"' in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_profiles.py -q -k "new_form_has_kind"`
Expected: FAIL (old form has org/group/user + member-only fields)

- [ ] **Step 3: Write minimal implementation**

Rewrite `profile_new.html`'s form to: a `kind` select with `organizational_unit` / `individual`; a `level` select; individual-only fields (career_stage, applicant_types, host_institution, country, role, years_since_phd); unit-only fields (org_type, capabilities, patient_populations, scale, mission_text); shared fields (areas, funding_instruments, funder_exclusions); plus the existing member/grants textareas. A small inline `<script>` toggles the two field-sets on the `kind` select (progressive enhancement — both sets are submitted, the route ignores irrelevant ones per `parse_structured`).

```html
{% extends "base.html" %}
{% block title %}New profile — Call Monitor{% endblock %}
{% block content %}
<p><a href="/profiles">&larr; All profiles</a></p>
<hgroup>
  <h1>Create a profile</h1>
  <p>Pick a kind, add seed data, and the local LLM drafts a description plus
     rubric. You can edit before it goes live.</p>
</hgroup>

<form method="post" action="/profiles/new">
  <label>Profile name
    <input type="text" name="name" required maxlength="200"
           placeholder="e.g. Digital Pathology Lab">
  </label>

  <label>Kind
    <select name="kind" id="kind-select" onchange="toggleKind()">
      <option value="organizational_unit" selected>Organizational unit</option>
      <option value="individual">Individual researcher</option>
    </select>
  </label>

  <fieldset id="unit-fields">
    <label>Level
      <select name="level">
        <option value="">(unspecified)</option>
        <option value="lab">Lab</option>
        <option value="group">Research group</option>
        <option value="department">Department</option>
        <option value="clinic">Clinic</option>
        <option value="institution">Institution</option>
        <option value="other">Other</option>
      </select>
    </label>
    <label>Organization type
      <select name="org_type">
        <option value="">(unspecified)</option>
        <option value="research_organisation">Research organisation</option>
        <option value="university_hospital">University hospital</option>
        <option value="public_body">Public body</option>
        <option value="sme">SME</option>
        <option value="other">Other</option>
      </select>
    </label>
    <label>Mission / strategy / capabilities <small class="muted">(free text)</small>
      <textarea name="mission_text" rows="4"
                placeholder="What this unit does; biobanks, cohorts, core facilities..."></textarea>
    </label>
    <label>Capabilities <small class="muted">(one per line: biobank, clinical_trials_unit, imaging_core, registry, cohort...)</small>
      <textarea name="capabilities" rows="3"></textarea>
    </label>
    <label>Patient populations <small class="muted">(one per line)</small>
      <textarea name="patient_populations" rows="2"></textarea>
    </label>
    <label>Scale <small class="muted">(free text, e.g. ~400 researchers)</small>
      <input type="text" name="scale" maxlength="200">
    </label>
  </fieldset>

  <fieldset id="individual-fields" hidden>
    <label>Career stage
      <select name="career_stage">
        <option value="">(unspecified)</option>
        <option value="phd_student">PhD student</option>
        <option value="postdoc">Postdoc</option>
        <option value="junior_pi">Junior PI</option>
        <option value="senior_pi">Senior PI</option>
        <option value="group_leader">Group leader</option>
      </select>
    </label>
    <label>Role <small class="muted">(free text)</small>
      <input type="text" name="role" maxlength="200">
    </label>
    <label>Years since PhD <small class="muted">(0–80)</small>
      <input type="text" name="years_since_phd">
    </label>
    <label>Host institution <small class="muted">(free text)</small>
      <input type="text" name="host_institution" maxlength="200">
    </label>
    <label>Country <small class="muted">(e.g. SE)</small>
      <input type="text" name="country" maxlength="200">
    </label>
  </fieldset>

  <label>Applicant types <small class="muted">(one per line: solo_pi, consortium_coordinator, consortium_partner, institution)</small>
    <textarea name="applicant_types" rows="2"></textarea>
  </label>
  <label>Funding instruments <small class="muted">(one per line: project_grant, fellowship, mobility, infrastructure, programme)</small>
    <textarea name="funding_instruments" rows="2"></textarea>
  </label>
  <label>Funder exclusions <small class="muted">(one per line, e.g. defence)</small>
    <textarea name="funder_exclusions" rows="2"></textarea>
  </label>
  <label>Research areas <small class="muted">(one per line)</small>
    <textarea name="areas" rows="3"></textarea>
  </label>

  <label>Member ORCIDs <small class="muted">(one per line; for an individual, your own)</small>
    <textarea name="orcids" rows="2" placeholder="0000-0001-2345-6789"></textarea>
  </label>
  <label>Members by name <small class="muted">(one per line: <code>Last F | Affiliation</code>)</small>
    <textarea name="names_affiliations" rows="2"></textarea>
  </label>
  <label>Pasted past grants <small class="muted">(optional, titles + abstracts)</small>
    <textarea name="pasted_grants" rows="4"></textarea>
  </label>

  <button type="submit">Bootstrap profile</button>
</form>

<script>
  function toggleKind() {
    var k = document.getElementById('kind-select').value;
    document.getElementById('unit-fields').hidden = (k !== 'organizational_unit');
    document.getElementById('individual-fields').hidden = (k !== 'individual');
  }
  toggleKind();
</script>
{% endblock %}
```

For `profile_edit.html`: add the same field groups (rendered with `{% if profile.kind == 'individual' %}` / `'organizational_unit'`), pre-filled from `profile.structured.get(ns, {})`. Use Jinja `profile.structured.get('org', {}).get('capabilities', []) | join('\n')` style. **For organizational units, include a `level` `<select name="level">`** (same options as `profile_new.html`) pre-selected to `profile.level` so the owner can fill in the NULL level on migrated orgs — this is the field Task 8 persists. For `profile_detail.html`: add a read-only section listing the populated new fields (skip empties), and show `profile.level` next to the kind.

> Keep the existing budget/currency/TRL/must_include/hard_exclusions blocks in `profile_edit.html` exactly as they are; just add the new groups around them. Read the current files before editing and preserve existing structure.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_profiles.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/templates/profile_new.html src/call_manager/web/templates/profile_edit.html src/call_manager/web/templates/profile_detail.html tests/test_web_profiles.py
git commit -m "feat(profiles): kind-branched new/edit/detail templates"
```

---

## Task 10: Ranking prompt integration + version bump

**Files:**
- Modify: `src/call_manager/ranking/prompt.py` (`PROMPT_VERSION` line 10; `_SYSTEM_TEMPLATE` / `_PROFILE_TEMPLATE` profile block ~line 55-70; `build_messages` profile formatting ~line 159-165)
- Test: `tests/test_ranking_prompt.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_ranking_prompt.py
from call_manager.ranking.prompt import PROMPT_VERSION, build_messages


class _P:  # minimal profile stub matching what build_messages reads
    name = "Lab"
    kind = "organizational_unit"
    description = "d"
    budget_currency = None
    structured = {
        "areas": ["oncology"],
        "hard_exclusions": [],
        "org": {"org_type": "university_hospital", "capabilities": ["biobank"]},
        "eligibility": {"applicant_types": ["consortium_partner"]},
        "funding": {"instruments": ["infrastructure"], "funder_exclusions": ["defence"]},
        "geo": {"country": "SE"},
    }


def test_prompt_includes_eligibility_and_org_context(make_call):
    # `make_call` is the existing call fixture/factory in this test module;
    # if absent, construct the same minimal call stub the other tests use.
    msgs = build_messages(profile=_P(), call=make_call())
    blob = "\n".join(m["content"] for m in msgs)
    assert "university_hospital" in blob
    assert "consortium_partner" in blob
    assert "infrastructure" in blob
    assert "defence" in blob            # funder_exclusions surfaced
    assert "biobank" in blob


def test_prompt_version_bumped():
    assert PROMPT_VERSION == "v4.0"
```

> If `build_messages` / a call factory aren't the exact names in this test file, read `tests/test_ranking_prompt.py` and `src/call_manager/ranking/prompt.py` first and match the real signatures.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ranking_prompt.py -q -k "eligibility or version_bumped"`
Expected: FAIL (new fields absent; version "v3.0")

- [ ] **Step 3: Write minimal implementation**

Bump the version and extend the PROFILE block. In `prompt.py`:

```python
PROMPT_VERSION = "v4.0"
```

In the profile template (the block that already renders `Kind`, `Description`, `Research areas`, `Hard exclusions`), append an eligibility/org/funding/geo section, and extend the eligibility-criterion instruction. Add a helper to format the namespaces and call it in `build_messages`:

```python
def _eligibility_block(structured: dict) -> str:
    elig = structured.get("eligibility") or {}
    org = structured.get("org") or {}
    funding = structured.get("funding") or {}
    geo = structured.get("geo") or {}
    lines = []
    if elig.get("career_stage"):
        lines.append(f"  Career stage: {elig['career_stage']}")
    if elig.get("applicant_types"):
        lines.append(f"  Applicant types: {', '.join(elig['applicant_types'])}")
    if org.get("org_type"):
        lines.append(f"  Organization type: {org['org_type']}")
    if org.get("capabilities"):
        lines.append(f"  Capabilities: {', '.join(org['capabilities'])}")
    if org.get("patient_populations"):
        lines.append(f"  Patient populations: {', '.join(org['patient_populations'])}")
    if funding.get("instruments"):
        lines.append(f"  Funding instruments sought: {', '.join(funding['instruments'])}")
    if funding.get("funder_exclusions"):
        lines.append(f"  Funder/instrument exclusions: {', '.join(funding['funder_exclusions'])}")
    if geo.get("country"):
        lines.append(f"  Country: {geo['country']}")
    if geo.get("host_institution"):
        lines.append(f"  Host institution: {geo['host_institution']}")
    return "\n".join(lines) if lines else "  (none specified)"
```

Add an `Eligibility & organizational context:` line to the PROFILE block of the user template, fed by `_eligibility_block(structured)`, and extend the `eligibility` criterion instruction text in the system template to read:

```
- eligibility: would this profile's applicant type / career stage / organization
  type be eligible to apply? Consider the eligibility & organizational context.
  Treat the "Funder/instrument exclusions" as a strong negative for eligibility
  (the applicant will not pursue those funders/instruments). pass = eligible;
  fail = clearly ineligible; unclear otherwise. Do NOT treat funder exclusions
  as a TOPIC exclusion — topic exclusions are handled separately.
```

Wire it into `build_messages` where the profile block is formatted (pass `eligibility_block=_eligibility_block(structured)` into the `.format(...)` call and add `Eligibility & organizational context:\n{eligibility_block}` to the template).

> Read the exact template/format call in `prompt.py` and insert the placeholder consistently. Keep the JSON output schema and the `topic_fit`/`eligibility`/`exclusions` keys unchanged so `parse_response` and its tests still pass.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ranking_prompt.py tests/test_ranking_rank_pair.py -q`
Expected: PASS (prompt + rank_pair, which references PROMPT_VERSION)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/ranking/prompt.py tests/test_ranking_prompt.py
git commit -m "feat(ranking): profile eligibility/org context in prompt; bump PROMPT_VERSION v4.0"
```

---

## Task 11: Integration, backward-compat, full suite + ruff

**Files:**
- Test: `tests/test_profiles_redesign_e2e.py`
- Verify: whole suite + lint

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_redesign_e2e.py
from call_manager.ranking.prompt import build_messages


def test_pre_migration_profile_ranks_without_error(make_call):
    """A profile with only the OLD structured keys (no new namespaces) must
    still build a ranking prompt without KeyError."""
    class _Legacy:
        name = "Legacy Group"
        kind = "organizational_unit"
        description = "d"
        budget_currency = None
        structured = {"areas": ["x"], "hard_exclusions": [], "budget_min": None}

    msgs = build_messages(profile=_Legacy(), call=make_call())
    assert msgs and any("Legacy Group" in m["content"] for m in msgs)


def test_embedding_text_unaffected_by_new_fields():
    from call_manager.embeddings.similarity import build_profile_text

    class _P:
        description = "Hepatology unit"
        structured = {
            "areas": ["liver"],
            "org": {"capabilities": ["biobank"]},  # must NOT enter embed text
        }

    text = build_profile_text(_P())
    assert "Hepatology unit" in text and "liver" in text
    assert "biobank" not in text  # new fields are prompt-only
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_profiles_redesign_e2e.py -q`
Expected: PASS (both — they assert already-correct behavior; if `build_messages`/`make_call` names differ, fix to match Task 10). This task is the regression guard.

- [ ] **Step 3: Full suite + lint**

Run (do NOT pipe pytest through grep — that masks its exit code; the APScheduler
"Event loop is closed" teardown lines are harmless noise, not failures):
```bash
uv run pytest -q -p no:cacheprovider   # exit code is authoritative
uv run ruff format src/ tests/ alembic/
uv run ruff check --fix src/ tests/ alembic/
uv run ruff check src/ tests/ alembic/
```
Expected: pytest exits 0 (look at the `N passed` summary line, not a grep); ruff clean.
If you must filter the noise, use `set -o pipefail` first so a failing run still
fails the pipe.

- [ ] **Step 4: Fix any regressions**

Likely touch points if red: other tests that construct `Profile(kind="group"|"org"|"user")` or `ProfileKind.GROUP` — update them to `"organizational_unit"`/`"individual"` / `ProfileKind.ORGANIZATIONAL_UNIT`. Run `grep -rn "ProfileKind\.\(ORG\|GROUP\|USER\)\|kind=\"\(org\|group\|user\)\"\|'\(org\|group\|user\)'" tests/ src/` to find them.

- [ ] **Step 5: Commit**

```bash
# Explicit paths — avoid `git add -A` (untracked var/ runtime dir must stay out).
git add src/ tests/ alembic/ docs/
git commit -m "test(profiles): redesign e2e + backward-compat; suite green"
```

---

## Self-review notes (already applied)

- **Spec coverage:** kind taxonomy (T2/T3), level + parent_profile_id (T2/T3), all four field groups (T1 vocab, T7/T8 routes, T9 forms), prompt-only enrichment + funder_exclusions→eligibility (T10), embedding-unchanged (T11), lossy downgrade (T3), validation caps (T1), re-rank-on-edit (T8), backward-compat (T11). The required deploy step (`/admin/rerank`) is operational, not code — call it out in the PR description, not a task.
- **Type consistency:** `parse_structured(kind=..., raw=...)` and `FieldValidationError(.status_code)` used identically in T1/T7/T8; `ProfileKind`/`ProfileLevel` values used in T2/T3/T7/T9; `PROMPT_VERSION="v4.0"` / `BOOTSTRAP_PROMPT_VERSION="v2"` referenced consistently.
- **Out of scope (per spec):** parent inheritance logic, deterministic eligibility gate, call-side eligibility extraction, adding new fields to the embedding text.
