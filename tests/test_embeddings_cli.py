"""CLI backfill — wraps refresh_missing_embeddings_tick + prints progress."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from call_monitor.db.models import Call, Profile, ProfileKind, Source, User
from call_monitor.embeddings.cli import backfill_main


def _seed(s, *, n_calls: int = 3, n_profiles: int = 2) -> None:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    for i in range(n_calls):
        s.add(Call(
            source_id=src.id, external_id=f"cli-{i}", url=f"https://x/{i}",
            title=f"Call {i}", summary="x",
            published_at=datetime.now(UTC),
        ))
    for i in range(n_profiles):
        u = User(email=f"cli-{i}@ki.se")
        s.add(u)
        s.flush()
        s.add(Profile(
            name=f"P{i}", kind=ProfileKind.GROUP,
            description=f"Description for P{i}",
            structured={}, created_by=u.id, ranking_scheme="v2",
        ))


def test_cli_backfill_embeds_all_null_rows(pg_session_factory, capsys):
    with pg_session_factory() as s:
        _seed(s, n_calls=3, n_profiles=2)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = [0.5] * 1024

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "calls_embedded" in captured.out
    assert "profiles_embedded" in captured.out

    with pg_session_factory() as s:
        n_call_null = s.execute(
            select(Call).where(Call.embedding.is_(None))
        ).scalars().all()
        n_profile_null = s.execute(
            select(Profile).where(Profile.embedding.is_(None))
        ).scalars().all()
    assert len(n_call_null) == 0
    assert len(n_profile_null) == 0


def test_cli_backfill_loops_until_all_done(pg_session_factory):
    """When more rows than batch_size, CLI loops; tick runs >1 time."""
    with pg_session_factory() as s:
        # Seed many calls so a batch_size=2 would require 3+ batches
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(5):
            s.add(Call(
                source_id=src.id, external_id=f"loop-{i}", url=f"https://x/{i}",
                title=f"L{i}", summary="x", published_at=datetime.now(UTC),
            ))
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = [0.5] * 1024

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill", "--batch-size", "2"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    # All 5 ended up embedded
    with pg_session_factory() as s:
        nulls = s.execute(select(Call).where(Call.embedding.is_(None))).scalars().all()
    assert len(nulls) == 0
    # embed was called 5 times (once per call)
    assert fake_client.embed.call_count == 5


def test_cli_backfill_terminates_when_only_blank_description_profiles_remain(
    pg_session_factory, capsys,
):
    """CLI must not infinite-loop when blank-description profiles exist —
    they're skipped at SQL level (Codex regression). Should exit 0
    (no failures, just nothing to do for those rows)."""
    with pg_session_factory() as s:
        u = User(email="blank-cli@ki.se")
        s.add(u)
        s.flush()
        # Add a profile with blank description
        s.add(Profile(
            name="BlankCLI", kind=ProfileKind.GROUP, description="",
            structured={}, created_by=u.id, ranking_scheme="v2",
        ))
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = AssertionError("embed should not be invoked")

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    # No failures, no embeds — should exit 0 cleanly
    assert exit_code == 0


def test_cli_backfill_returns_nonzero_on_persistent_failures(pg_session_factory):
    """If any row fails to embed (LLM down etc), exit 1 so deploy scripts catch it."""
    from call_monitor.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        _seed(s, n_calls=1, n_profiles=0)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = LLMError("down")

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1
