"""Fence F2 — detector fixtures.

Every case is a SYNTHETIC source string. Asserting detectors against the live
tree only proves they agree with themselves today; fixtures prove they still bite
in F5 when someone adds a tool.
"""

from __future__ import annotations

from tests.fencing.detectors import (
    KIND_CLAIM_FN,
    KIND_TOOL_COMMIT,
    KIND_TOOL_CONTEXT,
    Site,
    find_sites,
)

TOOLS = "src/call_manager/proposal_assist/tools/thing.py"
JOBS = "src/call_manager/proposal_assist/coherence/jobs.py"


def test_finds_a_committing_tool_function():
    src = """
def do_thing(ctx):
    with ctx.session_factory() as session:
        session.commit()
"""
    assert find_sites(src, TOOLS) == {Site(KIND_TOOL_COMMIT, TOOLS, "do_thing")}


def test_two_commits_in_one_function_are_one_site():
    src = """
def do_thing(ctx):
    with ctx.session_factory() as s:
        s.commit()
    with ctx.session_factory() as s2:
        s2.commit()
"""
    assert find_sites(src, TOOLS) == {Site(KIND_TOOL_COMMIT, TOOLS, "do_thing")}


def test_nested_functions_get_QUALIFIED_symbols():
    """references.py really has two committing helpers nested inside
    fetch_reference_impl. Bare names would collapse same-named nested helpers in
    different outer functions into one entry, silently under-reporting."""
    src = """
def outer_a(ctx):
    def helper():
        with ctx.session_factory() as s:
            s.commit()
    return helper

def outer_b(ctx):
    def helper():
        with ctx.session_factory() as s:
            s.commit()
    return helper
"""
    assert find_sites(src, TOOLS) == {
        Site(KIND_TOOL_COMMIT, TOOLS, "outer_a.helper"),
        Site(KIND_TOOL_COMMIT, TOOLS, "outer_b.helper"),
    }


def test_an_outer_and_its_nested_helper_are_separate_sites():
    src = """
def outer(ctx):
    with ctx.session_factory() as s:
        s.commit()

    def helper():
        with ctx.session_factory() as s2:
            s2.commit()
    return helper
"""
    assert find_sites(src, TOOLS) == {
        Site(KIND_TOOL_COMMIT, TOOLS, "outer"),
        Site(KIND_TOOL_COMMIT, TOOLS, "outer.helper"),
    }


def test_a_def_nested_inside_a_with_block_still_qualifies():
    """The scope stack pushes only on def/class, so a helper defined inside a
    `with` is still `outer.helper`. Task 4's symbol resolver must agree."""
    src = """
def outer(ctx):
    with ctx.session_factory() as s:
        def helper():
            s.commit()
        return helper
"""
    assert find_sites(src, TOOLS) == {Site(KIND_TOOL_COMMIT, TOOLS, "outer.helper")}


def test_a_method_gets_a_qualified_symbol():
    src = """
class Thing:
    def save(self, ctx):
        with ctx.session_factory() as s:
            s.commit()
"""
    assert find_sites(src, TOOLS) == {Site(KIND_TOOL_COMMIT, TOOLS, "Thing.save")}


def test_a_read_only_tool_function_is_not_a_site():
    src = """
def read_thing(ctx):
    with ctx.session_factory() as s:
        return s.get(X, 1)
"""
    assert find_sites(src, TOOLS) == set()


def test_a_commit_outside_tools_is_not_a_tool_commit():
    src = """
def do_thing(ctx):
    with ctx.session_factory() as s:
        s.commit()
"""
    assert find_sites(src, "src/call_manager/web/api/workspaces.py") == set()


def test_finds_claim_functions_by_prefix():
    src = """
def claim_next_coherence_job(session):
    return None

def claim_queued_jobs(session, *, limit=5):
    return []
"""
    assert find_sites(src, JOBS) == {
        Site(KIND_CLAIM_FN, JOBS, "claim_next_coherence_job"),
        Site(KIND_CLAIM_FN, JOBS, "claim_queued_jobs"),
    }


def test_string_helpers_named_claim_are_not_claim_functions():
    src = """
def claim_context_sha(claim_text):
    return ""

def claim_drift_hash(claim, anchor):
    return ""
"""
    assert find_sites(src, "src/call_manager/proposal_assist/citations/audit.py") == set()


def test_finds_a_tool_context_construction():
    src = """
def build(session_factory):
    ctx = ToolContext(workspace_id=w, session_factory=session_factory)
    return ctx
"""
    path = "src/call_manager/proposal_assist/drafting/factory.py"
    assert find_sites(src, path) == {Site(KIND_TOOL_CONTEXT, path, "build")}


def test_a_tool_context_with_session_factory_None_is_excluded():
    """Two real sites do this: proposals/accepting.py (request-driven, holds its
    own session) and tools/registry.py::collect_mcp_tool_names (a static
    tool-name collector). Neither can originate a tool commit."""
    src = """
def accept(session, pc):
    ctx = ToolContext(workspace_id=pc.workspace_id, session_factory=None, settings=None)
    return ctx
"""
    path = "src/call_manager/proposal_assist/proposals/accepting.py"
    assert find_sites(src, path) == set()


def test_a_module_level_tool_context_reports_module():
    src = "ctx = ToolContext(workspace_id=w, session_factory=sf)\n"
    path = "src/call_manager/proposal_assist/tools/registry.py"
    assert find_sites(src, path) == {Site(KIND_TOOL_CONTEXT, path, "<module>")}


def test_site_identity_is_three_fields_and_carries_no_line_number():
    a = Site(KIND_TOOL_COMMIT, TOOLS, "f")
    assert a == Site(KIND_TOOL_COMMIT, TOOLS, "f")
    assert len(a) == 3
    assert sorted([Site("b", "p", "s"), Site("a", "p", "s")])[0].kind == "a"


def test_a_port_write_call_is_a_tool_commit_site():
    """The whole reason Task 1 exists. The canonical converted form has NO
    .commit() anywhere, so a detector that only knows about .commit() drops the
    row the moment the site is converted — and merge() rebuilds rows only from
    what the scan found."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        "async def submit_scorecard_impl(ctx, *, ports, invocation):\n"
        "    ports.effects.submit_scorecard(invocation.stamp(), cmd)\n"
    )
    sites = find_sites(src, "src/call_manager/proposal_assist/tools/scorecard.py")
    assert (KIND_TOOL_COMMIT, "submit_scorecard_impl") in {
        (s.kind, s.symbol) for s in sites
    }


def test_an_accounting_call_is_a_tool_commit_site_too():
    """Accounting operations are unfenced by design (spec 3.3) but they are still
    writes, and they still own inventory rows."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        "async def fetch_reference_impl(ctx, *, ports):\n"
        "    def _refund_fetch_slot():\n"
        "        ports.accounting.refund_fetch_slot(job_id)\n"
    )
    sites = find_sites(src, "src/call_manager/proposal_assist/tools/references.py")
    assert (KIND_TOOL_COMMIT, "fetch_reference_impl._refund_fetch_slot") in {
        (s.kind, s.symbol) for s in sites
    }


def test_a_port_call_outside_tools_is_not_a_site():
    """Same scoping as .commit(): the inventory is about TOOL code. port_impl and
    the domain layer call these methods constantly and are not tool sites."""
    from tests.fencing.detectors import find_sites

    src = "def helper(ports):\n    ports.effects.submit_review(stamp, cmd)\n"
    assert find_sites(src, "src/call_manager/proposal_assist/proposals/accepting.py") == set()


def test_the_port_method_list_is_derived_from_the_protocols():
    """A hand-written list drifts the first time a port gains a method, and the
    drift is silent: the new effect simply stops being detected."""
    from call_manager.proposal_assist.ports.accounting import Accounting
    from call_manager.proposal_assist.ports.effects import Effects
    from tests.fencing.detectors import PORT_WRITE_METHODS

    assert frozenset(Effects.__protocol_attrs__) | frozenset(
        Accounting.__protocol_attrs__
    ) == PORT_WRITE_METHODS
    assert "submit_scorecard" in PORT_WRITE_METHODS
    assert "refund_fetch_slot" in PORT_WRITE_METHODS
    # Queries are reads. Fencing reads is explicitly out of scope (spec §8).
    assert "get_reference" not in PORT_WRITE_METHODS


def test_a_lookalike_receiver_is_not_a_site():
    """The chain root must be the INJECTED `ports` name. Matching on
    `<anything>.effects.submit_review(...)` would let a local helper — or a test
    double a tool happened to hold — mint an inventory row, and worse, mark a
    qualname fenced that never touched a real capability."""
    from tests.fencing.detectors import find_sites

    path = "src/call_manager/proposal_assist/tools/scorecard.py"
    src = (
        "async def submit_scorecard_impl(ctx):\n"
        "    fake = _Local()\n"
        "    fake.effects.submit_scorecard(stamp, cmd)\n"
    )
    assert find_sites(src, path) == set()


def test_a_declared_relocated_effect_still_owns_a_site():
    """The whole reason Task 1 exists. F3d3a moves the citation core out of tools/,
    and find_sites scoped tool_commit detection to tools/ alone — so the move would
    delete the row, and RENAMES cannot carry it because merge() validates the target
    against what the scan found."""
    import pathlib

    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "relocated_effect.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/citations/core.py"
    assert (KIND_TOOL_COMMIT, "_write_citekey_cache") in {
        (s.kind, s.symbol) for s in find_sites(src, path)
    }


def test_an_ordinary_domain_commit_is_not_a_site():
    """The widening is a DECLARED list, not a directory sweep. The domain layer
    commits constantly; recording all of it would mint dozens of UNCLASSIFIED rows
    and the gate would stop being read."""
    import pathlib

    from tests.fencing.detectors import find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "unrelated_domain_commit.py"
    ).read_text(encoding="utf-8")
    assert find_sites(src, "src/call_manager/proposal_assist/editing/save.py") == set()


def test_the_relocated_scope_is_declared_and_currently_holds_the_citation_core():
    """The set is source, not inference. A path enters it when a slice moves an
    effect out of tools/, and the slice that moves it is the one that adds it."""
    from tests.fencing.detectors import RELOCATED_EFFECT_PATHS

    assert frozenset(
        {"src/call_manager/proposal_assist/citations/core.py"}
    ) == RELOCATED_EFFECT_PATHS


def test_the_fenced_measurement_uses_the_same_scope():
    """find_sites and find_fenced_tool_commits must agree on what is in scope. If
    only one widened, a relocated effect would own a row whose fenced flag could
    never be measured — permanently false, and silently so."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "relocated_effect.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/citations/core.py"
    # It commits directly, so it is NOT fenced — but the function must have looked,
    # which an out-of-scope path would not have done.
    assert find_fenced_tool_commits(src, path) == set()
    converted = src.replace("session.commit()", "ports.effects.submit_review(a, b)")
    assert find_fenced_tool_commits(converted, path) == {
        (path, "_write_citekey_cache")
    }
