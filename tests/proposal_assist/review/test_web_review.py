"""Endpoint tests for the mock-panel review routes (POST/GET /review-runs).

Mirrors the evaluator endpoint test pattern: TestClient with an authed-owner
cookie, a disclosure-acknowledged active workspace, and fixtures from
tests/proposal_assist/conftest.py.
"""

from __future__ import annotations

import subprocess
import uuid

from call_manager.db.models import ReviewRun, WorkspaceEvent
from call_manager.proposal_assist.citations.gitio import blob_sha
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_workspace

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _make_ws(
    _app,
    current_user,
    pg_session_factory,
    tmp_path,
    *,
    phase: str = "active",
    ack: bool = True,
) -> str:
    """Insert a workspace, init a git repo, return the ws_id str."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=ack, current_phase=phase)
        s.flush()
        ws.rubric_criteria = [
            {"name": "Scientific quality", "weight": 0.6},
            {"name": "Impact", "weight": 0.4},
        ]
        s.commit()
        ws_id = ws.id

    # Initialise an empty git repo so content_sha() doesn't crash.
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)

    return str(ws_id)


# ---------------------------------------------------------------------------
# POST /review-runs — trigger + create-or-return-active
# ---------------------------------------------------------------------------


def test_trigger_creates_run_and_event(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "id" in body
    assert body["status"] in ("queued", "running")

    # WorkspaceEvent written.
    wid = uuid.UUID(ws_id)
    with pg_session_factory() as s:
        ev = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=wid, kind="review_run_triggered")
            .first()
        )
        assert ev is not None
        assert ev.actor_id == current_user.id


def test_repeat_trigger_returns_active_not_429(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """With cap=1 and an already-active run, the second POST must return 200
    with the SAME id (active-first bypasses the cap check)."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    _app.state.settings.review_runs_per_day_default = 1

    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] == r2.json()["id"]


def test_429_over_run_cap_when_no_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """If today's completed run already hit the cap and there is NO active run,
    POSTing must return 429 and must not create a new row."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    _app.state.settings.review_runs_per_day_default = 1
    wid = uuid.UUID(ws_id)

    # Seed a completed run so review_runs_today == 1, no active run.
    with pg_session_factory() as s:
        s.add(ReviewRun(workspace_id=wid, status="done", draft_sha="a" * 64))
        s.commit()

    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    assert r.status_code == 429, r.text

    # No new row created.
    with pg_session_factory() as s:
        count = s.query(ReviewRun).filter_by(workspace_id=wid).count()
    assert count == 1  # only the seeded done run


def test_gate_403_without_disclosure(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path, ack=False)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    assert r.status_code == 403, r.text


def test_gate_409_setup_phase(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path, phase="setup")
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/review-runs")
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# GET /review-runs/latest
# ---------------------------------------------------------------------------


def test_latest_serializes_items_budget_staleness(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    url_base = f"/api/workspaces/{ws_id}"

    # Before any run: returns null.
    assert client_authed_owner.get(f"{url_base}/review-runs/latest").json() is None

    # Trigger a run.
    trigger_resp = client_authed_owner.post(f"{url_base}/review-runs")
    assert trigger_resp.status_code == 200, trigger_resp.text
    run_id = trigger_resp.json()["id"]

    # Now latest returns the run with budget + stale keys.
    r = client_authed_owner.get(f"{url_base}/review-runs/latest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == run_id
    assert body["status"] in ("queued", "running")
    assert "budget" in body
    assert "stale" in body
    assert isinstance(body["items"], list)
    # Dollar fields must be strings (or None) on the wire — not JSON numbers.
    # A freshly-triggered run has estimated_dollars=None (not yet finalized).
    assert isinstance(body["estimated_dollars"], (str, type(None))), (
        f"estimated_dollars must be str|None, got {type(body['estimated_dollars'])!r}"
    )
    assert isinstance(body["budget"]["dollars_used"], str), (
        f"budget.dollars_used must be str, got {type(body['budget']['dollars_used'])!r}"
    )
    assert isinstance(body["budget"]["dollars_cap"], str), (
        f"budget.dollars_cap must be str, got {type(body['budget']['dollars_cap'])!r}"
    )
    # runs_used / runs_cap must be real JSON numbers.
    assert isinstance(body["budget"]["runs_used"], int)
    assert isinstance(body["budget"]["runs_cap"], int)


def test_latest_unauth_rejected(
    client_unauth, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/review-runs/latest")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /review-runs/{run_id}/files/{name}
# ---------------------------------------------------------------------------


def test_files_endpoint_by_sha_and_404s(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    wid = uuid.UUID(ws_id)
    url_base = f"/api/workspaces/{ws_id}"

    # Write fit-check.md into the workspace git repo and commit it.
    repo = workspace_repo_dir(str(tmp_path), wid)
    fit_content = "# Fit Check\n\nAll criteria met.\n"
    (repo / "fit-check.md").write_text(fit_content)
    subprocess.run(["git", "add", "fit-check.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add fit-check"],
        cwd=repo,
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        },
    )
    fc_sha = blob_sha(repo, "HEAD", "fit-check.md")

    # Insert a ReviewRun with fit_check_sha set.
    with pg_session_factory() as s:
        run = ReviewRun(
            workspace_id=wid,
            status="done",
            draft_sha="a" * 64,
            fit_check_sha=fc_sha,
        )
        s.add(run)
        s.commit()
        run_id = str(run.id)

    # Happy path: GET /files/fit-check.md returns markdown.
    r = client_authed_owner.get(f"{url_base}/review-runs/{run_id}/files/fit-check.md")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "fit-check.md"
    assert body["sha"] == fc_sha
    assert "Fit Check" in body["markdown"]

    # 404 — unknown file name.
    r404 = client_authed_owner.get(f"{url_base}/review-runs/{run_id}/files/unknown.md")
    assert r404.status_code == 404

    # 404 — wrong run_id.
    bad_run_id = str(uuid.uuid4())
    r_bad = client_authed_owner.get(
        f"{url_base}/review-runs/{bad_run_id}/files/fit-check.md"
    )
    assert r_bad.status_code == 404

    # 404 — rubric-review.md SHA not set on this run.
    r_no_sha = client_authed_owner.get(
        f"{url_base}/review-runs/{run_id}/files/rubric-review.md"
    )
    assert r_no_sha.status_code == 404


# ---------------------------------------------------------------------------
# OpenAPI contract — drift protection
# ---------------------------------------------------------------------------


def _option_types(prop: dict) -> set[str | None]:
    """The JSON-Schema `type`s a property may take (handles 3.1 anyOf-null)."""
    if "anyOf" in prop:
        return {o.get("type") for o in prop["anyOf"]}
    return {prop.get("type")}


def test_review_runs_declare_response_models_with_string_dollars(client_unauth):
    """The /review-runs endpoints MUST carry a response_model so the generated
    TS client is type-checked against the wire contract. The original crash
    (a number-typed frontend DTO calling .toFixed() on a string dollar value)
    was invisible precisely because the endpoints returned a bare ``dict`` and
    emitted no response schema. Pin the schema here so codegen stays honest:
    the three decimal dollar fields are JSON *strings*, never numbers.
    """
    # Read the schema via app.openapi() (mode-independent): the served
    # /openapi.json route is gated off in non-dev modes, but the contract this
    # test pins lives in the generated schema regardless of serving mode.
    schema = client_unauth.app.openapi()
    comps = schema["components"]["schemas"]
    for name in ("ReviewRunOut", "ReviewRunItemOut", "ReviewBudgetOut", "ReviewFileOut"):
        assert name in comps, f"{name} missing from OpenAPI components"

    run_props = comps["ReviewRunOut"]["properties"]
    # Decimal-on-the-wire: nullable string, never number.
    assert _option_types(run_props["estimated_dollars"]) == {"string", "null"}, (
        f"estimated_dollars must be nullable string, got {run_props['estimated_dollars']}"
    )

    budget_props = comps["ReviewBudgetOut"]["properties"]
    assert budget_props["dollars_used"]["type"] == "string"
    assert budget_props["dollars_cap"]["type"] == "string"
    # Counts stay integers (regression guard against over-stringifying).
    assert budget_props["runs_used"]["type"] == "integer"
    assert budget_props["runs_cap"]["type"] == "integer"

    item_props = comps["ReviewRunItemOut"]["properties"]
    assert _option_types(item_props["estimated_dollars"]) == {"string", "null"}
    # Item status enum matches the DB CHECK (skipped, not the run-level partial).
    assert set(item_props["status"]["enum"]) == {
        "queued", "running", "done", "failed", "skipped", "cancelled",
    }
