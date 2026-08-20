# Fence F3d2 — The Submitters and the Reads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the three submitter effect sites, the two `references.py` accounting
sites and `context.py`'s two reads to the ports, taking the exemption list from nine
modules to five — and make "converted" a thing the inventory can *measure* rather than a
box a human ticks.

**Architecture:** F3d1 built the spine and converted nothing. This slice is the first that
actually moves tool code onto the ports. It cannot start with a conversion, because
**converting a site deletes its own inventory row**: `detectors.py` records a `tool_commit`
only when it sees `<x>.commit()` under `tools/`, and the canonical converted form has no
`.commit()` at all. So the first three tasks teach the detector what a converted site looks
like, make the `fenced` flag mechanical instead of hand-asserted, and stop the F3d1
identity set from encoding slice ownership. Only then do the six conversions land.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest.

**Revision 1 (2026-08-20).** A Codex review of revision 0 returned eight findings, five of
them P1. All eight were verified against the code and all eight were real:

- **The re-slicing task was built on a misread and would have broken the gate.** The ingest
  commit at `references.py:580` sits inside `_do_fetch_and_ingest` (`:551`), which is its
  OWN inventory row, already F3e. So `fetch_reference_impl` holds only the reserve, and
  after conversion it measures `fenced: true` — under the `pending` F3e that revision 0
  moved it to, `test_each_slice_matches_its_declared_status` fails. **Task 3 is deleted**
  and the remaining tasks renumber.
- **36 existing direct callers were unaccounted for.** `ports` and `invocation` are
  required keywords, so every test calling an impl directly raises `TypeError`. Revision 0
  claimed those tests "must pass untouched". GC-13 and a migration step per task.
- **The accounting interleaving fixture could not be built.** `uq_turn_jobs_running`
  (`models.py:887-891`) forbids two running turns per workspace, and `refund_fetch_slot`
  ignores a non-running turn — so the old row's expected count of 0 was impossible.
- **The new query method is not covered automatically.** `test_ports_dto_contract.py`
  requires an explicit `CASES` entry per method; without one Task 8 goes red, and adding
  one adds a parametrised node.
- **A user-visible error string was truncated**, dropping the manual-upload guidance.
  Existing coverage matches a substring, so the regression would have shipped green.
- Two orphaned function-local imports (`ruff` F401); a detector rule that accepted any
  `<anything>.effects.X(...)` receiver; and two mutation steps that did not falsify the
  fence GC-4 requires.

Two lessons, both already on this project's record: **a nested function is a different
identity**, and **verify a claim about a file against the file**.

**Spec:** `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md` (revision
8). §7's F3d paragraph is the charter — "converts … `submit_review_impl`,
`submit_scorecard_impl` and `submit_uncited_claims_impl` to effect operations, the two
`references.py` accounting sites to owner-bound operations, and every remaining read to a
query port returning DTOs". §5 supplies the binding proof requirements. §3.2/§3.3 are the
port contracts.

---

## What the survey found, and what it changed

Three things were discovered against the real code, not read off the spec. Each one
changes the slice, and each is the reason a task exists that the spec does not name.

**1. Conversion is invisible to the detector, so it erases its own row.**
`tests/fencing/detectors.py:83-90` records `KIND_TOOL_COMMIT` for any `ast.Call` whose
func is an attribute named `commit`, when the path is under `tools/`. The converted form
is `ports.effects.submit_scorecard(stamp, cmd)` — no `.commit()` anywhere. `merge()`
(`inventory.py:290-300`) builds rows **only from what the scan found**, so the row is
dropped. If it was `fenced: false` it goes silently; if `fenced: true`,
`FencedSiteVanished` raises. Neither preserves it. So F3d1's
`test_every_f3d_identity_is_still_in_the_inventory` would go red on the first conversion
in this slice. **Detection has to be redesigned before conversion** — the same ordering
F2→F3 already paid for once.

**2. `fenced: true` is an unfalsifiable human assertion.**
Nothing in `tests/fencing/` checks that a row marked `fenced: true` uses a port. `merge()`
copies `prev["fenced"]` forward verbatim. A slice could be marked complete with every row
hand-flipped and no code converted at all. That is this project's recurring defect. Since
the detector must learn to see a port call anyway (finding 1), computing the flag from
what it sees costs almost nothing and turns `test_each_slice_matches_its_declared_status`
into a real gate. Scoped to `tool_commit` only: all nine `tool_commit` rows are
`fenced: false` today, and F3a's nine `claim_fn`/`tool_context` rows (all `fenced: true`)
are untouched.

**3. All five F3d2 rows CAN go green, because the ingest commit is a different row.**
`references.py` holds three `tool_commit` identities, not two: `fetch_reference_impl`
(the accounting reserve at `:475`), `fetch_reference_impl._refund_fetch_slot` (`:508`),
and `fetch_reference_impl._do_fetch_and_ingest` (`:580`) — the last nested in its own
function at `references.py:551` and already assigned to **F3e**. The detector keys on the
lexical qualname, so converting the reserve leaves `fetch_reference_impl` holding a port
call and no raw commit of its own. It measures fenced, it stays in F3d2, and F3d2 closes.

Revision 1 corrects this. Revision 0 read `:580` as sitting directly inside
`fetch_reference_impl`, concluded the row could never go green, and added a whole task to
move it to F3e and re-key `F3D_IDENTITIES` so that was legal. That task would have
*created* the failure it claimed to prevent: a row measuring `fenced: true` under a
`pending` F3e fails `test_each_slice_matches_its_declared_status`. The task is gone.

**What this slice does NOT take.** `history.py` stays pinned. Its only blocker is the
`repo_dir` locator and it owns zero inventory rows, so converting it would fence nothing
and would need a read-only git port that no spec section describes. `repo_dir` retires
with the other capability holders in F3f. Pins therefore go **9 → 5**, not 9 → 4.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **GC-1 — This slice converts six call sites and nothing else.** `submit_scorecard_impl`,
  `submit_review_impl`, `submit_uncited_claims_impl`, the reserve inside
  `fetch_reference_impl`, `fetch_reference_impl._refund_fetch_slot`, and `context.py`'s two
  reads. `tools/citations.py`, `tools/proposal.py`, `tools/audit.py` and `tools/history.py`
  are not touched. At the end, `TOOLS_NOT_YET_CONVERTED` has **five** entries:
  `audit.py`, `citations.py`, `history.py`, `proposal.py`, `references.py`.
- **GC-2 — Detection lands before conversion.** Tasks 1 and 2 must be complete and
  committed before Task 4. A conversion merged ahead of them deletes its own inventory row and takes
  F3d1's anti-vacuity ratchet red. If you find yourself editing `F3D_IDENTITIES` to make a
  test pass, stop: that is the failure the ratchet exists to catch.
- **GC-3 — A converted tool may import stdlib and `call_manager.proposal_assist.ports`,
  nothing else.** That includes the type of its own first parameter: `ToolContext` lives in
  `tools/registry.py`, so a converted impl must leave `ctx` unannotated. It may still READ
  `ctx.eval_rubric_criteria`, `ctx.review_run_item_id` and friends — those are not resource
  locators. It may NOT name `repo_dir`, `session_factory`, `settings` or `workspaces_dir`,
  as a parameter, attribute or argument (`tests/fencing/import_gate.py`, the locator rule).
- **GC-4 — Per-site proof requires the compensation disabled** (spec §5, and
  `2026-08-17` §9). Every conversion test in this slice must include one case where the
  tool's own guard is neutralised and the fence alone is what stops the write. "A
  conversion test that passes with the fence removed is measuring the compensation, not the
  fence." This is the technique that exposed C1's decorative tests. A task whose fence test
  still passes with the port's generation predicate removed has not proven anything.
- **GC-5 — Accounting is proven by the interleaving it exists for** (spec §5): reserve a
  slot at generation G, delete and restore, start a new turn at G+1, and assert the old
  actor's refund does not touch the new turn's row. Accounting operations are
  DELIBERATELY unfenced (spec §3.3) — do not add a generation predicate to them and do not
  write a test that demands one.
- **GC-6 — Behaviour is preserved exactly, including the error strings.**
  `port_impl/effects.py` already reproduces each tool's guards and refusal messages
  verbatim, in the same order. A conversion that changes a message the agent sees is a
  regression, and the existing tool tests are what catch it. Do not "improve" a string.
- **GC-7 — The exemption sets shrink, never grow.** `TOOLS_NOT_YET_CONVERTED` is a subset
  check: a module may carry a pin it no longer needs, but a NEW violation is red. When a
  module's violations shrink, shrink its pinned set in the same commit — a stale pin is a
  licence nobody revoked.
- **GC-8 — Test command.** `uv run pytest -n 16 --dist loadfile`. Never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Baseline on master before this
  branch: **3737 passed, 1 skipped** (`684d991`). The `1 skipped` is an
  `ANTHROPIC_API_KEY`-gated test — read it as the signal, not the total.
  `tests/fencing` alone is 84 tests in ~3.5s with no database.
- **GC-8a — If you work in a worktree, `uv run` is a trap.** A fresh worktree has no
  `.venv` and `requires-python = ">=3.12"`, so bare `uv sync` resolves to Python 3.14. Use
  `uv sync --python 3.12`, then confirm with
  `.venv/bin/python -c "import call_manager, pathlib; print(pathlib.Path(call_manager.__file__).resolve())"`
  that it points at THIS worktree's `src/` — if it points at the main tree, your source
  edits do nothing while the tests still pass.
- **GC-9 — `ruff check .` is the lint gate. `ruff format` is NOT** — do not run it and do
  not reformat files to satisfy it.
- **GC-10 — Mutation testing happens AFTER the task's commit, never before, and the restore
  is verified by a command that FAILS.** `git checkout <path>` cannot restore a file the
  task itself created — it errors, and piped through anything the non-zero exit is masked.
  So commit the green implementation first, then mutate, then:

  ```bash
  git checkout <path>
  git diff --exit-code HEAD -- <path>
  ```

  Never `git status --porcelain`: it exits 0 whether the tree is clean or filthy.
- **GC-13 — Every conversion breaks its existing direct callers, and the task that
  converts owns them.** `ports` and `invocation` are REQUIRED keyword parameters, so every
  test that calls an impl directly raises `TypeError` the moment its task lands. There are
  **36 such call sites** across six files:

  | file | impl |
  |---|---|
  | `tests/proposal_assist/evaluator/test_submit_scorecard.py` | `submit_scorecard_impl` |
  | `tests/proposal_assist/review/test_submit_review.py` | `submit_review_impl` |
  | `tests/proposal_assist/uncited_claims/test_submit.py` | `submit_uncited_claims_impl` |
  | `tests/proposal_assist/tools/test_references_fetch_tool.py` | `fetch_reference_impl` |
  | `tests/proposal_assist/test_references_admission.py` | `fetch_reference_impl` |
  | `tests/proposal_assist/test_tools_context.py` | `get_call_details_impl`, `get_profile_impl` |

  Each conversion task migrates its own file in the SAME commit. Migrating means passing
  `ports=build_ports(<the same session_factory the test already has>)` and, where the impl
  takes one, `invocation=snapshot_invocation(ctx)` — NOT rewriting what the test asserts.
  An assertion that has to change is a behaviour change and violates GC-6: stop and report
  it instead. Find every site in a file with:

  ```bash
  grep -n "_impl(" <file>
  ```
- **GC-12 — There is ONE database fixture and a seed module. Use them; do not invent
  fixtures.** `pg_session_factory` (`tests/conftest.py:70`) is the only DB fixture. Every
  row this slice needs is built by `tests/proposal_assist/f3b_seeds.py`, whose complete API
  is:

  ```python
  seed_workspace(session, *, generation: int = 0) -> uuid.UUID
  bump_generation(session, workspace_id, *, to: int) -> None   # does NOT commit
  seed_evaluator_job(session, workspace_id, *, generation: int) -> uuid.UUID
  seed_uncited_job(session, workspace_id, *, generation: int) -> uuid.UUID
  seed_review_item(session, workspace_id, *, generation: int) -> tuple[uuid.UUID, uuid.UUID]
  seed_running_turn(session, workspace_id) -> uuid.UUID
  ```

  `seed_workspace` creates a workspace with EMPTY snapshots, which is what Task 8's
  "unlinked" case needs. Read `tests/proposal_assist/test_f3b_lock_cases.py:46-54` for the
  established usage before writing a test. If you need a row shape the module does not
  seed, ADD a helper there rather than a fixture in the test file — that is where the next
  slice will look for it.
- **GC-11 — A mutation must fail for the reason you claim.** F3d1's dry run found three
  mutation steps that failed for an unrelated reason (a circular import that aborted
  collection, a fork that moved both sides of an identity check together, a mutation that
  failed identically with and without the mechanism it was justifying). When a step says
  "Expected: FAIL on X", confirm the named test failed with the named message — not merely
  that something went red.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/ports/scoring.py` | `compute_overall_band` — the pure weighted-band rollup a converted `submit_scorecard_impl` still has to perform. Stdlib only. |
| `tests/fencing/fixtures/converted_site.py` | Positive fixture: a tool-shaped module whose only write is a port call. The detector must record a `tool_commit` for it. |
| `tests/fencing/fixtures/half_converted_site.py` | Negative fixture: one qualname with BOTH a port call and a residual `.commit()`. Recorded, but NOT fenced. |
| `tests/proposal_assist/test_f3d2_conversions.py` | The slice's own tests: each converted site against its port, each with the compensation disabled. |

**Modified:**

| File | Change |
|---|---|
| `tests/fencing/detectors.py` | `find_sites` also records a `tool_commit` for a port effect/accounting call; new `find_fenced_tool_commits` reports which qualnames are port-only. |
| `tests/fencing/inventory.py` | `merge` COMPUTES `fenced` for `tool_commit` rows instead of copying it forward; four exemptions retire; `SLICE_STATUS["F3d2"]` closes. |
| `tests/fencing/fence_inventory.json` | The five F3d2 rows end `fenced: true`, written by regeneration and never by hand. |
| `tests/fencing/test_fence_coverage.py` | Gains the measured-fenced gate and the slice-closing assertions. |
| `src/call_manager/proposal_assist/evaluator/scoring.py` | Re-exports `compute_overall_band` from `ports/scoring.py`. |
| `src/call_manager/proposal_assist/ports/queries.py` | Gains `WorkspaceSnapshotView` and `get_workspace_snapshot`. |
| `src/call_manager/proposal_assist/port_impl/queries.py` | Implements `get_workspace_snapshot`. |
| `proposal_assist/tools/scorecard.py`, `review_submit.py`, `uncited_claims.py`, `context.py` | Converted: no session, no models, no `ToolContext` annotation. |
| `proposal_assist/tools/references.py` | Its two accounting sites call the port; ingest is untouched (F3e). |
| `proposal_assist/tools/registry.py` (`_collect_tools`) | Six call sites gain `pass_ports=True` and, where the effect needs a stamp, `pass_invocation=True`. |

**Deliberately NOT done here** (named so a reviewer does not read them as gaps):
`tools/history.py` keeps its `repo_dir` pin — zero inventory rows, and `repo_dir` retires
in F3f; `tools/citations.py` and `tools/proposal.py` are F3d3's; `references.py`'s ingest
commit (`:580`) and its eleven ingest-related pins are F3e's; `session_factory` is NOT
removed from `ToolContext` (spec puts that in F3f's final commit).

---

### Task 1: The detector learns what a converted site looks like

Today a `tool_commit` is recorded only for `<x>.commit()` under `tools/`
(`detectors.py:83-90`). Every conversion in this slice removes exactly that call, so
without this task each conversion deletes its own inventory row and F3d1's ratchet goes
red. Detection first — this is the ordering F2→F3 already paid for once.

The method names come from the Protocols themselves, so the list cannot drift away from
the ports. `Effects` and `Accounting` are `runtime_checkable` Protocols, so
`__protocol_attrs__` is the supported way to read their member names (F3c established this
idiom over `vars()`, which also returns dunders and helpers).

**Files:**
- Create: `tests/fencing/fixtures/converted_site.py`
- Create: `tests/fencing/fixtures/half_converted_site.py`
- Modify: `tests/fencing/detectors.py`
- Modify: `tests/fencing/test_detectors.py`

**Interfaces:**
- Consumes: `Effects` and `Accounting` from `call_manager.proposal_assist.ports`.
- Produces:
  - `PORT_WRITE_METHODS: frozenset[str]` in `tests/fencing/detectors.py`.
  - `find_sites` unchanged in signature; it now also records `KIND_TOOL_COMMIT` for a port
    write call under `tools/`.

- [ ] **Step 1: Write the failing test**

Append to `tests/fencing/test_detectors.py`:

```python
def test_a_port_write_call_is_a_tool_commit_site():
    """The whole reason Task 1 exists. The canonical converted form has NO
    .commit() anywhere, so a detector that only knows about .commit() drops the
    row the moment the site is converted — and merge() rebuilds rows only from
    what the scan found."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        "async def submit_scorecard_impl(ctx, *, ports, invocation):\n"
        "    ports.effects.submit_scorecard(invocation.stamp(), cmd)\n"
    )
    sites = find_sites(src, "src/call_manager/proposal_assist/tools/scorecard.py")
    assert (KIND_TOOL_COMMIT, "submit_scorecard_impl") in {
        (s.kind, s.symbol) for s in sites
    }


def test_an_accounting_call_is_a_tool_commit_site_too():
    """Accounting operations are unfenced by design (spec 3.3) but they are still
    writes, and they still own inventory rows."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        "async def fetch_reference_impl(ctx, *, ports):\n"
        "    def _refund_fetch_slot():\n"
        "        ports.accounting.refund_fetch_slot(job_id)\n"
    )
    sites = find_sites(src, "src/call_manager/proposal_assist/tools/references.py")
    assert (KIND_TOOL_COMMIT, "fetch_reference_impl._refund_fetch_slot") in {
        (s.kind, s.symbol) for s in sites
    }


def test_a_port_call_outside_tools_is_not_a_site():
    """Same scoping as .commit(): the inventory is about TOOL code. port_impl and
    the domain layer call these methods constantly and are not tool sites."""
    from tests.fencing.detectors import find_sites

    src = "def helper(ports):\n    ports.effects.submit_review(stamp, cmd)\n"
    assert find_sites(src, "src/call_manager/proposal_assist/proposals/accepting.py") == set()


def test_the_port_method_list_is_derived_from_the_protocols():
    """A hand-written list drifts the first time a port gains a method, and the
    drift is silent: the new effect simply stops being detected."""
    from call_manager.proposal_assist.ports.accounting import Accounting
    from call_manager.proposal_assist.ports.effects import Effects
    from tests.fencing.detectors import PORT_WRITE_METHODS

    assert PORT_WRITE_METHODS == frozenset(Effects.__protocol_attrs__) | frozenset(
        Accounting.__protocol_attrs__
    )
    assert "submit_scorecard" in PORT_WRITE_METHODS
    assert "refund_fetch_slot" in PORT_WRITE_METHODS
    # Queries are reads. Fencing reads is explicitly out of scope (spec §8).
    assert "get_reference" not in PORT_WRITE_METHODS


def test_a_lookalike_receiver_is_not_a_site():
    """The chain root must be the INJECTED `ports` name. Matching on
    `<anything>.effects.submit_review(...)` would let a local helper — or a test
    double a tool happened to hold — mint an inventory row, and worse, mark a
    qualname fenced that never touched a real capability."""
    from tests.fencing.detectors import find_sites

    path = "src/call_manager/proposal_assist/tools/scorecard.py"
    src = (
        "async def submit_scorecard_impl(ctx):\n"
        "    fake = _Local()\n"
        "    fake.effects.submit_scorecard(stamp, cmd)\n"
    )
    assert find_sites(src, path) == set()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/fencing/test_detectors.py -q -k "port_write or accounting_call or outside_tools or derived_from or lookalike" > /tmp/f3d2-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-1.txt
```
Expected: `PYTEST_EXIT=1`, with `ImportError: cannot import name 'PORT_WRITE_METHODS'`
for the fourth test and assertion failures for the first two.

- [ ] **Step 3: Teach the detector**

In `tests/fencing/detectors.py`, add to the imports at the top of the file:

```python
from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects
```

and, beside the other module-level constants:

```python
#: Every WRITE a converted tool can perform through the ports, read off the
#: Protocols so the list cannot drift away from them. Queries are excluded on
#: purpose: this design does not fence reads (spec §8), and a read is not a site.
PORT_WRITE_METHODS: frozenset[str] = frozenset(Effects.__protocol_attrs__) | frozenset(
    Accounting.__protocol_attrs__
)

#: The two bundle attributes a write is reached through.
_PORT_BUNDLE_ATTRS = frozenset({"effects", "accounting"})

#: The name the bundle arrives under. _safe_tool passes it as the keyword `ports`
#: (F3d1), so the canonical converted call is exactly `ports.effects.X(...)` or
#: `ports.accounting.X(...)`. Matching the METHOD NAME alone would record any local
#: helper called `submit_review`; matching `<anything>.effects.X` would let a test
#: double mint a row and mark a qualname fenced that never held a capability.
_PORT_ROOT = "ports"
```

Add the shared predicate at module level — Task 2 needs the identical rule, and two
copies of it would drift:

```python
def _is_port_write(node: ast.Call) -> bool:
    """`ports.effects.X(...)` or `ports.accounting.X(...)`, and nothing else.

    All three parts are load-bearing: the method must be a real port write, the
    bundle attribute must be `effects` or `accounting`, and the ROOT must be the
    injected `ports` name.
    """
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in PORT_WRITE_METHODS
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr in _PORT_BUNDLE_ATTRS
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == _PORT_ROOT
    )
```

Then, inside `visit`, extend the `ast.Call` branch. The existing `.commit()` check stays
exactly as it is; this is a second condition beside it:

```python
        elif isinstance(node, ast.Call):
            qual = ".".join(stack) if stack else "<module>"
            if (
                _TOOLS_DIR in norm
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit"
            ):
                sites.add(Site(KIND_TOOL_COMMIT, path, qual))

            # F3d2: the converted form. A tool that has been moved onto the ports
            # has no .commit() at all, so without this the row vanishes the moment
            # the site is converted — and merge() rebuilds only from the scan.
            if _TOOLS_DIR in norm and _is_port_write(node):
                sites.add(Site(KIND_TOOL_COMMIT, path, qual))
```

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d2-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-1.txt
```
Expected: `PYTEST_EXIT=0`, 89 passed — the 84 that existed plus these five. No existing
fencing test changes, because no production tool calls a port yet.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add tests/fencing/detectors.py tests/fencing/test_detectors.py
git commit -m "test(fencing): a port write is a tool_commit site, so conversion stops erasing its own row (F3d2 Task 1)"
```

- [ ] **Step 6: Prove the detector would miss the converted form without this**

In `tests/fencing/detectors.py`, change `_PORT_BUNDLE_ATTRS` to
`frozenset({"nothing"})`.

Run:
```bash
uv run pytest tests/fencing/test_detectors.py -q > /tmp/f3d2-1m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-1m.txt
```
Expected: FAIL on `test_a_port_write_call_is_a_tool_commit_site` AND
`test_an_accounting_call_is_a_tool_commit_site_too`, both with the identity missing from
the recorded set. `test_a_port_call_outside_tools_is_not_a_site` must still PASS — it is
the scoping guard and it is not what this mutation is about.

```bash
git checkout tests/fencing/detectors.py
git diff --exit-code HEAD -- tests/fencing/detectors.py
```

---

### Task 2: `fenced` is measured, not asserted

`merge()` copies `prev["fenced"]` forward verbatim (`inventory.py:300`), and NOTHING in
`tests/fencing/` checks that a `fenced: true` row uses a port. A slice could be marked
complete with every row hand-flipped and no code converted. Since Task 1 taught the
detector to see a port call, computing the flag costs one function and turns
`test_each_slice_matches_its_declared_status` into a real gate.

**Scoped to `tool_commit` deliberately.** All nine `tool_commit` rows are `fenced: false`
today, so this changes no stored value on landing. F3a's nine `claim_fn` and `tool_context`
rows are `fenced: true` and keep the stored flag — "is this a port call" is meaningless for
a claim function, and inventing a rule for it is out of scope.

The rule is **port call AND no raw commit**. A qualname holding both is half-converted, and
half-converted is not converted — that is exactly `fetch_reference_impl` after this slice.

**Files:**
- Create: `tests/fencing/fixtures/converted_site.py`
- Create: `tests/fencing/fixtures/half_converted_site.py`
- Modify: `tests/fencing/detectors.py`
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/test_fence_coverage.py`

**Interfaces:**
- Consumes: `PORT_WRITE_METHODS` and `find_sites` (Task 1).
- Produces:
  - `find_fenced_tool_commits(source: str, path: str) -> set[tuple[str, str]]` in
    `tests/fencing/detectors.py` — `(path, symbol)` for every qualname under `tools/`
    that makes a port write and NO raw `.commit()`.
  - `scan_tree()` returns `tuple[set[Site], set[tuple[str, str]]]` — the sites and the
    fenced identities.
  - `merge(previous, found, renames, fenced_ids)` — new fourth parameter.

- [ ] **Step 1: Write the fixtures**

Create `tests/fencing/fixtures/converted_site.py`:

```python
"""POSITIVE fixture: fully converted. One port write, no session, no commit.

Lives under tests/fencing/fixtures/ and is never imported — find_fenced_tool_commits
is handed its source with a FAKE tools/ path, because the rule is scoped by path.
"""


async def submit_scorecard_impl(ctx, *, ports, invocation):
    ports.effects.submit_scorecard(invocation.stamp(), cmd)
    return {"stored": True}
```

Create `tests/fencing/fixtures/half_converted_site.py`:

```python
"""NEGATIVE fixture: half-converted, which is not converted.

One qualname holding BOTH a port write and a residual raw commit. No site in the
tree has this shape after F3d2 — it is the shape a PARTIAL conversion produces, and
the rule exists so that a partial conversion cannot be reported as a finished one.
A rule that only looked for the port call would mark such a qualname fenced and let
its slice declare itself complete over code that still commits directly.
"""


async def fetch_reference_impl(ctx, *, ports, invocation):
    ports.accounting.reserve_fetch_slot(invocation.active_turn_job_id)
    with ctx.session_factory() as s:
        s.commit()
    return {"ok": True}
```

- [ ] **Step 2: Write the failing test**

Append to `tests/fencing/test_fence_coverage.py`:

```python
def test_a_fully_converted_site_is_measured_as_fenced():
    """`fenced` stops being a box a human ticks. Before this, merge() copied the
    stored boolean forward and nothing checked it against the code — a slice could
    be marked complete with every row hand-flipped and nothing converted."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "converted_site.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/tools/scorecard.py"
    assert find_fenced_tool_commits(src, path) == {(path, "submit_scorecard_impl")}


def test_a_half_converted_site_is_not_fenced():
    """Port call AND a residual raw commit — a PARTIAL conversion. Nothing in the
    tree has this shape after F3d2, which is exactly why it needs a fixture: the
    rule has to refuse it before a future slice produces one and reports itself
    complete over code that still commits directly."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits, find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "half_converted_site.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/tools/references.py"
    # The row still EXISTS — it is a write site either way.
    assert any(s.symbol == "fetch_reference_impl" for s in find_sites(src, path))
    # It is simply not fenced.
    assert find_fenced_tool_commits(src, path) == set()


def test_the_inventory_fenced_flags_match_the_tree():
    """The gate this task exists to arm. Every tool_commit row's `fenced` value is
    now recomputed from the source on every regeneration, so a hand-edit does not
    survive and `complete` cannot be claimed over unconverted code."""
    from tests.fencing.inventory import load_inventory, scan_tree

    _sites, fenced_ids = scan_tree()
    problems = [
        f"{row['path']}::{row['symbol']} says fenced={row['fenced']}"
        for row in load_inventory()
        if row["kind"] == "tool_commit"
        and row["fenced"] is not ((row["path"], row["symbol"]) in fenced_ids)
    ]
    assert not problems, (
        "tool_commit rows whose fenced flag disagrees with the code:\n  "
        + "\n  ".join(problems)
        + "\n\nRegenerate the inventory; do not edit the flag by hand."
    )


def test_non_tool_commit_rows_keep_their_stored_flag():
    """Scoping. `is this a port call` is meaningless for a claim function, and
    F3a's nine fenced rows must not be disturbed by a rule aimed at F3d."""
    rows = load_inventory()
    f3a = [r for r in rows if r["slice"] == "F3a"]
    assert len(f3a) == 9
    assert all(r["fenced"] for r in f3a)
    assert all(r["kind"] in {"claim_fn", "tool_context"} for r in f3a)
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/fencing/test_fence_coverage.py -q -k "measured_as_fenced or half_converted or fenced_flags_match or stored_flag" > /tmp/f3d2-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-2.txt
```
Expected: `PYTEST_EXIT=1`, `ImportError: cannot import name 'find_fenced_tool_commits'`.

- [ ] **Step 4: Implement the measurement**

In `tests/fencing/detectors.py`, add after `find_sites`:

```python
def find_fenced_tool_commits(source: str, path: str) -> set[tuple[str, str]]:
    """`(path, symbol)` for every qualname under tools/ that writes ONLY through a
    port.

    Port call AND no raw commit. A qualname holding both is half-converted, and
    half-converted is not converted — a slice must not be able to report itself
    complete over a site that still commits directly.

    Returns an empty set for a path outside tools/ — same scoping as find_sites.
    """
    norm = path.replace("\\", "/")
    if _TOOLS_DIR not in norm:
        return set()

    tree = ast.parse(source)
    port_calls: set[str] = set()
    raw_commits: set[str] = set()

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, SCOPED):
            stack = stack + [node.name]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            qual = ".".join(stack) if stack else "<module>"
            if node.func.attr == "commit":
                raw_commits.add(qual)
            elif _is_port_write(node):
                port_calls.add(qual)
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return {(path, q) for q in port_calls - raw_commits}
```

- [ ] **Step 5: Make the merge compute it**

In `tests/fencing/inventory.py`, change `scan_tree` to collect both, and `merge` to take
them. `scan_tree` currently returns `sites`; it now returns a pair:

```python
def scan_tree() -> tuple[set[Site], set[tuple[str, str]]]:
    """The sites, and the identities the code proves are port-only.

    Returns a PAIR since F3d2: `fenced` for a tool_commit is measured, not stored,
    so every caller that merges needs both halves and cannot forget the second.
    """
    sites: set[Site] = set()
    fenced_ids: set[tuple[str, str]] = set()
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(REPO).as_posix()
        source = p.read_text(encoding="utf-8")
        sites |= find_sites(source, rel)
        fenced_ids |= find_fenced_tool_commits(source, rel)
    return sites, fenced_ids
```

and import `find_fenced_tool_commits` beside `find_sites`.

In `merge`, add the parameter and use it for `tool_commit` rows only:

```python
def merge(
    previous: list[dict],
    found: set[Site],
    renames: dict[tuple[str, str, str], tuple[str, str, str]] | None,
    fenced_ids: set[tuple[str, str]],
) -> list[dict]:
```

and replace the `fenced` line in the row construction with:

```python
            # F3d2: for a tool_commit, `fenced` is MEASURED from the source, never
            # carried forward. It was a human boolean that nothing checked, which
            # made "slice complete" an assertion rather than a proof. Other kinds
            # keep the stored flag: `is this a port call` is meaningless for a
            # claim function.
            "fenced": (
                (s.path, s.symbol) in fenced_ids
                if s.kind == KIND_TOOL_COMMIT
                else (prev["fenced"] if prev else False)
            ),
```

Update the two callers — `tests/fencing/regenerate_inventory.py` and any test that calls
`merge` — to unpack the pair and pass `fenced_ids` through. Find them with:

```bash
grep -rn "scan_tree\|merge(" tests/fencing/ | grep -v "def merge"
```

- [ ] **Step 6: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d2-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-2.txt
uv run python -m tests.fencing.regenerate_inventory && git diff --exit-code tests/fencing/fence_inventory.json && echo "INVENTORY UNCHANGED"
```
Expected: `PYTEST_EXIT=0`, 93 passed (89 + 4). The regeneration must leave the JSON
**byte-identical**: every `tool_commit` row is `fenced: false` today and no tool calls a
port yet, so the measured value equals the stored one. If the file changes, the rule is
wrong — investigate before continuing.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add tests/fencing/detectors.py tests/fencing/inventory.py tests/fencing/test_fence_coverage.py tests/fencing/fixtures/converted_site.py tests/fencing/fixtures/half_converted_site.py tests/fencing/regenerate_inventory.py
git commit -m "test(fencing): fenced is measured from the code, not asserted by a human (F3d2 Task 2)"
```

- [ ] **Step 8: Prove a hand-flipped flag no longer survives**

Set `"fenced": true` on the `submit_scorecard_impl` row in
`tests/fencing/fence_inventory.json` — the exact edit that used to be the way to claim a
conversion.

Run:
```bash
uv run pytest tests/fencing -q -k "fenced_flags_match or declared_status" > /tmp/f3d2-2m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-2m.txt
```
Expected: FAIL on `test_the_inventory_fenced_flags_match_the_tree`, naming
`tools/scorecard.py::submit_scorecard_impl says fenced=True`. Before this task the same
edit produced a DIFFERENT failure (`declared_status`: fenced under a pending slice) and
would have been green the moment the slice was marked complete. That difference is the
whole point of the task.

```bash
git checkout tests/fencing/fence_inventory.json
git diff --exit-code HEAD -- tests/fencing/fence_inventory.json
```

---

### Task 3: `compute_overall_band` moves into `ports/`

`scorecard.py` clears three of its four pins by conversion, and stops on the fourth:
`from call_manager.proposal_assist.evaluator.scoring import compute_overall_band`
(`scorecard.py:14`). The gate is a positive allowlist — stdlib plus
`call_manager.proposal_assist.ports` — so the import is a violation regardless of the
function's purity.

The function is already stdlib-clean (`evaluator/scoring.py` imports nothing but
`from __future__ import annotations`) and it is a pure computation the converted tool still
has to perform: it returns the `overall_band` the tool reports back to the agent, and
`Effects.submit_scorecard` returns `None`. So it moves, and the old home re-exports —
exactly the relocation pattern F3d1 used four times.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/scoring.py`
- Modify: `src/call_manager/proposal_assist/evaluator/scoring.py`
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `compute_overall_band(items: list[tuple[str, float]]) -> str` in
  `call_manager.proposal_assist.ports.scoring`, re-exported from
  `call_manager.proposal_assist.evaluator.scoring` as the SAME object.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3d2_conversions.py`:

```python
"""F3d2 — the submitters and the reads. Each converted site against its port, and
each with the compensation disabled.

Spec 2026-08-18 revision 8 §5: "a conversion test that passes with the fence removed
is measuring the compensation, not the fence".
"""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_running_turn,
    seed_uncited_job,
    seed_workspace,
)

_G = 4  # the generation every actor in this file claims at
_NEXT = 5  # what a lifecycle transition moves it to


def _ctx_and_invocation(workspace_id, **extra):
    """A ToolContext at generation _G plus its frozen Invocation.

    ports=None deliberately: a converted tool receives its bundle as a KEYWORD from
    _safe_tool, not through the context, and these tests call the impls directly.
    """
    ctx = ToolContext(
        workspace_id=workspace_id,
        repo_dir=None,
        session_factory=None,
        settings=None,
        lifecycle_generation=_G,
        **extra,
    )
    return ctx, snapshot_invocation(ctx)


class _ExplodingPorts:
    """Fails the test if the tool reaches for a capability at all. Used wherever a
    tool must refuse a bad payload BEFORE touching a port."""

    def __getattr__(self, name):
        raise AssertionError(f"the port was touched: {name}")


def _criterion(index, name="A"):
    return {"index": index, "name": name, "band": "adequate", "note": "n"}


def test_the_band_rollup_is_reachable_from_ports():
    """A converted submit_scorecard_impl still has to compute the overall band —
    Effects.submit_scorecard returns None and the tool reports the band back to the
    agent — and it may import only stdlib and ports."""
    from call_manager.proposal_assist.ports.scoring import compute_overall_band

    assert callable(compute_overall_band)


def test_the_old_scoring_module_re_exports_the_same_function():
    """GC-4 of F3d1, still binding: one source of truth, and the moved object stays
    the SAME object so no caller can drift onto a second copy."""
    from call_manager.proposal_assist.evaluator import scoring as old
    from call_manager.proposal_assist.ports import scoring as new

    assert old.compute_overall_band is new.compute_overall_band
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-3.txt
```
Expected: `PYTEST_EXIT=1`,
`ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.scoring'`.

- [ ] **Step 3: Create the ports module**

Create `src/call_manager/proposal_assist/ports/scoring.py` by moving
`compute_overall_band` and every module-level constant it reads out of
`src/call_manager/proposal_assist/evaluator/scoring.py` VERBATIM, under this docstring:

```python
"""The weighted band rollup a converted scorecard tool still performs — stdlib only.

Moved out of evaluator/scoring.py in F3d2. `Effects.submit_scorecard` returns None,
so the tool computes the overall band itself and reports it to the agent; a
converted tool may import only the standard library and this package, so the
function has to live here.
"""

from __future__ import annotations
```

Before writing, read `evaluator/scoring.py` in full and move every name
`compute_overall_band` depends on. Anything left behind that the function references is
`F821` at Step 6, and `ruff check .` is a CI gate.

- [ ] **Step 4: Re-export from the old home**

In `src/call_manager/proposal_assist/evaluator/scoring.py`, delete the moved definitions
and add:

```python
from call_manager.proposal_assist.ports.scoring import (
    compute_overall_band as compute_overall_band,
)
```

The redundant-alias form is deliberate: `ruff`'s F401 flags a plain unused import. Keep
every other function in the module exactly as it is — only `compute_overall_band` and the
names it needs move.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/test_f3b_ports_boundary.py -q > /tmp/f3d2-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-3.txt
uv run pytest tests/proposal_assist -q -k "scoring or scorecard or evaluator" > /tmp/f3d2-3b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-3b.txt
```
Expected: `PYTEST_EXIT=0` for both. The boundary file proves `ports/` is still
stdlib-only after the move; the second command is every existing caller of the function.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/scoring.py src/call_manager/proposal_assist/evaluator/scoring.py tests/proposal_assist/test_f3d2_conversions.py
git commit -m "feat(ports): the band rollup a converted scorecard tool still performs (F3d2 Task 3)"
```

- [ ] **Step 7: Prove the re-export is real**

In `src/call_manager/proposal_assist/evaluator/scoring.py`, replace the re-export with a
fresh definition of the same shape:

```python
def compute_overall_band(items):
    """A second function that looks identical."""
    return "adequate"
```

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-3m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-3m.txt
```
Expected: FAIL on `test_the_old_scoring_module_re_exports_the_same_function` with
`assert <function compute_overall_band ...> is <function compute_overall_band ...>` — the
two objects print identically, which is exactly why the check is `is` and not `==`.

```bash
git checkout src/call_manager/proposal_assist/evaluator/scoring.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/evaluator/scoring.py
```

---

### Task 4: `submit_scorecard_impl` converts

The first real conversion. `port_impl/effects.py::SqlEffects.submit_scorecard` already
reproduces this tool's guards in the same order and with the same refusal strings
(target-claimable first, exactly-once second — the ordering F3b's review corrected), so the
conversion is a deletion, not a re-implementation.

Everything above the `with ctx.session_factory()` block is pure validation against the
PINNED rubric snapshot and stays exactly as it is.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/scorecard.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_collect_tools`)
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: `Ports` and `Invocation` on the dispatch path (F3d1); `compute_overall_band`
  from `ports.scoring` (Task 4).
- Produces: `submit_scorecard_impl(ctx, *, ports, invocation, criteria, summary)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d2_conversions.py`:

```python
async def test_scorecard_writes_through_the_port(pg_session_factory):
    """The conversion itself: the tool no longer opens a session, and the row lands."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )

    assert out["stored"] is True
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is not None


async def test_scorecard_does_not_land_after_the_generation_moves(pg_session_factory):
    """GC-4 — THE COMPENSATION IS DISABLED. The tool's own guard (job is this
    workspace's still-running evaluator run) is left SATISFIED: the job stays
    running and owned. Only the generation moves. If the write still lands, the
    fence is decorative and this test is the only thing that would have said so."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    # Stamp taken at _G — the actor's live claim.
    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    # The workspace moves on. The job is untouched and still perfectly claimable.
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


async def test_scorecard_refusal_messages_are_unchanged(pg_session_factory):
    """GC-6. The port reproduces the tool's strings verbatim and in the same order:
    a stale ctx pointing at a finished run must still get the TARGET message, not
    'already submitted'."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    with pg_session_factory() as s:
        s.get(EvaluatorJob, job_id).status = "succeeded"
        s.commit()

    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )
    assert out["error"] == (
        "scorecard target is not an active evaluator run for this workspace"
    )


async def test_scorecard_validation_still_runs_before_any_port_call():
    """The validation half is pure and unchanged. A bad payload must be refused
    without the port being touched at all — a converted tool that reaches for a
    capability before validating has widened its blast radius. No database here."""
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), eval_rubric_criteria=[{"name": "A"}]
    )
    out = await submit_scorecard_impl(
        ctx,
        ports=_ExplodingPorts(),
        invocation=invocation,
        criteria=[],  # covers zero of the one required index
        summary="ok",
    )
    assert "must cover criterion indexes" in out["error"]
```

`_ctx_and_invocation`, `_criterion` and `_ExplodingPorts` are the shared helpers written in
Task 3 Step 1. `eval_rubric_criteria`, `eval_draft_section_count`, `eval_spec_section_count`
and `origin_job_id` are ordinary `ToolContext` fields — pass them through `**extra`.

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k scorecard > /tmp/f3d2-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-4.txt
```
Expected: `PYTEST_EXIT=1`, all four failing with
`TypeError: submit_scorecard_impl() got an unexpected keyword argument 'ports'`. Note the
error comes from the impl itself, NOT from `_safe_tool` — these tests call the impl
directly, so nothing absorbs the keyword.

- [ ] **Step 3: Convert the tool**

In `src/call_manager/proposal_assist/tools/scorecard.py`, replace the import block:

```python
from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.ports.effects import SubmitScorecard
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.scoring import compute_overall_band
```

`from call_manager.db.models import EvaluatorJob` and the `tools.registry` import are
deleted. So is the `ToolContext` annotation — GC-3: a converted tool may not name it. The
signature becomes:

```python
async def submit_scorecard_impl(
    ctx, *, ports, invocation, criteria: list[dict[str, Any]], summary: str
) -> dict[str, Any]:
```

`ctx` is deliberately unannotated. It is still read for `ctx.eval_rubric_criteria`,
`ctx.eval_draft_section_count` and `ctx.eval_spec_section_count`, which are not resource
locators.

Leave every line from `rubric = list(ctx.eval_rubric_criteria)...` down to and including
`scorecard = {...}` EXACTLY as it is. Then replace the whole
`with ctx.session_factory() as session:` block — from `with` to `session.commit()` — with:

```python
    # The guards, the row lock and the exactly-once rule all moved into
    # port_impl/effects.py::SqlEffects.submit_scorecard, which reproduces them in
    # the same order and with these same strings. The generation predicate is in
    # the UPDATE's WHERE clause, so a workspace that moved on lands nothing.
    try:
        ports.effects.submit_scorecard(
            invocation.stamp(), SubmitScorecard(scorecard=scorecard)
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        # Busy, not failed. Without this clause _safe_tool's catch-all turns a
        # transient lease timeout into "internal error".
        return {"error": BUSY_ERROR_MESSAGE}
    return {"stored": True, "overall_band": overall}
```

- [ ] **Step 4: Flip the dispatch**

In `src/call_manager/proposal_assist/tools/registry.py`, find the `_safe_tool` call for
`submit_scorecard` inside `_collect_tools` and add both keywords:

```python
            return await _safe_tool(
                "submit_scorecard",
                ctx,
                submit_scorecard_impl,
                ctx,
                pass_ports=True,
                pass_invocation=True,
                criteria=args.get("criteria"),
                summary=args.get("summary"),
            )
```

Keep every other argument exactly as it is — read the current call and change only the two
new keywords. `pass_invocation=True` is required: `invocation.stamp()` is the fence.

- [ ] **Step 4a: Migrate the existing direct callers**

GC-13. `tests/proposal_assist/evaluator/test_submit_scorecard.py` calls
`submit_scorecard_impl` directly and every call now misses two required keywords. Find them:

```bash
grep -n "submit_scorecard_impl(" tests/proposal_assist/evaluator/test_submit_scorecard.py
```

For each, add `ports=build_ports(<the session factory that test already uses>)` and
`invocation=snapshot_invocation(ctx)`, importing `build_ports` from
`call_manager.proposal_assist.port_impl` and `snapshot_invocation` from
`call_manager.proposal_assist.tools.registry`. Change nothing else — in particular, do not
touch a single assertion.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-4.txt
uv run pytest tests/proposal_assist tests/fencing -q -n 16 --dist loadfile > /tmp/f3d2-4b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-4b.txt
```
Expected: `PYTEST_EXIT=0` for both — but only AFTER Step 4a. Run it first and expect
`TypeError: submit_scorecard_impl() missing 2 required keyword-only arguments` from every
direct caller in `tests/proposal_assist/evaluator/test_submit_scorecard.py`.

The ASSERTIONS in that file must not change: behaviour and strings are preserved (GC-6).
If one has to change, stop and report it — that is a behaviour change, not a test that
needs updating.

Then confirm the inventory now MEASURES the conversion:

```bash
uv run python -m tests.fencing.regenerate_inventory
git diff tests/fencing/fence_inventory.json
```
Expected: exactly one changed line — `submit_scorecard_impl` flips to `"fenced": true`.
Commit the regenerated file. This is Task 2 working: the flag followed the code with no
human touching it.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/scorecard.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/evaluator/test_submit_scorecard.py tests/fencing/fence_inventory.json
git commit -m "feat(tools): submit_scorecard writes through the effects port (F3d2 Task 4)"
```

- [ ] **Step 7: Prove the fence is what stops the write, not the guard**

This is GC-4 stated as a command. In
`src/call_manager/proposal_assist/port_impl/effects.py`, delete the
`live_at_generation(stamp.workspace_id, stamp.generation),` line from
`submit_scorecard`'s `update(...).where(...)`.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k scorecard > /tmp/f3d2-4m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-4m.txt
```
Expected: FAIL on `test_scorecard_does_not_land_after_the_generation_moves`, with the
scorecard column NOT None. The other three scorecard tests must still PASS — they are
about the conversion and the strings, not the fence.

If the generation test still passes with that line gone, the test is measuring the tool's
own guard and not the fence. Do not proceed: report it.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/effects.py
```

---

### Task 5: `submit_review_impl` converts

Same shape as Task 5, one difference worth knowing: the port stores `cmd.markdown`
VERBATIM. `ports/effects.py:32-36` says so explicitly — the strip-and-reject step is the
TOOL's job (`review_submit.py:16-18`) and a conversion that drops it loses it silently.
Keep it.

`SubmitReview` carries `review_run_item_id`, which the tool reads from
`ctx.review_run_item_id`. That is not a resource locator, so reading it is fine.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/review_submit.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_collect_tools`)
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: `Ports`, `Invocation`.
- Produces: `submit_review_impl(ctx, *, ports, invocation, markdown)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d2_conversions.py`:

```python
async def test_review_writes_through_the_port(pg_session_factory):
    from call_manager.db.models import ReviewRunItem
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        run_id, item_id = seed_review_item(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=run_id, review_run_item_id=item_id
    )
    out = await submit_review_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        markdown="  a body  ",
    )

    assert out["stored"] is True
    with pg_session_factory() as s:
        stored = s.get(ReviewRunItem, item_id).raw_response_json
    # STRIPPED by the tool, not the port: ports/effects.py:32-36 says the port
    # stores verbatim and the tool owns the strip-and-reject.
    assert stored == {"markdown": "a body"}


async def test_review_still_rejects_an_empty_body_without_touching_the_port():
    """The strip-and-reject step lives in the tool. A conversion that hands an
    empty string to the port loses it silently — the port stores verbatim."""
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), review_run_item_id=uuid.uuid4()
    )
    out = await submit_review_impl(
        ctx, ports=_ExplodingPorts(), invocation=invocation, markdown="   "
    )
    assert out["error"] == "markdown must be a non-empty string"


async def test_review_does_not_land_after_the_generation_moves(pg_session_factory):
    """GC-4 — THE COMPENSATION IS DISABLED. The item stays running and owned by a
    running run in this workspace, so every guard the tool used to apply is
    SATISFIED. Only the generation moves."""
    from call_manager.db.models import ReviewRunItem
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        run_id, item_id = seed_review_item(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=run_id, review_run_item_id=item_id
    )
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_review_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        markdown="a body",
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json is None
```

`seed_review_item` returns `(review_run_id, review_run_item_id)` — the run id is what the
tool used to compare against `ctx.origin_job_id`, so pass it as `origin_job_id`.

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k review > /tmp/f3d2-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-5.txt
```
Expected: `PYTEST_EXIT=1`, three failures with
`TypeError: submit_review_impl() got an unexpected keyword argument 'ports'`.

- [ ] **Step 3: Convert the tool**

Replace the whole of `src/call_manager/proposal_assist/tools/review_submit.py` below the
module docstring with:

```python
from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.ports.effects import SubmitReview
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)


async def submit_review_impl(ctx, *, ports, invocation, markdown: str) -> dict[str, Any]:
    # The strip-and-reject stays HERE. ports/effects.py:32-36: the port stores
    # cmd.markdown verbatim and does not reject an empty result — that is the tool
    # boundary's job, and a converter that drops this step loses it silently.
    body = markdown.strip() if isinstance(markdown, str) else ""
    if not body:
        return {"error": "markdown must be a non-empty string"}
    try:
        ports.effects.submit_review(
            invocation.stamp(),
            SubmitReview(review_run_item_id=ctx.review_run_item_id, markdown=body),
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"stored": True}
```

Keep the module docstring exactly as it is, except for the sentence that says the tool
"writes it onto the running ReviewRunItem" — that is now the port's job. Change that clause
to say the tool validates and hands the body to `Effects.submit_review`.

- [ ] **Step 4: Flip the dispatch**

In `_collect_tools`, add `pass_ports=True` and `pass_invocation=True` to the
`submit_review` `_safe_tool` call, changing nothing else about it.

- [ ] **Step 4a: Migrate the existing direct callers**

GC-13. `tests/proposal_assist/review/test_submit_review.py` calls `submit_review_impl` directly and every call now misses two required keywords. Find them:

```bash
grep -n "submit_review_impl(" tests/proposal_assist/review/test_submit_review.py
```

For each, add `ports=build_ports(<the session factory that test already uses>)` and `invocation=snapshot_invocation(ctx)`.
Import `build_ports` from `call_manager.proposal_assist.port_impl` and `snapshot_invocation` from `call_manager.proposal_assist.tools.registry`. Change nothing
else — in particular, do not touch a single assertion.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-5.txt
uv run pytest tests/proposal_assist tests/fencing -q -n 16 --dist loadfile > /tmp/f3d2-5b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-5b.txt
uv run python -m tests.fencing.regenerate_inventory && git diff tests/fencing/fence_inventory.json
```
Expected: `PYTEST_EXIT=0` for both, and exactly one changed inventory line —
`submit_review_impl` flips to `"fenced": true`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/review_submit.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/review/test_submit_review.py tests/fencing/fence_inventory.json
git commit -m "feat(tools): submit_review writes through the effects port (F3d2 Task 5)"
```

- [ ] **Step 7: Prove the fence is what stops the write, not the guard**

GC-4, per-site. In `src/call_manager/proposal_assist/port_impl/effects.py`, delete the
`live_at_generation(stamp.workspace_id, stamp.generation),` line from `submit_review`'s
`update(...).where(...)`.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k review > /tmp/f3d2-5f.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-5f.txt
```
Expected: FAIL on `test_review_does_not_land_after_the_generation_moves`, with
`raw_response_json` NOT None. The other two review tests must still PASS — they are about
the conversion and the strip, not the fence.

If the generation test still passes with that line gone, it is measuring the tool's own
guard and not the fence. Do not proceed: report it.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/effects.py
```

- [ ] **Step 8: Prove the strip did not move to the port**

In `src/call_manager/proposal_assist/tools/review_submit.py`, change
`SubmitReview(review_run_item_id=ctx.review_run_item_id, markdown=body)` to pass
`markdown=markdown` — the unstripped argument.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k review > /tmp/f3d2-5m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-5m.txt
```
Expected: FAIL on `test_review_writes_through_the_port` with
`assert {'markdown': '  a body  '} == {'markdown': 'a body'}`. This is the defect the port
docstring warns about, and it is invisible without a whitespace-carrying fixture.

```bash
git checkout src/call_manager/proposal_assist/tools/review_submit.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/tools/review_submit.py
```

---

### Task 6: `submit_uncited_claims_impl` converts

The third submitter. Its validation loop is pure and stays; only the guarded write moves.
`SubmitUncitedClaims` carries the validated list.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/uncited_claims.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_collect_tools`)
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: `Ports`, `Invocation`.
- Produces: `submit_uncited_claims_impl(ctx, *, ports, invocation, claims)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d2_conversions.py`:

```python
async def test_uncited_claims_writes_through_the_port(pg_session_factory):
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_uncited_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, uncited_draft_section_count=2
    )
    out = await submit_uncited_claims_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 0, "reason": "r"}],
    )

    assert out == {"stored": True, "count": 1}
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is not None


async def test_uncited_claims_does_not_land_after_the_generation_moves(
    pg_session_factory,
):
    """GC-4 — THE COMPENSATION IS DISABLED. The job stays running and owned; only
    the generation moves."""
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_uncited_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, uncited_draft_section_count=2
    )
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_uncited_claims_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 0, "reason": "r"}],
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is None


async def test_uncited_claims_validation_still_runs_before_any_port_call():
    """The section-index bound comes from ctx.uncited_draft_section_count, which the
    converted tool still reads. A bad index must be refused without the port."""
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), uncited_draft_section_count=1
    )
    out = await submit_uncited_claims_impl(
        ctx,
        ports=_ExplodingPorts(),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 5, "reason": "r"}],
    )
    assert "section_index must be an int in [0,1)" in out["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k uncited > /tmp/f3d2-6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-6.txt
```
Expected: `PYTEST_EXIT=1`, three failures with
`TypeError: submit_uncited_claims_impl() got an unexpected keyword argument 'ports'`.

- [ ] **Step 3: Convert the tool**

In `src/call_manager/proposal_assist/tools/uncited_claims.py`, replace the imports:

```python
from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.ports.effects import SubmitUncitedClaims
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)
```

Change the signature to
`async def submit_uncited_claims_impl(ctx, *, ports, invocation, claims: list[dict[str, Any]]) -> dict[str, Any]:`
— `ctx` unannotated, `UncitedClaimsJob` and the `tools.registry` import deleted. Keep
`_is_int` and the whole validation loop exactly as they are, then replace the
`with ctx.session_factory() as session:` block with:

```python
    # The row lock, the ownership guard and the exactly-once rule moved into
    # port_impl/effects.py::SqlEffects.submit_uncited_claims, with the same strings.
    try:
        ports.effects.submit_uncited_claims(
            invocation.stamp(), SubmitUncitedClaims(claims=out)
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"stored": True, "count": len(out)}
```

- [ ] **Step 4: Flip the dispatch**

In `_collect_tools`, add `pass_ports=True` and `pass_invocation=True` to the
`submit_uncited_claims` `_safe_tool` call.

- [ ] **Step 4a: Migrate the existing direct callers**

GC-13. `tests/proposal_assist/uncited_claims/test_submit.py` calls `submit_uncited_claims_impl` directly and every call now misses two required keywords. Find them:

```bash
grep -n "submit_uncited_claims_impl(" tests/proposal_assist/uncited_claims/test_submit.py
```

For each, add `ports=build_ports(<the session factory that test already uses>)` and `invocation=snapshot_invocation(ctx)`.
Import `build_ports` from `call_manager.proposal_assist.port_impl` and `snapshot_invocation` from `call_manager.proposal_assist.tools.registry`. Change nothing
else — in particular, do not touch a single assertion.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-6.txt
uv run pytest tests/proposal_assist tests/fencing -q -n 16 --dist loadfile > /tmp/f3d2-6b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-6b.txt
uv run python -m tests.fencing.regenerate_inventory && git diff tests/fencing/fence_inventory.json
```
Expected: `PYTEST_EXIT=0` for both, and exactly one changed inventory line —
`submit_uncited_claims_impl` flips to `"fenced": true`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/uncited_claims.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/uncited_claims/test_submit.py tests/fencing/fence_inventory.json
git commit -m "feat(tools): submit_uncited_claims writes through the effects port (F3d2 Task 6)"
```

- [ ] **Step 7: Prove the fence is what stops the write, not the guard**

GC-4, per-site. In `src/call_manager/proposal_assist/port_impl/effects.py`, delete the
`live_at_generation(stamp.workspace_id, stamp.generation),` line from
`submit_uncited_claims`'s `update(...).where(...)`.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k uncited > /tmp/f3d2-6f.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-6f.txt
```
Expected: FAIL on `test_uncited_claims_does_not_land_after_the_generation_moves`, with the
`claims` column NOT None. The other two uncited tests must still PASS.

If the generation test still passes with that line gone, it is measuring the tool's own
guard and not the fence. Do not proceed: report it.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/effects.py
```

- [ ] **Step 8: Prove the exactly-once rule survived the move**

In `src/call_manager/proposal_assist/port_impl/effects.py`, delete the
`UncitedClaimsJob.claims.is_(None),` predicate from `submit_uncited_claims`'s `update`.

Run:
```bash
uv run pytest tests/proposal_assist -q -k "uncited" > /tmp/f3d2-6m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-6m.txt
```
Expected: FAIL on the existing exactly-once test for this tool (find its name in the run
output and record it in your report) — a second submission now overwrites the first. If
NOTHING goes red, say so loudly: it means the exactly-once rule has no coverage on either
side of the move, which is a gap this task must close before it is done.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/effects.py
```

---

### Task 7: The two `references.py` accounting sites convert

Accounting is DELIBERATELY unfenced (spec §3.3): freeing a slot must happen after the
workspace has moved on, and fencing it would strand jobs holding their slots. So these take
no stamp — they take the turn job id, and that is the whole point. `Accounting`'s methods
are bound to `active_turn_job_id`, never "whichever turn is running for this workspace",
which is a real tightening over the current code: `references.py:465-473` matches on
`TurnJob.workspace_id == ctx.workspace_id AND status == "running"`.

`fetch_reference_impl` keeps its ingest commit (`:580`) — that is `_do_fetch_and_ingest`'s
and belongs to F3e. Under Task 2's rule the qualname is therefore port-call-AND-raw-commit
and stays `fenced: false`, which is why Task 3 moved its row to F3e. Only
`_refund_fetch_slot` goes green here.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py:460-510`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_collect_tools`)
- Modify: `tests/fencing/inventory.py` (`TOOLS_NOT_YET_CONVERTED["references.py"]`)
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: `Accounting` (`reserve_fetch_slot`, `refund_fetch_slot`),
  `MAX_FETCHES_PER_TURN` from `ports.accounting`.
- Produces: `fetch_reference_impl(ctx, *, ports, invocation, ...)` — existing keyword
  arguments unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d2_conversions.py`:

```python
async def test_the_refund_does_not_touch_a_later_turns_row(pg_session_factory):
    """GC-5, the interleaving accounting exists for (spec §5): reserve a slot at
    generation G, delete and restore, start a NEW turn at G+1, and assert the old
    actor's refund does not touch the new turn's row.

    TWO schema facts shape this and neither is optional. `uq_turn_jobs_running`
    (models.py:887-891) is a partial unique index on workspace_id WHERE
    status='running', so the old turn MUST leave 'running' before the new one can
    exist — the delete/restore is what does that. And refund_fetch_slot requires
    `TurnJob.status == "running"` (port_impl/accounting.py:38-45), so the dead
    actor's refund is a no-op on its own row too.

    The claim under test is therefore about the NEW turn: a late refund from an
    actor the workspace has moved past must not decrement the live turn's count.
    That is what binding to active_turn_job_id buys, and what the code this
    replaces (references.py:465-473, matching workspace + status) did NOT.
    """
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.port_impl import build_ports

    ports = build_ports(pg_session_factory)
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        old_turn = seed_running_turn(s, ws_id)

    assert ports.accounting.reserve_fetch_slot(old_turn) == 1

    with pg_session_factory() as s:  # delete + restore ends the old turn
        s.get(TurnJob, old_turn).status = "failed"
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()
        new_turn = seed_running_turn(s, ws_id)  # only now is this insertable

    assert ports.accounting.reserve_fetch_slot(new_turn) == 1

    # The old actor, still holding its slot, finally gives it back.
    ports.accounting.refund_fetch_slot(old_turn)

    with pg_session_factory() as s:
        # THE CLAIM: the live turn is untouched.
        assert s.get(TurnJob, new_turn).fetch_count == 1
        # The dead turn keeps its count: the status guard refuses to refund a turn
        # that is no longer running, and the row is terminal anyway.
        assert s.get(TurnJob, old_turn).fetch_count == 1


async def test_the_reserve_is_bound_to_the_turn_not_the_workspace(pg_session_factory):
    """The tightening the port makes over the code it replaces. A reserve against a
    turn id that is not running returns None even though ANOTHER turn in the same
    workspace is running and under cap."""
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.port_impl import build_ports

    ports = build_ports(pg_session_factory)
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        dead_turn = seed_running_turn(s, ws_id)
        s.get(TurnJob, dead_turn).status = "succeeded"
        s.commit()
        seed_running_turn(s, ws_id)  # a live turn, under cap

    assert ports.accounting.reserve_fetch_slot(dead_turn) is None


async def test_the_cap_constant_is_the_one_the_tool_reports():
    """The tool tells the agent 'fetch cap reached (3 per turn)'. That 3 was a
    literal at references.py:469 while the port carried its own constant — two
    numbers that must agree and nothing making them. No database here."""
    from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN

    assert MAX_FETCHES_PER_TURN == 3
```

`seed_running_turn` builds the `ChatTurn` a `TurnJob` requires; call it twice for the two
turns. Note `bump_generation` does NOT commit — the seed module leaves that to the caller,
and every block above commits explicitly.

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k "refund or reserve or cap_constant" > /tmp/f3d2-7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-7.txt
```
Expected: `PYTEST_EXIT=0` — **all three PASS immediately**, and that is correct.
`Accounting` already exists from F3b; these tests prove the PORT's owner-binding, which is
the spec-mandated proof for accounting, not the tool's conversion. They are written first
so that Step 3's rewrite has something that would catch a behaviour change. The tool
conversion itself is covered by the existing `references.py` tests, which must stay green
through Step 7.

- [ ] **Step 3: Convert the reserve**

In `src/call_manager/proposal_assist/tools/references.py`, replace the reserve block —
the `with ... as s:` that runs the `_sql_update(TurnJob) ... fetch_count + 1` statement and
its `s.commit()`, through the `if new_count_row is None:` branch — with:

```python
    new_count = ports.accounting.reserve_fetch_slot(invocation.active_turn_job_id)
    if new_count is None:
        # Either no running turn OR cap reached. Same message the tool has always
        # returned; MAX_FETCHES_PER_TURN replaces the literal 3 that used to sit in
        # the WHERE clause AND in this string with nothing keeping them equal.
        return {
            "error": (
                f"fetch cap reached ({MAX_FETCHES_PER_TURN} per turn) OR no running "
                "turn for this workspace — please ask the user to upload the "
                "reference manually via the /references panel."
            )
        }
```

That third line is not optional. The real string (`references.py:477-483`) ends with the
manual-upload guidance, and the existing coverage asserts on a SUBSTRING — so dropping it
is a user-visible GC-6 regression that no test would catch. Read the current string and
preserve its wording exactly except for the number, which becomes the interpolated
constant.

- [ ] **Step 4: Convert the refund**

Replace the body of `_refund_fetch_slot` — the `with ... as s2:` block containing the
`fetch_count - 1` update and `s2.commit()` — with:

```python
    def _refund_fetch_slot() -> None:
        """Return a slot reserved but never spent on usable content.

        Bound to the turn that reserved it, never to 'whichever turn is running for
        this workspace': a late refund from a turn the workspace has already moved
        past must not decrement a live turn's count. Unfenced ON PURPOSE (spec 3.3)
        — fencing it would strand jobs holding their slots.
        """
        ports.accounting.refund_fetch_slot(invocation.active_turn_job_id)
```

Keep the surrounding comment at `references.py:488-493` that explains why the slot is
reserved before the work.

- [ ] **Step 5: Add the imports and the dispatch**

Add to `references.py`'s imports:

```python
from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN
```

and DELETE the two function-local imports the conversion orphans — they sit inside
`fetch_reference_impl` at `references.py:440` and `:442`:

```python
    from sqlalchemy import update as _sql_update

    from call_manager.db.models import TurnJob
```

Both were used ONLY by the reserve (`:465-472`) and the refund (`:500-506`), and both of
those are now port calls. Leaving them is two `F401`s and `ruff check .` at Step 8 fails.
Verify with `grep -n "_sql_update\|TurnJob" src/call_manager/proposal_assist/tools/references.py`
— it must return nothing after the edit.

Change the signature to
`async def fetch_reference_impl(ctx, *, ports, invocation, ...)` keeping every existing
keyword argument. **Do NOT remove `ToolContext` from this file's imports or annotations** —
the module is still pinned for F3e, `ctx.settings.workspaces_dir` and
`ctx.citation_service` are still read by the ingest half, and removing the annotation while
the locator names remain buys nothing.

In `_collect_tools`, add `pass_ports=True` and `pass_invocation=True` to the
`fetch_reference` `_safe_tool` call.

- [ ] **Step 6: Shrink the exemption**

Run the gate to see what `references.py` still violates:

```bash
uv run python -c "
from tests.fencing.import_gate import check_file, normalized_violation, tool_key, tool_modules
for path in tool_modules():
    if tool_key(path) == 'references.py':
        for v in sorted({normalized_violation(x) for x in check_file(path)}):
            print(v)
"
```

There is no `scan_tool_modules`. The module's API is `tool_modules()`, `check_file(path)`,
`tool_key(path)` and `normalized_violation(v)` — and `tool_key` is the path RELATIVE to
`tools/`, never `path.name` (F3c's dry run caught that: basename keys let a nested module
inherit another's exemptions).

Then edit `TOOLS_NOT_YET_CONVERTED["references.py"]` in `tests/fencing/inventory.py` to
exactly that set. GC-7: the pins shrink in the same commit as the code. If the printed set
is unchanged from the current thirteen, say so in your report rather than editing nothing
silently — it would mean the accounting conversion removed no import, which is worth
knowing.

- [ ] **Step 6a: Migrate the existing direct callers**

GC-13. TWO files call `fetch_reference_impl` directly:
`tests/proposal_assist/tools/test_references_fetch_tool.py` and
`tests/proposal_assist/test_references_admission.py`. Find every site:

```bash
grep -n "fetch_reference_impl(" tests/proposal_assist/tools/test_references_fetch_tool.py tests/proposal_assist/test_references_admission.py
```

Add `ports=build_ports(<the session factory that test already uses>)` and
`invocation=snapshot_invocation(ctx)` to each. The admission file is the one that
exercises the cap, so its assertions are the real check that the port's turn-bound reserve
preserves the behaviour the workspace-bound one had — if an assertion there has to change,
STOP and report it: the tightening changed something user-visible and that is a decision,
not a test edit.

- [ ] **Step 7: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q > /tmp/f3d2-7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-7.txt
uv run pytest tests/proposal_assist tests/fencing -q -n 16 --dist loadfile > /tmp/f3d2-7b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-7b.txt
uv run python -m tests.fencing.regenerate_inventory && git diff tests/fencing/fence_inventory.json
```
Expected: `PYTEST_EXIT=0` for both. Exactly TWO inventory lines change:
`fetch_reference_impl` AND `fetch_reference_impl._refund_fetch_slot` both flip to
`"fenced": true`.

`fetch_reference_impl` flips because the ingest commit is NOT in that qualname — it is in
`fetch_reference_impl._do_fetch_and_ingest`, a nested function at `references.py:551` with
its own inventory row, already assigned to **F3e**. That third row must stay
`"fenced": false`. If it flipped, the F3e boundary has been crossed: stop and report.

- [ ] **Step 8: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/references.py src/call_manager/proposal_assist/tools/registry.py tests/fencing/inventory.py tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/tools/test_references_fetch_tool.py tests/proposal_assist/test_references_admission.py tests/fencing/fence_inventory.json
git commit -m "feat(tools): the two references accounting sites are owner-bound port operations (F3d2 Task 7)"
```

- [ ] **Step 9: Prove the owner binding is what protects the later turn**

In `src/call_manager/proposal_assist/port_impl/accounting.py`, change
`refund_fetch_slot`'s WHERE clause from matching `TurnJob.id == active_turn_job_id` to
matching `TurnJob.workspace_id == <the row's workspace> , TurnJob.status == "running"` —
i.e. reproduce the behaviour the tool had BEFORE this task.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k refund > /tmp/f3d2-7m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-7m.txt
```
Expected: FAIL on `test_the_refund_does_not_touch_a_later_turns_row` with the NEW turn's
`fetch_count` at 0 instead of 1 — the dead actor's refund decremented a live turn. That is
the defect the owner binding exists to prevent, and it is exactly what the pre-conversion
code did.

```bash
git checkout src/call_manager/proposal_assist/port_impl/accounting.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/accounting.py
```

---

### Task 8: `context.py`'s reads become a query port

Spec §7's F3d charter ends with "and every remaining read to a query port returning DTOs".
`context.py` is that clause: two tools, both reading four snapshot columns off the same
`Workspace` row.

One method serves both, because they read the same row and splitting them would mean two
round trips for a snapshot the workspace holds in one place. §5's warning applies: return a
DTO, never an attached instance — an attached ORM object carries `object_session(instance)`,
which is the whole Session again, and that is the leak that defeated the forwarding-proxy
design.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/queries.py`
- Modify: `src/call_manager/proposal_assist/port_impl/queries.py`
- Modify: `src/call_manager/proposal_assist/tools/context.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_collect_tools`)
- Modify: `tests/proposal_assist/test_f3d2_conversions.py`

**Interfaces:**
- Consumes: `Ports`.
- Produces:
  - `WorkspaceSnapshotView` in `ports/queries.py` — frozen dataclass with
    `call_body: str | None`, `rubric_criteria: list[dict[str, Any]]`,
    `rubric_funder_notes: str`, `profile_desc: str | None`.
  - `Queries.get_workspace_snapshot(workspace_id: uuid.UUID) -> WorkspaceSnapshotView | None`.
  - `get_call_details_impl(ctx, *, ports)` and `get_profile_impl(ctx, *, ports)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d2_conversions.py`:

```python
async def test_the_snapshot_view_carries_no_session(pg_session_factory):
    """§5: never return an attached instance. object_session(instance) hands the
    caller the full Session back, which is the leak that killed the forwarding
    proxy. A frozen DTO of plain values cannot do that."""
    import dataclasses

    from call_manager.proposal_assist.port_impl import build_ports

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)

    view = build_ports(pg_session_factory).queries.get_workspace_snapshot(ws_id)

    assert dataclasses.is_dataclass(view)
    for value in dataclasses.asdict(view).values():
        assert value is None or isinstance(value, str | list | dict)


async def test_a_missing_workspace_reads_as_none(pg_session_factory):
    from call_manager.proposal_assist.port_impl import build_ports

    ports = build_ports(pg_session_factory)
    assert ports.queries.get_workspace_snapshot(uuid.uuid4()) is None


async def test_call_details_reads_through_the_port(pg_session_factory):
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.context import get_call_details_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        ws = s.get(Workspace, ws_id)
        ws.call_body_snapshot = "the call body"
        ws.rubric_criteria = [{"name": "A"}]
        ws.rubric_funder_notes = "notes"
        s.commit()

    ctx, _inv = _ctx_and_invocation(ws_id)
    out = await get_call_details_impl(ctx, ports=build_ports(pg_session_factory))

    assert out == {
        "linked": True,
        "call_body": "the call body",
        "rubric_criteria": [{"name": "A"}],
        "rubric_funder_notes": "notes",
    }


async def test_an_unlinked_workspace_still_answers_linked_false(pg_session_factory):
    """GC-6. The empty-snapshot contract is the shape the agent depends on, and it
    is answered for BOTH tools by the same absent-or-empty rule. seed_workspace
    creates exactly this: a workspace with empty snapshots."""
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.context import (
        get_call_details_impl,
        get_profile_impl,
    )

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)

    ports = build_ports(pg_session_factory)
    ctx, _inv = _ctx_and_invocation(ws_id)

    assert await get_call_details_impl(ctx, ports=ports) == {"linked": False}
    assert await get_profile_impl(ctx, ports=ports) == {"linked": False}


async def test_a_deleted_workspace_reads_as_unlinked_not_an_error(pg_session_factory):
    """Reads are NOT fenced (spec §8), so there is no stamp and no refusal. The
    None the port returns for a vanished workspace must surface as the SAME shape
    an empty snapshot produces — not as an exception the agent has to parse."""
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.context import get_call_details_impl

    ctx, _inv = _ctx_and_invocation(uuid.uuid4())  # never existed
    out = await get_call_details_impl(ctx, ports=build_ports(pg_session_factory))
    assert out == {"linked": False}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py -q -k "snapshot_view or missing_workspace or call_details or unlinked" > /tmp/f3d2-8.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-8.txt
```
Expected: `PYTEST_EXIT=1`,
`AttributeError: 'SqlQueries' object has no attribute 'get_workspace_snapshot'`.

- [ ] **Step 3: Add the DTO and the contract**

In `src/call_manager/proposal_assist/ports/queries.py`, beside `ReferenceView`:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotView:
    """The workspace's frozen copies of the linked call and profile.

    Plain values only. Returning the Workspace row itself would hand the tool
    object_session(row) — the full Session again, which is the authority this whole
    design removes (§5).
    """

    call_body: str | None
    rubric_criteria: list[dict[str, Any]]
    rubric_funder_notes: str
    profile_desc: str | None
```

and add to the `Queries` Protocol:

```python
    def get_workspace_snapshot(
        self, workspace_id: uuid.UUID
    ) -> WorkspaceSnapshotView | None:
        """The linked call + profile snapshots, or None when the workspace is gone.

        Reads are NOT fenced (spec §8). A workspace that was deleted mid-turn reads
        as None, which the tools answer as {"linked": False} — the same shape they
        have always returned for an empty snapshot.
        """
        ...
```

- [ ] **Step 4: Implement it**

In `src/call_manager/proposal_assist/port_impl/queries.py`, follow the shape of the
existing `get_reference` exactly — same session handling, same "return a DTO built from
scalars" discipline:

```python
    def get_workspace_snapshot(
        self, workspace_id: uuid.UUID
    ) -> WorkspaceSnapshotView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    Workspace.call_body_snapshot,
                    Workspace.rubric_criteria,
                    Workspace.rubric_funder_notes,
                    Workspace.profile_desc_snapshot,
                ).where(Workspace.id == workspace_id)
            ).first()
        if row is None:
            return None
        return WorkspaceSnapshotView(
            call_body=row[0],
            rubric_criteria=list(row[1] or []),
            rubric_funder_notes=row[2] or "",
            profile_desc=row[3],
        )
```

Selecting COLUMNS rather than the entity is deliberate: there is no instance to be
attached, so the leak is structurally impossible rather than avoided by discipline.

- [ ] **Step 5: Convert the tool**

Replace `src/call_manager/proposal_assist/tools/context.py` below its module docstring
with:

```python
from __future__ import annotations

from typing import Any


async def get_call_details_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked call body + rubric metadata.
    Returns {"linked": False} when call_body_snapshot is empty."""
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.call_body:
        return {"linked": False}
    return {
        "linked": True,
        "call_body": snap.call_body,
        "rubric_criteria": snap.rubric_criteria,
        "rubric_funder_notes": snap.rubric_funder_notes,
    }


async def get_profile_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked profile description.
    Returns {"linked": False} when profile_desc_snapshot is empty."""
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.profile_desc:
        return {"linked": False}
    return {"linked": True, "description": snap.profile_desc}
```

`select`, `Workspace` and the `tools.registry` import are all deleted. Neither tool takes
`invocation`: reads are not fenced (§8) and there is no stamp to take.

- [ ] **Step 6: Flip the dispatch**

In `_collect_tools`, add `pass_ports=True` to BOTH the `get_call_details` and
`get_profile` `_safe_tool` calls. Do NOT add `pass_invocation=True` — these are reads.

- [ ] **Step 6a: Add the DTO contract case, and migrate the existing callers**

`tests/proposal_assist/test_ports_dto_contract.py` keeps a `CASES` dict — one callable per
`Queries` method — and `test_every_query_method_has_a_contract_case` fails until the new
method has one. It also drives
`@pytest.mark.parametrize("name", sorted(CASES))`, so the entry adds one test node. Add:

```python
    "get_workspace_snapshot": lambda q, ws, ref: q.get_workspace_snapshot(ws),
}
```

as the last entry. The dict's own comment is binding: **every case must return a NON-None
result, or it proves nothing.** The fixture that seeds `ws` must therefore leave a
workspace that exists — check what the file's fixture already builds before assuming it
does, and if `get_workspace_snapshot(ws)` would return None there, seed the row the case
needs rather than weakening the assertion.

Then, GC-13: `tests/proposal_assist/test_tools_context.py` calls both impls directly.

```bash
grep -n "get_call_details_impl(\|get_profile_impl(" tests/proposal_assist/test_tools_context.py
```

Add `ports=build_ports(<the session factory that test already uses>)` to each. Neither
takes `invocation` — reads are not fenced.

- [ ] **Step 7: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/test_f3b_ports_boundary.py tests/proposal_assist/test_ports_dto_contract.py -q > /tmp/f3d2-8.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-8.txt
uv run pytest tests/proposal_assist tests/fencing -q -n 16 --dist loadfile > /tmp/f3d2-8b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-8b.txt
```
Expected: `PYTEST_EXIT=0` for both — but only after Step 6a. `test_ports_dto_contract.py`
is NOT automatic: `test_every_query_method_has_a_contract_case` (line 61) reads
`Queries.__protocol_attrs__` and demands an explicit `CASES` entry per method, so a new
port method makes it RED until one is added.

The inventory does NOT change here: `context.py` has no `tool_commit` row. Confirm it:

```bash
uv run python -m tests.fencing.regenerate_inventory && git diff --exit-code tests/fencing/fence_inventory.json && echo "INVENTORY UNCHANGED, as expected"
```

- [ ] **Step 8: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/queries.py src/call_manager/proposal_assist/port_impl/queries.py src/call_manager/proposal_assist/tools/context.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d2_conversions.py tests/proposal_assist/test_ports_dto_contract.py tests/proposal_assist/test_tools_context.py
git commit -m "feat(ports): the workspace snapshot is a query port returning a DTO (F3d2 Task 8)"
```

- [ ] **Step 9: Prove the DTO contract catches a leak**

In `src/call_manager/proposal_assist/port_impl/queries.py`, change
`get_workspace_snapshot` to `select(Workspace).where(...)` and return the row itself
instead of building the view — the exact mistake the DTO rule exists to prevent.

Run:
```bash
uv run pytest tests/proposal_assist/test_ports_dto_contract.py tests/proposal_assist/test_f3d2_conversions.py -q -k "dto or snapshot_view" > /tmp/f3d2-8m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-8m.txt
```
Expected: FAIL on BOTH
`test_every_query_result_is_recursively_free_of_orm_instances[get_workspace_snapshot]` and
`test_the_snapshot_view_carries_no_session`. The first is F3c's walker doing its job
through the case added in Step 6a; the second is this slice's own check. If only the
second fires, the `CASES` entry is returning None and is proving nothing — the dict's
comment warns about exactly that. Fix the case, do not accept the single red.

```bash
git checkout src/call_manager/proposal_assist/port_impl/queries.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/queries.py
```

---

### Task 9: Four exemptions retire and F3d2 closes

The bookkeeping that makes the slice mean something. Four modules now import only stdlib
and `ports`, so their entries leave `TOOLS_NOT_YET_CONVERTED` and the gate starts enforcing
them for real. `SLICE_STATUS["F3d2"]` flips to `complete`, which — since Task 2 — is a
claim the inventory can check rather than one a human asserts.

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/test_fence_coverage.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `TOOLS_NOT_YET_CONVERTED` with five entries; `SLICE_STATUS["F3d2"] == "complete"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/fencing/test_fence_coverage.py`:

```python
def test_the_four_converted_modules_are_no_longer_exempt():
    """The point of the slice, stated as a fact about the exemption list. These
    four import stdlib and ports only; the gate now enforces them with no pins."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py",       # F3f — capability holders
        "citations.py",   # F3d3 — the citation core relocation
        "history.py",     # F3f — the repo_dir locator, and zero inventory rows
        "proposal.py",    # F3d3
        "references.py",  # F3e — the ingest half
    }


def test_f3d2_is_complete_and_every_row_it_owns_is_measured_fenced():
    """`complete` is now a measurable claim. Task 2 made `fenced` a function of the
    source, so this cannot be satisfied by editing the JSON."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory, scan_tree

    assert SLICE_STATUS["F3d2"] == "complete"
    _sites, fenced_ids = scan_tree()
    owned = [r for r in load_inventory() if r["slice"] == "F3d2"]
    assert len(owned) == 5
    for row in owned:
        assert row["fenced"] is True, row["symbol"]
        assert (row["path"], row["symbol"]) in fenced_ids, row["symbol"]


def test_the_ingest_row_is_untouched_and_still_f3e():
    """The boundary this slice must not cross. fetch_reference_impl._do_fetch_and_ingest
    is a SEPARATE nested identity (references.py:551) holding the ingest commit at
    :580. F3d2 converts the accounting around it and leaves it alone — which is also
    why fetch_reference_impl itself CAN go green here."""
    from tests.fencing.inventory import load_inventory

    row = next(
        r
        for r in load_inventory()
        if r["symbol"] == "fetch_reference_impl._do_fetch_and_ingest"
    )
    assert row["slice"] == "F3e"
    assert row["fenced"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/fencing/test_fence_coverage.py -q -k "no_longer_exempt or f3d2_is_complete or ingest_row_is_untouched" > /tmp/f3d2-9.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-9.txt
```
Expected: `PYTEST_EXIT=1` — nine keys where five are wanted, and
`SLICE_STATUS["F3d2"] == "pending"`.

- [ ] **Step 3: Retire the four exemptions**

In `tests/fencing/inventory.py`, delete the whole `"context.py"`, `"review_submit.py"`,
`"scorecard.py"` and `"uncited_claims.py"` entries from `TOOLS_NOT_YET_CONVERTED`. Leave
the other five exactly as they are — `references.py` keeps whatever Task 8 shrank it to.

- [ ] **Step 4: Close the slice**

In the same file, change the `SLICE_STATUS` line:

```python
    "F3d2": "complete",  # the submitters and the reads
```

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d2-9.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d2-9.txt
```
Expected: `PYTEST_EXIT=0`. `test_each_slice_matches_its_declared_status` is the one that
now has teeth: F3d2 is complete, so every row it owns must be `fenced: true`, and Task 2
means that value came from the source.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/test_fence_coverage.py
git commit -m "test(fencing): four modules leave the exemption list and F3d2 closes (F3d2 Task 9)"
```

- [ ] **Step 7: Prove the gate is now armed on a converted module**

In `src/call_manager/proposal_assist/tools/scorecard.py`, add
`from call_manager.db.models import EvaluatorJob` at the top and use it nowhere.

Run:
```bash
uv run pytest tests/fencing -q -k import_gate > /tmp/f3d2-9m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d2-9m.txt
```
Expected: FAIL, naming `scorecard.py` and `from call_manager.db.models import ...`. Before
Step 3 that same import was PINNED and the gate said nothing. That difference is what
retiring an exemption buys.

```bash
git checkout src/call_manager/proposal_assist/tools/scorecard.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/tools/scorecard.py
```

- [ ] **Step 8: Run the full suite**

Run:
```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d2-full.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d2-full.txt
```
Expected: `PYTEST_EXIT=0` and **3770 passed, 1 skipped**. That total is derived, not
measured: the master baseline of 3737 (`684d991`) plus 33 new tests —
5 (Task 1) + 4 (Task 2) + 2 (Task 3) + 4 (Task 4) + 3 (Task 5) + 3 (Task 6) + 3 (Task 7)
+ 6 (Task 8 — five in the slice file plus one parametrisation of
`test_every_query_result_is_recursively_free_of_orm_instances`, which `CASES` drives)
+ 3 (Task 9) = **33**.

The 36 migrated call sites (GC-13) add NOTHING to the total: those tests already exist and
are only being handed the new keywords. **3770 is the number a dry run must verify.** If your run differs,
work out WHICH term is wrong before touching anything — do not adjust the expectation to
match the run, and do not delete a test to reach it. **Read the `1 skipped` as the signal,
not the total**: it is the `ANTHROPIC_API_KEY`-gated test.

- [ ] **Step 9: Confirm the slice's shape**

Run:
```bash
uv run python -c "
from tests.fencing.inventory import (
    SLICE_STATUS, TOOLS_NOT_YET_CONVERTED, load_inventory, scan_tree
)
rows = load_inventory()
_s, fenced = scan_tree()
print('pins:', len(TOOLS_NOT_YET_CONVERTED), sorted(TOOLS_NOT_YET_CONVERTED))
print('F3d2 status:', SLICE_STATUS['F3d2'])
print('fenced tool_commit rows:', sum(1 for r in rows if r['kind']=='tool_commit' and r['fenced']))
print('measured fenced identities:', len(fenced))
print('F3d3 rows still unfenced:', sum(1 for r in rows if r['slice']=='F3d3' and not r['fenced']))
print('F3e rows still unfenced:', sum(1 for r in rows if r['slice']=='F3e' and not r['fenced']))
"
```
Expected exactly:
```
pins: 5 ['audit.py', 'citations.py', 'history.py', 'proposal.py', 'references.py']
F3d2 status: complete
fenced tool_commit rows: 5
measured fenced identities: 5
F3d3 rows still unfenced: 3
F3e rows still unfenced: 1
```
`F3e rows still unfenced: 1` is the one worth reading twice: it is
`fetch_reference_impl._do_fetch_and_ingest` and nothing else. If it reads 0, this slice
converted the ingest commit and crossed into F3e. If it reads 2, `fetch_reference_impl`
did not measure fenced — check Task 7 Step 7 before touching anything else.

---

## What F3d2 deliberately does NOT do

Named so a reviewer does not read them as gaps:

- **`tools/history.py` stays pinned.** Its only blocker is the `repo_dir` locator, it owns
  zero inventory rows, and converting it would need a read-only git port no spec section
  describes. `repo_dir` retires with the other capability holders in F3f. This is why the
  slice takes pins 9→5 rather than the 9→4 an early scoping note assumed.
- **`references.py`'s ingest commit is untouched.** `references.py:580` is
  `_do_fetch_and_ingest`'s write and belongs to F3e, which is also why
  `fetch_reference_impl` cannot be fenced here and why Task 3 moved its row.
- **`session_factory` stays on `ToolContext`.** The spec removes it in F3f's final commit,
  once nothing reaches for it. Five modules still do.
- **Reads are not fenced.** `get_workspace_snapshot` takes no stamp and has no generation
  predicate (spec §8). A workspace deleted mid-turn reads as `None`, which the tools answer
  as `{"linked": False}` — the shape they have always returned for an empty snapshot.
- **Accounting is not fenced.** Deliberate (spec §3.3): fencing a refund would strand jobs
  holding their slots. GC-5's interleaving test is the proof that replaces a generation
  predicate.
- **No `agent_view_*` decision.** `tools/citations.py` reads that read-time snapshot and
  `tools/proposal.py` writes it, so the two cannot convert independently. F3d3 owns it.

## Self-review notes

Checked against the spec and the survey that produced this cut:

| Requirement | Task |
|---|---|
| §7 "converts `submit_review_impl`, `submit_scorecard_impl` and `submit_uncited_claims_impl` to effect operations" | 5, 6, 7 |
| §7 "the two `references.py` accounting sites to owner-bound operations" | 8 |
| §7 "every remaining read to a query port returning DTOs" | 9 |
| §5 "TWO ORDERED LOCK CASES" | Already proven at the port in F3b (`test_f3b_lock_cases.py`); this slice adds the per-site conversion proofs |
| §5 "compensation disabled for each per-site test" | 5 Step 7, 6 Step 7, 7 Step 7, 8 Step 9, 9 Step 9 — each task's mutation removes the mechanism, not the guard |
| §5 "accounting proven by the interleaving it exists for" | 8 Step 1, `test_the_refund_does_not_touch_a_later_turns_row` |
| §5 "never return an attached instance" | 9 Steps 3, 4, 9 |
| Survey finding 1 — conversion erases its own row | 1 |
| Survey finding 2 — `fenced` is unfalsifiable | 2 |
| Survey finding 3 — identity encodes ownership | 3 |
| Exemption list 9 → 5 | 10 |

Three things a reviewer should push on, named here rather than discovered later:

1. **Task 2 changes `merge`'s contract for every caller.** The signature gains a
   parameter and `scan_tree` returns a pair. Every caller must be found and updated; Step 5
   gives the grep. A missed caller is a `TypeError` at regeneration time, which is loud, but
   a caller that passes an empty `fenced_ids` would silently unfence everything — check for
   that specifically.
2. **`F3D_IDENTITIES` is not touched at all, and that is the point.** Revision 0 planned
   to re-key it so `fetch_reference_impl` could move to F3e; revision 1 established the
   move was never needed (the ingest commit is a separate nested identity). If a future
   slice genuinely does need to re-slice a row, the `(slice, kind, symbol)` key will have
   to change then — but doing it speculatively here would have shipped a task whose only
   effect was to break the slice-status gate.
3. **The 3770 total is derived, not measured.** F3d1's plan had every count right and its
   reasoning wrong; this one has not been executed at all. Dry-run it before executing it.
