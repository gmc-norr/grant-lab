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
