"""Fence F2 — canonical-form verification.

Verification is CONTAINMENT, not analysis (Global Constraint 6). Revisions 1 and
2 tried to prove arbitrary code was fenced and each accepted a new class of
invalid shape — a detached pre-check, a post-commit mutex, a second unguarded
write, a fence in a nested scope, a reassigned statement variable, a bare ORM
add. Proving arbitrary Python is fenced is not decidable in a maintainable form.

So a fenced site must be WRITTEN in the canonical form instead:

    with fenced_write(session, ws, generation=gen):   # mutex form
        ...
    fenced_execute(session, stmt, workspace_id=ws, generation=gen)   # predicate form

and the rule is: every effect node is inside one, and none sits outside. Same
principle as proposal_assist/web/_writes.py — "exists so the rule cannot be
written wrongly".

F3 creates the two constructs. This module knows their names only.
"""

from __future__ import annotations

import ast

from tests.fencing.detectors import (
    KIND_CLAIM_FN,
    KIND_TOOL_COMMIT,
    KIND_TOOL_CONTEXT,
    SCOPED,
    call_name,
    qualified_defs,
)

FENCED_WRITE = "fenced_write"
FENCED_EXECUTE = "fenced_execute"
GENERATION_KW = "lifecycle_generation"
CLAIM_COLUMN = "claimed_at_generation"

#: Produces no semantic result. Spec 2 permits these on a stale generation and
#: spec 7 REQUIRES them to still run — demanding a fence would push F3 into
#: stranding jobs and losing token accounting.
NO_FENCE_REQUIRED = frozenset({"accounting"})

_WRITE_CTORS = frozenset({"update", "insert", "delete"})


def _own_nodes(scope: ast.AST):
    """Every node in *scope*, NOT descending into nested defs/classes.

    A nested def is its own site with its own inventory entry, so a fence inside
    one does not cover its parent and vice versa.
    """
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, SCOPED):
            continue
        yield child
        yield from _own_nodes(child)


def _is_effect(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = call_name(node)
    if name in {"commit", "add"}:
        return True
    if name == "execute" and node.args:
        return any(
            isinstance(n, ast.Call) and call_name(n) in _WRITE_CTORS
            for n in ast.walk(node.args[0])
        )
    return False


def _fenced_regions(scope: ast.AST) -> list[ast.AST]:
    """`with fenced_write(...)` statements in *scope*'s own code."""
    out = []
    for node in _own_nodes(scope):
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            isinstance(item.context_expr, ast.Call)
            and call_name(item.context_expr) == FENCED_WRITE
            for item in node.items
        ):
            out.append(node)
    return out


def _covered_ids(scope: ast.AST) -> set[int]:
    """id()s of every node inside a fenced region, plus fenced_execute calls."""
    covered: set[int] = set()
    for region in _fenced_regions(scope):
        for n in ast.walk(region):
            covered.add(id(n))
    for node in _own_nodes(scope):
        if isinstance(node, ast.Call) and call_name(node) == FENCED_EXECUTE:
            covered.add(id(node))
    return covered


def _verify_semantic_commit(scope: ast.AST) -> list[str]:
    covered = _covered_ids(scope)
    effects = [n for n in _own_nodes(scope) if _is_effect(n)]
    fenced_execs = [
        n for n in _own_nodes(scope)
        if isinstance(n, ast.Call) and call_name(n) == FENCED_EXECUTE
    ]
    if not effects and not fenced_execs:
        return [
            "no effect found — a semantic site that writes nothing is a "
            "misclassification, not a pass"
        ]
    bare = [n for n in effects if id(n) not in covered]
    if bare:
        names = sorted({call_name(n) or "?" for n in bare})
        return [
            f"{len(bare)} effect(s) outside the canonical form: {names}. Put them "
            f"inside `with {FENCED_WRITE}(...)` or use `{FENCED_EXECUTE}(...)`"
        ]
    return []


def _verify_caller_fenced(row: dict, defs: dict[str, ast.AST]) -> list[str]:
    ancestor_name = row.get("fenced_by")
    if not ancestor_name:
        return ["caller_fenced but no `fenced_by` declared"]
    ancestor = defs.get(ancestor_name)
    if ancestor is None:
        return [f"`fenced_by` {ancestor_name!r} does not resolve in this file"]
    own_name = row["symbol"].rsplit(".", 1)[-1]
    for region in _fenced_regions(ancestor):
        for n in ast.walk(region):
            if isinstance(n, ast.Call) and call_name(n) == own_name:
                return []
    return [
        f"{ancestor_name} does not call {own_name} inside a "
        f"`with {FENCED_WRITE}(...)` region"
    ]


def _verify_tool_context(row: dict, scope: ast.AST) -> list[str]:
    lifetime = row.get("ctx_lifetime")
    if lifetime == "retained":
        # Built during launch, which deliberately precedes the claim
        # (turns/processor.py:159) — the generation does not exist yet.
        # Requiring one here would bless a placeholder. F3's runtime two-turn
        # test is authoritative for this site.
        return []
    if lifetime != "one_shot":
        return [f"ctx_lifetime must be one_shot or retained, got {lifetime!r}"]
    for node in _own_nodes(scope):
        if isinstance(node, ast.Call) and call_name(node) == "ToolContext":
            kw = next((k for k in node.keywords if k.arg == GENERATION_KW), None)
            if kw is None:
                return [f"ToolContext(...) does not pass {GENERATION_KW}="]
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return [f"ToolContext(...) passes {GENERATION_KW}=None"]
            return []
    return ["no ToolContext(...) construction found"]


def _verify_claim_fn(scope: ast.AST) -> list[str]:
    """WRITE-presence, in this scope only.

    A read, a bare string, or a write inside a nested def does not count.

    Stated boundary: atomicity at claim time comes from the existing `FOR UPDATE`
    row lock inside one transaction, not from statement form — the ORM idiom sets
    the column in its own statement and is still atomic. Claiming to verify
    atomicity statically would be an over-claim.
    """
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == CLAIM_COLUMN:
                    return []
        if isinstance(node, ast.keyword) and node.arg == CLAIM_COLUMN:
            return []
    return [f"the claim does not WRITE {CLAIM_COLUMN} in its own scope"]


def verify(row: dict, source: str) -> list[str]:
    """Reasons this site is not properly fenced. Empty list = it is."""
    kind, category = row["kind"], row["category"]
    if kind == KIND_TOOL_COMMIT and category in NO_FENCE_REQUIRED:
        return []

    tree = ast.parse(source)
    defs = qualified_defs(tree)
    scope = tree if row["symbol"] == "<module>" else defs.get(row["symbol"])
    if scope is None:
        return [f"symbol {row['symbol']!r} not found"]

    if kind == KIND_TOOL_COMMIT:
        if category == "caller_fenced":
            return _verify_caller_fenced(row, defs)
        return _verify_semantic_commit(scope)
    if kind == KIND_TOOL_CONTEXT:
        return _verify_tool_context(row, scope)
    if kind == KIND_CLAIM_FN:
        return _verify_claim_fn(scope)
    return [f"unknown kind {kind!r}"]
