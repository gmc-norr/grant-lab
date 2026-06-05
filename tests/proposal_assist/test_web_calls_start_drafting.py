def test_call_detail_includes_best_profile_id_when_ranking_exists(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    from call_manager.db.models import Call, CallRanking, Profile, Source

    with pg_session_factory() as s:
        src = Source(slug="t", name="T")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call)
        s.flush()
        prof = Profile(
            name="P", description="d", kind="organizational_unit", created_by=current_user.id
        )
        s.add(prof)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=prof.id,
                aggregate_score=85,
                rationale="r",
                model="m",
                prompt_version="v",
            )
        )
        s.commit()
        cid, pid = call.id, prof.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert response.status_code == 200
    assert f"/workspaces/new?call_id={cid}&profile_id={pid}".encode() in response.content


def test_call_detail_falls_back_to_no_profile_id_when_no_ranking(
    client_authed_user,
    pg_session_factory,
):
    from call_manager.db.models import Call, Source

    with pg_session_factory() as s:
        src = Source(slug="t", name="T")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call)
        s.commit()
        cid = call.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert response.status_code == 200
    # Either: only ?call_id=N (no &profile_id), OR no Start drafting link at all if
    # the call has no rankings for the user. We just want the bare call_id form
    # — not a URL with profile_id.
    assert f"/workspaces/new?call_id={cid}".encode() in response.content
    # Confirm there's no profile_id query string immediately following.
    assert f"/workspaces/new?call_id={cid}&profile_id=".encode() not in response.content


def test_call_detail_picks_highest_scoring_profile_among_users_profiles(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """If multiple of the user's profiles have ranked this call, the
    one with the highest aggregate_score is used."""
    from call_manager.db.models import Call, CallRanking, Profile, Source

    with pg_session_factory() as s:
        src = Source(slug="t", name="T")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call)
        s.flush()
        low = Profile(
            name="Low", description="d", kind="organizational_unit", created_by=current_user.id
        )
        high = Profile(
            name="High", description="d", kind="organizational_unit", created_by=current_user.id
        )
        s.add_all([low, high])
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=low.id,
                aggregate_score=30,
                rationale="r",
                model="m",
                prompt_version="v",
            )
        )
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=high.id,
                aggregate_score=90,
                rationale="r",
                model="m",
                prompt_version="v",
            )
        )
        s.commit()
        cid, hpid = call.id, high.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert f"profile_id={hpid}".encode() in response.content


def test_call_detail_excludes_other_users_profiles(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A ranking against another user's profile must NOT pre-fill the link."""
    from call_manager.db.models import Call, CallRanking, Profile, Source
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        other = make_user(s, email="other@ki.se")
        src = Source(slug="t", name="T")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call)
        s.flush()
        their_prof = Profile(
            name="Theirs", description="d", kind="organizational_unit", created_by=other.id
        )
        s.add(their_prof)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=their_prof.id,
                aggregate_score=99,
                rationale="r",
                model="m",
                prompt_version="v",
            )
        )
        s.commit()
        cid, opid = call.id, their_prof.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert f"profile_id={opid}".encode() not in response.content
