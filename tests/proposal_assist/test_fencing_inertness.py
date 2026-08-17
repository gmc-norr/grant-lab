"""Fence F1 is INERT: it adds primitives and a column, and wires neither.

Global Constraint 1. F1 is purely additive so it can ship against a production
system with collab live. This file fails the moment someone wires a primitive
into a caller during F1 — which is F3-F7's job, not this slice's.

F2 replaces this file with the real coverage gate, which asserts the OPPOSITE:
that every belief-carrying site DOES fence. Deleting this file is correct then,
and only then.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "call_manager"
FENCING_NAMES = {"live_at_generation", "acquire_commit_mutex_at_generation"}


def _python_files():
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_production_module_imports_the_fencing_primitives_yet():
    """The primitives exist and nothing uses them. Any import outside
    fencing.py itself means a call site was converted during F1."""
    offenders = []
    for path in _python_files():
        if path.name == "fencing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("proposal_assist.fencing"):
                    offenders.append(f"{path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("proposal_assist.fencing"):
                        offenders.append(f"{path}: import {alias.name}")
    assert not offenders, (
        "F1 converts NO call sites (Global Constraint 1), but these modules "
        "import the fencing primitives:\n  " + "\n  ".join(offenders)
    )


def test_no_production_module_calls_the_fencing_primitives_yet():
    """Belt to the import test's braces: catches a call reached via a module
    alias or a re-export, which the import check alone would miss."""
    offenders = []
    for path in _python_files():
        if path.name == "fencing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in FENCING_NAMES:
                offenders.append(f"{path}:{node.lineno}: {name}(...)")
    assert not offenders, (
        "F1 converts NO call sites (Global Constraint 1), but these are calls "
        "to the fencing primitives:\n  " + "\n  ".join(offenders)
    )


def test_nothing_reads_or_writes_claimed_at_generation_yet():
    """The column exists and no claim path sets it. F4-F7 wire it."""
    offenders = []
    for path in _python_files():
        if path.name == "models.py":
            continue  # the declaration itself
        text = path.read_text(encoding="utf-8")
        if "claimed_at_generation" in text:
            offenders.append(str(path))
    assert not offenders, (
        "F1 adds the column but wires nothing (Global Constraint 1); these "
        "modules reference claimed_at_generation:\n  " + "\n  ".join(offenders)
    )
