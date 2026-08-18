"""Fence F2 — AST detectors for CANDIDATE effect sites.

Reads source as TEXT and imports nothing from `src/`. A gate that imports the
code it watches can be disarmed by the same refactor it exists to catch, and it
could not be unit-tested against synthetic fixtures.

These find CANDIDATES. Whether one is a semantic effect (must fence), accounting,
or covered by an enclosing critical section is DECLARED in fence_inventory.json:
`.commit()` is not the same unit as "semantic effect" (Global Constraint 5).

Site identity is (kind, path, qualified_symbol) — `outer.inner` / `Class.method`.
references.py has two committing helpers nested inside fetch_reference_impl, and
bare names would collapse same-named helpers across different outer functions.
"""

from __future__ import annotations

import ast
from typing import NamedTuple

KIND_TOOL_COMMIT = "tool_commit"
KIND_CLAIM_FN = "claim_fn"
KIND_TOOL_CONTEXT = "tool_context"

_TOOLS_DIR = "/proposal_assist/tools/"
_PROPOSAL_ASSIST = "/proposal_assist/"
_CLAIM_PREFIXES = ("claim_next_", "claim_queued_")

SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class Site(NamedTuple):
    kind: str
    path: str
    symbol: str


def call_name(node: ast.Call) -> str | None:
    """The bare callee name of a Call: `f()` -> "f", `a.b.f()` -> "f"."""
    f = node.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)


def qualified_defs(tree: ast.AST) -> dict[str, ast.AST]:
    """Map dotted qualified name -> def/class node.

    The scope stack pushes ONLY on def/class over a full recursive walk, so a def
    nested inside a `with`/`if`/`try` still qualifies as `outer.helper`. Detector
    and verifier MUST resolve symbols identically or the gate reports phantom
    "symbol not found" for sites that exist.
    """
    found: dict[str, ast.AST] = {}

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPED):
                qual = stack + [child.name]
                found[".".join(qual)] = child
                walk(child, qual)
            else:
                walk(child, stack)

    walk(tree, [])
    return found


def find_sites(source: str, path: str) -> set[Site]:
    """Every candidate belief-carrying effect site in *source*. Pure."""
    norm = path.replace("\\", "/")
    tree = ast.parse(source)
    sites: set[Site] = set()

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, SCOPED):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and _PROPOSAL_ASSIST in norm
                and node.name.startswith(_CLAIM_PREFIXES)
            ):
                sites.add(Site(KIND_CLAIM_FN, path, ".".join(stack + [node.name])))
            stack = stack + [node.name]

        elif isinstance(node, ast.Call):
            qual = ".".join(stack) if stack else "<module>"
            if (
                _TOOLS_DIR in norm
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit"
            ):
                sites.add(Site(KIND_TOOL_COMMIT, path, qual))

            if call_name(node) == "ToolContext":
                inert = any(
                    kw.arg == "session_factory"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is None
                    for kw in node.keywords
                )
                if not inert:
                    sites.add(Site(KIND_TOOL_CONTEXT, path, qual))

        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return sites
