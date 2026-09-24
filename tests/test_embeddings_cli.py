"""CLI backfill — wraps refresh_missing_embeddings_tick + prints progress."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from grant_lab.db.models import Call, Profile, ProfileKind, Source, User
from grant_lab.embeddings.cli import backfill_main
from grant_lab.embeddings.client import EmbedResult
from grant_lab.embeddings.refresh import refresh_missing_embeddings_tick as _real_tick
from grant_lab.profiles.fingerprint import call_embedding_fingerprint, embedding_fingerprint

_user_seq = {"n": 0}


def _embed_result(vec: list[float]) -> EmbedResult:
    return EmbedResult(vector=vec, embedded_chars=1)


def _make_user(s) -> User:
    """A fresh User, unique per call within a test — Profile.created_by is
    a real FK and each Profile below needs its own owner."""
    _user_seq["n"] += 1
    u = User(email=f"5b-cli-{_user_seq['n']}@umu.se")
    s.add(u)
    s.flush()
    return u


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


def _make_capped_tick(*, max_ticks: int = 20):
    """Build a fresh wrapper around the real tick, forcing max_batches down
    to 2, with its OWN call counter (never shared across tests — a module-
    level counter would leak state between tests sharing this helper).

    Task 5b gates: `backfill_main` never passes `max_batches`, so it always
    ran at the production default (`_TICK_MAX_BATCHES` = 200) with the
    production `--batch-size` default (200) — a 40,000-row cap no test
    corpus can reach. This mirrors the technique in
    tests/test_scheduler_embedding_cursor.py's `_tick_cursors` fixture:
    drive the REAL `refresh_missing_embeddings_tick` at a small cap so a
    handful of rows can exceed it, instead of faking the tick's return
    value (which would test the CLI's arithmetic, not its behavior against
    the real staleness/cursor code Task 4/5 changed).

    Review round 1 #3: a misimplementation of the non-force termination
    (e.g. freezing one cursor at 0) previously showed up as a ~120s
    pytest-timeout hang rather than a clear failure. `max_ticks` fails fast
    with a readable message instead — both gates below need 3 CLI-level
    tick() calls to finish under the real fix, so 20 is comfortably above
    that and still fails in well under a second against a tiny corpus.
    """
    calls = {"n": 0}

    def _tick(**kwargs):
        calls["n"] += 1
        assert calls["n"] <= max_ticks, (
            f"backfill did not terminate: exceeded {max_ticks} tick() calls"
        )
        kwargs["max_batches"] = 2
        return _real_tick(**kwargs)

    return _tick


def test_cli_backfill_reaches_stale_calls_beyond_one_ticks_cap(pg_session_factory):
    """Gate 5b-1: a non-force CLI run must reach stale calls AND stale
    profiles beyond one tick's cap (here batch_size=1 * max_batches=2 =
    2 rows/half/tick), and both cursors must be carried correctly.

    Before the Task 5b fix, non-force mode passed `after_call_id=0` /
    `after_profile_id=0` to every tick call, so the second (and every
    later) CLI-loop iteration re-read the SAME lowest-id prefix instead of
    advancing past it. Two fresh calls occupy exactly that prefix here, so
    the first tick call's cap is spent finding nothing to do on the call
    side, and the old "no successful embeds -> stop" rule broke the CLI
    loop immediately, before the stale call beyond the cap was examined.

    Review round 1 #2: the call and profile corpora are deliberately
    ASYMMETRIC (3 calls, 5 profiles) so the call half runs off the end of
    its table (`call_scan_exhausted` True) a full tick before the profile
    half does. That catches two mutants the earlier symmetric corpus let
    through: (a) freezing `after_profile_id` at 0 in non-force mode, and
    (b) breaking the outer loop on `summary["call_scan_exhausted"]` alone
    instead of the joint `summary["scan_exhausted"]` — either one would
    stop this run with `StaleProfileCLI-2` never embedded.
    """
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Two calls already fresh under the current model + fingerprint —
        # lowest ids, so a 2-row capped scan consumes them first and does
        # no work. Only ONE stale call follows (asymmetric vs. 3 stale
        # profiles below), so the call half exhausts first.
        for i in range(2):
            c = Call(
                source_id=src.id,
                external_id=f"5b1-fresh-{i}",
                url=f"https://x/5b1-fresh-{i}",
                title=f"FreshCLI-{i}",
                summary="x",
                published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            c.embedding = [0.5] * 1024
            c.embedding_model = "bge-m3"
            c.embedding_fingerprint = call_embedding_fingerprint(c)
        s.add(
            Call(
                source_id=src.id,
                external_id="5b1-stale-0",
                url="https://x/5b1-stale-0",
                title="StaleCLI-0",
                summary="x",
                published_at=datetime.now(UTC),
            )
        )
        # Two profiles already fresh, three stale beyond the cap — the
        # profile half still has work after the call half is done.
        for i in range(2):
            p = Profile(
                name=f"5b1-fresh-profile-{i}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description=f"FreshProfileCLI-{i}",
                structured={},
                created_by=_make_user(s).id,
            )
            s.add(p)
            s.flush()
            p.embedding = [0.5] * 1024
            p.embedding_model = "bge-m3"
            p.embedding_fingerprint = embedding_fingerprint(p)
        for i in range(3):
            s.add(
                Profile(
                    name=f"5b1-stale-profile-{i}",
                    kind=ProfileKind.ORGANIZATIONAL_UNIT,
                    description=f"StaleProfileCLI-{i}",
                    structured={},
                    created_by=_make_user(s).id,
                )
            )
        s.commit()

    # Record embed identity on the FAKE embedder — never assert via the DB
    # vector, which a hollow "write NULL anyway" path could satisfy. Both
    # build_call_text (title + "\n\n" + summary) and build_profile_text (bare
    # description here, no areas/org) put the identifying marker first, so
    # splitting on "\n\n" recovers an exact per-row title/description.
    embedded_titles: list[str] = []

    def _embed_side_effect(text, _client=None):
        embedded_titles.append(text.split("\n\n")[0])
        return _embed_result([0.5] * 1024)

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = _embed_side_effect

    with (
        patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client),
        patch(
            "grant_lab.embeddings.cli.refresh_missing_embeddings_tick",
            side_effect=_make_capped_tick(),
        ),
    ):
        exit_code = backfill_main(
            argv=["backfill", "--batch-size", "1"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    # Exact per-row expectation: each stale row embedded exactly once, no
    # fresh row embedded at all — a superset/subset mismatch (missed row,
    # double-embed, or a fresh row slipping through) fails this equality.
    expected = sorted(["StaleCLI-0", "StaleProfileCLI-0", "StaleProfileCLI-1", "StaleProfileCLI-2"])
    assert sorted(embedded_titles) == expected


def test_cli_backfill_persistent_call_failure_does_not_block_later_calls(pg_session_factory):
    """Gate 5b-2: a call that fails every time must not stop the non-force
    CLI from reaching (and embedding) calls after it, once reaching them
    needs the cursor carried across more than one capped tick. Exit code
    stays 1 because something did fail.
    """
    from grant_lab.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Lowest id: always fails to embed.
        s.add(
            Call(
                source_id=src.id,
                external_id="5b2-fail",
                url="https://x/5b2-fail",
                title="AlwaysFailCLI",
                summary="x",
                published_at=datetime.now(UTC),
            )
        )
        # Three more calls beyond one tick's 2-row cap that must still be
        # reached and embedded despite the permanent failure ahead of them.
        for i in range(3):
            s.add(
                Call(
                    source_id=src.id,
                    external_id=f"5b2-after-{i}",
                    url=f"https://x/5b2-after-{i}",
                    title=f"AfterFailCLI-{i}",
                    summary="x",
                    published_at=datetime.now(UTC),
                )
            )
        s.commit()

    embedded_titles: list[str] = []
    fail_count = {"n": 0}

    def _embed_side_effect(text, _client=None):
        title = text.split("\n\n")[0]
        if title == "AlwaysFailCLI":
            fail_count["n"] += 1
            raise LLMError("ollama down")
        embedded_titles.append(title)
        return _embed_result([0.5] * 1024)

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = _embed_side_effect

    with (
        patch("grant_lab.embeddings.cli._build_embed_client", return_value=fake_client),
        patch(
            "grant_lab.embeddings.cli.refresh_missing_embeddings_tick",
            side_effect=_make_capped_tick(),
        ),
    ):
        exit_code = backfill_main(
            argv=["backfill", "--batch-size", "1"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1
    # The cursor moves past a failing row regardless of outcome, so it is
    # attempted exactly once across the whole run, never retried forever.
    assert fail_count["n"] == 1
    # Exact per-row expectation: each row after the failing one embedded
    # exactly once, nothing else — not just "AfterFailCLI is a substring".
    expected = sorted(["AfterFailCLI-0", "AfterFailCLI-1", "AfterFailCLI-2"])
    assert sorted(embedded_titles) == expected
