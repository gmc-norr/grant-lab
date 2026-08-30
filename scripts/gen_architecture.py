#!/usr/bin/env python3
"""Generate docs/ARCHITECTURE.md from the code.

Reads the source with ast — never imports the app — so it runs without a database,
without settings, and without the drafting SDK.

The point of this file is that the map cannot go stale: a pytest test regenerates it
and fails on any diff, the same way schema.gen.ts is pinned in CI.

Run:  uv run python scripts/gen_architecture.py            # writes the file
      uv run python scripts/gen_architecture.py --stdout   # prints it
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MODELS = REPO / "src/call_manager/db/models.py"
CONFIG = REPO / "src/call_manager/config.py"
FENCING = REPO / "src/call_manager/proposal_assist/fencing.py"
INVENTORY = REPO / "tests/fencing/inventory.py"
OUT = REPO / "docs/ARCHITECTURE.md"


def _const(node: ast.AST) -> str | None:
    """The literal string a node evaluates to, if it is one. Handles text("...")."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "text":
            return _const(node.args[0])
    return None


def _kwarg(call: ast.Call, key: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


class Table:
    def __init__(self, cls: str, table: str, line: int) -> None:
        self.cls, self.table, self.line = cls, table, line
        self.columns: list[tuple[str, str]] = []
        self.checks: list[tuple[str, str]] = []
        self.indexes: list[tuple[str, list[str], bool, str | None]] = []
        self.fks: list[tuple[str, str, str]] = []


def parse_models() -> list[Table]:
    tree = ast.parse(MODELS.read_text())
    out: list[Table] = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        tname = None
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.AnnAssign | ast.Assign)
                and "__tablename__" in ast.dump(stmt)
                and (v := _const(getattr(stmt, "value", None) or ast.Pass()))
            ):
                tname = v
        if not tname:
            continue
        t = Table(cls.name, tname, cls.lineno)

        for stmt in cls.body:
            # columns
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                nm = stmt.target.id
                if nm.startswith("__"):
                    continue
                ann = ast.unparse(stmt.annotation) if stmt.annotation else ""
                if not ann.startswith("Mapped"):
                    continue
                t.columns.append((nm, ann.removeprefix("Mapped[").removesuffix("]")))
                if isinstance(stmt.value, ast.Call):
                    for a in stmt.value.args:
                        if isinstance(a, ast.Call):
                            fn = getattr(a.func, "id", "") or getattr(a.func, "attr", "")
                            if fn == "ForeignKey" and a.args and (tgt := _const(a.args[0])):
                                od = _kwarg(a, "ondelete")
                                t.fks.append((nm, tgt, _const(od) if od else ""))
            # __table_args__
            if isinstance(stmt, ast.Assign) and any(
                getattr(tg, "id", "") == "__table_args__" for tg in stmt.targets
            ):
                for el in getattr(stmt.value, "elts", []):
                    if not isinstance(el, ast.Call):
                        continue
                    fn = getattr(el.func, "id", "") or getattr(el.func, "attr", "")
                    nmk = _kwarg(el, "name")
                    nm = _const(nmk) if nmk else ""
                    if fn == "CheckConstraint" and el.args:
                        t.checks.append((nm or "", _const(el.args[0]) or ast.unparse(el.args[0])))
                    elif fn == "Index":
                        parts = [_const(a) for a in el.args]
                        idx_name = parts[0] if parts else ""
                        cols = [p for p in parts[1:] if p]
                        uq = _kwarg(el, "unique")
                        where = _kwarg(el, "postgresql_where")
                        t.indexes.append(
                            (
                                idx_name or "",
                                cols,
                                bool(getattr(uq, "value", False)),
                                _const(where) if where else None,
                            )
                        )
        out.append(t)
    return out


def parse_caps() -> list[tuple[str, str]]:
    tree = ast.parse(CONFIG.read_text())
    out = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                nm = stmt.target.id
                if not any(k in nm for k in ("cap", "max_", "_limit", "budget")):
                    continue
                default = ""
                if isinstance(stmt.value, ast.Call) and stmt.value.args:
                    default = ast.unparse(stmt.value.args[0])
                elif stmt.value is not None:
                    default = ast.unparse(stmt.value)
                out.append((nm, default))
    return sorted(set(out))


def parse_fence_primitives() -> list[tuple[str, int, str]]:
    tree = ast.parse(FENCING.read_text())
    out = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_"):
            doc = (ast.get_docstring(n) or "").strip().split("\n")[0]
            out.append((n.name, n.lineno, doc))
    return out


def parse_gate_allowlists() -> list[tuple[str, int]]:
    if not INVENTORY.exists():
        return []
    tree = ast.parse(INVENTORY.read_text())
    out = []
    for n in tree.body:
        tgt = None
        if isinstance(n, ast.Assign) and n.targets:
            tgt = getattr(n.targets[0], "id", None)
        elif isinstance(n, ast.AnnAssign):
            tgt = getattr(n.target, "id", None)
        if not tgt or not tgt.isupper():
            continue
        v = getattr(n, "value", None)
        size = len(getattr(v, "elts", []) or getattr(v, "keys", []) or []) if v else 0
        if size:
            out.append((tgt, size))
    return out


def render() -> str:
    tables = parse_models()
    L: list[str] = []
    A = L.append

    A("# Architecture map")
    A("")
    A("> **GENERATED — do not edit.** Produced by `scripts/gen_architecture.py` from the")
    A("> source, via `ast` (the app is never imported). A pytest test regenerates this")
    A("> file and fails on any diff, so it cannot go stale.")
    A("> Regenerate with `uv run python scripts/gen_architecture.py`.")
    A("")
    A("This map exists because two wrong recommendations were made from unmeasured")
    A("inference about facts that were not written down anywhere lookup-able.")
    A("")

    # --- partial unique indexes: the section that would have caught the error
    A("## Active-uniqueness: every partial UNIQUE index and its exact predicate")
    A("")
    A("Read this before claiming two tables share, or do not share, a rule.")
    A("")
    A("| table | index | columns | predicate |")
    A("|---|---|---|---|")
    rows = []
    for t in tables:
        for nm, cols, uq, where in t.indexes:
            if uq and where:
                rows.append((t.table, nm, ", ".join(cols), where))
    for r in sorted(rows):
        A(f"| `{r[0]}` | `{r[1]}` | {r[2]} | `{r[3]}` |")
    A("")
    seen: dict[str, list[str]] = {}
    for tb, _nm, _c, w in rows:
        seen.setdefault(w, []).append(tb)
    A(f"**{len(rows)} partial unique indexes across {len({r[0] for r in rows})} tables, "
      f"{len(seen)} distinct predicates.**")
    A("")
    for w, tbs in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        A(f"- `{w}` — {len(tbs)} table(s): {', '.join(sorted(tbs))}")
    A("")

    # --- tables
    A("## Tables")
    A("")
    A(f"{len(tables)} mapped tables.")
    A("")
    A("| table | class | cols | FKs | CHECKs | indexes |")
    A("|---|---|---|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        A(f"| `{t.table}` | `{t.cls}` | {len(t.columns)} | {len(t.fks)} "
          f"| {len(t.checks)} | {len(t.indexes)} |")
    A("")

    A("### Status vocabularies")
    A("")
    A("Every CHECK constraining a `status`-like column. Divergence here is a real defect.")
    A("")
    A("| table | constraint | vocabulary |")
    A("|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        for nm, expr in t.checks:
            if "status" in expr.lower() or "status" in (nm or "").lower():
                A(f"| `{t.table}` | `{nm}` | `{expr}` |")
    A("")

    A("### Ownership: what each table is keyed on")
    A("")
    A("| table | foreign keys (column → target, ON DELETE) |")
    A("|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        if t.fks:
            A(f"| `{t.table}` | "
              + "<br>".join(f"`{c}` → `{tg}`{f' ({od})' if od else ''}" for c, tg, od in t.fks)
              + " |")
    A("")

    # --- fence
    A("## The epoch fence")
    A("")
    A("Public primitives in `src/call_manager/proposal_assist/fencing.py`.")
    A("**Read the docstring for the exact SCOPE of each rule before relying on it** —")
    A("a rule that looks absolute usually is not.")
    A("")
    A("| primitive | line | first line of its contract |")
    A("|---|---|---|")
    for nm, ln, doc in parse_fence_primitives():
        A(f"| `{nm}` | fencing.py:{ln} | {doc} |")
    A("")
    gates = parse_gate_allowlists()
    if gates:
        A("### Armed-gate allowlists (`tests/fencing/inventory.py`)")
        A("")
        A("Each may only SHRINK. A growing entry is a reviewed diff.")
        A("")
        A("| allowlist | entries |")
        A("|---|---|")
        for nm, size in sorted(gates):
            A(f"| `{nm}` | {size} |")
        A("")

    # --- caps
    caps = parse_caps()
    A("## Caps, ceilings and limits")
    A("")
    A(f"{len(caps)} settings in `config.py`. A cap that exists in only one of a pair of")
    A("sibling paths is the shape of a real defect.")
    A("")
    A("| setting | default |")
    A("|---|---|")
    for nm, d in caps:
        A(f"| `{nm}` | `{d}` |")
    A("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    text = render()
    if "--stdout" in sys.argv:
        sys.stdout.write(text)
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(REPO)} ({len(text.splitlines())} lines)")
