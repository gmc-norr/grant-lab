"""Tests for the atomic rename_citekey tool (Slice 2 Task 7).

Exercises rename_citekey_impl directly (plain dicts, no MCP envelope). Reuses
the seeding / _init_repo / ToolContext pattern from test_citations_insert.py:
insert real citations first (so markers + ledger rows exist), then rename.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session as SASession

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.ledger import find_by_citekey, load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_manager.proposal_assist.tools import citations as citations_mod
from call_manager.proposal_assist.tools.citations import (
    CITATIONS_FILENAME,
    insert_citation_impl,
    rename_citekey_impl,
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
ANCHOR_A = "recurrence is reduced substantially"
ANCHOR_B = "a second distinct claim about the same paper"
PROPOSAL = (
    f"# Proposal\n\nWe argue that {ANCHOR_A} in our population.\n\nFurthermore {ANCHOR_B} holds.\n"
)


@pytest.mark.asyncio
async def test_happy_path_renames_both_markers_and_rows(
    pg_session_factory, _settings_env, tmp_path
):
    """A reference cited twice (two markers, two rows, same citekey) → rename
    rewrites BOTH markers in proposal.md and BOTH ledger rows in one new commit;
    tree clean; references.citekey updated in DB; markers_renamed == 2."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        citekey="oldkey2021",
    )

    r1 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r1, r1
    r2 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_B,
        chunk_index=0,
        evidence_quote="Secondary endpoints were not statistically significant",
    )
    assert "error" not in r2, r2
    assert r1["citekey"] == r2["citekey"] == "oldkey2021"

    before_head = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="oldkey2021", new_citekey="newkey2022")

    assert "error" not in result, result
    assert result["old_citekey"] == "oldkey2021"
    assert result["new_citekey"] == "newkey2022"
    assert result["markers_renamed"] == 2
    assert result["ledger_rows_renamed"] == 2
    assert result["commit_sha"] and result["commit_sha"] != before_head

    # Both markers in proposal.md now carry the new key (ev ids preserved).
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 2
    assert all(m["citekey"] == "newkey2022" for m in markers)
    assert {m["ev_id"] for m in markers} == {r1["ev_id"], r2["ev_id"]}
    assert "oldkey2021" not in proposal_text

    # Both ledger rows now carry the new key; none keep the old.
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert find_by_citekey(ledger, "oldkey2021") == []
    renamed_rows = find_by_citekey(ledger, "newkey2022")
    assert len(renamed_rows) == 2
    assert {row["id"] for row in renamed_rows} == {r1["ev_id"], r2["ev_id"]}

    # Exactly one new commit, clean tree.
    assert _commit_count(ctx.repo_dir) == before_count + 1
    assert _tree_is_clean(ctx.repo_dir)

    # references.citekey updated in DB.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.citekey == "newkey2022"


@pytest.mark.asyncio
async def test_old_citekey_not_present_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="present2021"
    )
    await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    before = _head(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="absent9999", new_citekey="whatever2030")

    assert result["error"] == "citekey not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_new_citekey_already_in_use_no_commit(pg_session_factory, _settings_env, tmp_path):
    """Two different references with two different citekeys; renaming one onto
    the other's key is rejected (collision against the ledger)."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid_a = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="alpha2021"
    )
    rid_b = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="beta2022"
    )
    ra = await insert_citation_impl(
        ctx,
        reference_id=str(rid_a),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in ra, ra
    rb = await insert_citation_impl(
        ctx,
        reference_id=str(rid_b),
        anchor=ANCHOR_B,
        chunk_index=0,
        evidence_quote="Secondary endpoints were not statistically significant",
    )
    assert "error" not in rb, rb

    before = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="alpha2021", new_citekey="beta2022")

    assert result["error"] == "new citekey already in use"
    assert _head(ctx.repo_dir) == before
    assert _commit_count(ctx.repo_dir) == before_count
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_new_citekey_in_db_but_not_ledger_collision(
    pg_session_factory, _settings_env, tmp_path
):
    """The new key is free in the ledger but already taken by a references.citekey
    in this workspace (no ledger row for it) → rejected via the DB-side collision
    branch; no commit, clean tree."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    # A reference whose citekey is taken in the DB but is NOT cited (no ledger row).
    _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="taken2020")
    # A separate reference that IS cited under a different citekey.
    rid_other = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="other2021"
    )
    r = await insert_citation_impl(
        ctx,
        reference_id=str(rid_other),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r, r

    before = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="other2021", new_citekey="taken2020")

    assert result["error"] == "new citekey already in use"
    assert _head(ctx.repo_dir) == before
    assert _commit_count(ctx.repo_dir) == before_count
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_old_equals_new_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="same2021"
    )
    await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    before = _head(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="same2021", new_citekey="same2021")

    assert result["error"] == "old and new citekey are the same"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_invalid_new_citekey_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="valid2021"
    )
    await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    before = _head(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="valid2021", new_citekey="bad]key")

    assert result["error"] == "invalid new citekey"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_invalid_old_citekey_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="valid2021"
    )
    await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    before = _head(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="bad]key", new_citekey="valid2099")

    assert result["error"] == "invalid old citekey"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_rename_succeeds_while_workspace_advisory_lock_held(
    pg_session_factory, _settings_env, tmp_path
):
    """Regression: rename_citekey must NOT re-acquire the controller's
    connection-scoped pg advisory lock. With that lock held by a separate
    session (as during a real turn), rename now SUCCEEDS — it serializes on the
    ToolContext asyncio.Lock instead of erroring as "busy"."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="lock2021"
    )
    r = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r, r
    count_before = _commit_count(ctx.repo_dir)

    holder = pg_session_factory()
    try:
        assert acquire_workspace_lock(holder, wsid) is True
        result = await rename_citekey_impl(ctx, old_citekey="lock2021", new_citekey="lock2099")
        assert "error" not in result, result
        assert result["new_citekey"] == "lock2099"

        proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert markers and all(m["citekey"] == "lock2099" for m in markers)
        ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
        assert find_by_citekey(ledger, "lock2099")
        assert find_by_citekey(ledger, "lock2021") == []
        assert _commit_count(ctx.repo_dir) == count_before + 1
        assert _tree_is_clean(ctx.repo_dir)
    finally:
        release_workspace_lock(holder, wsid)
        holder.close()


@pytest.mark.asyncio
async def test_db_cache_failure_still_succeeds(
    pg_session_factory, _settings_env, tmp_path, monkeypatch
):
    """A DB-cache (references.citekey) write failure AFTER the atomic git commit
    must NOT fail the tool or corrupt git. Git is canonical; reconciliation
    backfills the cache. Same arming technique as the insert suite."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)], citekey="cache2021"
    )
    r1 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r1, r1

    real_commit = SASession.commit
    real_write_and_commit = gitio.write_and_commit_files

    def boom_commit(self, *a, **kw):
        raise RuntimeError("simulated DB cache write failure")

    def wrapped_write_and_commit(*args, **kwargs):
        sha = real_write_and_commit(*args, **kwargs)
        # Arm the failure only AFTER git has committed.
        monkeypatch.setattr(SASession, "commit", boom_commit)
        return sha

    monkeypatch.setattr(citations_mod, "write_and_commit_files", wrapped_write_and_commit)

    before_count = _commit_count(ctx.repo_dir)

    result = await rename_citekey_impl(ctx, old_citekey="cache2021", new_citekey="cache2099")

    # Restore so teardown / later DB reads work normally.
    monkeypatch.setattr(SASession, "commit", real_commit)

    # Tool succeeded despite the cache write failure.
    assert "error" not in result, result
    assert result["new_citekey"] == "cache2099"
    assert result["commit_sha"]

    # Git rename persisted: HEAD advanced by 1, marker carries the new key.
    assert _commit_count(ctx.repo_dir) == before_count + 1
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 1
    assert markers[0]["citekey"] == "cache2099"
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert find_by_citekey(ledger, "cache2099")
    assert find_by_citekey(ledger, "cache2021") == []

    # Working tree clean.
    assert _tree_is_clean(ctx.repo_dir)

    # The cache write was rolled back: references.citekey stays the OLD value
    # (ledger remains canonical; reconciliation will backfill).
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.citekey == "cache2021"
