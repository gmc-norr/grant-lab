"""Task 5's three surfaces: the settings picker, the subscription writer, the digest.

The same world as tests/test_profile_access.py — imported, not rebuilt, so every
surface in this plan is asserted against the same four users and the same three
profiles (see that module's docstring). This file exists only because that one is
already 1084 lines; it is the same fixture and the same gate numbering.

**A subscriber receives, every Monday, the ranked list of calls matching a
profile.** That reveals its content whether or not the API hides its
`description`, so field-level hiding alone would be a lock on one door of two.

THE FIXTURE MUST BE LIVE. `assemble_for_user` emits a section only when a profile
has matching calls (`if not ordered: continue`), so with a users-and-profiles-only
world "B's digest has no P_priv section" is ALREADY TRUE of code that never checks
readability. Two things answer that here:

  * gate 13 shows the section APPEARING (revoke, re-share, it is back), which no
    users-and-profiles-only fixture can do; and
  * every absence assertion is written against a section list that still contains
    P_org, so an assemble that returned None for an unrelated reason is red, not
    green.

P_org is the control on purpose: it is the DEFAULT visibility, it is what every
profile in the live database is today, and a digest that lost it would be the
loudest possible day-one break. `seed_ranked_calls` scores P_org at 20 — below
`DIGEST_BORDERLINE_LOWER` — so this file seeds a second call, ranked against
P_org alone, to give the control a section of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, CallRanking, Source, Subscription
from grant_lab.digest.assemble import DigestPayload, assemble_for_user
from tests.test_profile_access import (
    RankedWorld,
    World,
    client_for,
    seed_ranked_calls,
    seed_world,
    setup_app,
)

# Comfortably inside the top bucket (DIGEST_TOP_THRESHOLD = 60), and distinct
# from every score seed_ranked_calls uses so a mis-attributed entry is visible.
ORG_CONTROL_SCORE = 71


@pytest.fixture
def world(pg_session_factory) -> World:
    return seed_world(pg_session_factory)


@pytest.fixture
def ranked(pg_session_factory, world) -> RankedWorld:
    return seed_ranked_calls(pg_session_factory, world)


@pytest.fixture
def org_call_id(pg_session_factory, world, ranked) -> int:
    """A second call, ranked against P_org ALONE and high enough to be a section.

    A separate call rather than a second ranking on the shared one: dedup gives a
    call to exactly one profile (highest score wins), so ranking the same call
    against both profiles would silently empty one of the two sections this file
    is comparing.
    """
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        call = Call(
            source_id=src.id,
            external_id="ranked-against-p-org-only",
            url="https://example.invalid/ranked-against-p-org-only",
            title="Org-wide oncology platform call",
            summary="A call ranked against P_org alone.",
            keywords=["oncology"],
            deadline=datetime.now(UTC) + timedelta(days=120),
            published_at=datetime.now(UTC) - timedelta(days=1),
        )
        s.add(call)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=world.p_org,
                aggregate_score=ORG_CONTROL_SCORE,
                rationale="ranked against P Org Wide",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
        call_id = call.id
        s.commit()
    return call_id


def _assemble(pg_session_factory, user_id: int) -> DigestPayload | None:
    """The digest this user would be emailed right now."""
    with pg_session_factory() as s:
        return assemble_for_user(s, user_id=user_id, now=datetime.now(UTC))


def _section_profile_ids(payload: DigestPayload | None) -> list[int]:
    """The profiles that got a section, id-sorted (the payload orders by name)."""
    return [] if payload is None else sorted(sec.profile_id for sec in payload.sections)


def _call_ids_in(payload: DigestPayload | None, profile_id: int) -> list[int]:
    if payload is None:
        return []
    return [
        e.call_id for sec in payload.sections if sec.profile_id == profile_id for e in sec.entries
    ]


def _subscription_rows(pg_session_factory, user_id: int) -> dict[int, bool]:
    with pg_session_factory() as s:
        return dict(
            s.execute(
                select(Subscription.profile_id, Subscription.digest_enabled).where(
                    Subscription.user_id == user_id
                )
            ).all()
        )


def _subscribe(client, profile_ids: list[int]) -> list[int]:
    r = client.put("/api/me/subscriptions", json={"profile_ids": profile_ids})
    assert r.status_code == 200, r.text
    return r.json()["subscribed"]


def _revoke(app, world: World) -> None:
    with client_for(app, world.a) as owner:
        r = owner.put(f"/api/profiles/{world.p_priv}/viewers", json={"user_ids": []})
    assert r.status_code == 200, r.text


def _reshare(app, world: World) -> None:
    with client_for(app, world.a) as owner:
        r = owner.post(
            f"/api/profiles/{world.p_priv}/viewers", json={"email": "b-colleague@umu.se"}
        )
    assert r.status_code == 200, r.text


# ==========================================================================
# Gate 13 — WRITTEN FIRST, and it must pass before gate 12's absence is worth
# anything: it shows the P_priv section APPEARING after a re-share, which a
# fixture that could never produce one cannot do.
# ==========================================================================
def test_gate13_resharing_brings_the_section_back_with_no_action_from_the_viewer(
    monkeypatch, pg_url, pg_session_factory, world, ranked, org_call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as viewer:
        assert _subscribe(viewer, [world.p_org, world.p_priv]) == sorted(
            [world.p_org, world.p_priv]
        )

    while_shared = _assemble(pg_session_factory, world.b)
    assert _section_profile_ids(while_shared) == sorted([world.p_org, world.p_priv]), (
        "precondition: the fixture is LIVE — while the share stands, B's digest "
        f"has a section for each subscribed profile; got "
        f"{_section_profile_ids(while_shared)}"
    )
    assert _call_ids_in(while_shared, world.p_priv) == [ranked.call_id]
    assert _call_ids_in(while_shared, world.p_org) == [org_call_id]

    _revoke(app, world)
    _reshare(app, world)

    restored = _assemble(pg_session_factory, world.b)
    assert _section_profile_ids(restored) == sorted([world.p_org, world.p_priv]), (
        "re-sharing must restore the section with NO second subscribe from B — "
        "that is what 'revocation is a filter, not a delete' buys. A section that "
        "cannot come back means the subscription row was destroyed on revoke; got "
        f"{_section_profile_ids(restored)}"
    )
    assert _call_ids_in(restored, world.p_priv) == [ranked.call_id], (
        "the restored section must carry the ranked call again, not merely exist"
    )


# ==========================================================================
# Gate 12 — the gate this task exists for. Revocation removes the profile from
# the NEXT digest, and touches no subscription row doing it.
# ==========================================================================
def test_gate12_revoking_a_share_removes_the_section_but_keeps_the_subscription(
    monkeypatch, pg_url, pg_session_factory, world, ranked, org_call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as viewer:
        _subscribe(viewer, [world.p_org, world.p_priv])
    before = _assemble(pg_session_factory, world.b)
    assert _section_profile_ids(before) == sorted([world.p_org, world.p_priv]), (
        f"precondition: B's digest carries both sections; got "
        f"{_section_profile_ids(before)}"
    )

    _revoke(app, world)

    after = _assemble(pg_session_factory, world.b)
    assert _section_profile_ids(after) == [world.p_org], (
        f"the share is gone, so P_priv ({world.p_priv}) must not appear in B's "
        f"next digest — and P_org must still be there, or this is a wall and not "
        f"a filter; got {_section_profile_ids(after)}"
    )
    assert _call_ids_in(after, world.p_org) == [org_call_id]
    assert _subscription_rows(pg_session_factory, world.b) == {
        world.p_org: True,
        world.p_priv: True,
    }, (
        "revocation is a FILTER: no subscription row is touched, so the "
        "digest_enabled preference survives and a re-share restores the section "
        "without B doing anything"
    )


# ==========================================================================
# Gate 12b — one innocent settings save in the middle. Without it, a diff that
# filters `valid_ids` and not `existing_active_ids` DELETES the row and gate 12
# never notices.
# ==========================================================================
def test_gate12b_saving_settings_after_a_revocation_does_not_destroy_the_row(
    monkeypatch, pg_url, pg_session_factory, world, ranked, org_call_id
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.b) as viewer:
        _subscribe(viewer, [world.p_org, world.p_priv])
    with pg_session_factory() as s:
        created_at_before = s.execute(
            select(Subscription.created_at).where(
                Subscription.user_id == world.b, Subscription.profile_id == world.p_priv
            )
        ).scalar_one()

    _revoke(app, world)

    # B opens Settings and presses Save — the picker echoes back exactly the
    # subscribed ids the endpoint just offered it, which is the only body a real
    # client can send.
    with client_for(app, world.b) as viewer:
        offered = viewer.get("/api/me/settings")
        assert offered.status_code == 200, offered.text
        keep = [p["id"] for p in offered.json()["profiles"] if p["subscribed"]]
        _subscribe(viewer, keep)

    rows = _subscription_rows(pg_session_factory, world.b)
    assert rows == {world.p_org: True, world.p_priv: True}, (
        "an unreadable-but-ACTIVE profile must be invisible to the diff in BOTH "
        "directions, exactly like an inactive one. Filtering only `valid_ids` "
        "drops it out of the desired set while leaving it in "
        "`existing_active_ids`, so it lands in `to_remove` and one innocent save "
        f"destroys the row and its digest_enabled preference; got {rows}"
    )
    with pg_session_factory() as s:
        created_at_after = s.execute(
            select(Subscription.created_at).where(
                Subscription.user_id == world.b, Subscription.profile_id == world.p_priv
            )
        ).scalar_one()
    assert created_at_after == created_at_before, (
        "the row must be LEFT ALONE, not deleted and re-inserted — a re-insert "
        "resets created_at and would reset digest_enabled with it"
    )

    after = _assemble(pg_session_factory, world.b)
    assert _section_profile_ids(after) == [world.p_org], (
        f"and the section is still absent after the save; got "
        f"{_section_profile_ids(after)}"
    )


# ==========================================================================
# Gate 11 — the subscription WRITER. A stranger cannot subscribe to a profile
# they cannot read, and gets no row for trying.
# ==========================================================================
def test_gate11_a_stranger_cannot_subscribe_to_an_unreadable_profile(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as stranger:
        subscribed = _subscribe(stranger, [world.p_org, world.p_secret, world.p_priv])
    assert subscribed == [world.p_org], (
        f"P_secret ({world.p_secret}) and P_priv ({world.p_priv}) are unreadable "
        f"to C, so they are dropped exactly as an inactive id is — and P_org, the "
        f"org-wide default, is still accepted; got {subscribed}"
    )
    assert _subscription_rows(pg_session_factory, world.c) == {world.p_org: True}, (
        "the response must not be the only thing filtered: a written row would "
        "put the profile's ranked calls in C's Monday email"
    )


# ==========================================================================
# Gate 11b — the picker's READ side, asserted on the JSON. Settings.tsx calls
# GET /me/settings, not GET /api/profiles, so Task 3 never reached this query;
# and a frontend-only fix leaves the ids and NAMES in any browser console.
# ==========================================================================
def test_gate11b_the_settings_payload_offers_only_readable_profiles(
    monkeypatch, pg_url, pg_session_factory, world
):
    app = setup_app(monkeypatch, pg_url)
    with client_for(app, world.c) as stranger:
        r = stranger.get("/api/me/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(p["id"] for p in body["profiles"]) == [world.p_org], (
        f"the picker's read-model hands out an id AND a name per profile; C may "
        f"read only P_org ({world.p_org}); got {body['profiles']}"
    )
    assert "P Private Secret" not in r.text, (
        f"the NAME leaks the profile as surely as the id does: {r.text}"
    )
    assert "P Private Shared" not in r.text, r.text

    # The viewer's half: sharing makes the profile subscribable, so this is a
    # filter and not a wall.
    with client_for(app, world.b) as viewer:
        viewer_body = viewer.get("/api/me/settings").json()
    assert sorted(p["id"] for p in viewer_body["profiles"]) == sorted(
        [world.p_org, world.p_priv]
    ), (
        f"B is a viewer on P_priv and must be offered it; got "
        f"{viewer_body['profiles']}"
    )
