"""0067 creates the one table every hosted-LLM call writes to.

The cap used to sum five tables on three different timestamp columns, so every
new feature had to remember two columns, a write on EVERY terminal path, and a
SELECT in caps.py. Six sites had already got it wrong. This is the one table.
"""

import pytest
from sqlalchemy import inspect, select, text

from grant_lab.db.models import User
from grant_lab.db.models.workspace import LlmSpend, Workspace


def _ws(session):
    u = User(status="active", email="spend@umu.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_table_and_indexes_exist(pg_session_factory, migrated_engine):
    insp = inspect(migrated_engine)
    assert "llm_spend" in insp.get_table_names()
    names = {i["name"] for i in insp.get_indexes("llm_spend")}
    # One index per read the caps make: global-by-day, and per-workspace-by-day.
    assert "ix_llm_spend_created" in names
    assert "ix_llm_spend_ws_created" in names


def test_a_row_round_trips(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(
            LlmSpend(
                workspace_id=ws_id,
                kind="chat_turn",
                ref_id="turn-1",
                status="ok",
                tokens_in=10,
                tokens_out=20,
                model="claude-opus-5",
                sdk_session_id="sess-1",
            )
        )
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (10, 20)
        assert row.created_at is not None  # server default


@pytest.mark.parametrize("bad", [-1, -1000])
def test_negative_tokens_are_refused(pg_session_factory, bad):
    # A negative row would silently REDUCE the day's total and let spend past
    # the cap. The constraint is the only thing stopping a caller's sign error.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="ok",
                       tokens_in=bad, tokens_out=0))
        with pytest.raises(Exception):  # noqa: B017
            s.commit()


def test_an_unknown_status_is_refused(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="maybe",
                       tokens_in=0, tokens_out=0))
        with pytest.raises(Exception):  # noqa: B017
            s.commit()


def test_an_unknown_kind_is_refused(pg_session_factory):
    # `kind` is what makes one table readable. An unconstrained free-text kind
    # would let a typo create a category the caps silently never sum.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="cohernce", status="ok",
                       tokens_in=0, tokens_out=0))
        with pytest.raises(Exception):  # noqa: B017
            s.commit()


def test_deleting_the_workspace_takes_its_rows(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="ok",
                       tokens_in=1, tokens_out=1))
        s.commit()
        s.execute(text("DELETE FROM workspaces WHERE id = :i"), {"i": ws_id})
        s.commit()
        assert s.execute(select(LlmSpend)).first() is None


def test_the_row_is_append_only_by_convention_not_by_trigger(pg_session_factory):
    # Documented, not enforced: there is no UPDATE trigger. The guarantee comes
    # from having exactly one writer (spend.record_spend) and Task 9's gate.
    # This test exists so that claim is written down where it can be read.
    assert not hasattr(LlmSpend, "__immutable__")
