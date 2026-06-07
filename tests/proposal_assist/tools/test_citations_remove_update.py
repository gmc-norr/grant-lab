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
from call_manager.proposal_assist.citations.ledger import (
    dump_ledger,
    find_by_id,
    load_ledger,
)
from call_manager.proposal_assist.citations.markers import find_markers, remove_marker
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


def _commit_files(repo_dir: Path, files: dict[str, str], message: str) -> None:
    """Hand-write + commit files in the test repo (simulates an out-of-band edit
    the agent's tools must tolerate)."""
    for name, content in files.items():
        (repo_dir / name).write_text(content)
    subprocess.run(["git", "add", *files.keys()], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, check=True)


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
    # insert and reaccept now share ONE extractor, so re-accepting an UNCHANGED
    # proposal re-snapshots the SAME live sentence — a fresh insert must not look
    # "drifted". The claim is the full sentence around the marker, not the anchor.
    assert new_claim["text"] == old_claim["text"]
    assert new_claim["text"] == "We argue that recurrence is reduced substantially"
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


# ---------------------------------------------------------------------------
# Edge cases flagged in review (FIX 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaccept_marker_after_period_uses_preceding_sentence(
    pg_session_factory, _settings_env, tmp_path
):
    """I1 boundary: a marker that sits AFTER a sentence-ending period must
    re-snapshot the PRECEDING sentence (the one the marker trails), not the
    following one."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    ev_id = ins["ev_id"]

    # Hand-edit proposal.md so the marker lands AFTER the period. The inserted
    # marker text is stable; we move it from "...substantially[M] in our..." to
    # "...substantially.[M] Next sentence here."
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    marker = next(m for m in find_markers(proposal_text) if m["ev_id"] == ev_id)
    marker_str = proposal_text[marker["start"] : marker["end"]]
    edited = f"# Proposal\n\nWe argue that {ANCHOR}.{marker_str} Next sentence here.\n"
    _commit_files(ctx.repo_dir, {PROPOSAL_FILENAME: edited}, "move marker after period")

    result = await update_citation_impl(ctx, evidence_id=ev_id, reaccept_claim=True)
    assert "error" not in result, result

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(ledger, ev_id)
    claim_text = row["claim_context"]["text"]
    assert claim_text == f"We argue that {ANCHOR}"
    assert "Next sentence here" not in claim_text
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_reaccept_orphaned_row_falls_back_to_stored_claim(
    pg_session_factory, _settings_env, tmp_path
):
    """An orphaned ledger row (its marker hand-removed from proposal.md) must
    fall back to the stored claim text and still refresh the hash — never
    overwrite the claim with empty."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    ev_id = ins["ev_id"]

    ledger0 = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    stored = find_by_id(ledger0, ev_id)["claim_context"]

    # Hand-remove the marker from proposal.md (row stays in the ledger -> orphan).
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    stripped, removed = remove_marker(proposal_text, ev_id)
    assert removed
    _commit_files(ctx.repo_dir, {PROPOSAL_FILENAME: stripped}, "hand-remove marker")
    assert find_markers((ctx.repo_dir / PROPOSAL_FILENAME).read_text()) == []

    result = await update_citation_impl(ctx, evidence_id=ev_id, reaccept_claim=True)
    assert "error" not in result, result

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(ledger, ev_id)
    # Stored claim text preserved (not blanked); hash recomputed from it.
    assert row["claim_context"]["text"] == stored["text"]
    assert (
        row["claim_context"]["hash"]
        == hashlib.sha256(normalize_for_quote_match(stored["text"]).encode("utf-8")).hexdigest()
    )
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_remove_row_whose_marker_was_hand_removed(
    pg_session_factory, _settings_env, tmp_path
):
    """The warn-and-drop branch: removing a citation whose marker is already gone
    from proposal.md still drops the ledger row, commits, and leaves a clean
    tree (the ledger is canonical)."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid)
    ev_id = ins["ev_id"]

    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    stripped, removed = remove_marker(proposal_text, ev_id)
    assert removed
    _commit_files(ctx.repo_dir, {PROPOSAL_FILENAME: stripped}, "hand-remove marker")
    count_before = _commit_count(ctx.repo_dir)

    result = await remove_citation_impl(ctx, evidence_id=ev_id)
    assert "error" not in result, result
    assert result["ev_id"] == ev_id

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert find_by_id(ledger, ev_id) is None
    assert ledger["evidence"] == []
    # A new commit was made and the tree is clean.
    assert _commit_count(ctx.repo_dir) == count_before + 1
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_update_stance_with_malformed_locator_missing_chunk_index(
    pg_session_factory, _settings_env, tmp_path
):
    """A pre-existing row with a malformed locator (no chunk_index) must still be
    updatable for fields that don't touch the binding (e.g. stance), thanks to
    the .get() guards — not crash."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], citekey="smith2021"
    )
    ins = await _insert_one(ctx, rid, stance="supports")
    ev_id = ins["ev_id"]

    # Corrupt the locator: drop chunk_index, keep section_path.
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    for row in ledger["evidence"]:
        if row["id"] == ev_id:
            row["locator"] = {"section_path": "Results"}  # no chunk_index
    _commit_files(ctx.repo_dir, {CITATIONS_FILENAME: dump_ledger(ledger)}, "corrupt locator")

    # Changing only stance must NOT touch the binding (so .get() guards suffice).
    result = await update_citation_impl(ctx, evidence_id=ev_id, stance="background")
    assert "error" not in result, result
    assert "stance" in result["updated_fields"]

    refreshed = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = find_by_id(refreshed, ev_id)
    assert row["stance"] == "background"
    assert "chunk_index" not in row["locator"]  # untouched
    assert _tree_is_clean(ctx.repo_dir)
