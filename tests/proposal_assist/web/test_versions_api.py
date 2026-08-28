"""Version-history endpoints: list from git, read one blob.

Modelled on tests/proposal_assist/review/test_web_review.py — TestClient with an
authed-owner cookie and a workspace whose repo lives under tmp_path.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from call_manager.db.models import ArtifactVersion
from tests.proposal_assist.conftest import make_user, make_workspace


def _make_ws(_app, current_user, pg_session_factory, tmp_path) -> str:
    """Workspace + an EMPTY git repo (git init, no commits)."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="active")
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return str(ws_id)


def _commit(repo: Path, name: str, body: str, msg: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


# --- the list -------------------------------------------------------------


def test_list_is_newest_first_and_scoped_to_the_artifact(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _commit(repo, "proposal.md", "# draft 1\n", "draft 1")
    _commit(repo, "proposal-spec.md", "# spec 0\n", "spec 0")

    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_available"] is True
    assert body["truncated"] is False
    assert [v["subject"] for v in body["versions"]] == ["draft 1", "draft 0"]

    r2 = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=spec")
    assert [v["subject"] for v in r2.json()["versions"]] == ["spec 0"]


def test_readable_history_with_no_matching_commits_is_NOT_unavailable(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """The other side of `history_available`, and the one an "empty means broken"
    regression would silently eat.

    A repo with real commits, none of which touched the requested artifact, is a
    perfectly readable history that happens to be empty for this tab. It must
    answer `history_available: true` with no versions — distinct from the unborn
    HEAD below, which is `false`. Every other success-path test here has at least
    one matching commit, so none of them can fail on
    `if not versions: history_available = False`.
    """
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal-spec.md", "# spec only\n", "spec only")

    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json() == {"versions": [], "truncated": False, "history_available": True}


def test_unborn_head_is_a_state_not_a_500(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """THE gate for the commonest new-workspace state. `git log` on a repo with
    no commits exits 128, which list_commits converts to GitCommitError. An
    endpoint that assumes an empty list turns this into the app-wide 500 from
    main.py:713-716."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json() == {"versions": [], "truncated": False, "history_available": False}


def test_missing_repo_directory_is_also_a_state(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    import shutil

    shutil.rmtree(tmp_path / ws_id / "repo")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json()["history_available"] is False


def test_a_commit_with_no_artifact_versions_row_still_lists(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """The whole reason the list is sourced from git (spec §12). Four routine
    commit paths write no artifact_versions row; those versions must still be
    reachable, with the provenance fields absent rather than erroring."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha_with = _commit(repo, "proposal.md", "# one\n", "one")
    _commit(repo, "proposal.md", "# two\n", "two")

    with pg_session_factory() as s:
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha_with,
                created_by="user",
                message="one",
            )
        )
        s.commit()

    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft").json()
    rows = {v["subject"]: v for v in body["versions"]}
    assert rows["two"]["created_by"] is None
    assert rows["two"]["triggering_turn_id"] is None
    assert rows["one"]["created_by"] == "user"


def test_a_version_carries_the_prompt_that_caused_it(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Goal 1 is "who made each one AND WHY". `triggering_turn_id` points at the
    USER turn (turns/processor.py:359), and that turn's `content` is JSONB, so
    the prompt has to be extracted, not read off a column."""
    from call_manager.db.models import ChatTurn

    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")

    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=uuid.UUID(ws_id),
            role="user",
            content={"type": "text", "text": "Tighten the aims section"},
        )
        s.add(turn)
        s.flush()
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="agent",
                message="one",
                triggering_turn_id=turn.id,
            )
        )
        s.commit()

    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] == "Tighten the aims section"


def test_a_block_list_turn_still_yields_its_text(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """A submit carrying an attachment, a reference or a selection emits a LIST
    of blocks whose FIRST block is the text (turns/submit.py:109). A reader that
    handles only the dict shape silently loses the reason on exactly the turns
    that had the most context."""
    from call_manager.db.models import ChatTurn

    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")

    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=uuid.UUID(ws_id),
            role="user",
            content=[
                {"type": "text", "text": "Use this figure"},
                {"type": "image", "attachment_id": str(uuid.uuid4())},
            ],
        )
        s.add(turn)
        s.flush()
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="agent",
                message="one",
                triggering_turn_id=turn.id,
            )
        )
        s.commit()

    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] == "Use this figure"


def test_a_version_with_no_turn_has_no_prompt(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Absence is NORMAL — every `reconciled` row and every commit with no DB row
    at all. It must render as ordinary, never as an error."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")
    with pg_session_factory() as s:
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="reconciled",
                message="one",
            )
        )
        s.commit()
    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] is None
    assert row["created_by"] == "reconciled"


def test_unknown_artifact_is_422_not_500(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history?artifact=bogus")
    assert r.status_code == 422, r.text


# --- reading one version --------------------------------------------------


def test_read_returns_the_markdown_at_that_commit(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    old = _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _commit(repo, "proposal.md", "# draft 1\n", "draft 1")

    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history/{old}?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json() == {"commit_sha": old, "artifact": "draft", "markdown": "# draft 0\n"}


def test_read_rejects_an_option_shaped_sha_before_git(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """422 at the boundary, NOT the app-wide 500. blob_sha fails open — it echoes
    an unresolvable ref back as if it were a sha — so this is the only place a
    bad value gets a clean answer."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft\n", "draft")
    r = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/history/--output=proposal.md?artifact=draft"
    )
    assert r.status_code == 422, r.text


def test_read_of_an_unknown_sha_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft\n", "draft")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history/{'a' * 40}?artifact=draft")
    assert r.status_code == 404, r.text


def test_read_of_a_commit_that_never_touched_the_artifact_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    only_spec = _commit(repo, "proposal-spec.md", "# spec\n", "spec")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/history/{only_spec}?artifact=draft")
    assert r.status_code == 404, r.text


# --- membership -----------------------------------------------------------


def test_non_member_gets_403_not_a_document(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """`client_authed_other` does not exist in this repo's fixture set. The
    established pattern for a non-member 403 (tests/proposal_assist/
    test_web_proposed_changes.py:291-304) is the inversion: keep
    `client_authed_owner`, but own the workspace by a stranger instead."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-v@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.commit()
        ws_id = str(ws.id)
    repo = tmp_path / ws_id / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    sha = _commit(repo, "proposal.md", "# draft\n", "draft")

    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/history").status_code == 403
    assert (
        client_authed_owner.get(f"/api/workspaces/{ws_id}/history/{sha}").status_code == 403
    )
