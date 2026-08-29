"""Citation spend was capped separately and invisible to the "global" cap.

`citation_llm_usage` is its own append-only ledger with its own per-op caps, and
`check_global_daily_token_cap` never summed it — so "global daily token cap" was
false. `record_usage` is the one writer, so it gains one insert.
"""

from sqlalchemy import select

from call_manager.db.models import CitationLlmUsage, LlmSpend, User, Workspace
from call_manager.proposal_assist.citations.usage import record_usage
from call_manager.proposal_assist.spend import sum_spend_today


def _ws(session):
    u = User(status="active", email="cite@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_record_usage_writes_both_ledgers(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="ok",
                     tokens_in=40, tokens_out=9, model="gemma")
        s.commit()
        cite = s.execute(select(CitationLlmUsage)).scalar_one()
        spend = s.execute(select(LlmSpend)).scalar_one()
        assert (cite.tokens_in, cite.tokens_out) == (40, 9)
        assert (spend.tokens_in, spend.tokens_out) == (40, 9)
        assert spend.kind == "citation"
        # `op` has no column on llm_spend; it survives as ref_id so a row can
        # still be traced back to what made it.
        assert spend.ref_id == "metadata"


def test_citation_spend_counts_toward_the_global_total(pg_session_factory):
    # The unification. Before, this returned (0, 0).
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="ok",
                     tokens_in=40, tokens_out=9)
        s.commit()
        assert sum_spend_today(s) == (40, 9)


def test_a_failed_citation_call_records_in_both(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="failed",
                     tokens_in=40, tokens_out=0)
        s.commit()
        assert s.execute(select(LlmSpend)).scalar_one().status == "failed"


def test_the_two_ledgers_cannot_drift_because_there_is_one_writer(pg_session_factory):
    # Not a behaviour test — a statement of the design, checked. If a second
    # writer for CitationLlmUsage ever appears, this count goes wrong.
    import inspect

    from call_manager.proposal_assist.citations import usage as mod

    src = inspect.getsource(mod)
    assert src.count("CitationLlmUsage(") == 1, "a second citation writer would drift"
