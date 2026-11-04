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


def _package_of(module: str, is_package: bool) -> str:
    """The package a relative import inside `module` resolves against.

    A package (`a/b/__init__.py`) resolves `.` against ITSELF; a plain module
    (`a/b/c.py`) resolves it against its parent. Same rule as
    `importlib._bootstrap._resolve_name`.
    """
    return module if is_package else module.rpartition(".")[0]


def _resolve_relative(module: str | None, level: int, package: str) -> str | None:
    """`from ..x import y` inside `package` -> the absolute module name.

    Returns None when the import climbs above the root, which cannot be part of
    a cycle inside this tree.
    """
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None
    base = bits[0]
    if not base:
        return None
    return f"{base}.{module}" if module else base


def _top_level_imports(body: list[ast.stmt], package: str):
    """Names imported when the module body EXECUTES.

    RELATIVE IMPORTS COUNT. They were skipped (`st.level == 0`) until the
    2026-09-05 outside review demonstrated the hole: the same two-module cycle
    written with `from .second import x` instead of the absolute spelling failed
    at runtime with a partially-initialized-module ImportError and this scanner
    reported `cycles=[]`. Four relative imports exist in
    `proposal_assist/collab/`, so the blind spot covered real code.
    """
    for st in body:
        if isinstance(st, ast.Import):
            for alias in st.names:
                yield alias.name
        elif isinstance(st, ast.ImportFrom):
            if st.level == 0:
                if not st.module:
                    continue
                target = st.module
            else:
                resolved = _resolve_relative(st.module, st.level, package)
                if resolved is None:
                    continue
                target = resolved
            yield target
            for alias in st.names:
                yield f"{target}.{alias.name}"
        elif isinstance(st, ast.If):
            if "TYPE_CHECKING" in ast.unparse(st.test):
                continue  # never executed at runtime
            yield from _top_level_imports(st.body, package)
            yield from _top_level_imports(st.orelse, package)
        elif isinstance(st, ast.Try):
            yield from _top_level_imports(st.body, package)


def _cycles(root: Path = SRC) -> list[list[str]]:
    files = sorted(root.rglob("*.py"))
    known = {_module_name(f, root) for f in files}
    edges: dict[str, set[str]] = defaultdict(set)

    packages = {_module_name(f, root) for f in files if f.name == "__init__.py"}

    for f in files:
        me = _module_name(f, root)
        package = _package_of(me, f.name == "__init__.py")
        for target in _top_level_imports(ast.parse(f.read_text()).body, package):
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
            for dep in {target} | {a for a in packages if target.startswith(a + ".")}:
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
    """A cycle check over zero files passes vacuously. Pin that it read the tree.

    311 modules at dfb6dbae; 118 after the 2026-11-03 workspace removal."""
    files = list(SRC.rglob("*.py"))
    assert len(files) > 100, f"only {len(files)} modules scanned — is SRC wrong? {SRC}"


# ---------------------------------------------------------------------------
# The gate on the gate.
#
# `_top_level_imports` skipped every relative import until the 2026-09-05
# outside review built the same cycle twice — once absolute, once relative —
# and showed the scanner reporting the first and missing the second while BOTH
# failed at runtime. These cases pin the fix, and the runtime control is what
# makes them evidence rather than a restatement of the implementation.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402

import pytest  # noqa: E402


def _write_cycle(root: Path, *, relative: bool) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    second = ".second" if relative else "cycleprobe.second"
    first = ".first" if relative else "cycleprobe.first"
    (root / "first.py").write_text(f"from {second} import B\n\nA = 1\n")
    (root / "second.py").write_text(f"from {first} import A\n\nB = 2\n")


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_the_scanner_finds_a_two_module_cycle_in_either_spelling(tmp_path, relative):
    """Both spellings are the SAME cycle. The relative one was invisible.

    What trivially passes this: a scanner that reports a cycle for the absolute
    case only — which is exactly what shipped, and why the `relative` id is the
    one that matters here.
    """
    root = tmp_path / "cycleprobe"
    _write_cycle(root, relative=relative)
    found = _cycles(root)
    assert [sorted(c) for c in found] == [["cycleprobe.first", "cycleprobe.second"]], (
        f"relative={relative}: scanner reported {found}"
    )


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_both_spellings_really_do_fail_at_runtime(tmp_path, relative):
    """The control. Without it the case above only proves the scanner agrees
    with itself; this proves the thing it is scanning for is a real ImportError."""
    root = tmp_path / "cycleprobe"
    _write_cycle(root, relative=relative)
    proc = subprocess.run(
        [sys.executable, "-c", "import cycleprobe.first"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0, f"relative={relative}: expected an ImportError, got exit 0"
    assert "partially initialized module" in proc.stderr, proc.stderr[-500:]


def test_a_relative_import_that_climbs_above_the_root_is_ignored_not_crashed(tmp_path):
    """`from ... import x` at depth 1 escapes the tree. It cannot be part of a
    cycle inside it, and it must not raise while building the graph."""
    root = tmp_path / "cycleprobe"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "lonely.py").write_text(
        textwrap.dedent("""
            from ... import something

            X = 1
        """)
    )
    assert _cycles(root) == []
