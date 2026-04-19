from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy import select

from call_monitor.db.models import Call, Profile, ProfileKind, Source, User
from call_monitor.embeddings.refresh import (
    refresh_call_embedding,
    refresh_missing_embeddings_tick,
    refresh_profile_embedding,
)
from call_monitor.ranking.ollama_client import LLMError


def _seed_call(s, *, source_slug: str = "vinnova", title: str = "T") -> int:
    src = s.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"{title}-1", url="https://x",
        title=title, summary="S", full_text="F",
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
        name=name, kind=ProfileKind.GROUP, description=f"{name} description",
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

    logger_name = "call_monitor.embeddings.refresh"
    with pg_session_factory() as s, caplog.at_level(logging.WARNING, logger=logger_name):
        ok = refresh_call_embedding(s, client, cid)
        s.commit()
    assert ok is False
    assert any(
        "embedding_compute_failed" in r.getMessage()
        or "embedding_compute_failed" in r.message
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
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
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
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
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
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    # Both got re-embedded (the old-model one + the NULL one)
    assert summary["calls_embedded"] == 2

    with pg_session_factory() as s:
        # Both now stamped with current model
        assert s.get(Call, cid_old).embedding_model == "bge-m3"
        assert s.get(Call, cid_null).embedding_model == "bge-m3"


def test_refresh_missing_tick_skips_blank_description_profiles(pg_session_factory):
    """Codex regression: a profile with blank description has no text to
    embed. The tick must not select it (would otherwise infinite-loop:
    select NULL → no-op clear → still NULL → re-select)."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="BlankDesc")
        s.flush()
        s.get(Profile, pid).description = ""
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    # Embed should NEVER be called — blank-description profile is skipped at SQL level
    client.embed.side_effect = AssertionError("embed should not be invoked for blank-desc profile")

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    assert summary["profiles_embedded"] == 0
    assert summary["profiles_failed"] == 0


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
        session_factory=pg_session_factory, embed_client=client,
        batch_size=200, force=True,
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
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    assert summary["calls_embedded"] == 0
    assert summary["profiles_embedded"] == 0

    with pg_session_factory() as s:
        # Vectors untouched
        assert list(s.get(Call, cid).embedding) == [0.9] * 1024
        assert list(s.get(Profile, pid).embedding) == [0.9] * 1024


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
