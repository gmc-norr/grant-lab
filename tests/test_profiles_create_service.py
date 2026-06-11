import pytest

from call_manager.db.models import Profile, ProfileBootstrapJob, Subscription, User
from call_manager.profiles.create import create_profile_with_bootstrap, merge_bootstrap_into_profile
from call_manager.profiles.seed import DraftProfile


class _P:
    def __init__(self, kind, description, structured):
        self.kind, self.description, self.structured = kind, description, structured


def test_merge_fills_empty_and_keeps_user_values():
    p = _P("organizational_unit", "", {"areas": ["oncology"], "must_include": ["x"]})
    draft = DraftProfile(
        description="Drafted.",
        structured={
            "areas": ["genomics"],
            "must_include": ["y"],
            "budget_min": 100000,
            "trl_max": 7,
            "hard_exclusions": ["defence"],
            "geo": {"country": "Sweden"},
        },
        publications_fetched=2,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "Drafted."
    assert p.structured["areas"] == ["oncology"]  # user wins
    assert p.structured["must_include"] == ["x"]  # user wins
    assert p.structured["budget_min"] == 100000  # filled from draft (rubric scalar)
    assert p.structured["trl_max"] == 7  # filled from draft
    assert p.structured["hard_exclusions"] == ["defence"]  # filled from draft (rubric list)
    assert p.structured["geo"]["country"] == "Sweden"  # filled from draft namespace (sanitized)


def test_kind_inappropriate_namespace_field_is_dropped():
    # career_stage is individual-only; on an organizational_unit it must NOT be persisted.
    p = _P("organizational_unit", "", {"areas": []})
    draft = DraftProfile(
        description="D.",
        structured={"eligibility": {"career_stage": "senior"}},
        publications_fetched=0,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert "career_stage" not in p.structured.get("eligibility", {})


def test_merge_does_not_overwrite_nonempty_description():
    p = _P("organizational_unit", "User wrote this.", {"areas": []})
    draft = DraftProfile(
        description="LLM draft.",
        structured={"areas": ["x"]},
        publications_fetched=0,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "User wrote this."
    assert p.structured["areas"] == ["x"]


def test_create_atomic_success(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="c@ki.se")
        s.add(u)
        s.flush()
        p = create_profile_with_bootstrap(
            session=s,
            owner_id=u.id,
            name="Grp",
            kind="organizational_unit",
            level="group",
            structured={"areas": ["oncology"]},
            seed_payload={"name": "Grp", "members": [], "mission_text": "m"},
        )
        s.commit()
        pid, uid = p.id, u.id
    with pg_session_factory() as s:
        assert s.get(Profile, pid).description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid, status="queued").count() == 1
        assert s.get(Subscription, (uid, pid)) is not None
        assert s.get(User, uid).default_profile_id == pid


def test_create_rolls_back_on_failure(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="c2@ki.se")
        s.add(u)
        s.flush()
        with pytest.raises(Exception):  # noqa: B017
            create_profile_with_bootstrap(
                session=s,
                owner_id=10_000_000,
                name="Bad",
                kind="organizational_unit",
                level="group",
                structured={},
                seed_payload={"name": "Bad"},
            )
            s.commit()
        s.rollback()
    with pg_session_factory() as s:
        assert s.query(Profile).filter_by(name="Bad").count() == 0


def test_merge_drops_overlong_areas_item():
    from call_manager.profiles.fields import MAX_TAG_LEN

    overlong = "x" * (MAX_TAG_LEN + 5)
    p = _P("individual", "", {"areas": []})
    draft = DraftProfile(
        description="D.",
        structured={"areas": ["valid area", overlong]},
        publications_fetched=0,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.structured["areas"] == ["valid area"]
    assert all(len(a) <= MAX_TAG_LEN for a in p.structured["areas"])


def test_existing_default_is_preserved(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="c3@ki.se")
        s.add(u)
        s.flush()
        first = create_profile_with_bootstrap(
            session=s,
            owner_id=u.id,
            name="First",
            kind="organizational_unit",
            level="group",
            structured={},
            seed_payload={"name": "First"},
        )
        s.commit()
        first_id, uid = first.id, u.id
    with pg_session_factory() as s:
        create_profile_with_bootstrap(
            session=s,
            owner_id=uid,
            name="Second",
            kind="organizational_unit",
            level="group",
            structured={},
            seed_payload={"name": "Second"},
        )
        s.commit()
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == first_id  # fill-if-null: not changed
