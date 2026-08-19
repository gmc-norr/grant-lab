"""F3c — the import boundary for tool code.

The rule is POSITIVE: a tool may import the standard library and the ports, and
nothing else. A forbidden-name list would not do — a tool can reach an effectful
project helper that exposes no SQLAlchemy in the tool's own module.
"""

from __future__ import annotations

from tests.fencing.import_gate import check_source


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
