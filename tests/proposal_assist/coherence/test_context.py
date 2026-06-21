from pathlib import Path

from call_manager.proposal_assist.coherence.context import assemble_coherence_context
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir: Path, ws_id, *, draft: str, spec: str) -> None:
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(draft)
    (repo / "proposal-spec.md").write_text(spec)


def test_assembles_criteria_spec_draft_and_lock_state(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [{"name": "Novelty", "description": "How new", "weight": 3}]
        ws.rubric_funder_notes = "Favors translational work."
        s.commit()
        ws_id = ws.id
    _seed_repo(tmp_path, ws_id, draft="## Aims\n\nCure X.\n", spec="## Aims\n\nIntend to cure X.\n")

    with pg_session_factory() as s:
        text = assemble_coherence_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )

    assert "Novelty" in text
    assert "Favors translational work." in text
    assert "Cure X." in text  # draft
    assert "Intend to cure X." in text  # spec
    assert "unlocked" in text.lower()  # lock state surfaced (spec_locked_at is None)


def test_handles_missing_artifact_files(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    # No repo written at all.
    with pg_session_factory() as s:
        text = assemble_coherence_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert isinstance(text, str) and len(text) > 0  # no crash; empty artifacts noted
    assert "(draft is empty)" in text  # degraded-path placeholder for missing draft
    assert "(spec is empty)" in text  # degraded-path placeholder for missing spec
