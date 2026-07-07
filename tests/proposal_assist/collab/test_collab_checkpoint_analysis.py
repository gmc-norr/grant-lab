"""3b-5d.D3: the checkpoint funnel enqueues Tier-2 claim-analysis coverage for
just-committed DRAFT content (spec §3.2). Gate is `changed and artifact ==
"draft"` — NOT record_version, so the silent tail-scan path (3b-5c) is covered
too. Enqueue-only: the drain tick does the LLM work later."""

import subprocess
from pathlib import Path

from pycrdt import Doc
from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, ClaimAnalysisJob
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import (
    checkpoint_artifact,
    serialize_doc_to_markdown,
)
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_room_edit(pg_session_factory, wid, actor_id, md, *, artifact="draft") -> Doc:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact=artifact, actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def _setup_ws(pg_session_factory, tmp_path, *, email, artifact="draft"):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact=artifact)
        s.commit()
    return wid, oid


def _jobs(session, wid):
    return (
        session.execute(select(ClaimAnalysisJob).where(ClaimAnalysisJob.workspace_id == wid))
        .scalars()
        .all()
    )


def _live_shas(md: str) -> set[str]:
    return {sec.sha for sec in split_sections(md) if sec.text.strip()}


def test_draft_checkpoint_enqueues_coverage_for_new_sections(pg_session_factory, tmp_path):
    """A draft checkpoint that advances git enqueues one queued job per
    non-empty section of the serialized doc, pinned to the checkpoint's
    commit_sha (spec §6 case 1)."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-draft@ki.se")
    doc = _seed_room_edit(
        pg_session_factory,
        wid,
        oid,
        "## Background\n\nrewritten claim.\n\n## Aims\n\nsecond claim.\n",
    )
    expected_shas = _live_shas(serialize_doc_to_markdown(doc))
    assert len(expected_shas) == 2  # sanity: two distinct non-empty sections

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is True

    with pg_session_factory() as s:
        jobs = _jobs(s, wid)
        assert {j.section_sha for j in jobs} == expected_shas
        assert all(j.status == "queued" for j in jobs)
        assert all(j.commit_sha == res.commit_sha for j in jobs)


def test_spec_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """A SPEC checkpoint commits (changed=True) but enqueues no claim analysis
    (mirrors save_section: spec §3 has no claim analysis) — spec §6 case 2.
    Non-vacuous: the spec content's sections are uncovered, so without the
    artifact gate the hook WOULD insert."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-spec@ki.se", artifact="spec")
    _seed_room_edit(
        pg_session_factory, wid, oid, "## Approach\n\nspec content.\n", artifact="spec"
    )
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="spec", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is True  # a REAL spec commit — the guard is the artifact, not vacuity
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_seed_only_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """The unauthored-seed early-return (checkpoint.py:91-106) never reaches the
    hook — no git commit, no jobs (spec §6 case 3)."""
    wid, _ = _setup_ws(pg_session_factory, tmp_path, email="d3-seed@ki.se")
    _seed_room_edit(pg_session_factory, wid, None, "## Background\n\noriginal.\n")
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is False
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_unchanged_content_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """The gate is `changed`: a checkpoint whose serialized content equals HEAD
    (changed=False) must NOT enqueue (spec §6 case 4). Non-vacuous: coverage
    rows from the first checkpoint are DELETED, so if the hook ran on the
    second checkpoint the shas would be uncovered and rows WOULD appear.
    Also covers the max_id<=watermark no-op (spec §6 case 3, first variant)."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-nochange@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nedited once.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert len(_jobs(s, wid)) > 0  # checkpoint 1 enqueued
        s.query(ClaimAnalysisJob).filter_by(workspace_id=wid).delete()
        s.commit()

    # A new AUTHORED log row carrying the SAME full state: Yjs merge is
    # idempotent (same client ids), so the rebuilt doc serializes to content
    # identical to HEAD -> write_and_commit_files returns head -> changed=False.
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=doc.get_update()
        )
        s.commit()
    with pg_session_factory() as s:
        res2 = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res2.committed is False  # changed=False: real attempt, empty git diff
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []

    # And the max_id<=watermark early-return (no new rows at all): still nothing.
    with pg_session_factory() as s:
        res3 = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res3.committed is False
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_scan_path_checkpoint_enqueues_coverage(pg_session_factory, tmp_path):
    """record_version=False (the periodic tail-scan, 3b-5c) still enqueues —
    the gate is `changed`, NOT record_version (spec §6 case 5). Quiet for
    provenance, loud for coverage."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-scan@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nscan edit.\n")
    expected_shas = _live_shas(serialize_doc_to_markdown(doc))

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
            record_version=False,
        )
        s.commit()
    assert res.committed is True

    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []  # quiet: no provenance row
        jobs = _jobs(s, wid)
        assert {j.section_sha for j in jobs} == expected_shas  # loud: coverage
        assert all(j.commit_sha == res.commit_sha for j in jobs)
