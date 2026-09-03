"""The four background LLM passes must read the LIVE document, not stale disk.

Slice 3a C1 routed three consumers through `canonical_artifact_markdown` —
`read_proposal`, the staleness sweep and the citation anchor probe — pinned in
test_canonical_consumers.py. The four hosted-LLM pass context assemblers were missed
and still read the git working tree directly.

Git HEAD lags the Yjs log until a checkpoint fires, so with collab enabled these four
send a stale document to a hosted model: the evaluator scores prose the researcher
already rewrote, the coherence keeper reconciles against text that no longer exists,
uncited-claims flags claims that were cited an hour ago, and review critiques a draft
nobody is looking at. Three of the four also run UNATTENDED on APScheduler ticks, so
the spend happens with no user present to notice the output is about the wrong text.
"""

from __future__ import annotations

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.coherence.context import assemble_coherence_context
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.evaluator.context import assemble_evaluator_context
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.context import assemble_review_context
from call_manager.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from tests.proposal_assist.collab.test_collab_kernel_accept import _init_repo
from tests.proposal_assist.conftest import make_user, make_workspace

STALE_DISK = "## Aims\n\nThe original sentence nobody is looking at any more.\n"
LIVE_ROOM = "## Aims\n\nThe rewritten sentence the researcher can actually see.\n"
# The spec must carry DIFFERENT text. Collab is enabled for the draft only, so the
# spec is legitimately read from disk and rendered into the evaluator and coherence
# prompts — if it repeated STALE_DISK the draft assertion would fire on the spec and
# report a bug that is not there.
SPEC_DISK = "## Spec\n\nSpec prose, deliberately unlike either draft sentence.\n"

_ASSEMBLERS = {
    "evaluator": assemble_evaluator_context,
    "coherence": assemble_coherence_context,
    "review": assemble_review_context,
    "uncited_claims": assemble_uncited_claims_context,
}


def _workspace_with_room_ahead_of_disk(pg_session_factory, tmp_path, email):
    """A workspace whose durable log holds LIVE_ROOM while git HEAD holds STALE_DISK.

    This is the ordinary steady state, not a contrived one: every edit lands in the
    log immediately and reaches git only when a checkpoint fires.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=email))
        s.commit()
        wid = ws.id

    repo = workspace_repo_dir(tmp_path, wid)
    _init_repo(repo, STALE_DISK)
    (repo / filename_for(Artifact.SPEC)).write_text(SPEC_DISK)

    seed = pm_json_to_ydoc(markdown_to_pm(pm_to_markdown(markdown_to_pm(LIVE_ROOM)))).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    return wid


# The four return four different shapes. Named explicitly rather than duck-typed, so
# a future signature change fails loudly here instead of quietly reading the wrong field.
_DRAFT_TEXT = {
    "evaluator": lambda out: out.instruction,       # draft is rendered into the prompt
    "coherence": lambda out: out,                   # returns the instruction string itself
    "review": lambda out: out.draft_text,           # carries the raw draft
    "uncited_claims": lambda out: out.instruction,
}


def _assemble(name, pg_session_factory, wid, tmp_path):
    """The draft content this pass would actually send to a hosted model."""
    with pg_session_factory() as s:
        out = _ASSEMBLERS[name](session=s, workspace_id=wid, workspaces_dir=tmp_path)
    return _DRAFT_TEXT[name](out)


def test_every_pass_reads_the_live_room_not_stale_disk(pg_session_factory, tmp_path):
    """The whole point, asserted as an exact set so a partial fix cannot pass."""
    wid = _workspace_with_room_ahead_of_disk(
        pg_session_factory, tmp_path, "pass-canon@umu.se"
    )
    stale = {}
    for name in _ASSEMBLERS:
        instruction = _assemble(name, pg_session_factory, wid, tmp_path)
        if "The original sentence" in instruction:
            stale[name] = "sent the STALE disk text to a hosted model"
        elif "The rewritten sentence" not in instruction:
            stale[name] = "sent neither the live nor the stale text"
    assert stale == {}, stale


def test_flag_off_still_reads_raw_disk(pg_session_factory, tmp_path):
    """Control. With collab disabled, disk IS authoritative and must stay so —
    otherwise the fix would silently change behaviour for every non-collab
    workspace, and a test that cannot distinguish the two proves nothing."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="pass-canon-off@umu.se"))
        s.commit()
        wid = ws.id
    repo = workspace_repo_dir(tmp_path, wid)
    _init_repo(repo, STALE_DISK)
    (repo / filename_for(Artifact.SPEC)).write_text(SPEC_DISK)
    # No log, no enable_collab.
    for name in _ASSEMBLERS:
        instruction = _assemble(name, pg_session_factory, wid, tmp_path)
        assert "The original sentence" in instruction, name
