#!/usr/bin/env python3
"""Generate docs/ARCHITECTURE.md from the code.

Reads the source with ast — never imports the app — so it runs without a database,
without settings, and without any LLM SDK.

The point of this file is that the map cannot go stale: a pytest test regenerates it
and fails on any diff, the same way schema.gen.ts is pinned in CI.

Run:  uv run python scripts/gen_architecture.py            # writes the file
      uv run python scripts/gen_architecture.py --stdout   # prints it
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MODELS_PKG = REPO / "src/grant_lab/db/models"
#: The proposal workspace's tables (spec 2026-11-03-workspace-removal-design.md §3).
#: Still in the database and still mapped, but nothing on master reads or writes
#: them. Parsed like the live ones and marked "(parked)" wherever a table is named.
PARKED_PKG = MODELS_PKG / "workspace"
CONFIG = REPO / "src/grant_lab/config.py"
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
        self.parked = False
        self.columns: list[tuple[str, str]] = []
        self.checks: list[tuple[str, str]] = []
        self.indexes: list[tuple[str, list[str], bool, str | None]] = []
        self.fks: list[tuple[str, str, str]] = []

    @property
    def label(self) -> str:
        return f"`{self.table}`" + (" (parked)" if self.parked else "")


def _model_modules(pkg: pathlib.Path) -> list[pathlib.Path]:
    """The model modules of *pkg*, in the order its `__init__.py` imports them.

    Ordered by that file rather than alphabetically so the generated document
    keeps following the package's own subsystem grouping, and so a module added
    to the package is picked up here without editing this script. Every .py in
    the package must be reachable this way -- an unimported module would be
    silently missing from ARCHITECTURE.md, which is the failure this assert
    exists to prevent.
    """
    init = (pkg / "__init__.py").read_text()
    order: list[pathlib.Path] = []
    for node in ast.parse(init).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            _, _, tail = node.module.rpartition(".")
            path = pkg / f"{tail}.py"
            if path.is_file() and path not in order:
                order.append(path)
    found = {p for p in pkg.glob("*.py") if p.name != "__init__.py"}
    missing = sorted(p.name for p in found - set(order))
    assert not missing, f"model modules not imported by {pkg.name}/__init__.py: {missing}"
    return order


def parse_models(pkg: pathlib.Path = MODELS_PKG) -> list[Table]:
    out: list[Table] = []
    classes: list[ast.ClassDef] = []
    for path in _model_modules(pkg):
        classes += [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)]
    for cls in classes:
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


def all_tables() -> list[Table]:
    """The live tables, then the parked ones, each marked.

    `_model_modules` reads one directory, so a model SUB-package is invisible to
    it. The parked package is parsed on its own; any other sub-package would drop
    out of the map silently, which the assert refuses.
    """
    subs = sorted(p.name for p in MODELS_PKG.iterdir() if (p / "__init__.py").is_file())
    assert subs == [PARKED_PKG.name], (
        f"model sub-packages {subs}: only {PARKED_PKG.name!r} is parsed, so any other "
        "would be missing from ARCHITECTURE.md"
    )
    parked = parse_models(PARKED_PKG)
    for t in parked:
        t.parked = True
    return parse_models(MODELS_PKG) + parked


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


def _is_status_like(nm: str | None, expr: str) -> bool:
    """A CHECK on a lifecycle column: `status` anywhere, or `state` as a whole
    word or after `_`. escalation_state yes; ck_collab_artifact_state_artifact
    (a table name) and a word like `estate` no."""
    text = f"{nm or ''} {expr}".lower()
    return "status" in text or re.search(r"(?<![a-z0-9])state\b", text) is not None


def render() -> str:
    tables = all_tables()
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
    A("A table marked **(parked)** belongs to the proposal workspace, which left master")
    A("on 2026-11-03. It and its rows are still in the database and its definition is in")
    A("`src/grant_lab/db/models/workspace/`, but no code on master reads or writes it.")
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
                rows.append((t.table, nm, ", ".join(cols), where, t.label))
    for _tb, nm, cols, where, label in sorted(rows):
        A(f"| {label} | `{nm}` | {cols} | `{where}` |")
    A("")
    seen: dict[str, list[str]] = {}
    for tb, _nm, _c, w, _label in rows:
        seen.setdefault(w, []).append(tb)
    A(
        f"**{len(rows)} partial unique indexes across {len({r[0] for r in rows})} tables, "
        f"{len(seen)} distinct predicates.**"
    )
    A("")
    for w, tbs in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        A(f"- `{w}` — {len(tbs)} table(s): {', '.join(sorted(tbs))}")
    A("")

    # --- tables
    A("## Tables")
    A("")
    A(f"{len(tables)} mapped tables, {sum(t.parked for t in tables)} of them parked.")
    A("")
    A("| table | class | cols | FKs | CHECKs | indexes |")
    A("|---|---|---|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        A(
            f"| {t.label} | `{t.cls}` | {len(t.columns)} | {len(t.fks)} "
            f"| {len(t.checks)} | {len(t.indexes)} |"
        )
    A("")

    A("### Status vocabularies")
    A("")
    A(
        "Every CHECK constraining a `status`- or `state`-like column. "
        "Divergence here is a real defect."
    )
    A("")
    A("| table | constraint | vocabulary |")
    A("|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        for nm, expr in t.checks:
            if _is_status_like(nm, expr):
                A(f"| {t.label} | `{nm}` | `{expr}` |")
    A("")

    A("### Ownership: what each table is keyed on")
    A("")
    A("| table | foreign keys (column → target, ON DELETE) |")
    A("|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        if t.fks:
            A(
                f"| {t.label} | "
                + "<br>".join(f"`{c}` → `{tg}`{f' ({od})' if od else ''}" for c, tg, od in t.fks)
                + " |"
            )
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
