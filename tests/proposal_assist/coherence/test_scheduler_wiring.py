from call_manager.config import Settings
from call_manager.db.models import CoherenceJob
from call_manager.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from tests.proposal_assist.conftest import make_workspace


def test_drain_tick_claims_and_runs_a_queued_job(tmp_path, pg_session_factory, _settings_env):
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text("## Aims\n\nD.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nS.\n")

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=10, tokens_out=5, model="m", session_id="s")])

    asyncio.run(
        drain_coherence_queue_tick(
            session_factory=pg_session_factory,
            settings=settings,
            adapter_builder=lambda **kw: fake,
        )
    )

    with pg_session_factory() as s:
        job = s.query(CoherenceJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "completed"
