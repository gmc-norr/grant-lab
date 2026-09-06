import json

from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.profiles.fingerprint import (
    FINGERPRINT_VERSION,
    embedding_fingerprint,
    rank_fingerprint,
)


def _profile(**kw):
    # kind MUST be one of the two ck_profiles_kind allows. "research_group"
    # passes the seven in-memory tests (no flush) and then dies with
    # IntegrityError in the one round-trip test that matters.
    base = dict(
        name="Hepatology",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="Liver disease",
        structured={},
        budget_currency="SEK",
        created_by=1,
    )
    base.update(kw)
    return Profile(**base)


def test_both_fingerprints_carry_the_version_tag():
    p = _profile()
    assert embedding_fingerprint(p).startswith(f"{FINGERPRINT_VERSION}:")
    assert rank_fingerprint(p).startswith(f"{FINGERPRINT_VERSION}:")


def test_embedding_fingerprint_tracks_the_text_builder_output():
    """Hashing build_profile_text's OUTPUT, not its inputs, is the point:
    editing that function must invalidate every vector automatically."""
    a = _profile(description="Liver disease")
    b = _profile(description="Liver  disease")  # different text, same fields
    assert embedding_fingerprint(a) != embedding_fingerprint(b)


def test_rank_fingerprint_covers_budget_and_trl():
    """Rev 1's enumeration omitted these; criteria.py:111 and :129 read them
    straight off `structured`. Hashing the whole blob is why this passes."""
    a = _profile(structured={"areas": ["liver"], "budget_min": 100})
    b = _profile(structured={"areas": ["liver"], "budget_min": 200})
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_rank_fingerprint_covers_the_embedding_fingerprint():
    """Catches a build_profile_text edit, which changes no text field."""
    a = _profile()
    a.embedding_fingerprint = "v1:aaa"
    b = _profile()
    b.embedding_fingerprint = "v1:bbb"
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_key_order_does_not_change_the_fingerprint():
    a = _profile(structured={"zebra": 1, "apple": 2})
    b = _profile(structured={"apple": 2, "zebra": 1})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_integral_floats_and_ints_hash_the_same():
    """json.dumps(1) != json.dumps(1.0). Postgres may hand back either.
    Without canonicalization this alone re-ranks the corpus daily."""
    a = _profile(structured={"budget_min": 1000})
    b = _profile(structured={"budget_min": 1000.0})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_empty_collections_are_omitted():
    a = _profile(structured={"areas": ["liver"], "must_include": []})
    b = _profile(structured={"areas": ["liver"]})
    assert rank_fingerprint(a) == rank_fingerprint(b)


def test_survives_a_real_postgres_round_trip(pg_session_factory):
    """GATE 1. An unstable fingerprint marks every profile permanently stale
    and re-ranks the whole corpus on every daily tick, presenting as
    unexplained GPU load rather than as an error.

    `endowment` is load-bearing. `1000.0` does NOT change across the trip --
    measured: Postgres hands back exactly what json.dumps wrote, and
    `json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)`
    is True for a fixture without it. Only a float json.dumps writes in
    EXPONENT form flips: Postgres numeric normalizes 1e+16 to
    10000000000000000 and json.loads reads that back as an INT. Without
    `endowment` this gate exercises nothing but sort_keys=True, and stays
    GREEN with the whole numeric rule deleted.
    """
    structured = {
        "zebra": {"nested_z": 1, "nested_a": 2},
        "apple": [3, 2, 1],
        "budget_min": 1000.0,
        "endowment": 1e16,
        "must_include": [],
    }
    with pg_session_factory() as session:
        u = User(email="fp-roundtrip@umu.se", status="active")
        session.add(u)
        session.flush()
        p = _profile(structured=structured, created_by=u.id)
        before = rank_fingerprint(p)
        raw_before = json.dumps(p.structured, sort_keys=False)

        session.add(p)
        session.commit()
        pid = p.id
        session.expunge_all()

        loaded = session.get(Profile, pid)
        raw_after = json.dumps(loaded.structured, sort_keys=False)

        # PRECONDITION: if these are already identical, this test proves nothing.
        assert raw_before != raw_after, (
            "fixture too simple: the round trip changed nothing, so this test "
            "cannot detect an unstable canonicalizer"
        )

        assert rank_fingerprint(loaded) == before
