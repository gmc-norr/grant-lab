"""F3c — the import boundary for tool code.

The rule is POSITIVE: a tool may import the standard library and the ports, and
nothing else. A forbidden-name list would not do — a tool can reach an effectful
project helper that exposes no SQLAlchemy in the tool's own module.
"""

from __future__ import annotations

from tests.fencing.import_gate import (
    FIXTURES,
    REPO,
    TOOLS_DIR,
    check_composition_root,
    check_file,
    check_source,
    normalized_violation,
    tool_key,
    tool_modules,
)
from tests.fencing.inventory import GATE_SKIPS, TOOLS_NOT_YET_CONVERTED


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


# ----------------------------------------------------- the gate over tools/


def test_every_tool_module_is_either_clean_or_exempt():
    """The armed gate. A module not named in TOOLS_NOT_YET_CONVERTED must pass."""
    problems = []
    for path in tool_modules():
        key = tool_key(path)
        if key in GATE_SKIPS or key in TOOLS_NOT_YET_CONVERTED:
            continue
        problems.extend(check_file(path))
    assert not problems, (
        "tool modules violate the import boundary and are not exempt:\n  "
        + "\n  ".join(problems)
        + "\n\nEither fix the module or, if it is genuinely not converted yet, add "
        "it to TOOLS_NOT_YET_CONVERTED in tests/fencing/inventory.py — which is a "
        "reviewed diff, not a formality."
    )


def test_no_exemption_is_stale():
    """The other direction, and the one that makes the list shrink. A converted
    module left on the list turns the gate green for the wrong reason."""
    clean = []
    for name in sorted(TOOLS_NOT_YET_CONVERTED):
        path = TOOLS_DIR / name
        if not check_file(path):
            clean.append(name)
    assert not clean, (
        "these modules now PASS the import boundary but are still exempt:\n  "
        + "\n  ".join(clean)
        + "\n\nDelete them from TOOLS_NOT_YET_CONVERTED — removing the entry is "
        "part of converting the module."
    )


def test_no_exempt_module_acquires_a_new_violation():
    """The THIRD direction, and the one that stops `tools/` drifting further away
    while it waits for its slice. `test_no_exemption_is_stale` asks only that ONE
    violation survive, so without this an exempt module could import anything at all
    and stay green — and today every non-skipped module is exempt, so that is the
    whole package. The pin is normalized: line numbers are dropped, because moving a
    line is not new authority, and it is a SUBSET check, so removing a violation is
    always free."""
    problems = []
    for name, pinned in sorted(TOOLS_NOT_YET_CONVERTED.items()):
        for violation in check_file(TOOLS_DIR / name):
            if normalized_violation(violation) not in pinned:
                problems.append(violation)
    assert not problems, (
        "exempt modules acquired violations their entry does not pin:\n  "
        + "\n  ".join(problems)
        + "\n\nConvert the module, or — if this is genuinely unavoidable for now — "
        "widen its entry in TOOLS_NOT_YET_CONVERTED. Widening is a reviewed diff: do "
        "not paste whatever this failure printed."
    )


def test_every_exemption_names_a_module_that_exists():
    """A renamed or deleted tool must not leave a permanent free pass behind."""
    on_disk = {tool_key(p) for p in tool_modules()}
    missing = sorted(TOOLS_NOT_YET_CONVERTED.keys() - on_disk)
    assert not missing, f"exemptions naming modules that no longer exist: {missing}"


def test_the_skips_and_the_exemptions_do_not_overlap():
    """A module is permanently infrastructure or temporarily unconverted, never
    both — otherwise deleting the exemption would silently change nothing."""
    assert not (GATE_SKIPS & TOOLS_NOT_YET_CONVERTED.keys())


def test_the_composition_root_may_not_reach_the_database():
    """The skip is narrow, not unconditional. registry.py wires every tool, so it
    names modules a tool may not — but it must not do database work itself, and a
    TYPE_CHECKING-only annotation import cannot do anything at runtime."""
    seen = set()
    problems = []
    for path in tool_modules():
        key = tool_key(path)
        if key not in GATE_SKIPS:
            continue
        seen.add(key)
        # The repo-relative path, not the bare key: a failure must name something a
        # human can open, which is what test_check_file_reports_a_readable_path pins
        # for the other rule.
        shown = str(path.resolve().relative_to(REPO))
        problems.extend(check_composition_root(path.read_text(encoding="utf-8"), path=shown))
    assert not problems, "\n  ".join(problems)
    assert seen == set(GATE_SKIPS), (
        f"GATE_SKIPS names a module that is not in tools/: {sorted(GATE_SKIPS - seen)}"
    )


def test_a_nested_tool_module_cannot_inherit_a_top_level_exemption(tmp_path, monkeypatch):
    """The identity is the path under `tools/`, not the basename. Otherwise a new
    `tools/sub/audit.py` rides on `audit.py`'s exemption, and a new
    `tools/sub/__init__.py` rides on the composition root's PERMANENT skip while the
    narrower rule — which reads `TOOLS_DIR / key` — never sees it. `citations.py` is
    37 KB and `references.py` 23 KB, so splitting one into a package during
    conversion is exactly what F3d will want to do."""
    import tests.fencing.import_gate as ig

    nested = tmp_path / "sub" / "audit.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("from sqlalchemy import select\n", encoding="utf-8")
    monkeypatch.setattr(ig, "TOOLS_DIR", tmp_path)
    assert ig.tool_key(nested) == "sub/audit.py"
    assert ig.tool_key(nested) not in TOOLS_NOT_YET_CONVERTED
    assert ig.tool_key(nested) not in GATE_SKIPS


def test_the_composition_root_rule_rejects_a_runtime_sqlalchemy_import():
    src = "from sqlalchemy.orm import sessionmaker\n"
    assert len(check_composition_root(src, path="registry.py")) == 1


def test_the_composition_root_rule_allows_a_type_checking_import():
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.orm import sessionmaker\n"
    )
    assert check_composition_root(src, path="registry.py") == []


def test_the_composition_root_rule_rejects_models_even_under_type_checking():
    """Naming a model is naming a table. There is no annotation-only excuse for
    it in the module that builds the tool surface."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from call_manager.db.models import Workspace\n"
    )
    assert len(check_composition_root(src, path="registry.py")) == 1


def test_the_composition_root_rule_sees_models_imported_as_a_submodule():
    """The most natural spelling of the forbidden import — the one the codebase
    itself writes, at `src/call_manager/db/__init__.py:7`.

    This one is caught by `node.module` alone, because the ban is on the PACKAGE and
    `node.module` here IS `call_manager.db`. It pins the spelling, not the alias
    join; `test_the_composition_root_rule_rejects_the_db_package_itself` is what
    proves that."""
    plain = "from call_manager.db import models\n"
    assert len(check_composition_root(plain, path="registry.py")) == 1
    aliased = "from call_manager.db import models as m\n"
    assert len(check_composition_root(aliased, path="registry.py")) == 1


def test_the_composition_root_rule_rejects_the_db_package_itself():
    """The ban is on the PACKAGE. `src/call_manager/db/__init__.py:7` re-exports
    models as a side effect its own comment explains, so importing the package hands
    you `db.models.Workspace` without naming the models module at all.

    The FIRST assertion is the only thing in this file that proves `_imported_names`:
    in `from call_manager import db` the package arrives as an ALIAS, so `node.module`
    is just `call_manager` and reading it alone misses this entirely. The second
    assertion is an `ast.Import`, where the dotted name is already on the alias."""
    assert len(check_composition_root("from call_manager import db\n", path="registry.py")) == 1
    assert len(check_composition_root("import call_manager.db\n", path="registry.py")) == 1


def test_the_composition_root_rule_rejects_a_counterfeit_type_checking_guard():
    """A guard only guards when the name resolves to `typing.TYPE_CHECKING`. A module
    that rebinds the name runs the body at import time — `TYPE_CHECKING = True`
    followed by `if TYPE_CHECKING: from sqlalchemy.orm import sessionmaker` binds a
    usable sessionmaker, which is the opposite of what the exemption grants."""
    rebound = (
        "TYPE_CHECKING = True\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.orm import sessionmaker\n"
    )
    assert len(check_composition_root(rebound, path="registry.py")) == 1
    borrowed = (
        "import types\n"
        "cfg = types.SimpleNamespace(TYPE_CHECKING=True)\n"
        "if cfg.TYPE_CHECKING:\n"
        "    import sqlalchemy\n"
    )
    assert len(check_composition_root(borrowed, path="registry.py")) == 1


def test_the_composition_root_rule_accepts_every_real_spelling_of_the_guard():
    """The rule must not cost the composition root a legitimate annotation import, in
    any of the three ways `typing.TYPE_CHECKING` is actually written."""
    for src in (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import sqlalchemy\n",
        "from typing import TYPE_CHECKING as TC\nif TC:\n    import sqlalchemy\n",
        "import typing\nif typing.TYPE_CHECKING:\n    import sqlalchemy\n",
    ):
        assert check_composition_root(src, path="registry.py") == []


def test_the_composition_root_rule_rejects_a_relative_import():
    """`from ...db import models` reaches the db package without ever spelling its
    dotted name, and source text alone cannot resolve a relative import. Neither
    skipped file has one today."""
    src = "from ...db import models\n"
    assert len(check_composition_root(src, path="registry.py")) == 1
