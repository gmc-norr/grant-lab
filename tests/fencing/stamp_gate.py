"""Fence F3e1a — a mechanism-2 generation must come from a FenceStamp.

Spec 2026-08-21 §7.0.3. Revisions 1-4 of that spec could only SAY "capture the
generation early"; four review rounds found defects in the prose that said it. This
turns the rule into something the build checks.

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
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return problems
