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

    # The pair above is NOT enough on its own: it differs only in
    # `description`, so `_digest(profile.description or "")` -- an
    # implementation that never calls build_profile_text at all -- passes it
    # identically. Measured: that mutant left this whole file green. So pin
    # the OTHER fields the builder folds in (similarity.py:88-97) with
    # `description` held fixed.
    #
    # `areas` is the commonest real profile edit. If it stopped reaching the
    # text, editing it would leave the stored vector marked current forever
    # and the vector-staleness check would never fire.
    areas_a = _profile(description="Liver disease", structured={"areas": ["liver cancer"]})
    areas_b = _profile(description="Liver disease", structured={"areas": ["transplant rejection"]})
    assert embedding_fingerprint(areas_a) != embedding_fingerprint(areas_b)


def _org_profile(**org):
    """A profile whose only variable is `structured["org"]`."""
    return _profile(description="Liver disease", structured={"areas": ["liver"], "org": org})


def test_embedding_fingerprint_tracks_the_org_fields_the_builder_folds_in():
    """Task 28 appended `org.capabilities` and `org.patient_populations` to
    the embedding text (similarity.py:93-99). Neither is a column on Profile,
    so a fingerprint built from the row's scalar fields would miss both and
    call a stale vector current."""
    cap_a = _org_profile(capabilities=["biobank"])
    cap_b = _org_profile(capabilities=["sequencing core"])
    assert embedding_fingerprint(cap_a) != embedding_fingerprint(cap_b)

    pop_a = _org_profile(patient_populations=["cirrhosis"])
    pop_b = _org_profile(patient_populations=["hepatitis B"])
    assert embedding_fingerprint(pop_a) != embedding_fingerprint(pop_b)


def test_rank_fingerprint_covers_budget_and_trl():
    """Rev 1's enumeration omitted these; criteria.py:111 and :129 read them
    straight off `structured`. Hashing the whole blob is why this passes."""
    a = _profile(structured={"areas": ["liver"], "budget_min": 100})
    b = _profile(structured={"areas": ["liver"], "budget_min": 200})
    assert rank_fingerprint(a) != rank_fingerprint(b)

    # TRL was covered by construction, not by assertion: varying only
    # `budget_min` leaves this test green with every trl_* key stripped from
    # the payload, so the name promised coverage the body did not deliver.
    trl_min_a = _profile(structured={"areas": ["liver"], "trl_min": 3, "trl_max": 9})
    trl_min_b = _profile(structured={"areas": ["liver"], "trl_min": 7, "trl_max": 9})
    assert rank_fingerprint(trl_min_a) != rank_fingerprint(trl_min_b)

    trl_max_a = _profile(structured={"areas": ["liver"], "trl_min": 3, "trl_max": 6})
    trl_max_b = _profile(structured={"areas": ["liver"], "trl_min": 3, "trl_max": 9})
    assert rank_fingerprint(trl_max_a) != rank_fingerprint(trl_max_b)


def test_rank_fingerprint_covers_budget_currency_and_name():
    """Measured on rev 1 of this file: deleting "budget_currency" from the
    payload left all 8 tests green, and deleting "name" as well still left 8
    green. `budget_currency` is a genuine ranking input -- criteria.py:115-120
    returns UNKNOWN / currency_mismatch off it -- so a payload that dropped it
    would call a ranking built under the wrong currency current forever."""
    money = {"areas": ["liver"], "budget_min": 100, "budget_max": 200}
    sek = _profile(structured=dict(money), budget_currency="SEK")
    eur = _profile(structured=dict(money), budget_currency="EUR")
    assert rank_fingerprint(sek) != rank_fingerprint(eur)

    # `name` is insurance (nothing in ProfileUpdateIn writes it) but it does
    # reach the LLM prompt at ranking/prompt.py:218-219, so it is in the
    # payload and must stay pinned.
    hepatology = _profile(name="Hepatology")
    cardiology = _profile(name="Cardiology")
    assert rank_fingerprint(hepatology) != rank_fingerprint(cardiology)


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
