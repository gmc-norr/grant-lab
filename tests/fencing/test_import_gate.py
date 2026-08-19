"""F3c — the import boundary for tool code.

The rule is POSITIVE: a tool may import the standard library and the ports, and
nothing else. A forbidden-name list would not do — a tool can reach an effectful
project helper that exposes no SQLAlchemy in the tool's own module.
"""

from __future__ import annotations

from tests.fencing.import_gate import FIXTURES, check_file, check_source


def _check(src: str) -> list[str]:
    return check_source(src, path="tools/example.py")


# ------------------------------------------------------------------- allowed


def test_the_standard_library_is_allowed():
    assert _check("import uuid\nfrom dataclasses import dataclass\n") == []


def test_future_annotations_are_allowed():
    assert _check("from __future__ import annotations\n") == []


def test_the_ports_package_is_allowed():
    src = (
        "from call_manager.proposal_assist.ports import Ports\n"
        "from call_manager.proposal_assist.ports.effects import SubmitScorecard\n"
        "from call_manager.proposal_assist.ports.errors import EffectRejected\n"
    )
    assert _check(src) == []


# ------------------------------------------------------------------ rejected


def test_sqlalchemy_is_rejected():
    assert len(_check("from sqlalchemy import select\n")) == 1


def test_the_models_module_is_rejected():
    assert len(_check("from call_manager.db.models import Workspace\n")) == 1


def test_the_port_implementations_are_rejected():
    """The whole point of the split: a tool may name the contract, never the
    implementation that owns a session."""
    src = "from call_manager.proposal_assist.port_impl.effects import SqlEffects\n"
    assert len(_check(src)) == 1


def test_a_plausible_but_unapproved_internal_helper_is_rejected():
    """THE DECISIVE CASE (spec 9). `editing.sectionizer` contains no SQLAlchemy in
    its own module, so a forbidden-name implementation would wave it through — and
    a tool that can reach arbitrary project helpers can reach effectful ones."""
    src = "from call_manager.proposal_assist.editing.sectionizer import split_sections\n"
    violations = _check(src)
    assert len(violations) == 1
    assert "sectionizer" in violations[0]


def test_a_sibling_tool_module_is_rejected():
    src = "from call_manager.proposal_assist.tools.citations import _read_proposal\n"
    assert len(_check(src)) == 1


def test_plain_import_of_a_project_module_is_rejected():
    assert len(_check("import call_manager.proposal_assist.collab.canonical\n")) == 1


def test_a_third_party_package_is_rejected():
    """Not stdlib, not ports. The allowlist has exactly two entries."""
    assert len(_check("import httpx\n")) == 1


# ----------------------------------------------------- relative imports at all


def test_a_bare_relative_import_is_rejected():
    """`from . import x` inside tools/ can only resolve to another tools/ module."""
    violations = _check("from . import citations\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


def test_a_dotted_relative_import_is_rejected():
    violations = _check("from .citations import helper\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


def test_a_parent_relative_import_is_rejected():
    violations = _check("from ..collab import canonical\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


# ------------------------------------------------------------------- reporting


def test_a_violation_names_the_file_and_what_it_imported():
    src = "from sqlalchemy import select\n"
    violation = check_source(src, path="tools/scorecard.py")[0]
    assert "tools/scorecard.py" in violation
    assert "sqlalchemy" in violation


def test_every_violation_is_reported_not_just_the_first():
    src = (
        "from sqlalchemy import select\n"
        "from call_manager.db.models import Workspace\n"
        "import httpx\n"
    )
    assert len(_check(src)) == 3


def test_an_import_inside_a_function_is_still_checked():
    """`tools/proposal.py` uses a lazy import today to break a module cycle. A
    checker that only walked module-level nodes would miss exactly that."""
    src = (
        "def f():\n"
        "    from call_manager.proposal_assist.proposals.proposing import propose\n"
        "    return propose\n"
    )
    assert len(_check(src)) == 1


def test_a_type_checking_only_import_is_still_checked():
    """`if TYPE_CHECKING:` imports do not execute, but they still name a module
    and still tell the next reader that reaching it is normal."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.orm import sessionmaker\n"
    )
    assert len(_check(src)) == 1


# --------------------------------------------------------- resource locators


def test_reaching_the_repo_directory_is_rejected():
    """Spec 4.1. The import list is clean here — this is exactly the hole a
    forbidden-import rule alone leaves open."""
    src = 'def f(ctx):\n    (ctx.repo_dir / "proposal.md").write_text("x")\n'
    violations = _check(src)
    assert len(violations) == 1
    assert "repo_dir" in violations[0]


def test_reaching_the_session_factory_is_rejected():
    src = "def f(ctx):\n    with ctx.session_factory() as s:\n        return s\n"
    assert len(_check(src)) == 1


def test_reaching_settings_is_rejected():
    """`ctx.settings` carries workspaces_dir, the ollama url and the embedding
    model — it is an authority handle, not a value."""
    src = "def f(ctx):\n    return ctx.settings.ollama_url\n"
    assert len(_check(src)) == 1


def test_a_chained_locator_reports_each_hop():
    """`ctx.settings.workspaces_dir` names two locators; both are reported so a
    partial fix cannot look complete."""
    src = "def f(ctx):\n    return ctx.settings.workspaces_dir\n"
    assert len(_check(src)) == 2


def test_a_locator_named_on_any_object_is_rejected():
    """The rule is on the NAME, not on `ctx` specifically — rebinding the context
    to another variable must not launder it."""
    src = "def f(anything):\n    return anything.repo_dir\n"
    assert len(_check(src)) == 1


def test_an_unrelated_attribute_is_fine():
    src = "def f(ctx):\n    return ctx.workspace_id\n"
    assert _check(src) == []


def test_a_locator_arriving_as_a_parameter_is_rejected():
    """Spec 4.1, the laundering case. Nothing here is an attribute: the composition
    root passes `ctx.repo_dir` in and the tool never writes `ctx` at all. An
    attribute-only rule waves this through, and `registry.py` — the module that would
    do the passing — is permanently skipped, so nothing else would catch it. The
    parameter and its one use are both reported. `citations.py:135` is this shape in
    production today."""
    src = 'def f(repo_dir, text):\n    (repo_dir / "proposal.md").write_text(text)\n'
    violations = _check(src)
    assert len(violations) == 2
    assert all("repo_dir" in v for v in violations)


def test_a_locator_bound_to_a_local_name_is_rejected():
    """A locator pulled out of `**kwargs` never touches an attribute either."""
    src = (
        "def f(**kw):\n"
        '    session_factory = kw["session_factory"]\n'
        "    return session_factory()\n"
    )
    assert len(_check(src)) == 2


# ------------------------------------------------------------ reading a file


def test_the_clean_fixture_passes():
    assert check_file(FIXTURES / "clean_tool.py") == []


def test_the_helper_reaching_fixture_fails():
    violations = check_file(FIXTURES / "reaches_a_helper.py")
    assert len(violations) == 1
    assert "sectionizer" in violations[0]


def test_the_locator_reaching_fixture_fails():
    violations = check_file(FIXTURES / "reaches_a_locator.py")
    assert violations, "the locator fixture stopped violating — fix the fixture"
    assert any("repo_dir" in v for v in violations)


def test_the_laundered_locator_fixture_fails():
    """Clean imports AND no attribute access, and still a repo write."""
    violations = check_file(FIXTURES / "laundered_locator.py")
    assert violations, "the parameter fixture stopped violating — fix the fixture"
    assert any("repo_dir" in v for v in violations)


def test_check_file_reports_a_readable_path():
    """A violation must name a path a human can open, not an absolute temp path."""
    violation = check_file(FIXTURES / "reaches_a_helper.py")[0]
    assert violation.startswith("tests/fencing/fixtures/reaches_a_helper.py:")
