"""`Base.metadata` must be topologically sortable.

`users` and `profiles` reference each other (`users.default_profile_id` ->
`profiles.id`, `profiles.created_by` -> `users.id`). Without `use_alter=True`
on one side, SQLAlchemy cannot order the two for DDL: it emits an SAWarning and
falls back to an arbitrary order.

Nothing in the tree calls `create_all`/`drop_all` today — Alembic drives the
real schema table-by-table and never needs the global sort — so the fallback
has been harmless. `tests/conftest.py`'s truncate teardown does call
`sorted_tables`, but it TRUNCATEs with CASCADE, which is order-independent.

These pin the cycle as broken so it stays broken: the moment anything does
sort the whole metadata (a from-scratch `create_all`, a `drop_all`, a
harness that rebuilds the schema in one pass), an unsortable cycle stops being
cosmetic.
"""

import warnings

from sqlalchemy import Table

from grant_lab.db.models import Base


def test_metadata_sorts_without_a_cycle_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sorted_names = [t.name for t in Base.metadata.sorted_tables]

    cycle_warnings = [str(w.message) for w in caught if "cycles" in str(w.message)]
    assert not cycle_warnings, (
        "Base.metadata has an unresolvable FK cycle; break it with use_alter=True "
        f"on the NULLABLE side of the loop. SQLAlchemy said: {cycle_warnings}"
    )
    # The fallback path returns every table too, so a count check alone would
    # pass even with the cycle present — assert the ordering instead.
    assert sorted_names.index("users") < sorted_names.index("profiles"), (
        "with the cycle broken, `profiles.created_by` (NOT NULL -> users.id) must "
        "order users first; `users.default_profile_id` is the deferred ALTER"
    )


def test_the_users_profiles_cycle_is_broken_on_the_nullable_side():
    """`use_alter` must sit on `users.default_profile_id`, not on
    `profiles.created_by`. The former is nullable, so a row can exist before the
    constraint is added; the latter is NOT NULL and cannot be deferred."""
    users: Table = Base.metadata.tables["users"]
    profiles: Table = Base.metadata.tables["profiles"]

    (deferred,) = [
        fk for fk in users.foreign_keys if fk.column.table.name == "profiles"
    ]
    assert deferred.use_alter is True
    assert deferred.parent.nullable is True, "the deferred side must be nullable"
    assert deferred.constraint.name == "fk_users_default_profile_id", (
        "use_alter needs a stable name to emit ALTER TABLE ADD/DROP CONSTRAINT; "
        "it must match alembic/versions/0046's create_foreign_key name or the "
        "model drifts from the database"
    )

    (not_deferred,) = [
        fk for fk in profiles.foreign_keys if fk.column.table.name == "users"
    ]
    assert not_deferred.use_alter is not True, (
        "profiles.created_by is NOT NULL — deferring it would let a profile row "
        "exist with an unenforced owner"
    )
