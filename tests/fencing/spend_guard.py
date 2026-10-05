"""Any FUNCTION that stamps a token field must also record spend.

The defect this prevents: `job.input_tokens = done.tokens_in or 0` written on the
happy path and nowhere else. Six sites had it, all losing spend in the same
direction. The per-job columns are a display cache now, so writing one without a
matching `record_spend` call re-opens exactly the gap the ledger closed.

FUNCTION-level, modelled on `tests/fencing/sink_guard.py`'s call-level pins: a
function either records spend in its OWN body, or its `(path, qualname)` is
pinned here with a reason. The pinned list can only shrink.

Module scope would not work. `turns/outcomes.py` has two independent terminal
writers and `evaluator/pass_runner.py` has a success path and a `_fail`; under a
per-file rule one `record_spend` anywhere in the file — in a dead branch, in an
unrelated helper, on the other terminal path — exempts every token assignment in
it, permanently.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# Attribute names that mean "I am writing token telemetry to a row".
TOKEN_FIELDS = frozenset({"input_tokens", "output_tokens", "tokens_in", "tokens_out"})

# Names that mean "I am recording spend", before import aliasing.
SPEND_WRITERS = frozenset({"record_spend", "record_usage"})

# Functions that stamp a token field and legitimately do NOT record spend.
# Keyed by (path relative to src/, dotted qualname). A reason is MANDATORY.
# Adding to this list is a reviewed diff.
#
# Keyed by FUNCTION, not by module, on purpose: a module-level pin would bless
# every sibling function in the same file forever.
PINNED: dict[tuple[str, str], str] = {
    (
        "grant_lab/proposal_assist/spend.py",
        "record_spend",
    ): (
        "this IS the writer — it constructs the LlmSpend row, which the ORM-constructor "
        "rule sees as a token stamp. Requiring it to call itself is nonsense; the "
        "one-writer property is asserted separately by llm_spend_writers()"
    ),
    (
        "grant_lab/proposal_assist/turns/submit.py",
        "submit_user_turn",
    ): (
        "writes an ESTIMATE, not spend: ChatTurn(tokens_in=...) at submit time is "
        "sum(attachment.tokens_estimate) + len(text)//4, computed before any hosted "
        "call is made. The real usage supersedes it at finalize (processor.py) and "
        "outcomes.py clears it, and both of those DO record. A ledger row here would "
        "charge the cap for a call that has not happened yet"
    ),
    (
        "grant_lab/proposal_assist/review/jobs.py",
        "rollup_run_totals",
    ): (
        "pure re-derivation of run totals from items that each already recorded "
        "their own ledger row; it has three call sites (pass_runner.py:174, "
        "pass_runner.py:364, jobs.py:184) and a reaped-then-late-finalized run "
        "reaches it twice, so an append-only row here would double-count"
    ),
}


def _collect_functions(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    prefix: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return all function defs under tree with their dotted qualname.

    Copied from tests/fencing/sink_guard.py:292 — same traversal, same qualnames.
    """
    results: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for stmt in getattr(tree, "body", []):
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


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes belonging to `func` itself, excluding nested def/class BODIES.

    Nested functions are collected separately by _collect_functions and judged in
    their own right, so a nested closure that records must not bless its parent.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


# The modules a real spend writer can come from. A call only counts if its name
# was BOUND by an import from one of these — see _spend_writer_names.
WRITER_MODULES = frozenset(
    {
        "grant_lab.proposal_assist.spend",
        "grant_lab.proposal_assist.citations.usage",
    }
)


# `record_usage` writes the CITATION ledger. It counts as a spend writer only for
# modules inside the citations package. Elsewhere it was a real bypass: a function
# could stamp `job.input_tokens` on an evaluator job while recording unrelated
# citation spend, and the gate went green.
CITATION_PREFIX = "grant_lab/proposal_assist/citations/"


def _writers_for(rel: str) -> frozenset[str]:
    if rel.startswith(CITATION_PREFIX):
        return SPEND_WRITERS
    return frozenset({"record_spend"})


def _spend_writer_names(
    tree: ast.AST, allowed: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve which names in this module actually mean "record spend".

    Returns (bare_names, module_aliases): a Call counts if it is `name(...)` with
    name in bare_names, or `alias.record_spend(...)` with alias in module_aliases.

    Accepting the bare string `record_spend` was a real bypass, measured against
    the shipped guard: a module that defined its own local `def record_spend`, or
    called an unrelated `something.record_spend()`, blessed every token stamp in
    it and the gate stayed green. The name must be BOUND BY AN IMPORT from a
    module in WRITER_MODULES. Honouring `as` aliases is the other direction —
    `from ...spend import record_spend as rs` was a false positive on correct
    code before it was handled.
    """
    names: set[str] = set()
    module_aliases: set[str] = set()
    # Names the module REBINDS after importing them. An import is not a promise
    # that the name still means the writer at the call site: `from ...spend import
    # record_spend` followed by `def record_spend(...)` passed the gate while
    # calling a local no-op. A rebound name is dropped from the writer set.
    shadowed: set[str] = set()

    def _bind(target: ast.AST) -> None:
        """Every name this target binds, however nested."""
        if isinstance(target, ast.Name):
            shadowed.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for el in target.elts:
                _bind(el)
        elif isinstance(target, ast.Starred):
            _bind(target.value)

    # MODULE-LEVEL bindings only — `tree.body`, not `ast.walk`. Walking the whole
    # tree pooled every function's parameters and locals into one set, so a single
    # helper with a parameter named `record_spend` invalidated the import for every
    # sibling in the file. Function-local bindings are handled per function by
    # _local_bindings, which is the scope Python actually uses.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _bind(node.target)
        elif isinstance(node, ast.Import):
            # `import fake as record_spend` rebinds the name to something that is
            # not the writer. The writer modules are excluded, or
            # `import grant_lab.proposal_assist.spend` would shadow the very
            # path it establishes — the control test caught exactly that.
            for alias in node.names:
                if alias.name in WRITER_MODULES:
                    continue
                shadowed.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # The same for `from unrelated import record_spend`. Only imports from a
            # WRITER module establish a writer; one from anywhere else REPLACES it,
            # and handling ast.Import but not ast.ImportFrom left that half open.
            mod = node.module or ""
            for alias in node.names:
                # Skip BOTH shapes that establish a writer: a name imported FROM a
                # writer module, and a name that IS one (`from grant_lab
                # .proposal_assist import spend`). Without the second, this rule
                # shadowed the very module alias it establishes — the same trap the
                # ast.Import branch above hit, caught by the same control test.
                if mod in WRITER_MODULES or f"{mod}.{alias.name}" in WRITER_MODULES:
                    continue
                shadowed.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in WRITER_MODULES:
                for alias in node.names:
                    if alias.name in allowed:
                        names.add(alias.asname or alias.name)
            # `from grant_lab.proposal_assist import spend` -> `spend.record_spend`
            for alias in node.names:
                if f"{mod}.{alias.name}" in WRITER_MODULES:
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in WRITER_MODULES:
                    continue
                # `import a.b.c as x` binds x -> match `x.record_spend(...)`.
                # Plain `import a.b.c` binds `a` and the real call is the fully
                # qualified `a.b.c.record_spend(...)`. Record the dotted path so
                # _dotted_name can match that chain — without this, legitimate
                # fully-qualified code was reported as an offender (measured).
                module_aliases.add(alias.asname or alias.name)
    # A dotted alias is only as good as its root: `import a.b.spend` binds `a`, so
    # `a = fake` invalidates the whole `a.b.spend.record_spend` path even though the
    # dotted string itself was never rebound. Plain set subtraction missed that.
    live_aliases = {
        alias for alias in module_aliases
        if alias not in shadowed and alias.split(".", 1)[0] not in shadowed
    }
    return frozenset(names - shadowed), frozenset(live_aliases)


def _target_stamps(target: ast.AST) -> bool:
    """True if this assignment target writes a token field.

    Recursive because a target can NEST: `(job.input_tokens, job.output_tokens)
    = pair` is a Tuple of Attributes, and `[a.tokens_in] = xs` is a List. An
    earlier version matched only a bare `ast.Attribute`, so all three of
    tuple-unpack, annotated and augmented assignment slipped through the gate —
    measured, not theorised. `+=` matters most: accumulating a token total is a
    natural way to write exactly the code this gate exists to catch.
    """
    if isinstance(target, ast.Attribute):
        return target.attr in TOKEN_FIELDS
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_stamps(el) for el in target.elts)
    if isinstance(target, ast.Starred):
        return _target_stamps(target.value)
    return False


def _non_assignment_stamp(node: ast.AST, model_names: frozenset[str]) -> bool:
    """Token writes that are not Python assignments at all.

    An outside review found three, none of which `_stamp_lines` could see because
    none produces an assignment target:
      * `update(Job).values(input_tokens=n)` — a Core UPDATE
      * `setattr(job, "input_tokens", n)` — the dynamic form
      * `bulk_update_mappings(Job, [{"input_tokens": n}])` — a dict key
    A second writer using any of these could omit record_spend and the gate would
    stay green, which is the one thing this gate must never do.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # .values(input_tokens=...) and .values(**{"input_tokens": ...}) on any chain.
    # The unpacked form has kw.arg is None and the names live in the dict literal —
    # an earlier comment here claimed to cover it while the code filtered it out,
    # which is worse than not claiming it.
    if isinstance(func, ast.Attribute) and func.attr == "values":
        for kw in node.keywords:
            if kw.arg in TOKEN_FIELDS:
                return True
            if kw.arg is None and isinstance(kw.value, ast.Dict):
                for key in kw.value.keys:
                    if isinstance(key, ast.Constant) and key.value in TOKEN_FIELDS:
                        return True
                # KNOWN LIMIT, measured. `fields = {...}; .values(**fields)` hides
                # its keys, and flagging every opaque `**` was tried and reverted:
                # it reported FOUR unrelated functions (`values(**payload)` on
                # calls, alerts and annotations — tables with no token columns)
                # to catch a shape nobody writes. Pins are how gates rot, so the
                # honest limit beats four standing pins. An inline dict IS caught.
    # Model(input_tokens=n) — building a ROW with token fields. Restricted to
    # classes imported from grant_lab.db.models, because "any call with a
    # tokens_in= keyword" is far too wide: it reported twelve legitimate
    # functions, `record_spend` itself among them, since `tokens_in` IS in
    # TOKEN_FIELDS. Measured against the real tree, not reasoned about — the
    # first draft of this rule shipped a comment asserting the opposite.
    if model_names and any(kw.arg in TOKEN_FIELDS for kw in node.keywords if kw.arg):
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if called in model_names:
            return True
    # setattr(obj, "input_tokens", value) — only a literal name is checkable
    if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
        target = node.args[1]
        if isinstance(target, ast.Constant) and target.value in TOKEN_FIELDS:
            return True
    # bulk_*_mappings(Model, [{"input_tokens": n}]) — dict-literal keys anywhere
    # in the call's arguments.
    if isinstance(func, ast.Attribute) and func.attr.startswith("bulk_"):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                for key in sub.keys:
                    if isinstance(key, ast.Constant) and key.value in TOKEN_FIELDS:
                        return True
    return False


def _model_names(tree: ast.AST) -> frozenset[str]:
    """Names bound by an import from grant_lab.db.models — the ORM rows."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == (
            "grant_lab.db.models"
        ):
            for alias in node.names:
                out.add(alias.asname or alias.name)
    return frozenset(out)


def _local_bindings(nodes: list[ast.AST], func: ast.AST) -> frozenset[str]:
    """Names bound INSIDE one function — its parameters and its own bindings.

    Kept separate from the module-level shadow set because pooling them was a
    false-positive generator: one helper with a parameter named `record_spend`
    invalidated the import for every sibling function in the file. A local
    binding shadows the writer in ITS function and nowhere else.
    """
    out: set[str] = set()

    def _bind(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            out.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for el in target.elts:
                _bind(el)
        elif isinstance(target, ast.Starred):
            _bind(target.value)

    args = getattr(func, "args", None)
    if args is not None:
        for arg in [
            *args.posonlyargs, *args.args, *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ]:
            out.add(arg.arg)
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            _bind(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Import):
            # A function-local `import fake as record_spend` rebinds the name for
            # THIS function while the module-level import stays live in `writer_names`.
            # Without this branch the function's unrelated call satisfied the gate.
            # Writer modules are excluded for the same reason as at module level:
            # a local `import grant_lab.proposal_assist.spend` ESTABLISHES the
            # writer, and shadowing it would break the very path it opens.
            for alias in node.names:
                if alias.name in WRITER_MODULES:
                    continue
                out.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # Same for `from unrelated import record_spend`, skipping BOTH shapes
            # that establish a writer — a name imported FROM a writer module, and a
            # name that IS one. Handling ast.Import alone left half of it open; that
            # exact asymmetry was the module-level bug this mirrors.
            mod = node.module or ""
            for alias in node.names:
                if mod in WRITER_MODULES or f"{mod}.{alias.name}" in WRITER_MODULES:
                    continue
                out.add(alias.asname or alias.name)
    return frozenset(out)


def _stamp_lines(nodes: list[ast.AST], model_names: frozenset[str]) -> list[int]:
    lines: list[int] = []
    for node in nodes:
        if _non_assignment_stamp(node, model_names):
            lines.append(node.lineno)
            continue
        # Assign has a LIST of targets (`a = b = value`); AnnAssign and AugAssign
        # each have exactly one. All three write the attribute.
        if isinstance(node, ast.Assign):
            targets: list[ast.AST] = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(_target_stamps(t) for t in targets):
            lines.append(node.lineno)
    return sorted(lines)


def _dotted_name(node: ast.AST) -> str | None:
    """`a.b.c` -> "a.b.c" for Name/Attribute chains; None for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _records_spend(
    nodes: list[ast.AST],
    writers: tuple[frozenset[str], frozenset[str]],
    bare_writers: frozenset[str],
) -> bool:
    bare, module_aliases = writers
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in bare:
                return True
            # Only `<imported module>.record_spend(...)`, where the module part
            # is a name or dotted chain bound by a real import. An arbitrary
            # attribute call that merely ENDS in record_spend does not count.
            if isinstance(func, ast.Attribute) and func.attr in bare_writers:
                base = _dotted_name(func.value)
                if base is not None and base in module_aliases:
                    return True
    return False


def offenders() -> list[str]:
    """`path::qualname` for every function that stamps, records nothing, is unpinned."""
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        allowed = _writers_for(rel)
        writer_names = _spend_writer_names(tree, allowed)
        models = _model_names(tree)
        for qname, func in _collect_functions(tree, ""):
            if (rel, qname) in PINNED:
                continue
            nodes = _own_nodes(func)
            local = _local_bindings(nodes, func)
            live = (
                frozenset(writer_names[0] - local),
                frozenset(a for a in writer_names[1] if a.split(".", 1)[0] not in local),
            )
            if _stamp_lines(nodes, models) and not _records_spend(nodes, live, allowed):
                out.append(f"{rel}::{qname}")
    return sorted(out)


def stale_pins() -> list[str]:
    """Pins whose function no longer exists, or no longer stamps a token field."""
    stale: list[str] = []
    for rel, qname in PINNED:
        path = SRC / rel
        if not path.exists():
            stale.append(f"{rel}::{qname}")
            continue
        tree = ast.parse(path.read_text())
        funcs = dict(_collect_functions(tree, ""))
        func = funcs.get(qname)
        if func is None or not _stamp_lines(_own_nodes(func), _model_names(tree)):
            stale.append(f"{rel}::{qname}")
    return sorted(stale)


# The package re-export AND the class's own module: importing from the
# submodule was invisible (review 2026-10-05).
_LLM_SPEND_MODULES = ("grant_lab.db.models", "grant_lab.db.models.spend")


def llm_spend_writers() -> list[str]:
    """Modules under src/ that touch the `LlmSpend` class at all.

    The plan's central claim is "exactly one writer". Checking for a
    `LlmSpend(...)` CONSTRUCTOR call could never assert that: an ORM constructor
    is only one of the ways to write a row. `session.execute(insert(LlmSpend)...)`
    and `bulk_insert_mappings(LlmSpend, ...)` contain no constructor call and
    named no offender.

    So this does not look for a call shape at all — it reports any module that
    REFERENCES the name, however it was imported (bare, aliased, or qualified as
    `models.LlmSpend`). That over-reports by design: a module that merely reads
    the table is also reported, and the test's expected list is the audit. Two
    modules are allowed: `db/models/spend.py` declares the class, and
    `proposal_assist/spend.py` is the writer.
    """
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        local_names: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    if mod in _LLM_SPEND_MODULES and alias.name == "LlmSpend":
                        local_names.add(alias.asname or alias.name)
                    elif f"{mod}.{alias.name}" in _LLM_SPEND_MODULES:
                        module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _LLM_SPEND_MODULES:
                        module_aliases.add(alias.asname or alias.name)
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in local_names or (
                isinstance(node, ast.Attribute)
                and node.attr == "LlmSpend"
                and _dotted_name(node.value) in module_aliases
            ):
                hit = True
            elif isinstance(node, ast.ClassDef) and node.name == "LlmSpend":
                hit = True  # the declaration itself
            if hit:
                out.append(str(path.relative_to(SRC)))
                break
    return sorted(out)
