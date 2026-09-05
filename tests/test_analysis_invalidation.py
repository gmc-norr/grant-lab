"""Library-change invalidation hooks (Task 12). Spec 2026-06-10 §3.

Both directions: library GREW (a reference's text ingest completed) →
re-check open no-match findings; library SHRANK/CHANGED (a reference was
tombstoned or hard-deleted) → re-check open supported findings pointing at
the affected reference.

Uses the production repo layout <workspaces_dir>/<workspace_id>/repo (per
test_analysis_pipeline) and seeds annotations/classifications directly —
no LLM or embedder is involved; the hooks are pure DB + filesystem.
"""

import hashlib
import json
import subprocess

from grant_lab.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    User,
    UserClaimAnnotation,
    Workspace,
)
from grant_lab.proposal_assist.analysis.invalidation import (
    on_reference_text_ingested,
    on_reference_unavailable,
)
from grant_lab.proposal_assist.citations.gitio import _git_env
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.references.deletion import delete_or_tombstone_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM_TEXT = "This is a factual claim"
EMBED_DIM = 1024

# --------------------------------------------------------------------------- #
# Helpers (production <id>/repo layout, per test_analysis_pipeline)
# --------------------------------------------------------------------------- #


def _init_repo(tmp_path, ws_id, doc=DOC, citations_json=None):
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


def _live_sha(doc=DOC, index=1):
    return split_sections(doc)[index].sha


def _seed_reference(session, workspace_id):
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
            section_path="Results",
            text="some chunk text",
            embedding=[0.0] * EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    session.flush()
    return ref.id


def _seed_annotation(session, ws_id, *, section_sha, kind, reference_id=None, status="open"):
    ann = UserClaimAnnotation(
        workspace_id=ws_id,
        section_sha=section_sha,
        section_occurrence=0,
        section_heading="Body",
        sentence_ordinal=0,
        char_offset=0,
        claim_sha=hashlib.sha256(CLAIM_TEXT.encode()).hexdigest(),
        claim_text=CLAIM_TEXT,
        kind=kind,
        evidence_id="",
        reference_id=reference_id,
        status=status,
    )
    session.add(ann)
    session.flush()
    return ann.id


def _seed_classification(session, ws_id, *, outcome, reference_id=None):
    row = ClaimClassification(
        workspace_id=ws_id,
        claim_sha=hashlib.sha256(CLAIM_TEXT.encode()).hexdigest(),
        prompt_version="v1",
        is_claim=True,
        outcome=outcome,
        reference_id=reference_id,
        chunk_index=0 if outcome == "supported" else None,
        evidence_quote="some chunk text" if outcome == "supported" else None,
    )
    session.add(row)
    session.flush()
    return row.id


def _jobs(session, ws_id, section_sha=None):
    q = session.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued")
    if section_sha is not None:
        q = q.filter_by(section_sha=section_sha)
    return q.all()


# --------------------------------------------------------------------------- #
# Direct hook tests
# --------------------------------------------------------------------------- #


def test_ingest_hook_stales_no_match_and_enqueues(pg_session_factory, tmp_path):
    """Library grew: no_match classifications go stale and sections with open
    uncited_no_match annotations still live at HEAD are re-enqueued."""
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        _init_repo(tmp_path, ws.id)
        sha = _live_sha()
        _seed_annotation(s, ws.id, section_sha=sha, kind="uncited_no_match")
        cc_id = _seed_classification(s, ws.id, outcome="no_match")
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        n = on_reference_text_ingested(s, workspace_id=ws_id, workspaces_dir=tmp_path)
        s.commit()
    assert n >= 1

    with pg_session_factory() as s:
        cc = s.get(ClaimClassification, cc_id)
        assert cc.stale_at is not None
        assert len(_jobs(s, ws_id, section_sha=sha)) == 1


def test_unavailable_hook_collect_before_stale(pg_session_factory, tmp_path):
    """REGRESSION (spec rev-3 ordering bug): the open-annotation shas must be
    collected BEFORE the annotations are flipped to stale — the collection
    filters status='open', so staleing first would always enqueue zero."""
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        _init_repo(tmp_path, ws.id)
        sha = _live_sha()
        rid = _seed_reference(s, ws.id)
        ann_id = _seed_annotation(
            s, ws.id, section_sha=sha, kind="uncited_supported", reference_id=rid
        )
        cc_id = _seed_classification(s, ws.id, outcome="supported", reference_id=rid)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        n = on_reference_unavailable(
            s, workspace_id=ws_id, reference_id=rid, workspaces_dir=tmp_path
        )
        s.commit()
    assert n >= 1  # would be 0 if the hook staled before collecting

    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "stale"
        assert s.get(ClaimClassification, cc_id).stale_at is not None
        assert len(_jobs(s, ws_id, section_sha=sha)) == 1


def test_unavailable_noop_when_section_gone(pg_session_factory, tmp_path):
    """A section_sha no longer at HEAD gets no job, but the DB staleing of
    annotations + classifications still happens."""
    gone_sha = "f" * 64
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        _init_repo(tmp_path, ws.id)
        rid = _seed_reference(s, ws.id)
        ann_id = _seed_annotation(
            s, ws.id, section_sha=gone_sha, kind="uncited_supported", reference_id=rid
        )
        cc_id = _seed_classification(s, ws.id, outcome="supported", reference_id=rid)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        n = on_reference_unavailable(
            s, workspace_id=ws_id, reference_id=rid, workspaces_dir=tmp_path
        )
        s.commit()
    assert n == 0

    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "stale"
        assert s.get(ClaimClassification, cc_id).stale_at is not None
        assert len(_jobs(s, ws_id)) == 0


def test_hooks_tolerate_missing_repo(pg_session_factory, tmp_path):
    """No repo dir at all: both hooks return 0 without raising. Staleing is
    DB-only and must still happen — only the enqueue depends on the repo."""
    with pg_session_factory() as s:
        ws, _ = _ws(s)  # NOTE: no _init_repo
        rid = _seed_reference(s, ws.id)
        nm_ann = _seed_annotation(s, ws.id, section_sha="a" * 64, kind="uncited_no_match")
        sup_ann = _seed_annotation(
            s, ws.id, section_sha="b" * 64, kind="uncited_supported", reference_id=rid
        )
        nm_cc = _seed_classification(s, ws.id, outcome="no_match")
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        assert on_reference_text_ingested(s, workspace_id=ws_id, workspaces_dir=tmp_path) == 0
        assert (
            on_reference_unavailable(
                s, workspace_id=ws_id, reference_id=rid, workspaces_dir=tmp_path
            )
            == 0
        )
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimClassification, nm_cc).stale_at is not None
        assert s.get(UserClaimAnnotation, sup_ann).status == "stale"
        # The no-match annotation is NOT flipped by the ingest hook — the
        # pipeline's fan-out replaces it when the re-analysis runs.
        assert s.get(UserClaimAnnotation, nm_ann).status == "open"
        assert len(_jobs(s, ws_id)) == 0


# --------------------------------------------------------------------------- #
# End-to-end through references/deletion.py (both branches)
# --------------------------------------------------------------------------- #


def test_deletion_tombstone_branch_invokes_hook(pg_session_factory, tmp_path):
    """A CITED reference tombstones — and the hook runs: the open
    uncited_supported annotation goes stale and its section is re-enqueued."""
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        rid = _seed_reference(s, ws.id)
        repo = _init_repo(
            tmp_path,
            ws.id,
            citations_json=json.dumps(
                {"version": 2, "evidence": [{"id": "ev-1", "reference_id": str(rid)}]}
            ),
        )
        sha = _live_sha()
        ann_id = _seed_annotation(
            s, ws.id, section_sha=sha, kind="uncited_supported", reference_id=rid
        )
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s, repo_dir=repo, reference_id=rid, workspace_id=ws_id
        )
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        assert s.get(Reference, rid).tombstoned_at is not None
        assert s.get(UserClaimAnnotation, ann_id).status == "stale"
        assert len(_jobs(s, ws_id, section_sha=sha)) == 1


def test_deletion_hard_delete_branch_invokes_hook(pg_session_factory, tmp_path):
    """An UNCITED reference hard-deletes — REGRESSION for hook-before-delete
    ordering: the FKs on ClaimClassification.reference_id and
    UserClaimAnnotation.reference_id are SET NULL on delete, so a hook running
    after session.delete(ref) would match zero rows and stale nothing."""
    with pg_session_factory() as s:
        ws, _ = _ws(s)
        repo = _init_repo(tmp_path, ws.id)  # no citations.json → uncited
        sha = _live_sha()
        rid = _seed_reference(s, ws.id)
        ann_id = _seed_annotation(
            s, ws.id, section_sha=sha, kind="uncited_supported", reference_id=rid
        )
        cc_id = _seed_classification(s, ws.id, outcome="supported", reference_id=rid)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s, repo_dir=repo, reference_id=rid, workspace_id=ws_id
        )
    assert outcome == "deleted"

    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "stale"  # staled BEFORE the delete NULLed the FK
        assert ann.reference_id is None  # SET NULL fired after the hook ran
        cc = s.get(ClaimClassification, cc_id)
        assert cc.stale_at is not None
        assert cc.reference_id is None
        assert len(_jobs(s, ws_id, section_sha=sha)) == 1
