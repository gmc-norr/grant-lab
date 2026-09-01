"""JSON /api citation-audit endpoints (Cockpit Slice F)."""

from call_manager.db.models import User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.paths import workspace_repo_dir

# The semantic/cap tests (added per the note below) import CitationLLMResult /
# _StubCitationService themselves — keep this top import block F401-clean.

ORPHAN_DRAFT = "We claim X [@ghost]<!-- ev:ev-aaaaaaaaaaaa -->.\n"


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        viewer = User(status="active", email="viewer@ki.se")
        s.add_all([owner, viewer])
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        return owner.id, viewer.id, ws.id


def _write(tmp_path, ws_id, *, proposal=None, citations=None):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    if proposal is not None:
        (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)


def _client(app, user_id, test_client_factory):
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


def test_audit_orphan_marker(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal=ORPHAN_DRAFT, citations=dump_ledger(empty_ledger()))
    r = _client(app, owner_id, test_client_factory).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_present"] is True
    types = {i["type"] for i in body["issues"]}
    assert "orphan_marker" in types
    assert body["error_count"] + body["warning_count"] == len(body["issues"])


def test_audit_no_draft(monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    r = _client(app, owner_id, test_client_factory).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 200
    body = r.json()
    assert body["draft_present"] is False
    assert body["issues"] == []


def test_audit_corrupt_ledger_409(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations="{ not json")
    r = _client(app, owner_id, test_client_factory).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "citation_ledger_invalid"


def test_deep_check_corrupt_ledger_409(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    # The deep handler has its OWN corrupt-ledger branch (citation_audit.py); the spec
    # pins 409 citation_ledger_invalid for GET AND deep-check, so guard it explicitly.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations="{ not json")
    r = _client(app, owner_id, test_client_factory).post(
        f"/api/workspaces/{ws_id}/citation-audit/deep"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "citation_ledger_invalid"


def test_audit_viewer_can_read(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations=dump_ledger(empty_ledger()))
    assert (
        _client(app, viewer_id, test_client_factory)
        .get(f"/api/workspaces/{ws_id}/citation-audit")
        .status_code
        == 200
    )


def test_deep_check_viewer_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations=dump_ledger(empty_ledger()))
    r = _client(app, viewer_id, test_client_factory).post(
        f"/api/workspaces/{ws_id}/citation-audit/deep"
    )
    assert r.status_code == 403


def test_deep_check_no_service_deterministic_only(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    app = _app(monkeypatch, pg_url, tmp_path)
    app.state.citation_service = None  # offline override → deterministic only
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal=ORPHAN_DRAFT, citations=dump_ledger(empty_ledger()))
    r = _client(app, owner_id, test_client_factory).post(
        f"/api/workspaces/{ws_id}/citation-audit/deep"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["semantic_available"] is False
    assert body["cap_exceeded"] is False
    assert body["checked"] == 0
    assert "orphan_marker" in {i["type"] for i in body["issues"]}


def test_deep_check_semantic_flags(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    """A judgeable citation with a contradicts stub produces a semantic issue."""
    from call_manager.db.models import Reference, ReferenceChunk
    from call_manager.proposal_assist.citations import CitationLLMResult
    from call_manager.proposal_assist.citations.audit import chunk_content_sha, claim_context_sha
    from call_manager.proposal_assist.citations.ledger import append_evidence, build_evidence_row
    from call_manager.proposal_assist.citations.markers import build_marker

    EMBED_DIM = 1024
    CHUNK_TEXT = (
        "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
        "Secondary endpoints were not statistically significant."
    )
    EVIDENCE_QUOTE = "reduces recurrence by 30%"
    EV_ID = "ev-aaaaaaaaaaaa"
    CITEKEY = "smith2021"

    class _StubCitationService:
        def __init__(self, result):
            self._result = result
            self.calls = []

        def complete_json(self, *, workspace_id, op, system, user):
            self.calls.append({"workspace_id": workspace_id, "op": op})
            return self._result

    def _verdict_result(
        verdict, rationale="because reasons", *, cap_exceeded=False, cap_scope=None
    ):
        return CitationLLMResult(
            data=None if cap_exceeded else {"verdict": verdict, "rationale": rationale},
            usage=None,
            model="stub-model",
            cap_exceeded=cap_exceeded,
            cap_scope=cap_scope,
        )

    def _seed_reference(workspace_id):
        with pg_session_factory() as s:
            ref = Reference(
                workspace_id=workspace_id,
                kind="paper",
                source_type="uploaded",
                filename="seed.pdf",
                storage_path="seed/seed.pdf",
                mime_type="application/pdf",
                citekey=CITEKEY,
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
                    text=CHUNK_TEXT,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
            s.commit()
        return rid

    def _build_evidence_row(rid):
        claim_text = "placeholder"
        return build_evidence_row(
            ev_id=EV_ID,
            citekey=CITEKEY,
            reference_id=str(rid),
            chunk_index=0,
            section_path="Results",
            source_content_hash=None,
            chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
            evidence_quote=EVIDENCE_QUOTE,
            evidence_quote_offsets=[0, 0],
            stance="supports",
            confidence=None,
            claim_context_text=claim_text,
            claim_context_hash=claim_context_sha(claim_text),
            citation_snapshot="Smith 2021",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    rid = _seed_reference(ws_id)

    proposal_text = (
        f"# Proposal\n\nAdjuvant therapy cuts recurrence{build_marker(CITEKEY, EV_ID)}.\n"
    )
    ledger = append_evidence(empty_ledger(), _build_evidence_row(rid))

    _write(tmp_path, ws_id, proposal=proposal_text, citations=dump_ledger(ledger))

    app.state.citation_service = _StubCitationService(_verdict_result("contradicts"))

    r = _client(app, owner_id, test_client_factory).post(
        f"/api/workspaces/{ws_id}/citation-audit/deep"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["semantic_available"] is True
    assert body["checked"] == 1
    assert any(i["type"].startswith("semantic_") for i in body["issues"])


def test_deep_check_cap_exceeded(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    """Seeding citation_llm_usage rows past the workspace cap makes cap_exceeded=True."""
    from call_manager.proposal_assist.citations.usage import record_usage

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal=ORPHAN_DRAFT, citations=dump_ledger(empty_ledger()))

    # Seed usage rows whose tokens_in sum >= the workspace daily cap.
    cap = app.state.settings.citation_llm_daily_token_cap_input
    with pg_session_factory() as s:
        record_usage(
            s,
            workspace_id=ws_id,
            op="validate_citation",
            status="ok",
            tokens_in=cap,
            tokens_out=0,
            model="stub",
        )
        s.commit()

    r = _client(app, owner_id, test_client_factory).post(
        f"/api/workspaces/{ws_id}/citation-audit/deep"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cap_exceeded"] is True
