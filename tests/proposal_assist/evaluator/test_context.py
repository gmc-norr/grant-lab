from grant_lab.proposal_assist.evaluator.context import (
    assemble_evaluator_context,
    content_sha,
)
from tests.proposal_assist.conftest import make_workspace


def _seed(tmp_path, ws_id, draft, spec):
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(draft)
    (repo / "proposal-spec.md").write_text(spec)
    return repo


def test_assembles_criteria_sections_and_counts(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour", "weight": 0.6},
            {"name": "Impact", "description": "Reach", "weight": 0.4},
        ]
        ws.rubric_funder_notes = "Single-PI only."
        s.flush()
        ws_id = ws.id
        repo = _seed(tmp_path, ws_id, "## Aims\n\nA draft.\n", "## Aims\n\nThe spec.\n")
        ctx = assemble_evaluator_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert "[0] Scientific quality" in ctx.instruction
    assert "[1] Impact" in ctx.instruction
    assert "Single-PI only." in ctx.instruction
    assert "[§0]" in ctx.instruction and "[§1]" in ctx.instruction  # index labels
    assert "A draft." in ctx.instruction and "The spec." in ctx.instruction
    # split_sections yields section 0 (empty preamble) + section 1 (## Aims): count 2.
    assert ctx.draft_section_count == 2 and ctx.spec_section_count == 2
    # The returned sha hashes the SAME bytes the endpoint will re-hash.
    assert ctx.draft_sha == content_sha(repo / "proposal.md")
    assert ctx.spec_sha == content_sha(repo / "proposal-spec.md")


def test_empty_rubric_and_missing_files(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = []
        s.flush()
        ctx = assemble_evaluator_context(
            session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path)
        )
    assert "no criteria" in ctx.instruction.lower()
    assert "(draft is empty)" in ctx.instruction
    # missing files -> 0 sections, '' hashes
    assert ctx.draft_section_count == 0 and ctx.spec_section_count == 0
    assert ctx.draft_sha == "" and ctx.spec_sha == ""


def test_content_sha_changes_with_content(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("one")
    a = content_sha(p)
    p.write_text("two")
    assert a != content_sha(p)
    assert content_sha(tmp_path / "missing.md") == ""
