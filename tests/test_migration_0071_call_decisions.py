"""Migration 0071: `call_decisions` replaces the global per-user dismissal.

The assertion that trivially passes is "the new table has rows". A fixture with
one user and one profile satisfies every WRONG reading of the backfill query, so
this file seeds a fixture where each wrong reading produces a *different* count:

| profile | active? | subscribed by | default of | must appear? |
| ------- | ------- | ------------- | ---------- | ------------ |
| P1      | yes     | U1            | -          | **yes**      |
| P2      | **no**  | U1            | -          | no - inactive |
| P3      | yes     | -             | **U2**     | **yes**      |
| P4      | yes     | U3            | -          | no - another group's |
| P5      | yes     | **U1**        | **U1**     | **yes, once** |

**Why the default arm is split across two users.** The plan's table asks one
user to have BOTH P3 and P5 as their default. `users.default_profile_id` is a
single nullable scalar FK, so that fixture cannot exist. The discriminating
power is what matters, not the user count, so P3 (default and NOT subscribed -
the row that dies if the `OR p.id = u.default_profile_id` arm is dropped) is
given to U2, and P5 (subscribed AND default - the row that proves the `OR` in a
JOIN's ON clause does not duplicate) stays with U1.

Expected: **exactly 3 rows** - (U1,P1), (U1,P5), (U2,P3) - all `state='hidden'`,
`decided_at` equal to the source `dismissed_at`, `decided_by = user_id`.

U3 is subscribed to P4 and has NO dismissal: without them, a query that fanned
one user's hide onto every other user's profile would still pass. U4 has a
dismissal and no profiles at all: they are the orphan the migration must count
and report rather than silently discard.

**0071 drops `user_call_dismissals`, in the same commit that deletes the model
and its four importers.** A commit that dropped the table while keeping the
mapped class would error 2885 tests in teardown, because tests/conftest.py
truncates every table in `Base.metadata`. Gate 5 pins the drop; gate 7 pins the
lossy rebuild on the way back down.

`pg_session_factory` is requested for its teardown TRUNCATE, and every downgrade
is wrapped in try/finally so the shared session-scoped container is left at head
even on failure (precedent: tests/test_migration_0070_profile_visibility.py). A
test that leaves the DB below head breaks every later test on the same xdist
worker.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


# ---------------------------------------------------------------------------
# Raw-SQL seed helpers. The fixture is seeded at revision 0070, where the ORM's
# `call_decisions` mapping has no table behind it, so the models cannot be used
# on both sides of the upgrade. Everything below is therefore plain SQL.
# ---------------------------------------------------------------------------
def _mk_user(conn, email: str) -> int:
    conn.execute(text("INSERT INTO users (email, status) VALUES (:e, 'active')"), {"e": email})
    return conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar_one()


def _mk_profile(conn, name: str, owner_id: int, *, is_active: bool = True) -> int:
    conn.execute(
        text(
            "INSERT INTO profiles (name, kind, level, created_by, is_active) "
            "VALUES (:n, 'organizational_unit', 'group', :u, :a)"
        ),
        {"n": name, "u": owner_id, "a": is_active},
    )
    return conn.execute(text("SELECT id FROM profiles WHERE name = :n"), {"n": name}).scalar_one()


def _mk_call(conn, external_id: str) -> int:
    sid = conn.execute(
        text(
            "INSERT INTO sources (name, slug) VALUES (:n, :s) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"n": "Fixture Source 71", "s": "fixture_71"},
    ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO calls (source_id, external_id, url, title, keywords, raw) "
            "VALUES (:s, :x, 'http://example.test/c', 'Fixture call 71', '{}', '{}'::jsonb) "
            "RETURNING id"
        ),
        {"s": sid, "x": external_id},
    ).scalar_one()


def _mk_call_via_session(s, external_id: str, slug: str) -> int:
    """Same insert as _mk_call, through a Session rather than a Connection."""
    sid = s.scalar(
        text(
            "INSERT INTO sources (name, slug) VALUES (:n, :s) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"n": f"Fixture Source {slug}", "s": slug},
    )
    return s.scalar(
        text(
            "INSERT INTO calls (source_id, external_id, url, title, keywords, raw) "
            "VALUES (:s, :x, 'http://example.test/c', 'Fixture call 71', '{}', '{}'::jsonb) "
            "RETURNING id"
        ),
        {"s": sid, "x": external_id},
    )


def _seed(eng) -> dict[str, int]:
    """Seed the fixture table above AT REVISION 0070 and return the ids."""
    with eng.begin() as c:
        owner = _mk_user(c, "owner71@umu.se")
        p = {
            tag: _mk_profile(c, f"Lab {tag} 71", owner, is_active=(tag != "p2"))
            for tag in ("p1", "p2", "p3", "p4", "p5")
        }
        u1 = _mk_user(c, "u1-71@umu.se")
        u2 = _mk_user(c, "u2-71@umu.se")
        u3 = _mk_user(c, "u3-71@umu.se")
        u4 = _mk_user(c, "u4-71@umu.se")

        for uid, pid in ((u1, p["p1"]), (u1, p["p2"]), (u1, p["p5"]), (u3, p["p4"])):
            c.execute(
                text("INSERT INTO subscriptions (user_id, profile_id) VALUES (:u, :p)"),
                {"u": uid, "p": pid},
            )
        # U1's default is P5 (also subscribed - the double-match row).
        # U2's default is P3 and U2 subscribes to nothing - the default-only row.
        c.execute(
            text("UPDATE users SET default_profile_id = :p WHERE id = :u"),
            {"p": p["p5"], "u": u1},
        )
        c.execute(
            text("UPDATE users SET default_profile_id = :p WHERE id = :u"),
            {"p": p["p3"], "u": u2},
        )

        cid = _mk_call(c, "call-71-a")
        # Distinct timestamps so `decided_at = dismissed_at` is a real
        # assertion rather than "both happen to be now()".
        for uid, ts in (
            (u1, "2026-01-02 03:04:05+00"),
            (u2, "2026-02-03 04:05:06+00"),
            (u4, "2026-03-04 05:06:07+00"),
        ):
            c.execute(
                text(
                    "INSERT INTO user_call_dismissals (user_id, call_id, dismissed_at) "
                    "VALUES (:u, :c, :t)"
                ),
                {"u": uid, "c": cid, "t": ts},
            )

    return {
        "owner": owner,
        "u1": u1,
        "u2": u2,
        "u3": u3,
        "u4": u4,
        "call": cid,
        **p,
    }


def _seed_at_0070(pg_url, capsys) -> tuple[object, dict[str, int], str]:
    """Downgrade to 0070, seed, upgrade back to head. Returns (engine, ids, stdout).

    The stdout is captured across the FINAL upgrade only, so the orphan report
    the migration prints can be asserted.
    """
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    # The caller disposes `eng` in ITS finally - but only if it ever RECEIVES it.
    # Anything that raises in here (a failed downgrade, a seed that trips a
    # constraint) returns nothing, so the engine and its pooled connections leak
    # for the rest of the worker. Dispose on the way out of every raising path.
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0070")
        try:
            ids = _seed(eng)
            capsys.readouterr()  # discard everything the downgrade/seed emitted
        finally:
            command.upgrade(cfg, "head")
    except BaseException:
        eng.dispose()
        raise
    return eng, ids, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Gate 1 - the backfill set is exactly subscriptions ∪ default, active only.
# ---------------------------------------------------------------------------
def test_backfill_writes_exactly_the_subscribed_and_default_active_profiles(
    pg_url, pg_session_factory, capsys
):
    eng, ids, _out = _seed_at_0070(pg_url, capsys)
    try:
        with eng.begin() as c:
            rows = {
                tuple(r)
                for r in c.execute(
                    text("SELECT user_id, profile_id, call_id FROM call_decisions")
                ).all()
            }
        expected = {
            (ids["u1"], ids["p1"], ids["call"]),
            (ids["u1"], ids["p5"], ids["call"]),
            (ids["u2"], ids["p3"], ids["call"]),
        }
        assert rows == expected, (
            "the backfill set is every ACTIVE profile the user is subscribed to plus "
            "their default: P1 and P5 for U1, P3 for U2. P2 is inactive, P4 belongs to "
            f"another group, U3 never dismissed anything, U4 has no profiles. Got "
            f"{sorted(rows)}, expected {sorted(expected)}"
        )
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Gate 2 - P5 is subscribed AND default, and appears exactly ONCE. This is what
# proves the `OR` inside the JOIN's ON clause does not duplicate, rather than
# proving `ON CONFLICT DO NOTHING` cleaned up after it.
# ---------------------------------------------------------------------------
def test_a_profile_that_is_both_subscribed_and_default_appears_once(
    pg_url, pg_session_factory, capsys
):
    eng, ids, _out = _seed_at_0070(pg_url, capsys)
    try:
        with eng.begin() as c:
            n = c.scalar(
                text("SELECT count(*) FROM call_decisions WHERE profile_id = :p"),
                {"p": ids["p5"]},
            )
        assert n == 1, (
            f"P5 matches both arms of the OR, and the OR sits in a JOIN's ON clause "
            f"where it is a boolean test evaluated once per candidate profiles row - "
            f"so it must contribute exactly one row, not two; got {n}"
        )
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Gate 3 - the carried-over columns. state, decided_at and decided_by are what
# make a migrated row indistinguishable from one the new UI would write.
# ---------------------------------------------------------------------------
def test_backfilled_rows_are_hidden_and_keep_their_timestamp_and_author(
    pg_url, pg_session_factory, capsys
):
    eng, ids, _out = _seed_at_0070(pg_url, capsys)
    try:
        with eng.begin() as c:
            rows = c.execute(
                text(
                    "SELECT c.user_id, c.state, c.decided_by, c.note, "
                    "       c.decided_at = d.dismissed_at AS ts_kept "
                    "  FROM call_decisions c "
                    "  JOIN (SELECT :u1 AS user_id, "
                    "               TIMESTAMPTZ '2026-01-02 03:04:05+00' AS dismissed_at "
                    "        UNION ALL "
                    "        SELECT :u2, TIMESTAMPTZ '2026-02-03 04:05:06+00') d "
                    "    ON d.user_id = c.user_id"
                ),
                {"u1": ids["u1"], "u2": ids["u2"]},
            ).all()
        assert len(rows) == 3, f"expected the 3 backfilled rows, got {len(rows)}"
        assert {r.state for r in rows} == {"hidden"}, (
            f"a dismissal is a HIDE; migrating it as anything else silently un-hides or "
            f"mis-labels every call the user already dealt with. Got "
            f"{sorted({r.state for r in rows})}"
        )
        assert all(r.ts_kept for r in rows), (
            "decided_at must be the original dismissed_at, not now() - otherwise every "
            "migrated decision claims to have been made on upgrade day"
        )
        assert {(r.user_id, r.decided_by) for r in rows} == {
            (ids["u1"], ids["u1"]),
            (ids["u2"], ids["u2"]),
        }, "decided_by must be the dismissing user themselves"
        assert {r.note for r in rows} == {None}, "the backfill invents no note text"
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Gate 4 - nobody else's profile is touched, and the orphan is REPORTED.
# ---------------------------------------------------------------------------
def test_no_fanout_onto_another_users_profile_and_orphans_are_reported(
    pg_url, pg_session_factory, capsys
):
    eng, ids, out = _seed_at_0070(pg_url, capsys)
    try:
        with eng.begin() as c:
            on_p4 = c.scalar(
                text("SELECT count(*) FROM call_decisions WHERE profile_id = :p"),
                {"p": ids["p4"]},
            )
            for tag in ("u3", "u4"):
                n = c.scalar(
                    text("SELECT count(*) FROM call_decisions WHERE user_id = :u"),
                    {"u": ids[tag]},
                )
                assert n == 0, f"{tag} must have no decision row; got {n}"
        assert on_p4 == 0, (
            f"P4 is another group's profile - U1 and U2 are neither subscribed to it nor "
            f"defaulted to it, so hiding a call must not write a row there; got {on_p4}"
        )
        assert "1 dismissal(s)" in out, (
            f"U4 dismissed a call and has no active subscribed or default profile, so "
            f"exactly one dismissal fans out to nothing. The migration must SAY so on "
            f"stdout rather than dropping it silently. Captured stdout was:\n{out}"
        )
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Gate 5 - the OLD table is gone at head. Two sources of truth for "does this
# user want to see this call" is the failure mode this migration exists to
# prevent, and a create-and-backfill that leaves the original in place is
# indistinguishable from one that never ran.
# ---------------------------------------------------------------------------
def test_the_old_dismissals_table_is_gone_at_head(pg_url, pg_session_factory):
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        insp = inspect(eng)
        assert insp.has_table("call_decisions"), "0071 must create call_decisions"
        assert not insp.has_table("user_call_dismissals"), (
            "0071 must DROP user_call_dismissals in the same migration that backfills "
            "call_decisions. Leaving it behind is how two sources of truth start."
        )
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Gate 6 - the CHECK constraint. compare_metadata does not compare CHECK
# bodies, so `test_schema_drift` would stay green on a constraint narrowed to
# ('hidden') alone. Both halves are asserted.
# ---------------------------------------------------------------------------
def test_state_check_admits_saved_and_hidden_and_rejects_anything_else(
    pg_session_factory, profile_factory
):
    pid, uid = profile_factory(name="Check Lab 71")
    with pg_session_factory() as s:
        cid = _mk_call_via_session(s, "call-71-b", "fixture_71b")
        for state in ("hidden", "saved"):
            s.execute(
                text(
                    "INSERT INTO call_decisions (user_id, profile_id, call_id, state) "
                    "VALUES (:u, :p, :c, :st)"
                ),
                {"u": uid, "p": pid, "c": cid, "st": state},
            )
            s.execute(text("DELETE FROM call_decisions"))
        s.commit()

    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO call_decisions (user_id, profile_id, call_id, state) "
                "VALUES (:u, :p, :c, 'undecided')"
            ),
            {"u": uid, "p": pid, "c": cid},
        )
        s.commit()


# ---------------------------------------------------------------------------
# Gate 6b - the unique constraint. 0071's `ON CONFLICT (user_id, profile_id,
# call_id)` names this index, and `hide_call` upserts against it, so "the
# constraint exists" is load-bearing rather than decorative. Schema drift
# would notice a MISSING constraint, but not one narrowed to (user_id,
# call_id), which would silently make a decision global again - the exact bug
# this table exists to fix.
# ---------------------------------------------------------------------------
def test_one_decision_per_user_profile_call_but_the_same_call_on_two_profiles_is_fine(
    pg_session_factory, profile_factory
):
    pid_a, uid = profile_factory(name="Uniq Lab A 71")
    pid_b, _other = profile_factory(name="Uniq Lab B 71")
    with pg_session_factory() as s:
        cid = _mk_call_via_session(s, "call-71-c", "fixture_71c")
        for pid in (pid_a, pid_b):
            s.execute(
                text(
                    "INSERT INTO call_decisions (user_id, profile_id, call_id, state) "
                    "VALUES (:u, :p, :c, 'hidden')"
                ),
                {"u": uid, "p": pid, "c": cid},
            )
        s.commit()
        assert s.scalar(text("SELECT count(*) FROM call_decisions")) == 2, (
            "the SAME user hiding the SAME call on TWO profiles is the whole point of "
            "the table - a constraint keyed (user_id, call_id) would reject the second"
        )
        with pytest.raises(IntegrityError):
            s.execute(
                text(
                    "INSERT INTO call_decisions (user_id, profile_id, call_id, state) "
                    "VALUES (:u, :p, :c, 'saved')"
                ),
                {"u": uid, "p": pid_a, "c": cid},
            )
            s.commit()


# ---------------------------------------------------------------------------
# Gate 6c - the two FKs to users.id behave DIFFERENTLY, and compare_metadata
# does not compare `ondelete` - so observe it rather than trust it (precedent:
# tests/test_migration_0070_profile_visibility.py). RESTRICT on decided_by
# would make DELETE FROM users raise instead of either of these outcomes.
# ---------------------------------------------------------------------------
def test_deleting_the_decider_nulls_decided_by_but_deleting_the_owner_cascades(
    pg_session_factory, profile_factory
):
    # NEITHER user may be the profile's creator: profiles.created_by is ON
    # DELETE RESTRICT, so deleting a creator raises a ForeignKeyViolation on
    # fk_profiles_created_by and the CASCADE half of this gate would never be
    # reached.
    pid, _creator = profile_factory(name="Cascade Lab 71")
    with pg_session_factory() as s:
        owner = s.scalar(
            text(
                "INSERT INTO users (email, status) VALUES ('decider71@umu.se','active') "
                "RETURNING id"
            )
        )
        colleague = s.scalar(
            text(
                "INSERT INTO users (email, status) VALUES ('colleague71@umu.se','active') "
                "RETURNING id"
            )
        )
        cid = _mk_call_via_session(s, "call-71-d", "fixture_71d")
        s.execute(
            text(
                "INSERT INTO call_decisions (user_id, profile_id, call_id, state, decided_by) "
                "VALUES (:u, :p, :c, 'hidden', :b)"
            ),
            {"u": owner, "p": pid, "c": cid, "b": colleague},
        )
        s.commit()

        # The colleague who pressed the key leaves the organisation.
        s.execute(text("DELETE FROM users WHERE id = :u"), {"u": colleague})
        s.commit()
        row = s.execute(text("SELECT user_id, decided_by FROM call_decisions")).all()
        assert row == [(owner, None)], (
            "deleting the DECIDER must leave the decision standing with decided_by NULL "
            f"- RESTRICT would have refused the DELETE outright; got {row}"
        )

        # The owner leaves. Their decision is theirs, and goes with them.
        s.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})
        s.commit()
        assert s.scalar(text("SELECT count(*) FROM call_decisions")) == 0, (
            "user_id is ON DELETE CASCADE: a decision dies with its OWNER"
        )


# ---------------------------------------------------------------------------
# Gate 6d (D20) - the two FK indexes. Both call_id and profile_id CASCADE, and
# without an index Postgres sequentially scans the largest per-user table for
# every Call or Profile deletion. `compare_metadata` DOES see a missing index,
# but only because the model declares it; this asserts it in the DATABASE, on
# the exact column lists, so a model/migration pair that both dropped it is
# still caught.
# ---------------------------------------------------------------------------
def test_the_two_fk_indexes_exist_on_call_id_and_profile_id(pg_session_factory):
    with pg_session_factory() as s:
        cols = {
            name: tuple(sorted(c.split(",")))
            for name, c in s.execute(
                text(
                    "SELECT i.relname, "
                    "       string_agg(a.attname, ',' ORDER BY a.attname) "
                    "  FROM pg_index x "
                    "  JOIN pg_class i ON i.oid = x.indexrelid "
                    "  JOIN pg_class t ON t.oid = x.indrelid "
                    "  JOIN pg_attribute a "
                    "    ON a.attrelid = t.oid AND a.attnum = ANY(x.indkey) "
                    " WHERE t.relname = 'call_decisions' "
                    " GROUP BY i.relname"
                )
            ).all()
        }
    assert cols.get("ix_call_decisions_call_id") == ("call_id",), (
        f"calls.id CASCADEs into call_decisions; without an index on call_id every "
        f"call deletion sequentially scans this table (D20). Indexes present: {cols}"
    )
    assert cols.get("ix_call_decisions_profile_id") == ("profile_id",), (
        f"profiles.id CASCADEs into call_decisions; without an index on profile_id "
        f"every profile deletion sequentially scans this table (D20). Indexes "
        f"present: {cols}"
    )
    assert cols.get("ix_call_decisions_user_profile_state") == (
        "profile_id",
        "state",
        "user_id",
    ), (
        f"the Saved and Hidden lists filter (user_id, profile_id, state); this is the "
        f"index they were sized for. Indexes present: {cols}"
    )


# ---------------------------------------------------------------------------
# Gate 7 - the downgrade rebuilds the old table from `hidden` rows ONLY, and
# collapses the profile dimension it cannot represent. Both halves are asserted
# on data that DISTINGUISHES them: one call saved (must not come back as a
# dismissal) and one call hidden on TWO profiles (must come back exactly once).
# ---------------------------------------------------------------------------
def test_downgrade_rebuilds_distinct_pairs_from_hidden_rows_only(
    pg_url, pg_session_factory, capsys
):
    cfg = _cfg(pg_url)
    eng, ids, _out = _seed_at_0070(pg_url, capsys)
    try:
        # At head now. Add the two discriminating rows the backfill cannot make:
        # a SAVED decision, and a second profile's hide of a DIFFERENT call.
        with eng.begin() as c:
            saved_call = _mk_call(c, "call-71-saved")
            double_call = _mk_call(c, "call-71-double")
            c.execute(
                text(
                    "INSERT INTO call_decisions "
                    "  (user_id, profile_id, call_id, state, decided_at, decided_by) "
                    "VALUES (:u, :p, :c, 'saved', '2026-05-05 00:00:00+00', :u)"
                ),
                {"u": ids["u1"], "p": ids["p1"], "c": saved_call},
            )
            for pid, ts in (
                (ids["p1"], "2026-06-01 00:00:00+00"),
                (ids["p5"], "2026-07-01 00:00:00+00"),
            ):
                c.execute(
                    text(
                        "INSERT INTO call_decisions "
                        "  (user_id, profile_id, call_id, state, decided_at, decided_by) "
                        "VALUES (:u, :p, :c, 'hidden', :t, :u)"
                    ),
                    {"u": ids["u1"], "p": pid, "c": double_call, "t": ts},
                )

        command.downgrade(cfg, "0070")
        try:
            insp = inspect(eng)
            assert not insp.has_table("call_decisions"), (
                "downgrade to 0070 left call_decisions in place"
            )
            with eng.begin() as c:
                rows = {
                    (r[0], r[1]): r[2]
                    for r in c.execute(
                        text("SELECT user_id, call_id, dismissed_at FROM user_call_dismissals")
                    ).all()
                }
        finally:
            command.upgrade(cfg, "head")

        assert (ids["u1"], saved_call) not in rows, (
            "a SAVED decision must NOT be rebuilt as a dismissal - that would hide the "
            "calls the user explicitly kept"
        )
        assert (ids["u1"], double_call) in rows, "a hidden call must come back"
        assert len([k for k in rows if k[1] == double_call]) == 1, (
            "hidden on TWO profiles collapses to ONE (user_id, call_id) row - the old "
            f"table has no profile column. Got {sorted(k for k in rows if k[1] == double_call)}"
        )
        assert rows[(ids["u1"], double_call)].isoformat().startswith("2026-06-01"), (
            "the collapsed row keeps min(decided_at), the OLDEST decision, not the newest"
        )
        # The three backfilled hides of the ORIGINAL call also survive, deduped
        # per user: U1 was hidden on P1 and P5, so U1 appears once.
        assert {k for k in rows if k[1] == ids["call"]} == {
            (ids["u1"], ids["call"]),
            (ids["u2"], ids["call"]),
        }, (
            "U4's dismissal was never migrated (no active profile), so it cannot come "
            f"back. Got {sorted(k for k in rows if k[1] == ids['call'])}"
        )
    finally:
        eng.dispose()
