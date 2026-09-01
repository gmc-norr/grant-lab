"""The citation LLM is the app's SIXTH cloud-egress path, and it was ungated.

`CITATION_LLM_PROVIDER=anthropic` sends proposal text AND uploaded reference text
to an external API. Measured on this codebase before these gates existed: the
owner-only deep check and a MEMBER's reference upload both egressed with the
disclosure notice unacknowledged and `DRAFTING_ENABLED=false`, each returning
200 with the failure swallowed.

Two layers, because one cannot cover the other's ground:
  * an operator opt-in (`CITATION_LLM_ALLOW_CLOUD`) — the only thing that can
    cover the UNATTENDED scheduler path, which has no request to gate;
  * per-user disclosure consent on the request paths — the same promise the
    drafting subsystem already makes, which a global flag cannot make per user.
"""

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.citations.llm import CitationLLMConfigError, build_citation_llm


def _settings(**over):
    base = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "CITATION_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key",
    }
    base.update(over)
    return Settings(**base, _env_file=None)


def test_cloud_citation_provider_refuses_to_build_without_the_operator_opt_in():
    # The unattended claim-analysis ticks call this service with no request and
    # no user, so no per-request gate can reach them. This is what covers them:
    # without the explicit flag the app cannot boot with a cloud citation LLM at
    # all (build_citation_service runs inside create_app and does not catch).
    with pytest.raises(CitationLLMConfigError) as ei:
        build_citation_llm(_settings())
    assert "CITATION_LLM_ALLOW_CLOUD" in str(ei.value)


def test_the_opt_in_is_what_permits_it():
    llm = build_citation_llm(_settings(CITATION_LLM_ALLOW_CLOUD="true"))
    assert type(llm._client).__name__ == "AnthropicClient"


def test_the_opt_in_does_not_bypass_the_missing_key_check():
    # Order matters: an opt-in with no key must still be a config error, not a
    # client that fails at first use.
    with pytest.raises(CitationLLMConfigError) as ei:
        build_citation_llm(_settings(CITATION_LLM_ALLOW_CLOUD="true", ANTHROPIC_API_KEY=""))
    assert "ANTHROPIC_API_KEY" in str(ei.value)


def test_the_default_on_prem_provider_is_untouched_by_the_flag():
    # The flag must not become a switch the local path also has to satisfy.
    llm = build_citation_llm(_settings(CITATION_LLM_PROVIDER="ollama"))
    assert type(llm._client).__name__ == "OllamaClient"


# --- Layer 2: per-user consent, at the production entry points ---------------
#
# Layer 1 covers the machine; it cannot tell one user from another. These prove
# the request paths. Every case runs with the operator opt-in ON — otherwise the
# app would not boot and the test would prove nothing about the routes.

from fastapi.testclient import TestClient  # noqa: E402

from call_manager.db.models import (  # noqa: E402
    Reference,
    ReferenceChunk,
    User,
    Workspace,
    WorkspaceEvent,
)
from call_manager.main import create_app  # noqa: E402
from call_manager.proposal_assist.citations.audit import (  # noqa: E402
    chunk_content_sha,
    claim_context_sha,
)
from call_manager.proposal_assist.citations.ledger import (  # noqa: E402
    append_evidence,
    build_evidence_row,
    dump_ledger,
    empty_ledger,
)
from call_manager.proposal_assist.citations.markers import build_marker  # noqa: E402
from call_manager.proposal_assist.paths import workspace_repo_dir  # noqa: E402

CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."
EV_ID = "ev-aaaaaaaaaaaa"
CITEKEY = "smith2021"
_CREATED_APPS = []


@pytest.fixture(autouse=True)
def _dispose_created_app_engines():
    yield
    while _CREATED_APPS:
        _CREATED_APPS.pop().state.engine.dispose()


def _cloud_app(monkeypatch, pg_url, tmp_path, *, drafting="true"):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "CITATION_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key",
        "CITATION_LLM_ALLOW_CLOUD": "true",
        "DRAFTING_ENABLED": drafting,
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    _CREATED_APPS.append(app)
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _record_egress(monkeypatch):
    """Record every attempt to reach the external API, at the HTTP boundary."""
    sent = []

    def fake_chat(self, messages, **kw):
        sent.append(messages)
        raise RuntimeError("egress attempted")

    from call_manager.ranking.anthropic_client import AnthropicClient

    monkeypatch.setattr(AnthropicClient, "chat", fake_chat)
    return sent


def _seed_deep_check(pg_session_factory, tmp_path, *, acknowledged: bool):
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        owner_id, ws_id = owner.id, ws.id
        if acknowledged:
            s.add(
                WorkspaceEvent(
                    workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=owner_id
                )
            )
        ref = Reference(
            workspace_id=ws_id,
            kind="paper",
            source_type="uploaded",
            filename="s.pdf",
            storage_path="s/s.pdf",
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
                workspace_id=ws_id,
                chunk_index=0,
                section_path="Results",
                text=CHUNK_TEXT,
                embedding=[0.0] * 1024,
                embedding_model="fake",
            )
        )
        s.commit()

    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(
        f"# P\n\nAdjuvant therapy cuts recurrence{build_marker(CITEKEY, EV_ID)}.\n"
    )
    row = build_evidence_row(
        ev_id=EV_ID,
        citekey=CITEKEY,
        reference_id=str(rid),
        chunk_index=0,
        section_path="Results",
        source_content_hash=None,
        chunk_content_hash=chunk_content_sha(CHUNK_TEXT),
        evidence_quote="reduces recurrence by 30%",
        evidence_quote_offsets=[0, 0],
        stance="supports",
        confidence=None,
        claim_context_text="placeholder",
        claim_context_hash=claim_context_sha("placeholder"),
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    (repo / "citations.json").write_text(dump_ledger(append_evidence(empty_ledger(), row)))
    return owner_id, ws_id


def _deep_check(app, user_id, ws_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c.post(f"/api/workspaces/{ws_id}/citation-audit/deep")


def test_deep_check_without_the_ack_sends_nothing_and_says_why(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # BEFORE this gate: 200, with the proposal text AND the reference chunk in
    # the request body, the notice unacknowledged. Measured, not assumed.
    app = _cloud_app(monkeypatch, pg_url, tmp_path)
    sent = _record_egress(monkeypatch)
    owner_id, ws_id = _seed_deep_check(pg_session_factory, tmp_path, acknowledged=False)
    r = _deep_check(app, owner_id, ws_id)
    assert sent == [], "proposal/reference text left the building without consent"
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "disclosure_not_acknowledged"


def test_deep_check_with_the_ack_is_allowed_through(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # The other side of the gate: consent given, the call is made. Without this
    # the gate could be a blanket refusal and still pass the test above.
    app = _cloud_app(monkeypatch, pg_url, tmp_path)
    sent = _record_egress(monkeypatch)
    owner_id, ws_id = _seed_deep_check(pg_session_factory, tmp_path, acknowledged=True)
    r = _deep_check(app, owner_id, ws_id)
    assert len(sent) == 1
    assert r.status_code == 200, r.text


def test_the_admin_kill_switch_stops_the_deep_check_even_with_the_ack(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # DRAFTING_ENABLED=false is an administrator saying "stop calling the hosted
    # model". The citation surface calls one, so it must obey.
    app = _cloud_app(monkeypatch, pg_url, tmp_path, drafting="false")
    sent = _record_egress(monkeypatch)
    owner_id, ws_id = _seed_deep_check(pg_session_factory, tmp_path, acknowledged=True)
    r = _deep_check(app, owner_id, ws_id)
    assert sent == []
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "cloud_llm_disabled"


# --- The member-level path: an upload is not an AI action --------------------


def _real_pdf_bytes() -> bytes:
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "SECRET UNPUBLISHED HYPOTHESIS about adjuvant therapy.")
    c.save()
    return buf.getvalue()


def _upload_as_member(monkeypatch, pg_url, tmp_path, pg_session_factory, *, acknowledged):
    from tests.proposal_assist._refs_helpers import (
        FakeEmbedClient,
        add_member,
        authed_client,
        build_app,
    )
    from tests.proposal_assist.conftest import make_user, make_workspace

    for k, v in {
        "CITATION_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key",
        "CITATION_LLM_ALLOW_CLOUD": "true",
    }.items():
        monkeypatch.setenv(k, v)
    sent = _record_egress(monkeypatch)
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()

    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        ws_id, member_id = ws.id, member.id
        if acknowledged:
            s.add(
                WorkspaceEvent(
                    workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=member_id
                )
            )
            s.commit()

    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
        data={"kind": "paper"},
    )
    return r, sent


def test_a_members_upload_without_the_ack_sends_nothing_but_still_succeeds(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    # BEFORE this gate: a MEMBER — not the owner — uploaded a PDF and its text
    # went to the external API, notice unacknowledged, status 200, the failure
    # swallowed. Measured.
    #
    # The upload must still SUCCEED: it is not an AI action, and metadata
    # extraction already degrades to a heuristic when there is no service.
    # Refusing the upload would break a non-AI feature over an AI consent.
    r, sent = _upload_as_member(
        monkeypatch, pg_url, tmp_path, pg_session_factory, acknowledged=False
    )
    assert sent == [], "a member's PDF text left the building without consent"
    assert r.status_code == 200, r.text


def test_a_members_upload_does_not_ride_on_the_OWNERS_ack(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    # Consent is per ACTOR, not per workspace. The upload route is
    # `require_member`, so without the actor predicate in `has_disclosure_ack`
    # a member's PDF would go to the external API on the owner's consent
    # record. Measured: deleting that predicate left the whole suite green.
    from tests.proposal_assist._refs_helpers import (
        FakeEmbedClient,
        add_member,
        authed_client,
        build_app,
    )
    from tests.proposal_assist.conftest import make_user, make_workspace

    for k, v in {
        "CITATION_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key",
        "CITATION_LLM_ALLOW_CLOUD": "true",
    }.items():
        monkeypatch.setenv(k, v)
    sent = _record_egress(monkeypatch)
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()

    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        ws_id, member_id, owner_id = ws.id, member.id, owner.id
        # ONLY the owner acknowledges.
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=owner_id))
        s.commit()

    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
        data={"kind": "paper"},
    )
    assert sent == [], "a member's PDF rode on the owner's consent"
    assert r.status_code == 200, r.text


def test_a_members_upload_with_the_ack_still_gets_llm_metadata(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    # The degrade must be conditional, not a permanent removal of the feature.
    r, sent = _upload_as_member(
        monkeypatch, pg_url, tmp_path, pg_session_factory, acknowledged=True
    )
    assert len(sent) == 1
    assert r.status_code == 200, r.text
