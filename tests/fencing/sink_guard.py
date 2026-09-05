"""Fence F3e1a — sink-completeness guard.

Spec 2026-08-21 §3.2.  Proves "every way this repository can invoke git is
enumerated."  Three clauses:

1. No subprocess call outside a declared sink function can be a git commit.
2. No alternative execution surface (os.system / os.popen /
   asyncio.create_subprocess_*) exists in src/.
3. No subprocess call uses shell=True (outside declared sink files).

Additionally asserts no git library (GitPython / pygit2 / dulwich) is importable
or declared in the manifests.

The allowlist ``PROVEN_NON_COMMIT`` is call-level, not function-level: each pin
is a ``(path_suffix, qualname, lineno_offset, argv_repr)`` tuple derived from the
AST.  ``test_no_pin_is_stale`` ensures every pin still corresponds to a call that
the fail-closed guard would report — so the list can only SHRINK over time.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from tests.fencing.git_fence import GIT_SINKS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_UV_LOCK = _REPO_ROOT / "uv.lock"

# ---------------------------------------------------------------------------
# Derived sink index
# ---------------------------------------------------------------------------

# Map path_suffix -> set of qualnames that are declared sinks.
_SINK_BY_SUFFIX: dict[str, set[str]] = {}
for _suffix, _qualname, _kind in GIT_SINKS:
    _SINK_BY_SUFFIX.setdefault(_suffix, set()).add(_qualname)

# All sink qualnames (to skip in files whose suffix is not in GIT_SINKS).
_ALL_SINK_QUALNAMES: frozenset[str] = frozenset(q for _, q, _ in GIT_SINKS)

# Files that are declared sink files (whole-file path suffix match).
_SINK_FILE_SUFFIXES: frozenset[str] = frozenset(s for s, _, _ in GIT_SINKS)


def _sink_qualnames_for_path(path_str: str) -> frozenset[str]:
    """Return the set of qualnames that are declared sinks for this file path."""
    for suffix in _SINK_BY_SUFFIX:
        if path_str.endswith(suffix):
            return frozenset(_SINK_BY_SUFFIX[suffix])
    return frozenset()


def _is_sink_file(path_str: str) -> bool:
    """True iff this file is one of the declared sink files."""
    return any(path_str.endswith(s) for s in _SINK_FILE_SUFFIXES)


# ---------------------------------------------------------------------------
# Import alias resolution helpers
# ---------------------------------------------------------------------------


class _ImportAliases(NamedTuple):
    """Resolved alias maps for subprocess, os, and asyncio in one source file."""

    # Set of local names that refer to the subprocess MODULE.
    subprocess_module_names: frozenset[str]
    # Set of local names imported directly from subprocess (run, Popen, call, etc.).
    subprocess_member_names: frozenset[str]
    # Set of local names that refer to os.system / os.popen (bare name form).
    os_system_names: frozenset[str]  # from os import system → {"system"}
    os_popen_names: frozenset[str]  # from os import popen  → {"popen"}
    # Set of local names that refer to the os MODULE.
    os_module_names: frozenset[str]
    # Set of local names that refer to the asyncio MODULE.
    asyncio_module_names: frozenset[str]
    # Set of local names imported directly from asyncio.
    asyncio_create_subprocess_names: frozenset[str]


def _resolve_aliases(tree: ast.AST) -> _ImportAliases:
    subprocess_mods: set[str] = set()
    subprocess_members: set[str] = set()
    os_system: set[str] = set()
    os_popen: set[str] = set()
    os_mods: set[str] = set()
    asyncio_mods: set[str] = set()
    asyncio_create: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                if alias.name == "subprocess":
                    subprocess_mods.add(local)
                elif alias.name == "os":
                    os_mods.add(local)
                elif alias.name == "asyncio":
                    asyncio_mods.add(local)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                if mod == "subprocess":
                    subprocess_members.add(local)
                elif mod == "os" and alias.name == "system":
                    os_system.add(local)
                elif mod == "os" and alias.name == "popen":
                    os_popen.add(local)
                elif mod == "asyncio" and alias.name.startswith("create_subprocess"):
                    asyncio_create.add(local)

    # Default names when imported without alias.
    # (covered above — "import subprocess" → local name "subprocess")

    return _ImportAliases(
        subprocess_module_names=frozenset(subprocess_mods),
        subprocess_member_names=frozenset(subprocess_members),
        os_system_names=frozenset(os_system),
        os_popen_names=frozenset(os_popen),
        os_module_names=frozenset(os_mods),
        asyncio_module_names=frozenset(asyncio_mods),
        asyncio_create_subprocess_names=frozenset(asyncio_create),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Git subcommands that are provably not "commit".
_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    [
        "log",
        "show",
        "diff",
        "rev-parse",
        "cat-file",
        "init",
        "status",
        "fetch",
        "pull",
        "clone",
        "remote",
        "branch",
        "tag",
        "stash",
        "config",
        "ls-tree",
        "ls-files",
        "describe",
        "format-patch",
        "archive",
        "shortlog",
        "blame",
        "bisect",
        "gc",
        "fsck",
        "pack-refs",
        "prune",
        "reflog",
        "for-each-ref",
        "hash-object",
        "update-index",
        "write-tree",
        "read-tree",
        "count-objects",
        "verify-pack",
        "check-ignore",
        "check-attr",
        "version",
        "help",
        "rev-list",
        "name-rev",
        "symbolic-ref",
        "var",
        "submodule",
        "worktree",
    ]
)


def _first_arg_is_definitely_not_commit(argv_node: ast.expr) -> bool:
    """Return True only when we can PROVE from the AST alone that this argv
    does not invoke 'git commit'.

    Proof criteria (all must hold):
    1.  ``argv_node`` is a literal ``ast.List`` or ``ast.Tuple``.
    2.  ALL elements are ``ast.Constant`` strings (no f-strings, no names,
        no concatenations).
    3.  The first element is the string ``"git"`` (or clearly not git at all).
    4.  The element immediately after ``"git"`` (skipping -c / --git-dir /
        --work-tree / --no-pager option pairs) is a string in
        ``_SAFE_GIT_SUBCOMMANDS``.

    Anything else returns False → fail closed.
    """
    # Must be a literal sequence.
    if not isinstance(argv_node, (ast.List, ast.Tuple)):
        return False
    elts = argv_node.elts
    # Every element must be a Constant string — no f-strings, no variables.
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
        return False

    if not elts:
        return False

    values = [e.value for e in elts]  # type: ignore[union-attr]

    first = values[0]
    if first != "git":
        # Not a git call at all → definitely not commit.
        return True

    # Walk past git option flags that take a value (e.g. -c key=val,
    # --git-dir=..., --work-tree=..., --no-pager).
    idx = 1
    while idx < len(values):
        v = values[idx]
        if v == "--":
            # End of options; next would be subcommand positional — treat as unknown.
            return False
        if v in ("-c", "--git-dir", "--work-tree", "--namespace"):
            # Two-element option: skip the value too.
            idx += 2
            continue
        if v.startswith("-"):
            # Single flag without a separate value arg.
            idx += 1
            continue
        # This is the subcommand.
        return v in _SAFE_GIT_SUBCOMMANDS

    return False


_SUBPROCESS_ATTRS: frozenset[str] = frozenset(
    ["run", "Popen", "call", "check_call", "check_output"]
)


def _is_subprocess_call(node: ast.Call, aliases: _ImportAliases) -> bool:
    """True if this Call is a subprocess.run / subprocess.Popen / subprocess.call /
    subprocess.check_call / subprocess.check_output or a directly-imported variant."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if (
            func.attr in _SUBPROCESS_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases.subprocess_module_names
        ):
            return True
    elif isinstance(func, ast.Name) and (
        func.id in aliases.subprocess_member_names and func.id in _SUBPROCESS_ATTRS
    ):
        return True
    return False


def _has_shell_true(node: ast.Call) -> bool:
    """True if the call has shell=True as a keyword argument."""
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _get_argv_node(node: ast.Call) -> ast.expr | None:
    """Return the first positional argument (argv) of a subprocess call, or None."""
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "args":
            return kw.value
    return None


# ---------------------------------------------------------------------------
# Function collection (qualname → AST node)
# ---------------------------------------------------------------------------


def _collect_functions(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    prefix: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return all function defs under tree with their dotted qualname."""
    results = []
    body = getattr(tree, "body", [])
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.append((qname, stmt))
            results.extend(_collect_functions(stmt, qname))
        elif isinstance(stmt, ast.ClassDef):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.extend(_collect_functions(stmt, qname))
        else:
            for child in ast.walk(stmt):
                if child is stmt:
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.append((qname, child))
                    results.extend(_collect_functions(child, qname))
                elif isinstance(child, ast.ClassDef):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.extend(_collect_functions(child, qname))
    return results


# ---------------------------------------------------------------------------
# Fingerprint type
# ---------------------------------------------------------------------------

# Call-level fingerprint: (relative_path, qualname, lineno_offset, argv_repr)
# - relative_path: path relative to _SRC_ROOT (as a string)
# - qualname: dotted qualname of the enclosing function
# - lineno_offset: call.lineno - func.lineno (0 = first line of function)
# - argv_repr: ast.dump of the first argument to the subprocess call, or ""
#   for Popen with a variable argv
Fingerprint = tuple[str, str, int, str]


# ---------------------------------------------------------------------------
# Allowlist — derived from Step-4 probe; each entry is a call-level fingerprint.
# Reason comments are MANDATORY: they record WHY the call is proven non-committing.
# ---------------------------------------------------------------------------

PROVEN_NON_COMMIT: tuple[Fingerprint, ...] = (
    # -----------------------------------------------------------------------
    # tools/history.py — three read-only git calls with dynamic argv elements.
    # The dynamic parts are sha/limit arguments, not the subcommand itself, but
    # because the list contains non-Constant elements the fail-closed checker
    # cannot prove "log"/"show"/"diff" is in position 1.
    #
    # These moved out of the async `*_impl` handlers into sync `_git_*` helpers
    # so the handlers can await them off the event loop (ruff ASYNC221 — a git
    # log on a large workspace was blocking the whole single-loop instance).
    # Only qualname and offset changed: each argv_repr below is byte-identical
    # to the one that was pinned before the move, which is the evidence that the
    # git commands themselves did not change.
    # -----------------------------------------------------------------------
    (  # _git_log_proposal: git log -n{limit} ... — reads commit log, no write
        "grant_lab/proposal_assist/tools/history.py",
        "_git_log_proposal",
        1,
        "List(elts=[Constant(value='git'), Constant(value='log'), JoinedStr(values=[Constant(value='-n'), FormattedValue(value=Name(id='limit', ctx=Load()), conversion=-1)]), Constant(value='--pretty=format:%H%x00%ct%x00%s'), Constant(value='--'), Name(id='PROPOSAL_FILENAME', ctx=Load())], ctx=Load())",  # noqa: E501
    ),
    (  # _git_show_proposal: git show {sha}:{path} — reads a blob, no write
        "grant_lab/proposal_assist/tools/history.py",
        "_git_show_proposal",
        1,
        "List(elts=[Constant(value='git'), Constant(value='show'), JoinedStr(values=[FormattedValue(value=Name(id='commit_sha', ctx=Load()), conversion=-1), Constant(value=':'), FormattedValue(value=Name(id='PROPOSAL_FILENAME', ctx=Load()), conversion=-1)])], ctx=Load())",  # noqa: E501
    ),
    (  # _git_diff_proposal: git diff {sha_a} {sha_b} -- file — reads a diff, no write
        "grant_lab/proposal_assist/tools/history.py",
        "_git_diff_proposal",
        1,
        "List(elts=[Constant(value='git'), Constant(value='diff'), Name(id='commit_a', ctx=Load()), Name(id='commit_b', ctx=Load()), Constant(value='--'), Name(id='PROPOSAL_FILENAME', ctx=Load())], ctx=Load())",  # noqa: E501
    ),
    # -----------------------------------------------------------------------
    # citations/gitio.py — three read-only helpers outside write_and_commit_files.
    # blob_sha and read_blob use dynamic sha/ref args; list_commits passes a
    # locally-built argv variable to Popen.
    # -----------------------------------------------------------------------
    (  # blob_sha: git rev-parse {ref}:{path} — resolves a blob SHA, no write
        "grant_lab/proposal_assist/citations/gitio.py",
        "blob_sha",
        6,
        "List(elts=[Constant(value='git'), Constant(value='rev-parse'), JoinedStr(values=[FormattedValue(value=Name(id='ref', ctx=Load()), conversion=-1), Constant(value=':'), FormattedValue(value=Name(id='path', ctx=Load()), conversion=-1)])], ctx=Load())",  # noqa: E501
    ),
    (  # read_blob: git cat-file -p {sha} — reads blob content, no write
        "grant_lab/proposal_assist/citations/gitio.py",
        "read_blob",
        8,
        "List(elts=[Constant(value='git'), Constant(value='cat-file'), Constant(value='-p'), Name(id='sha', ctx=Load())], ctx=Load())",  # noqa: E501
    ),
    (  # list_commits: Popen(argv) where argv = ["git", "log", ..., "--", pathspec]
        # — reads history, no write. The optional pathspec is appended after a
        # literal `--`, so a filename can never be parsed as an option. Offset
        # moved 29 -> 42 when that pathspec block and its docstring paragraph
        # went in above the Popen, then 42 -> 67 when `--parents`, the
        # `all_branches` block and their docstring paragraphs went in. DERIVED
        # from `pristine_stray_git_report()`, not counted by hand. The call
        # itself is unchanged, and `git log` still writes nothing.
        "grant_lab/proposal_assist/citations/gitio.py",
        "list_commits",
        67,
        "Name(id='argv', ctx=Load())",
    ),
    # -----------------------------------------------------------------------
    # citations/gitio.py — branch primitives (2026-08-29-workspace-branches).
    # Two new fail-closed call sites, both proven non-committing by audit:
    #   * _git is the shared subprocess helper `git *args`; a Starred argv can
    #     never be proven statically, but every caller in this module passes a
    #     read/ref-mutating verb only — symbolic-ref, branch (create/list/-D),
    #     show-ref, rev-parse, checkout. None create a commit. `git branch` and
    #     `git checkout` move refs/HEAD but do NOT commit; the commit sink stays
    #     write_and_commit_files (a declared sink), which _git never invokes.
    #   * is_valid_branch_name runs `git check-ref-format --branch <name>`, a
    #     pure name-validation read with no repository write of any kind.
    # -----------------------------------------------------------------------
    (  # _git: subprocess.run(["git", *args]) — shared helper, ref reads/moves only, never commits
        "grant_lab/proposal_assist/citations/gitio.py",
        "_git",
        4,
        "List(elts=[Constant(value='git'), Starred(value=Name(id='args', ctx=Load()), ctx=Load())], ctx=Load())",  # noqa: E501
    ),
    (  # is_valid_branch_name: git check-ref-format --branch {name} — validates a name, no write
        "grant_lab/proposal_assist/citations/gitio.py",
        "is_valid_branch_name",
        24,
        "List(elts=[Constant(value='git'), Constant(value='check-ref-format'), Constant(value='--branch'), Name(id='name', ctx=Load())], ctx=Load())",  # noqa: E501
    ),
)


# ---------------------------------------------------------------------------
# Core predicate helpers
# ---------------------------------------------------------------------------


def _stray_git_commit_in(source: str, path: str) -> bool:
    """Return True if *source* contains any subprocess call (outside declared
    sink functions) that cannot be proven NOT to commit.

    Fail-closed: anything with a non-literal or unknown argv is reported,
    even if it is actually a read-only call.
    """
    tree = ast.parse(source)
    aliases = _resolve_aliases(tree)
    sink_qualnames = _sink_qualnames_for_path(path)
    funcs = _collect_functions(tree, "")

    # Walk all calls in the file that are subprocess invocations.
    for qname, func in funcs:
        if qname in sink_qualnames:
            continue  # This is a declared sink function — skip.
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_call(node, aliases):
                continue
            argv_node = _get_argv_node(node)
            if argv_node is None:
                # No argv provided — cannot prove safe.
                return True
            if not _first_arg_is_definitely_not_commit(argv_node):
                return True
    return False


def _execution_surfaces_in(source: str, path: str) -> bool:
    """Return True if *source* contains any os.system, os.popen, or
    asyncio.create_subprocess_* call."""
    tree = ast.parse(source)
    aliases = _resolve_aliases(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            # Module.method call: os.system, os.popen, asyncio.create_subprocess_exec, etc.
            if isinstance(func.value, ast.Name):
                mod_name = func.value.id
                if mod_name in aliases.os_module_names and func.attr in ("system", "popen"):
                    return True
                if mod_name in aliases.asyncio_module_names and func.attr.startswith(
                    "create_subprocess"
                ):
                    return True
        elif isinstance(func, ast.Name):
            # Directly-imported: from os import system; system(...)
            if func.id in aliases.os_system_names:
                return True
            if func.id in aliases.os_popen_names:
                return True
            if func.id in aliases.asyncio_create_subprocess_names:
                return True

    return False


def _shell_true_in(source: str, path: str) -> bool:
    """Return True if *source* contains any subprocess call with shell=True,
    outside a declared sink file."""
    if _is_sink_file(path):
        return False
    tree = ast.parse(source)
    aliases = _resolve_aliases(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_subprocess_call(node, aliases):
            continue
        if _has_shell_true(node):
            return True
    return False


# ---------------------------------------------------------------------------
# Fingerprint computation
# ---------------------------------------------------------------------------


def _fingerprints_for_file(source: str, path_str: str) -> list[Fingerprint]:
    """Return call-level fingerprints for every subprocess call in *source* that
    cannot be proven non-committing AND is outside a declared sink function.

    Used both by ``pristine_stray_git_report()`` (allowlist ignored) and by
    ``check_no_stray_git_commit()`` (allowlist applied).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    aliases = _resolve_aliases(tree)
    sink_qualnames = _sink_qualnames_for_path(path_str)
    funcs = _collect_functions(tree, "")

    fingerprints: list[Fingerprint] = []

    for qname, func in funcs:
        if qname in sink_qualnames:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_call(node, aliases):
                continue
            argv_node = _get_argv_node(node)
            if argv_node is not None and _first_arg_is_definitely_not_commit(argv_node):
                continue
            # This call is fail-closed (cannot prove non-commit).
            lineno_offset = node.lineno - func.lineno
            argv_repr = ast.dump(argv_node) if argv_node is not None else ""
            fingerprints.append((path_str, qname, lineno_offset, argv_repr))

    return fingerprints


# ---------------------------------------------------------------------------
# Public check functions
# ---------------------------------------------------------------------------


def pristine_stray_git_report() -> list[Fingerprint]:
    """Return ALL fail-closed fingerprints, ignoring the allowlist.

    This is what ``test_no_pin_is_stale`` compares against: every pin in
    ``PROVEN_NON_COMMIT`` must appear here.
    """
    results: list[Fingerprint] = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = str(py_file.relative_to(_SRC_ROOT))
        source = py_file.read_text(encoding="utf-8")
        results.extend(_fingerprints_for_file(source, rel))
    return results


def check_no_stray_git_commit() -> list[str]:
    """Return ``path::qualname`` strings for every unallowlisted stray subprocess
    call in src/.  An empty list means the tree is clean.

    Implementation: pristine_stray_git_report() minus PROVEN_NON_COMMIT.
    """
    pristine = pristine_stray_git_report()
    strays = [fp for fp in pristine if fp not in PROVEN_NON_COMMIT]
    # Return as path::qualname strings for readability in assert messages.
    return [f"{path}::{qualname}" for path, qualname, _, _ in strays]


def check_execution_surfaces() -> list[str]:
    """Return ``path::qualname`` strings for files in src/ that use alternative
    execution surfaces (os.system / os.popen / asyncio.create_subprocess_*)."""
    results: list[str] = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        rel = str(py_file.relative_to(_SRC_ROOT))
        if _execution_surfaces_in(source, rel):
            # Find which qualnames have the offending calls.
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            aliases = _resolve_aliases(tree)
            funcs = _collect_functions(tree, "")
            found_qualnames: list[str] = []
            for qname, func in funcs:
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    hit = False
                    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                        mod = fn.value.id
                        if mod in aliases.os_module_names and fn.attr in ("system", "popen"):
                            hit = True
                        if mod in aliases.asyncio_module_names and fn.attr.startswith(
                            "create_subprocess"
                        ):
                            hit = True
                    elif isinstance(fn, ast.Name):
                        if fn.id in aliases.os_system_names or fn.id in aliases.os_popen_names:
                            hit = True
                        if fn.id in aliases.asyncio_create_subprocess_names:
                            hit = True
                    if hit:
                        found_qualnames.append(qname)
            for qname in found_qualnames:
                results.append(f"{rel}::{qname}")
    return results


def check_no_shell_true() -> list[str]:
    """Return ``path::qualname`` strings for subprocess calls with shell=True
    in src/ outside declared sink files."""
    results: list[str] = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = str(py_file.relative_to(_SRC_ROOT))
        if _is_sink_file(rel):
            continue
        source = py_file.read_text(encoding="utf-8")
        if not _shell_true_in(source, rel):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        aliases = _resolve_aliases(tree)
        funcs = _collect_functions(tree, "")
        for qname, func in funcs:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and _is_subprocess_call(node, aliases)
                    and _has_shell_true(node)
                ):
                    results.append(f"{rel}::{qname}")
                    break
    return results


# ---------------------------------------------------------------------------
# Git-library checks (AST + manifest)
# ---------------------------------------------------------------------------

_GIT_LIBRARY_NAMES: frozenset[str] = frozenset(["git", "pygit2", "dulwich"])
_GIT_LIBRARY_MANIFEST_PATTERNS: frozenset[str] = frozenset(["gitpython", "pygit2", "dulwich"])


def _git_library_import_in(source: str, path: str) -> bool:
    """Return True if *source* contains an import of a git library (git/pygit2/dulwich).

    Matches on the top-level module name exactly — does NOT false-positive on
    names that merely contain those substrings (e.g. ``gitlab``, ``logging``).
    Checks both ``import git`` / ``import git.repo`` and ``from pygit2 import …``
    forms via AST nodes (never source-text substring matching).

    *path* is accepted for API symmetry with the other ``_*_in`` helpers and
    may be used for future exemption logic.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _GIT_LIBRARY_NAMES:
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod_top = (node.module or "").split(".")[0]
            if mod_top in _GIT_LIBRARY_NAMES:
                return True
    return False


def _manifest_declares_git_lib(text: str) -> bool:
    """Return True if manifest *text* (pyproject.toml or uv.lock) contains any
    of the git-library package names (gitpython / pygit2 / dulwich).

    The check is intentionally text-based and case-insensitive: TOML/lock files
    are not Python AST, and package names are case-insensitive in PEP 508 / pip.
    """
    lowered = text.lower()
    return any(pattern in lowered for pattern in _GIT_LIBRARY_MANIFEST_PATTERNS)


def check_no_git_library_imports() -> list[str]:
    """Return file paths where a git library (git/pygit2/dulwich) is imported."""
    results: list[str] = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        if _git_library_import_in(source, str(py_file.relative_to(_SRC_ROOT))):
            results.append(str(py_file.relative_to(_SRC_ROOT)))
    return results


def check_no_git_library_in_manifests() -> list[str]:
    """Return manifest files that declare a git library as a dependency."""
    results: list[str] = []
    for manifest in [_PYPROJECT, _UV_LOCK]:
        if not manifest.exists():
            continue
        text = manifest.read_text(encoding="utf-8")
        if _manifest_declares_git_lib(text):
            for pattern in _GIT_LIBRARY_MANIFEST_PATTERNS:
                if pattern in text.lower():
                    results.append(f"{manifest.name}::{pattern}")
    return results
