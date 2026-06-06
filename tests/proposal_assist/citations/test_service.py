from call_manager.db.models import CitationLlmUsage
from call_manager.proposal_assist.citations.llm import LLMResult, LLMUsage
from call_manager.proposal_assist.citations.service import CitationLLMService
from tests.proposal_assist.conftest import make_user, make_workspace


def _settings(**over):
    import os

    for k, v in {
        "DATABASE_URL": "postgresql://x",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://localhost:8000",
    }.items():
        os.environ.setdefault(k, v)
    from call_manager.config import Settings

    s = Settings()
    for k, v in over.items():
        object.__setattr__(s, k, v)
    return s


def _ws(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        return ws.id


class _OkLLM:
    def __init__(self):
        self.called = 0

    def complete_json(self, *, system, user):
        self.called += 1
        return LLMResult(data={"title": "T"}, usage=LLMUsage(tokens_in=30, tokens_out=5), model="m")


class _BoomLLM:
    def __init__(self, exc):
        self._exc = exc
        self.called = 0

    def complete_json(self, *, system, user):
        self.called += 1
        raise self._exc


def _rows(pg_session_factory, wsid):
    from sqlalchemy import select

    with pg_session_factory() as s:
        return list(
            s.scalars(select(CitationLlmUsage).where(CitationLlmUsage.workspace_id == wsid))
        )


def test_success_records_ok(pg_session_factory):
    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(session_factory=pg_session_factory, llm=_OkLLM(), settings=_settings())
    res = svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    assert res.cap_exceeded is False and res.data == {"title": "T"}
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].tokens_in == 30


def test_failure_records_failed_and_raises(pg_session_factory):
    import pytest

    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(
        session_factory=pg_session_factory,
        llm=_BoomLLM(ValueError("x")),
        settings=_settings(),
    )
    with pytest.raises(ValueError):
        svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "failed"


def test_timeout_records_timeout_and_raises(pg_session_factory):
    import pytest

    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(
        session_factory=pg_session_factory,
        llm=_BoomLLM(TimeoutError()),
        settings=_settings(),
    )
    with pytest.raises(TimeoutError):
        svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "timeout"


def test_cap_exceeded_returns_typed_result_without_calling(pg_session_factory):
    wsid = _ws(pg_session_factory)
    llm = _OkLLM()
    svc = CitationLLMService(
        session_factory=pg_session_factory,
        llm=llm,
        settings=_settings(citation_llm_daily_token_cap_input=20),
    )
    with pg_session_factory() as s:
        s.add(
            CitationLlmUsage(
                workspace_id=wsid,
                op="metadata_extract",
                status="ok",
                tokens_in=25,
                tokens_out=0,
            )
        )
        s.commit()
    res = svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    assert res.cap_exceeded is True and res.cap_scope == "workspace"
    assert res.data is None
    assert llm.called == 0  # client NOT invoked when capped
    assert len(_rows(pg_session_factory, wsid)) == 1  # no new row for the blocked call


def test_global_cap_blocks_without_calling(pg_session_factory):
    wsid = _ws(pg_session_factory)
    llm = _OkLLM()
    # Workspace cap high, global cap low → the global branch must trip.
    svc = CitationLLMService(
        session_factory=pg_session_factory,
        llm=llm,
        settings=_settings(
            citation_llm_daily_token_cap_input=10_000_000,
            global_citation_llm_daily_token_cap_input=20,
        ),
    )
    with pg_session_factory() as s:
        s.add(
            CitationLlmUsage(
                workspace_id=wsid, op="metadata_extract", status="ok", tokens_in=25, tokens_out=0
            )
        )
        s.commit()
    res = svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    assert res.cap_exceeded is True and res.cap_scope == "global"
    assert llm.called == 0
    assert len(_rows(pg_session_factory, wsid)) == 1
