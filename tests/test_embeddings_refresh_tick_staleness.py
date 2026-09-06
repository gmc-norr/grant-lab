"""Task 4 gates: the daily tick breaks on rows examined, not rows worked."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from grant_lab.db.models import Profile
from grant_lab.embeddings.refresh import (
    _refresh_missing_embeddings_batch,
    refresh_missing_embeddings_tick,
)
from grant_lab.profiles.fingerprint import embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_profile


@pytest.fixture()
def fake_embedder():
    """FakeEmbedder counts embed calls (gate 7) AND supplies session(), which
    _refresh_missing_embeddings_batch opens twice per batch."""
    return FakeEmbedder()


def test_a_changed_text_is_stale_even_with_a_current_vector_and_model(
    pg_session_factory, fake_embedder
):
    """GATE 4. Vector present, model current -- invisible to the old predicate."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint == embedding_fingerprint(p)
        p.description = "Cardiac imaging"
        s.commit()
        stale_fp = p.embedding_fingerprint

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding_fingerprint != stale_fp
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_stale_profile_behind_a_full_fresh_page_is_still_reached(
    pg_session_factory, fake_embedder
):
    """GATE 5 -- the one that would have caught rev 1's mistake.

    Uses batch_size=2 rather than a realistic corpus, so the loop's exit
    condition is what is under test, not the row count.
    """
    with pg_session_factory() as s:
        for i in range(4):
            make_profile(s, description=f"Fresh {i}")
        s.commit()
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )

    with pg_session_factory() as s:
        stale = make_profile(s, description="Originally this")
        s.commit()
        sid = stale.id
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        assert s.get(Profile, sid).embedding is not None
        # Now make the HIGHEST id stale, behind two full pages of fresh rows.
        s.get(Profile, sid).description = "Changed to something else"
        s.commit()

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=2
    )
    with pg_session_factory() as s:
        p = s.get(Profile, sid)
        assert p.embedding_fingerprint == embedding_fingerprint(p), (
            "the drain stopped at the first page with nothing to do and threw away its cursor"
        )


def test_a_profile_cleared_to_blank_is_invalidated_by_the_tick(pg_session_factory, fake_embedder):
    """GATE 8 -- hole 4, a defect in already-shipped code.

    refresh.py:328 excluded blank profiles from the tick, so the branch that
    wipes a cleared profile's vector could only ever fire from the request
    path -- the lossy one.
    """
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease")
        s.commit()
        pid = p.id
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        assert s.get(Profile, pid).embedding is not None
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}
        s.commit()

    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
        assert p.embedding_fingerprint is None


def test_an_already_blank_and_already_cleared_profile_is_not_rescanned_forever(
    pg_session_factory, fake_embedder
):
    """The other half of gate 8: including blank rows must not break termination.

    The mechanism that can actually break is NOT "a blank row gets re-embedded"
    -- it cannot, because such a row is never appended to `profile_ids`. It is
    "a blank row is re-EXAMINED every iteration because the cursor never moves
    past it". So this asserts the cursor, and then proves at the tick level
    that a stale row sitting BEHIND a blank one is still reached.
    """
    with pg_session_factory() as s:
        p = make_profile(s, description="")
        p.structured = {}
        s.commit()
        blank_id = p.id
    summary = _refresh_missing_embeddings_batch(
        session_factory=pg_session_factory,
        embed_client=fake_embedder,
        batch_size=200,
        force=False,
        after_call_id=0,
        after_profile_id=0,
    )
    assert summary["profiles_embedded"] == 0
    assert summary["profiles_failed"] == 0
    # It WAS examined -- that is what keeps the drain going ...
    assert summary["profiles_seen"] == 1
    # ... and the cursor moved past it, which is what stops the drain from
    # re-reading it on every iteration until max_batches.
    assert summary["max_profile_id"] == blank_id

    # Tick-level proof, with batch_size=1 so the blank row owns page one: a
    # stale row behind it must still be repaired. With a stuck cursor every
    # page re-reads the blank row and this profile is never reached.
    with pg_session_factory() as s:
        behind = make_profile(s, description="Behind the blank row")
        s.commit()
        behind_id = behind.id
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, batch_size=1
    )
    with pg_session_factory() as s:
        p = s.get(Profile, behind_id)
        assert p.embedding is not None
        assert p.embedding_fingerprint == embedding_fingerprint(p)


def test_force_mode_ignores_the_fingerprint(pg_session_factory, fake_embedder):
    """GATE 7. force=True must re-embed every embeddable row regardless."""
    with pg_session_factory() as s:
        make_profile(s, description="Liver disease")
        s.commit()
    refresh_missing_embeddings_tick(session_factory=pg_session_factory, embed_client=fake_embedder)
    fake_embedder.calls = 0

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=fake_embedder, force=True
    )
    assert fake_embedder.calls > 0, "force mode skipped a fingerprint-fresh row"


def test_the_staleness_decision_does_not_load_the_vector(pg_session_factory, fake_embedder):
    """GATE 6. Parsing 1024-dim vectors is the measured dominant cost of a
    profile load; loading them to decide whether to replace them is a
    regression. Assert on the emitted SQL of the CANDIDATE SELECT.
    """
    import re

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with pg_session_factory() as session:
        engine = session.bind
        event.listen(engine, "before_cursor_execute", _record)
        try:
            make_profile(session, description="Liver disease")
            session.commit()
            statements.clear()
            _refresh_missing_embeddings_batch(
                session_factory=pg_session_factory,
                embed_client=fake_embedder,
                batch_size=200,
                force=False,
                after_call_id=0,
                after_profile_id=0,
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    # Narrow to the paged CANDIDATE scan. A filter of
    # `"FROM profiles" in s and "SELECT" in s` also catches the
    # `session.get(Profile, pid)` that refresh_profile_embedding must issue
    # for the row it is about to embed -- which loads the vector by necessity.
    candidate = [s for s in statements if "ORDER BY profiles.id" in s and "LIMIT" in s]
    assert len(candidate) == 1, f"expected exactly one candidate scan, got {len(candidate)}"
    sql = candidate[0]

    # Look ONLY at the result columns. `re.split` on a FROM word boundary, not
    # `sql.split(" FROM ")`: the compiled SQL puts a newline before FROM, so
    # the space-delimited form silently returns the WHOLE statement and every
    # position-sensitive assertion below then reads the WHERE clause too.
    select_list = re.split(r"\bFROM\b", sql, maxsplit=1)[0]

    # The vector must not be a result column in ANY shape the ORM emits it:
    #   `profiles.embedding,`        mid-list
    #   `profiles.embedding`         last, no trailing comma
    #   `profiles.embedding AS ...`  aliased (what session.get emits)
    # The two lookaheads exclude the three things that legitimately start with
    # the same prefix: profiles.embedding_model, profiles.embedding_fingerprint,
    # and `profiles.embedding IS NULL AS embedding_is_null` -- the computed
    # flag, which is exactly what this gate wants to see instead.
    leaked = re.findall(r"profiles\.embedding(?!_)(?!\s+IS\s+NULL)", select_list)
    assert not leaked, f"the candidate SELECT loaded the 1024-dim vector:\n{sql}"
    assert "embedding_is_null" in select_list, (
        f"no computed NULL flag in the candidate scan:\n{sql}"
    )
