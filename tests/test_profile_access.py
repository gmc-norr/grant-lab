"""The seven-surface access test. Task 3 wires the first three surfaces.

ONE fixture, one world, seven surfaces. Tasks 4 and 5 EXTEND this file (dashboard,
call detail, settings, digest) with the same `seed_world()` — they will add
CallRanking rows and subscriptions to it rather than building a second world, so
that every surface is asserted against the same four users and the same three
profiles. A per-surface fixture is how "ten of eleven surfaces get it right" ships.

The world:

  A  owner of all three profiles
  B  colleague — an explicit profile_viewers row on P_priv, and nothing else
  C  stranger — no rows anywhere
  D  admin

  P_org     visibility 'org'      — the DEFAULT, so this is every profile today
  P_priv    visibility 'private'  — B is a viewer
  P_secret  visibility 'private'  — no viewers at all

What trivially passes: asserting only that C cannot see P_secret. That is true of
an endpoint that returns nothing at all, or 404s for everyone. Gates 2-5 are what
prove the filter is a FILTER and not a WALL, and gate 6 is what proves reading
stayed wider than editing instead of the two collapsing into one rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import grant_lab.profiles.post_edit as post_edit
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileViewer,
    Source,
    User,
)
from grant_lab.main import create_app

# An id no profile can have. Used for the "does a profile exist here?" oracle
# probes, which must be indistinguishable from an unreadable real profile.
NO_SUCH_PROFILE_ID = 987_654_321


def setup_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    # PUT /api/profiles/{id} queues a re-embed background task that would hit
    # Ollama. Every PUT in this file is expected to be REFUSED, but a mutation
    # that lets one through must fail on the assertion, not on a network call.
    monkeypatch.setattr(post_edit, "refresh_and_backfill_in_background", lambda **kw: None)
    return create_app()


def client_for(app, uid: int) -> TestClient:
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


@dataclass(frozen=True)
class World:
    a: int  # owner of all three profiles
    b: int  # colleague: viewer on p_priv only
    c: int  # stranger
    d: int  # admin
    p_org: int
    p_priv: int
    p_secret: int


def seed_world(pg_session_factory) -> World:
    """A, B, C, D and the three profiles. Read back from the DB, not asserted
    from the objects we just built: `visibility` has a SERVER default and no
    Python-side one, so an unrefreshed instance carries None and a fixture that
    trusted its own attributes would be describing a world that does not exist.
    """
    with pg_session_factory() as s:
        a = User(email="a-owner@umu.se", name="Anna Owner", status="active")
        b = User(email="b-colleague@umu.se", name="Bo Colleague", status="active")
        c = User(email="c-stranger@umu.se", name="Cai Stranger", status="active")
        d = User(email="d-admin@umu.se", name="Dag Admin", status="active", is_admin=True)
        s.add_all([a, b, c, d])
        s.flush()

        def _profile(name: str, visibility: str | None) -> Profile:
            p = Profile(
                name=name,
                kind="organizational_unit",
                level="group",
                description=f"{name} description.",
                structured={"areas": ["oncology"]},
                created_by=a.id,
            )
            if visibility is not None:
                p.visibility = visibility
            return p

        p_org = _profile("P Org Wide", None)  # relies on the server default
        p_priv = _profile("P Private Shared", "private")
        p_secret = _profile("P Private Secret", "private")
        s.add_all([p_org, p_priv, p_secret])
        s.flush()
        s.add(ProfileViewer(profile_id=p_priv.id, user_id=b.id))
        s.commit()
        world = World(
            a=a.id,
            b=b.id,
            c=c.id,
            d=d.id,
            p_org=p_org.id,
            p_priv=p_priv.id,
            p_secret=p_secret.id,
        )

    # The world is only worth asserting against if it is the world we meant.
    with pg_session_factory() as s:
        got = dict(
            s.execute(
                text("SELECT id, visibility FROM profiles WHERE id = ANY(:ids)"),
                {"ids": [world.p_org, world.p_priv, world.p_secret]},
            ).all()
        )
        assert got == {
            world.p_org: "org",
            world.p_priv: "private",
            world.p_secret: "private",
        }, got
        viewer_rows = {
            tuple(r)
            for r in s.execute(text("SELECT profile_id, user_id FROM profile_viewers")).all()
        }
        assert viewer_rows == {(world.p_priv, world.b)}, viewer_rows
    return world


@pytest.fixture
def world(pg_session_factory) -> World:
    return seed_world(pg_session_factory)


_VALID_EDIT = {
    "description": "Edited by someone.",
    "areas": "oncology",
    "budget_min": "100",
    "budget_max": "200",
    "budget_currency": "SEK",
    "trl_min": "3",
    "trl_max": "7",
    "must_include": "",
    "hard_exclusions": "",
    "level": "group",
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


def _list_ids(client) -> list[int]:
    r = client.get("/api/profiles")
    assert r.status_code == 200, r.text
    return [p["id"] for p in r.json()["profiles"]]


# ==========================================================================
# Gate 1 — the stranger. Absence from the list AND a 404 on the detail.
# ==========================================================================
def test_gate1_stranger_cannot_see_or_fetch_a_private_profile(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as client:
        ids = _list_ids(client)
        assert world.p_secret not in ids, (
            f"GET /api/profiles leaked P_secret ({world.p_secret}) to a stranger: {ids}"
        )
        assert world.p_priv not in ids, (
            f"GET /api/profiles leaked P_priv ({world.p_priv}) to a non-viewer: {ids}"
        )
        r = client.get(f"/api/profiles/{world.p_secret}")
    assert r.status_code == 404, (
        f"a stranger must get 404 — a 403 confirms the profile EXISTS and lets "
        f"someone walk /api/profiles/1..N to learn who is working on what; "
        f"got {r.status_code}: {r.text}"
    )
    assert r.json()["error"]["code"] == "not_found"


def test_gate1_stranger_404_is_identical_to_a_nonexistent_profile(
    monkeypatch, pg_url, pg_session_factory, world
):
    """The two answers must be indistinguishable, body included."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as client:
        unreadable = client.get(f"/api/profiles/{world.p_secret}")
        missing = client.get(f"/api/profiles/{NO_SUCH_PROFILE_ID}")
    assert unreadable.status_code == missing.status_code == 404
    assert unreadable.json() == missing.json(), (
        f"the 'unreadable' and 'does not exist' answers differ, which is the "
        f"oracle itself: {unreadable.json()} vs {missing.json()}"
    )


# ==========================================================================
# Gate 2 — the colleague. A viewer row is a real grant, on list AND detail.
# ==========================================================================
def test_gate2_viewer_reads_the_shared_private_profile(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as client:
        ids = _list_ids(client)
        assert world.p_priv in ids, (
            f"B has a profile_viewers row on P_priv ({world.p_priv}) and must see "
            f"it in the list; got {ids}"
        )
        assert world.p_secret not in ids, (
            "B's viewer row is for P_priv ONLY — it must not grant P_secret"
        )
        r = client.get(f"/api/profiles/{world.p_priv}")
    assert r.status_code == 200, r.text
    body = r.json()
    # "Fully readable", not merely 200: the payload is the real profile.
    assert body["id"] == world.p_priv
    assert body["name"] == "P Private Shared"
    assert body["areas"] == ["oncology"]
    assert body["can_edit"] is False  # reading, not editing


# ==========================================================================
# Gate 3 — the owner, who has NO viewer row. created_by is its own arm.
# ==========================================================================
def test_gate3_owner_reads_own_private_profile_without_a_viewer_row(
    monkeypatch, pg_url, pg_session_factory, world
):
    with pg_session_factory() as s:
        rows = s.execute(
            text("SELECT 1 FROM profile_viewers WHERE profile_id = :p AND user_id = :u"),
            {"p": world.p_secret, "u": world.a},
        ).all()
    assert rows == [], "the owner must have no viewer row — that is what makes this a gate"

    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        ids = _list_ids(client)
        r = client.get(f"/api/profiles/{world.p_secret}")
    assert world.p_secret in ids, f"the owner's own private profile vanished from the list: {ids}"
    assert r.status_code == 200, f"the owner cannot read their own profile: {r.text}"
    assert r.json()["can_edit"] is True


# ==========================================================================
# Gate 4 — the admin, who has neither a viewer row nor ownership.
# ==========================================================================
def test_gate4_admin_reads_every_private_profile(monkeypatch, pg_url, pg_session_factory, world):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.d) as client:
        ids = _list_ids(client)
        r = client.get(f"/api/profiles/{world.p_secret}")
    assert {world.p_org, world.p_priv, world.p_secret} <= set(ids), ids
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "P Private Secret"


# ==========================================================================
# Gate 5 — the day-one assertion. 'org' is the default; nobody's Monday changes.
# ==========================================================================
@pytest.mark.parametrize("who", ["a", "b", "c", "d"])
def test_gate5_everyone_reads_an_org_profile(monkeypatch, pg_url, pg_session_factory, world, who):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, getattr(world, who)) as client:
        ids = _list_ids(client)
        r = client.get(f"/api/profiles/{world.p_org}")
    assert world.p_org in ids, (
        f"{who} lost sight of an 'org' profile. Every profile in the live "
        f"database is 'org' — this filter must change nothing on day one; got {ids}"
    )
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "org"


# ==========================================================================
# Gate 6 — reading is WIDER than editing. B reads P_priv and still gets 403.
# ==========================================================================
def test_gate6_viewer_can_read_but_cannot_edit(monkeypatch, pg_url, pg_session_factory, world):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as client:
        read = client.get(f"/api/profiles/{world.p_priv}")
        write = client.put(f"/api/profiles/{world.p_priv}", json=_VALID_EDIT)
    assert read.status_code == 200, "precondition: B can READ P_priv"
    assert write.status_code == 403, (
        f"B is a VIEWER of P_priv, not its owner. A 404 here would mean the edit "
        f"rule was widened to the read rule and the two collapsed into one; "
        f"got {write.status_code}: {write.text}"
    )
    assert write.json()["error"]["code"] == "forbidden"


def test_gate6_update_does_not_leak_existence_to_a_stranger(
    monkeypatch, pg_url, pg_session_factory, world
):
    """The oracle assertion. PUT used to 404 only for a missing id and 403 for
    every active profile the caller did not own, so C learned P_secret exists by
    trying to edit it."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as client:
        unreadable = client.put(f"/api/profiles/{world.p_secret}", json=_VALID_EDIT)
        missing = client.put(f"/api/profiles/{NO_SUCH_PROFILE_ID}", json=_VALID_EDIT)
    assert unreadable.status_code == 404, (
        f"PUT on an unreadable profile must 404, not 403: {unreadable.status_code}"
    )
    assert missing.status_code == 404, missing.text
    assert unreadable.json() == missing.json(), (
        f"PUT's 'unreadable' and 'does not exist' answers differ: "
        f"{unreadable.json()} vs {missing.json()}"
    )


def test_owner_may_still_edit_and_admin_too(monkeypatch, pg_url, pg_session_factory, world):
    """The new 404 sits in front of the edit path; it must not block the people
    who could always edit. Without this, 'PUT returns 404 for everyone' passes
    both gate 6 assertions."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        owner = client.put(f"/api/profiles/{world.p_secret}", json=_VALID_EDIT)
    with client_for(app, world.d) as client:
        admin = client.put(f"/api/profiles/{world.p_secret}", json=_VALID_EDIT)
    assert owner.status_code == 200, owner.text
    assert admin.status_code == 200, admin.text


# ==========================================================================
# Step 3 — the sharing endpoints. Gated on EDIT: 404 for what the caller
# cannot read, 403 for what they can read but not edit.
# ==========================================================================

_SHARING_OPS = (
    ("get", "/viewers", None),
    ("put", "/viewers", {"user_ids": []}),
    ("post", "/viewers", {"email": "c-stranger@umu.se"}),
    ("put", "/visibility", {"visibility": "org"}),
)


@pytest.mark.parametrize(("method", "suffix", "body"), _SHARING_OPS)
def test_sharing_ops_403_for_a_viewer_who_is_not_the_owner(
    monkeypatch, pg_url, pg_session_factory, world, method, suffix, body
):
    """B can read P_priv. B must not be able to re-share it, or the owner is not
    the owner."""
    app = setup_app(monkeypatch, pg_url)
    url = f"/api/profiles/{world.p_priv}{suffix}"
    with client_for(app, world.b) as client:
        r = client.request(method.upper(), url, json=body)
    assert r.status_code == 403, (
        f"{method.upper()} {suffix} let a non-owner viewer through with {r.status_code}: {r.text}"
    )
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(("method", "suffix", "body"), _SHARING_OPS)
def test_sharing_ops_404_for_a_stranger_with_no_existence_oracle(
    monkeypatch, pg_url, pg_session_factory, world, method, suffix, body
):
    """The mutation this exists to catch: guard all four with a bare
    `if not can_edit: raise 403`. B's 403 above still passes, and C gets a clean
    "does this profile exist?" oracle on the very endpoints this plan adds."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as client:
        unreadable = client.request(
            method.upper(), f"/api/profiles/{world.p_secret}{suffix}", json=body
        )
        missing = client.request(
            method.upper(), f"/api/profiles/{NO_SUCH_PROFILE_ID}{suffix}", json=body
        )
    assert unreadable.status_code == 404, (
        f"{method.upper()} {suffix} answered {unreadable.status_code} for a profile "
        f"the caller cannot read; anything but 404 confirms it exists: {unreadable.text}"
    )
    assert missing.status_code == 404, missing.text
    assert unreadable.json() == missing.json(), (
        f"{method.upper()} {suffix}: unreadable and nonexistent must be "
        f"indistinguishable; got {unreadable.json()} vs {missing.json()}"
    )


@pytest.mark.parametrize(("method", "suffix", "body"), _SHARING_OPS)
def test_sharing_ops_admin_may_manage_a_profile_they_do_not_own(
    monkeypatch, pg_url, pg_session_factory, world, method, suffix, body
):
    """The edit rule is created_by OR admin (profiles.py, spec 4.3). Gate 4 and
    the PUT-profile test exercise the admin arm elsewhere; without this, dropping
    `and not user.is_admin` from the sharing ops' ladder reddens nothing."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.d) as client:
        r = client.request(method.upper(), f"/api/profiles/{world.p_secret}{suffix}", json=body)
    assert r.status_code == 200, (
        f"{method.upper()} {suffix}: an admin must be able to manage sharing on a "
        f"profile they do not own; got {r.status_code}: {r.text}"
    )


def test_owner_lists_viewers_with_names_and_emails(monkeypatch, pg_url, pg_session_factory, world):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.get(f"/api/profiles/{world.p_priv}/viewers")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "viewers": [{"user_id": world.b, "name": "Bo Colleague", "email": "b-colleague@umu.se"}]
    }, "a list of bare ids is unusable in a UI — the response carries name and email"


def test_owner_replaces_viewers_and_the_response_is_the_servers_truth(
    monkeypatch, pg_url, pg_session_factory, world
):
    """Reconcile-from-the-answer, the pattern update_subscriptions already uses:
    ids that are not active users are dropped silently and the response says so."""
    with pg_session_factory() as s:
        inactive = User(email="pending@umu.se", name="Pending Person", status="pending")
        s.add(inactive)
        s.commit()
        s.refresh(inactive)
        inactive_id = inactive.id

    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.put(
            f"/api/profiles/{world.p_priv}/viewers",
            json={"user_ids": [world.c, inactive_id, NO_SUCH_PROFILE_ID, world.a]},
        )
    assert r.status_code == 200, r.text
    got = r.json()["viewers"]
    assert [v["user_id"] for v in got] == [world.c], (
        f"B was replaced by C; the inactive user, the unknown id and the OWNER's "
        f"own id are all dropped (the owner never has a row); got {got}"
    )
    with pg_session_factory() as s:
        rows = {
            tuple(x)
            for x in s.execute(text("SELECT profile_id, user_id FROM profile_viewers")).all()
        }
    assert rows == {(world.p_priv, world.c)}, rows

    # B was replaced, so B loses the profile.
    with client_for(app, world.b) as client:
        assert client.get(f"/api/profiles/{world.p_priv}").status_code == 404
    with client_for(app, world.c) as client:
        assert client.get(f"/api/profiles/{world.p_priv}").status_code == 200


def test_removing_the_last_viewer_does_not_flip_visibility(
    monkeypatch, pg_url, pg_session_factory, world
):
    """Visibility and the viewer list are independent. A profile with no viewers
    is a private profile with no viewers, not an org one."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.put(f"/api/profiles/{world.p_priv}/viewers", json={"user_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["viewers"] == []
    with pg_session_factory() as s:
        vis = s.scalar(text("SELECT visibility FROM profiles WHERE id = :p"), {"p": world.p_priv})
    assert vis == "private", f"emptying the viewer list must not touch visibility; got {vis!r}"
    with client_for(app, world.c) as client:
        assert client.get(f"/api/profiles/{world.p_priv}").status_code == 404


def test_owner_adds_a_viewer_by_email(monkeypatch, pg_url, pg_session_factory, world):
    """A non-admin owner has no endpoint that lists users (GET /api/admin/users is
    admin-only), so 'add by id' alone would leave them unable to learn a
    colleague's id. Adding by email is the shape the repo already uses."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.post(
            f"/api/profiles/{world.p_secret}/viewers", json={"email": "c-stranger@umu.se"}
        )
    assert r.status_code == 200, r.text
    assert [v["email"] for v in r.json()["viewers"]] == ["c-stranger@umu.se"]
    with client_for(app, world.c) as client:
        assert client.get(f"/api/profiles/{world.p_secret}").status_code == 200


def test_add_viewer_is_idempotent_and_the_owner_is_a_no_op(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        again = client.post(
            f"/api/profiles/{world.p_priv}/viewers", json={"email": "b-colleague@umu.se"}
        )
        owner = client.post(
            f"/api/profiles/{world.p_priv}/viewers", json={"email": "a-owner@umu.se"}
        )
    assert again.status_code == 200, again.text
    assert [v["user_id"] for v in again.json()["viewers"]] == [world.b]
    assert owner.status_code == 200, owner.text
    assert [v["user_id"] for v in owner.json()["viewers"]] == [world.b], (
        "the owner already has access through created_by and must NEVER get a "
        "viewer row — one would let 'remove this viewer' lock them out"
    )
    with pg_session_factory() as s:
        assert (
            s.scalar(
                text("SELECT count(*) FROM profile_viewers WHERE profile_id = :p"),
                {"p": world.p_priv},
            )
            == 1
        )


@pytest.mark.parametrize("email", ["nobody@umu.se", "pending@umu.se", "", "not-an-email"])
def test_add_viewer_gives_no_user_existence_oracle(
    monkeypatch, pg_url, pg_session_factory, world, email
):
    """Unknown, non-active or otherwise unaddable: one generic 400, the same one
    every time. A 404 (or a distinguishable message) would turn this endpoint
    into a directory of who has an account."""
    with pg_session_factory() as s:
        s.add(User(email="pending@umu.se", name="Pending Person", status="pending"))
        s.commit()

    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.post(f"/api/profiles/{world.p_secret}/viewers", json={"email": email})
        known_but_unaddable = client.post(
            f"/api/profiles/{world.p_secret}/viewers", json={"email": "pending@umu.se"}
        )
    assert r.status_code == 400, f"expected the generic 400, got {r.status_code}: {r.text}"
    assert r.json() == known_but_unaddable.json(), (
        f"the answer for {email!r} differs from the answer for a real but "
        f"non-active account, which is the oracle: {r.json()} vs "
        f"{known_but_unaddable.json()}"
    )
    with pg_session_factory() as s:
        assert (
            s.scalar(
                text("SELECT count(*) FROM profile_viewers WHERE profile_id = :p"),
                {"p": world.p_secret},
            )
            == 0
        )


def test_owner_flips_visibility_both_ways(monkeypatch, pg_url, pg_session_factory, world):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        to_private = client.put(
            f"/api/profiles/{world.p_org}/visibility", json={"visibility": "private"}
        )
        assert to_private.status_code == 200, to_private.text
        assert to_private.json() == {"visibility": "private"}
    # C could read P_org a moment ago.
    with client_for(app, world.c) as stranger:
        assert stranger.get(f"/api/profiles/{world.p_org}").status_code == 404
    with client_for(app, world.a) as client:
        back = client.put(f"/api/profiles/{world.p_org}/visibility", json={"visibility": "org"})
        assert back.status_code == 200, back.text
        assert back.json() == {"visibility": "org"}
        assert client.get(f"/api/profiles/{world.p_org}").json()["visibility"] == "org"
    with client_for(app, world.c) as stranger:
        assert stranger.get(f"/api/profiles/{world.p_org}").status_code == 200


def test_viewer_rows_survive_a_visibility_round_trip(
    monkeypatch, pg_url, pg_session_factory, world
):
    """P_priv has B's viewer row. Flipping to 'org' and back must not delete it:
    visibility and the viewer list are independent, so an owner can widen and
    re-narrow without re-inviting everyone. The other flip test uses P_org,
    which has no rows, so a flip that deleted viewers reddened nothing there."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        assert (
            client.put(
                f"/api/profiles/{world.p_priv}/visibility", json={"visibility": "org"}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/profiles/{world.p_priv}/visibility", json={"visibility": "private"}
            ).status_code
            == 200
        )
    with pg_session_factory() as s:
        rows = (
            s.execute(select(ProfileViewer.user_id).where(ProfileViewer.profile_id == world.p_priv))
            .scalars()
            .all()
        )
    assert rows == [world.b], f"the viewer row must survive the round trip; got {rows}"
    with client_for(app, world.b) as viewer:
        assert viewer.get(f"/api/profiles/{world.p_priv}").status_code == 200


def test_visibility_rejects_a_value_outside_the_vocabulary(
    monkeypatch, pg_url, pg_session_factory, world
):
    """The CHECK constraint is the last line, not the first: an unknown value is
    refused at the schema before it can reach the database."""
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = client.put(f"/api/profiles/{world.p_org}/visibility", json={"visibility": "maybe"})
    assert r.status_code == 422, f"got {r.status_code}: {r.text}"
    with pg_session_factory() as s:
        assert (
            s.scalar(text("SELECT visibility FROM profiles WHERE id = :p"), {"p": world.p_org})
            == "org"
        )


def test_detail_carries_visibility_so_the_owner_ui_can_render_its_own_state(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        org = client.get(f"/api/profiles/{world.p_org}").json()
        priv = client.get(f"/api/profiles/{world.p_priv}").json()
    assert org["visibility"] == "org"
    assert priv["visibility"] == "private"


# ==========================================================================
# Task 4 — the calls API and the dashboard.
#
# These surfaces do not look like profile endpoints, which is exactly why a
# fix misses them: `GET /api/calls` resolves `?profile=` AND the stored
# default in web/dashboard.py, `GET /api/calls/{id}` resolves `?profile=` a
# second time with a lookup of its own, and the facet list hands out profile
# NAMES to everyone.
#
# THE FIXTURE MUST BE LIVE. "No P_secret score appears" is already true of an
# empty database, and a gate that passes against an empty database is not a
# gate. So every absence below is paired with a presence on the SAME row: the
# owner sees the score and the rendered criterion detail that the stranger
# does not.
# ==========================================================================


@dataclass(frozen=True)
class RankedWorld:
    """One call, ranked against all three profiles of `World`.

    `score`, `budget_marker` and `trl_marker` are keyed by profile id. The
    markers are strings that appear in `_criterion_detail`'s rendering for
    exactly ONE profile, so "C did not see P_secret's numbers" is asserted
    against text that provably exists in the database and that the owner
    provably does receive.
    """

    call_id: int
    score: dict[int, int]
    budget_marker: dict[int, str]
    trl_marker: dict[int, str]


def seed_ranked_calls(pg_session_factory, world: World) -> RankedWorld:
    """Make the world RANKED: one call with a CallRanking + criteria rows for
    each of P_org, P_priv and P_secret.

    Shape copied from tests/test_digest_assemble.py::_seed_call (same Call +
    CallRanking pair); the criteria rows and the structured budget/TRL fields
    are what make `_criterion_detail` render a non-None string, which is what
    gate 9 is about. Task 5 reuses this helper for the digest rather than
    building a second world.

    SIDE EFFECT worth knowing before you reuse it: it also STAMPS each profile's
    rubric bands — a distinct budget_min/budget_max/trl_min/trl_max per profile,
    plus budget_currency — so the markers below are unique per profile.
    """
    spec = {
        # profile id: (score, budget_min, budget_max, trl_min, trl_max)
        world.p_org: (41, 111_111, 222_222, 2, 5),
        world.p_priv: (62, 555_555, 666_666, 4, 8),
        world.p_secret: (93, 777_777, 888_888, 6, 9),
    }
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        call = Call(
            source_id=src.id,
            external_id="ranked-against-all-three",
            url="https://example.invalid/ranked-against-all-three",
            title="Precision oncology platform call",
            summary="A call ranked against all three profiles.",
            keywords=["oncology"],
            deadline=datetime.now(UTC) + timedelta(days=90),
            published_at=datetime.now(UTC) - timedelta(days=1),
            budget_min=1_000_000,
            budget_max=2_000_000,
            currency="SEK",
            raw={"trl_min": 3, "trl_max": 7},
        )
        s.add(call)
        s.flush()
        for pid, (score, b_min, b_max, t_min, t_max) in spec.items():
            p = s.get(Profile, pid)
            p.structured = {
                **(p.structured or {}),
                "budget_min": b_min,
                "budget_max": b_max,
                "trl_min": t_min,
                "trl_max": t_max,
            }
            p.budget_currency = "SEK"
            s.add(
                CallRanking(
                    call_id=call.id,
                    profile_id=pid,
                    aggregate_score=score,
                    rationale=f"ranked against {p.name}",
                    model="gemma4:e4b-q8",
                    prompt_version="v2.0",
                    ranked_at=datetime.now(UTC),
                )
            )
            for criterion in ("budget", "trl"):
                s.add(
                    CallRankingCriterion(
                        call_id=call.id,
                        profile_id=pid,
                        criterion=criterion,
                        rating="pass",
                    )
                )
        call_id = call.id
        s.commit()

    ranked = RankedWorld(
        call_id=call_id,
        score={pid: v[0] for pid, v in spec.items()},
        budget_marker={pid: f"{v[1]:,}" for pid, v in spec.items()},
        trl_marker={pid: f"{v[3]}–{v[4]}" for pid, v in spec.items()},
    )
    # The markers are only useful if they are unique — a shared marker would
    # make "P_secret's numbers are absent" accidentally false for a reason
    # that has nothing to do with the read rule.
    assert len(set(ranked.budget_marker.values())) == 3, ranked.budget_marker
    assert len(set(ranked.trl_marker.values())) == 3, ranked.trl_marker
    return ranked


@pytest.fixture
def ranked(pg_session_factory, world) -> RankedWorld:
    return seed_ranked_calls(pg_session_factory, world)


def _scores(body: dict) -> list[int | None]:
    return [item["aggregate_score"] for item in body["items"]]


def _facet_ids(client) -> list[int]:
    r = client.get("/api/calls")
    assert r.status_code == 200, r.text
    return [p["id"] for p in r.json()["facets"]["profiles"]]


# ==========================================================================
# Gate 7 — the EXPLICIT arm. An unreadable ?profile degrades; it never 404s.
# ==========================================================================
def test_gate7_explicit_unreadable_profile_degrades_with_no_oracle(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    # Precondition: the fixture is LIVE — this exact request, made by the
    # owner, returns P_secret's score. Without this the assertions below are
    # equally true of an empty database.
    with client_for(app, world.a) as owner:
        seen = owner.get(f"/api/calls?profile={world.p_secret}").json()
    assert seen["effective"]["profile_id"] == world.p_secret, seen["effective"]
    assert _scores(seen) == [ranked.score[world.p_secret]], seen["items"]

    with client_for(app, world.c) as stranger:
        unreadable = stranger.get(f"/api/calls?profile={world.p_secret}")
        missing = stranger.get(f"/api/calls?profile={NO_SUCH_PROFILE_ID}")
    assert unreadable.status_code == 200, (
        f"an unreadable ?profile must DEGRADE to the unranked list. A 404 (or "
        f"any status that differs from the one a nonexistent id gets) turns "
        f"?profile=N into an oracle for every id a user can type; got "
        f"{unreadable.status_code}: {unreadable.text}"
    )
    body = unreadable.json()
    assert body["effective"]["profile_id"] is None, (
        f"assert the RESOLVED value, not merely the absence of scores: "
        f"{body['effective']}"
    )
    assert _scores(body) == [None], (
        f"P_secret's score reached a stranger through ?profile=: {body['items']}"
    )
    assert unreadable.json() == missing.json(), (
        f"the 'unreadable' and 'does not exist' answers differ, which is the "
        f"oracle itself: {unreadable.json()} vs {missing.json()}"
    )


# ==========================================================================
# Gate 7b — THE BYPASS. The stored default is a second arm, and no ?profile=
# is ever sent, so a patch to the explicit arm alone never runs.
# ==========================================================================
def test_gate7b_storing_an_unreadable_profile_as_the_default_is_refused(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as stranger:
        refused = stranger.post("/api/me/default-profile", json={"profile_id": world.p_secret})
        missing = stranger.post(
            "/api/me/default-profile", json={"profile_id": NO_SUCH_PROFILE_ID}
        )
    assert refused.status_code == 404, (
        f"POST /api/me/default-profile stored an unreadable profile: "
        f"{refused.status_code}: {refused.text}"
    )
    assert refused.json() == missing.json(), (
        f"one status AND one body for missing, inactive and unreadable, or "
        f"storing is itself the oracle: {refused.json()} vs {missing.json()}"
    )
    with pg_session_factory() as s:
        assert s.get(User, world.c).default_profile_id is None
    # "404 for everyone" would pass both assertions above.
    with client_for(app, world.a) as owner:
        allowed = owner.post("/api/me/default-profile", json={"profile_id": world.p_secret})
    assert allowed.status_code == 200, f"the OWNER must still be able to store it: {allowed.text}"
    with pg_session_factory() as s:
        assert s.get(User, world.a).default_profile_id == world.p_secret


def test_gate7b_a_stored_unreadable_default_degrades_on_a_bare_visit(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    """The hole the whole task exists for. The row is written directly because
    the endpoint now refuses it — but it arrives legitimately too: stored while
    the profile was shared, then revoked. No `?profile=` is ever sent, so an
    explicit-arm-only patch is never reached.
    """
    with pg_session_factory() as s:
        s.get(User, world.c).default_profile_id = world.p_secret
        s.get(User, world.a).default_profile_id = world.p_secret
        s.commit()

    app = setup_app(monkeypatch, pg_url)
    # Live fixture: the same stored default, for its owner, ranks the feed.
    with client_for(app, world.a) as owner:
        owned = owner.get("/api/calls").json()
    assert owned["effective"]["profile_id"] == world.p_secret, owned["effective"]
    assert _scores(owned) == [ranked.score[world.p_secret]], owned["items"]

    with client_for(app, world.c) as stranger:
        bare = stranger.get("/api/calls")
    assert bare.status_code == 200, bare.text
    body = bare.json()
    assert body["effective"]["profile_id"] is None, (
        f"a stored default the user may not read must degrade to unranked on a "
        f"BARE visit, exactly as a deactivated one does; got {body['effective']}"
    )
    assert _scores(body) == [None], (
        f"P_secret's score reached a stranger with no ?profile= in the URL at "
        f"all — the stored-default arm: {body['items']}"
    )
    with pg_session_factory() as s:
        assert s.get(User, world.c).default_profile_id == world.p_secret, (
            "the resolver FILTERS the stored default; it must not delete the row"
        )


# ==========================================================================
# Gate 8 — the facet list. It hands out profile NAMES.
# ==========================================================================
def test_gate8_facets_offer_only_the_profiles_the_caller_may_read(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as stranger:
        r = stranger.get("/api/calls")
    assert r.status_code == 200, r.text
    facets = r.json()["facets"]["profiles"]
    ids = [p["id"] for p in facets]
    names = [p["name"] for p in facets]
    assert world.p_org in ids, (
        f"every profile in the live database is 'org' — the facet list must "
        f"still offer them, or this filter is a wall; got {ids}"
    )
    assert world.p_secret not in ids, f"the facet list leaked P_secret: {ids}"
    assert world.p_priv not in ids, f"the facet list leaked P_priv to a non-viewer: {ids}"
    assert "P Private Secret" not in names, (
        f"the NAME is the payload here — someone can read the whole research "
        f"programme off a dropdown; got {names}"
    )


def test_gate8_facets_are_a_filter_not_a_wall(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    """Owner, viewer and admin each keep the profiles they are entitled to."""
    app = setup_app(monkeypatch, pg_url)
    expected = {
        world.a: {world.p_org, world.p_priv, world.p_secret},  # owner of all three
        world.b: {world.p_org, world.p_priv},  # viewer of P_priv only
        world.d: {world.p_org, world.p_priv, world.p_secret},  # admin
    }
    for uid, want in expected.items():
        with client_for(app, uid) as client:
            got = set(_facet_ids(client))
        assert got == want, f"user {uid}: facet list {sorted(got)} != {sorted(want)}"


# ==========================================================================
# Gate 9 — the call-detail surface, which resolves ?profile= ITSELF.
# ==========================================================================
def test_gate9_call_detail_hides_an_unreadable_profiles_ranking_and_criteria(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    url = f"/api/calls/{ranked.call_id}?profile={world.p_secret}"
    with client_for(app, world.a) as owner:
        seen = owner.get(url)
    assert seen.status_code == 200, seen.text
    body = seen.json()
    # Live fixture: there IS a ranking, and there IS a rendered detail string.
    assert body["ranking"]["aggregate_score"] == ranked.score[world.p_secret], body["ranking"]
    details = {c["criterion"]: c["detail"] for c in body["criteria"]}
    assert ranked.budget_marker[world.p_secret] in (details.get("budget") or ""), details
    assert ranked.trl_marker[world.p_secret] in (details.get("trl") or ""), details

    with client_for(app, world.c) as stranger:
        hidden = stranger.get(url)
        missing = stranger.get(f"/api/calls/{ranked.call_id}?profile={NO_SUCH_PROFILE_ID}")
    assert hidden.status_code == 200, (
        f"the call itself is readable — only the profile's view of it is not. "
        f"A 404 here would be an oracle: {hidden.status_code}: {hidden.text}"
    )
    hidden_body = hidden.json()
    assert hidden_body["ranking"] is None, hidden_body["ranking"]
    assert hidden_body["criteria"] == [], hidden_body["criteria"]
    assert ranked.budget_marker[world.p_secret] not in hidden.text, hidden.text
    assert ranked.trl_marker[world.p_secret] not in hidden.text, hidden.text
    # The invariant, stated at the boundary: profile_obj is None whenever the
    # profile is not readable — indistinguishable from an id that does not
    # exist, which is exactly what `profile_obj = None` produces.
    assert hidden.json() == missing.json(), (
        f"unreadable and nonexistent must be the same answer: "
        f"{hidden.json()} vs {missing.json()}"
    )


# ==========================================================================
# Gate 10 — the viewer. A shared profile must be USABLE, not merely visible.
# ==========================================================================
def test_gate10_the_viewer_can_use_the_shared_profile_on_every_surface(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as viewer:
        feed = viewer.get(f"/api/calls?profile={world.p_priv}").json()
        facets = _facet_ids(viewer)
        stored = viewer.post("/api/me/default-profile", json={"profile_id": world.p_priv})
        bare = viewer.get("/api/calls").json()
        detail = viewer.get(f"/api/calls/{ranked.call_id}?profile={world.p_priv}").json()
        other = viewer.get(f"/api/calls?profile={world.p_secret}").json()

    assert feed["effective"]["profile_id"] == world.p_priv, feed["effective"]
    assert _scores(feed) == [ranked.score[world.p_priv]], (
        f"B has a profile_viewers row on P_priv and must SEE its score — a "
        f"shared profile that ranks nothing is not shared; got {feed['items']}"
    )
    assert world.p_priv in facets, facets
    assert stored.status_code == 200, stored.text
    assert bare["effective"]["profile_id"] == world.p_priv, bare["effective"]
    assert _scores(bare) == [ranked.score[world.p_priv]], bare["items"]
    assert detail["ranking"]["aggregate_score"] == ranked.score[world.p_priv], detail["ranking"]
    details = {c["criterion"]: c["detail"] for c in detail["criteria"]}
    assert ranked.budget_marker[world.p_priv] in (details.get("budget") or ""), details
    assert other["effective"]["profile_id"] is None, (
        "B's viewer row is for P_priv ONLY — it must not resolve P_secret"
    )


# ==========================================================================
# Gate 10b — revocation is a FILTER, not a delete.
# ==========================================================================
def test_gate10b_revoking_a_share_degrades_the_stored_default_without_erasing_it(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as viewer:
        assert (
            viewer.post(
                "/api/me/default-profile", json={"profile_id": world.p_priv}
            ).status_code
            == 200
        )
        before = viewer.get("/api/calls").json()
    assert before["effective"]["profile_id"] == world.p_priv, (
        f"precondition: while shared, B's bare visit is RANKED by P_priv; "
        f"got {before['effective']}"
    )
    assert _scores(before) == [ranked.score[world.p_priv]], before["items"]

    with client_for(app, world.a) as owner:
        revoke = owner.put(f"/api/profiles/{world.p_priv}/viewers", json={"user_ids": []})
    assert revoke.status_code == 200, revoke.text

    with client_for(app, world.b) as viewer:
        after = viewer.get("/api/calls").json()
    assert after["effective"]["profile_id"] is None, (
        f"the share is gone, so the next bare visit must degrade to unranked; "
        f"got {after['effective']}"
    )
    assert _scores(after) == [None], after["items"]
    with pg_session_factory() as s:
        assert s.get(User, world.b).default_profile_id == world.p_priv, (
            "revocation FILTERS the preference; it must not delete the row, or "
            "re-sharing silently loses the colleague's setting"
        )

    with client_for(app, world.a) as owner:
        reshare = owner.post(
            f"/api/profiles/{world.p_priv}/viewers", json={"email": "b-colleague@umu.se"}
        )
    assert reshare.status_code == 200, reshare.text
    with client_for(app, world.b) as viewer:
        restored = viewer.get("/api/calls").json()
    assert restored["effective"]["profile_id"] == world.p_priv, (
        f"re-sharing must restore the stored preference with no second POST "
        f"from B — that is what 'filter, not delete' buys; got "
        f"{restored['effective']}"
    )
    assert _scores(restored) == [ranked.score[world.p_priv]], restored["items"]


# ==========================================================================
# best_profile_id — a DECISION, pinned so that widening it is a visible act.
# ==========================================================================
def test_best_profile_id_stays_owner_scoped_by_decision(
    monkeypatch, pg_url, pg_session_factory, world, ranked
):
    """`best_profile_id` is `Profile.created_by == user.id`, and stays that way.

    Widening it to the READ rule is defensible product-wise (a shared profile
    that is never chosen reads as a bug) but it is NOT a day-one no-op: every
    profile in the live database is 'org', so a widened query would pick from
    every profile in the organisation and change every user's call page the day
    this merges. That is a product decision for a human, not part of a privacy
    fix — so it is pinned here rather than left as an unrecorded choice.

    Delete this test in the commit that widens it.
    """
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as owner:
        owned = owner.get(f"/api/calls/{ranked.call_id}").json()
    with client_for(app, world.b) as viewer:
        shared = viewer.get(f"/api/calls/{ranked.call_id}").json()
    with client_for(app, world.d) as admin:
        everything = admin.get(f"/api/calls/{ranked.call_id}").json()

    assert owned["best_profile_id"] == world.p_secret, (
        f"the fixture is live and best-fit HAS an answer to give: A owns all "
        f"three ranked profiles and P_secret scores highest; got "
        f"{owned['best_profile_id']}"
    )
    assert shared["best_profile_id"] is None, (
        f"B can READ P_org (org-wide) and P_priv (shared) and BOTH are ranked "
        f"on this call, so a best-fit widened to the read rule would answer "
        f"{world.p_priv} here. It answers None because the scope is ownership, "
        f"deliberately; got {shared['best_profile_id']}"
    )
    assert everything["best_profile_id"] is None, (
        f"an admin reads every profile, so a widened best-fit would answer "
        f"{world.p_secret} here; got {everything['best_profile_id']}"
    )
