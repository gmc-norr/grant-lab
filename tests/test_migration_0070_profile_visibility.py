"""Migration 0070: profile_viewers + profiles.visibility, and a day-one no-op.

The point of this file is not that the column exists — that is the assertion a
reviewer's mutation trivially satisfies. The point is that **Monday does not
change**: a profile that existed before the upgrade is still `'org'` and still
readable by an unrelated colleague afterwards, and every CURRENT subscriber of
every profile is carried forward as an explicit viewer so that the first owner
who flips a profile to `'private'` does not silently cancel a digest.

The owner is deliberately NOT backfilled: `created_by` already grants them
access, and a row would make "remove this viewer" able to lock an owner out of
their own profile. The fixture therefore subscribes the owner to their own
profile — without that, dropping the owner-exclusion clause from the backfill
would change nothing observable and the gate would be vacuous.

`pg_session_factory` is requested in the migration tests solely for its teardown
TRUNCATE, and every downgrade is wrapped in try/finally so the shared
session-scoped container is left at head even on failure (precedent:
tests/test_migration_0065_artifact_versions_unique_sha.py). A test that leaves
the DB below head breaks every later test on the same xdist worker.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, User
from grant_lab.main import create_app


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid: int) -> TestClient:
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _mk_user(conn, email: str) -> int:
    conn.execute(text("INSERT INTO users (email, status) VALUES (:e, 'active')"), {"e": email})
    return conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar_one()


def _mk_profile(conn, name: str, owner_id: int) -> int:
    """Insert a profile with raw SQL — at revision 0069 the ORM's `visibility`
    column does not exist in the database yet, so the model cannot be used."""
    conn.execute(
        text(
            "INSERT INTO profiles (name, kind, level, created_by) "
            "VALUES (:n, 'organizational_unit', 'group', :u)"
        ),
        {"n": name, "u": owner_id},
    )
    return conn.execute(text("SELECT id FROM profiles WHERE name = :n"), {"n": name}).scalar_one()


# Same shape as tests/test_api_profiles_create.py's _BODY — the real endpoint's
# contract, so gate 1a exercises the route rather than a hand-built row.
_CREATE_BODY = {
    "name": "Visibility Default Lab",
    "kind": "organizational_unit",
    "level": "group",
    "orcids": "",
    "names_affiliations": "",
    "pasted_grants": "",
    "mission_text": "imaging",
    "areas": "oncology",
    "career_stage": "",
    "applicant_types": "",
    "role": "",
    "years_since_phd": "",
    "org_type": "university_hospital",
    "capabilities": "biobank",
    "patient_populations": "",
    "scale": "",
    "funding_instruments": "",
    "funder_exclusions": "",
    "country": "SE",
    "host_institution": "UmU",
}


# --------------------------------------------------------------------------
# Gate 1a — a profile created through the REAL endpoint is 'private'.
#
# CHANGED 2026-10-28 (review 10-06 M11, still open on 10-26). This gate used to
# assert 'org' and was right to, in 2026-09: the whole point of 0070 was that
# day one changed nothing. But the 2026-09-04 decision "profiles are personal"
# made private the intended default for a NEW profile, and the application
# never implemented it — `create_profile_with_bootstrap` set no visibility, so
# the COLUMN default governed new rows too and every new profile was
# org-readable. The application now says `visibility="private"` explicitly.
#
# What this gate still protects is the thing it was built for, and that has NOT
# moved: the column's server default is still 'org' and the migration still
# leaves pre-existing rows readable — gate 1b below, and the DDL gates further
# down, assert exactly that, and they are untouched. The mutation 1b was written
# to catch ("set existing rows to private, default the column to org") is
# therefore still caught.
# --------------------------------------------------------------------------
def test_profile_created_through_the_api_is_private(monkeypatch, pg_url, pg_session_factory):
    """POST /api/profiles produces visibility='private'.

    Read back through the ORM so this covers both halves: the mapped attribute
    and the column the migration actually created.
    """
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="creator70a@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    client = _client_for(app, uid)
    with client:
        r = client.post("/api/profiles", json=_CREATE_BODY)
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    with pg_session_factory() as s:
        created = s.get(Profile, new_id)
        assert created is not None
        assert created.visibility == "private", (
            f"a profile created through POST /api/profiles must be 'private' "
            f"(decision 2026-09-04 'profiles are personal'), got {created.visibility!r}"
        )


# --------------------------------------------------------------------------
# Gate 1b — a PRE-EXISTING profile is 'org' after the upgrade, and an
# unrelated active user can still read it.
# --------------------------------------------------------------------------
def test_preexisting_profile_is_org_and_still_readable(monkeypatch, pg_url, pg_session_factory):
    """The gate that catches "set existing rows to private, default the column
    to org" — a mutation that passes gate 1a and every backfill gate while
    making every profile in the live database unreadable.

    "Readable" at this task means `GET /api/profiles/{id}` returns 200. There is
    no visibility filter yet, so today an unrelated user gets 200 — asserting
    exactly that is the day-one contract the Task 3 filter must preserve.
    """
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0069")
        try:
            insp = inspect(eng)
            cols = {c["name"] for c in insp.get_columns("profiles")}
            assert "visibility" not in cols, "downgrade to 0069 left profiles.visibility in place"
            assert "profile_viewers" not in insp.get_table_names(), (
                "downgrade to 0069 left the profile_viewers table in place"
            )
            with eng.begin() as c:
                owner = _mk_user(c, "owner70b@umu.se")
                stranger = _mk_user(c, "stranger70b@umu.se")
                pid = _mk_profile(c, "Pre-existing Lab 70b", owner)
        finally:
            command.upgrade(cfg, "head")

        with eng.begin() as c:
            vis = c.execute(
                text("SELECT visibility FROM profiles WHERE id = :p"), {"p": pid}
            ).scalar_one()
        assert vis == "org", (
            f"a profile that existed BEFORE the upgrade must be 'org' afterwards — "
            f"anything else narrows access on day one; got {vis!r}"
        )

        app = _app(monkeypatch, pg_url)
        client = _client_for(app, stranger)
        with client:
            r = client.get(f"/api/profiles/{pid}")
        assert r.status_code == 200, (
            f"an unrelated active user could read this profile before the upgrade "
            f"and must still read it after; got {r.status_code}: {r.text}"
        )
    finally:
        eng.dispose()


def _seed_backfill_fixture(eng) -> tuple[int, dict[str, int]]:
    """Profile P owned by A; subscriptions for A, B and C; user D unrelated.

    A's subscription to their OWN profile is load-bearing: owners routinely
    subscribe to their own digest, and without that row the owner-exclusion
    clause in the backfill could be deleted without any gate noticing.
    """
    with eng.begin() as c:
        ids = {tag: _mk_user(c, f"{tag}70c@umu.se") for tag in ("a", "b", "c", "d")}
        pid = _mk_profile(c, "Backfill Lab 70c", ids["a"])
        for tag in ("a", "b", "c"):
            c.execute(
                text("INSERT INTO subscriptions (user_id, profile_id) VALUES (:u, :p)"),
                {"u": ids[tag], "p": pid},
            )
    return pid, ids


# --------------------------------------------------------------------------
# Gate 2 — the backfill preserves access: exactly the non-owner subscribers.
# --------------------------------------------------------------------------
def test_backfill_carries_subscribers_forward_but_not_the_owner(pg_url, pg_session_factory):
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0069")
        try:
            pid, ids = _seed_backfill_fixture(eng)
        finally:
            command.upgrade(cfg, "head")

        with eng.begin() as c:
            rows = {
                tuple(r)
                for r in c.execute(text("SELECT profile_id, user_id FROM profile_viewers")).all()
            }
        assert rows == {(pid, ids["b"]), (pid, ids["c"])}, (
            f"the backfill must make exactly the NON-OWNER current subscribers "
            f"explicit viewers: B and C, not the owner A ({ids['a']}) and not the "
            f"unrelated D ({ids['d']}); got {sorted(rows)}"
        )
        # ON DELETE CASCADE is declared by the migration and compare_metadata
        # does not check ondelete — so observe it rather than trust it.
        with eng.begin() as c:
            c.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
            left = c.scalar(
                text("SELECT count(*) FROM profile_viewers WHERE profile_id = :p"),
                {"p": pid},
            )
        assert left == 0, "profile_viewers rows must CASCADE away with their profile"
    finally:
        eng.dispose()


# --------------------------------------------------------------------------
# Gate 3 — the owner has no row and still reads. This is the assumption the
# exclusion in gate 2 rests on, so it is asserted rather than assumed.
# --------------------------------------------------------------------------
def test_owner_has_no_viewer_row_and_still_reads(monkeypatch, pg_url, pg_session_factory):
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0069")
        try:
            pid, ids = _seed_backfill_fixture(eng)
        finally:
            command.upgrade(cfg, "head")

        with eng.begin() as c:
            owner_rows = c.execute(
                text("SELECT 1 FROM profile_viewers WHERE profile_id = :p AND user_id = :u"),
                {"p": pid, "u": ids["a"]},
            ).all()
        assert owner_rows == [], (
            "the owner must NOT get a profile_viewers row — created_by already "
            "grants access, and a row would let 'remove this viewer' lock an "
            "owner out of their own profile"
        )

        app = _app(monkeypatch, pg_url)
        client = _client_for(app, ids["a"])
        with client:
            r = client.get(f"/api/profiles/{pid}")
        assert r.status_code == 200, (
            f"the owner reads their own profile with no viewer row; got {r.status_code}: {r.text}"
        )
    finally:
        eng.dispose()


# --------------------------------------------------------------------------
# Gate 4 — the CHECK constraint is real.
# --------------------------------------------------------------------------
def test_visibility_check_rejects_an_unknown_value(pg_session_factory, profile_factory):
    """ck_profiles_visibility admits only 'private' and 'org'."""
    pid, _owner = profile_factory(name="Check Lab 70d")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text("UPDATE profiles SET visibility = 'maybe' WHERE id = :p"),
            {"p": pid},
        )
        s.commit()
    # The other half: the CHECK must ADMIT 'private'. Nothing else in this task
    # writes 'private', and compare_metadata does not compare CHECK bodies, so a
    # constraint narrowed to ('org') alone would otherwise ship green.
    with pg_session_factory() as s:
        s.execute(
            text("UPDATE profiles SET visibility = 'private' WHERE id = :p"),
            {"p": pid},
        )
        s.commit()
        assert (
            s.scalar(text("SELECT visibility FROM profiles WHERE id = :p"), {"p": pid}) == "private"
        )
