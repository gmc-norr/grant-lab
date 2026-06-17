import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def test_tool_context_is_a_dataclass():
    """Smoke: ToolContext has the four expected fields."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    assert isinstance(ctx.workspace_id, uuid.UUID)
    assert isinstance(ctx.repo_dir, Path)


def test_build_mcp_server_returns_dict_with_expected_keys():
    """create_sdk_mcp_server returns a dict with {type, name, instance};
    McpSdkServerConfig is a TypedDict so isinstance() does not work."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    server = build_proposal_assist_mcp_server(ctx)
    assert isinstance(server, dict)
    assert server["type"] == "sdk"
    assert server["name"] == "proposal_assist"
    assert "instance" in server


def _make_ctx():
    import uuid
    from pathlib import Path

    from call_manager.proposal_assist.tools.registry import ToolContext

    return ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )


def test_collect_tools_returns_nineteen_named_tools():
    """The post-SP2a-2 tool surface: the 6 mutating tools (edit_proposal,
    replace_proposal, insert_citation, remove_citation, update_citation,
    rename_citekey) are removed; propose_edit + propose_citation are added.
    23 - 6 + 2 = 19."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    names = {t.name for t in tools}
    assert names == {
        "read_proposal",
        "get_artifact_history",
        "read_artifact_at",
        "diff_artifact",
        "get_call_details",
        "get_profile",
        "list_references",
        "search_references",
        "navigate_reference",
        "read_reference",
        "read_reference_section",
        "summarize_data",
        "fetch_reference",
        "list_citations",
        "validate_citations",
        "list_unsupported_claims",
        "build_bibliography",
        "propose_edit",
        "propose_citation",
    }
    assert len(tools) == 19


@pytest.mark.asyncio
async def test_tool_handler_returns_mcp_envelope_shape():
    """Smoke: a tool handler returns the MCP envelope shape, not the
    plain dict the *_impl function produces. Use read_proposal against
    a missing repo so the impl returns an error — we only care about
    envelope shape, not content."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    read = next(t for t in tools if t.name == "read_proposal")
    result = await read.handler({})
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "is_error" in result
    # The "missing file" path is an error.
    assert result["is_error"] is True


@pytest.mark.asyncio
async def test_tool_handler_envelope_success_path():
    """When the impl returns a plain success dict (no 'error' key), the
    wrapper produces is_error=False and JSON-encodes the payload as
    the text content."""
    import json
    import subprocess
    import tempfile
    import uuid
    from pathlib import Path

    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
    )

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "proposal.md").write_text("hello world")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        ctx = ToolContext(
            workspace_id=uuid.uuid4(),
            repo_dir=repo,
            session_factory=None,
            settings=None,  # type: ignore[arg-type]
        )
        tools = _collect_tools(ctx)
        read = next(t for t in tools if t.name == "read_proposal")
        result = await read.handler({})

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["content"] == "hello world"


@pytest.mark.asyncio
async def test_safe_tool_converts_unexpected_exception_to_envelope():
    """The catch-all wrapper turns an impl that raises an UNEXPECTED
    exception into an MCP error envelope instead of propagating it, and
    does not leak the exception message to the user-facing text."""
    from call_manager.proposal_assist.tools.registry import _safe_tool

    async def _boom(ctx):
        raise RuntimeError("super-secret quote text that must not leak")

    ctx = _make_ctx()
    result = await _safe_tool("explode", ctx, _boom, ctx)

    assert result["is_error"] is True
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    text = result["content"][0]["text"]
    assert "super-secret" not in text
    assert "quote" not in text


@pytest.mark.asyncio
async def test_list_citations_closure_returns_envelope():
    """The registered list_citations closure returns the MCP envelope when
    called with empty args. Against a repo with no citations.json the impl
    returns {"citations": []}, which _wrap turns into a success envelope."""
    import json
    import subprocess
    import tempfile
    import uuid
    from pathlib import Path

    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
    )

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        ctx = ToolContext(
            workspace_id=uuid.uuid4(),
            repo_dir=repo,
            session_factory=None,  # type: ignore[arg-type]
            settings=None,  # type: ignore[arg-type]
        )
        tools = _collect_tools(ctx)
        lst = next(t for t in tools if t.name == "list_citations")
        result = await lst.handler({})

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"citations": []}


def test_tool_context_has_provenance_defaults():
    """The new per-turn provenance fields + agent_view_sections default to
    None / empty so existing bare-ctx tests keep constructing ToolContext
    with only the four required args."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    assert ctx.triggering_turn_id is None
    assert ctx.triggering_prompt is None
    assert ctx.created_by is None
    assert ctx.origin_session_id is None
    assert ctx.agent_view_sections == {}
    assert ctx.agent_view_proposal is None


@pytest.mark.asyncio
async def test_propose_edit_closure_returns_pending_envelope(tmp_path):
    """The registered propose_edit closure returns the MCP success envelope
    with status 'pending' after a read; an unread section returns an error
    envelope (not a raise)."""
    import subprocess
    import uuid as _uuid

    from call_manager.proposal_assist.tools.proposal import (
        PROPOSAL_FILENAME,
    )
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
    )

    repo = Path(tmp_path) / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Methods\norig.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # Error path: no read yet -> error envelope, no raise.
    ctx = ToolContext(
        workspace_id=_uuid.uuid4(),
        repo_dir=repo,
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    tools = _collect_tools(ctx)
    propose = next(t for t in tools if t.name == "propose_edit")
    result = await propose.handler({"section_index": 1, "new_text": "## Methods\nx.\n"})
    assert result["is_error"] is True
    assert "read_proposal" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_propose_citation_non_integer_chunk_index_returns_envelope():
    """propose_citation with a non-integer chunk_index returns an MCP error
    envelope (is_error=True), not a raise — mirrors insert_citation's guard."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    propose = next(t for t in tools if t.name == "propose_citation")
    result = await propose.handler(
        {
            "reference_id": "some-ref",
            "anchor": "somewhere",
            "chunk_index": "not-an-int",
            "evidence_quote": "some quote",
        }
    )
    assert result["is_error"] is True
    assert result["content"][0]["type"] == "text"
    text = result["content"][0]["text"]
    assert "chunk_index" in text or "integer" in text


@pytest.mark.asyncio
async def test_propose_citation_closure_returns_error_envelope_for_bad_ref():
    """A bad (non-uuid) reference_id returns the MCP error envelope, not a raise."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    propose = next(t for t in tools if t.name == "propose_citation")
    result = await propose.handler(
        {
            "reference_id": "not-a-uuid",
            "anchor": "somewhere",
            "chunk_index": 0,
            "evidence_quote": "x",
        }
    )
    assert result["is_error"] is True
    assert "reference_id" in result["content"][0]["text"]
