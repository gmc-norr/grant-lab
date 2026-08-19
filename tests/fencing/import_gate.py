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
    return violations
