"""The gate for the invariant `ranking/freshness.py::_profile_rows` states in a
comment and nothing executes: the narrowed Row must produce a byte-identical
rank_fingerprint to a loaded Profile.

It reads the rows out of `_profile_rows` itself, so it cannot drift from the
code it guards.
"""

import pytest

from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking import freshness as F


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


def test_the_narrowed_row_and_the_loaded_profile_agree(session):
    # created_by is NOT NULL -- a bare created_by=None dies at commit() with an
    # IntegrityError, so the User is required.
    u = User(email="rg@umu.se", name="rg", status="active")
    session.add(u)
    session.flush()
    # embedding/embedding_model/embedding_fingerprint are all SET on purpose.
    # Left NULL, _canonical (fingerprint.py:40) drops them from BOTH payloads
    # and the two sides agree on "key absent" -- so the gate would redden only
    # via AttributeError, and softening fingerprint.py to
    # `getattr(profile, "embedding_model", None)` (the natural way to silence
    # that AttributeError) would turn it green while the bug returned. With
    # real values the assertion compares VALUES, not attribute existence.
    # `embedding` is required alongside `embedding_model`:
    # ck_profiles_embedding_model_paired (db/models/profiles.py:72-75) enforces
    # (embedding IS NULL) = (embedding_model IS NULL), so setting one without
    # the other fails at commit.
    p = Profile(
        name="Hepatology",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        level="group",
        description="Liver disease",
        structured={"areas": ["liver"]},
        budget_currency="SEK",
        created_by=u.id,
        visibility="private",
        embedding=[0.0] * 1024,
        embedding_model="bge-m3",
        embedding_fingerprint="v2:" + "0" * 64,
    )
    session.add(p)
    session.commit()

    loaded = session.get(Profile, p.id)
    # The rows reconcile/ingest actually hash, from the function that builds them.
    rows = [r for r in F._profile_rows(session) if r.id == p.id]
    assert len(rows) == 1, "the active-profile select did not return the seeded profile"
    row = rows[0]
    assert rank_fingerprint(row) == rank_fingerprint(loaded), (
        "freshness._profile_rows does not carry every column rank_fingerprint reads"
    )
