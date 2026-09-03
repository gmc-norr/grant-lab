"""Defect-2 regression tests for the deep-check LLM fan-out cap.

The POST /citation-audit/deep endpoint used to make one LLM call per uncached
evidence row with NO per-request ceiling, making it an unbounded, synchronous
fan-out.

After the fix:
- CITATION_DEEP_CHECK_MAX_PER_REQUEST (default 25) limits how many FRESH
  (uncached) LLM calls are made per request.
- Already-cached rows are returned as before (no change to cached behaviour).
- The response carries a new field `deferred` (count of rows that could not be
  judged this request due to the cap); `checked` reflects only the rows
  actually judged this request.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from call_manager.db.models import Reference, ReferenceChunk, User, Workspace
from call_manager.main import create_app
from call_manager.proposal_assist.citations import CitationLLMResult
from call_manager.proposal_assist.citations.audit import chunk_content_sha, claim_context_sha
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    empty_ledger,
)
from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.paths import workspace_repo_dir

EMBED_DIM = 1024
CHUNK_TEXT = "Evidence text about therapy reducing recurrence."
EVIDENCE_QUOTE = "reducing recurrence"


class _CountingCitationService:
    """Records how many LLM calls are made; always returns 'supports'."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"op": op})
        return CitationLLMResult(
            data={"verdict": "supports", "rationale": "looks good"},
            usage=None,
            model="stub-model",
            cap_exceeded=False,
            cap_scope=None,
        )


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(pg_session_factory, *, email="defect2-owner@umu.se"):
    with pg_session_factory() as s:
        owner = User(status="active", email=email)
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.commit()
        return owner.id, ws.id


def _seed_many_references(pg_session_factory, ws_id, *, count: int):
    """Seed `count` references + chunks in ws_id, return list of (ref_id, citekey)."""
    results = []
    with pg_session_factory() as s:
        for i in range(count):
            citekey = f"smith{2000 + i}"
            ref = Reference(
                workspace_id=ws_id,
                kind="paper",
                source_type="uploaded",
                filename=f"ref{i}.pdf",
                storage_path=f"ref{i}/ref{i}.pdf",
                mime_type="application/pdf",
                citekey=citekey,
                metadata_status="approved",
            )
            s.add(ref)
            s.flush()
            s.add(
                ReferenceChunk(
                    reference_id=ref.id,
                    workspace_id=ws_id,
                    chunk_index=0,
                    section_path="Results",
                    text=CHUNK_TEXT,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
            results.append((ref.id, citekey))
        s.commit()
    return results


def _build_ledger_and_proposal(refs):
    """Build a ledger + proposal with one evidence row + marker per reference."""
    ledger = empty_ledger()
    proposal_parts = ["# Proposal\n\n"]
    for i, (rid, citekey) in enumerate(refs):
        ev_id = f"ev-{i:012x}"
        claim_text = "placeholder"
        row = build_evidence_row(
            ev_id=ev_id,
            citekey=citekey,
            reference_id=str(rid),
            chunk_index=0,
            section_path="Results",
            source_content_hash=None,
            chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
            evidence_quote=EVIDENCE_QUOTE,
            evidence_quote_offsets=[0, 25],
            stance="supports",
            confidence=None,
            claim_context_text=claim_text,
            claim_context_hash=claim_context_sha(claim_text),
            citation_snapshot=f"{citekey} et al",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        ledger = append_evidence(ledger, row)
        proposal_parts.append(
            f"Therapy reduces recurrence{build_marker(citekey, ev_id)}.\n\n"
        )
    return ledger, "".join(proposal_parts)


def _write(tmp_path, ws_id, *, proposal, citations):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(proposal)
    (repo / "citations.json").write_text(citations)


def _client(app, user_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


# ---------------------------------------------------------------------------
# The core Defect-2 regression test
# ---------------------------------------------------------------------------

def test_deep_check_cap_limits_fresh_llm_calls(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """When there are more uncached rows than CITATION_DEEP_CHECK_MAX_PER_REQUEST,
    the handler must:
    - Make at most cap fresh LLM calls (not one per row).
    - Return deferred > 0 in the response.
    - Return checked == number of rows actually judged this request.
    """
    CAP = 3  # tiny cap so we don't need many rows

    app = _app(monkeypatch, pg_url, tmp_path)
    # Override the cap on the already-created Settings object (post-create).
    app.state.settings.citation_deep_check_max_per_request = CAP

    owner_id, ws_id = _seed(pg_session_factory, email="d2s1@umu.se")
    app.state.session_factory = pg_session_factory

    # Seed more references than the cap.
    refs = _seed_many_references(pg_session_factory, ws_id, count=CAP + 2)
    ledger, proposal_text = _build_ledger_and_proposal(refs)
    _write(tmp_path, ws_id, proposal=proposal_text, citations=dump_ledger(ledger))

    stub = _CountingCitationService()
    app.state.citation_service = stub

    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/citation-audit/deep")
    assert r.status_code == 200, r.text
    body = r.json()

    # At most CAP fresh LLM calls must have been made.
    assert len(stub.calls) <= CAP, (
        f"Expected at most {CAP} LLM calls, made {len(stub.calls)}"
    )

    # checked == rows actually judged this request
    assert body["checked"] == len(stub.calls), (
        f"body['checked']={body['checked']} but stub.calls={len(stub.calls)}"
    )

    # deferred field must be present and > 0
    assert "deferred" in body, "Response must include 'deferred' field"
    assert body["deferred"] > 0, (
        f"Expected deferred > 0 when rows exceed cap, got {body['deferred']}"
    )

    # total rows = checked + deferred
    total_rows = len(refs)
    assert body["checked"] + body["deferred"] == total_rows, (
        f"checked({body['checked']}) + deferred({body['deferred']}) "
        f"!= total_rows({total_rows})"
    )


def test_deep_check_deferred_zero_when_within_limit(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """When the number of uncached rows is within the cap, deferred==0 and all
    rows are judged."""
    app = _app(monkeypatch, pg_url, tmp_path)
    # Use a large cap so 2 rows are well within it.
    app.state.settings.citation_deep_check_max_per_request = 25

    owner_id, ws_id = _seed(pg_session_factory, email="d2s2@umu.se")
    app.state.session_factory = pg_session_factory

    refs = _seed_many_references(pg_session_factory, ws_id, count=2)
    ledger, proposal_text = _build_ledger_and_proposal(refs)
    _write(tmp_path, ws_id, proposal=proposal_text, citations=dump_ledger(ledger))

    stub = _CountingCitationService()
    app.state.citation_service = stub

    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/citation-audit/deep")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "deferred" in body, "Response must include 'deferred' field"
    assert body["deferred"] == 0, (
        f"Expected deferred==0 when rows within cap, got {body['deferred']}"
    )
    assert body["checked"] == 2
