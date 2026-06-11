import uuid

from sqlalchemy import inspect, text

from call_manager.db.models import Profile, ProfileBootstrapJob, ProfileKind, User


def _mk_profile(session):
    u = User(email="creator@ki.se")
    session.add(u)
    session.flush()
    p = Profile(
        name="Grp",
        kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
        level="group",
        description="",
        structured={},
        created_by=u.id,
    )
    session.add(p)
    session.flush()
    return p.id, u.id


def test_bootstrap_table_and_default_column_exist(migrated_engine):
    insp = inspect(migrated_engine)
    assert "profile_bootstrap_jobs" in insp.get_table_names()
    assert "default_profile_id" in {c["name"] for c in insp.get_columns("users")}


def test_bootstrap_job_unique_per_profile(pg_session_factory):
    with pg_session_factory() as s:
        pid, _ = _mk_profile(s)
        s.add(ProfileBootstrapJob(profile_id=pid, status="queued", seed={"name": "Grp"}))
        s.commit()
        dup = s.execute(
            text(
                "INSERT INTO profile_bootstrap_jobs (id, profile_id, status) "
                "VALUES (:i, :p, 'queued') ON CONFLICT DO NOTHING RETURNING id"
            ),
            {"i": str(uuid.uuid4()), "p": pid},
        ).first()
        assert dup is None


def test_default_profile_id_set_null_on_profile_delete(pg_session_factory):
    with pg_session_factory() as s:
        pid, uid = _mk_profile(s)
        s.get(User, uid).default_profile_id = pid
        s.commit()
        s.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
        s.commit()
        s.expire_all()
        assert s.get(User, uid).default_profile_id is None
