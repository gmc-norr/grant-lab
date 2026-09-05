"""The twelfth surface: POST /api/workspaces {profile_id} (PD-9).

The same world as tests/test_profile_access.py — imported, not rebuilt (see that
module's docstring). This file exists because the surface is not a "profile"
surface at all: it is proposal-assist workspace creation, and its `profile_id`
was validated by EXISTENCE alone. Two things were wrong with that:

  * 400 vs 201 is an existence oracle over every profile id, on a route nobody
    reviewing "who may read a profile" would think to open; and
  * Phase-0 setup copies the chosen profile's `description` into
    `ws.profile_desc_snapshot`, which the drafting agent's read-only get_profile
    tool returns on a plain "ask" turn. The oracle was the smaller half.

What trivially passes: asserting only that C is refused P_secret. A route that
400s every profile_id passes that and breaks every user. Gates B-201 and A-201
are what make this a FILTER and not a WALL, and the org control is the day-one
assertion — every profile in the live database is 'org', so creation from the
dashboard's resolved profile must keep working for everyone.

No filesystem work happens here: create_workspace() writes DB rows and enqueues
the Phase-0 job, it does not build the git repo (that is the job's work, and the
job is never run in this file). The autouse conftest fixture
`_workspaces_dir_is_never_the_real_one` points WORKSPACES_DIR at a temp root
regardless.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, Source, Workspace
from tests.test_profile_access import (
    NO_SUCH_PROFILE_ID,
    World,
    client_for,
    seed_world,
    setup_app,
)


@pytest.fixture
def world(pg_session_factory) -> World:
    return seed_world(pg_session_factory)


@pytest.fixture
def call_id(pg_session_factory) -> int:
    """One Call, so `call_id` is never the reason a request is refused."""
    with pg_session_factory() as s:
        src = Source(name="pv-workspaces", slug="pv-workspaces")
        s.add(src)
        s.flush()
        call = Call(
            source_id=src.id,
            external_id="ext-pv-workspaces",
            url="https://x.test/pv-workspaces",
            title="A Linked Call",
        )
        s.add(call)
        s.commit()
        return call.id


def _create(client, call_id: int, profile_id: int):
    return client.post(
        "/api/workspaces",
        json={"call_id": call_id, "title": "From the dashboard", "profile_id": profile_id},
    )


def _answer_without_the_echoed_id(response, profile_id: int) -> dict:
    """The refusal body with the caller's OWN id blanked out.

    The sibling surfaces compare `unreadable.json() == missing.json()` verbatim
    because their detail is the constant "profile not found". This one echoes the
    id back ("No such profile: 41"), deliberately kept identical to the string the
    existence check already used — so verbatim equality is impossible and would be
    the wrong property anyway: the id came from the caller, who already knows it.
    What must not differ is everything else. Substituting the sent id is how that
    is asserted, and it is strictly stronger than comparing status codes: a
    "not shared with you" message would survive a status-only check.
    """
    return json.loads(response.text.replace(str(profile_id), "<ID>"))


# ==========================================================================
# Gate A — the stranger. No oracle: unreadable answers exactly like missing.
# ==========================================================================
def test_stranger_cannot_create_a_workspace_against_a_private_profile(
    monkeypatch, pg_url, pg_session_factory, world, call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as client:
        unreadable = _create(client, call_id, world.p_secret)
        missing = _create(client, call_id, NO_SUCH_PROFILE_ID)
    assert unreadable.status_code == 400, (
        f"C is not the owner of P_secret ({world.p_secret}), is not a viewer and "
        f"is not an admin; a 201 hands them the profile's description through "
        f"ws.profile_desc_snapshot: {unreadable.status_code} {unreadable.text}"
    )
    assert missing.status_code == 400, missing.text
    assert _answer_without_the_echoed_id(
        unreadable, world.p_secret
    ) == _answer_without_the_echoed_id(missing, NO_SUCH_PROFILE_ID), (
        f"the 'unreadable' and 'does not exist' answers differ, which is the "
        f"oracle itself: {unreadable.text} vs {missing.text}"
    )
    with pg_session_factory() as s:
        assert s.scalars(select(Workspace)).all() == [], (
            "the refusal must happen BEFORE create_workspace: a workspace row "
            "(with its Phase-0 job) survived a 400"
        )


# ==========================================================================
# Gate B — the colleague. A viewer row is a real grant here too.
# ==========================================================================
def test_viewer_may_create_a_workspace_against_the_shared_private_profile(
    monkeypatch, pg_url, pg_session_factory, world, call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as client:
        shared = _create(client, call_id, world.p_priv)
        not_shared = _create(client, call_id, world.p_secret)
    assert shared.status_code == 201, (
        f"B has a profile_viewers row on P_priv ({world.p_priv}) — sharing that "
        f"grants reading must grant this: {shared.text}"
    )
    assert not_shared.status_code == 400, (
        "B's viewer row is for P_priv ONLY — it must not reach P_secret"
    )
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(shared.json()["id"]))
        assert ws is not None and ws.profile_id == world.p_priv, (
            "a 201 that dropped the profile would pass a status-only gate while "
            "silently changing what Phase-0 snapshots"
        )


# ==========================================================================
# Gate C — the owner. created_by is its own arm; no viewer row exists.
# ==========================================================================
def test_owner_may_create_a_workspace_against_their_own_private_profile(
    monkeypatch, pg_url, pg_session_factory, world, call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.a) as client:
        r = _create(client, call_id, world.p_secret)
    assert r.status_code == 201, (
        f"the owner of P_secret cannot start a proposal against it — the check is "
        f"a WALL, not a filter: {r.text}"
    )
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(r.json()["id"]))
        assert ws is not None and ws.profile_id == world.p_secret


# ==========================================================================
# Gate D — day one. 'org' is the default, so this route changes for nobody.
# ==========================================================================
@pytest.mark.parametrize("who", ["a", "b", "c", "d"])
def test_everyone_may_create_a_workspace_against_an_org_profile(
    monkeypatch, pg_url, pg_session_factory, world, call_id, who
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, getattr(world, who)) as client:
        r = _create(client, call_id, world.p_org)
    assert r.status_code == 201, (
        f"{who} can no longer start a proposal against an 'org' profile. Every "
        f"profile in the live database is 'org' — this check must change nothing "
        f"on day one: {r.text}"
    )


# ==========================================================================
# The admin arm, which has neither ownership nor a viewer row.
# ==========================================================================
def test_admin_may_create_a_workspace_against_any_private_profile(
    monkeypatch, pg_url, pg_session_factory, world, call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.d) as client:
        r = _create(client, call_id, world.p_secret)
    assert r.status_code == 201, r.text
