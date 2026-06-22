import hashlib

from call_manager.proposal_assist.review.context import (
    assemble_review_context,
    consolidation_instruction,
    persona_instruction,
)
from call_manager.proposal_assist.review.personas import persona_prompt


def test_context_and_instructions(review_workspace):
    # review_workspace fixture (added below) -> (session, workspace_id, workspaces_dir)
    session, ws_id, wdir, repo = review_workspace
    repo.joinpath("proposal.md").write_text("# Aims\nWe will cure X.\n")
    repo.joinpath("proposal-spec.md").write_text("# Intent\nCure X by 2030.\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "draft"], cwd=repo, check=True)

    ctx = assemble_review_context(session=session, workspace_id=ws_id, workspaces_dir=wdir)
    assert ctx.draft_sha == hashlib.sha256(b"# Aims\nWe will cure X.\n").hexdigest()
    instr = persona_instruction("persona_domain_skeptic", ctx)
    assert persona_prompt("persona_domain_skeptic")[:40] in instr
    assert "We will cure X." in instr
    cons = consolidation_instruction(ctx, {"persona_domain_skeptic": "- weak novelty"}, "GO")
    assert "weak novelty" in cons and "GO" in cons
