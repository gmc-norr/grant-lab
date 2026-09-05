"""User-initiated citation insert from an annotation (Task 11, spec §4).

Service-level tests for insert_from_annotation: canonical marker + ledger row
in one atomic commit, occurrence-aware idempotency (rev-3 finding 4), and the
user-facing conflict modes. Production repo layout via the test_analysis_pipeline
helpers; annotations are seeded directly (the pipeline is exercised elsewhere).
"""

import subprocess
from datetime import UTC, datetime

import pytest

from grant_lab.db.models import (
    ArtifactVersion,
    Reference,
    TurnJob,
    UserClaimAnnotation,
    WorkspaceEvent,
)
from grant_lab.proposal_assist.analysis.insert import InsertConflict, insert_from_annotation
from grant_lab.proposal_assist.citations.audit import validate_citations_deterministic
from grant_lab.proposal_assist.citations.gitio import _git_env, current_head
from grant_lab.proposal_assist.citations.ledger import (
    build_evidence_row,
    dump_ledger,
    empty_ledger,
    load_ledger,
)
from grant_lab.proposal_assist.citations.markers import find_markers
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from tests.test_analysis_pipeline import _init_repo, _seed_reference, _ws

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


def _seed_annotation(
    session,
    ws_id,
    repo,
    *,
    section_index=1,
    occurrence=0,
    kind="uncited_supported",
    claim_text=CLAIM,
    reference_id=None,
    chunk_index=0,
    quote=QUOTE,
    status="open",
):
    """Seed one annotation addressed at the claim sentence of *section_index*."""
    doc = (repo / "proposal.md").read_text()
    sec = split_sections(doc)[section_index]
    ann = UserClaimAnnotation(
        workspace_id=ws_id,
        section_sha=sec.sha,
        section_occurrence=occurrence,
        section_heading=sec.heading,
        sentence_ordinal=0,
        char_offset=sec.text.index(claim_text),
        claim_sha="0" * 64,
        claim_text=claim_text,
        kind=kind,
        evidence_id="",
        reference_id=reference_id,
        chunk_index=chunk_index,
        evidence_quote=quote,
        status=status,
    )
    session.add(ann)
    session.flush()
    return ann


def _insert(session, ws, user, ann, tmp_path):
    return insert_from_annotation(
        session=session,
        workspace=ws,
        user_id=user.id,
        annotation=ann,
        workspaces_dir=tmp_path,
    )


def test_insert_severed_when_collab_flips_after_guard(pg_session_factory, tmp_path):
    """Item 2b: draft collab is enabled by the time insert_from_annotation rechecks
    UNDER the mutex (the route's pre-mutex guard raced enable-on-touch) -> abort
    with CollabSeveranceConflict, writing NOTHING to git."""
    from grant_lab.proposal_assist.collab import state
    from grant_lab.proposal_assist.collab.state import CollabSeveranceConflict

    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        head_before = current_head(repo)
        with pytest.raises(CollabSeveranceConflict):
            _insert(s, ws, user, ann, tmp_path)
        s.rollback()
    assert current_head(repo) == head_before  # git untouched


def test_happy_path_canonical_marker_row_and_provenance(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)

        res = _insert(s, ws, user, ann, tmp_path)
        s.commit()

        ev_id, citekey = res["ev_id"], res["citekey"]
        assert res["commit_sha"] == current_head(repo)

        # Canonical marker trails the claim sentence (after its terminator).
        new_doc = (repo / "proposal.md").read_text()
        marker = f"[@{citekey}]<!-- ev:{ev_id} -->"
        assert f"This is a factual claim.{marker}" in new_doc

        # citations.json gained the row in ONE commit with proposal.md.
        ledger = load_ledger((repo / "citations.json").read_text())
        (row,) = ledger["evidence"]
        assert row["id"] == ev_id
        assert row["citekey"] == citekey
        assert row["reference_id"] == str(ref_id)
        assert row["evidence_quote"] == QUOTE
        assert row["stance"] == "supports"
        assert row["claim_context"]["text"] == CLAIM

        # Provenance: ArtifactVersion('user') + WorkspaceEvent + resolution.
        av = s.query(ArtifactVersion).one()
        assert av.created_by == "user"
        assert av.commit_sha == res["commit_sha"]
        ev = s.query(WorkspaceEvent).one()
        assert ev.kind == "user_citation_insert"
        assert ev.payload == {"annotation_id": ann.id, "ev_id": ev_id, "citekey": citekey}
        assert ann.status == "inserted"
        assert ann.resolved_by_user_id == user.id
        assert ann.resolved_at is not None
        # Citekey cache mirror (agent-path parity).
        assert s.get(Reference, ref_id).citekey == citekey

        # Deterministic audit: the new ev is fully linked (no orphans).
        issues = validate_citations_deterministic(
            proposal_text=new_doc, ledger=ledger, session=s, workspace_id=ws.id
        )
        assert not [i for i in issues if i["type"] in ("orphan_marker", "orphan_row")]


def test_second_call_is_idempotent(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)
        _insert(s, ws, user, ann, tmp_path)
        s.commit()
        head = current_head(repo)

        res = _insert(s, ws, user, ann, tmp_path)
        s.commit()
        assert res == {"idempotent": True}
        assert current_head(repo) == head


def test_same_evidence_elsewhere_does_not_satisfy_this_occurrence(pg_session_factory, tmp_path):
    """Rev-3 finding 4: a live marker + row for the SAME (ref, chunk, quote)
    on ANOTHER sentence must not no-op this annotation's insert."""
    other_ev = "ev-aaaaaaaaaaaa"
    doc = (
        "# T\nintro\n"
        f"## A\nAnother cited sentence.[@k1]<!-- ev:{other_ev} -->\n"
        "## Body\nThis is a factual claim.\n"
    )
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        ref = s.get(Reference, ref_id)
        ref.citekey = "k1"
        s.flush()
        now = datetime.now(UTC).isoformat()
        pre_row = build_evidence_row(
            ev_id=other_ev,
            citekey="k1",
            reference_id=str(ref_id),
            chunk_index=0,
            section_path=None,
            source_content_hash=None,
            chunk_content_hash="0" * 64,
            evidence_quote=QUOTE,
            evidence_quote_offsets=[12, 26],
            stance="supports",
            confidence=None,
            claim_context_text="Another cited sentence",
            claim_context_hash="0" * 64,
            citation_snapshot="k1",
            created_at=now,
            updated_at=now,
        )
        ledger = {**empty_ledger(), "evidence": [pre_row]}
        repo = _init_repo(tmp_path, ws.id, doc, citations_json=dump_ledger(ledger))
        ann = _seed_annotation(s, ws.id, repo, section_index=2, reference_id=ref_id)

        res = _insert(s, ws, user, ann, tmp_path)
        s.commit()

        assert "idempotent" not in res
        assert res["ev_id"] != other_ev
        new_doc = (repo / "proposal.md").read_text()
        assert len(find_markers(new_doc)) == 2
        new_ledger = load_ledger((repo / "citations.json").read_text())
        assert len(new_ledger["evidence"]) == 2


def test_byte_identical_duplicate_sections_conflict(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Dup\nThis is a factual claim.\n## Dup\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, doc)
        ann = _seed_annotation(s, ws.id, repo, section_index=1, reference_id=ref_id)

        with pytest.raises(InsertConflict, match="repeated text"):
            _insert(s, ws, user, ann, tmp_path)
        s.rollback()
        assert (repo / "proposal.md").read_text() == doc


def test_tombstoned_reference_conflict(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        s.get(Reference, ref_id).tombstoned_at = datetime.now(UTC)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)

        with pytest.raises(InsertConflict, match="no longer available"):
            _insert(s, ws, user, ann, tmp_path)
        s.rollback()
        assert (repo / "proposal.md").read_text() == DOC


def test_section_sha_gone_from_head_conflict(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)

        # Edit the section so its sha no longer exists at HEAD.
        (repo / "proposal.md").write_text("# T\nintro\n## Body\nEdited away.\n")
        subprocess.run(["git", "commit", "-aqm", "edit"], cwd=repo, check=True, env=_git_env())

        with pytest.raises(InsertConflict, match="re-analysis is pending"):
            _insert(s, ws, user, ann, tmp_path)


def test_turn_in_flight_conflict(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id)
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.flush()

        with pytest.raises(InsertConflict, match="assistant is working"):
            _insert(s, ws, user, ann, tmp_path)


def test_dismissed_annotation_conflict(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        ann = _seed_annotation(s, ws.id, repo, reference_id=ref_id, status="dismissed")

        with pytest.raises(InsertConflict, match="no longer actionable"):
            _insert(s, ws, user, ann, tmp_path)
