"""Tier-2 claim-analysis pipeline (Task 10). Spec 2026-06-10 §3.

Uses the production repo layout <workspaces_dir>/<workspace_id>/repo and the
real claim_analysis_jobs queue. The citation LLM + embedder are faked.
"""

import subprocess
from datetime import UTC, datetime

from call_manager.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    User,
    UserClaimAnnotation,
    Workspace,
)
from call_manager.proposal_assist.analysis import pipeline
from call_manager.proposal_assist.analysis.pipeline import drain_analysis_queue
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.citations.gitio import _git_env, current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.references.embed import FakeEmbedder

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLLMResult:
    def __init__(self, data, cap=False):
        self.data, self.cap_exceeded = data, cap


class FakeCitationService:
    def __init__(self, verdict="supports", quote="verified words", cap=False):
        self.calls = []
        self._verdict, self._quote, self._cap = verdict, quote, cap

    def complete_json(self, *, workspace_id, op, system, user):
        self.calls.append(op)
        if self._cap:
            return FakeLLMResult(None, cap=True)
        if op == "user_claim_classify":
            return FakeLLMResult({"is_claim": "claim" in user.lower()})
        return FakeLLMResult(
            {"verdict": self._verdict, "evidence_quote": self._quote, "rationale": "r"}
        )


# --------------------------------------------------------------------------- #
# Repo / workspace helpers (production <id>/repo layout, per test_editing_save)
# --------------------------------------------------------------------------- #


def _init_repo(tmp_path, ws_id, doc, citations_json=None):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text(doc)
    if citations_json is not None:
        (repo / "citations.json").write_text(citations_json)
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
    return repo


def _ws(session):
    user = User(email="owner@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws, user


def _seed_reference(session, workspace_id, chunk_text):
    """Create one done reference with a single chunk embedded by FakeEmbedder."""
    embedder = FakeEmbedder()
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        text_status="done",
        metadata_status="approved",
    )
    session.add(ref)
    session.flush()
    session.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=workspace_id,
            chunk_index=0,
            text=chunk_text,
            embedding=embedder.embed(chunk_text),
            embedding_model=embedder.model,
        )
    )
    session.flush()
    return ref.id


def _enqueue_section(session, ws_id, repo, section_index):
    doc = (repo / "proposal.md").read_text()
    secs = split_sections(doc)
    sec = secs[section_index]
    enqueue_section_job(
        session,
        workspace_id=ws_id,
        section_index=section_index,
        section_heading=sec.heading,
        section_sha=sec.sha,
        commit_sha=current_head(repo),
    )
    return sec.sha


def _drain(pg_session_factory, tmp_path, svc, embedder=None):
    return drain_analysis_queue(
        session_factory=pg_session_factory,
        citation_service=svc,
        embedder=embedder or FakeEmbedder(),
        workspaces_dir=tmp_path,
    )


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def test_uncited_supported(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        _seed_reference(s, ws.id, "Background. verified words appear here. End.")
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["done"] == 1

    with pg_session_factory() as s:
        anns = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).all()
        assert len(anns) == 1
        a = anns[0]
        assert a.kind == "uncited_supported"
        assert a.status == "open"
        assert a.reference_id is not None
        assert a.evidence_quote == "verified words"


def test_uncited_no_match_when_quote_absent(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        # Chunk does NOT contain "verified words" → quote verification fails.
        _seed_reference(s, ws.id, "Background discussion with no matching span.")
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["done"] == 1
    with pg_session_factory() as s:
        a = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert a.kind == "uncited_no_match"


def test_classification_cache_avoids_reclassify(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        _seed_reference(s, ws.id, "verified words here.")
        sha = _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id
        commit = current_head(repo)

    svc = FakeCitationService()
    _drain(pg_session_factory, tmp_path, svc)
    classify_after_first = svc.calls.count("user_claim_classify")
    assert classify_after_first >= 1

    # Delete the done job and re-enqueue the SAME content.
    with pg_session_factory() as s:
        s.query(ClaimAnalysisJob).delete()
        enqueue_section_job(
            s,
            workspace_id=ws_id,
            section_index=1,
            section_heading="Body",
            section_sha=sha,
            commit_sha=commit,
        )
        s.commit()

    _drain(pg_session_factory, tmp_path, svc)
    # Cache hit → classify count must NOT grow.
    assert svc.calls.count("user_claim_classify") == classify_after_first


def test_cited_first_scoped_ledger_and_unsupported(pg_session_factory, tmp_path, monkeypatch):
    ev = "ev-1111aaaa2222"
    other = "ev-9999ffff8888"
    doc = f"# T\nintro\n## S\nCited sentence here[@k]<!-- ev:{ev} -->.\n"
    citations = (
        '{"version": 2, "evidence": ['
        f'{{"id": "{ev}", "citekey": "k", "stance": "supports"}}, '
        f'{{"id": "{other}", "citekey": "other", "stance": "supports"}}'
        "]}"
    )
    captured = {}

    def fake_validate(**kwargs):
        captured["ledger"] = kwargs["ledger"]
        return [{"type": "semantic_unsupported", "evidence_id": ev, "severity": "warning"}]

    monkeypatch.setattr(pipeline, "validate_citations_semantic", fake_validate)

    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc, citations_json=citations)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["done"] == 1

    scoped = captured["ledger"]["evidence"]
    assert [r["id"] for r in scoped] == [ev]  # unrelated row filtered OUT

    with pg_session_factory() as s:
        a = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert a.kind == "cited_unsupported"
        assert a.evidence_id == ev


def test_supersession_when_section_gone(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n## Keep\nkeep me\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    # Remove the enqueued section before draining.
    env = _git_env()
    (repo / "proposal.md").write_text("# T\nintro\n## Keep\nkeep me\n")
    subprocess.run(["git", "commit", "-aqm", "drop section"], cwd=repo, check=True, env=env)

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["superseded"] == 1
    with pg_session_factory() as s:
        assert s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).count() == 0


def test_duplicate_sections_fan_out_and_dismiss_isolation(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Dup\nThis is a factual claim.\n## Dup\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        # No reference → uncited_no_match for both occurrences.
        sha = _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id
        commit = current_head(repo)

    _drain(pg_session_factory, tmp_path, FakeCitationService())
    with pg_session_factory() as s:
        anns = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).all()
        assert {a.section_occurrence for a in anns} == {0, 1}
        # Dismiss occurrence 0.
        dismissed = next(a for a in anns if a.section_occurrence == 0)
        dismissed.status = "dismissed"
        s.commit()

    # Re-run analysis on the same content.
    with pg_session_factory() as s:
        s.query(ClaimAnalysisJob).delete()
        enqueue_section_job(
            s,
            workspace_id=ws_id,
            section_index=1,
            section_heading="Dup",
            section_sha=sha,
            commit_sha=commit,
        )
        s.commit()
    _drain(pg_session_factory, tmp_path, FakeCitationService())

    with pg_session_factory() as s:
        by_occ = {
            a.section_occurrence: a.status
            for a in s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).all()
        }
        assert by_occ[0] == "dismissed"
        assert by_occ[1] == "open"


def test_cap_exceeded_marks_failure(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService(cap=True))
    assert counts["failed"] == 1
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        # attempts bumped at claim; first failure → back to queued for retry.
        assert job.status == "queued"
        assert job.attempts == 1
        assert "cap" in (job.last_error or "").lower()


def test_audit_unavailable_blocks_completion(pg_session_factory, tmp_path, monkeypatch):
    ev = "ev-1111aaaa2222"
    doc = f"# T\nintro\n## S\nCited sentence here[@k]<!-- ev:{ev} -->.\n"
    citations = (
        f'{{"version": 2, "evidence": [{{"id": "{ev}", "citekey": "k", "stance": "supports"}}]}}'
    )

    monkeypatch.setattr(
        pipeline,
        "validate_citations_semantic",
        lambda **kw: [{"type": "audit_unavailable", "evidence_id": "x"}],
    )

    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc, citations_json=citations)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["done"] == 0
    assert counts["failed"] == 1
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued"  # retry, not done
        assert s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).count() == 0


def test_stale_annotation_reopened_not_duplicated(pg_session_factory, tmp_path):
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        sha = _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id
        commit = current_head(repo)

    _drain(pg_session_factory, tmp_path, FakeCitationService())
    with pg_session_factory() as s:
        a = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert a.kind == "uncited_no_match"
        a.status = "stale"
        s.commit()

    with pg_session_factory() as s:
        s.query(ClaimAnalysisJob).delete()
        enqueue_section_job(
            s,
            workspace_id=ws_id,
            section_index=1,
            section_heading="Body",
            section_sha=sha,
            commit_sha=commit,
        )
        s.commit()
    _drain(pg_session_factory, tmp_path, FakeCitationService())

    with pg_session_factory() as s:
        anns = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).all()
        assert len(anns) == 1  # reopened, not duplicated
        assert anns[0].status == "open"


def test_stale_supported_cache_refreshes_to_no_match(pg_session_factory, tmp_path):
    """A stale ClaimClassification row with outcome='supported' is refreshed
    when the verdict path fails verification.  The refreshed row must have
    outcome='no_match' with all evidence fields cleared, and the fan-out must
    produce an open uncited_no_match annotation."""
    doc = "# T\nintro\n## Body\nThis is a factual claim.\n"
    claim_sentence = "This is a factual claim"
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc)
        ref_id = _seed_reference(s, ws.id, "Background. verified words appear here. End.")
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    # Seed a stale ClaimClassification row for the claim sentence with
    # outcome='supported' and evidence fields populated.
    claim_sha = pipeline._claim_sha(claim_sentence)
    with pg_session_factory() as s:
        from call_manager.proposal_assist.analysis.pipeline import ANALYSIS_PROMPT_VERSION

        s.add(
            ClaimClassification(
                workspace_id=ws_id,
                claim_sha=claim_sha,
                prompt_version=ANALYSIS_PROMPT_VERSION,
                is_claim=True,
                outcome="supported",
                reference_id=ref_id,
                chunk_index=0,
                evidence_quote="verified words",
                verdict_json={
                    "verdict": "supports",
                    "evidence_quote": "verified words",
                    "rationale": "r",
                },
                stale_at=datetime.now(UTC),
            )
        )
        s.commit()

    # The FakeCitationService returns a quote that is NOT present in the chunk
    # text → quote verification fails → outcome must downgrade to no_match.
    svc = FakeCitationService(quote="words that are not in the chunk")
    counts = _drain(pg_session_factory, tmp_path, svc)
    assert counts["done"] == 1

    with pg_session_factory() as s:
        from call_manager.proposal_assist.analysis.pipeline import ANALYSIS_PROMPT_VERSION

        cc = (
            s.query(ClaimClassification)
            .filter_by(
                workspace_id=ws_id,
                claim_sha=claim_sha,
                prompt_version=ANALYSIS_PROMPT_VERSION,
            )
            .one()
        )
        assert cc.outcome == "no_match"
        assert cc.stale_at is None
        assert cc.reference_id is None
        assert cc.chunk_index is None
        assert cc.evidence_quote is None

        ann = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert ann.kind == "uncited_no_match"
        assert ann.status == "open"


def test_marker_before_first_sentence_offset(pg_session_factory, tmp_path, monkeypatch):
    """A marker-only first body line must attach its evidence to sentence 0
    (no silent drop of the cited set)."""
    ev = "ev-aaaaaa111111"
    doc = f"# T\nintro\n## S\n[@k]<!-- ev:{ev} -->\nThis sentence is cited.\n"
    citations = (
        f'{{"version": 2, "evidence": [{{"id": "{ev}", "citekey": "k", "stance": "supports"}}]}}'
    )

    monkeypatch.setattr(
        pipeline,
        "validate_citations_semantic",
        lambda **kw: [{"type": "semantic_unsupported", "evidence_id": ev, "severity": "warning"}],
    )

    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id, doc, citations_json=citations)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()
        ws_id = ws.id

    counts = _drain(pg_session_factory, tmp_path, FakeCitationService())
    assert counts["done"] == 1
    with pg_session_factory() as s:
        a = s.query(UserClaimAnnotation).filter_by(workspace_id=ws_id).one()
        assert a.kind == "cited_unsupported"
        assert a.evidence_id == ev
        assert a.sentence_ordinal == 0
