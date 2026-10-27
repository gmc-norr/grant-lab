"""F3d1 — the spine. Everything a converted tool will need, in place, with nothing
converted.

Spec 2026-08-18 revision 8 §7. This slice is additive by construction: if any test
here starts needing a tool module to have changed, the slice has exceeded its scope.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from types import SimpleNamespace

import pytest

from grant_lab.proposal_assist.ports.accounting import Accounting
from grant_lab.proposal_assist.ports.effects import Effects
from grant_lab.proposal_assist.ports.queries import Queries


def test_the_artifact_filenames_are_reachable_from_ports():
    """A converted tool may import only stdlib and ports, and it still has to name
    proposal.md. These constants are the reason ports/artifacts.py exists."""
    from grant_lab.proposal_assist.ports.artifacts import (
        CITATIONS_FILENAME,
        PROPOSAL_FILENAME,
        SPEC_FILENAME,
    )

    assert PROPOSAL_FILENAME == "proposal.md"
    assert SPEC_FILENAME == "proposal-spec.md"
    assert CITATIONS_FILENAME == "citations.json"


def test_the_old_artifacts_module_re_exports_the_same_objects():
    """GC-4: one source of truth. Every existing importer keeps working, and the
    value it gets is the value ports defines — not a second copy that can drift."""
    from grant_lab.proposal_assist import artifacts as old
    from grant_lab.proposal_assist.ports import artifacts as new

    assert old.PROPOSAL_FILENAME is new.PROPOSAL_FILENAME
    assert old.SPEC_FILENAME is new.SPEC_FILENAME
    assert old.Artifact is new.Artifact
    assert old.filename_for is new.filename_for


def test_spec_skeleton_stays_out_of_ports():
    """Seeded page content is not a capability. Keeping it behind is what stops
    ports/ from becoming the place things go when nobody knows where they belong."""
    from grant_lab.proposal_assist import artifacts as old
    from grant_lab.proposal_assist.ports import artifacts as new

    assert hasattr(old, "SPEC_SKELETON")
    assert not hasattr(new, "SPEC_SKELETON")


def test_no_module_redefines_the_citations_filename_except_the_unconverted_tool():
    """CITATIONS_FILENAME was written out FOUR times: references/deletion.py:38,
    reconcile.py:36, editing/save.py:41 and tools/citations.py:80. Three now import
    it. editing/save.py is included even though the plan's narrative is about tools
    — it is the copy eight other modules import (export.py, human_cite.py,
    collab/kernel.py, analysis/insert.py, web/api/workspaces.py,
    web/api/citation_audit.py, problems/aggregate.py), so leaving it out would leave
    the widest-blast-radius literal unguarded. The fourth is tools/citations.py,
    which F3d3 converts — until then this test is what stops the copies drifting."""
    from grant_lab.proposal_assist import reconcile
    from grant_lab.proposal_assist.editing import save
    from grant_lab.proposal_assist.ports.artifacts import CITATIONS_FILENAME
    from grant_lab.proposal_assist.references import deletion
    from grant_lab.proposal_assist.tools import citations

    assert reconcile.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert deletion.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert save.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert citations.CITATIONS_FILENAME == CITATIONS_FILENAME


def test_the_busy_error_is_reachable_from_ports():
    """Every Effects method can raise this — the shared lease has a 10s cap. A
    converted tool that cannot NAME it has to let it reach _safe_tool's catch-all,
    which turns "busy, retry" into "internal error"."""
    from grant_lab.proposal_assist.ports.errors import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    assert issubclass(CommitMutexTimeout, RuntimeError)
    assert BUSY_ERROR_MESSAGE == "workspace is busy — try again in a moment"


def test_locking_re_exports_the_identical_error_class():
    """GC-4, and it is load-bearing here: collab/test_collab_retire_mutex.py asserts
    membership of this class in a set of clean errors. Two classes that merely look
    alike would pass every equality test in the tree and fail that one."""
    from grant_lab.proposal_assist.drafting import locking
    from grant_lab.proposal_assist.ports import errors

    assert locking.CommitMutexTimeout is errors.CommitMutexTimeout
    assert locking.BUSY_ERROR_MESSAGE is errors.BUSY_ERROR_MESSAGE
    assert locking.DELETED_ERROR_MESSAGE is errors.DELETED_ERROR_MESSAGE


def test_an_existing_raise_site_is_still_caught_by_the_ports_class():
    """The relocation must not fork the taxonomy: something raised through the old
    import path is catchable through the new one."""
    from grant_lab.proposal_assist.drafting.locking import CommitMutexTimeout as Old
    from grant_lab.proposal_assist.ports.errors import CommitMutexTimeout as New

    try:
        raise Old("busy")
    except New as exc:
        assert str(exc) == "busy"


def test_invocation_is_reachable_from_ports():
    """GC-2, the reason this slice exists. Every exempt tool module pins the
    tools.registry import, a converted impl needs Invocation in its signature, and
    `from __future__ import annotations` does not save you — ruff reports F821 for a
    lazily-annotated name with no import. So the type must live in ports."""
    from grant_lab.proposal_assist.ports.invocation import Invocation

    assert hasattr(Invocation, "stamp")


def test_the_snapshot_factory_stays_with_the_context_it_reads():
    """ports/ is stdlib-only, so nothing in it may name ToolContext. The factory
    belongs beside its only production caller, _safe_tool."""
    from grant_lab.proposal_assist.ports.invocation import Invocation
    from grant_lab.proposal_assist.tools.registry import snapshot_invocation

    assert not hasattr(Invocation, "snapshot")
    assert callable(snapshot_invocation)


def test_registry_still_exports_invocation_as_the_same_class():
    """GC-4. Two test files and any future reader import it from registry."""
    from grant_lab.proposal_assist.ports.invocation import Invocation as FromPorts
    from grant_lab.proposal_assist.tools.registry import Invocation as FromRegistry

    assert FromPorts is FromRegistry


def test_a_snapshot_still_stamps_and_still_refuses_an_unfenced_actor():
    """The behaviour F3a and F3b pinned survives the move: generation 0 is REAL, and
    a missing generation raises rather than defaulting."""
    import uuid

    import pytest

    from grant_lab.proposal_assist.ports.errors import UnfencedInvocation
    from grant_lab.proposal_assist.tools.registry import (
        ToolContext,
        snapshot_invocation,
    )

    ws = uuid.uuid4()

    def _ctx(generation):
        return ToolContext(
            workspace_id=ws,
            repo_dir=None,
            session_factory=None,
            settings=None,
            lifecycle_generation=generation,
        )

    assert snapshot_invocation(_ctx(0)).stamp().generation == 0
    with pytest.raises(UnfencedInvocation):
        snapshot_invocation(_ctx(None)).stamp()


#: Every factory that builds a tool surface, and the builder-specific kwargs it needs.
#: All five already hold a session_factory, which is the only thing build_ports wants.
#: Parametrised for the same reason the F3a construction table is: a per-factory
#: omission is SILENT, because ToolContext.ports defaults to None.
_TOOL_SURFACE_FACTORIES = [
    pytest.param(
        "grant_lab.proposal_assist.evaluator.agent",
        "build_evaluator_adapter",
        lambda: {
            "requested_by": 1,
            "job_id": uuid.uuid4(),
            "draft_section_count": 0,
            "spec_section_count": 0,
            "rubric_criteria": [],
        },
        id="evaluator",
    ),
    pytest.param(
        "grant_lab.proposal_assist.coherence.agent",
        "build_keeper_adapter",
        lambda: {"requested_by": 1, "job_id": uuid.uuid4(), "locked": False},
        id="coherence_keeper",
    ),
    pytest.param(
        "grant_lab.proposal_assist.review.agent",
        "build_review_adapter",
        lambda: {
            "review_run_id": uuid.uuid4(),
            "item_id": uuid.uuid4(),
            "kind": "persona",
        },
        id="review",
    ),
    pytest.param(
        "grant_lab.proposal_assist.uncited_claims.agent",
        "build_uncited_claims_adapter",
        lambda: {"requested_by": 1, "job_id": uuid.uuid4(), "draft_section_count": 0},
        id="uncited_claims",
    ),
]


@pytest.mark.parametrize("module_path, builder_name, extra_kwargs", _TOOL_SURFACE_FACTORIES)
def test_every_one_shot_factory_injects_a_working_ports_bundle(
    tmp_path, monkeypatch, module_path, builder_name, extra_kwargs
):
    """A capability bundle, not module globals: every tool surface is handed one at
    construction. isinstance against the runtime_checkable Protocols is what proves
    the object is usable rather than merely present."""
    mod = importlib.import_module(module_path)
    builder = getattr(mod, builder_name)

    captured: dict = {}

    def _capture(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr(mod, "build_proposal_assist_mcp_server", _capture)

    class _FakeSDK:
        def __init__(self, *, tools_factory=None, **kwargs):
            tools_factory(tmp_path, uuid.uuid4())

    monkeypatch.setattr(mod, "ClaudeAgentSDKAdapter", _FakeSDK, raising=False)
    kwargs = dict(extra_kwargs())
    if "_sdk_factory" in inspect.signature(builder).parameters:
        kwargs["_sdk_factory"] = _FakeSDK

    builder(
        settings=SimpleNamespace(
            proposal_assist_llm_model="m",
            workspaces_dir=tmp_path,
            # Every builder routes through build_sdk_adapter now, which reads the
            # org key off settings (H4). Fake value; nothing here starts a CLI.
            anthropic_api_key="sk-ant-api03-FAKE-not-a-real-key",
        ),
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        lifecycle_generation=3,
        **kwargs,
    )

    ports = captured["ctx"].ports
    assert ports is not None, f"{builder_name} built a tool surface with no ports"
    assert isinstance(ports.queries, Queries)
    assert isinstance(ports.effects, Effects)
    assert isinstance(ports.accounting, Accounting)


def test_every_tool_surface_construction_injects_ports():
    """The fifth factory cannot be faked like the four above: drafting/factory.py
    imports ClaudeAgentSDKAdapter and build_proposal_assist_mcp_server LAZILY, inside
    _build_adapter, and reaches build_citation_service on the way — so patching its
    module namespace does not reach them and a stub Settings does not survive the
    trip. An AST check covers all five uniformly instead.

    The two INERT constructions are excluded by the same literal
    tests/fencing/detectors.py keys on: session_factory=None. That is not a
    coincidence to tidy away — it is the one rule both places have to agree on.
    """
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    missing = []
    found = 0
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "ToolContext":
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            inert = isinstance(kwargs.get("session_factory"), ast.Constant) and (
                kwargs["session_factory"].value is None
            )
            if inert:
                continue
            found += 1
            if "ports" not in kwargs:
                missing.append(f"{path.relative_to(src_root)}:{node.lineno}")

    assert found == 5, f"expected 5 live tool-surface constructions, found {found}"
    assert not missing, "tool surfaces built without a ports bundle:\n  " + "\n  ".join(missing)


def test_a_context_without_ports_is_still_constructible():
    """GC-5. collect_mcp_tool_names in tools/registry.py and the shim in
    proposals/accepting.py build a context with session_factory=None, and
    tests/fencing/detectors.py keys on exactly that literal to classify them INERT and
    keep them out of the inventory. They must not gain a ports argument (there is no
    session_factory there to build a bundle from), so the field has to default.
    No line numbers here on purpose: Task 3 already moved the registry site."""
    from grant_lab.proposal_assist.tools.registry import ToolContext

    ctx = ToolContext(workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None)
    assert ctx.ports is None


async def test_safe_tool_threads_the_ports_bundle_to_the_impl():
    """The dispatch half of the spine. F3d2 flips pass_ports per tool as sites are
    converted; this proves the wire works before anything rides on it."""
    from grant_lab.proposal_assist.port_impl import build_ports
    from grant_lab.proposal_assist.tools.registry import ToolContext, _safe_tool

    seen: dict = {}

    async def _impl(ctx, *, ports=None, invocation=None):
        seen["ports"] = ports
        seen["invocation"] = invocation
        return {"ok": True}

    bundle = build_ports(lambda: None)
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
        lifecycle_generation=2,
        ports=bundle,
    )

    await _safe_tool("t", ctx, _impl, ctx, pass_ports=True, pass_invocation=True)

    assert seen["ports"] is bundle
    assert seen["invocation"].lifecycle_generation == 2


async def test_asking_for_ports_a_surface_never_built_fails_loudly():
    """Not a runtime condition — a tool surface built by a path that never called
    build_ports. Handing the impl None would surface later as an AttributeError deep
    inside a tool, attributed to the wrong cause."""
    from grant_lab.proposal_assist.tools.registry import ToolContext, _safe_tool

    async def _impl(ctx, *, ports=None):
        return {"ok": True}

    ctx = ToolContext(workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None)

    with pytest.raises(RuntimeError, match="no ports"):
        await _safe_tool("t", ctx, _impl, ctx, pass_ports=True)


async def test_an_unconverted_tool_is_dispatched_exactly_as_before():
    """GC-1. Nothing is converted in this slice, so the default path must be
    untouched: no ports keyword reaches an impl that did not ask for one."""
    from grant_lab.proposal_assist.tools.registry import ToolContext, _safe_tool

    seen: dict = {}

    async def _impl(ctx):
        seen["called"] = True
        return {"ok": True}

    ctx = ToolContext(workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None)

    result = await _safe_tool("t", ctx, _impl, ctx)
    assert seen["called"] is True
    # NOT `"error" not in str(result)`: the success envelope is
    # {"content": [...], "is_error": False}, so the substring "error" is always
    # present inside the KEY. That assertion is false by construction, before and
    # after this task.
    assert result["is_error"] is False
    assert "ports" not in seen
