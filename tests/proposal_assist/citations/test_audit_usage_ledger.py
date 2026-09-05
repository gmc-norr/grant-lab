"""Usage-ledger coverage for the audit LLM ops (Slice 3 Task 8).

Proves that the two audit-path LLM operations -- ``validate_citation``
(semantic judge) and ``list_unsupported`` (uncited-claim judge) -- record a
``citation_llm_usage`` row when driven through a REAL ``CitationLLMService``
(with a stubbed INNER llm), NOT a stubbed service. This is the part that proves
audit calls go through the capped/ledgered path: the same code that
``check_citation_caps`` later reads.

Seeding mirrors test_audit_semantic.py (Reference + ReferenceChunk + a live
marker so the row is judgeable) and test_list_unsupported_claims.py (_init_repo
+ ToolContext). The fake inner llm mirrors test_service.py's ``_OkLLM``: a
``complete_json(system=, user=)`` returning ``LLMResult(data=, usage=, model=)``
with nonzero tokens.
"""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from grant_lab.config import Settings
from grant_lab.db.models import CitationLlmUsage, Reference, ReferenceChunk
from grant_lab.proposal_assist.citations.audit import (
    chunk_content_sha,
    claim_context_sha,
    validate_citations_semantic,
)
from grant_lab.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    empty_ledger,
)
from grant_lab.proposal_assist.citations.llm import LLMResult, LLMUsage
from grant_lab.proposal_assist.citations.markers import build_marker
from grant_lab.proposal_assist.citations.service import CitationLLMService
from grant_lab.proposal_assist.tools.audit import list_unsupported_claims_impl
from grant_lab.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from grant_lab.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024

CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
EVIDENCE_QUOTE = "reduces recurrence by 30%"
EV_ID = "ev-aaaaaaaaaaaa"
CITEKEY = "smith2021"

TOKENS_IN = 42
TOKENS_OUT = 7
MODEL = "fake-judge"


class _FakeInnerLLM:
    """Mirrors test_service.py's _OkLLM: the inner-call contract of
    CitationLLMService is ``complete_json(system=, user=) -> LLMResult``.
    Returns a preset JSON payload + nonzero usage."""

    def __init__(self, data: dict):
        self._data = data
        self.called = 0

    def complete_json(self, *, system, user) -> LLMResult:
        self.called += 1
        return LLMResult(
            data=self._data,
            usage=LLMUsage(tokens_in=TOKENS_IN, tokens_out=TOKENS_OUT),
            model=MODEL,
        )


def _real_service(pg_session_factory, data: dict) -> CitationLLMService:
    """A REAL CitationLLMService (caps + ledger enforced) wrapping a fake inner
    llm -- the whole point of this test is that the ledger row is written by the
    service, not a stub."""
    return CitationLLMService(
        session_factory=pg_session_factory, llm=_FakeInnerLLM(data), settings=Settings()
    )


def _rows(pg_session_factory, wsid, op):
    with pg_session_factory() as s:
        return list(
            s.scalars(
                select(CitationLlmUsage).where(
                    CitationLlmUsage.workspace_id == wsid,
                    CitationLlmUsage.op == op,
                )
            )
        )


def _make_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        return ws.id


def _seed_reference(pg_session_factory, workspace_id, *, chunk_text=CHUNK_TEXT, citekey=CITEKEY):
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=workspace_id,
                chunk_index=0,
                section_path="Results",
                text=chunk_text,
                embedding=[0.0] * EMBED_DIM,
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    return rid


def _proposal_with_marker(claim_lead, ev_id=EV_ID, citekey=CITEKEY) -> str:
    return f"# Proposal\n\n{claim_lead}{build_marker(citekey, ev_id)}.\n"


def _row(rid, *, stance="supports", chunk_text=CHUNK_TEXT, claim_text="placeholder"):
    return build_evidence_row(
        ev_id=EV_ID,
        citekey=CITEKEY,
        reference_id=str(rid),
        chunk_index=0,
        section_path="Results",
        source_content_hash=None,
        chunk_content_hash=chunk_content_sha(chunk_text),
        evidence_quote=EVIDENCE_QUOTE,
        evidence_quote_offsets=[0, 0],
        stance=stance,
        confidence=None,
        claim_context_text=claim_text,
        claim_context_hash=claim_context_sha(claim_text),
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


# --------------------------------------------------------------------------- #
# validate_citation op -> ledger row
# --------------------------------------------------------------------------- #
def test_validate_citation_op_recorded_in_ledger(pg_session_factory, _settings_env):
    """Driving the semantic judge through a REAL CitationLLMService records a
    citation_llm_usage row with op='validate_citation', status='ok', tokens."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    # Valid verdict for the row's stance so the row is judged (not degraded).
    service = _real_service(pg_session_factory, {"verdict": "supports", "rationale": "ok"})

    with pg_session_factory() as s:
        validate_citations_semantic(
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            session_factory=pg_session_factory,
            workspace_id=wsid,
            citation_service=service,
            deterministic_issues=[],
        )

    rows = _rows(pg_session_factory, wsid, "validate_citation")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.status == "ok"
    assert row.op == "validate_citation"
    assert row.tokens_in == TOKENS_IN
    assert row.tokens_out == TOKENS_OUT
    assert row.model == MODEL


# --------------------------------------------------------------------------- #
# list_unsupported op -> ledger row
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_unsupported_op_recorded_in_ledger(pg_session_factory, _settings_env, tmp_path):
    """Driving list_unsupported_claims_impl through a REAL CitationLLMService
    records a citation_llm_usage row with op='list_unsupported', status='ok'."""
    wsid = _make_workspace(pg_session_factory)
    repo_dir = tmp_path / "repo"
    # One uncited factual sentence -> exactly one judge call.
    _init_repo(repo_dir, "# Proposal\n\nAdjuvant therapy reduces recurrence by thirty percent.\n")
    service = _real_service(pg_session_factory, {"needs_citation": True, "suggested_query": "x"})
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings(),
        citation_service=service,
    )

    out = await list_unsupported_claims_impl(ctx)
    assert out["semantic_available"] is True

    rows = _rows(pg_session_factory, wsid, "list_unsupported")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.status == "ok"
    assert row.op == "list_unsupported"
    assert row.tokens_in == TOKENS_IN
    assert row.tokens_out == TOKENS_OUT
    assert row.model == MODEL


# --------------------------------------------------------------------------- #
# the rows are visible to the cap checker (caps apply to audit)
# --------------------------------------------------------------------------- #
def test_audit_rows_visible_to_cap_checker(pg_session_factory, _settings_env):
    """The ledger rows written by audit ops are exactly what check_citation_caps
    sums -- proving caps apply to audit calls."""
    from grant_lab.proposal_assist.citations.usage import check_citation_caps

    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    service = _real_service(pg_session_factory, {"verdict": "supports", "rationale": "ok"})
    with pg_session_factory() as s:
        validate_citations_semantic(
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            session_factory=pg_session_factory,
            workspace_id=wsid,
            citation_service=service,
            deterministic_issues=[],
        )

    settings = Settings()
    # Tighten the workspace input cap to the tokens we just spent: the audit row
    # alone must trip the cap (check uses >=), proving caps see audit usage.
    object.__setattr__(settings, "citation_llm_daily_token_cap_input", TOKENS_IN)
    with pg_session_factory() as s:
        cap = check_citation_caps(s, wsid, settings=settings)
    assert cap.exceeded is True
