"""Tests for the build_bibliography MCP tool (Slice 4 Task 5).

build_bibliography_impl wires the fail-closed bibliography engine behind a
write tool: it resolves the effective citation style (arg or the workspace
setting), reads proposal.md + the ledger from disk, and renders bibliography.md
under the intra-turn write lock. The engine itself is exhaustively tested in
tests/proposal_assist/citations/test_build_bibliography.py; here we exercise the
TOOL wiring (style resolution, blocked-path shaping, on-disk read, MCP envelope).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.db.models import Reference
from call_manager.proposal_assist.citations.bibliography import BIBLIOGRAPHY_FILENAME
from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.tools.audit import build_bibliography_impl
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext, _collect_tools
from tests.proposal_assist.conftest import make_workspace

SMITH_CSL = {
    "author": [{"family": "Smith", "given": "Jane"}],
    "title": "Adjuvant therapy outcomes",
    "container-title": "Journal of Oncology",
    "volume": "27",
    "issue": "4",
    "page": "511-520",
    "issued": {"date-parts": [[2021]]},
    "DOI": "10.1000/abc",
}


def _init_repo(repo_dir: Path, proposal: str, citations: dict | None) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(proposal)
    files = [PROPOSAL_FILENAME]
    if citations is not None:
        (repo_dir / CITATIONS_FILENAME).write_text(json.dumps(citations))
        files.append(CITATIONS_FILENAME)
    subprocess.run(["git", "add", *files], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _ledger(rows: list[dict]) -> dict:
    return {"version": 2, "evidence": rows}


def _row(*, citekey: str, reference_id, ev_id: str) -> dict:
    return {"id": ev_id, "citekey": citekey, "reference_id": str(reference_id)}


def _seed_reference(
    pg_session_factory,
    workspace_id,
    *,
    citekey: str,
    csl_json: dict,
    metadata_status: str = "approved",
) -> uuid.UUID:
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            csl_json=csl_json,
            metadata_status=metadata_status,
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.commit()
    return rid


def _make_workspace(pg_session_factory, *, citation_style: str = "vancouver"):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.citation_style = citation_style
        s.commit()
        return ws.id


def _ctx(pg_session_factory, repo_dir: Path, wsid):
    # build_bibliography_impl never reads ctx.settings, so None is fine here and
    # avoids the Settings() env requirement (mirrors test_tools_registry).
    return ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_happy_path_writes_bibliography(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = f"# P\n\nWe argue {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} here.\n"
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        proposal,
        _ledger([_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]),
    )

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))

    assert "error" not in result
    assert result["citekey_count"] == 1
    assert "Smith" in result["bibliography_md"]
    assert result["commit_sha"]
    assert (repo / BIBLIOGRAPHY_FILENAME).exists()


@pytest.mark.asyncio
async def test_blocked_path_returns_error_and_blocked(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(
        pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL, metadata_status="pending"
    )
    proposal = f"# P\n\nWe argue {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} here.\n"
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        proposal,
        _ledger([_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]),
    )

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))

    assert "blocked" in result
    assert result["blocked"][0]["citekey"] == "smith2021"
    assert result["blocked"][0]["reason"] == "metadata_unapproved"
    # Fail-closed: nothing written.
    assert not (repo / BIBLIOGRAPHY_FILENAME).exists()


@pytest.mark.asyncio
async def test_style_override_arg_beats_workspace_setting(pg_session_factory, tmp_path):
    # Workspace default is vancouver; the explicit author-year arg must win.
    wsid = _make_workspace(pg_session_factory, citation_style="vancouver")
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021a", csl_json=SMITH_CSL)
    proposal = f"# P\n\nWe argue {build_marker('smith2021a', 'ev-aaaaaaaaaaaa')} here.\n"
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        proposal,
        _ledger([_row(citekey="smith2021a", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]),
    )

    result = await build_bibliography_impl(
        _ctx(pg_session_factory, repo, wsid), style="author-year"
    )

    # Author-year renders "(2021a)"; Vancouver would render "[1] ".
    assert "(2021a)" in result["bibliography_md"]
    assert "[1] " not in result["bibliography_md"]


@pytest.mark.asyncio
async def test_default_style_from_workspace_setting(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory, citation_style="author-year")
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021a", csl_json=SMITH_CSL)
    proposal = f"# P\n\nWe argue {build_marker('smith2021a', 'ev-aaaaaaaaaaaa')} here.\n"
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        proposal,
        _ledger([_row(citekey="smith2021a", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]),
    )

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))  # no style arg

    assert "(2021a)" in result["bibliography_md"]  # author-year applied from setting


@pytest.mark.asyncio
async def test_invalid_style_rejected(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    repo = tmp_path / "repo"
    _init_repo(repo, "# P\n", _ledger([]))

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid), style="chicago")

    assert "error" in result
    assert "invalid style" in result["error"]
    assert not (repo / BIBLIOGRAPHY_FILENAME).exists()


@pytest.mark.asyncio
async def test_missing_proposal_returns_error(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))

    assert "error" in result


@pytest.mark.asyncio
async def test_empty_bibliography_success_no_commit(pg_session_factory, tmp_path):
    # No markers → success envelope with empty body and no commit; nothing written.
    wsid = _make_workspace(pg_session_factory)
    repo = tmp_path / "repo"
    _init_repo(repo, "# P\n\nNo citations here.\n", _ledger([]))

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))

    assert "error" not in result
    assert result["citekey_count"] == 0
    assert result["bibliography_md"] == ""
    assert result["commit_sha"] is None
    assert not (repo / BIBLIOGRAPHY_FILENAME).exists()


@pytest.mark.asyncio
async def test_missing_workspace_row_falls_back_to_vancouver(pg_session_factory, tmp_path):
    # Workspace row absent (e.g. deleted mid-turn): style resolution must fall
    # back to vancouver rather than crash. Use an empty proposal so no references
    # are needed and the success path is reached.
    wsid = uuid.uuid4()  # never inserted
    repo = tmp_path / "repo"
    _init_repo(repo, "# P\n\nNothing cited.\n", _ledger([]))

    result = await build_bibliography_impl(_ctx(pg_session_factory, repo, wsid))

    assert "error" not in result
    assert result["citekey_count"] == 0


@pytest.mark.asyncio
async def test_registered_closure_returns_mcp_envelope(pg_session_factory, tmp_path):
    wsid = _make_workspace(pg_session_factory)
    rid = _seed_reference(pg_session_factory, wsid, citekey="smith2021", csl_json=SMITH_CSL)
    proposal = f"# P\n\nWe argue {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} here.\n"
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        proposal,
        _ledger([_row(citekey="smith2021", reference_id=rid, ev_id="ev-aaaaaaaaaaaa")]),
    )

    tools = _collect_tools(_ctx(pg_session_factory, repo, wsid))
    tool = next(t for t in tools if t.name == "build_bibliography")
    result = await tool.handler({})

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["citekey_count"] == 1
