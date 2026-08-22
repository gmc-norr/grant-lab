"""Fence F3e1a — conservative git-commit-fence predicate.

CONSERVATIVE BY DESIGN: it under-reports. A shape it cannot prove fenced measures
unfenced, including shapes that are perfectly correct. That direction is the safe
one — a false ``fenced: true`` lets a slice claim a conversion it did not make, while
a false ``false`` costs a pin and a sentence.

**The rule** (all three clauses are required):

1. ``acquire_commit_mutex_at_generation`` appears as a *standalone expression statement
   directly in that function's body* — an ``ast.Expr`` whose value is the call, and
   whose parent statement list is the function body itself (NOT inside ``if``, ``try``,
   ``for``, ``while``, or ``with``).
2. It appears *before every statement containing a sink call* — compared by statement
   index in the same top-level block, not by AST preorder.
3. NO ``acquire_commit_mutex_live`` appears anywhere in the function.

Everything else is **unfenced**.

**Callee-name matching**: both acquisitions and sinks are matched by their *final
callee name* — the ``.attr`` of an ``ast.Attribute``, or the ``id`` of an ``ast.Name``.
So ``mod.write_and_commit_files(...)`` matches ``write_and_commit_files``. This is a
deliberate robustness choice for aliases; every acquisition and sink in this repo is
module-level with no ambiguous same-named peer method.

**Nested scope attribution**: a sink inside a nested ``def`` / ``class`` belongs to
that inner qualname ALONE, no matter how deeply or indirectly it is nested.
``_collect_functions`` recurses into defs at any depth (including defs nested inside
``if``/``try``/``with``/``for`` blocks), building the qualname from the enclosing
function/class names only (control-flow blocks are NOT scope boundaries).
``sink_calls`` uses ``_iter_own_nodes`` for each body statement, which descends into
control-flow children but stops at nested ``def``/``class`` boundaries — so a sink
inside a branch-nested ``def`` is NOT inherited by the outer function.

**``stmt_index`` semantics**: set only when the sink is a *bare top-level expression
statement* (``ast.Expr`` whose value IS the sink call directly in the function body).
It is ``None`` for a captured sink (``sha = write_and_commit_files(...)``), a sink
nested in a branch / ``with`` / argument, or a sink inside a nested def. ``None``
means "position unknown to the cheap check" → unprovable → unfenced.

All nine real sites capture or nest their sink, so all nine measure ``fenced: false``
through this checker regardless of acquisition shape. ``fenced: false`` does NOT mean
broken — it means not provable by the cheap check. See Task-5 brief §"What this costs".
"""

from __future__ import annotations

import ast
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Canonical sink declaration
# ---------------------------------------------------------------------------

#: The ONLY two ways this repository makes a git commit, declared as EXACT
#: identities. Verified by sweeping every "commit" string argument in src/.
GIT_SINKS: tuple[tuple[str, str, str], ...] = (
    ("proposal_assist/citations/gitio.py", "write_and_commit_files", "always"),
    ("proposal_assist/phases/setup.py", "_run_git", "argv"),  # only argv[0] == "commit"
)

_ACQ_AT_GEN = "acquire_commit_mutex_at_generation"
_ACQ_LIVE = "acquire_commit_mutex_live"

# Names of all sink functions (canonical, final name component).
_SINK_NAMES: frozenset[str] = frozenset(name for _, name, _ in GIT_SINKS)

# Map canonical sink name → kind
_SINK_KIND: dict[str, str] = {name: kind for _, name, kind in GIT_SINKS}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _callee_name(node: ast.expr) -> str | None:
    """Return the final callee name of a Call node's func, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_commit_call(canonical: str, node: ast.Call) -> bool:
    """True iff this Call is a committed git-sink invocation for *canonical*."""
    kind = _SINK_KIND.get(canonical)
    if kind == "always":
        return True
    if kind == "argv":
        # _run_git(["commit", ...]) — first positional argument must be a list
        # whose first element is the string "commit".
        if node.args:
            first_arg = node.args[0]
            if (
                isinstance(first_arg, ast.List)
                and first_arg.elts
                and isinstance(first_arg.elts[0], ast.Constant)
                and first_arg.elts[0].value == "commit"
            ):
                return True
        return False
    return False


def _sink_bindings(tree: ast.AST) -> dict[str, str]:
    """Return a map of local-name → canonical-sink-name for any alias assignments.

    Handles patterns like ``wac = write_and_commit_files`` so that a later call
    to ``wac(...)`` still matches the canonical sink identity and the argv rule
    applies correctly.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and (
            isinstance(node.value, ast.Name) and node.value.id in _SINK_NAMES
        ):
            # simple_value = canonical_sink
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value.id
    # The canonical names themselves map to themselves.
    for name in _SINK_NAMES:
        bindings[name] = name
    return bindings


def _iter_own_nodes(stmt: ast.stmt):
    """Walk all AST nodes within *stmt* WITHOUT crossing into nested function/class scopes.

    Nested FunctionDef / AsyncFunctionDef / ClassDef nodes are yielded but their
    bodies are NOT descended into — they are separate qualnames.
    """
    yield stmt
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Yield the scope node itself (so callers see it) but do NOT recurse.
            yield child
        else:
            yield from _iter_own_nodes(child)


def _collect_functions(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    prefix: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return all function defs in *tree*, with their dotted qualname under *prefix*.

    Recurses into defs at ANY depth, including defs nested inside ``if``/``try``/
    ``with``/``for`` blocks.  Control-flow blocks are NOT scope boundaries — only
    ``def``/``class`` nodes introduce a new qualname segment.
    """
    results = []
    body = getattr(tree, "body", [])
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.append((qname, stmt))
            results.extend(_collect_functions(stmt, qname))
        elif isinstance(stmt, ast.ClassDef):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.extend(_collect_functions(stmt, qname))
        else:
            # Control-flow node (if/try/for/while/with/match/…): look for nested
            # function/class defs WITHOUT introducing a new qualname segment.
            # Use _iter_own_nodes to walk into the control-flow children but stop
            # at nested def/class boundaries — then handle those boundaries here.
            for child in _iter_own_nodes(stmt):
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class _SinkEntry(NamedTuple):
    qualname: str
    node: ast.Call
    stmt_index: int | None


def sink_calls(source: str, path: str) -> list[tuple[str, ast.Call, int | None]]:
    """Return ``[(qualname, call_node, stmt_index)]`` for every git-sink call.

    ``stmt_index`` is the index of the function-body top-level statement that
    contains the call.  It is set ONLY when the sink is a **bare top-level
    expression statement** (``ast.Expr`` whose value IS the sink call) — i.e.
    the sink is directly in the function body as a standalone call, not captured,
    not nested in a branch / ``with`` / argument, and not inside a nested def.
    ``None`` means position unknown → unprovable → unfenced.

    *path* is used to filter which GIT_SINKS apply (matched by suffix).  When no
    suffix matches, all sink names are eligible (conservative: may over-count but
    never misses).

    A sink inside a nested ``def`` / ``class`` belongs to THAT inner qualname
    alone — the outer function is NOT credited.
    """
    tree = ast.parse(source)
    bindings = _sink_bindings(tree)

    # Determine which sink names are relevant for this file.
    relevant: set[str] = set()
    for suffix, name, _ in GIT_SINKS:
        if path.endswith(suffix):
            relevant.add(name)
    if not relevant:
        relevant = set(_SINK_NAMES)

    results: list[tuple[str, ast.Call, int | None]] = []

    for qualname, func in _collect_functions(tree, ""):
        body = func.body
        for stmt_idx, stmt in enumerate(body):
            # Skip nested scopes — they are separate qualnames, analysed on their own pass.
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue

            # Check if this statement IS a bare top-level sink expression.
            is_bare_top_level = (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and (callee := _callee_name(stmt.value.func)) is not None
                and bindings.get(callee) in relevant
                and _is_commit_call(bindings[callee], stmt.value)
            )

            if is_bare_top_level:
                results.append((qualname, stmt.value, stmt_idx))
            else:
                # Walk the statement WITHOUT crossing into nested function/class scopes
                # — use _iter_own_nodes so a sink inside a branch-nested def is NOT
                # attributed to the outer function (it will be attributed on that inner
                # def's own pass through _collect_functions).
                for node in _iter_own_nodes(stmt):
                    if node is stmt:
                        continue
                    if not isinstance(node, ast.Call):
                        continue
                    callee = _callee_name(node.func)
                    if callee is None:
                        continue
                    canonical = bindings.get(callee)
                    if canonical not in relevant:
                        continue
                    if not _is_commit_call(canonical, node):
                        continue
                    # This sink is nested — stmt_index is None (position unknown).
                    results.append((qualname, node, None))

    return results


def unprotected_sinks(source: str, path: str) -> dict[str, list[str]]:
    """Return ``{qualname: [reason, ...]}`` for every qualname holding a sink call
    that is NOT fenced by the conservative predicate.

    A qualname is fenced iff ALL of:
    - ``acquire_commit_mutex_at_generation`` appears as a standalone ``ast.Expr``
      directly in the function body (not inside if/try/for/while/with), AND
    - it appears before every statement containing a sink call (by stmt_index), AND
    - no ``acquire_commit_mutex_live`` appears anywhere in the function.

    Everything else is unfenced, including correct shapes this checker cannot prove.
    """
    tree = ast.parse(source)
    calls = sink_calls(source, path)
    if not calls:
        return {}

    # Group sink entries by qualname.
    by_qualname: dict[str, list[tuple[ast.Call, int | None]]] = {}
    for qualname, node, stmt_idx in calls:
        by_qualname.setdefault(qualname, []).append((node, stmt_idx))

    # Build a map of qualname → function AST for inspection.
    func_map: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for qualname, func in _collect_functions(tree, ""):
        func_map[qualname] = func

    result: dict[str, list[str]] = {}

    for qualname, sink_entries in by_qualname.items():
        func = func_map.get(qualname)
        if func is None:
            result[qualname] = ["function AST not found"]
            continue

        body = func.body
        reasons: list[str] = []

        # --- Find top-level acquire_commit_mutex_at_generation statements ---
        acq_at_gen_indices: list[int] = []
        for stmt_idx, stmt in enumerate(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and _callee_name(stmt.value.func) == _ACQ_AT_GEN
            ):
                acq_at_gen_indices.append(stmt_idx)

        if not acq_at_gen_indices:
            reasons.append(
                f"no top-level acquire_commit_mutex_at_generation in {qualname}"
            )

        # --- Check for acquire_commit_mutex_live anywhere in the function ---
        has_live = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and _callee_name(node.func) == _ACQ_LIVE
            ):
                has_live = True
                break

        if has_live:
            reasons.append(
                f"acquire_commit_mutex_live present in {qualname} "
                f"(disqualifies even when acquire_commit_mutex_at_generation also present)"
            )

        # --- Check order: every sink stmt_index must be AFTER the acquisition ---
        if acq_at_gen_indices and not has_live:
            first_acq = min(acq_at_gen_indices)
            for _node, stmt_idx in sink_entries:
                if stmt_idx is None:
                    # Position unknown — cannot prove order.
                    reasons.append(
                        f"sink in {qualname} has unknown position (captured or nested) "
                        f"— order unprovable by cheap check"
                    )
                elif stmt_idx <= first_acq:
                    reasons.append(
                        f"sink at stmt_index {stmt_idx} is not after acquisition "
                        f"at stmt_index {first_acq} in {qualname}"
                    )

        if reasons:
            result[qualname] = reasons

    return result


def fenced_qualnames(source: str, path: str) -> set[str]:
    """Return the set of qualnames whose git-sink calls are provably fenced.

    Defined as ``holders - set(unprotected_sinks(...))``, so the two halves
    partition every sink-holding qualname with no overlap and no gap.
    """
    calls = sink_calls(source, path)
    holders = {q for q, _n, _i in calls}
    unprotected = set(unprotected_sinks(source, path))
    return holders - unprotected
