"""Tests for the semantic citation judge (Slice 3 Task 4).

The semantic layer judges the LIVE claim vs the evidence quote via the enforced
CitationLLMService, with a content-keyed verdict cache. It runs AFTER the
deterministic layer and consumes its issues to skip un-judgeable rows.

Patterns reused from test_audit_deterministic.py (seeding) and
test_metadata_extractor.py (the _StubCitationService).
"""

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import CitationLLMResult
from call_manager.proposal_assist.citations.audit import (
    chunk_content_sha,
    claim_context_sha,
    evidence_quote_sha,
    validate_citations_semantic,
)
from call_manager.proposal_assist.citations.claims import claim_sentence_around
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    empty_ledger,
)
from call_manager.proposal_assist.citations.markers import build_marker, find_markers
from call_manager.proposal_assist.citations.verdict_cache import (
    VALIDATOR_VERSION,
    build_cache_key,
    get_verdict,
    store_verdict,
)
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024

CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
EVIDENCE_QUOTE = "reduces recurrence by 30%"
EV_ID = "ev-aaaaaaaaaaaa"
CITEKEY = "smith2021"


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class _StubCitationService:
    """Records calls; returns a single preset CitationLLMResult."""

    def __init__(self, result: CitationLLMResult):
        self._result = result
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"workspace_id": workspace_id, "op": op, "system": system, "user": user})
        return self._result


class _RaisingCitationService:
    """complete_json raises for the first N calls, then returns a preset result."""

    def __init__(self, result: CitationLLMResult, *, raise_times: int = 1):
        self._result = result
        self._raise_times = raise_times
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"op": op, "user": user})
        if len(self.calls) <= self._raise_times:
            raise RuntimeError("LLM unavailable")
        return self._result


def _verdict_result(verdict, rationale="because reasons", *, cap_exceeded=False, cap_scope=None):
    return CitationLLMResult(
        data=None if cap_exceeded else {"verdict": verdict, "rationale": rationale},
        usage=None,
        model="stub-model",
        cap_exceeded=cap_exceeded,
        cap_scope=cap_scope,
    )


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
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


def _live_claim(proposal_text, ev_id=EV_ID) -> str:
    m = next(x for x in find_markers(proposal_text) if x["ev_id"] == ev_id)
    return claim_sentence_around(proposal_text, m["start"], m["end"])


def _row(
    rid,
    *,
    ev_id=EV_ID,
    citekey=CITEKEY,
    stance="supports",
    evidence_quote=EVIDENCE_QUOTE,
    chunk_text=CHUNK_TEXT,
    claim_text="placeholder",
):
    return build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(rid),
        chunk_index=0,
        section_path="Results",
        source_content_hash=None,
        chunk_content_hash=chunk_content_sha(chunk_text),
        evidence_quote=evidence_quote,
        evidence_quote_offsets=[0, 0],
        stance=stance,
        confidence=None,
        claim_context_text=claim_text,
        claim_context_hash=claim_context_sha(claim_text),
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _run(stub, *, proposal_text, ledger, session, pg_session_factory, wsid, det_issues=None):
    return validate_citations_semantic(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        session_factory=pg_session_factory,
        workspace_id=wsid,
        citation_service=stub,
        deterministic_issues=det_issues or [],
    )


def _cache_key_for(proposal_text, *, chunk_text, evidence_quote, stance, ev_id=EV_ID):
    live = _live_claim(proposal_text, ev_id)
    return build_cache_key(
        claim_hash=claim_context_sha(live),
        chunk_content_hash=chunk_content_sha(chunk_text),
        evidence_quote_hash=evidence_quote_sha(evidence_quote),
        stance=stance,
        validator_version=VALIDATOR_VERSION,
    )


# --------------------------------------------------------------------------- #
# supports / contradicts / unsupported
# --------------------------------------------------------------------------- #
def test_supports_no_issue(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("supports"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert issues == []
    assert len(stub.calls) == 1
    assert stub.calls[0]["op"] == "validate_citation"
    assert stub.calls[0]["workspace_id"] == wsid


def test_contradicts_error_issue(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("contradicts"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert len(issues) == 1
    assert issues[0]["type"] == "semantic_contradicts"
    assert issues[0]["severity"] == "error"
    assert issues[0]["evidence_id"] == EV_ID
    assert issues[0]["citekey"] == CITEKEY


def test_unsupported_warning_issue(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("unsupported"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert len(issues) == 1
    assert issues[0]["type"] == "semantic_unsupported"
    assert issues[0]["severity"] == "warning"


# --------------------------------------------------------------------------- #
# background stance: relevant / irrelevant + allowed-set guard
# --------------------------------------------------------------------------- #
def test_background_irrelevant_warning(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("This is background context")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="background"))
    stub = _StubCitationService(_verdict_result("irrelevant"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert len(issues) == 1
    assert issues[0]["type"] == "semantic_irrelevant"
    assert issues[0]["severity"] == "warning"
    # user message must steer to the background verdict set
    assert "relevant" in stub.calls[0]["user"]


def test_background_relevant_no_issue(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("This is background context")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="background"))
    stub = _StubCitationService(_verdict_result("relevant"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert issues == []


def test_invalid_verdict_for_stance_is_audit_unavailable(pg_session_factory, _settings_env):
    """A 'supports' verdict for a background-stance row is invalid → audit_unavailable,
    not a crash and not a semantic issue."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("This is background context")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="background"))
    stub = _StubCitationService(_verdict_result("supports"))  # not in {relevant,irrelevant}
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert len(issues) == 1
    assert issues[0]["type"] == "audit_unavailable"
    assert issues[0]["severity"] == "warning"


# --------------------------------------------------------------------------- #
# cache hit avoids the LLM call
# --------------------------------------------------------------------------- #
def test_cache_hit_avoids_llm(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    key = _cache_key_for(
        proposal_text, chunk_text=CHUNK_TEXT, evidence_quote=EVIDENCE_QUOTE, stance="supports"
    )
    store_verdict(
        pg_session_factory,
        workspace_id=wsid,
        cache_key=key,
        verdict="contradicts",
        rationale="cached",
        validator_version=VALIDATOR_VERSION,
    )
    stub = _StubCitationService(_verdict_result("supports"))  # would say supports if called
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert stub.calls == [], "cache hit must not call the LLM"
    # cached verdict 'contradicts' must be applied
    assert len(issues) == 1
    assert issues[0]["type"] == "semantic_contradicts"


# --------------------------------------------------------------------------- #
# claim_drift bypasses cache READ (always re-judges)
# --------------------------------------------------------------------------- #
def test_claim_drift_bypasses_cache(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    key = _cache_key_for(
        proposal_text, chunk_text=CHUNK_TEXT, evidence_quote=EVIDENCE_QUOTE, stance="supports"
    )
    store_verdict(
        pg_session_factory,
        workspace_id=wsid,
        cache_key=key,
        verdict="supports",
        rationale="cached",
        validator_version=VALIDATOR_VERSION,
    )
    det_issues = [
        {
            "type": "claim_drift",
            "severity": "error",
            "citekey": CITEKEY,
            "evidence_id": EV_ID,
            "location": "x",
            "detail": "drift",
        }
    ]
    stub = _StubCitationService(_verdict_result("contradicts"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
            det_issues=det_issues,
        )
    assert len(stub.calls) == 1, "claim_drift must re-judge despite a cached verdict"
    assert any(i["type"] == "semantic_contradicts" for i in issues)


# --------------------------------------------------------------------------- #
# cap_exceeded → audit_unavailable + stop
# --------------------------------------------------------------------------- #
def test_cap_exceeded_stops(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    # two rows, both judgeable
    rid2 = _seed_reference(pg_session_factory, wsid, citekey="jones2020")
    proposal_text = (
        "# Proposal\n\n"
        f"Adjuvant therapy cuts recurrence{build_marker(CITEKEY, EV_ID)}. "
        f"More evidence here{build_marker('jones2020', 'ev-bbbbbbbbbbbb')}.\n"
    )
    ledger = empty_ledger()
    ledger = append_evidence(ledger, _row(rid, stance="supports"))
    ledger = append_evidence(
        ledger, _row(rid2, ev_id="ev-bbbbbbbbbbbb", citekey="jones2020", stance="supports")
    )
    stub = _StubCitationService(_verdict_result(None, cap_exceeded=True, cap_scope="workspace"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    unavailable = [i for i in issues if i["type"] == "audit_unavailable"]
    assert len(unavailable) == 1, "exactly one audit_unavailable then stop"
    assert "semantic_contradicts" not in {i["type"] for i in issues}
    # stopped after first cap: only one call attempted
    assert len(stub.calls) == 1


# --------------------------------------------------------------------------- #
# exception → audit_unavailable + continue
# --------------------------------------------------------------------------- #
def test_exception_continues(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    rid2 = _seed_reference(pg_session_factory, wsid, citekey="jones2020")
    proposal_text = (
        "# Proposal\n\n"
        f"Adjuvant therapy cuts recurrence{build_marker(CITEKEY, EV_ID)}. "
        f"More evidence here{build_marker('jones2020', 'ev-bbbbbbbbbbbb')}.\n"
    )
    ledger = empty_ledger()
    ledger = append_evidence(ledger, _row(rid, stance="supports"))
    ledger = append_evidence(
        ledger, _row(rid2, ev_id="ev-bbbbbbbbbbbb", citekey="jones2020", stance="supports")
    )
    # First call raises, second returns contradicts
    stub = _RaisingCitationService(_verdict_result("contradicts"), raise_times=1)
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    types = [i["type"] for i in issues]
    assert "audit_unavailable" in types, "the raising row yields audit_unavailable"
    assert "semantic_contradicts" in types, "the other row is still judged"
    assert len(stub.calls) == 2


# --------------------------------------------------------------------------- #
# skip set: a row flagged with a SKIP-type det issue is NOT judged
# --------------------------------------------------------------------------- #
def test_skip_set_not_judged(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    for skip_type in ("chunk_missing", "quote_missing", "orphan_row"):
        det_issues = [
            {
                "type": skip_type,
                "severity": "error",
                "citekey": CITEKEY,
                "evidence_id": EV_ID,
                "location": "x",
                "detail": "d",
            }
        ]
        stub = _StubCitationService(_verdict_result("contradicts"))
        with pg_session_factory() as s:
            issues = _run(
                stub,
                proposal_text=proposal_text,
                ledger=ledger,
                session=s,
                pg_session_factory=pg_session_factory,
                wsid=wsid,
                det_issues=det_issues,
            )
        assert stub.calls == [], f"{skip_type} row must not be judged"
        assert issues == [], f"{skip_type} row must produce no semantic issue"


# --------------------------------------------------------------------------- #
# verdict stored on miss
# --------------------------------------------------------------------------- #
def test_verdict_stored_on_miss(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("unsupported", rationale="weak link"))
    with pg_session_factory() as s:
        _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    key = _cache_key_for(
        proposal_text, chunk_text=CHUNK_TEXT, evidence_quote=EVIDENCE_QUOTE, stance="supports"
    )
    with pg_session_factory() as s:
        cached = get_verdict(s, workspace_id=wsid, cache_key=key)
    assert cached is not None
    assert cached.verdict == "unsupported"
    assert cached.rationale == "weak link"


# --------------------------------------------------------------------------- #
# privacy: no raw chunk text in issue detail
# --------------------------------------------------------------------------- #
def test_no_chunk_text_in_detail(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("contradicts", rationale="rationale text ok"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    for i in issues:
        assert CHUNK_TEXT not in i["detail"]
        # a distinctive substring of the chunk must not leak either
        assert "Secondary endpoints" not in i["detail"]


# --------------------------------------------------------------------------- #
# never raises; unknown stance skipped
# --------------------------------------------------------------------------- #
def test_unknown_stance_skipped(pg_session_factory, _settings_env):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = _proposal_with_marker("Adjuvant therapy cuts recurrence")
    ledger = append_evidence(empty_ledger(), _row(rid, stance="mystery"))
    stub = _StubCitationService(_verdict_result("supports"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert stub.calls == []
    assert issues == []


def test_no_marker_row_skipped(pg_session_factory, _settings_env):
    """A ledger row with no live marker (no claim to judge) is skipped."""
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid)
    proposal_text = "# Proposal\n\nNothing cited here.\n"
    ledger = append_evidence(empty_ledger(), _row(rid, stance="supports"))
    stub = _StubCitationService(_verdict_result("contradicts"))
    with pg_session_factory() as s:
        issues = _run(
            stub,
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            pg_session_factory=pg_session_factory,
            wsid=wsid,
        )
    assert stub.calls == []
    assert issues == []
