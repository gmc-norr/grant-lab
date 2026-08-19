"""The tool-code import boundary (spec 2026-08-18 revision 8, section 4).

    tools/ may import only a positive allowlist: the query, effect and accounting
    ports, their DTOs and commands, and the standard library. Not SQLAlchemy, not
    db.models, not Session, Engine or a session factory — and not any project
    module outside the allowlist.

Stated POSITIVELY on purpose. A forbidden-name list is not enough: a tool can
reach an effectful project helper that exposes no SQLAlchemy in the tool's own
module, and `editing.sectionizer` is a real example. Anything not on the list is
a violation, and adding to the list is a reviewed diff.

This is architectural hygiene for trusted code (section 4.2). It makes the wrong
thing hard to write and easy to spot in review, and makes coverage a CI check
rather than a memory. It is NOT adversarial containment — that would need a
process, OS or database-privilege boundary, and this design does not claim one.

Everything here works on SOURCE TEXT. Nothing is imported: a fixture that had to
be importable would drag its own violations into the test process, and could not
express a forbidden import at all.
"""

from __future__ import annotations

import ast
import pathlib
import sys

#: The one project package a tool may name. Submodules included.
PORTS_PREFIX = "call_manager.proposal_assist.ports"

#: Resource locators (section 4.1). Closing imports alone does not close semantic
#: authority: ToolContext exposes repo_dir, so
#:     (ctx.repo_dir / "proposal.md").write_text(new_text)
#: passes every import check. A tool holds names of operations, not paths.
#:
#: The rule is on the NAME wherever it is written — as an attribute, as a parameter,
#: or as a local binding. A tool that takes `repo_dir` as an argument and is wired
#: from registry.py holds exactly the same authority while writing no attribute at
#: all, and registry.py is permanently skipped by the gate.
LOCATOR_NAMES = frozenset({"repo_dir", "workspaces_dir", "session_factory", "settings"})


def _module_is_allowed(dotted: str) -> bool:
    """The entire allowlist: the standard library, plus the ports package.

    `sys.stdlib_module_names` is the interpreter's own answer to "is this
    stdlib", so the rule cannot drift from the Python version in use.
    """
    if dotted.split(".")[0] in sys.stdlib_module_names:
        return True
    return dotted == PORTS_PREFIX or dotted.startswith(PORTS_PREFIX + ".")


def check_source(source: str, *, path: str) -> list[str]:
    """Every import violation in *source*, one string each. Empty when clean.

    Walks the WHOLE tree, so a lazy import inside a function body and an import
    under `if TYPE_CHECKING:` are both checked. The first is how
    `tools/proposal.py` breaks a module cycle today; the second does not execute
    but still tells the next reader that reaching that module is normal.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_is_allowed(alias.name):
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import inside tools/ can only resolve to another tools/
                # module, and it carries no dotted name to test against the allowlist,
                # so it is refused outright rather than resolved. None exists today —
                # audit.py reaches a private helper in citations.py by ABSOLUTE
                # import, which the allowlist already rejects. This closes the
                # re-spelling, not a current hole.
                shown = "." * node.level + (node.module or "")
                violations.append(
                    f"{path}:{node.lineno}: relative import from {shown}"
                )
            elif node.module is not None and not _module_is_allowed(node.module):
                violations.append(f"{path}:{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Attribute) and node.attr in LOCATOR_NAMES:
            violations.append(f"{path}:{node.lineno}: names the resource locator {node.attr}")
        elif isinstance(node, ast.Name) and node.id in LOCATOR_NAMES:
            violations.append(f"{path}:{node.lineno}: names the resource locator {node.id}")
        elif isinstance(node, ast.arg) and node.arg in LOCATOR_NAMES:
            violations.append(f"{path}:{node.lineno}: names the resource locator {node.arg}")
    return violations


REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def check_file(path: pathlib.Path) -> list[str]:
    """Check one source file. Violations name the repo-relative path, so a failure
    names something a human can open."""
    shown = str(path.resolve().relative_to(REPO))
    return check_source(path.read_text(encoding="utf-8"), path=shown)


TOOLS_DIR = REPO / "src" / "call_manager" / "proposal_assist" / "tools"

#: The composition root may name project modules a tool may not, but it must not do
#: database work. The `call_manager.db` PACKAGE is forbidden outright — naming a model
#: is naming a table, and `db/__init__.py:7` re-exports `models`, so importing the
#: package alone hands you `db.models.Workspace`. SQLAlchemy is forbidden at RUNTIME
#: only: an import that exists solely to annotate a field cannot execute anything.
_DB_PACKAGE = "call_manager.db"


def tool_modules() -> list[pathlib.Path]:
    """Every `.py` file under `tools/`, sorted. No subpackage exists today; if one
    appears it is discovered, and `tool_key` gives it an identity no top-level
    exemption can match."""
    return sorted(p for p in TOOLS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def tool_key(path: pathlib.Path) -> str:
    """The identity an exemption or a skip must name: the path RELATIVE TO `tools/`,
    POSIX-style.

    Never `path.name`. A basename key lets a new `tools/sub/audit.py` inherit
    top-level `audit.py`'s exemption and a new `tools/sub/__init__.py` inherit the
    composition root's PERMANENT skip — while `check_composition_root`, which reads
    `TOOLS_DIR / key`, never sees the nested file at all. Two dirty modules would then
    sit inside `tools/` with the whole gate green. For a top-level module the key IS
    the basename, so both lists read exactly as before.
    """
    return path.resolve().relative_to(TOOLS_DIR.resolve()).as_posix()


def normalized_violation(violation: str) -> str:
    """`path:lineno: what` -> `what`.

    A pin records WHAT a dirty module still does, not WHERE it does it: moving a line
    is not new authority, so the pin must survive an unrelated edit. Lives here, next
    to the code that formats the string it parses.
    """
    _prefix, sep, rest = violation.partition(": ")
    assert sep, f"unparseable violation string: {violation!r}"
    return rest


def _typing_flag_names(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """The names in this module that really resolve to `typing.TYPE_CHECKING`.

    Returns (bare names, typing-module aliases): `from typing import TYPE_CHECKING as
    TC` contributes "TC" to the first, `import typing as t` contributes "t" to the
    second. A name the module ASSIGNS to anywhere is dropped from both — a module that
    writes `TYPE_CHECKING = True` runs the guarded body at import time, and a guard
    that runs is not a guard. Dropping on any assignment, even a local one, is
    deliberately conservative: it can cost a false failure, never a miss.
    """
    flags: set[str] = set()
    modules: set[str] = set()
    rebound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing" and not node.level:
            for alias in node.names:
                if alias.name == "TYPE_CHECKING":
                    flags.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    modules.add(alias.asname or "typing")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            rebound.add(node.id)
    return frozenset(flags - rebound), frozenset(modules - rebound)


def _type_checking_node_ids(tree: ast.AST) -> set[int]:
    """ids of every node inside an `if TYPE_CHECKING:` body, where TYPE_CHECKING is
    the real `typing.TYPE_CHECKING` and not a local name that merely spells it."""
    flags, modules = _typing_flag_names(tree)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        named = (isinstance(test, ast.Name) and test.id in flags) or (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id in modules
        )
        if named:
            for child in node.body:
                for inner in ast.walk(child):
                    guarded.add(id(inner))
    return guarded


def _imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Every dotted module path an import statement can bind.

    `from call_manager.db import models` binds the SUBMODULE `call_manager.db.models`,
    but `node.module` says only `call_manager.db` — so the aliases have to be joined
    onto the module, or the most natural spelling of the forbidden import is invisible.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]


def check_composition_root(source: str, *, path: str) -> list[str]:
    """The narrow rule for GATE_SKIPS. Not the tool rule — these files legitimately
    name every tool module in order to wire them.

    One violation per import statement: the first hit wins, so a statement reaching the
    db package is reported once whether it is spelled as the package, as the models
    module, or as a name inside it.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=path)
    guarded = _type_checking_node_ids(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and (node.level or node.module is None):
            # Source text alone cannot resolve a relative import, and
            # `from ...db import models` would reach the db package without ever
            # spelling its dotted name. Neither skipped file has one today.
            shown = "." * node.level + (node.module or "")
            violations.append(f"{path}:{node.lineno}: relative import from {shown}")
            continue
        names = _imported_names(node)
        db = [n for n in names if n == _DB_PACKAGE or n.startswith(_DB_PACKAGE + ".")]
        if db:
            violations.append(f"{path}:{node.lineno}: imports {db[0]}")
            continue
        sqla = [n for n in names if n.split(".")[0] == "sqlalchemy"]
        if sqla and id(node) not in guarded:
            violations.append(f"{path}:{node.lineno}: imports {sqla[0]} outside TYPE_CHECKING")
    return violations
