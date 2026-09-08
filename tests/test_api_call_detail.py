from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    Source,
    User,
)
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, email="u@umu.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call(pg_session_factory, *, title="CD"):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=title,
            url="https://x",
            title=title,
            summary="A summary.",
            published_at=datetime.now(UTC),
            keywords=["genomics", "oncology"],
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _seed_profile(pg_session_factory, *, owner_id, active=True, name="P"):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            is_active=active,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _seed_ranking(pg_session_factory, *, cid, pid, score=72, sim=0.42):
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=pid,
                aggregate_score=score,
                model="claude-sonnet-4-6",
                prompt_version="v1",
                embed_similarity=sim,
                rationale="Strong fit.",
            )
        )
        s.add(
            CallRankingCriterion(
                call_id=cid, profile_id=pid, criterion="embedding", rating="pass"
            )
        )
        s.commit()


def test_detail_no_profile_returns_call_without_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["call"]["id"] == cid
    assert body["call"]["keywords"] == ["genomics", "oncology"]
    assert body["call"]["summary"] == "A summary."
    assert body["ranking"] is None
    assert body["criteria"] == []
    assert body["best_profile_id"] is None


def test_detail_active_profile_returns_ranking_and_why(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ranking"]["aggregate_score"] == 72
    assert body["ranking"]["model_label"] == "Sonnet 4.6"
    emb = next(c for c in body["criteria"] if c["criterion"] == "embedding")
    assert emb["detail"] == "Cosine similarity 0.420 (pass threshold 0.30)."
    assert emb["is_reason_code"] is True
    assert body["best_profile_id"] == pid


def test_detail_active_not_owned_profile_still_shows_ranking(
    monkeypatch, pg_url, pg_session_factory
):
    # P1b parity: ranking display keys off is_active ALONE, not ownership.
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    other_uid = _login(app, pg_session_factory, email="other@umu.se")[1]
    pid = _seed_profile(pg_session_factory, owner_id=other_uid, name="Other")
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    assert other_uid != uid
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"]["aggregate_score"] == 72
    # ...but best_profile_id (ownership-gated) stays None for the logged-in user.
    assert r.json()["best_profile_id"] is None


def test_detail_inactive_profile_no_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid, active=False)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_empty_profile_is_none(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_returns_the_callers_own_decision_note(monkeypatch, pg_url, pg_session_factory):
    """The page must be able to SHOW the note before it offers to replace it.

    Re-hiding an already-hidden call is `hidden -> hidden`, which the upsert's
    CASE deliberately PRESERVES the note through
    (`test_re_hiding_an_already_hidden_call_keeps_the_note`). So the strip this
    page opens on that path sits over an existing reason, and without this field
    it opens empty and one Enter overwrites it unseen.
    """
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid}).status_code == 200
        assert (
            client.put(
                f"/api/calls/{cid}/note",
                params={"profile_id": pid},
                json={"note": "not eligible -- materials science"},
            ).status_code
            == 200
        )
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["call"]["decision_note"] == "not eligible -- materials science"


def test_detail_note_is_null_without_a_profile(monkeypatch, pg_url, pg_session_factory):
    """`?profile=` is OPTIONAL here (unlike every decision route), and a decision
    row cannot exist without one. The POPULATED control matters: the same call
    carries a note under `?profile=N` in the test above, so this is the profile
    scope doing the work, not an empty database."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "mine"})
        with_profile = client.get(f"/api/calls/{cid}?profile={pid}")
        no_profile = client.get(f"/api/calls/{cid}")
        empty_profile = client.get(f"/api/calls/{cid}?profile=")
    assert with_profile.json()["call"]["decision_note"] == "mine"
    assert no_profile.json()["call"]["decision_note"] is None
    assert empty_profile.json()["call"]["decision_note"] is None


def test_detail_never_shows_another_users_note(monkeypatch, pg_url, pg_session_factory):
    """The detail half of `test_another_users_note_is_invisible`. Must reject a
    query keyed on (profile_id, call_id) alone -- the natural mistake, because
    the golden-set miner IS keyed that way, and because both users here read the
    SAME profile, so `can_read_profile` passing is not the scope."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    # A SECOND user's decision on the SAME (profile, call), written straight
    # through the session: there is no endpoint for "act as someone else".
    with pg_session_factory() as s:
        other = User(status="active", email="colleague@umu.se")
        s.add(other)
        s.flush()
        s.add(
            CallDecision(
                user_id=other.id,
                profile_id=pid,
                call_id=cid,
                state="hidden",
                note="THEIRS",
                decided_by=other.id,
            )
        )
        s.commit()
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    # No row of the caller's own -> null, not the colleague's text.
    assert r.json()["call"]["decision_note"] is None
    assert "THEIRS" not in r.text


def test_detail_note_needs_a_readable_active_profile(monkeypatch, pg_url, pg_session_factory):
    """An inactive profile degrades to the unranked view (`ranking is None`), and
    the note degrades with it -- the field is read INSIDE that same branch, so it
    cannot outlive the profile resolution that gates every other per-profile
    value on this page."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "mine"})
        assert client.get(f"/api/calls/{cid}?profile={pid}").json()["call"]["decision_note"] == (
            "mine"
        )
    with pg_session_factory() as s:
        s.get(Profile, pid).is_active = False
        s.commit()
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"] is None
    assert r.json()["call"]["decision_note"] is None


def test_detail_bad_profile_400(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=abc")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_detail_missing_call_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/calls/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_detail_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 401


def test_detail_returns_the_callers_own_decision_state(monkeypatch, pg_url, pg_session_factory):
    """PD-31b. The page can SHOW a note but not know it is looking at a hidden
    call, because it learns that only from its own session state -- so after a
    RELOAD it reads "Hide this call", offers no Undo, and the note it now
    receives has nowhere to render. `decision_note` without `decision_state` is
    half a fix.
    """
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        assert client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid}).status_code == 200
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["call"]["decision_state"] == "hidden"


def test_detail_decision_state_reports_saved_too(monkeypatch, pg_url, pg_session_factory):
    """Not hard-coded to "hidden". A saved call carries a note as well (the
    strip is offered on `s`), and a field that only ever answers one value
    cannot tell the page which decision it is annotating."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        assert client.post(f"/api/calls/{cid}/save", params={"profile_id": pid}).status_code == 200
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.json()["call"]["decision_state"] == "saved"


def test_detail_decision_state_is_null_when_undecided(monkeypatch, pg_url, pg_session_factory):
    """Undecided is the ABSENCE of a row. The POPULATED control is the two tests
    above: the same fixture answers "hidden" and "saved", so this null is the
    missing row, not an empty database."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.json()["call"]["decision_state"] is None


def test_detail_never_shows_another_users_decision_state(
    monkeypatch, pg_url, pg_session_factory
):
    """Same three-column scope as the note. `user_id` is the predicate a
    copy-paste from the golden-set miner would drop -- the miner omits it on
    purpose -- and dropping it here would show a colleague's decision as the
    caller's own."""
    from grant_lab.db.models import CallDecision, User

    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    with pg_session_factory() as s:
        other = User(status="active", email="colleague-state@umu.se")
        s.add(other)
        s.flush()
        s.add(
            CallDecision(
                user_id=other.id,
                profile_id=pid,
                call_id=cid,
                state="hidden",
                note="THEIRS",
                decided_by=other.id,
            )
        )
        s.commit()
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.json()["call"]["decision_state"] is None
    assert "THEIRS" not in r.text
