"""Tests for deterministic citation validation (Slice 3 Task 3).

Pure DB/string re-checks of the canonical citations.json ledger against the live
proposal.md + chunks. No LLM. Each issue type is exercised independently:

- The CLEAN case is built via the real insert_citation_impl so the proposal.md +
  citations.json are guaranteed-consistent (the #1 correctness risk is the hash
  bases diverging from insert, which would false-positive on every clean cite).
- Other cases construct ledger rows directly (build_evidence_row) and mutate one
  thing at a time to trigger exactly one issue type.
"""

import hashlib
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.audit import (
    chunk_content_sha,
    claim_context_sha,
    evidence_quote_sha,
    validate_citations_deterministic,
)
from call_manager.proposal_assist.citations.claims import claim_sentence_around
from call_manager.proposal_assist.citations.core import insert_citation_impl
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    empty_ledger,
    load_ledger,
)
from call_manager.proposal_assist.citations.markers import build_marker, find_markers
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_user, make_workspace


def _stamp(ctx: ToolContext) -> FenceStamp:
    """The fencing identity insert_citation_impl now requires. These test
    workspaces are all at lifecycle_generation 0, so the stamp the caller would
    thread carries generation 0."""
    return FenceStamp(
        workspace_id=ctx.workspace_id,
        generation=0,
        active_turn_job_id=None,
        origin_job_id=None,
    )

EMBED_DIM = 1024

CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = f"# Proposal\n\nWe argue that {ANCHOR} in our population.\n"


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _seed_reference(
    pg_session_factory,
    workspace_id,
    *,
    chunks: list[tuple[int, str | None, str]],
    citekey: str | None = None,
    csl_json: dict | None = None,
    original_sha256: str | None = None,
    metadata_status: str = "approved",
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
            metadata_status=metadata_status,
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


def _make_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        return ws.id


def _row(
    *,
    rid,
    ev_id="ev-aaaaaaaaaaaa",
    citekey="smith2021",
    chunk_index=0,
    section_path="Results",
    source_content_hash=None,
    chunk_content_hash,
    evidence_quote="reduces recurrence by 30%",
    claim_text,
    stance="supports",
):
    """Build a v2 evidence row with caller-supplied hashes."""
    return build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(rid),
        chunk_index=chunk_index,
        section_path=section_path,
        source_content_hash=source_content_hash,
        chunk_content_hash=chunk_content_hash,
        evidence_quote=evidence_quote,
        evidence_quote_offsets=[0, 0],
        stance=stance,
        confidence=None,
        claim_context_text=claim_text,
        claim_context_hash=claim_context_sha(claim_text),
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _proposal_with_marker(claim_lead: str, ev_id="ev-aaaaaaaaaaaa", citekey="smith2021") -> str:
    """A proposal whose claim sentence is `claim_lead` followed by the marker."""
    return f"# Proposal\n\n{claim_lead}{build_marker(citekey, ev_id)}.\n"


def _live_claim_for(proposal_text: str, ev_id: str) -> str:
    m = next(x for x in find_markers(proposal_text) if x["ev_id"] == ev_id)
    return claim_sentence_around(proposal_text, m["start"], m["end"])


def _errors(issues):
    return [i for i in issues if i.get("severity") == "error"]


def _types(issues):
    return {i.get("type") for i in issues}


# --------------------------------------------------------------------------- #
# Clean case (real insert) — the byte-exact-hash guard.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clean_citation_no_errors(pg_session_factory, _settings_env, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
        original_sha256=hashlib.sha256(b"original bytes").hexdigest(),
        metadata_status="verified_external",
    )
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    result = await insert_citation_impl(
        ctx,
        stamp=_stamp(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )
    assert "error" not in result, result

    proposal_text = (repo_dir / PROPOSAL_FILENAME).read_text()
    ledger = load_ledger((repo_dir / CITATIONS_FILENAME).read_text())

    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    assert _errors(issues) == [], issues


# --------------------------------------------------------------------------- #
# Defect B7-3: empty-claim insert must NOT report spurious permanent claim_drift.
# A marker at section start (no preceding prose) extracts an empty claim; insert
# falls back to hashing the anchor, and the validator must derive the SAME hash.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_claim_insert_has_no_spurious_drift(
    pg_session_factory, _settings_env, tmp_path
):
    wsid = _make_workspace(pg_session_factory)
    repo_dir = tmp_path / "repo"
    # The anchor is the heading text itself, so the marker lands inside a heading
    # line; the heading-aware extractor (defect B7-2) yields an empty claim and
    # insert falls back to hashing the anchor.
    anchor = "Aims of the study"
    proposal = f"## {anchor}\n\nBody prose here.\n"
    _init_repo(repo_dir, proposal)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
        original_sha256=hashlib.sha256(b"original bytes").hexdigest(),
        metadata_status="verified_external",
    )
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    result = await insert_citation_impl(
        ctx,
        stamp=_stamp(ctx),
        reference_id=str(rid),
        anchor=anchor,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )
    assert "error" not in result, result

    proposal_text = (repo_dir / PROPOSAL_FILENAME).read_text()
    ledger = load_ledger((repo_dir / CITATIONS_FILENAME).read_text())
    # The live claim around the just-inserted marker is empty (heading excluded).
    assert _live_claim_for(proposal_text, result["ev_id"]) == ""

    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    drift = [i for i in issues if i["type"] == "claim_drift"]
    assert drift == [], f"spurious claim_drift on empty-claim insert: {drift}"


# --------------------------------------------------------------------------- #
# orphan_marker
# --------------------------------------------------------------------------- #
def test_orphan_marker(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text,
            ledger=empty_ledger(),
            session=s,
            workspace_id=wsid,
        )
    orphans = [i for i in issues if i["type"] == "orphan_marker"]
    assert len(orphans) == 1
    assert orphans[0]["severity"] == "error"
    assert orphans[0]["citekey"] == "smith2021"
    assert orphans[0]["evidence_id"] == "ev-aaaaaaaaaaaa"


# --------------------------------------------------------------------------- #
# orphan_row
# --------------------------------------------------------------------------- #
def test_orphan_row(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text="anything")
    ledger = append_evidence(empty_ledger(), row)
    # No marker in proposal.
    proposal_text = "# Proposal\n\nNothing cited here.\n"
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    orphans = [i for i in issues if i["type"] == "orphan_row"]
    assert len(orphans) == 1
    assert orphans[0]["severity"] == "error"
    assert orphans[0]["citekey"] == "smith2021"
    assert orphans[0]["evidence_id"] == "ev-aaaaaaaaaaaa"
    # An orphan_row must NOT also be reported as claim_drift (no live marker).
    assert "claim_drift" not in _types(issues)


# --------------------------------------------------------------------------- #
# reference_missing
# --------------------------------------------------------------------------- #
def test_reference_missing_nonexistent(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    claim_lead = "We argue that recurrence drops"
    proposal_text = _proposal_with_marker(claim_lead)
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    bogus_rid = uuid.uuid4()
    row = _row(
        rid=bogus_rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    miss = [i for i in issues if i["type"] == "reference_missing"]
    assert len(miss) == 1
    assert miss[0]["severity"] == "error"
    # Chunk/quote checks are skipped — no chunk_missing or quote_missing piled on.
    assert "chunk_missing" not in _types(issues)
    assert "quote_missing" not in _types(issues)


def test_reference_in_other_workspace_missing(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    with pg_session_factory() as s:
        other_owner = make_user(s, email=f"other-{uuid.uuid4()}@ki.se")
        s.commit()
        other_ws = make_workspace(s, owner=other_owner)
        s.commit()
        other_wsid = other_ws.id
    rid = _seed_reference(pg_session_factory, other_wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    assert "reference_missing" in _types(issues)


# --------------------------------------------------------------------------- #
# chunk_missing
# --------------------------------------------------------------------------- #
def test_chunk_missing(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(
        rid=rid,
        chunk_index=99,
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        claim_text=live_claim,
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    miss = [i for i in issues if i["type"] == "chunk_missing"]
    assert len(miss) == 1
    assert miss[0]["severity"] == "error"
    # Hash/quote checks skipped.
    assert "index_drift" not in _types(issues)
    assert "quote_missing" not in _types(issues)


# --------------------------------------------------------------------------- #
# source_replaced
# --------------------------------------------------------------------------- #
def test_source_replaced_flagged_when_both_present_and_differ(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    ref_sha = hashlib.sha256(b"new source bytes").hexdigest()
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        original_sha256=ref_sha,
    )
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(
        rid=rid,
        source_content_hash=hashlib.sha256(b"old source bytes").hexdigest(),
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        claim_text=live_claim,
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "source_replaced"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"


def test_source_replaced_not_flagged_when_ref_sha_none(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        original_sha256=None,
    )
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(
        rid=rid,
        source_content_hash=hashlib.sha256(b"some hash").hexdigest(),
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        claim_text=live_claim,
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    assert "source_replaced" not in _types(issues)


# --------------------------------------------------------------------------- #
# index_drift
# --------------------------------------------------------------------------- #
def test_index_drift_when_chunk_text_mutated(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    # Row records the ORIGINAL chunk hash...
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)
    # ... but the chunk text in the DB is changed afterward.
    with pg_session_factory() as s:
        chunk = (
            s.query(ReferenceChunk)
            .filter(
                ReferenceChunk.reference_id == rid,
                ReferenceChunk.chunk_index == 0,
                ReferenceChunk.workspace_id == wsid,
            )
            .one()
        )
        chunk.text = CHUNK_TEXT + " Extra appended sentence."
        s.commit()
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "index_drift"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"


# --------------------------------------------------------------------------- #
# quote_missing
# --------------------------------------------------------------------------- #
def test_quote_missing(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(
        rid=rid,
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        evidence_quote="this exact phrase is absent from the chunk",
        claim_text=live_claim,
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "quote_missing"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"


# --------------------------------------------------------------------------- #
# claim_drift
# --------------------------------------------------------------------------- #
def test_claim_drift_when_prose_changes(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    # Row records the claim "We argue that recurrence drops".
    row = _row(
        rid=rid,
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        claim_text="We argue that recurrence drops",
    )
    ledger = append_evidence(empty_ledger(), row)
    # But the live proposal prose around the marker now says something else.
    proposal_text = _proposal_with_marker("We now claim the opposite entirely")
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "claim_drift"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"


def test_no_claim_drift_when_prose_matches(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    claim_lead = "We argue that recurrence drops"
    proposal_text = _proposal_with_marker(claim_lead)
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    assert _errors(issues) == [], issues


# --------------------------------------------------------------------------- #
# metadata_unapproved
# --------------------------------------------------------------------------- #
def test_metadata_unapproved_warning(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        metadata_status="pending",
    )
    claim_lead = "We argue that recurrence drops"
    proposal_text = _proposal_with_marker(claim_lead)
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    warns = [i for i in issues if i["type"] == "metadata_unapproved"]
    assert len(warns) == 1
    assert warns[0]["severity"] == "warning"
    # The rest of the checks pass (no error for a pending-but-otherwise-clean cite).
    assert _errors(issues) == [], issues


# --------------------------------------------------------------------------- #
# never-raises on malformed rows
# --------------------------------------------------------------------------- #
def test_malformed_row_does_not_raise(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    # A grab-bag of malformed rows: bad uuid, missing locator, missing id.
    bad_uuid_row = {
        "id": "ev-aaaaaaaaaaaa",
        "citekey": "smith2021",
        "reference_id": "not-a-uuid",
        "evidence_quote": "x",
        "claim_context": {"text": "y", "hash": "z"},
    }
    no_locator_row = {
        "id": "ev-bbbbbbbbbbbb",
        "citekey": "jones2020",
        "reference_id": str(uuid.uuid4()),
        "evidence_quote": "x",
    }
    no_id_row = {"citekey": "noid2019", "reference_id": str(uuid.uuid4())}
    ledger = {
        "version": 2,
        "evidence": [bad_uuid_row, no_locator_row, no_id_row],
    }
    with pg_session_factory() as s:
        # Must not raise.
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    assert isinstance(issues, list)
    # The bad-uuid row should surface a reference_missing (the live-marker ev).
    assert "reference_missing" in _types(issues)


def test_empty_inputs_no_issues(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text="# Empty\n\nNo citations here.\n",
            ledger=empty_ledger(),
            session=s,
            workspace_id=wsid,
        )
    assert issues == []


# --------------------------------------------------------------------------- #
# quote_missing — QuoteAmbiguous branch
# --------------------------------------------------------------------------- #
def test_quote_ambiguous(pg_session_factory, _settings_env):
    """A chunk containing the evidence_quote TWICE triggers a quote_missing error
    whose detail distinguishes ambiguity from a genuinely absent quote."""
    wsid = _make_workspace(pg_session_factory)
    # Repeat the target phrase so find_quote_offsets raises QuoteAmbiguous.
    repeated_phrase = "reduces recurrence by 30%"
    ambiguous_chunk_text = (
        f"First mention: {repeated_phrase} in arm A. Second mention: {repeated_phrase} in arm B."
    )
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", ambiguous_chunk_text)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(
        rid=rid,
        chunk_content_hash=chunk_content_sha(ambiguous_chunk_text),
        evidence_quote=repeated_phrase,
        claim_text=live_claim,
    )
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "quote_missing"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"
    # Detail must signal ambiguity, not just "not found".
    detail = flagged[0]["detail"]
    assert "multiple" in detail or "ambiguous" in detail, detail


# --------------------------------------------------------------------------- #
# claim_drift — absent claim_context.hash (missing snapshot)
# --------------------------------------------------------------------------- #
def test_claim_drift_absent_snapshot(pg_session_factory, _settings_env):
    """A LIVE-marker row whose claim_context has no hash emits claim_drift with a
    detail that mentions the absent snapshot, and does NOT raise."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    proposal_text = _proposal_with_marker("We argue that recurrence drops")
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    # Build a row with a valid claim_text, then strip out the hash to simulate a
    # legacy/malformed row that has no claim_context.hash.
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    row["claim_context"] = None  # missing snapshot — no hash present
    ledger = append_evidence(empty_ledger(), row)
    with pg_session_factory() as s:
        # Must not raise.
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )
    flagged = [i for i in issues if i["type"] == "claim_drift"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "error"
    detail = flagged[0]["detail"]
    assert "absent" in detail or "cannot verify" in detail, detail


def test_evidence_quote_sha_is_normalized_and_bare_hex():
    # Whitespace-tolerant (matches normalize_for_quote_match), bare 64-hex,
    # and shares the same normalize-then-sha256 basis as claim_context_sha.
    a = evidence_quote_sha("reduces  recurrence\nby 30%")
    b = evidence_quote_sha("reduces recurrence by 30%")
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    # Same normalize+sha basis as the claim hash for identical input.
    assert evidence_quote_sha("a phrase") == claim_context_sha("a phrase")


# --------------------------------------------------------------------------- #
# reference_tombstoned
# --------------------------------------------------------------------------- #
def test_tombstoned_reference_emits_warning(pg_session_factory, _settings_env):
    """A citation whose reference was tombstoned AFTER insertion must trigger a
    reference_tombstoned WARNING, and the other checks (chunk/quote/hash) must
    still run — the tombstone is an additional warning, not a short-circuit."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    claim_lead = "We argue that recurrence drops"
    proposal_text = _proposal_with_marker(claim_lead)
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)

    # Tombstone the reference after the citation was created.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        ref.tombstoned_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )

    tombstoned = [i for i in issues if i["type"] == "reference_tombstoned"]
    assert len(tombstoned) == 1, issues
    assert tombstoned[0]["severity"] == "warning"
    assert tombstoned[0]["citekey"] == "smith2021"
    assert tombstoned[0]["evidence_id"] == "ev-aaaaaaaaaaaa"
    assert "tombstoned" in tombstoned[0]["detail"]

    # The citation is otherwise clean — no errors must be present.
    assert _errors(issues) == [], issues


def test_non_tombstoned_reference_no_tombstone_issue(pg_session_factory, _settings_env):
    """A live (non-tombstoned) reference must NOT produce a reference_tombstoned
    issue."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    claim_lead = "We argue that recurrence drops"
    proposal_text = _proposal_with_marker(claim_lead)
    live_claim = _live_claim_for(proposal_text, "ev-aaaaaaaaaaaa")
    row = _row(rid=rid, chunk_content_hash=chunk_content_sha(CHUNK_TEXT), claim_text=live_claim)
    ledger = append_evidence(empty_ledger(), row)

    with pg_session_factory() as s:
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=s, workspace_id=wsid
        )

    assert "reference_tombstoned" not in _types(issues)
    assert _errors(issues) == [], issues
