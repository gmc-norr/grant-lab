"""Lock: the dead `sources/extraction.py` and its libraries stay removed.

`sources/extraction.py` was imported by no production module. It was deleted on
2026-11-05 together with its only importers of `pymupdf` and `trafilatura`. Each
check below reads the real repo file; the mutation tests run the same check on a
doctored copy and assert it refuses.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOVED_MODULE = Path("src/grant_lab/sources/extraction.py")
REMOVED_DISTS = {"pymupdf", "trafilatura"}
REMOVED_IMPORTS = {"pymupdf", "fitz", "trafilatura"}
SCANNED_DIRS = ("src", "scripts", "alembic")


def _dist_name(requirement: str) -> str:
    """`"trafilatura>=2.0"` / `"Foo[extra]; marker"` -> normalised distribution name."""
    name = requirement
    for sep in ("[", ";", "<", ">", "=", "!", "~", " ", "@"):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


def module_file_exists(root: Path) -> bool:
    return (root / REMOVED_MODULE).exists()


def forbidden_imports(root: Path) -> list[str]:
    """`path:line import x` for every real import of a removed library (via `ast`)."""
    found: list[str] = []
    for d in SCANNED_DIRS:
        for path in sorted((root / d).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for n in names:
                    if n.split(".")[0] in REMOVED_IMPORTS:
                        found.append(f"{path.relative_to(root)}:{node.lineno} import {n}")
    return found


def declared_dependencies(pyproject: Path) -> set[str]:
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    return {_dist_name(d) for d in deps} & REMOVED_DISTS


def locked_packages(lock: Path) -> set[str]:
    pkgs = tomllib.loads(lock.read_text()).get("package", [])
    return {p["name"] for p in pkgs} & REMOVED_DISTS


# --- the real tree -----------------------------------------------------------


def test_the_extraction_module_is_gone():
    assert not module_file_exists(ROOT)


def test_no_python_file_imports_a_removed_library():
    assert forbidden_imports(ROOT) == []


def test_pyproject_does_not_depend_on_a_removed_library():
    assert declared_dependencies(ROOT / "pyproject.toml") == set()


def test_the_lock_does_not_contain_a_removed_library():
    assert locked_packages(ROOT / "uv.lock") == set()


# --- the checks refuse a doctored copy ----------------------------------------


def test_the_module_check_refuses_a_recreated_file(tmp_path):
    assert not module_file_exists(tmp_path), "control: empty tree passes"
    target = tmp_path / REMOVED_MODULE
    target.parent.mkdir(parents=True)
    target.write_text("")
    assert module_file_exists(tmp_path)


def test_the_import_check_refuses_each_import_form_but_not_a_comment(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "alembic").mkdir()
    ok = tmp_path / "src" / "ok.py"
    ok.write_text('# import trafilatura\n"""uses pymupdf and fitz"""\nx = "trafilatura"\n')
    assert forbidden_imports(tmp_path) == [], "control: mentions are not imports"
    for source in (
        "import trafilatura\n",
        "import pymupdf as p\n",
        "import fitz\n",
        "from trafilatura.settings import use_config\n",
        "def f():\n    import trafilatura\n",
    ):
        ok.write_text(source)
        assert forbidden_imports(tmp_path), f"not refused: {source!r}"
    ok.write_text("import trafilatura\n")
    (tmp_path / "scripts" / "s.py").write_text("")
    (tmp_path / "scripts" / "s.py").write_text("import fitz\n")
    (tmp_path / "alembic" / "a.py").write_text("import pymupdf\n")
    assert len(forbidden_imports(tmp_path)) == 3


def test_the_dependency_check_refuses_a_readded_requirement(tmp_path):
    text = (ROOT / "pyproject.toml").read_text()
    assert declared_dependencies(ROOT / "pyproject.toml") == set(), "control"
    for req in ("trafilatura>=2.0.0", "pymupdf>=1.25.0", "PyMuPDF", "trafilatura[all]>=2"):
        mutated = text.replace("dependencies = [\n", f'dependencies = [\n    "{req}",\n', 1)
        assert mutated != text, "no change made"
        copy_path = tmp_path / "pyproject.toml"
        copy_path.write_text(mutated)
        assert declared_dependencies(copy_path), f"not refused: {req}"


def test_the_lock_check_refuses_a_readded_package(tmp_path):
    text = (ROOT / "uv.lock").read_text()
    assert locked_packages(ROOT / "uv.lock") == set(), "control"
    for name in sorted(REMOVED_DISTS):
        copy_path = tmp_path / "uv.lock"
        copy_path.write_text(text + f'\n[[package]]\nname = "{name}"\nversion = "1.0.0"\n')
        assert locked_packages(copy_path) == {name}
