"""Gates for the profile-scoped decision API (spec 4.1).

Every gate here needs TWO profiles. A single-profile fixture passes each of them
against a `(user_id, call_id)` implementation, which is exactly the shape this
task replaces -- so "one user, two profiles, one call" is the fixture, not an
embellishment.

`saved` rows are written straight through the session: release 1 ships no save
endpoint yet (that is a later task), and a gate that can only reach the states
its own endpoint writes cannot see the state predicate at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from call_manager.db.models import Call, CallDecision, Profile, ProfileKind, Source, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _seed(
    pg_session_factory,
    *,
    n_profiles: int = 2,
    n_calls: int = 1,
    deadlines: list[datetime | None] | None = None,
):
    """One active user owning `n_profiles` active profiles, plus `n_calls` calls.

    Profiles take the server default visibility 'org', so `can_read_profile`
    admits them and the 404 arm under test is the UNREADABLE one, not "every
    profile 404s".

    `deadlines`, when given, must be length `n_calls` and overrides the per-call
    default of no deadline -- used by the closed-calls gate (D1), which needs
    calls whose deadline has passed without changing the fixture every other
    gate in this file seeds against. `published_at` ascends with the index, so
    DecisionCall1 is NEWER than DecisionCall0 in the unranked sort.
    """
    with pg_session_factory() as s:
        u = User(status="active", email="decider@umu.se")
        s.add(u)
        s.flush()
        pids = []
        for i in range(n_profiles):
            p = Profile(
                name=f"P{i + 1}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
                level="group",
                description="d",
                structured={},
                created_by=u.id,
            )
            s.add(p)
            s.flush()
            pids.append(p.id)
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        cids = []
        for i in range(n_calls):
            c = Call(
                source_id=src.id,
                external_id=f"DEC-{i}",
                url=f"https://x/dec{i}",
                title=f"DecisionCall{i}",
                published_at=datetime(2026, 4, i + 1, tzinfo=UTC),
                deadline=(deadlines[i] if deadlines else None),
            )
            s.add(c)
            s.flush()
            cids.append(c.id)
        s.commit()
        return u.id, pids, cids


def _client(app, uid):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def _titles(body) -> list[str]:
    return [i["title"] for i in body["items"]]


def _decide(pg_session_factory, *, user_id, profile_id, call_id, state):
    with pg_session_factory() as s:
        s.add(
            CallDecision(
                user_id=user_id,
                profile_id=profile_id,
                call_id=call_id,
                state=state,
                decided_by=user_id,
            )
        )
        s.commit()


def _states(pg_session_factory, *, user_id, call_id) -> dict[int, str]:
    with pg_session_factory() as s:
        return {
            r.profile_id: r.state
            for r in s.execute(
                select(CallDecision).where(
                    CallDecision.user_id == user_id, CallDecision.call_id == call_id
                )
            ).scalars()
        }


def _rows(pg_session_factory, *, user_id, call_id):
    """Every decision row for (user, call), as plain tuples.

    Returns a LIST, not a dict keyed by profile: the flip gate asserts
    `len(...) == 1`, and a dict would collapse a duplicated row into one entry
    and hide exactly the bug that gate exists to catch.
    """
    with pg_session_factory() as s:
        return [
            (r.profile_id, r.state, r.decided_at, r.decided_by)
            for r in s.execute(
                select(CallDecision)
                .where(CallDecision.user_id == user_id, CallDecision.call_id == call_id)
                .order_by(CallDecision.id)
            ).scalars()
        ]


def _second_user(pg_session_factory, email="colleague@umu.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email)
        s.add(u)
        s.commit()
        return u.id


# ---------------------------------------------------------------------------
# Gate 1 - per-profile isolation. Hide on P1; the call is gone for P1 and still
# there for P2. A single-profile fixture passes on a global dismissal, which is
# why both halves are asserted on the SAME call in the SAME test.
# ---------------------------------------------------------------------------
def test_hiding_on_one_profile_leaves_the_call_visible_on_the_other(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, p2), (cid,) = _seed(pg_session_factory)
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
        for_p1 = _titles(client.get("/api/calls", params={"profile": p1}).json())
        for_p2 = _titles(client.get("/api/calls", params={"profile": p2}).json())
    assert "DecisionCall0" not in for_p1, "a call hidden on P1 must leave P1's list"
    assert "DecisionCall0" in for_p2, (
        "hiding on P1 must NOT hide on P2 - that is the global dismissal this task removes"
    )


# ---------------------------------------------------------------------------
# Gate 3 - a SAVED call is not a hidden one. The archive predicate filters on
# state='hidden'; drop that and a saved call vanishes from the list the user
# saved it in.
# ---------------------------------------------------------------------------
def test_a_saved_call_stays_in_the_list_for_that_profile(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="saved")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        titles = _titles(client.get("/api/calls", params={"profile": p1}).json())
    assert "DecisionCall0" in titles, "'saved' is not 'hidden' - the archive must still show it"


# ---------------------------------------------------------------------------
# Gate 5 - the hidden LIST is profile-scoped too. Opposite states on the two
# profiles, so this catches BOTH a dropped profile predicate and a dropped
# state predicate in `list_hidden_calls`.
# ---------------------------------------------------------------------------
def test_hidden_list_is_scoped_by_profile_and_state(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, p2), (cid,) = _seed(pg_session_factory)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="hidden")
    _decide(pg_session_factory, user_id=uid, profile_id=p2, call_id=cid, state="saved")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        r1 = client.get("/api/calls/hidden", params={"profile_id": p1})
        r2 = client.get("/api/calls/hidden", params={"profile_id": p2})
    assert r1.status_code == 200 and r2.status_code == 200
    assert [i["id"] for i in r1.json()["items"]] == [cid], "P1 hid it, so P1's list has it"
    assert [i["id"] for i in r2.json()["items"]] == [], (
        "P2 SAVED it. It must not appear in P2's hidden list - not via a missing profile "
        "predicate, and not via a missing state predicate."
    )


# ---------------------------------------------------------------------------
# Gate 5b (D9) - the hidden list REQUIRES a profile. There is no honest
# cross-profile answer: a decision belongs to a profile, and the "any profile,
# DISTINCT by call" alternative is the one shape that WIDENS.
# ---------------------------------------------------------------------------
def test_hidden_list_requires_a_profile_id(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        bare = client.get("/api/calls/hidden")
        scoped = client.get("/api/calls/hidden", params={"profile_id": p1})
    assert bare.status_code == 422, (
        f"GET /calls/hidden without profile_id answered {bare.status_code}; a decision "
        f"list with no profile has no honest row set (D9)"
    )
    assert scoped.status_code == 200
    assert [i["id"] for i in scoped.json()["items"]] == [cid], (
        "the positive control: WITH the profile the same row is listed, so the 422 above "
        "is the missing parameter and not an empty fixture"
    )


# ---------------------------------------------------------------------------
# Gate 5c - the hidden list is scoped by USER. The unique key is
# (user_id, profile_id, call_id), so a colleague can hold their own hidden row
# on the exact same call and profile; dropping `user_id` from the join reddens
# no other gate here, because no other gate seeds a second user's row.
# ---------------------------------------------------------------------------
def test_hidden_list_does_not_leak_a_strangers_hidden_row(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    stranger = _second_user(pg_session_factory, email="stranger2@umu.se")
    _decide(pg_session_factory, user_id=stranger, profile_id=p1, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        items = client.get("/api/calls/hidden", params={"profile_id": p1}).json()["items"]
    assert items == [], (
        "a stranger's hidden row on the SAME call and profile must not appear in "
        f"this user's hidden list when this user has not hidden it themselves: {items}"
    )


# ---------------------------------------------------------------------------
# Gate 6 - un-hide is scoped by profile AND by state. A delete keyed on
# (user_id, call_id) alone passes every other gate in this file.
# ---------------------------------------------------------------------------
def test_unhide_touches_neither_a_saved_row_nor_another_profiles_row(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, p2), (cid,) = _seed(pg_session_factory)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="saved")
    _decide(pg_session_factory, user_id=uid, profile_id=p2, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        r = client.delete(f"/api/calls/{cid}/hide", params={"profile_id": p1})
    assert r.status_code == 200
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {p1: "saved", p2: "hidden"}, (
        "un-hiding on P1 must delete only a HIDDEN row on P1: it may not drop P1's saved "
        "row and it may not reach across to P2"
    )


def test_unhide_removes_this_profiles_hidden_row(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, p2), (cid,) = _seed(pg_session_factory)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="hidden")
    _decide(pg_session_factory, user_id=uid, profile_id=p2, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert (
            client.delete(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
        )
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {p2: "hidden"}


# ---------------------------------------------------------------------------
# Gate 6b - un-hide is scoped by USER too, and the assertion is made on the
# STRANGER's rows directly. A `_states()` helper that filters on the caller's
# own user_id cannot see the foreign row at all, which is the hollow shape.
# ---------------------------------------------------------------------------
def test_unhide_does_not_touch_a_strangers_hidden_row_on_the_same_call_and_profile(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    stranger = _second_user(pg_session_factory, email="stranger@umu.se")
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="hidden")
    _decide(pg_session_factory, user_id=stranger, profile_id=p1, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert (
            client.delete(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
        )
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}, (
        "the acting user's own hidden row must be gone"
    )
    assert _states(pg_session_factory, user_id=stranger, call_id=cid) == {p1: "hidden"}, (
        "un-hide must be scoped by user_id - a stranger's hidden row on the SAME "
        "call and profile must survive another user's un-hide"
    )


# ---------------------------------------------------------------------------
# Gate 6c - un-hide must NOT validate the profile. hide requires a readable,
# active profile; un-hide must not, because a profile can be deactivated or
# un-shared AFTER a call was hidden on it, and the delete is already scoped to
# `user_id == user.id` -- there is nothing left for a readability check to
# guard, and adding one strands the hidden row behind a 404 forever.
# ---------------------------------------------------------------------------
def test_unhide_after_the_profile_is_deactivated_still_removes_the_row(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
    with pg_session_factory() as s:
        s.get(Profile, p1).is_active = False
        s.commit()
    with _client(app, uid) as client:
        # Self-check the premise: hide must now be REFUSED on this profile.
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 404
        r = client.delete(f"/api/calls/{cid}/hide", params={"profile_id": p1})
    assert r.status_code == 200, r.text
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}, (
        "un-hide must remove the row even though the profile is now inactive"
    )


def test_unhide_after_the_profile_is_unshared_still_removes_the_row(
    monkeypatch, pg_url, pg_session_factory
):
    """The profile starts out READABLE (default 'org' visibility) so the hide
    can succeed, then is flipped to 'private' -- unreadable to `uid`, who is
    neither its owner nor an admin nor an explicit viewer."""
    uid, _pids, (cid,) = _seed(pg_session_factory, n_profiles=1)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@umu.se")
        s.add(owner)
        s.flush()
        shared = Profile(
            name="Shared",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner.id,
        )
        s.add(shared)
        s.flush()
        shared_id = shared.id
        s.commit()
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert (
            client.post(f"/api/calls/{cid}/hide", params={"profile_id": shared_id}).status_code
            == 200
        )
    with pg_session_factory() as s:
        s.get(Profile, shared_id).visibility = "private"
        s.commit()
    with _client(app, uid) as client:
        # Self-check the "unshared" premise: hide must now be REFUSED on this
        # profile. Without this the test could be passing against a profile that
        # stayed readable the whole time.
        assert (
            client.post(f"/api/calls/{cid}/hide", params={"profile_id": shared_id}).status_code
            == 404
        ), "the premise of this test is that the profile is no longer readable"
        r = client.delete(f"/api/calls/{cid}/hide", params={"profile_id": shared_id})
    assert r.status_code == 200, r.text
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}, (
        "un-hide must remove the row even though the profile is no longer readable by this user"
    )


# ---------------------------------------------------------------------------
# Gate 8 - hiding a SAVED call flips the row and rewrites ALL THREE columns.
# `ON CONFLICT DO NOTHING` was correct when the row had one meaning; with two
# states it silently does nothing. The pre-existing row is seeded with a
# DIFFERENT decided_by and a 2020 decided_at so each column is observably moved.
# ---------------------------------------------------------------------------
def test_hiding_a_saved_call_flips_the_row_and_rewrites_three_columns(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    colleague = _second_user(pg_session_factory, email="colleague8@umu.se")
    with pg_session_factory() as s:
        s.add(
            CallDecision(
                user_id=uid,
                profile_id=p1,
                call_id=cid,
                state="saved",
                decided_by=colleague,
                decided_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
    rows = _rows(pg_session_factory, user_id=uid, call_id=cid)
    assert len(rows) == 1, f"the upsert must UPDATE the existing row, not add one: {rows}"
    (pid, state, decided_at, decided_by) = rows[0]
    assert (pid, state) == (p1, "hidden"), (
        "hide on a saved row must UPDATE it, not DO NOTHING - one row, state 'hidden'"
    )
    assert decided_by == uid, (
        f"decided_by must become the ACTOR ({uid}), not stay the colleague ({colleague})"
    )
    assert decided_at.year > 2020, (
        f"decided_at is the Saved/Hidden sort key (D12), so a flip must bump it; got "
        f"{decided_at}"
    )


# Gate 8b - re-hiding the SAME call on the SAME profile. "Still one row, still
# hidden" is true under DO NOTHING as well, so asserting only that is a byte-for-
# byte restatement of tests/test_api_calls_hide.py::test_hide_is_idempotent and
# cannot see the upsert at all. What DO UPDATE actually buys here is that a
# re-hide is a NEW decision: `decided_at` moves forward, which is what pushes the
# call back to the top of Hidden (D12 orders that list by decided_at DESC).
def test_hide_is_idempotent_within_one_profile(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
        first = _rows(pg_session_factory, user_id=uid, call_id=cid)
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 200
        second = _rows(pg_session_factory, user_id=uid, call_id=cid)
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {p1: "hidden"}
    assert len(first) == 1 and len(second) == 1, (
        f"one row per (user, profile, call) throughout: {first} -> {second}"
    )
    assert second[0][2] > first[0][2], (
        "a re-hide is a NEW decision, so `decided_at` must move forward and the call "
        "returns to the top of Hidden (D12). DO NOTHING leaves it frozen at the first "
        f"hide. Got {first[0][2]} -> {second[0][2]}"
    )


# ---------------------------------------------------------------------------
# Gate 9 - the write rule. profile_id is REQUIRED (call_decisions.profile_id is
# NOT NULL, so a hide with no profile has nowhere to go), and the profile must
# exist, be active AND be readable. BOTH dead-id flavours are asserted: a check
# that OMITS `is_active` still rejects the unknown id, so the unknown-id case
# alone cannot see it.
# ---------------------------------------------------------------------------
def test_hide_without_profile_id_is_422_and_writes_nothing(monkeypatch, pg_url, pg_session_factory):
    uid, _pids, (cid,) = _seed(pg_session_factory)
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide").status_code == 422
        assert client.delete(f"/api/calls/{cid}/hide").status_code == 422
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}


def test_hide_on_an_unreadable_or_unknown_profile_is_404_and_writes_nothing(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (_p1, _p2), (cid,) = _seed(pg_session_factory)
    with pg_session_factory() as s:
        other = User(status="active", email="other@umu.se")
        s.add(other)
        s.flush()
        secret = Profile(
            name="Secret",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=other.id,
            visibility="private",
        )
        s.add(secret)
        s.flush()
        secret_id = secret.id
        s.commit()
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        r = client.post(f"/api/calls/{cid}/hide", params={"profile_id": secret_id})
        assert r.status_code == 404, r.text
        assert (
            client.post(f"/api/calls/{cid}/hide", params={"profile_id": 999999}).status_code == 404
        )
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}, (
        "a rejected hide must write no row at all"
    )


def test_hide_on_an_inactive_profile_is_404(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (cid,) = _seed(pg_session_factory)
    with pg_session_factory() as s:
        s.get(Profile, p1).is_active = False
        s.commit()
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": p1}).status_code == 404
    assert _states(pg_session_factory, user_id=uid, call_id=cid) == {}


# ---------------------------------------------------------------------------
# Gate 11 - the no-profile archive is the ANY rule, verbatim today's global
# behaviour.
# ---------------------------------------------------------------------------
def test_no_profile_archive_suppresses_a_call_hidden_on_any_profile(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, _p2), (cid, _other) = _seed(pg_session_factory, n_calls=2)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=cid, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        titles = _titles(client.get("/api/calls").json())
    assert "DecisionCall0" not in titles, (
        "no profile -> ANY: one hide anywhere suppresses the call in the unranked archive"
    )
    assert "DecisionCall1" in titles


# ---------------------------------------------------------------------------
# Gate 11b (D8) - a decision view with NO effective profile is EMPTY, never the
# cross-profile union. The archive's "degrade to ANY profile" rule WIDENS when
# reused for saved/hidden. The 200 (not 422) is load-bearing: the match feed
# reads `effective.profile_id: null` off this response to show its onboarding
# screen.
# ---------------------------------------------------------------------------
def test_no_profile_saved_and_hidden_are_empty_not_the_union(
    monkeypatch, pg_url, pg_session_factory
):
    uid, (p1, p2), (c_saved, c_hidden) = _seed(pg_session_factory, n_calls=2)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_saved, state="saved")
    _decide(pg_session_factory, user_id=uid, profile_id=p2, call_id=c_hidden, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        for view in ("saved", "hidden"):
            r = client.get("/api/calls", params={"view": view})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["effective"]["profile_id"] is None, (
                "self-check: this user has no stored default, so there must be no "
                "effective profile for the union rule to widen over"
            )
            assert body["items"] == [], (
                f"?view={view} with no profile must be EMPTY, not the ANY-union: "
                f"{_titles(body)}"
            )
            assert body["pagination"]["total"] == 0, (
                f"?view={view} pagination.total must agree with the empty page; got "
                f"{body['pagination']['total']}"
            )
        # Positive control: WITH the profile each view returns its own row, so
        # the empties above are the no-profile rule and not an empty fixture.
        saved_p1 = client.get("/api/calls", params={"view": "saved", "profile": p1}).json()
        hidden_p2 = client.get("/api/calls", params={"view": "hidden", "profile": p2}).json()
        assert _titles(saved_p1) == ["DecisionCall0"]
        assert _titles(hidden_p2) == ["DecisionCall1"]


# ---------------------------------------------------------------------------
# Gate 10 - the `view` parameter. `archive` is today's behaviour under a name,
# so an omitted parameter and ?view=archive must agree.
# ---------------------------------------------------------------------------
def test_the_four_views_partition_the_decisions(monkeypatch, pg_url, pg_session_factory):
    uid, (p1, _p2), (c_hidden, c_saved, c_undecided) = _seed(pg_session_factory, n_calls=3)
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_hidden, state="hidden")
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_saved, state="saved")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:

        def ids(view=None):
            params = {"profile": p1}
            if view is not None:
                params["view"] = view
            return sorted(i["id"] for i in client.get("/api/calls", params=params).json()["items"])

        assert ids() == sorted([c_saved, c_undecided]), "archive = NOT hidden"
        assert ids("archive") == ids(), "an omitted view must be exactly ?view=archive"
        assert ids("inbox") == [c_undecided], "inbox = no decision at all"
        assert ids("saved") == [c_saved]
        assert ids("hidden") == [c_hidden]
    with _client(app, uid) as client:
        assert client.get("/api/calls", params={"view": "nonsense"}).status_code == 422


# ---------------------------------------------------------------------------
# Gate 12 (D1) - Saved and Hidden list DECISIONS, so a closed call stays in
# them; the inbox and the archive still hide closed calls by default. Four
# calls, three with a 2020 deadline.
# ---------------------------------------------------------------------------
def test_saved_and_hidden_keep_closed_calls_but_the_inbox_does_not(
    monkeypatch, pg_url, pg_session_factory
):
    past = datetime(2020, 1, 1, tzinfo=UTC)
    uid, (p1, _p2), (c_open_saved, c_closed_saved, c_closed_hidden, c_closed_undecided) = _seed(
        pg_session_factory, n_calls=4, deadlines=[None, past, past, past]
    )
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_open_saved, state="saved")
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_closed_saved, state="saved")
    _decide(pg_session_factory, user_id=uid, profile_id=p1, call_id=c_closed_hidden, state="hidden")
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:

        def body(view, **extra):
            return client.get(
                "/api/calls", params={"profile": p1, "view": view, **extra}
            ).json()

        saved = body("saved")
        assert sorted(i["id"] for i in saved["items"]) == sorted(
            [c_open_saved, c_closed_saved]
        ), (
            "Saved lists what the user saved. A closed call must not drop out of it - "
            f"that is exactly when they go looking for it. Got {_titles(saved)}"
        )
        assert saved["pagination"]["total"] == 2, (
            f"pagination.total must not apply open_expr either; got "
            f"{saved['pagination']['total']}"
        )
        hidden = body("hidden")
        assert [i["id"] for i in hidden["items"]] == [c_closed_hidden], (
            f"a hidden call that closed is still a hidden call. Got {_titles(hidden)}"
        )
        inbox = body("inbox")
        assert inbox["items"] == [], (
            f"the only undecided call is CLOSED, so the inbox is empty by default. Got "
            f"{_titles(inbox)}"
        )
        with_closed = body("inbox", show_closed="true")
        assert [i["id"] for i in with_closed["items"]] == [c_closed_undecided], (
            "show_closed=true still governs the inbox, so the closed undecided call "
            f"comes back. Got {_titles(with_closed)}"
        )


# ---------------------------------------------------------------------------
# Gate 13 (D12) - Saved is a log of what you just did: ordered by decided_at
# DESC and by NOTHING ELSE. THREE calls, because a two-row fixture whose
# expected order happens to equal ascending id cannot tell "ordered by
# decided_at" from "not ordered at all" - the heap returns insertion order for
# a table that small, so DELETING the order_by stays green.
#
# The expectation below disagrees with every ordering the code could fall back
# to at once: id ASC (= insertion/heap order), id DESC, published_at DESC, and
# the ranked branch's `closed_expr.asc()` prefix. That last one is why the
# fixture carries a CLOSED call (deadline 2020) saved MOST recently: it must
# come FIRST, so re-adding "open before closed" ahead of decided_at - the shape
# every other view uses - reddens this gate instead of passing unnoticed.
# ---------------------------------------------------------------------------
def test_saved_is_ordered_most_recent_decision_first(monkeypatch, pg_url, pg_session_factory):
    past = datetime(2020, 1, 1, tzinfo=UTC)
    uid, (p1, _p2), (c_pub_first, c_closed, c_pub_last) = _seed(
        pg_session_factory, n_calls=3, deadlines=[None, past, None]
    )
    # `_seed` gives ascending ids AND ascending published_at, so:
    #   id ASC / heap    -> [c_pub_first, c_closed, c_pub_last]
    #   id DESC          -> [c_pub_last, c_closed, c_pub_first]
    #   published_at DESC-> [c_pub_last, c_closed, c_pub_first]
    #   closed-last      -> [c_pub_last, c_pub_first, c_closed]
    #   decided_at DESC  -> [c_closed, c_pub_last, c_pub_first]   <- the only one
    # The rows are INSERTED in ascending call-id order so that the decision
    # table's own heap order is the id-ASC one too, not the expectation.
    with pg_session_factory() as s:
        for call_id, when in (
            (c_pub_first, "2026-04-01 00:00:00+00"),
            (c_closed, "2026-06-01 00:00:00+00"),
            (c_pub_last, "2026-05-01 00:00:00+00"),
        ):
            s.execute(
                text(
                    "INSERT INTO call_decisions "
                    "  (user_id, profile_id, call_id, state, decided_at, decided_by) "
                    "VALUES (:u, :p, :c, 'saved', :t, :u)"
                ),
                {"u": uid, "p": p1, "c": call_id, "t": when},
            )
        s.commit()
    app = _app(monkeypatch, pg_url)
    with _client(app, uid) as client:
        body = client.get("/api/calls", params={"profile": p1, "view": "saved"}).json()
    assert [i["id"] for i in body["items"]] == [c_closed, c_pub_last, c_pub_first], (
        "Saved is ordered decided_at DESC (D12). The call saved on 2026-06-01 comes "
        "first even though it is CLOSED and was published before one of the others - "
        "so neither the inbox's closed-LAST/published-desc order nor the table's "
        f"insertion order can pass this. Got {_titles(body)}, expected "
        f"{[c_closed, c_pub_last, c_pub_first]}"
    )
