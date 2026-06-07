"""Tests for the atomic remove_citation + update_citation tools (Slice 2 Task 6).

Exercises the *_impl directly (plain dicts, no MCP envelope). Reuses the seeding
/ _init_repo / ToolContext pattern from test_citations_insert.py. Each test first
inserts a real citation (via insert_citation_impl) to obtain a genuine
ev_id / marker / ledger row, then exercises remove / update.
"""

import hashlib
import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.ledger import find_by_id, load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.citations.quotes import normalize_for_quote_match
from call_manager.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_manager.proposal_assist.tools.citations import (
    CITATIONS_FILENAME,
    insert_citation_impl,
    remove_citation_impl,
    update_citation_impl,
)
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _head(repo_dir: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit_count(repo_dir: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _tree_is_clean(repo_dir: Path) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == ""


def _seed_reference(
    pg_session_factory,
    workspace_id,
    *,
    chunks: list[tuple[int, str | None, str]],
    citekey: str | None = None,
    csl_json: dict | None = None,
    original_sha256: str | None = None,
) -> uuid.UUID:
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            csl_json=csl_json,
            original_sha256=original_sha256,
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        for idx, section_path, text in chunks:
            s.add(
                ReferenceChunk(
                    reference_id=rid,
                    workspace_id=workspace_id,
                    chunk_index=idx,
                    section_path=section_path,
                    text=text,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
        s.commit()
    return rid


def _build_ctx(pg_session_factory, tmp_path, initial_proposal):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, initial_proposal)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
    )
    return ctx, wsid


CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
CHUNK_TEXT_2 = (
    "A larger replication study confirmed a 25% reduction across all arms. "
    "No serious adverse events were reported."
)
ANCHOR = "recurrence is reduced substantially"
ANCHOR_2 = "a second distinct claim sentence"
PROPOSAL = f"# Proposal\n\nWe argue that {ANCHOR} in our population.\n"
PROPOSAL_TWO_ANCHORS = f"# Proposal\n\nWe argue that {ANCHOR} here.\n\nAlso {ANCHOR_2} too.\n"


async def _insert_one(
    ctx,
    rid,
    *,
    anchor=ANCHOR,
    chunk_index=0,
    evidence_quote="reduces recurrence by 30%",
    stance="supports",
    confidence=None,
):
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=anchor,
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        stance=stance,
        confidence=confidence,
    )
    assert "error" not in result, result
    return result


# ---------------------------------------------------------------------------
# remove_citation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_happy_path(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    ev_id = ins["ev_id"]
    head_after_insert = _head(ctx.repo_dir)
    count_after_insert = _commit_count(ctx.repo_dir)

    result = await remove_citation_impl(ctx, evidence_id=ev_id)

    assert "error" not in result, result
    assert result["ev_id"] == ev_id
    assert result["commit_sha"] and result["commit_sha"] != head_after_insert
    assert result["commit_sha"] == _head(ctx.repo_dir)

    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    assert ev_id not in proposal_text
    assert find_markers(proposal_text) == []

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert find_by_id(ledger, ev_id) is None
    assert ledger["evidence"] == []

    assert _commit_count(ctx.repo_dir) == count_after_insert + 1
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_remove_not_found_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="smith2021"
    )
    await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    result = await remove_citation_impl(ctx, evidence_id="ev-doesnotexist0")

    assert result["error"] == "citation not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_remove_one_of_two_same_citekey(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL_TWO_ANCHORS)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="preset2020"
    )
    r1 = await _insert_one(ctx, rid, anchor=ANCHOR)
    r2 = await _insert_one(
        ctx,
        rid,
        anchor=ANCHOR_2,
        evidence_quote="Secondary endpoints were not statistically significant",
    )

    result = await remove_citation_impl(ctx, evidence_id=r1["ev_id"])
    assert "error" not in result, result

    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 1
    assert markers[0]["ev_id"] == r2["ev_id"]
    assert markers[0]["citekey"] == "preset2020"

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert find_by_id(ledger, r1["ev_id"]) is None
    assert find_by_id(ledger, r2["ev_id"]) is not None
    assert len(ledger["evidence"]) == 1


@pytest.mark.asyncio
async def test_remove_lock_busy_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    holder = pg_session_factory()
    try:
        assert acquire_workspace_lock(holder, wsid) is True
        result = await remove_citation_impl(ctx, evidence_id=ins["ev_id"])
        assert "error" in result
        err = result["error"].lower()
        assert "busy" in err or "retry" in err
        assert _head(ctx.repo_dir) == before
        assert _tree_is_clean(ctx.repo_dir)
    finally:
        release_workspace_lock(holder, wsid)
        holder.close()


# ---------------------------------------------------------------------------
# update_citation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_stance_only(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid, stance="supports")
    ev_id = ins["ev_id"]

    ledger0 = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    old_updated_at = find_by_id(ledger0, ev_id)["updated_at"]
    proposal_before = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    count_before = _commit_count(ctx.repo_dir)

    result = await update_citation_impl(ctx, evidence_id=ev_id, stance="background")

    assert "error" not in result, result
    assert result["ev_id"] == ev_id
    assert "stance" in result["updated_fields"]
    assert "updated_at" in result["updated_fields"]

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(ledger, ev_id)
    assert row["stance"] == "background"
    assert row["updated_at"] != old_updated_at

    # marker unchanged in proposal.md (citekey unchanged); only citations.json moved
    assert (ctx.repo_dir / PROPOSAL_FILENAME).read_text() == proposal_before
    assert _commit_count(ctx.repo_dir) == count_before + 1
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_chunk_and_quote_reverifies(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT), (1, "Discussion", CHUNK_TEXT_2)],
        citekey="smith2021",
    )
    ins = await _insert_one(ctx, rid, chunk_index=0, evidence_quote="reduces recurrence by 30%")
    ev_id = ins["ev_id"]

    result = await update_citation_impl(
        ctx,
        evidence_id=ev_id,
        chunk_index=1,
        evidence_quote="25% reduction across all arms",
    )
    assert "error" not in result, result

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(ledger, ev_id)
    assert row["locator"]["chunk_index"] == 1
    assert row["locator"]["section_path"] == "Discussion"
    assert row["evidence_quote"] == "25% reduction across all arms"
    assert row["chunk_content_hash"] == hashlib.sha256(CHUNK_TEXT_2.encode("utf-8")).hexdigest()
    start, end = row["evidence_quote_offsets"]
    assert normalize_for_quote_match(CHUNK_TEXT_2[start:end]) == normalize_for_quote_match(
        "25% reduction across all arms"
    )
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_quote_not_in_new_chunk(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT), (1, "Discussion", CHUNK_TEXT_2)],
        citekey="smith2021",
    )
    ins = await _insert_one(ctx, rid, chunk_index=0)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(
        ctx,
        evidence_id=ins["ev_id"],
        chunk_index=1,
        evidence_quote="this phrase is absent from chunk 1",
    )
    assert result["error"] == "evidence quote not found in chunk"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_new_quote_bad_in_same_chunk(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid, chunk_index=0)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(
        ctx,
        evidence_id=ins["ev_id"],
        evidence_quote="totally not present quote",
    )
    assert result["error"] == "evidence quote not found in chunk"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_chunk_not_found(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid, chunk_index=0)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(
        ctx,
        evidence_id=ins["ev_id"],
        chunk_index=99,
        evidence_quote="reduces recurrence by 30%",
    )
    assert result["error"] == "chunk not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_unknown_stance(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(ctx, evidence_id=ins["ev_id"], stance="bogus")
    assert "error" in result
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_unknown_confidence(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(ctx, evidence_id=ins["ev_id"], confidence="bogus")
    assert "error" in result
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_reaccept_claim(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    ev_id = ins["ev_id"]

    ledger0 = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    old_claim = find_by_id(ledger0, ev_id)["claim_context"]

    result = await update_citation_impl(ctx, evidence_id=ev_id, reaccept_claim=True)
    assert "error" not in result, result
    assert "claim_context" in result["updated_fields"]

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(ledger, ev_id)
    new_claim = row["claim_context"]
    # claim text is re-snapshotted from the live sentence around the marker, so it
    # should now be the full sentence (longer than the original bare anchor).
    assert new_claim["text"] != old_claim["text"]
    assert ANCHOR in new_claim["text"]
    assert (
        new_claim["hash"]
        == hashlib.sha256(normalize_for_quote_match(new_claim["text"]).encode("utf-8")).hexdigest()
    )
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_noop_returns_error(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(ctx, evidence_id=ins["ev_id"])
    assert "error" in result
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_not_found(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    result = await update_citation_impl(ctx, evidence_id="ev-nope00000000", stance="background")
    assert result["error"] == "citation not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_lock_busy_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    before = _head(ctx.repo_dir)

    holder = pg_session_factory()
    try:
        assert acquire_workspace_lock(holder, wsid) is True
        result = await update_citation_impl(ctx, evidence_id=ins["ev_id"], stance="background")
        assert "error" in result
        err = result["error"].lower()
        assert "busy" in err or "retry" in err
        assert _head(ctx.repo_dir) == before
        assert _tree_is_clean(ctx.repo_dir)
    finally:
        release_workspace_lock(holder, wsid)
        holder.close()
