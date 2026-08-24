"""Fence F3e1a — a mechanism-2 generation must come from a FenceStamp minted early.

Spec 2026-08-21 §7.0.3. Revisions 1-4 of that spec could only SAY "capture the
generation early"; four review rounds found defects in the prose that said it. This
turns the rule into something the build checks.

TWO rules, because either alone is satisfiable by an unfenced site:

* the mutex's `generation=` must be `<name>.generation`, and
* `stamp_for_workspace(...)` must be handed a bare name.

The second exists because the first is a rule about SHAPE and says nothing about
where the name came from. Measured on this tree with only the first rule in force:
replacing a converted site's
`stamp = stamp_for_workspace(workspace)` with
`stamp = stamp_for_workspace(session.get(Workspace, workspace.id))` left the mutex
line letter-perfect and the whole fencing suite green, while fencing nothing at all —
the belief was minted from a read taken at the write.

Reads source as TEXT and matches parsed AST call nodes. It never inspects source
text for a substring, because a comment beside the code would satisfy that — a
mistake this project has shipped four times.
"""

from __future__ import annotations

import ast

#: The mechanism-2 acquisition. Its generation argument is the one under scrutiny.
MECHANISM_2_ACQUIRE = "acquire_commit_mutex_at_generation"

#: The attribute a stamp exposes. `stamp.generation` is the only accepted shape.
STAMP_FIELD = "generation"

#: The service-side stamp mint (`ports/stamp.py`, spec §7.0.2). Constraining only
#: the mutex's `generation=` shape is not enough: `stamp_for_workspace(...)` accepts
#: ANY object with `.lifecycle_generation`, so a late read laundered through it —
#: `generation=stamp_for_workspace(session.get(Workspace, wid)).generation` — wears
#: a perfect `<name>.generation` at the mutex and still fences nothing. The mint must
#: therefore be handed the object an access check ALREADY loaded, which at the syntax
#: level means a bare name.
STAMP_MINT = "stamp_for_workspace"

#: The mint's only parameter, for the `stamp_for_workspace(workspace=ws)` spelling.
STAMP_MINT_PARAM = "workspace"

SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_stamp_attribute(node: ast.AST) -> bool:
    """`<name>.generation` and nothing else.

    A Call anywhere in the chain is what rejects an inline late read such as
    `session.get(Workspace, wid).lifecycle_generation`: its value is an Attribute
    whose .value is a Call, not a Name.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == STAMP_FIELD
        and isinstance(node.value, ast.Name)
    )


def _minted_from(node: ast.Call) -> ast.AST | None:
    """The expression a `stamp_for_workspace(...)` call mints from, or None.

    None means "no workspace argument at all" — a TypeError at runtime, but at
    scan time it is simply an unprovable origin, and this gate treats an origin
    it cannot see as a violation rather than waving it through.
    """
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == STAMP_MINT_PARAM:
            return kw.value
    return None


def check_source(source: str, path: str) -> list[str]:
    """One string per violation. Pure; imports nothing from src/."""
    tree = ast.parse(source)
    problems: list[str] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, SCOPED):
            stack = stack + [node.name]
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == MECHANISM_2_ACQUIRE:
                qual = ".".join(stack) if stack else "<module>"
                supplied = [kw.value for kw in node.keywords if kw.arg == STAMP_FIELD]
                # A positional third argument IS the generation, and it is ALWAYS
                # wrong: the primitive is keyword-only, so a positional generation is
                # a violation regardless of the value's shape. Flag it directly —
                # do NOT route it through _is_stamp_attribute, or a positional
                # `stamp.generation` (a valid stamp attribute) would be accepted and
                # test_a_positional_generation_is_rejected could never pass.
                positional_generation = len(node.args) > 2
                if positional_generation:
                    problems.append(
                        f"{path}::{qual} passes generation positionally to "
                        f"{MECHANISM_2_ACQUIRE} — must use generation= keyword"
                    )
                if not supplied and not positional_generation:
                    problems.append(
                        f"{path}::{qual} calls {MECHANISM_2_ACQUIRE} with no generation"
                    )
                for value in supplied:
                    if not _is_stamp_attribute(value):
                        problems.append(
                            f"{path}::{qual} passes a generation that is not "
                            f"<stamp>.{STAMP_FIELD} — the actor's belief must come "
                            f"from a FenceStamp, never a read at the mutex"
                        )
            elif name == STAMP_MINT:
                qual = ".".join(stack) if stack else "<module>"
                source_expr = _minted_from(node)
                # A bare Name is a workspace object bound EARLIER — by an access
                # check, a liveness pre-screen, a parameter. Anything else is an
                # expression evaluated right here, and a `session.get`/`select`
                # hiding inside it re-reads the generation at the write. Both
                # production mints pass a bare name today; a conversion that
                # cannot is a Group D conversion (thread a stamp parameter in),
                # not a case for relaxing this.
                if not isinstance(source_expr, ast.Name):
                    problems.append(
                        f"{path}::{qual} mints a stamp from an expression rather "
                        f"than a bound name — {STAMP_MINT}() must be handed the "
                        f"workspace the caller's access check already loaded, or "
                        f"the stamp is itself a read at the mutex"
                    )
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return problems
