"""The gate for the invariant `ranking/queue.py:163-167` states in a comment and
nothing executes: the narrowed Row must produce a byte-identical rank_fingerprint
to a loaded Profile.

It reads the column list out of reconcile_missing_rankings' own source, so it
cannot drift from the code it guards.
"""

import inspect
import re

import pytest
from sqlalchemy import select

from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking import queue as Q


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
    # Left NULL, _canonical (fingerprint.py:46) drops them from BOTH payloads
    # and the two sides agree on "key absent" -- so the gate would redden only
    # via AttributeError, and softening fingerprint.py to
    # `getattr(profile, "embedding_model", None)` (the natural way to silence
    # that AttributeError) would turn it green while the bug returned. With
    # real values the assertion compares VALUES, not attribute existence.
    # `embedding` is required alongside `embedding_model`:
    # ck_profiles_embedding_model_paired (db/models/profiles.py:60-63) enforces
    # (embedding IS NULL) = (embedding_model IS NULL), so setting one without
    # the other fails at commit.
    p = Profile(name="Hepatology", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="Liver disease", structured={"areas": ["liver"]},
                budget_currency="SEK", created_by=u.id, visibility="private",
                embedding=[0.0] * 1024, embedding_model="bge-m3",
                embedding_fingerprint="v2:" + "0" * 64)
    session.add(p)
    session.commit()

    loaded = session.get(Profile, p.id)
    # The function is reconcile_missing_rankings (queue.py:113), NOT enqueue_reconcile.
    src = inspect.getsource(Q.reconcile_missing_rankings)
    block = src.split("profiles = session.execute(", 1)[1].split(").where(Profile.is_active", 1)[0]
    cols = re.findall(r"Profile\.(\w+)", block)
    assert cols, "could not read queue.py's column list"
    row = session.execute(
        select(*[getattr(Profile, c) for c in cols]).where(Profile.id == p.id)
    ).one()
    assert rank_fingerprint(row) == rank_fingerprint(loaded), (
        f"queue.py's narrowed SELECT {cols} does not carry every column "
        f"rank_fingerprint reads"
    )
