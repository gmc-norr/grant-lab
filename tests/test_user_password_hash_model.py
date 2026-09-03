from call_manager.db.models import User


def test_password_hash_defaults_none_and_persists(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="pw@umu.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.password_hash is None
        u.password_hash = "argon2-hash-here"
        s.commit()
        s.refresh(u)
        assert u.password_hash == "argon2-hash-here"
