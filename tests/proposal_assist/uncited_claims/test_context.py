from pathlib import Path

from grant_lab.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from tests.proposal_assist.conftest import make_workspace


def _seed(tmp_path, ws_id, body):
    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(body)


def test_assembles_sections_and_sha(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    _seed(tmp_path, ws_id, "## Aims\n\nWe claim 30% relapse.\n\n## Methods\n\nMore.\n")
    with pg_session_factory() as s:
        ctx = assemble_uncited_claims_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert ctx.draft_section_count == 3  # preamble(0) + 2 headed sections
    assert "30% relapse" in ctx.section_texts[1]
    assert ctx.draft_sha  # non-empty content sha
    assert "DRAFT" in ctx.instruction


def test_empty_draft_yields_zero_sections(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    _seed(tmp_path, ws_id, "")
    with pg_session_factory() as s:
        ctx = assemble_uncited_claims_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert ctx.draft_section_count == 0 and ctx.section_texts == {}
