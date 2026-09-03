"""The two remaining ungated draft readers must judge the live document.

Both compute the SAME deterministic broken-citation fact over the SAME draft, on
two different screens. Converting one and not the other would leave the Problems
panel and the Citation Audit page contradicting each other about one workspace at
one moment, so they are pinned together here.

The Problems panel case is the sharper one.

`24396bbb` moved this function's three staleness comparisons onto the room-canonical
basis (they had to: the passes they compare against now evaluate the live document)
but left the draft READ on git. So one function came to judge two different documents
at once — it reports broken citations and over-length sections from stale git HEAD
while its `scorecard_stale` flags describe the live room.

Both directions are wrong and both are visible:

* FALSE POSITIVE — a citation the researcher already fixed is still reported broken,
  because the deleted marker is still in git.
* FALSE NEGATIVE — a section they just made over-length is not reported at all,
  because git has not seen it.
"""

from __future__ import annotations

import hashlib

import pytest

from call_manager.db.models import EvaluatorJob, UncitedClaimsJob, Workspace
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import current_artifact_text_and_sha
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.problems.aggregate import assemble_problems
from tests.proposal_assist.collab.test_collab_kernel_accept import _init_repo
from tests.proposal_assist.conftest import make_user, make_workspace

_CREATED_APPS = []


@pytest.fixture(autouse=True)
def _dispose_created_app_engines():
    yield
    while _CREATED_APPS:
        _CREATED_APPS.pop().state.engine.dispose()


# A marker whose ledger row does not exist -> one deterministic broken-citation issue.
GHOST = "[@ghost]<!-- ev:ev-abcdef123456 -->"
STALE_DISK = f"## Aims\n\nA claim {GHOST} whose ledger row was never written.\n"
# The researcher deleted that marker and wrote a long section. No marker, over budget.
LIVE_ROOM = "## Aims\n\n" + ("The rewritten claim the researcher can actually see. " * 8) + "\n"

BUDGET = 200


def _setup(pg_session_factory, tmp_path, email, *, collab=True):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=email))
        s.commit()
        wid = ws.id
    _init_repo(workspace_repo_dir(tmp_path, wid), STALE_DISK)
    if collab:
        seed = pm_json_to_ydoc(
            markdown_to_pm(pm_to_markdown(markdown_to_pm(LIVE_ROOM)))
        ).get_update()
        with pg_session_factory() as s:
            store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
            state.enable_collab(s, workspace_id=wid, artifact="draft")
            s.commit()
    return wid


def _categories(pg_session_factory, wid, tmp_path):
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(tmp_path, wid),
            ws_rubric_updated_at=None,
            budget=BUDGET,
        )
    return [r["category"] for r in out["problems"]]


def test_the_panel_judges_the_live_document_not_stale_git(pg_session_factory, tmp_path):
    wid = _setup(pg_session_factory, tmp_path, "problems-canon@umu.se")
    cats = _categories(pg_session_factory, wid, tmp_path)

    assert "broken_citation" not in cats, (
        "the panel reported a broken citation the researcher already fixed — the "
        "deleted marker only exists in stale git HEAD"
    )
    assert "over_length" in cats, (
        "the panel missed an over-length section the researcher can see — git HEAD "
        "has not caught up with the live document"
    )


def test_flag_off_the_panel_still_judges_raw_git(pg_session_factory, tmp_path):
    """Control. With collab disabled git IS the document, so the verdicts invert.
    A fix that ignored the flag would change every non-collab workspace, and a test
    that cannot tell the two apart proves nothing."""
    wid = _setup(pg_session_factory, tmp_path, "problems-canon-off@umu.se", collab=False)
    cats = _categories(pg_session_factory, wid, tmp_path)
    assert "broken_citation" in cats
    assert "over_length" not in cats


# --------------------------------------------------------------------------- #
# The Citation Audit endpoint — the same deterministic check, its own screen.
# --------------------------------------------------------------------------- #


def _audit_types(monkeypatch, pg_url, tmp_path, pg_session_factory, *, collab):
    from fastapi.testclient import TestClient

    from call_manager.main import create_app

    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    _CREATED_APPS.append(app)
    app.state.settings.workspaces_dir = str(tmp_path)

    with pg_session_factory() as s:
        owner = make_user(s, email=f"audit-canon-{'on' if collab else 'off'}@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, oid = ws.id, owner.id
    _init_repo(workspace_repo_dir(tmp_path, wid), STALE_DISK)
    if collab:
        seed = pm_json_to_ydoc(
            markdown_to_pm(pm_to_markdown(markdown_to_pm(LIVE_ROOM)))
        ).get_update()
        with pg_session_factory() as s:
            store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
            state.enable_collab(s, workspace_id=wid, artifact="draft")
            s.commit()

    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=oid))
    r = c.get(f"/api/workspaces/{wid}/citation-audit")
    assert r.status_code == 200, r.text
    return {i["type"] for i in r.json()["issues"]}


def test_the_citation_audit_endpoint_audits_the_live_document(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    types = _audit_types(monkeypatch, pg_url, tmp_path, pg_session_factory, collab=True)
    assert "orphan_marker" not in types, (
        "the audit page reported an orphan marker the researcher already deleted — it "
        "only exists in stale git HEAD"
    )


def test_flag_off_the_citation_audit_endpoint_still_audits_raw_git(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """Control, as above."""
    types = _audit_types(monkeypatch, pg_url, tmp_path, pg_session_factory, collab=False)
    assert "orphan_marker" in types


# --------------------------------------------------------------------------- #
# The panel's two staleness flags. Every existing test seeds evaluated_draft_sha=""
# (or "stale-sha") and asserts the flag IS set — which is true on ANY basis and stays
# true if the flag is hardwired on. Setting draft_sha to a constant reddened nothing
# in the whole suite. The matching case is what distinguishes them.
# --------------------------------------------------------------------------- #


def _stale_flags(pg_session_factory, wid, tmp_path, ws_rubric_updated_at):
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(tmp_path, wid),
            ws_rubric_updated_at=ws_rubric_updated_at,
            budget=BUDGET,
        )
    return out["scorecard_stale"], out["uncited_claims_stale"]


def test_a_pass_that_evaluated_the_live_draft_is_not_reported_stale(pg_session_factory, tmp_path):
    """Both flags, both directions, against the LIVE sha.

    Kills two mutations at once: hardwiring the flags on (the live-sha job would be
    reported stale) and comparing against git (the live sha never equals the disk sha,
    so a correct pass would be reported stale forever — an unattended re-run loop).
    """
    wid = _setup(pg_session_factory, tmp_path, "stale-flags@umu.se")
    repo = workspace_repo_dir(tmp_path, wid)
    with pg_session_factory() as s:
        ws = s.get(Workspace, wid)
        rubric_ts = ws.rubric_updated_at
        _, live_sha = current_artifact_text_and_sha(
            s, workspace_id=wid, artifact="draft", repo_dir=repo
        )
        _, live_spec_sha = current_artifact_text_and_sha(
            s, workspace_id=wid, artifact="spec", repo_dir=repo
        )
        disk_sha = hashlib.sha256(STALE_DISK.encode()).hexdigest()
        assert live_sha and live_sha != disk_sha  # guard the fixture
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={"criteria": [], "overall_band": "ok", "summary": ""},
                evaluated_draft_sha=live_sha,
                evaluated_spec_sha=live_spec_sha,
                evaluated_rubric_updated_at=rubric_ts,
            )
        )
        s.add(
            UncitedClaimsJob(
                workspace_id=wid, status="completed", evaluated_draft_sha=live_sha, claims=[]
            )
        )
        s.commit()

    scorecard_stale, uncited_stale = _stale_flags(pg_session_factory, wid, tmp_path, rubric_ts)
    assert scorecard_stale == [], (
        "a scorecard evaluated against the LIVE draft was reported stale — the panel is "
        "comparing against a different document than the pass read"
    )
    assert uncited_stale is False


def test_a_pass_that_evaluated_the_stale_disk_draft_is_reported_stale(pg_session_factory, tmp_path):
    """The other direction, so the flags are not simply hardwired off."""
    wid = _setup(pg_session_factory, tmp_path, "stale-flags-disk@umu.se")
    disk_sha = hashlib.sha256(STALE_DISK.encode()).hexdigest()
    with pg_session_factory() as s:
        rubric_ts = s.get(Workspace, wid).rubric_updated_at
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={"criteria": [], "overall_band": "ok", "summary": ""},
                evaluated_draft_sha=disk_sha,
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=rubric_ts,
            )
        )
        s.add(
            UncitedClaimsJob(
                workspace_id=wid, status="completed", evaluated_draft_sha=disk_sha, claims=[]
            )
        )
        s.commit()

    scorecard_stale, uncited_stale = _stale_flags(pg_session_factory, wid, tmp_path, rubric_ts)
    assert "draft" in scorecard_stale
    assert uncited_stale is True
