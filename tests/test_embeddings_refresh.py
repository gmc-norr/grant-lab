from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy import select

from call_manager.db.models import Call, Profile, ProfileKind, Source, User
from call_manager.embeddings.refresh import (
    refresh_call_embedding,
    refresh_missing_embeddings_tick,
    refresh_profile_embedding,
)
from call_manager.ranking.ollama_client import LLMError


def _seed_call(s, *, source_slug: str = "vinnova", title: str = "T") -> int:
    src = s.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=f"{title}-1",
        url="https://x",
        title=title,
        summary="S",
        full_text="F",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id


def _seed_profile(s, *, name: str = "P") -> int:
    u = User(email=f"{name.lower()}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description=f"{name} description",
        structured={"areas": [], "hard_exclusions": []},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    return p.id


def _fake_client(vec: list[float] | None = None, *, raises: Exception | None = None):
    client = MagicMock()
    client.model = "bge-m3"
    if raises is not None:
        client.embed.side_effect = raises
    else:
        client.embed.return_value = vec or [0.1] * 1024
    return client


def test_refresh_call_embedding_writes_vector_and_model(pg_session_factory):
    with pg_session_factory() as s:
        cid = _seed_call(s, title="C1")
        s.commit()
    client = _fake_client(vec=[0.5] * 1024)

    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, cid)
        s.commit()
    assert ok is True

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        assert c.embedding is not None
        assert len(c.embedding) == 1024
        assert c.embedding_model == "bge-m3"


def test_refresh_call_embedding_returns_false_on_llm_error(pg_session_factory, caplog):
    import logging

    with pg_session_factory() as s:
        cid = _seed_call(s, title="C2")
        s.commit()
    client = _fake_client(raises=LLMError("ollama down"))

    logger_name = "call_manager.embeddings.refresh"
    with pg_session_factory() as s, caplog.at_level(logging.WARNING, logger=logger_name):
        ok = refresh_call_embedding(s, client, cid)
        s.commit()
    assert ok is False
    assert any(
        "embedding_compute_failed" in r.getMessage() or "embedding_compute_failed" in r.message
        for r in caplog.records
    )

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        assert c.embedding is None


def test_refresh_call_embedding_returns_false_for_missing_call(pg_session_factory):
    client = _fake_client()
    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, 999999)
    assert ok is False


def test_refresh_profile_embedding_writes_vector_and_model(pg_session_factory):
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="PE1")
        s.commit()
    client = _fake_client(vec=[0.3] * 1024)

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid)
        s.commit()
    assert ok is True

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is not None
        assert p.embedding_model == "bge-m3"


def test_refresh_missing_embeddings_tick_processes_only_nulls(pg_session_factory):
    """Tick batches NULL-embedding rows. Already-embedded rows with the
    SAME model are skipped (the mismatch case is tested separately)."""
    client = _fake_client(vec=[0.1] * 1024)
    with pg_session_factory() as s:
        c1 = _seed_call(s, title="MissTick1")
        c2 = _seed_call(s, title="MissTick2")
        # c2 already has embedding under the SAME model — should be skipped
        s.get(Call, c2).embedding = [0.9] * 1024
        s.get(Call, c2).embedding_model = "bge-m3"
        p1 = _seed_profile(s, name="MissTick3")
        s.commit()

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )
    assert summary["calls_embedded"] == 1
    assert summary["profiles_embedded"] == 1

    with pg_session_factory() as s:
        assert s.get(Call, c1).embedding is not None
        # c2 untouched (pgvector returns numpy array; convert via list())
        assert list(s.get(Call, c2).embedding) == [0.9] * 1024
        assert s.get(Profile, p1).embedding is not None


def test_refresh_missing_embeddings_tick_continues_after_one_failure(pg_session_factory):
    """If embed_client raises for one row, tick logs and continues with next."""
    with pg_session_factory() as s:
        _seed_call(s, title="MTickFail1")
        _seed_call(s, title="MTickFail2")
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = [LLMError("first one fails"), [0.5] * 1024]

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )
    assert summary["calls_embedded"] == 1
    assert summary["calls_failed"] == 1


def test_refresh_missing_tick_re_embeds_rows_with_mismatched_model(pg_session_factory):
    """Codex H2 regression: a row stored under model 'old-model' is re-embedded
    when the configured model is 'bge-m3'. NULL rows still also picked up."""
    with pg_session_factory() as s:
        cid_old = _seed_call(s, title="OldModel")
        s.flush()
        s.get(Call, cid_old).embedding = [0.9] * 1024
        s.get(Call, cid_old).embedding_model = "old-model"
        cid_null = _seed_call(s, title="NullModel")
        s.commit()

    client = _fake_client(vec=[0.1] * 1024)
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )
    # Both got re-embedded (the old-model one + the NULL one)
    assert summary["calls_embedded"] == 2

    with pg_session_factory() as s:
        # Both now stamped with current model
        assert s.get(Call, cid_old).embedding_model == "bge-m3"
        assert s.get(Call, cid_null).embedding_model == "bge-m3"


def test_refresh_missing_tick_noops_on_blank_desc_no_areas_profile(pg_session_factory):
    """A profile with no description AND no areas has no embeddable text.
    After the Codex #2 change (areas included in build_profile_text), the
    tick's SQL pre-filter was dropped, so such a profile gets selected —
    but the inline `if not text.strip()` guard in refresh_profile_embedding
    makes the per-row call a no-op: the embedding client is never invoked."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="BlankDesc")
        s.flush()
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {}  # no areas either
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    # The inline no-op guard must prevent this from being invoked.
    client.embed.side_effect = AssertionError(
        "embed must not be invoked when both description and areas are empty"
    )

    # Not asserting on the summary counts here — the inline path returns
    # True (state kept consistent with blank input), but the important
    # invariant is that we never hit the remote embedder with empty text.
    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )


def test_refresh_missing_tick_embeds_profile_with_only_areas(pg_session_factory):
    """Codex #2: a profile with an empty description but populated areas
    still has embeddable signal — the tick must pick it up."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="AreasOnly")
        s.flush()
        p = s.get(Profile, pid)
        p.description = ""
        p.structured = {"areas": ["clinical AI", "radiology"]}
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.return_value = [0.2] * 1024

    refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )

    # Verify the embed was actually called and the vector landed.
    assert client.embed.call_count == 1
    call_text = client.embed.call_args[0][0]
    assert "clinical AI" in call_text
    assert "radiology" in call_text
    with pg_session_factory() as s:
        assert s.get(Profile, pid).embedding is not None


def test_refresh_missing_tick_force_true_re_embeds_already_embedded_rows(pg_session_factory):
    """Plan F Task 7: force=True drops the NULL/mismatch filter and re-embeds
    every row. Used after deploys that change the text being embedded
    (e.g., Plan F's full_text rollout)."""
    with pg_session_factory() as s:
        cid = _seed_call(s, title="ForceRe1")
        s.flush()
        # Already embedded with the CURRENT model — would be skipped by default
        s.get(Call, cid).embedding = [0.9] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        pid = _seed_profile(s, name="ForceRe2")
        s.flush()
        s.get(Profile, pid).embedding = [0.9] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.commit()

    client = _fake_client(vec=[0.1] * 1024)
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
        force=True,
    )
    assert summary["calls_embedded"] == 1
    assert summary["profiles_embedded"] == 1

    with pg_session_factory() as s:
        # Vectors were overwritten with the new (0.1) values
        assert list(s.get(Call, cid).embedding) == [0.1] * 1024
        assert list(s.get(Profile, pid).embedding) == [0.1] * 1024


def test_refresh_missing_tick_force_false_default_skips_already_embedded(pg_session_factory):
    """Regression guard: default behavior (force=False) is byte-identical to
    today — already-embedded same-model rows are NOT re-embedded."""
    with pg_session_factory() as s:
        cid = _seed_call(s, title="ForceFalse1")
        s.flush()
        s.get(Call, cid).embedding = [0.9] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        pid = _seed_profile(s, name="ForceFalse2")
        s.flush()
        s.get(Profile, pid).embedding = [0.9] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.commit()

    client = _fake_client(vec=[0.1] * 1024)
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=200,
    )
    assert summary["calls_embedded"] == 0
    assert summary["profiles_embedded"] == 0

    with pg_session_factory() as s:
        # Vectors untouched
        assert list(s.get(Call, cid).embedding) == [0.9] * 1024
        assert list(s.get(Profile, pid).embedding) == [0.9] * 1024


def test_refresh_call_embedding_clears_columns_on_force_mode_failure(pg_session_factory):
    """Codex regression: in --force mode, an LLMError must NULL both
    embedding columns so the daily refresh tick can see the row needs
    a retry. Without this, the row keeps its OLD vector AND its current
    model name — invisible to the daily tick's NULL/mismatched filter
    and silently scoring against a known-stale vector."""
    with pg_session_factory() as s:
        cid = _seed_call(s, title="ForceFailRow")
        s.flush()
        # Pre-seed an OLD-but-current-model embedding (simulates the state
        # right before a --force run that fails).
        s.get(Call, cid).embedding = [0.5] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = LLMError("ollama down")

    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, cid, force=True)
        s.commit()
    assert ok is False

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        # Both columns nulled — daily tick can re-select on embedding IS NULL
        assert c.embedding is None
        assert c.embedding_model is None


def test_refresh_call_embedding_default_mode_failure_preserves_columns(pg_session_factory):
    """Default (non-force) behavior unchanged: on LLMError, the row's
    existing columns are LEFT untouched. (In default mode, the row's
    prior state was already either NULL or mismatched-model — the daily
    tick will retry regardless. Preserving means 'no progress', not
    'overwrite with NULL'.)"""
    with pg_session_factory() as s:
        cid = _seed_call(s, title="DefaultFailRow")
        s.flush()
        s.get(Call, cid).embedding = [0.5] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = LLMError("ollama down")

    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, cid)  # default force=False
        s.commit()
    assert ok is False

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        # Existing values preserved
        assert list(c.embedding) == [0.5] * 1024
        assert c.embedding_model == "bge-m3"


def test_refresh_profile_embedding_clears_columns_on_force_mode_failure(pg_session_factory):
    """Mirror of the call-side test for profiles."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="ForceFailProf")
        s.flush()
        s.get(Profile, pid).embedding = [0.5] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = LLMError("ollama down")

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid, force=True)
        s.commit()
    assert ok is False

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None


def test_refresh_profile_embedding_default_mode_failure_preserves_columns(pg_session_factory):
    """Mirror of the call-side default-mode test."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="DefaultFailProf")
        s.flush()
        s.get(Profile, pid).embedding = [0.5] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = LLMError("ollama down")

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid)  # default force=False
        s.commit()
    assert ok is False

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert list(p.embedding) == [0.5] * 1024
        assert p.embedding_model == "bge-m3"


def test_refresh_missing_tick_force_propagates_to_per_row_helpers(pg_session_factory):
    """When the tick is invoked with force=True, per-row LLM failures must
    clear columns; the daily tick (force=False) on the next pass should
    then re-select the cleared rows via its NULL filter and succeed once
    Ollama recovers."""
    with pg_session_factory() as s:
        cid = _seed_call(s, title="ForceTickFail")
        s.flush()
        s.get(Call, cid).embedding = [0.5] * 1024
        s.get(Call, cid).embedding_model = "bge-m3"
        pid = _seed_profile(s, name="ForceTickFailProf")
        s.flush()
        s.get(Profile, pid).embedding = [0.5] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.commit()

    # First pass — force=True with a failing client.
    failing = MagicMock()
    failing.model = "bge-m3"
    failing.embed.side_effect = LLMError("ollama transient")

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=failing,
        batch_size=200,
        force=True,
    )
    assert summary["calls_failed"] == 1
    assert summary["profiles_failed"] == 1

    # Both rows should now have cleared columns
    with pg_session_factory() as s:
        assert s.get(Call, cid).embedding is None
        assert s.get(Call, cid).embedding_model is None
        assert s.get(Profile, pid).embedding is None
        assert s.get(Profile, pid).embedding_model is None

    # Second pass — default (force=False) with a working client.
    # The cleared rows are picked up via the NULL filter.
    working = _fake_client(vec=[0.7] * 1024)
    summary2 = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=working,
        batch_size=200,
    )
    assert summary2["calls_embedded"] == 1
    assert summary2["profiles_embedded"] == 1

    with pg_session_factory() as s:
        assert list(s.get(Call, cid).embedding) == [0.7] * 1024
        assert s.get(Call, cid).embedding_model == "bge-m3"
        assert list(s.get(Profile, pid).embedding) == [0.7] * 1024
        assert s.get(Profile, pid).embedding_model == "bge-m3"


def test_refresh_missing_tick_drains_backlog_in_one_call(pg_session_factory):
    """Daily-tick drain: with more NULL rows than batch_size, a single tick
    invocation loops over batches until the backlog is exhausted — it no
    longer leaves the remainder for "tomorrow". Before the fix this would
    take ceil(N / batch_size) days to recover after a model swap."""
    with pg_session_factory() as s:
        for i in range(5):
            _seed_call(s, title=f"Drain{i}")
        s.commit()

    client = _fake_client(vec=[0.1] * 1024)
    # batch_size=2 forces at least 3 batches to drain 5 calls.
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=2,
    )
    assert summary["calls_embedded"] == 5

    with pg_session_factory() as s:
        for i in range(5):
            cid = s.execute(select(Call.id).where(Call.external_id == f"Drain{i}-1")).scalar_one()
            assert s.get(Call, cid).embedding is not None


def test_refresh_missing_tick_drain_stops_when_all_remaining_fail(pg_session_factory):
    """The drain loop must terminate (not infinite-loop) when every remaining
    row fails to embed — "no successful embeds this batch" is the stop signal."""
    with pg_session_factory() as s:
        _seed_call(s, title="AlwaysFail1")
        _seed_call(s, title="AlwaysFail2")
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = LLMError("ollama permanently down")

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory,
        embed_client=client,
        batch_size=1,
    )
    # One batch of failures, no progress → stop. Exactly batch_size failures
    # recorded (the loop breaks after the first zero-progress batch).
    assert summary["calls_embedded"] == 0
    assert summary["calls_failed"] >= 1


def test_refresh_profile_embedding_clears_when_description_blank(pg_session_factory):
    """Codex M2 regression: clearing description removes the stale embedding."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="ClearMe")
        s.flush()
        s.get(Profile, pid).embedding = [0.9] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.get(Profile, pid).description = ""  # cleared
        s.commit()

    client = _fake_client(vec=[0.5] * 1024)  # would NOT be invoked
    client.embed.side_effect = AssertionError("embed should not be called for blank text")

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid)
        s.commit()

    assert ok is True  # successful refresh = state-consistent
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
