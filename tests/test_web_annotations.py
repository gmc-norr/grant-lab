"""Annotation-strip web routes (HTMX fragments) — Task 11.

Fixture pattern mirrors tests/test_web_sections.py (create_app + signed
session cookie, three role clients sharing ONE app instance, production
<workspaces_dir>/<id>/repo layout via the test_analysis_pipeline helpers).
"""

import pytest
from fastapi.testclient import TestClient

from call_manager.db.models import (
    ClaimAnalysisJob,
    User,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from call_manager.main import create_app
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.test_analysis_pipeline import _init_repo, _seed_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture
def app(_settings_env, pg_session_factory, tmp_path):
    a = create_app()
    a.state.session_factory = pg_session_factory
    a.state.settings.workspaces_dir = str(tmp_path)
    return a


@pytest.fixture
def users(app, pg_session_factory):
    with pg_session_factory() as s:
        rows = {
            role: User(status="active", email=f"ann-{role}@ki.se")
            for role in ("owner", "viewer", "outsider")
        }
        s.add_all(rows.values())
        s.commit()
        return {role: u.id for role, u in rows.items()}


def _client_for(app, user_id) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return client


@pytest.fixture
def client_owner(app, users):
    return _client_for(app, users["owner"])


@pytest.fixture
def client_viewer(app, users):
    return _client_for(app, users["viewer"])


@pytest.fixture
def client_outsider(app, users):
    return _client_for(app, users["outsider"])


@pytest.fixture
def ws_setup(app, users, pg_session_factory, tmp_path):
    """Workspace + repo + reference/chunk + one open uncited_supported and one
    open uncited_no_match annotation on section 1. Returns ids + section sha."""
    with pg_session_factory() as s:
        ws = Workspace(owner_id=users["owner"], title="annotations-ws")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=users["viewer"]))
        ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
        repo = _init_repo(tmp_path, ws.id, DOC)
        sec = split_sections(DOC)[1]
        supported = UserClaimAnnotation(
            workspace_id=ws.id,
            section_sha=sec.sha,
            section_occurrence=0,
            section_heading=sec.heading,
            sentence_ordinal=0,
            char_offset=sec.text.index(CLAIM),
            claim_sha="0" * 64,
            claim_text=CLAIM,
            kind="uncited_supported",
            evidence_id="",
            reference_id=ref_id,
            chunk_index=0,
            evidence_quote=QUOTE,
            status="open",
        )
        no_match = UserClaimAnnotation(
            workspace_id=ws.id,
            section_sha=sec.sha,
            section_occurrence=0,
            section_heading=sec.heading,
            sentence_ordinal=1,
            char_offset=sec.text.index(CLAIM),
            claim_sha="1" * 64,
            claim_text="Another unsupported claim",
            kind="uncited_no_match",
            evidence_id="",
            status="open",
        )
        s.add_all([supported, no_match])
        s.commit()
        return {
            "ws_id": ws.id,
            "sha": sec.sha,
            "supported_id": supported.id,
            "no_match_id": no_match.id,
            "repo": repo,
        }


def _strip_url(ws_setup):
    return f"/workspaces/{ws_setup['ws_id']}/sections/1/annotations?sha={ws_setup['sha']}"


def test_strip_renders_insert_button_for_owner(client_owner, ws_setup):
    r = client_owner.get(_strip_url(ws_setup))
    assert r.status_code == 200
    assert CLAIM in r.text
    assert "Insert citation" in r.text
    assert "Supported by" in r.text
    # The no_match annotation renders its dismiss form for the owner.
    assert "Dismiss" in r.text


def test_strip_hides_actions_for_viewer(client_viewer, ws_setup):
    r = client_viewer.get(_strip_url(ws_setup))
    assert r.status_code == 200
    assert CLAIM in r.text
    assert "Insert citation" not in r.text
    assert "Dismiss" not in r.text


def test_outsider_strip_403(client_outsider, ws_setup):
    r = client_outsider.get(_strip_url(ws_setup))
    assert r.status_code == 403


def test_insert_post_writes_marker(client_owner, ws_setup, pg_session_factory):
    ws_id, ann_id = ws_setup["ws_id"], ws_setup["supported_id"]
    r = client_owner.post(f"/workspaces/{ws_id}/annotations/{ann_id}/insert?n=1")
    assert r.status_code == 200
    doc = (ws_setup["repo"] / "proposal.md").read_text()
    assert "<!-- ev:ev-" in doc
    assert f"{CLAIM}.[@" in doc
    with pg_session_factory() as s:
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "inserted"
        # Fix 5: a ClaimAnalysisJob must be enqueued for the section that now
        # contains the new marker. The section sha changes after insert (the
        # marker text is new), so just assert a queued job exists for the ws.
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued").first()
        assert job is not None, "expected a queued ClaimAnalysisJob after insert"


def test_viewer_insert_post_403(client_viewer, ws_setup):
    ws_id, ann_id = ws_setup["ws_id"], ws_setup["supported_id"]
    r = client_viewer.post(f"/workspaces/{ws_id}/annotations/{ann_id}/insert?n=1")
    assert r.status_code == 403


def test_dismiss_requires_reason(client_owner, ws_setup, pg_session_factory):
    ws_id, ann_id = ws_setup["ws_id"], ws_setup["no_match_id"]
    # Missing form field → FastAPI validation 422.
    r = client_owner.post(f"/workspaces/{ws_id}/annotations/{ann_id}/dismiss?n=1")
    assert r.status_code == 422
    # Whitespace-only reason → our manual 422.
    r = client_owner.post(
        f"/workspaces/{ws_id}/annotations/{ann_id}/dismiss?n=1",
        data={"reason": "   "},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "a reason is required"
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"


def test_dismiss_with_reason(client_owner, ws_setup, pg_session_factory, users):
    ws_id, ann_id = ws_setup["ws_id"], ws_setup["no_match_id"]
    r = client_owner.post(
        f"/workspaces/{ws_id}/annotations/{ann_id}/dismiss?n=1",
        data={"reason": "cited in the cover letter"},
    )
    assert r.status_code == 200
    with pg_session_factory() as s:
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "dismissed"
        assert ann.dismissed_reason == "cited in the cover letter"
        assert ann.resolved_by_user_id == users["owner"]
        ev = (
            s.query(WorkspaceEvent).filter_by(workspace_id=ws_id, kind="user_claim_dismissed").one()
        )
        assert ev.payload == {"annotation_id": ann_id, "reason": "cited in the cover letter"}
    # Re-dismissing a non-open annotation → 409.
    r = client_owner.post(
        f"/workspaces/{ws_id}/annotations/{ann_id}/dismiss?n=1",
        data={"reason": "again"},
    )
    assert r.status_code == 409


def test_reanalyze_enqueues_and_shows_pending(client_owner, ws_setup, pg_session_factory):
    import re

    ws_id = ws_setup["ws_id"]
    r = client_owner.post(f"/workspaces/{ws_id}/sections/1/reanalyze")
    assert r.status_code == 200
    assert "Checking claims" in r.text
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued"
        assert job.section_sha == ws_setup["sha"]
        # commit_sha must be a real 40-char hex SHA, NOT the proposal text.
        assert re.fullmatch(r"[0-9a-f]{40}", job.commit_sha), job.commit_sha


def test_strip_shows_failed_job_with_retry(
    client_owner, client_viewer, ws_setup, pg_session_factory
):
    ws_id = ws_setup["ws_id"]
    with pg_session_factory() as s:
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws_id,
                section_index=1,
                section_heading="Body",
                section_sha=ws_setup["sha"],
                commit_sha="0" * 40,
                status="failed",
                last_error="boom",
            )
        )
        s.commit()
    r = client_owner.get(_strip_url(ws_setup))
    assert r.status_code == 200
    assert "Analysis failed" in r.text
    assert "boom" in r.text
    assert "Retry" in r.text
    # Viewers see the failure but not the Retry button.
    r = client_viewer.get(_strip_url(ws_setup))
    assert r.status_code == 200
    assert "Analysis failed" in r.text
    assert "Retry" not in r.text


def test_strip_out_of_range_n_returns_404(client_owner, ws_setup):
    """Fix 4: GET strip with n out of bounds returns 404 (mirrors _section_or_404)."""
    ws_id, sha = ws_setup["ws_id"], ws_setup["sha"]
    r = client_owner.get(f"/workspaces/{ws_id}/sections/99/annotations?sha={sha}")
    assert r.status_code == 404


def test_dismiss_races_insert_guarded(client_owner, ws_setup, pg_session_factory):
    """Fix 2: guarded dismiss rejects a concurrent insert.

    Deterministic simulation: flip the annotation's status to 'inserted'
    directly in the DB (as a concurrent insert would), then POST dismiss.
    The guarded UPDATE sees status != 'open' and must return 409.
    """
    ws_id, ann_id = ws_setup["ws_id"], ws_setup["supported_id"]
    # Simulate a concurrent insert that committed before dismiss.
    with pg_session_factory() as s:
        s.query(UserClaimAnnotation).filter_by(id=ann_id).update(
            {"status": "inserted"}, synchronize_session=False
        )
        s.commit()
    r = client_owner.post(
        f"/workspaces/{ws_id}/annotations/{ann_id}/dismiss?n=1",
        data={"reason": "not needed"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "annotation is no longer actionable"
