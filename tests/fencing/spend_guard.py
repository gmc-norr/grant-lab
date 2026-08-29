"""Any FUNCTION that stamps a token field must also record spend.

The defect this prevents: `job.input_tokens = done.tokens_in or 0` written on the
happy path and nowhere else. Six sites had it, all losing spend in the same
direction. The per-job columns are a display cache now, so writing one without a
matching `record_spend` call re-opens exactly the gap the ledger closed.

FUNCTION-level, modelled on `tests/fencing/sink_guard.py`'s call-level pins: a
function either records spend in its OWN body, or its `(path, qualname)` is
pinned here with a reason. The pinned list can only shrink.

Module scope would not work. `turns/outcomes.py` has two independent terminal
writers and `evaluator/pass_runner.py` has a success path and a `_fail`; under a
per-file rule one `record_spend` anywhere in the file — in a dead branch, in an
unrelated helper, on the other terminal path — exempts every token assignment in
it, permanently.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# Attribute names that mean "I am writing token telemetry to a row".
TOKEN_FIELDS = frozenset({"input_tokens", "output_tokens", "tokens_in", "tokens_out"})

# Names that mean "I am recording spend", before import aliasing.
SPEND_WRITERS = frozenset({"record_spend", "record_usage"})

# Functions that stamp a token field and legitimately do NOT record spend.
# Keyed by (path relative to src/, dotted qualname). A reason is MANDATORY.
# Adding to this list is a reviewed diff.
#
# Keyed by FUNCTION, not by module, on purpose: a module-level pin would bless
# every sibling function in the same file forever.
PINNED: dict[tuple[str, str], str] = {
    (
        "call_manager/proposal_assist/review/jobs.py",
        "rollup_run_totals",
    ): (
        "pure re-derivation of run totals from items that each already recorded "
        "their own ledger row; it has three call sites (pass_runner.py:174, "
        "pass_runner.py:364, jobs.py:184) and a reaped-then-late-finalized run "
        "reaches it twice, so an append-only row here would double-count"
    ),
}


def _collect_functions(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    prefix: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return all function defs under tree with their dotted qualname.

    Copied from tests/fencing/sink_guard.py:292 — same traversal, same qualnames.
    """
    results: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.append((qname, stmt))
            results.extend(_collect_functions(stmt, qname))
        elif isinstance(stmt, ast.ClassDef):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.extend(_collect_functions(stmt, qname))
        else:
            for child in ast.walk(stmt):
                if child is stmt:
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.append((qname, child))
                    results.extend(_collect_functions(child, qname))
                elif isinstance(child, ast.ClassDef):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.extend(_collect_functions(child, qname))
    return results


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes belonging to `func` itself, excluding nested def/class BODIES.

    Nested functions are collected separately by _collect_functions and judged in
    their own right, so a nested closure that records must not bless its parent.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _spend_writer_names(tree: ast.AST) -> frozenset[str]:
    """Local names that refer to a spend writer, honouring `as` aliases.

    Without this, `from ...spend import record_spend as rs` is a false POSITIVE
    on correct code — reproduced before this was added.
    """
    names = set(SPEND_WRITERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in SPEND_WRITERS and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


def _stamp_lines(nodes: list[ast.AST]) -> list[int]:
    lines: list[int] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in TOKEN_FIELDS:
                    lines.append(node.lineno)
    return sorted(lines)


def _records_spend(nodes: list[ast.AST], writer_names: frozenset[str]) -> bool:
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in writer_names:
                return True
    return False


def offenders() -> list[str]:
    """`path::qualname` for every function that stamps, records nothing, is unpinned."""
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        writer_names = _spend_writer_names(tree)
        for qname, func in _collect_functions(tree, ""):
            if (rel, qname) in PINNED:
                continue
            nodes = _own_nodes(func)
            if _stamp_lines(nodes) and not _records_spend(nodes, writer_names):
                out.append(f"{rel}::{qname}")
    return sorted(out)


def stale_pins() -> list[str]:
    """Pins whose function no longer exists, or no longer stamps a token field."""
    stale: list[str] = []
    for rel, qname in PINNED:
        path = SRC / rel
        if not path.exists():
            stale.append(f"{rel}::{qname}")
            continue
        tree = ast.parse(path.read_text())
        funcs = dict(_collect_functions(tree, ""))
        func = funcs.get(qname)
        if func is None or not _stamp_lines(_own_nodes(func)):
            stale.append(f"{rel}::{qname}")
    return sorted(stale)


def llm_spend_constructors() -> list[str]:
    """Modules under src/ that CONSTRUCT an `LlmSpend` row.

    The plan's central claim is "exactly one writer". Nothing checked it: the
    stamps/records rule above would be satisfied by a second module that both
    constructed a row and called record_spend. This is the claim, asserted.
    """
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LlmSpend"
            ):
                out.append(str(path.relative_to(SRC)))
                break
    return sorted(out)
