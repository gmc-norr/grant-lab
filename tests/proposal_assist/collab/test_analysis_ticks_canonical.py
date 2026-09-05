"""The two claim-analysis scheduler ticks must read the LIVE document, not stale disk.

Same bug class as test_pass_context_canonical.py, in the one pass family that runs
completely unattended. Git HEAD lags the Yjs log, and the periodic tail-bounding scan
only fires once a room has accumulated ``COLLAB_CHECKPOINT_MIN_TAIL`` (200) unwritten
updates — so a room edited steadily but lightly can leave git behind for an entire
session. Against that stale HEAD:

* ``reconcile_analysis_coverage`` (every 900s) enqueues jobs for sections the
  researcher has already rewritten, and never enqueues the ones they can see.
* ``analyze_job`` (every 20s) re-reads the same stale HEAD to locate the job's
  section, so it spends LLM calls auditing that superseded prose.

The two are one mechanism: the enqueuer's basis and the drain's basis MUST be the same
document, or every job the enqueuer creates is superseded on arrival. So they are
pinned together here.
"""

from __future__ import annotations

from sqlalchemy import select

from grant_lab.db.models import ClaimAnalysisJob
from grant_lab.proposal_assist.analysis.pipeline import analyze_job
from grant_lab.proposal_assist.analysis.queue import claim_queued_jobs, enqueue_section_job
from grant_lab.proposal_assist.analysis.reconcile import reconcile_analysis_coverage
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.canonical import canonical_artifact_markdown
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from grant_lab.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.references.embed import FakeEmbedder
from tests.proposal_assist.collab.test_collab_kernel_accept import _init_repo
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import FakeCitationService

STALE_DISK = "## Aims\n\nThe original claim nobody is looking at any more.\n"
LIVE_ROOM = "## Aims\n\nThe rewritten claim the researcher can actually see.\n"


def _workspace_with_room_ahead_of_disk(pg_session_factory, tmp_path, email, *, collab=True):
    """git HEAD holds STALE_DISK; the durable log holds LIVE_ROOM.

    The ordinary steady state, not a contrived one: every edit lands in the log
    immediately and reaches git only when a checkpoint fires.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=email))
        s.commit()
        wid = ws.id

    _init_repo(workspace_repo_dir(tmp_path, wid), STALE_DISK)
    if not collab:
        return wid

    seed = pm_json_to_ydoc(markdown_to_pm(pm_to_markdown(markdown_to_pm(LIVE_ROOM)))).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    return wid


def _shas(text):
    return {sec.sha for sec in split_sections(text) if sec.text.strip()}


def _live_shas(pg_session_factory, wid, tmp_path):
    repo = workspace_repo_dir(tmp_path, wid)
    with pg_session_factory() as s:
        text = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=repo
        )
    return _shas(text)


def _enqueued(pg_session_factory, wid):
    with pg_session_factory() as s:
        return set(
            s.execute(
                select(ClaimAnalysisJob.section_sha).where(ClaimAnalysisJob.workspace_id == wid)
            ).scalars()
        )


def test_the_reconcile_tick_enqueues_the_live_sections_not_the_stale_disk_ones(
    pg_session_factory, tmp_path
):
    wid = _workspace_with_room_ahead_of_disk(pg_session_factory, tmp_path, "recon-canon@umu.se")
    live, stale = _live_shas(pg_session_factory, wid, tmp_path), _shas(STALE_DISK)
    # Guard the fixture itself: if the two bases ever coincided the assertion below
    # would pass without proving anything.
    assert live and stale and not (live & stale)

    reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)

    assert _enqueued(pg_session_factory, wid) == live, (
        "the reconcile tick enqueued work for the stale git HEAD, so the sections the "
        "researcher is actually editing are never analysed"
    )


def test_the_drain_tick_analyses_the_live_section_instead_of_superseding_it(
    pg_session_factory, tmp_path
):
    """The other half. A job carrying a LIVE section sha is unrunnable while the drain
    resolves that sha against stale HEAD — it is superseded on arrival, unanalysed."""
    wid = _workspace_with_room_ahead_of_disk(pg_session_factory, tmp_path, "drain-canon@umu.se")
    repo = workspace_repo_dir(tmp_path, wid)
    with pg_session_factory() as s:
        text = canonical_artifact_markdown(s, workspace_id=wid, artifact="draft", repo_dir=repo)
        (sec,) = [x for x in split_sections(text) if x.text.strip()]
        enqueue_section_job(
            s,
            workspace_id=wid,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sec.sha,
            commit_sha="0" * 40,
        )
        s.commit()

    with pg_session_factory() as s:
        (job,) = claim_queued_jobs(s, limit=1)
        s.commit()
        analyze_job(
            job,
            session=s,
            session_factory=pg_session_factory,
            citation_service=FakeCitationService(),
            embedder=FakeEmbedder(),
            workspaces_dir=tmp_path,
        )
        status = job.status
        s.commit()

    assert status != "superseded", (
        "the drain tick resolved the job's section against stale git HEAD and threw the "
        "job away, so the live section is never analysed"
    )


def test_flag_off_both_ticks_still_read_raw_disk(pg_session_factory, tmp_path):
    """Control. With collab disabled git IS the document and must stay so — a fix that
    ignored the flag would change behaviour for every non-collab workspace, and a test
    that cannot tell the two apart proves nothing."""
    wid = _workspace_with_room_ahead_of_disk(
        pg_session_factory, tmp_path, "ticks-off@umu.se", collab=False
    )
    reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
    assert _enqueued(pg_session_factory, wid) == _shas(STALE_DISK)
