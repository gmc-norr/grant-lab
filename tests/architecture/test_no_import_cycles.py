"""No module-level import cycle inside `grant_lab`.

Three real cycles lived in this codebase undetected until 2026-09-05, when the
match-edition process cut removed `main.py`'s eager imports:

    fencing -> drafting/__init__ -> controller -> reconcile -> fencing
    fencing -> drafting/__init__ -> factory -> port_impl -> effects -> fencing

They never failed because `main.py` imported the whole chain into every process
before anything else could reach it, and `tests/proposal_assist/conftest.py`
imports `main`. That is luck, not design: the same cycles broke immediately once
the import order changed, and a `python -c "import grant_lab.proposal_assist.fencing"`
on the commit before the fix still fails.

MODULE-LEVEL imports only. A function-local import cannot deadlock a module body
— deferring one is the FIX for a cycle, not an instance of it — and `if
TYPE_CHECKING:` blocks never execute at runtime. Counting either would make the
gate red for the very changes that repair it.

PROVEN TO BITE, not merely green. `_cycles()` run against a worktree at
**65d706e8** — the commit before the fix, where the failure is reproducible as a
real ImportError — reports exactly ONE component, and it is the real one:

    drafting <-> drafting.controller <-> drafting.factory <-> fencing
             <-> port_impl <-> port_impl.effects <-> reconcile

Against the current tree it reports zero. The first draft of this gate scored
**0 on that same pre-fix tree** — hollow — because it drew an edge only to the
imported module. Importing `a.b.c` executes `a/__init__.py` and `a/b/__init__.py`
FIRST, and this cycle runs through `drafting/__init__.py`, never through
`drafting.locking` alone. Ancestor edges are what make the gate real; excluding
ancestors OF THE IMPORTER is what keeps it honest (an `__init__` importing its
own submodule is not a cycle, and counting it produced three false components).

The check is pure `ast` — the application is never imported.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "grant_lab"


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _top_level_imports(body: list[ast.stmt]):
    """Names imported when the module body EXECUTES."""
    for st in body:
        if isinstance(st, ast.Import):
            for alias in st.names:
                yield alias.name
        elif isinstance(st, ast.ImportFrom) and st.module and st.level == 0:
            yield st.module
            for alias in st.names:
                yield f"{st.module}.{alias.name}"
        elif isinstance(st, ast.If):
            if "TYPE_CHECKING" in ast.unparse(st.test):
                continue  # never executed at runtime
            yield from _top_level_imports(st.body)
            yield from _top_level_imports(st.orelse)
        elif isinstance(st, ast.Try):
            yield from _top_level_imports(st.body)


def _cycles(root: Path = SRC) -> list[list[str]]:
    files = sorted(root.rglob("*.py"))
    known = {_module_name(f, root) for f in files}
    edges: dict[str, set[str]] = defaultdict(set)

    packages = {_module_name(f, root) for f in files if f.name == "__init__.py"}

    for f in files:
        me = _module_name(f, root)
        for target in _top_level_imports(ast.parse(f.read_text()).body):
            if not target.startswith(root.name):
                continue
            # `from pkg.mod import thing` — walk up to the deepest real module
            while target and target not in known:
                target = target.rpartition(".")[0]
            if not target:
                continue
            # Importing `a.b.c` EXECUTES a/__init__.py and a/b/__init__.py first,
            # so the importer depends on every ancestor package too. Without these
            # edges this gate is hollow: it scored 0 on the tree where the fencing
            # cycle was reproducible as a real ImportError, because that cycle runs
            # through drafting/__init__.py and never through drafting.locking alone.
            for dep in {target} | {
                a for a in packages if target.startswith(a + ".")
            }:
                if dep == me:
                    continue
                # An ancestor of the IMPORTER is already mid-import by definition
                # (that is how control reached this module), so depending on it is
                # not a cycle. Without this, every `__init__.py` that imports its
                # own submodule reports as one — three of the five components this
                # gate found at 65d706e8 were that shape and are benign.
                if me.startswith(dep + "."):
                    continue
                edges[me].add(dep)

    # Tarjan, iterative: any strongly-connected component of size > 1 is a cycle.
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    onstack: dict[str, bool] = {}
    stack: list[str] = []
    counter = [0]
    found: list[list[str]] = []

    def strong(v: str) -> None:
        work = [(v, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                onstack[node] = True
            recurse = False
            succs = sorted(edges[node])
            for i in range(pi, len(succs)):
                w = succs[i]
                if w not in index:
                    work[-1] = (node, i + 1)
                    work.append((w, 0))
                    recurse = True
                    break
                if onstack.get(w):
                    low[node] = min(low[node], index[w])
            if recurse:
                continue
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    found.append(sorted(comp))
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])

    for m in sorted(known):
        if m not in index:
            strong(m)
    return found


def test_no_module_level_import_cycle_in_grant_lab():
    cycles = _cycles()
    assert cycles == [], "module-level import cycles:\n" + "\n".join(
        f"  {len(c)} modules: " + " <-> ".join(x.removeprefix("grant_lab.") for x in c)
        for c in cycles
    )


def test_the_analysis_actually_saw_the_codebase():
    """A cycle check over zero files passes vacuously. Pin that it read the tree."""
    files = list(SRC.rglob("*.py"))
    assert len(files) > 200, f"only {len(files)} modules scanned — is SRC wrong? {SRC}"
