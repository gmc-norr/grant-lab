"""Migration 0076: profile-name uniqueness scoped to the owner."""

from __future__ import annotations

from sqlalchemy import inspect


def test_global_active_name_index_is_gone(migrated_engine):
    live = {ix["name"] for ix in inspect(migrated_engine).get_indexes("profiles")}
    assert "uq_profiles_active_name" not in live


def test_owner_scoped_active_name_index_exists(migrated_engine):
    indexes = inspect(migrated_engine).get_indexes("profiles")
    by_name = {ix["name"]: ix for ix in indexes}
    assert "uq_profiles_active_owner_name" in by_name
    ix = by_name["uq_profiles_active_owner_name"]
    assert ix["unique"] is True
    assert ix["column_names"] == ["created_by", "name"]
    # dialect_options carries the partial-index predicate on Postgres.
    # Assert the VALUE too, not just the column: `is_active in str(where)`
    # alone also passes for `is_active = false` or `NOT is_active`, and a
    # flipped predicate silently un-does the partial-index semantics while
    # leaving the index name, columns and uniqueness all correct.
    where = str(ix.get("dialect_options", {}).get("postgresql_where") or "").lower()
    assert "is_active" in where, where
    assert "true" in where, where
    assert "not" not in where, where
