"""CLI backfill — wraps refresh_missing_embeddings_tick + prints progress."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from grant_lab.db.models import Call, Profile, ProfileKind, Source, User
from grant_lab.embeddings.cli import backfill_main
from grant_lab.embeddings.client import EmbedResult


def _embed_result(vec: list[float]) -> EmbedResult:
    return EmbedResult(vector=vec, embedded_chars=1)


def _seed(s, *, n_calls: int = 3, n_profiles: int = 2) -> None:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    for i in range(n_calls):
        s.add(
            Call(
                source_id=src.id,
                external_id=f"cli-{i}",
                url=f"https://x/{i}",
                title=f"Call {i}",
                summary="x",
                published_at=datetime.now(UTC),
            )
        )
    for i in range(n_profiles):
        u = User(email=f"cli-{i}@umu.se")
        s.add(u)
        s.flush()
        s.add(
            Profile(
                name=f"P{i}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description=f"Description for P{i}",
                structured={},
                created_by=u.id,
            )
        )


def test_cli_backfill_embeds_all_null_rows(pg_session_factory, capsys):
    with pg_session_factory() as s:
        _seed(s, n_calls=3, n_profiles=2)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = _embed_result([0.5] * 1024)

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "calls_embedded" in captured.out
    assert "profiles_embedded" in captured.out

    with pg_session_factory() as s:
        n_call_null = s.execute(select(Call).where(Call.embedding.is_(None))).scalars().all()
        n_profile_null = (
            s.execute(select(Profile).where(Profile.embedding.is_(None))).scalars().all()
        )
    assert len(n_call_null) == 0
    assert len(n_profile_null) == 0


def test_cli_backfill_loops_until_all_done(pg_session_factory):
    """When more rows than batch_size, CLI loops; tick runs >1 time."""
    with pg_session_factory() as s:
        # Seed many calls so a batch_size=2 would require 3+ batches
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(5):
            s.add(
                Call(
                    source_id=src.id,
                    external_id=f"loop-{i}",
                    url=f"https://x/{i}",
                    title=f"L{i}",
                    summary="x",
                    published_at=datetime.now(UTC),
                )
            )
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = _embed_result([0.5] * 1024)

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
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
    pg_session_factory,
    capsys,
):
    """CLI must not infinite-loop when blank-description profiles exist —
    they're skipped at SQL level (Codex regression). Should exit 0
    (no failures, just nothing to do for those rows)."""
    with pg_session_factory() as s:
        u = User(email="blank-cli@umu.se")
        s.add(u)
        s.flush()
        # Add a profile with blank description
        s.add(
            Profile(
                name="BlankCLI",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="",
                structured={},
                created_by=u.id,
            )
        )
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = AssertionError("embed should not be invoked")

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    # No failures, no embeds — should exit 0 cleanly
    assert exit_code == 0


def test_cli_backfill_force_flag_re_embeds_already_embedded_rows(pg_session_factory):
    """Plan F Task 7: --force flag passes through to refresh tick and re-embeds
    rows even when they're already embedded under the current model."""
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="force-cli-1",
            url="https://x/f1",
            title="ForceCLI",
            summary="x",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        # Pre-embed under the current model — default would skip
        c.embedding = [0.9] * 1024
        c.embedding_model = "bge-m3"
        s.commit()
        cid = c.id

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = _embed_result([0.1] * 1024)

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill", "--force"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    # embed was called for the row (would have been 0 without --force)
    assert fake_client.embed.call_count >= 1
    with pg_session_factory() as s:
        # Vector was overwritten
        assert list(s.get(Call, cid).embedding) == [0.1] * 1024


def test_cli_backfill_force_terminates_with_small_batch_size(pg_session_factory):
    """Plan F Task 7: --force must NOT infinite-loop when batch_size < total rows.
    Cursor-based pagination advances past each batch."""
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(5):
            c = Call(
                source_id=src.id,
                external_id=f"force-page-{i}",
                url=f"https://x/{i}",
                title=f"FP{i}",
                summary="x",
                published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            # All pre-embedded under current model — without cursor pagination,
            # --force would infinite-loop on the first batch.
            c.embedding = [0.9] * 1024
            c.embedding_model = "bge-m3"
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = _embed_result([0.1] * 1024)

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill", "--force", "--batch-size", "2"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    # Each row re-embedded exactly once
    assert fake_client.embed.call_count == 5


def test_cli_backfill_returns_nonzero_on_persistent_failures(pg_session_factory):
    """If any row fails to embed (LLM down etc), exit 1 so deploy scripts catch it."""
    from grant_lab.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        _seed(s, n_calls=1, n_profiles=0)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = LLMError("down")

    with patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1


def test_cli_backfill_force_keeps_going_after_first_all_failed_batch(pg_session_factory):
    """Codex Plan F regression: in --force mode, the CLI must NOT stop
    after a batch of all-failed rows. Old termination ("no successful
    embeds") caused immediate exit when the first cursor-paginated batch
    had only failures, leaving later rows untried. New termination uses
    "no rows processed" (cursors exhausted) in force mode."""
    call_count = {"n": 0}

    def fake_tick(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First batch: all-failed (5 calls). Cursor advances to id=5.
            return {
                "calls_embedded": 0,
                "calls_failed": 5,
                "profiles_embedded": 0,
                "profiles_failed": 0,
                "max_call_id": 5,
                "max_profile_id": 0,
            }
        if call_count["n"] == 2:
            # Second batch (would have been skipped by old logic): all succeeded.
            return {
                "calls_embedded": 5,
                "calls_failed": 0,
                "profiles_embedded": 0,
                "profiles_failed": 0,
                "max_call_id": 10,
                "max_profile_id": 0,
            }
        # Third batch: nothing left to process — terminate.
        return {
            "calls_embedded": 0,
            "calls_failed": 0,
            "profiles_embedded": 0,
            "profiles_failed": 0,
            "max_call_id": 10,
            "max_profile_id": 0,
        }

    fake_client = MagicMock()
    fake_client.model = "bge-m3"

    with (
        patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client),
        patch("grant_lab.embeddings.cli.refresh_missing_embeddings_tick", side_effect=fake_tick),
    ):
        exit_code = backfill_main(
            argv=["backfill", "--force"],
            session_factory=pg_session_factory,
        )
    # 3 ticks total: failed batch did NOT stop us, success batch ran, empty batch ended loop.
    assert call_count["n"] == 3
    # Exit 1 because there were failures (5 failed in batch 1).
    assert exit_code == 1
