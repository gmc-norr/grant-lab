"""Citation spend was capped separately and invisible to the "global" cap.

`citation_llm_usage` is its own append-only ledger with its own per-op caps, and
`check_global_daily_token_cap` never summed it — so "global daily token cap" was
false. `record_usage` is the one writer, so it gains one insert.
"""

import pytest
from sqlalchemy import select

from grant_lab.config import Settings
from grant_lab.db.models import CitationLlmUsage, LlmSpend, User, Workspace
from grant_lab.proposal_assist.citations.service import CitationLLMService
from grant_lab.proposal_assist.citations.usage import record_usage
from grant_lab.proposal_assist.spend import record_spend, sum_spend_today


def _ws(session):
    u = User(status="active", email="cite@umu.se")
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
                     tokens_in=40, tokens_out=9, model="gemma", provider="anthropic")
        s.commit()
        cite = s.execute(select(CitationLlmUsage)).scalar_one()
        spend = s.execute(select(LlmSpend)).scalar_one()
        assert (cite.tokens_in, cite.tokens_out) == (40, 9)
        assert (spend.tokens_in, spend.tokens_out) == (40, 9)
        assert spend.kind == "citation"
        # `op` has no column on llm_spend; it survives as ref_id so a row can
        # still be traced back to what made it.
        assert spend.ref_id == "metadata"


def test_citation_spend_counts_toward_the_global_total(pg_session_factory, _settings_env):
    # The unification. Before, this returned (0, 0).
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="ok",
                     tokens_in=40, tokens_out=9, provider="anthropic")
        s.commit()
        assert sum_spend_today(s, settings=Settings()) == (40, 9)


def test_a_failed_citation_call_records_in_both(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="failed",
                     tokens_in=40, tokens_out=0, provider="anthropic")
        s.commit()
        assert s.execute(select(LlmSpend)).scalar_one().status == "failed"


def test_the_two_ledgers_cannot_drift_because_there_is_one_writer(pg_session_factory):
    # Not a behaviour test — a statement of the design, checked. If a second
    # writer for CitationLlmUsage ever appears, this count goes wrong.
    import inspect

    from grant_lab.proposal_assist.citations import usage as mod

    src = inspect.getsource(mod)
    assert src.count("CitationLlmUsage(") == 1, "a second citation writer would drift"


def test_citation_refuses_when_the_GLOBAL_cap_is_reached(
    pg_session_factory, monkeypatch, _settings_env
):
    """Citation spend was unified INTO the global total by this branch — but the
    service only ever consulted its own per-op caps.

    That made it a sixth family that adds to the number and never reads it, which
    is precisely the violation the ledger exists to remove, recreated inside the
    branch removing it. The LLM must not be called at all: `_Boom` raises if it is.
    """
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")

    class _Boom:
        model = "gemma"

        def complete_json(self, *, system, user):  # pragma: no cover - must not run
            raise AssertionError("the LLM was called despite the global cap")

    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(
            s, workspace_id=ws_id, kind="chat_turn", status="ok", tokens_in=5000, tokens_out=0
        )
        s.commit()

    svc = CitationLLMService(
        session_factory=pg_session_factory, llm=_Boom(), settings=Settings()
    )
    result = svc.complete_json(workspace_id=ws_id, op="metadata", system="s", user="u")

    assert result.cap_exceeded is True
    assert result.cap_scope == "global"
    assert result.data is None
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "citation")).first() is None


def test_a_LOCAL_citation_row_does_not_eat_the_hosted_budget(pg_session_factory, _settings_env):
    """Review 2026-10-26: local Gemma citation tokens counted against the
    HOSTED cap, so on-prem extraction could block Opus chat.

    `CITATION_LLM_PROVIDER` defaults to **ollama** — so in the live deployment
    EVERY citation row is on-prem, spends GPU time rather than money, and was
    nonetheless summed into `check_global_daily_token_cap`, whose entire subject
    is the Anthropic bill. A reference-heavy afternoon of local extraction could
    deny a drafting turn.

    The provider is tagged on the row via the EXISTING `model` column (no new
    column, no migration): measured, the raw value cannot tell the two apart —
    it is NULL on both the `failed` and `timeout` paths for either provider, and
    a single `CITATION_LLM_MODEL` feeds both branches of `build_citation_llm`,
    so the `ok` path collides too. `record_spend(local=True)` therefore STAMPS
    the distinction instead of inferring it.

    The citation ledger is untouched: its own per-op caps still see the spend
    (that is where an on-prem volume ceiling belongs), and the row still carries
    the real model id for audit."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="verify", status="ok",
                     tokens_in=40, tokens_out=9, model="gemma4:e4b-it-q4_K_M",
                     provider="ollama")
        s.commit()
        # The row still EXISTS — "we spent nothing" and "we never looked" stay
        # different facts (spend.py's rule), it just is not hosted money.
        spend = s.execute(select(LlmSpend)).scalar_one()
        assert spend.kind == "citation"
        assert (spend.tokens_in, spend.tokens_out) == (40, 9)
        # ...and the citation ledger keeps the real model id for audit.
        assert s.execute(select(CitationLlmUsage)).scalar_one().model == "gemma4:e4b-it-q4_K_M"

        assert sum_spend_today(s, settings=Settings()) == (0, 0)


def test_a_HOSTED_citation_row_still_counts(pg_session_factory, _settings_env):
    """Non-vacuity: the exclusion is on the PROVIDER, not on `kind='citation'`.
    An exclusion that dropped every citation row would undo the unification this
    file exists for."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="verify", status="ok",
                     tokens_in=40, tokens_out=9, model="claude-sonnet-5-5",
                     provider="anthropic")
        s.commit()
        assert sum_spend_today(s, settings=Settings()) == (40, 9)


def test_an_unknown_provider_is_treated_as_hosted(pg_session_factory, _settings_env):
    """Fail toward COUNTING. A provider name this module does not recognise as
    on-prem must not silently stop billing the cap; over-counting is the safe
    direction for a spend guardrail. (`build_citation_llm` refuses unknown
    providers outright, so this is defence in depth.)"""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="verify", status="ok",
                     tokens_in=11, tokens_out=0, provider="some-new-cloud")
        s.commit()
        assert sum_spend_today(s, settings=Settings()) == (11, 0)


def test_the_two_caps_charge_the_same_timeout_the_same_amount(pg_session_factory, _settings_env):
    """The M7 rule, applied by BOTH readers to the one row `record_usage` writes.

    `citations/usage.py::_sum_today` charges a `timeout` at
    `citation_llm_max_output_tokens`; `sum_spend_today` now does too. If the two
    expressions ever diverge the same row means different money to the per-op
    cap and the global cap, which is the asymmetry this closes."""
    from grant_lab.proposal_assist.citations.usage import _sum_today

    settings = Settings()
    with pg_session_factory() as s:
        ws_id = _ws(s)
        # A hosted timeout: the server may have finished and billed the reply,
        # but the client never saw a count, so both ledgers hold 0 tokens.
        record_usage(s, workspace_id=ws_id, op="verify", status="timeout",
                     tokens_in=0, tokens_out=0, model=None, provider="anthropic")
        s.commit()
        citation_in, citation_out = _sum_today(s, workspace_id=ws_id, settings=settings)
        global_in, global_out = sum_spend_today(s, settings=settings)
        assert (global_in, global_out) == (citation_in, citation_out)
        assert global_out == settings.citation_llm_max_output_tokens


def test_a_LOCAL_timeout_is_not_charged_to_the_hosted_cap_either(pg_session_factory, _settings_env):
    """The timeout CHARGE follows the same provider rule as the tokens, or the
    exclusion would leak the charge it was meant to exclude."""
    settings = Settings()
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="verify", status="timeout",
                     tokens_in=0, tokens_out=0, model=None, provider="ollama")
        s.commit()
        assert sum_spend_today(s, settings=settings) == (0, 0)


def test_the_exclusion_is_decided_by_the_column_text_alone(pg_session_factory, _settings_env):
    """Why the writes below must be guarded: compiled, `sum_spend_today`'s
    predicate is

        llm_spend.model IS NULL OR (llm_spend.model NOT LIKE 'local:' || '%')

    — no reference to the `local` flag that wrote the row. The tag is in-band
    by design (no new column, no migration), so the ONLY thing separating a
    hosted row from an invisible one is that nobody but `record_spend(local=
    True)` writes those six characters. Here the row is inserted through the
    ORM, bypassing the writer, and 40 hosted input tokens vanish."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(
            LlmSpend(
                workspace_id=ws_id,
                kind="citation",
                status="ok",
                tokens_in=40,
                tokens_out=9,
                model="local:claude-sonnet-5-5",
            )
        )
        s.commit()
        assert s.execute(select(LlmSpend)).scalar_one().tokens_in == 40
        assert sum_spend_today(s, settings=Settings()) == (0, 0)


def test_record_spend_refuses_a_HOSTED_row_carrying_the_local_tag(pg_session_factory):
    """The guard. `local=False` (the default for every kind but a citation call
    on an on-prem provider) may not supply a `local:`-prefixed model: the row
    would read as on-prem to every hosted cap for the rest of the Stockholm day.

    Refused rather than recorded-and-counted: the prefix is a stamp this writer
    owns, and a caller holding a string that starts with it is wrong about which
    provider spent the money. Raised BEFORE the insert, so nothing lands."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        with pytest.raises(ValueError, match="may not carry"):
            record_spend(
                s,
                workspace_id=ws_id,
                kind="citation",
                status="ok",
                tokens_in=40,
                tokens_out=9,
                model="local:claude-sonnet-5-5",
            )
        assert s.execute(select(LlmSpend)).first() is None


def test_CITATION_LLM_MODEL_cannot_forge_the_local_tag(_settings_env, monkeypatch):
    """The other entry point, and the only operator-settable one: ONE
    CITATION_LLM_MODEL feeds both branches of `build_citation_llm`, so
    `CITATION_LLM_PROVIDER=anthropic` with `CITATION_LLM_MODEL=local:anything`
    writes a HOSTED row that `sum_spend_today` excludes — real Anthropic spend
    no cap sees. Refused at Settings() construction, so a deploy carrying it
    cannot reach a paid call at all; the `record_spend` guard above would only
    fire per row, after the money was spent."""
    from pydantic import ValidationError

    monkeypatch.setenv("CITATION_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CITATION_LLM_MODEL", "local:claude-sonnet-5-5")
    with pytest.raises(ValidationError, match="CITATION_LLM_MODEL must not start with"):
        Settings()

    # Non-vacuity: the bare id this forges is itself fine.
    monkeypatch.setenv("CITATION_LLM_MODEL", "claude-sonnet-5-5")
    assert Settings().citation_llm_model == "claude-sonnet-5-5"


def test_the_local_tag_is_spelled_the_same_in_config_and_in_the_ledger():
    """config.py duplicates the prefix rather than importing it: `Settings()` is
    constructed in the match-edition boot, which must load ZERO proposal-assist
    modules (tests/architecture/test_match_edition_process_cut.py). Duplication
    needs a pin, or the validator could one day refuse a different string than
    the predicate excludes."""
    from grant_lab.config import _LOCAL_SPEND_TAG
    from grant_lab.proposal_assist.spend import LOCAL_MODEL_PREFIX

    assert _LOCAL_SPEND_TAG == LOCAL_MODEL_PREFIX

