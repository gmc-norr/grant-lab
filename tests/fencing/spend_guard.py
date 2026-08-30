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


# The modules a real spend writer can come from. A call only counts if its name
# was BOUND by an import from one of these — see _spend_writer_names.
WRITER_MODULES = frozenset(
    {
        "call_manager.proposal_assist.spend",
        "call_manager.proposal_assist.citations.usage",
    }
)


def _spend_writer_names(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve which names in this module actually mean "record spend".

    Returns (bare_names, module_aliases): a Call counts if it is `name(...)` with
    name in bare_names, or `alias.record_spend(...)` with alias in module_aliases.

    Accepting the bare string `record_spend` was a real bypass, measured against
    the shipped guard: a module that defined its own local `def record_spend`, or
    called an unrelated `something.record_spend()`, blessed every token stamp in
    it and the gate stayed green. The name must be BOUND BY AN IMPORT from a
    module in WRITER_MODULES. Honouring `as` aliases is the other direction —
    `from ...spend import record_spend as rs` was a false positive on correct
    code before it was handled.
    """
    names: set[str] = set()
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in WRITER_MODULES:
                for alias in node.names:
                    if alias.name in SPEND_WRITERS:
                        names.add(alias.asname or alias.name)
            # `from call_manager.proposal_assist import spend` -> `spend.record_spend`
            for alias in node.names:
                if f"{mod}.{alias.name}" in WRITER_MODULES:
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c as x` binds x; plain `import a.b.c` binds `a`,
                # which we cannot match on attribute shape, so require asname.
                if alias.name in WRITER_MODULES and alias.asname:
                    module_aliases.add(alias.asname)
    return frozenset(names), frozenset(module_aliases)


def _target_stamps(target: ast.AST) -> bool:
    """True if this assignment target writes a token field.

    Recursive because a target can NEST: `(job.input_tokens, job.output_tokens)
    = pair` is a Tuple of Attributes, and `[a.tokens_in] = xs` is a List. An
    earlier version matched only a bare `ast.Attribute`, so all three of
    tuple-unpack, annotated and augmented assignment slipped through the gate —
    measured, not theorised. `+=` matters most: accumulating a token total is a
    natural way to write exactly the code this gate exists to catch.
    """
    if isinstance(target, ast.Attribute):
        return target.attr in TOKEN_FIELDS
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_stamps(el) for el in target.elts)
    if isinstance(target, ast.Starred):
        return _target_stamps(target.value)
    return False


def _stamp_lines(nodes: list[ast.AST]) -> list[int]:
    lines: list[int] = []
    for node in nodes:
        # Assign has a LIST of targets (`a = b = value`); AnnAssign and AugAssign
        # each have exactly one. All three write the attribute.
        if isinstance(node, ast.Assign):
            targets: list[ast.AST] = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(_target_stamps(t) for t in targets):
            lines.append(node.lineno)
    return sorted(lines)


def _records_spend(nodes: list[ast.AST], writers: tuple[frozenset[str], frozenset[str]]) -> bool:
    bare, module_aliases = writers
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in bare:
                return True
            # Only `<imported module alias>.record_spend(...)`. An arbitrary
            # attribute call that merely ENDS in record_spend does not count.
            if (
                isinstance(func, ast.Attribute)
                and func.attr in SPEND_WRITERS
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            ):
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
        # Resolve how THIS module refers to the class. Matching only
        # ast.Name("LlmSpend") missed `models.LlmSpend(...)` and
        # `from ...models import LlmSpend as SpendRow` — both measured against the
        # shipped guard, both silently absent from the "exactly one writer" claim.
        local_names: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    if mod == "call_manager.db.models" and alias.name == "LlmSpend":
                        local_names.add(alias.asname or alias.name)
                    elif f"{mod}.{alias.name}" == "call_manager.db.models":
                        module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "call_manager.db.models" and alias.asname:
                        module_aliases.add(alias.asname)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = (isinstance(func, ast.Name) and func.id in local_names) or (
                isinstance(func, ast.Attribute)
                and func.attr == "LlmSpend"
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            )
            if hit:
                out.append(str(path.relative_to(SRC)))
                break
    return sorted(out)
