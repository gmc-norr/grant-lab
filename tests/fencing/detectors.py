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

from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects

KIND_TOOL_COMMIT = "tool_commit"
KIND_CLAIM_FN = "claim_fn"
KIND_TOOL_CONTEXT = "tool_context"
KIND_GIT_COMMIT = "git_commit"

_TOOLS_DIR = "/proposal_assist/tools/"
_PROPOSAL_ASSIST = "/proposal_assist/"
_CLAIM_PREFIXES = ("claim_next_", "claim_queued_")

#: Modules OUTSIDE tools/ that hold effects relocated out of it.
#:
#: An effect does not stop being a tool effect because its code moved — "the audit
#: unit is the EFFECT, not the mutex site". Without this, relocating a site deletes
#: its inventory row (merge() rebuilds only from the scan) and RENAMES cannot carry
#: it, because merge() validates every rename target against found_ids.
#:
#: DECLARED, never inferred. Widening to all of proposal_assist/ would record every
#: domain-layer commit as an UNCLASSIFIED site and the gate would stop being read.
#: A path enters this set in the SAME slice that moves an effect into it.
RELOCATED_EFFECT_PATHS: frozenset[str] = frozenset({
    "src/call_manager/proposal_assist/citations/core.py",  # F3d3a
})


def _in_effect_scope(norm: str) -> bool:
    """Is this path one where a tool effect may live? tools/, or a declared
    relocation target."""
    return _TOOLS_DIR in norm or norm in RELOCATED_EFFECT_PATHS

#: Every WRITE a converted tool can perform through the ports, read off the
#: Protocols so the list cannot drift away from them. Queries are excluded on
#: purpose: this design does not fence reads (spec §8), and a read is not a site.
PORT_WRITE_METHODS: frozenset[str] = frozenset(Effects.__protocol_attrs__) | frozenset(
    Accounting.__protocol_attrs__
)

#: The two bundle attributes a write is reached through.
_PORT_BUNDLE_ATTRS = frozenset({"effects", "accounting"})

#: What counts as committing WITHOUT a port, for the fenced measurement below.
#: `.commit()` is the obvious form; `.begin()` is the quiet one — a
#: `with session_factory.begin():` block commits on exit with no `.commit()` node
#: anywhere, so a partial conversion written that way would measure FENCED.
#: Verified: adding "begin" changes nothing today (zero `.begin()` calls under
#: tools/), and removing it makes a `.begin()`-based half-conversion measure
#: fenced. Fail-closed on purpose — the slice most exposed is F3e's ingest, and
#: its plan is not written yet.
_RAW_COMMIT_ATTRS = frozenset({"commit", "begin"})

#: The name the bundle arrives under. _safe_tool passes it as the keyword `ports`
#: (F3d1), so the canonical converted call is exactly `ports.effects.X(...)` or
#: `ports.accounting.X(...)`. Matching the METHOD NAME alone would record any local
#: helper called `submit_review`; matching `<anything>.effects.X` would let a test
#: double mint a row and mark a qualname fenced that never held a capability.
_PORT_ROOT = "ports"

SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class Site(NamedTuple):
    kind: str
    path: str
    symbol: str


def call_name(node: ast.Call) -> str | None:
    """The bare callee name of a Call: `f()` -> "f", `a.b.f()` -> "f"."""
    f = node.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)


def _is_port_write(node: ast.Call) -> bool:
    """`ports.effects.X(...)` or `ports.accounting.X(...)`, and nothing else.

    All three parts are load-bearing: the method must be a real port write, the
    bundle attribute must be `effects` or `accounting`, and the ROOT must be the
    injected `ports` name.
    """
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in PORT_WRITE_METHODS
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr in _PORT_BUNDLE_ATTRS
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == _PORT_ROOT
    )


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
            # _RAW_COMMIT_ATTRS, not a bare "commit": `with session.begin():`
            # commits on exit with no .commit() node anywhere. F3d2 taught
            # find_fenced_tool_commits that, but left discovery here blind, so a
            # begin-only effect owned NO inventory row at all — it was never
            # discovered, so nothing could notice it going missing. Discovery and
            # measurement must agree on what a raw commit is or the gap is silent.
            if (
                _in_effect_scope(norm)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_COMMIT_ATTRS
            ):
                sites.add(Site(KIND_TOOL_COMMIT, path, qual))

            # F3d2: the converted form. A tool that has been moved onto the ports
            # has no .commit() at all, so without this the row vanishes the moment
            # the site is converted — and merge() rebuilds only from the scan.
            if _in_effect_scope(norm) and _is_port_write(node):
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


def find_fenced_tool_commits(source: str, path: str) -> set[tuple[str, str, str]]:
    """`(kind, path, symbol)` for every qualname IN EFFECT SCOPE that writes ONLY through
    a port.

    Port call AND no raw commit. A qualname holding both is half-converted, and
    half-converted is not converted — a slice must not be able to report itself
    complete over a site that still commits directly.

    Effect scope is `tools/` plus the declared RELOCATED_EFFECT_PATHS (F3d3a), not
    `tools/` alone. Returns an empty set outside it — same scoping as find_sites,
    and the two must stay identical: a site discovered but never measured, or
    measured but never discovered, is a hole nothing reports.
    """
    norm = path.replace("\\", "/")
    if not _in_effect_scope(norm):
        return set()

    tree = ast.parse(source)
    port_calls: set[str] = set()
    raw_commits: set[str] = set()

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, SCOPED):
            stack = stack + [node.name]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            qual = ".".join(stack) if stack else "<module>"
            if node.func.attr in _RAW_COMMIT_ATTRS:
                raw_commits.add(qual)
            elif _is_port_write(node):
                port_calls.add(qual)
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return {(KIND_TOOL_COMMIT, path, q) for q in port_calls - raw_commits}
