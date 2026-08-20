"""Tests for the atomic insert_citation tool (Slice 2 Task 5).

Exercises the *_impl directly (plain dicts, no MCP envelope). Uses a real
git repo in tmp_path (_init_repo) plus the pgvector testcontainer
(pg_session_factory) to seed References + ReferenceChunks.
"""

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session as SASession

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import core as citations_mod
from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.core import insert_citation_impl
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.citations.quotes import normalize_for_quote_match
from call_manager.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
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
    """Seed a Reference + its ReferenceChunks. chunks is a list of
    (chunk_index, section_path, text). Returns the reference id."""
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
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = f"# Proposal\n\nWe argue that {ANCHOR} in our population.\n"


@pytest.mark.asyncio
async def test_happy_path_inserts_marker_and_ledger_row(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    src_sha = hashlib.sha256(b"original bytes").hexdigest()
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={
            "author": [{"family": "Smith"}],
            "issued": {"date-parts": [[2021]]},
        },
        original_sha256=src_sha,
    )

    before_head = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )

    assert "error" not in result, result
    assert result["ev_id"].startswith("ev-")
    assert result["citekey"] == "smith2021"
    assert result["commit_sha"] and result["commit_sha"] != before_head

    # Marker is in proposal.md right after the anchor.
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    marker = f"[@smith2021]<!-- ev:{result['ev_id']} -->"
    assert ANCHOR + marker in proposal_text

    # Ledger has exactly one row with the right fields.
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
    row = ledger["evidence"][0]
    assert row["id"] == result["ev_id"]
    assert row["citekey"] == "smith2021"
    assert row["reference_id"] == str(rid)
    assert row["locator"]["chunk_index"] == 0
    assert row["locator"]["section_path"] == "Results"
    assert row["evidence_quote"] == "reduces recurrence by 30%"
    assert row["stance"] == "supports"
    assert row["source_content_hash"] == src_sha

    # Offsets round-trip via normalize.
    start, end = row["evidence_quote_offsets"]
    assert normalize_for_quote_match(CHUNK_TEXT[start:end]) == normalize_for_quote_match(
        "reduces recurrence by 30%"
    )
    # chunk_content_hash == sha256(chunk.text) bare hex.
    assert row["chunk_content_hash"] == hashlib.sha256(CHUNK_TEXT.encode("utf-8")).hexdigest()

    # claim_context is the LIVE claim sentence around the inserted marker (the
    # canonical extractor — NOT the bare anchor), so a fresh insert agrees with a
    # later reaccept_claim and never immediately looks "drifted". The anchor is a
    # substring of that sentence; the trailing period/newline are stripped.
    claim_text = row["claim_context"]["text"]
    # The marker trails the claim, so extraction walks from the sentence start to
    # the marker position (text AFTER the marker, " in our population.", is not
    # part of the claim the marker attaches to).
    assert claim_text == "We argue that recurrence is reduced substantially"
    assert ANCHOR in claim_text
    assert claim_text != ANCHOR  # leading "We argue that " included, not bare anchor
    assert (
        row["claim_context"]["hash"]
        == hashlib.sha256(normalize_for_quote_match(claim_text).encode("utf-8")).hexdigest()
    )

    # Exactly one new commit, clean tree.
    assert _commit_count(ctx.repo_dir) == before_count + 1
    assert _tree_is_clean(ctx.repo_dir)

    # references.citekey now set in DB.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.citekey == "smith2021"


@pytest.mark.asyncio
async def test_tombstoned_reference_rejected_no_commit(pg_session_factory, _settings_env, tmp_path):
    """A tombstoned reference cannot be cited: insert returns an error BEFORE
    any write (no commit, clean tree)."""
    from datetime import UTC, datetime

    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        ref.tombstoned_at = datetime.now(UTC)
        s.commit()

    before_head = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" in result
    assert "tombstoned" in result["error"]
    assert _head(ctx.repo_dir) == before_head
    assert _commit_count(ctx.repo_dir) == before_count
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_anchor_not_found_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor="this anchor does not exist",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert result["error"] == "anchor not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_anchor_ambiguous_no_commit(pg_session_factory, _settings_env, tmp_path):
    dup = "the shared phrase"
    proposal = f"# P\n\nFirst {dup} here. Second {dup} there.\n"
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, proposal)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=dup,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert result["error"] == "anchor is ambiguous"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_quote_not_found_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="this quote is not in the chunk at all",
    )
    assert result["error"] == "evidence quote not found in chunk"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_quote_ambiguous_no_commit(pg_session_factory, _settings_env, tmp_path):
    chunk = "alpha beta gamma. alpha beta gamma again."
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, chunk)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="alpha beta gamma",
    )
    assert result["error"] == "evidence quote is ambiguous (occurs multiple times) in chunk"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_reference_in_another_workspace_not_found(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    # Seed the reference under a DIFFERENT workspace.
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        other_owner = make_user(s, email=f"other-{uuid.uuid4()}@ki.se")
        s.commit()
        other_ws = make_workspace(s, owner=other_owner)
        s.commit()
        other_wsid = other_ws.id
    rid = _seed_reference(pg_session_factory, other_wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert result["error"] == "reference not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_chunk_index_missing_no_commit(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=99,
        evidence_quote="reduces recurrence by 30%",
    )
    assert result["error"] == "chunk not found"
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_bad_uuid_returns_error(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id="not-a-uuid",
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" in result
    assert _head(ctx.repo_dir) == before


@pytest.mark.asyncio
async def test_db_cache_update_failure_still_succeeds(
    pg_session_factory, _settings_env, tmp_path, monkeypatch
):
    """The spec's headline recovery guarantee: a DB-cache (references.citekey)
    write failure AFTER the atomic git commit must NOT fail the tool or corrupt
    git. Git is canonical; reconciliation backfills the cache.

    We force the failure by wrapping the citations module's write_and_commit_files
    so that the moment the real git commit returns, Session.commit is patched to
    raise. Step 14 (ref.citekey = ...; session.commit()) then blows up. Reads
    (session.get/query) and the advisory-lock acquire (session.execute) never call
    commit, so they are unaffected — only the post-git cache write raises.
    """
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    # No citekey on the ref → step 14 will attempt ref.citekey = ...; commit().
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={
            "author": [{"family": "Smith"}],
            "issued": {"date-parts": [[2021]]},
        },
    )

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

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )

    # Restore so teardown / later DB reads work normally.
    monkeypatch.setattr(SASession, "commit", real_commit)

    # (a) Tool succeeded: ev_id/citekey/commit_sha present, no error.
    assert "error" not in result, result
    assert result["ev_id"].startswith("ev-")
    assert result["citekey"] == "smith2021"
    assert result["commit_sha"]

    # (b) Git commit persisted: HEAD advanced by 1, marker + ledger row present.
    assert _commit_count(ctx.repo_dir) == before_count + 1
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    marker = f"[@smith2021]<!-- ev:{result['ev_id']} -->"
    assert ANCHOR + marker in proposal_text
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
    assert ledger["evidence"][0]["id"] == result["ev_id"]

    # (c) Working tree clean.
    assert _tree_is_clean(ctx.repo_dir)

    # The cache write was rolled back: references.citekey stays NULL (the
    # ledger remains canonical; reconciliation will backfill).
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.citekey is None


@pytest.mark.asyncio
async def test_insert_succeeds_while_workspace_advisory_lock_held(
    pg_session_factory, _settings_env, tmp_path
):
    """Regression for the connection-scoped advisory-lock bug.

    During a real drafting turn the controller holds the per-workspace Postgres
    advisory lock on a long-lived connection for the WHOLE turn. The citation
    mutation tools run in-process during that turn; if they re-acquire the same
    advisory key on a fresh pool connection, pg_try_advisory_lock returns FALSE
    (it is per-CONNECTION) and every mutation fails as "workspace busy".

    We reproduce that condition: acquire the workspace advisory lock on a SEPARATE
    holder session FIRST (held open for the duration), THEN call insert. The fix
    makes insert serialize on the ToolContext asyncio.Lock + a plain session
    instead of re-acquiring the pg lock, so it SUCCEEDS here. Before the fix this
    returned {"error": "...busy..."} and made no commit — this test is the proof.
    """
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={
            "author": [{"family": "Smith"}],
            "issued": {"date-parts": [[2021]]},
        },
    )

    before_head = _head(ctx.repo_dir)
    before_count = _commit_count(ctx.repo_dir)

    holder = pg_session_factory()
    try:
        # The controller's long-lived lock_session — held OPEN across the insert.
        assert acquire_workspace_lock(holder, wsid) is True

        result = await insert_citation_impl(
            ctx,
            reference_id=str(rid),
            anchor=ANCHOR,
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
        )

        # The insert must SUCCEED despite the externally-held advisory lock.
        assert "error" not in result, result
        assert result["ev_id"].startswith("ev-")
        assert result["citekey"] == "smith2021"
        assert result["commit_sha"] and result["commit_sha"] != before_head

        # Marker present in proposal.md, row present in citations.json, HEAD moved.
        proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
        marker = f"[@smith2021]<!-- ev:{result['ev_id']} -->"
        assert ANCHOR + marker in proposal_text
        ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
        assert len(ledger["evidence"]) == 1
        assert ledger["evidence"][0]["id"] == result["ev_id"]
        assert _commit_count(ctx.repo_dir) == before_count + 1
        assert _tree_is_clean(ctx.repo_dir)
    finally:
        release_workspace_lock(holder, wsid)
        holder.close()


@pytest.mark.asyncio
async def test_concurrent_inserts_serialize_no_corruption(
    pg_session_factory, _settings_env, tmp_path
):
    """Two concurrent insert_citation_impl calls on ONE ctx must serialize on the
    ToolContext asyncio.Lock and BOTH succeed without corrupting the
    read-modify-write of proposal.md + citations.json.

    Both markers end up in proposal.md, both rows in citations.json, and the git
    history is consistent (two new commits, clean tree). gather ordering is
    nondeterministic, so we assert set-membership rather than order.
    """
    import asyncio

    anchor2 = "a second distinct claim sentence"
    proposal = f"# Proposal\n\nWe argue that {ANCHOR} here.\n\nAlso {anchor2} too.\n"
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, proposal)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        citekey="preset2020",
    )

    before_count = _commit_count(ctx.repo_dir)

    r1, r2 = await asyncio.gather(
        insert_citation_impl(
            ctx,
            reference_id=str(rid),
            anchor=ANCHOR,
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
        ),
        insert_citation_impl(
            ctx,
            reference_id=str(rid),
            anchor=anchor2,
            chunk_index=0,
            evidence_quote="Secondary endpoints were not statistically significant",
        ),
    )

    assert "error" not in r1, r1
    assert "error" not in r2, r2
    ev_ids = {r1["ev_id"], r2["ev_id"]}
    assert len(ev_ids) == 2

    # Both markers present in proposal.md (set-membership; order is nondeterministic).
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert {m["ev_id"] for m in markers} == ev_ids

    # Both rows present in citations.json.
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert {row["id"] for row in ledger["evidence"]} == ev_ids

    # Two new commits, clean tree — the RMW was not corrupted by interleaving.
    assert _commit_count(ctx.repo_dir) == before_count + 2
    assert _tree_is_clean(ctx.repo_dir)


@pytest.mark.asyncio
async def test_insert_with_fallback_citekey_when_csl_thin(
    pg_session_factory, _settings_env, tmp_path
):
    """When CSL is too thin for an author+year key, generate_citekey returns a
    'ref-<12hex>' fallback. That fallback must satisfy the marker validators and
    round-trip through find_markers, and references.citekey must be set in the DB.
    """
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={},  # thin: no author / no year → ref-<hex> fallback
    )

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )

    assert "error" not in result, result
    citekey = result["citekey"]
    assert re.fullmatch(r"ref-[0-9a-f]{12}", citekey), citekey

    # Marker with the fallback key is present in proposal.md ...
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    marker = f"[@{citekey}]<!-- ev:{result['ev_id']} -->"
    assert ANCHOR + marker in proposal_text

    # ... and round-trips through the marker validators.
    markers = find_markers(proposal_text)
    assert len(markers) == 1
    assert markers[0]["citekey"] == citekey
    assert markers[0]["ev_id"] == result["ev_id"]

    # references.citekey set in the DB.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.citekey == citekey


@pytest.mark.asyncio
async def test_citekey_reuse_when_already_set(pg_session_factory, _settings_env, tmp_path):
    anchor2 = "a second distinct claim sentence"
    proposal = f"# Proposal\n\nWe argue that {ANCHOR} here.\n\nAlso {anchor2} too.\n"
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, proposal)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, None, CHUNK_TEXT)],
        citekey="preset2020",
    )

    r1 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert r1["citekey"] == "preset2020"

    r2 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=anchor2,
        chunk_index=0,
        evidence_quote="Secondary endpoints were not statistically significant",
    )
    assert r2["citekey"] == "preset2020"
    assert r2["ev_id"] != r1["ev_id"]

    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 2
    assert all(m["citekey"] == "preset2020" for m in markers)
    assert {m["ev_id"] for m in markers} == {r1["ev_id"], r2["ev_id"]}

    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 2


@pytest.mark.asyncio
async def test_idempotency_same_args_no_duplicate(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, None, CHUNK_TEXT)],
        citekey="dup2019",
    )
    r1 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r1
    count_after_first = _commit_count(ctx.repo_dir)

    r2 = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in r2
    assert r2["ev_id"] == r1["ev_id"]
    assert r2.get("idempotent") is True

    # No new commit, single marker, single row.
    assert _commit_count(ctx.repo_dir) == count_after_first
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    assert len(find_markers(proposal_text)) == 1
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1


@pytest.mark.asyncio
async def test_whitespace_tolerant_quote_stores_raw_offsets(
    pg_session_factory, _settings_env, tmp_path
):
    # Chunk has a newline inside the quoted span.
    chunk = "Adjuvant therapy reduces recurrence\nby 30% in the treated cohort."
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, None, chunk)],
        citekey="ws2022",
    )
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" not in result, result
    ledger = load_ledger((ctx.repo_dir / CITATIONS_FILENAME).read_text())
    row = ledger["evidence"][0]
    start, end = row["evidence_quote_offsets"]
    raw_span = chunk[start:end]
    assert "\n" in raw_span  # raw offsets span the newline
    assert normalize_for_quote_match(raw_span) == "reduces recurrence by 30%"


@pytest.mark.asyncio
async def test_insert_self_heals_on_ledger_citekey_collision(
    pg_session_factory, _settings_env, tmp_path
):
    """A prior best-effort cache write failure can leave R1's DB citekey NULL
    while the ledger already binds 'smith2021' to R1. Inserting a citation for a
    DIFFERENT same-author reference R2 must NOT hard-error; it self-heals by
    bumping the suffix to a key free in BOTH the ledger and the DB (smith2021a).
    """
    anchor2 = "a second distinct claim sentence"
    proposal = f"# Proposal\n\nWe argue that {ANCHOR} here.\n\nAlso {anchor2} too.\n"
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, proposal)

    smith_csl = {
        "author": [{"family": "Smith"}],
        "issued": {"date-parts": [[2021]]},
    }
    # R1: same-author/year, DB citekey NULL (simulating a stranded cache).
    rid1 = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], csl_json=smith_csl
    )
    # R2: same author/year → generate_citekey would yield "smith2021" too.
    rid2 = _seed_reference(
        pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)], csl_json=smith_csl
    )

    # Pre-seed the ledger so a row binds "smith2021" to R1 (DB cache NULL).
    from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger

    ledger = {
        **empty_ledger(),
        "evidence": [
            {"id": "ev-preexisting", "citekey": "smith2021", "reference_id": str(rid1)},
        ],
    }
    (ctx.repo_dir / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    subprocess.run(["git", "add", CITATIONS_FILENAME], cwd=ctx.repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed ledger"], cwd=ctx.repo_dir, check=True)

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid2),
        anchor=anchor2,
        chunk_index=0,
        evidence_quote="Secondary endpoints were not statistically significant",
    )

    assert "error" not in result, result
    assert result["citekey"] == "smith2021a", result
    proposal_text = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    assert f"[@smith2021a]<!-- ev:{result['ev_id']} -->" in proposal_text

    with pg_session_factory() as s:
        assert s.get(Reference, rid2).citekey == "smith2021a"


@pytest.mark.asyncio
async def test_unknown_stance_rejected(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, None, CHUNK_TEXT)])
    before = _head(ctx.repo_dir)
    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="bogus",
    )
    assert "error" in result
    assert _head(ctx.repo_dir) == before
    assert _tree_is_clean(ctx.repo_dir)
