from grant_lab.db.models import ProposedChange
from tests.proposal_assist.conftest import make_workspace


def test_proposed_change_row_roundtrips(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        pc = ProposedChange(
            workspace_id=ws.id,
            target_artifact="draft",
            section_index=1,
            base_commit_sha="a" * 40,
            base_section_sha="b" * 64,
            kind="prose_edit",
            payload={"new_text": "## Methods\nrewritten.\n"},
            origin_kind="interactive_turn",
        )
        s.add(pc)
        s.commit()
        got = s.get(ProposedChange, pc.id)
        assert got.status == "pending"
        assert got.payload["new_text"].startswith("## Methods")
        assert got.created_at is not None
