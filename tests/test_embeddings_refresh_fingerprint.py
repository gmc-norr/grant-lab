"""Task 3 gates: the fingerprint travels with the vector in every branch."""

from __future__ import annotations

from grant_lab.db.models import Profile
from grant_lab.embeddings.refresh import refresh_profile_embedding
from grant_lab.profiles.fingerprint import embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_profile


def test_success_stamps_the_fingerprint(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_blank_text_clears_all_three(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}
        s.commit()
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_force_failure_clears_all_three(pg_session_factory):
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(fail=True), pid, force=True)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_non_force_failure_leaves_all_three_untouched(pg_session_factory):
    """The review's 'normal failure case'. The old vector survives and the
    fingerprint keeps describing it truthfully, so the row is DETECTABLY
    stale rather than silently wrong."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        refresh_profile_embedding(s, FakeEmbedder(), pid)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        fp_before = p.embedding_fingerprint
        p.description = "Completely different subject"
        s.commit()
        refresh_profile_embedding(s, FakeEmbedder(fail=True), pid, force=False)
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is not None
        assert p.embedding_model == "bge-m3"
        assert p.embedding_fingerprint == fp_before
        # ...and that is now provably stale:
        assert p.embedding_fingerprint != embedding_fingerprint(p)


def test_blank_text_clears_a_stray_fingerprint_with_no_vector(pg_session_factory):
    """The guard's third disjunct, which nothing else reaches.

    A row can carry a fingerprint with no vector: `ck_profiles_embedding_model_paired`
    pairs `embedding` with `embedding_model` only, and trap 1 deliberately leaves
    `embedding_fingerprint` out of it. Without the `embedding_fingerprint is not None`
    disjunct the guard skips such a row, and it keeps claiming to describe content it
    has no vector for -- the state Task 7's reconcile arm reads.
    """
    with pg_session_factory() as s:
        p = make_profile(
            s,
            description="",
            structured={},
            embedding=None,
            embedding_model=None,
            embedding_fingerprint="v1:" + "0" * 64,
        )
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        assert refresh_profile_embedding(s, FakeEmbedder(), pid) is True
        s.commit()
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint is None
        assert p.embedding is None
        assert p.embedding_model is None
