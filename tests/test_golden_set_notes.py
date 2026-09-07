"""Notes reach the golden-set candidate list, unattributed (§7 G7, G12).

G12 is an HTTP round trip on purpose. `CandidateOut(**asdict(c))` builds a
Pydantic model, and Pydantic v2 IGNORES unknown keys by default -- so a field
added only to the CandidateRow dataclass is dropped with no error, no warning
and an absent key. A miner unit test cannot see that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from grant_lab.ranking.golden_set import candidates_dismissed_high_score

_CAP = 3


def _admin_client(app, pg_session_factory):
    """An ADMIN session. `_login` in test_api_calls_hide.py seeds a plain user,
    and /api/admin/golden-set is behind require_admin -- so this seeds its own
    rather than UPDATE-ing someone into an admin after the fact."""
    from fastapi.testclient import TestClient

    with pg_session_factory() as s:
        u = User(status="active", email="gsadmin@umu.se", is_admin=True)
        s.add(u)
        s.commit()
        uid = u.id
    client = TestClient(app)
    client.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return client


def _seed_pair(session, *, n_hidden_notes: int, n_saved_notes: int):
    """One call ranked 80 for one profile, hidden by `n_hidden_notes` users who
    each wrote a note, and saved-with-a-note by `n_saved_notes` others."""
    src = session.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    owner = User(status="active", email="gsowner@umu.se")
    session.add(owner)
    session.flush()
    prof = Profile(
        name="Group GS",
        # `.value`, matching tests/test_api_calls_hide.py's _login.
        kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
        level="group",
        description="x",
        structured={},
        created_by=owner.id,
    )
    # Same shape as _seed_call in tests/test_api_calls_hide.py: `fetched_at`
    # takes its column default, `published_at` is the one set explicitly.
    call = Call(
        source_id=src.id,
        external_id="gs-1",
        url="https://example.org/gs-1",
        title="GS Call",
        published_at=datetime.now(UTC),
    )
    session.add_all([prof, call])
    session.flush()
    session.add(
        CallRanking(
            call_id=call.id,
            profile_id=prof.id,
            aggregate_score=80,
            rationale="",
            model="m",
            prompt_version="1",
            raw_response={},
            ranked_at=datetime.now(UTC),
        )
    )
    base = datetime.now(UTC)
    for i in range(n_hidden_notes):
        u = User(status="active", email=f"hider{i}@umu.se")
        session.add(u)
        session.flush()
        session.add(
            CallDecision(
                user_id=u.id,
                profile_id=prof.id,
                call_id=call.id,
                state="hidden",
                note=f"hidden-{i}",
                # Ascending, so hidden-(n-1) is the NEWEST decision.
                decided_at=base + timedelta(minutes=i),
                decided_by=u.id,
            )
        )
    for i in range(n_saved_notes):
        u = User(status="active", email=f"saver{i}@umu.se")
        session.add(u)
        session.flush()
        session.add(
            CallDecision(
                user_id=u.id,
                profile_id=prof.id,
                call_id=call.id,
                state="saved",
                note=f"SAVED-{i}",
                decided_at=base + timedelta(hours=1, minutes=i),
                decided_by=u.id,
            )
        )
    session.commit()
    return call.id, prof.id


def test_candidate_carries_the_hidden_notes_newest_first(pg_session_factory):
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=2, n_saved_notes=0)
        rows = candidates_dismissed_high_score(s)
    assert len(rows) == 1
    assert rows[0].user_notes == ["hidden-1", "hidden-0"]


def test_saved_notes_are_excluded_and_the_filter_precedes_the_cap(pg_session_factory):
    """G7. FIVE saved notes against ONE hidden one: with fewer notes than the
    cap, filtering after the cap gives the same answer as filtering before it,
    so the seed must EXCEED the cap or the gate proves nothing. The saved notes
    are also the NEWEST, so a cap applied first would evict the hidden one."""
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=5)
        rows = candidates_dismissed_high_score(s)
    assert rows[0].user_notes == ["hidden-0"]


def test_notes_are_capped_at_three(pg_session_factory):
    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=5, n_saved_notes=0)
        rows = candidates_dismissed_high_score(s)
    assert rows[0].user_notes == ["hidden-4", "hidden-3", "hidden-2"]
    assert len(rows[0].user_notes) == _CAP


def test_the_http_response_carries_user_notes(monkeypatch, pg_url, pg_session_factory):
    """G12. The serialization boundary, not the miner."""
    from tests.test_api_calls_hide import _setup

    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=0)
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/api/admin/golden-set")
    assert r.status_code == 200
    cands = r.json()["candidates"]
    hit = [c for c in cands if c["reason"] == "dismissed_high_score"]
    assert hit, cands
    assert hit[0]["user_notes"] == ["hidden-0"]


def test_the_candidate_response_carries_no_author_metadata(
    monkeypatch, pg_url, pg_session_factory
):
    """G6, D3. Fields, deliberately. This gate does not and cannot assert that
    free text is anonymous -- only that the system attaches no author."""
    from tests.test_api_calls_hide import _setup

    with pg_session_factory() as s:
        _seed_pair(s, n_hidden_notes=1, n_saved_notes=0)
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/api/admin/golden-set")
    for c in r.json()["candidates"]:
        for banned in ("user_id", "email", "name", "decided_by"):
            assert banned not in c, (banned, c)
    assert "@umu.se" not in r.text


def test_a_busy_page_does_not_starve_the_oldest_candidates_notes(pg_session_factory):
    """The cap must be PER PAIR, not a page-wide budget.

    A page-wide `.limit(_CANDIDATE_LIMIT * _NOTE_CAP * 10)` over a global
    `decided_at DESC` was the obvious way to bound this read, and it is
    silently wrong: the budget is spent newest-first across ALL pairs, which
    is unrelated to the page's own `aggregate_score DESC` ordering. Once the
    page's annotated hidden decisions exceed it, the candidate whose hides are
    OLDEST returns [] -- and that is the candidate whose reason a labeller can
    least reconstruct.

    Reproduced here at the shape that actually reaches it: not one
    pathological pair, but ordinary use by enough readers. 32 readers each
    annotating 19 of the 20 candidates is 611 rows against a 600 budget.
    The 20th candidate is the highest-scoring one and its hides are the
    oldest, so it is first on the page and last in the budget.

    Verified 2026-09-07 against the page-wide LIMIT: this test fails with
    `assert [] == ['old-0', ...]`, and passes with the window.
    """
    n_readers = 32
    n_new_calls = 19
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        owner = User(status="active", email="starveowner@umu.se")
        s.add(owner)
        s.flush()
        prof = Profile(
            name="Group Starve",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="x",
            structured={},
            created_by=owner.id,
        )
        s.add(prof)
        s.flush()
        readers = []
        for i in range(n_readers):
            u = User(status="active", email=f"starve{i}@umu.se")
            s.add(u)
            readers.append(u)
        s.flush()

        base = datetime.now(UTC)

        def _seed(external_id: str, score: int, when, note_prefix: str, n: int) -> int:
            call = Call(
                source_id=src.id,
                external_id=external_id,
                url=f"https://example.org/{external_id}",
                title=external_id,
                published_at=base,
            )
            s.add(call)
            s.flush()
            s.add(
                CallRanking(
                    call_id=call.id,
                    profile_id=prof.id,
                    aggregate_score=score,
                    rationale="",
                    model="m",
                    prompt_version="1",
                    raw_response={},
                    ranked_at=base,
                )
            )
            for j in range(n):
                s.add(
                    CallDecision(
                        user_id=readers[j].id,
                        profile_id=prof.id,
                        call_id=call.id,
                        state="hidden",
                        note=f"{note_prefix}-{j}",
                        decided_at=when + timedelta(seconds=j),
                        decided_by=readers[j].id,
                    )
                )
            return call.id

        # The victim: highest score, so FIRST on the page -- and oldest hides,
        # so LAST in a page-wide newest-first budget.
        old = base - timedelta(days=200)
        victim = _seed("starve-victim", 99, old, "old", 3)
        # 19 more candidates, each annotated by all 32 readers, all recent.
        for k in range(n_new_calls):
            _seed(f"starve-new-{k}", 61 + k, base, f"new{k}", n_readers)
        s.commit()

        rows = candidates_dismissed_high_score(s)

    hit = [r for r in rows if r.call_id == victim]
    assert hit, "the victim must be ON the page, or this gate is vacuous"
    # 32 readers * 19 calls = 608 newer rows, above the 600-row page budget the
    # window replaces.
    assert hit[0].user_notes == ["old-2", "old-1", "old-0"], (
        "the top-scoring candidate lost its notes to a page-wide budget spent "
        "on newer decisions for lower-scoring candidates"
    )
