"""F3b — the ports boundary: stdlib-only contracts, injected implementations."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.ports import Ports
from grant_lab.proposal_assist.ports.accounting import Accounting
from grant_lab.proposal_assist.ports.effects import Effects
from grant_lab.proposal_assist.ports.queries import Queries

PORTS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "grant_lab" / "proposal_assist" / "ports"
)
PORT_IMPL_DIR = PORTS_DIR.parent / "port_impl"


def _ports_module_names() -> list[str]:
    """Every importable module under ports/, discovered from the filesystem so
    this check cannot go stale as modules are added — a hand-maintained list
    would silently stop covering a new module like ports/errors.py."""
    names = []
    for path in sorted(PORTS_DIR.rglob("*.py")):
        parts = list(path.relative_to(PORTS_DIR.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append("grant_lab.proposal_assist." + ".".join(parts))
    return names


def test_importing_the_ports_package_pulls_in_no_sqlalchemy():
    """Transitive, and therefore run in a FRESH interpreter. A source-level scan
    would miss a stdlib-looking project helper that imports a session two hops
    down; sys.modules cannot be fooled that way."""
    modules = _ports_module_names()
    assert modules, "discovered zero modules under ports/ — the walk itself is broken"
    import_statements = "; ".join(f"import {name}" for name in modules)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{import_statements}; "
            "import sys; print(sorted(m for m in sys.modules if m.split('.')[0] "
            "in {'sqlalchemy', 'psycopg', 'alembic'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # check=True already turns an import failure (module dies before printing)
    # into a raised CalledProcessError. The strict "[]" comparison below also
    # rejects an empty stdout on its own: "" != "[]", so a child that produced
    # no output cannot be misread as a pass.
    assert result.stdout.strip() == "[]", (
        f"the ports package reached the database layer: {result.stdout.strip()}"
    )


def test_the_ports_package_never_names_the_implementation_package():
    """The dependency runs one way. A ports module importing port_impl would let
    a tool reach every session in the tree through a stdlib-looking name.

    A bare relative import — `from .. import port_impl` — carries the name in
    node.names, not node.module: node.module is None for that form, so a check
    that requires node.module to be truthy never looks at it. `from ..port_impl
    import x` is unaffected (node.module == "port_impl" there), but is covered
    explicitly below rather than assumed."""
    offenders = []
    for path in sorted(PORTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if "port_impl" in name or name.split(".")[0] in {"sqlalchemy", "psycopg"}:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, "\n  ".join(offenders)


def test_no_implementation_module_holds_a_session_factory_as_a_module_global():
    """Spec 4.3. `effects._session_factory` would be reachable from any module
    that can import effects — which, after F3d, is every tool."""
    import importlib

    offenders = []
    for path in sorted(PORT_IMPL_DIR.glob("*.py")):
        module = importlib.import_module(
            f"grant_lab.proposal_assist.port_impl.{path.stem}"
            if path.stem != "__init__"
            else "grant_lab.proposal_assist.port_impl"
        )
        for attr, value in vars(module).items():
            if isinstance(value, (sessionmaker, Engine, Session)):
                offenders.append(f"{path.name}::{attr}")
    assert not offenders, "\n  ".join(offenders)


def test_build_ports_returns_the_three_capabilities(pg_session_factory):
    ports = build_ports(pg_session_factory)
    assert isinstance(ports, Ports)
    assert isinstance(ports.queries, Queries)
    assert isinstance(ports.effects, Effects)
    assert isinstance(ports.accounting, Accounting)


def test_the_bundle_is_frozen(pg_session_factory):
    import dataclasses

    ports = build_ports(pg_session_factory)
    try:
        ports.effects = None  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("Ports is mutable — a tool could swap its own effects in")


def test_the_bundle_exposes_no_session(pg_session_factory):
    """GC-1, checked one level down: a tool holding Ports must not be able to
    walk to a session through a public attribute."""
    ports = build_ports(pg_session_factory)
    for capability in (ports.queries, ports.effects, ports.accounting):
        public = {
            name: getattr(capability, name)
            for name in dir(capability)
            if not name.startswith("_")
        }
        for name, value in public.items():
            assert not isinstance(value, (sessionmaker, Engine, Session)), (
                f"{type(capability).__name__}.{name} is a database handle"
            )
