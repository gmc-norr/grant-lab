"""Cockpit /spec JSON surface (GET/POST/init). Mirrors tests/test_api_workspaces.py:
same _app/_seed/_client helpers and production repo layout."""

from __future__ import annotations

import subprocess

from call_manager.db.models import ArtifactVersion
from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.test_api_workspaces import _app, _client, _seed


def _add_spec(tmp_path, ws_id, content=""):
    """Write + commit proposal-spec.md into the already-initialized repo."""
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / "proposal-spec.md").write_text(content)
    env = _git_env()
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)


def test_get_spec_returns_sections_no_citations(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft preamble\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    assert "citations" not in body  # SpecOut has no citations field
    assert body["can_edit"] is True
    assert [s["index"] for s in body["sections"]] == [0, 1]  # empty preamble + Aims
    assert body["sections"][1]["heading"] == "Aims"


def test_get_spec_member_allowed_stranger_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    assert _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 200
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 403


def test_get_spec_404_when_file_absent(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    # No _add_spec -> proposal-spec.md does not exist.
    assert _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 404


def test_save_spec_section_owner_commits(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nbe truly great.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["issues"] == []  # spec path skips Tier-1
    assert body["job_enqueued"] is False


def test_save_spec_section_structure_guard_is_400(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "be great.\n",  # dropped the ## heading
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 400


def test_save_spec_section_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    spec = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec").json()
    r = _client(app, member_id).post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\ny\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": spec["sections"][1]["sha"],
        },
    )
    assert r.status_code == 403


def test_save_spec_section_stale_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Mirror tests/test_api_workspaces.py::test_save_section_stale_is_409 for the
    # spec route — proves the spec mapping is StaleSectionError -> 409 conflict
    # (a duplicated route could otherwise carry a spec-specific mapping typo).
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    # Concurrent commit: move HEAD AND change the section bytes under the captured base.
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / "proposal-spec.md").write_text("## Aims\nSOMEONE ELSE EDITED.\n")
    subprocess.run(["git", "commit", "-aqm", "concurrent"], cwd=repo, check=True, env=_git_env())
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nmy late edit.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_save_spec_section_bad_index_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/99",
        json={
            "content": "## X\nbody.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": "0" * 64,
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_save_spec_section_busy_is_429(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # CommitMutexTimeout -> 429 + Retry-After + busy message (NOT a sanitized 500).
    # Mirror tests/test_api_workspaces.py::test_save_section_busy_is_retriable_429.
    from call_manager.proposal_assist.drafting.locking import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()

    def _boom(*args, **kwargs):
        raise CommitMutexTimeout("busy")

    # Both routes call the same imported save_section; patch the symbol, hit /spec.
    monkeypatch.setattr("call_manager.web.api.workspaces.save_section", _boom)
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nrewritten.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": spec["sections"][1]["sha"],
        },
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_requests"
    assert r.json()["error"]["message"] == BUSY_ERROR_MESSAGE
    assert r.headers.get("Retry-After") == "2"


def test_init_spec_seeds_six_sections_when_empty(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")  # whitespace-only spec
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    # 6 sections: empty index-0 preamble + the 5 H2 sections.
    assert [s["index"] for s in body["sections"]] == [0, 1, 2, 3, 4, 5]
    assert [s["heading"] for s in body["sections"]] == [
        "",
        "Aims",
        "Approach",
        "Novelty & Significance",
        "Expected Impact",
        "Key Claims",
    ]


def test_init_spec_is_idempotent_noop_when_nonempty(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nmy own words.\n")
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    # Did not clobber the user's content.
    assert any("my own words." in s["text"] for s in r.json()["sections"])
    # Calling twice on an already-seeded spec adds no new init commit.
    c.post(f"/api/workspaces/{ws_id}/spec")
    with pg_session_factory() as s:
        n = (
            s.query(ArtifactVersion)
            .filter(
                ArtifactVersion.workspace_id == ws_id,
                ArtifactVersion.message == "user spec init",
            )
            .count()
        )
    assert n == 0  # non-empty -> never an init commit


def test_init_spec_seeds_exactly_one_commit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec")
    c.post(f"/api/workspaces/{ws_id}/spec")  # second sees content -> no-op
    with pg_session_factory() as s:
        n = (
            s.query(ArtifactVersion)
            .filter(
                ArtifactVersion.workspace_id == ws_id,
                ArtifactVersion.message == "user spec init",
            )
            .count()
        )
    assert n == 1


def test_init_spec_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")
    assert _client(app, member_id).post(f"/api/workspaces/{ws_id}/spec").status_code == 403


def test_get_spec_exposes_lock_fields(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    before = c.get(f"/api/workspaces/{ws_id}/spec").json()
    assert before["locked"] is False
    assert before["can_edit"] is True and before["can_manage_lock"] is True
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    after = c.get(f"/api/workspaces/{ws_id}/spec").json()
    assert after["locked"] is True
    assert after["can_edit"] is False  # locked -> read-only even for the owner
    assert after["can_manage_lock"] is True  # owner can still unlock
    assert after["locked_by"] == "owner@ki.se"


def test_get_spec_member_cannot_manage_lock(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    _client(app, owner_id).post(f"/api/workspaces/{ws_id}/spec/lock")
    body = _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec").json()
    assert body["can_manage_lock"] is False
    assert body["can_edit"] is False and body["locked"] is True
