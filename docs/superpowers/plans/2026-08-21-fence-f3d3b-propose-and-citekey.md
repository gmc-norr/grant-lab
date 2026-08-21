# Fence F3d3b — the two propose sites

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `propose_citation_impl` and `_propose_artifact_edit_impl` to the effect
port, move the citekey-cache row to the slice whose mechanism it needs, and close F3d3.

**Architecture:** Both impls stop calling `proposing.py` and stop committing; they call
`Effects.propose_artifact_edit` / `Effects.propose_citation_insert`, which F3d1 already
built. `propose_citation_impl` keeps a READ-only session — reads are not fenced (spec §8)
and a session with no commit does not block the fenced measurement. No code moves, no new
port operation, no migration.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest. No frontend.

---

## Revision 1 — what this plan used to say, and why it does not any more

Revision 0 was reviewed by 61 adversarial agents against the real tree. 46 findings were
confirmed, 26 of them P1. Three were design-level and killed the original shape. They are
recorded here because a future slice will be tempted by the same moves.

**It tried to convert `_write_citekey_cache`, and that deadlocks.** Its `owns_txn=True`
branch runs at `citations/core.py:633`, inside `_insert_citation_locked`, whose session
took the **exclusive** commit mutex at `core.py:556` (`acquire_commit_mutex_live` ->
`pg_advisory_xact_lock`, transaction-scoped) and has not committed — the only commit in
that whole span is the one revision 0 proposed to delete. A port operation opens a
**second connection** and takes `pg_advisory_xact_lock_shared` on the **same key**.
Connection B waits for a lock connection A cannot release until B returns:
`lock_timeout` is 10s, so every accept-time insert would stall 10s and then raise
`CommitMutexTimeout`, which the helper's best-effort `except Exception` swallows — the
citekey cache silently never written. An agent reproduced it against a real
`pgvector:pg16` container. `port_impl/effects.py:14-30` documents this exact inversion in
its module docstring.

**The design already said where that effect belongs.** `ports/effects.py:118-119`:
*"Every F3b operation is DB-only (mechanism 1). The exclusive-mutex mechanism belongs to
effects that touch git or the filesystem, which arrive in F3e."* And spec §3.5 names
`_insert_citation_locked` as its example of an effect whose operation must own the entire
git-then-rows sequence under the mutex. `EFFECT_MECHANISMS` carries the rule that each
operation declares ONE mechanism and never upgrades between them. So the citekey write is
a **mechanism-2** site inside a **mechanism-1** slice. Task 1 moves its row to F3e rather
than forcing the category error. That is not dodging the gate — it is the cut the design
already drew, and the same move F3a made when it split F3d into F3d1/F3d2/F3d3.

**It tried to move a helper out of `tools/` that did not need to move.** Revision 0 had
`read_live_draft_text` "own its own session", which is impossible: there is no ambient
session factory. `Ports` carries `queries`, `effects`, `accounting` and nothing else
(`ports/__init__.py:25-34`), and `db/session.py:48` exposes only `make_session_factory(engine)`
— the factory is injected. The tool's only source is `ctx.session_factory`, which
revision 0's own test forbade it from naming. The two tasks cancelled each other out.

The measurement that dissolves it: **`fenced` is "port call AND no raw commit"**, so a
session used only for READS does not block it. Verified —

```
body: with ctx.session_factory() as session: session.get(...); ports.effects.propose_citation_insert(...)
sites (a row?): {'t'}
fenced: {(<tools/citations.py>, 't')}
```

And `session_factory` is ALREADY a pinned import-gate violation for `citations.py`,
retiring in F3f beside `repo_dir`. So the tool keeps its read session, this slice adds no
new violation, and nothing moves.

---

## Global Constraints

- **GC-1 — behaviour parity is the bar.** Every error string, every returned dict shape,
  and the ORDER in which checks run must survive. The ONE sanctioned change is the
  deletion/stale race: a converted site now answers `DELETED_ERROR_MESSAGE` or
  `BUSY_ERROR_MESSAGE` where it used to answer its own target error. Anything else is a
  regression — report it, do not absorb it.
- **GC-2 — a tool may not WRITE through a session.** Reads are not fenced (spec §8), and
  `propose_citation_impl` keeps a read-only session in this slice. What must go is the
  `session.commit()` and everything that depends on it. Do NOT write a test asserting
  `"session_factory" not in` the source: it is false by design here, and revision 0's
  version of that test contradicted its own Task 2.
- **GC-3 — no exemption retires.** `citations.py` and `proposal.py` still name `repo_dir`
  AND `session_factory`, both of which leave with the capability holders in **F3f**. Pins
  stay at 5. If `test_no_exemption_is_stale` ever tells you a pin is clean, stop: that is
  a bigger claim than this slice makes.
- **GC-4 — read the failure text before concluding.** The root conftest
  (`tests/conftest.py:135`) has an autouse teardown fixture that imports the app for every
  test in the repo, so a broken src import shows up as mass teardown **ERRORS** with zero
  assertion failures. Key every judgement on **FAILED**, never on ERROR.
- **GC-5 — a mutation must fail the NAMED test for the NAMED reason.** A test that goes
  red because collection aborted proves nothing. And a test a COMMENT can satisfy is not
  a test: revision 0 shipped an `inspect.getsource` check that passed under its own
  mutation because the comment beside the code contained the string. Prefer behavioural
  checks.
- **GC-6 — `git checkout <path>` destroys uncommitted work in that file** and cannot
  restore an untracked file. Commit before mutating, or restore from a copy.
- **Test commands:** full suite `uv run pytest -n 16 --dist loadfile -q`; never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Lint gate is `uv run ruff check .`
  — `ruff format` is NOT a gate. **Measured baseline at `fc618425`: 3788 passed, 1
  skipped.** The `1 skipped` is a real-SDK test gated on `ANTHROPIC_API_KEY`; `0 skipped`
  means a live API call ran.

---

## What the survey measured (2026-08-21, verified against `fc618425`)

Everything here was produced by running a command. Re-derive anything you rely on.

**The gate that goes red, and the one that does not.**
`test_each_slice_matches_its_declared_status` (`tests/fencing/test_fence_coverage.py:105`)
reads the **STORED** `fenced` field out of `fence_inventory.json` via `load_inventory()`.
It does NOT re-scan. So it stays green through the conversions and only matters at the
regeneration in Task 4. What DOES go red the moment a site converts:

- `test_the_inventory_matches_the_tree_exactly` — the JSON no longer matches a fresh scan.
- `test_no_tool_module_gained_a_port_call` (`tests/proposal_assist/test_f3d3a_relocation.py:170-186`)
  — it hard-asserts the fenced set equals **exactly** F3d2's five pairs, plus zero fenced
  F3d3 rows. It is F3d3a's, it is not in `tests/fencing`, and revision 0 never mentioned it.

**The two impls' REAL callers.** The registry never names `_propose_artifact_edit_impl`.
It wires two PUBLIC wrappers:

| wrapper | defined | registry site |
|---|---|---|
| `propose_edit_impl` | `tools/proposal.py:188` | `registry.py:331` (import), `:374` (`_safe_tool`) |
| `propose_spec_edit_impl` | `tools/proposal.py:206` | `:414` (`_safe_tool`) |

Both take only `(ctx, *, section_index, new_text)` today, so **both gain `ports` and
`invocation` too**, and **nine existing tests call them**. `propose_citation_impl` has
exactly ONE wiring site (`registry.py:760`); `:685` is the deferred import.

**`Invocation` already carries what the port needs.** `ports/invocation.py` holds
`origin_kind`, `origin_job_id`, `triggering_turn_id`, `triggering_prompt`, `created_by`,
`origin_session_id`, and `turn_selection` — the last documented at `:44-51` as existing
*precisely* for the propose tools, as a frozen deep copy, because `ToolContext` is mutable
and shared. **Use `invocation.turn_selection`, never `ctx.turn_selection`.**

**One live parity risk.** `propose_citation_impl` currently HARDCODES
`origin_kind="interactive_turn"` (`tools/citations.py:274`) and passes no
`origin_job_id`. `ProposeCitationInsert` carries no `origin_kind` — it comes from
`invocation.provenance()`. Task 3 must prove those agree for the interactive path rather
than assume it.

**Known, pre-existing, not this slice's to fix:** `_safe_tool` raises `RuntimeError` when
`pass_ports=True` and `ctx.ports is None`, and that raise sits OUTSIDE its try/except
(`registry.py:292-301`), so it escapes the MCP handler instead of returning an error dict.
Unreachable while every factory passes ports. Note it if you touch that code; do not fix
it here.

---

## Deliberately NOT done here

- **`_write_citekey_cache` is not touched.** Its row moves to F3e (Task 1). Converting it
  needs a mechanism-2 operation owning the whole accept-time git-then-rows sequence, which
  is F3e's shape and F3e's spec section.
- **No new port operation.** `EFFECT_MECHANISMS` and `EFFECT_NAMES` are untouched, so
  neither set-equality gate fires. `f3b_seeds.py` needs no new seeder — it has none for
  `Reference`, which revision 0's Task 5 would have required.
- **No exemption retires**, per GC-3.
- **`collab/kernel.py`, `accepting.py` and `proposing.py` are not touched.**
  `proposing.py` keeps `propose_prose_edit` and `propose_citation_insert`; tests and
  `accepting.py` still use them.

---

## File Structure

| file | responsibility after this slice |
|---|---|
| `tests/fencing/inventory.py` | citekey row -> F3e in `F3D_IDENTITIES`; `SLICE_STATUS["F3d3"] = "complete"`; shrunken pins |
| `tests/fencing/fence_inventory.json` | regenerated |
| `tests/proposal_assist/test_f3d3a_relocation.py` | its fenced-set literal restated at each conversion |
| `src/call_manager/proposal_assist/tools/proposal.py` | `_propose_artifact_edit_impl` + both wrappers on the port |
| `src/call_manager/proposal_assist/tools/citations.py` | `propose_citation_impl` writes through the port, reads through a session |
| `src/call_manager/proposal_assist/tools/registry.py` | three wiring sites pass `ports` / `invocation` |
| `tests/proposal_assist/test_f3d3b_conversion.py` | new — the slice's own claims |

---

### Task 1: The citekey-cache row moves to F3e

Not a bookkeeping move. `_write_citekey_cache`'s owner branch runs inside a transaction
holding the workspace commit mutex EXCLUSIVELY, so the operation that fences it must own
the whole git-then-rows sequence under that mutex — mechanism 2, which
`ports/effects.py:118-119` says arrives in F3e. Fencing it with a mechanism-1 operation
deadlocks; that is measured, not predicted.

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/fence_inventory.json`
- Create: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing importable.

- [ ] **Step 1: Reproduce the reason before acting on it**

Do not take this plan's word for the deadlock. Confirm the shape yourself:

```bash
grep -n "acquire_commit_mutex_live\|session.commit()\|session.rollback()" src/call_manager/proposal_assist/citations/core.py
sed -n '14,30p' src/call_manager/proposal_assist/port_impl/effects.py
```
Expected: `acquire_commit_mutex_live` at `:556`, the only `session.commit()` at `:521`
(inside `_write_citekey_cache`'s owner arm), and a module docstring in `port_impl` warning
"Call these operations with no other transaction open for the same workspace."

Record in your report that you checked, and what you saw. If the mutex call is NOT on the
path to `:633`, this task's justification is wrong and you should stop.

- [ ] **Step 2: Write the failing test**

Create `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
"""F3d3b — the two propose sites go onto the effect port, and F3d3 closes.

Spec 2026-08-18 section 7 assigns three rows to F3d3. Two of them are DB-only writes
(mechanism 1) and convert here. The third, _write_citekey_cache, is reached only from
inside a transaction already holding the workspace commit mutex EXCLUSIVELY, so it needs
an operation owning the whole git-then-rows sequence — mechanism 2, which
ports/effects.py:118-119 says arrives in F3e. Its row moves there.
"""

from __future__ import annotations


def test_the_citekey_row_belongs_to_the_slice_whose_mechanism_it_needs():
    """The reassignment, stated where a reviewer will look for it.

    Fencing this row with a mechanism-1 operation is not merely inelegant, it hangs: the
    caller holds pg_advisory_xact_lock on the workspace's mutex key and the port would
    open a SECOND connection asking for pg_advisory_xact_lock_shared on the same key.
    port_impl/effects.py's module docstring names this inversion explicitly.
    """
    from tests.fencing.inventory import F3D_IDENTITIES, load_inventory

    assert ("F3e", "tool_commit", "_write_citekey_cache") in F3D_IDENTITIES
    assert ("F3d3", "tool_commit", "_write_citekey_cache") not in F3D_IDENTITIES

    row = next(r for r in load_inventory() if r["symbol"] == "_write_citekey_cache")
    assert row["slice"] == "F3e"
    assert row["fenced"] is False


def test_f3d3_now_owns_exactly_the_two_propose_sites():
    from tests.fencing.inventory import load_inventory

    assert {r["symbol"] for r in load_inventory() if r["slice"] == "F3d3"} == {
        "propose_citation_impl",
        "_propose_artifact_edit_impl",
    }
```

- [ ] **Step 3: Run it to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q > /tmp/f3d3b-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-1.txt
```
Expected: both FAIL — the identity is still keyed `F3d3`.

- [ ] **Step 4: Move the identity**

In `tests/fencing/inventory.py`, change the `F3D_IDENTITIES` entry:

```python
    ("F3e", "tool_commit", "_write_citekey_cache"),
```

Read the comment block above that set before editing it. It says *"Deleting an entry here
is not a way to make a test pass."* This is a MOVE, not a delete — the identity survives
with a different owner — but the comment is the right thing to be arguing with, so extend
it rather than ignoring it:

```python
#: `_write_citekey_cache` moved F3d3 -> F3e in F3d3b. Not to make a gate pass: its only
#: owner-transaction caller holds the commit mutex EXCLUSIVELY, so the operation that
#: fences it must own the whole git-then-rows sequence under that mutex — mechanism 2,
#: which ports/effects.py says arrives in F3e. A mechanism-1 operation on a second
#: connection blocks for lock_timeout and then raises. Measured, not predicted.
```

- [ ] **Step 5: Regenerate and read the diff**

```bash
uv run python -m tests.fencing.regenerate_inventory
git diff tests/fencing/fence_inventory.json
```
Expected: **exactly one changed line** — the citekey row's `slice`, `F3d3` -> `F3e`. Its
`fenced` stays `false`, its `path` stays `citations/core.py`, row count stays **20**.
`RENAMES` must be `{}`; this slice renames nothing.

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py tests/fencing -q > /tmp/f3d3b-1b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-1b.txt
uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -3
```
Expected: `PYTEST_EXIT=0` and **3790 passed, 1 skipped** (3788 + this task's 2). Both F3d3
and F3e are still `pending` with all rows `fenced: false`, so the bidirectional gate is
satisfied on both sides. Nothing has converted yet.

- [ ] **Step 7: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/fence_inventory.json tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "test(fence): the citekey-cache row moves to the slice whose mechanism it needs (F3d3b Task 1)"
```

---

### Task 2: `_propose_artifact_edit_impl` and both wrappers go onto the port

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py`
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: `Effects.propose_artifact_edit`, `ProposeArtifactEdit` (F3d1, existing).
- Produces: `propose_edit_impl(ctx, *, ports, invocation, section_index, new_text)` and
  `propose_spec_edit_impl(...)` with the same added parameters.
  Return shape unchanged: `{"proposed_change_id": str, "status": "pending"}` or `{"error": str}`.

- [ ] **Step 1: Read what the port already does**

```bash
uv run python -c "
import ast
s=open('src/call_manager/proposal_assist/port_impl/effects.py').read()
for n in ast.walk(ast.parse(s)):
    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='propose_artifact_edit':
        print(ast.get_source_segment(s,n))
"
```
The port impl already folds `selection_anchor` / `selection_from` / `selection_to` into
the payload, guarded on `target_artifact == "draft"`. **So the conversion DELETES the
payload tweak at `tools/proposal.py:176-183`** — it moved to the port in F3d1. Compare
the two key-for-key before deleting anything; if they disagree, stop and report.

- [ ] **Step 2: Write the failing test**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_the_artifact_edit_uses_the_invocation_snapshot_not_the_live_context():
    """ports/invocation.py:44-51 exists for exactly this call site. ToolContext is
    MUTABLE and shared — set_turn_provenance restamps it every turn — so a tool that
    reads ctx.turn_selection when its effect happens can copy a LATER turn's selection
    into a durable proposed-change row.

    The check is on the RECEIVER, not the attribute name. A first draft of this test
    walked __code__.co_names, which records `turn_selection` for BOTH `ctx.turn_selection`
    and `invocation.turn_selection` — so it passed against the unconverted source and
    proved nothing. Verified: the AST form below returns True/False for the two
    receivers respectively; the co_names form returns True for both.
    """
    import ast
    import inspect
    import textwrap

    from call_manager.proposal_assist.tools import proposal as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._propose_artifact_edit_impl)))

    def reads(receiver: str, attr: str) -> bool:
        return any(
            isinstance(n, ast.Attribute)
            and n.attr == attr
            and isinstance(n.value, ast.Name)
            and n.value.id == receiver
            for n in ast.walk(tree)
        )

    assert reads("invocation", "turn_selection"), (
        "reads the selection off the live ToolContext instead of the dispatch snapshot — "
        "a later turn's selection can be copied into a durable proposed-change row"
    )
    assert not reads("ctx", "turn_selection")
    assert reads("ports", "effects") or "propose_artifact_edit" in ast.dump(tree), (
        "does not call the effect port"
    )
    assert not reads("ctx", "session_factory"), "still opens a session to write"
```

- [ ] **Step 3: Run it to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k artifact_edit > /tmp/f3d3b-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-2.txt
```
Expected: FAIL on `still opens a session to write`.

- [ ] **Step 4: Convert the impl**

Keep the docstring, the keyword-only shape, and both `not_read_err` returns byte-for-byte.

```python
async def _propose_artifact_edit_impl(
    ctx,
    *,
    ports,
    invocation,
    target_artifact: str,
    head: str | None,
    sections: dict[int, str],
    section_index: int,
    new_text: str,
    not_read_err: str,
) -> dict[str, Any]:
    """<<< keep the existing docstring verbatim >>>"""
    if head is None:
        return {"error": not_read_err}
    base_section_sha = sections.get(section_index)
    if base_section_sha is None:
        return {"error": not_read_err}

    try:
        pc_id = ports.effects.propose_artifact_edit(
            invocation.stamp(),
            ProposeArtifactEdit(
                target_artifact=target_artifact,
                section_index=section_index,
                new_text=new_text,
                base_commit_sha=head,
                base_section_sha=base_section_sha,
                provenance=invocation.provenance(),
                selection=invocation.turn_selection,
            ),
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"proposed_change_id": str(pc_id), "status": "pending"}
```

**`invocation.turn_selection`, not `ctx.turn_selection`.** The former is a frozen deep
copy taken at dispatch; the latter is live and shared. Passing the wrong one is a real
misattribution bug that no test in this plan would catch.

Catch **all three** exceptions. `ports/effects.py`'s Protocol docstring says a tool
catching only the first two downgrades a transient busy into "internal error", because
`_safe_tool` turns every unnamed exception into that.

None of `ProposeArtifactEdit`, `EffectRejected`, `WorkspaceGenerationStale`,
`CommitMutexTimeout`, `DELETED_ERROR_MESSAGE`, `BUSY_ERROR_MESSAGE` is imported in
`tools/proposal.py` today — its imports at `:21-26` are `artifacts`, `citations.gitio`,
`collab.canonical`, `collab.state`, `editing.sectionizer`, `tools.registry`. Add them from
`ports.effects` and `ports.errors`. The lazy `propose_prose_edit` import and its
module-cycle comment go away.

- [ ] **Step 5: Convert both wrappers**

`propose_edit_impl` (`:188`) and `propose_spec_edit_impl` (`:206`) each gain `ports` and
`invocation` keyword-only parameters and pass them straight through. Nothing else in them
changes.

- [ ] **Step 6: Wire the registry**

The registry names the WRAPPERS, not the impl: `registry.py:331` (import), `:374`
(`propose_edit_impl` inside `_safe_tool`) and `:414` (`propose_spec_edit_impl`). Read how
`submit_review_impl` is wired and copy that exactly:

```bash
grep -n -B3 -A8 "submit_review_impl" src/call_manager/proposal_assist/tools/registry.py
```

Do not invent a new wiring shape.

- [ ] **Step 7: Fix the nine existing wrapper tests**

```bash
grep -rn "propose_edit_impl\|propose_spec_edit_impl" tests/ --include=*.py
```
Each call site must now supply `ports=` and `invocation=`. **Change how they CALL; do not
change what they ASSERT.** If an assertion has to move for a test to pass, that is a
behaviour change — stop and record it under PLAN DEFECTS.

- [ ] **Step 8: Restate F3d3a's fenced-set literal**

`tests/proposal_assist/test_f3d3a_relocation.py:170-186`
(`test_no_tool_module_gained_a_port_call`) asserts the fenced set equals exactly F3d2's
five pairs and that zero F3d3 rows are fenced. Both are now false. **This must be edited
in THIS commit** or the suite is red for a reason nothing in the commit fixes.

Add the new pair to the literal set and change the F3d3 count assertion to `== 1`. Update
the docstring: its claim was "F3d2's five and no more — F3d3a converts nothing", which was
true of F3d3a and is not true here. Do NOT weaken it into something derived — the point of
that test is that the set is written down.

- [ ] **Step 9: Run everything**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-2b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-2b.txt
uv run pytest tests/fencing -q 2>&1 | tail -3
```
Expected: **3791 passed, 1 skipped** (3790 + this task's 1) and `tests/fencing` **RED on
`test_the_inventory_matches_the_tree_exactly` only** — the JSON now disagrees with a fresh
scan, and it stays that way until Task 4 regenerates.

`test_each_slice_matches_its_declared_status` should still PASS: it reads the stored JSON,
which has not been regenerated. If it fails, the JSON was regenerated early — find out by
whom. If anything OUTSIDE `tests/fencing` is red, that is collateral this plan did not
predict: record it under PLAN DEFECTS with the failing test names.

- [ ] **Step 10: Prove the fence is real**

Temporarily change the `WorkspaceGenerationStale` handler to `raise`. Then run the tests
that actually exercise the deleted-workspace path — find them rather than guessing, since
revision 0's `-k` selector matched eleven tests and none of them called this impl:

```bash
grep -rln "propose_edit_impl\|propose_spec_edit_impl" tests/ --include=*.py
```
Run those files. Expected: the deleted-workspace case raises instead of returning an error
dict. If nothing changes, no test covers the stale path and the fence is unproven — say so
under PLAN DEFECTS rather than moving on.

Restore from a copy (GC-6).

- [ ] **Step 11: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/tools/proposal.py src/call_manager/proposal_assist/tools/registry.py tests/
git commit -m "refactor(proposal): the artifact-edit propose and both wrappers go onto the port (F3d3b Task 2)"
```

---

### Task 3: `propose_citation_impl` writes through the port

Its session stops writing and keeps reading. Everything between the two is unchanged.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py`
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`
- Modify: `tests/proposal_assist/tools/test_propose_citation.py`, `tests/proposal_assist/collab/test_canonical_consumers.py`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: `Effects.propose_citation_insert`, `ProposeCitationInsert` (F3d1).
- Produces: `propose_citation_impl(ctx, *, ports, invocation, reference_id, anchor, chunk_index, evidence_quote, stance, confidence)`.
  Return shape unchanged.

- [ ] **Step 1: Settle the provenance parity question FIRST**

This is the one place the conversion can silently change stored data. Today the tool
hardcodes `origin_kind="interactive_turn"` (`tools/citations.py:274`) and passes no
`origin_job_id`. The port takes both from `invocation.provenance()` and the stamp.

```bash
uv run python -c "
import ast
s=open('src/call_manager/proposal_assist/tools/citations.py').read()
for n in ast.walk(ast.parse(s)):
    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='propose_citation_impl':
        seg=ast.get_source_segment(s,n)
        print('\n'.join(l for l in seg.splitlines() if 'origin' in l or 'triggering' in l or 'created_by' in l))
"
grep -n "origin_kind" src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/ports/invocation.py | head
```

Decide, and WRITE DOWN in your report, whether `invocation.origin_kind` equals
`"interactive_turn"` on every path that reaches this tool. If it does, the hardcode is
redundant and the port is strictly better. **If it does not, stop** — silently restamping
durable provenance is a GC-1 violation, and the plan needs a decision rather than an
implementer's guess.

- [ ] **Step 2: Write the failing tests**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_the_citation_propose_writes_through_the_port_and_reads_through_a_session():
    """GC-2 precisely: the WRITE authority goes, the read stays.

    A session used only for reads holds no commit, and `fenced` is measured as
    "port call AND no raw commit" — so this tool measures fenced while still reading
    through a session. Its session_factory and repo_dir namings are already pinned
    import-gate violations that retire in F3f.
    """
    import inspect

    from call_manager.proposal_assist.tools import citations as mod

    src = inspect.getsource(mod.propose_citation_impl)
    assert "ports.effects.propose_citation_insert" in src, "does not call the effect port"
    assert "session.commit()" not in src, "still commits directly"
    assert "propose_citation_insert(\n            session=" not in src


def test_the_tombstone_check_survived_the_conversion():
    """The check most likely to be lost. ReferenceView.is_tombstoned exists so the tool
    can tell 'no such reference' from 'tombstoned, cannot be cited' — two different
    messages to the agent. A conversion that reads only `is None` collapses them."""
    import inspect

    from call_manager.proposal_assist.tools import citations as mod

    src = inspect.getsource(mod.propose_citation_impl)
    assert "reference is tombstoned and cannot be cited" in src
```

- [ ] **Step 3: Run them to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k "citation_propose or tombstone" > /tmp/f3d3b-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-3.txt
```
Expected: FAIL on `does not call the effect port`. The tombstone test passes already — it
is a ratchet against the conversion, not a red-to-green step. Say so in your report.

- [ ] **Step 4: Convert the write only**

Keep the whole session block up to and including the section-keying at
`tools/citations.py:225-260`. Replace ONLY the tail — the `propose_citation_insert(...)`
call, the `pc_id = str(pc.id)` and the `session.commit()` — and move it OUTSIDE the
`with` block so the read session closes before the effect runs:

```python
    try:
        pc_id = ports.effects.propose_citation_insert(
            invocation.stamp(),
            ProposeCitationInsert(
                section_index=section_index,
                base_commit_sha=base_commit_sha,
                base_section_sha=base_section_sha,
                reference_id=ref_uuid,
                anchor=anchor,
                chunk_index=chunk_index,
                evidence_quote=evidence_quote,
                stance=stance,
                confidence=confidence,
                provenance=invocation.provenance(),
            ),
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"proposed_change_id": str(pc_id), "status": "pending"}
```

**The effect call must be OUTSIDE the `with ctx.session_factory()` block.** That is the
same two-connection inversion that killed revision 0's Task 5: the port opens its own
session and takes the shared lease, and calling it with the read session still open on the
same workspace is what `port_impl/effects.py:14-30` warns against. The read session takes
no advisory lock today, so it would not currently hang — but the ordering is the rule, and
this is the file where the rule was learned.

`reference_id` is a `uuid.UUID` on the DTO, not a string — the old
`propose_citation_insert` took `str(ref_uuid)`. Check the DTO before converting.

Every error string keeps its bytes and every check keeps its order (GC-1).

- [ ] **Step 5: Wire the registry and fix the two test modules**

One wiring site: `registry.py:760`. (`:685` is the deferred import.)

```bash
grep -rn "propose_citation_impl" tests/ --include=*.py
```
`tests/proposal_assist/tools/test_propose_citation.py` and
`tests/proposal_assist/collab/test_canonical_consumers.py` call it directly. Change how
they CALL it; do not change what they ASSERT.

`test_canonical_consumers.py` exists to prove the collab-on branch reads room-canonical
text. That branch is untouched by this slice, so confirm the test still reaches it rather
than passing vacuously.

- [ ] **Step 6: Restate F3d3a's fenced-set literal again**

Same edit as Task 2 Step 8, one more pair, and the F3d3 fenced count becomes `== 2`.

- [ ] **Step 7: Run everything**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-3b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-3b.txt
uv run pytest tests/fencing -q 2>&1 | tail -3
```
Expected: **3793 passed, 1 skipped** (3791 + this task's 2), `tests/fencing` still RED on
`test_the_inventory_matches_the_tree_exactly` only.

- [ ] **Step 8: Prove the fence, and prove the read still happens**

Two mutations, both restored from a copy afterwards (GC-6):

1. `WorkspaceGenerationStale` handler -> `raise`. Run the two test modules from Step 5.
   Expected: the deleted-workspace case raises.
2. Make `ports.effects.propose_citation_insert` a no-op returning a fixed UUID. Expected:
   the tests that assert a row LANDED go red, the tests that assert an early `{"error": ...}`
   stay green. That separates "the port does the write" from "the tool still validates".

- [ ] **Step 9: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/tools/citations.py src/call_manager/proposal_assist/tools/registry.py tests/
git commit -m "refactor(citations): the citation propose writes through the effect port (F3d3b Task 3)"
```

---

### Task 4: F3d3 closes

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/fence_inventory.json`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

- [ ] **Step 1: Regenerate and read the diff before flipping anything**

```bash
uv run python -m tests.fencing.regenerate_inventory
git diff tests/fencing/fence_inventory.json
```
Expected: exactly two `fenced` fields flip `false` -> `true`. Row count stays **20**.

**If fewer than two flipped, do not flip the status.** Find which row still measures
unfenced. The likeliest cause is a raw commit left in the converted qualname —
`find_fenced_tool_commits` requires port call AND no raw commit, and `_RAW_COMMIT_ATTRS`
is `{"commit", "begin"}`, so a `with session.begin():` counts even with no `.commit()`.

- [ ] **Step 2: Flip the status**

```python
    "F3d3": "complete",
```

- [ ] **Step 3: Shrink both exemptions**

```bash
uv run python -c "
from tests.fencing.import_gate import check_file, normalized_violation, tool_key, tool_modules
for p in tool_modules():
    k = tool_key(p)
    if k in ('citations.py', 'proposal.py'):
        vs = check_file(p)
        print(f'--- {k}: {len(vs)} raw ---')
        for v in sorted({normalized_violation(x) for x in vs}): print(repr(v))
"
```
Replace both pinned sets with exactly what it prints. Before: **citations.py 15 raw**,
**proposal.py 14 raw**. Report both after-counts.

Neither RETIRES (GC-3): both keep `repo_dir` AND `session_factory`. If a set comes back
empty, stop — `test_no_exemption_is_stale` is telling you something this slice did not
expect.

- [ ] **Step 4: Write the closing tests**

```python
def test_both_f3d3_rows_are_fenced_and_the_slice_says_so():
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = {r["symbol"]: r for r in load_inventory() if r["slice"] == "F3d3"}
    assert set(rows) == {"propose_citation_impl", "_propose_artifact_edit_impl"}
    assert all(r["fenced"] for r in rows.values())
    assert SLICE_STATUS["F3d3"] == "complete"


def test_the_exemption_list_still_has_five_keys():
    """This slice SHRINKS two pin sets and retires neither: both modules still name
    repo_dir and session_factory, which are F3f's."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py", "citations.py", "history.py", "proposal.py", "references.py",
    }
```

- [ ] **Step 5: Run everything**

```bash
uv run pytest tests/fencing -q > /tmp/f3d3b-4.txt 2>&1; echo "FENCE_EXIT=$?"; tail -20 /tmp/f3d3b-4.txt
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-4b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-4b.txt
uv run ruff check .
```
Expected: `FENCE_EXIT=0` — the first green fencing suite since Task 2 — and the full suite
green at **3795 passed, 1 skipped** (3793 + 2). Derive that yourself from the tests you
actually added and report your arithmetic beside the run. If they disagree, find which
term is wrong. Do not adjust either to match the other and do not delete a test to reach
a number.

- [ ] **Step 6: Prove the flip is load-bearing**

Set `SLICE_STATUS["F3d3"]` back to `"pending"`.

```bash
uv run pytest tests/fencing -q -k slice_matches > /tmp/f3d3b-4m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-4m.txt
```
Expected: FAIL on `test_each_slice_matches_its_declared_status` naming BOTH rows as
`fenced=True but slice F3d3 is pending`. That is the gate's other direction, and it is why
the flip could not have happened earlier.

Restore from a copy (GC-6).

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/fence_inventory.json tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "test(fence): F3d3 closes — both propose sites measure fenced (F3d3b Task 4)"
```

---

### Task 5: Confirm the slice claimed nothing it did not do

- [ ] **Step 1: Write the closing checks**

```python
def test_the_citekey_cache_is_untouched():
    """The row moved; the code did not. If a later slice finds this failing, it is
    converting the accept path — which is F3e's job and needs a mechanism-2 operation."""
    import inspect

    from call_manager.proposal_assist.citations import core

    src = inspect.getsource(core._write_citekey_cache)
    assert "session.commit()" in src
    assert "begin_nested" in src
    assert "ports" not in inspect.signature(core._write_citekey_cache).parameters


def test_no_new_effect_operation_was_added():
    """EFFECT_MECHANISMS and EFFECT_NAMES are set-equality gates; this slice converts
    onto operations F3d1 already built and adds none."""
    from call_manager.proposal_assist.ports.effects import EFFECT_MECHANISMS

    assert set(EFFECT_MECHANISMS) == {
        "submit_scorecard",
        "submit_review",
        "submit_uncited_claims",
        "propose_artifact_edit",
        "propose_citation_insert",
    }
```

- [ ] **Step 2: Run and report the shape**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q 2>&1 | tail -3
uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -3
uv run python -c "
from tests.fencing.inventory import SLICE_STATUS, TOOLS_NOT_YET_CONVERTED, load_inventory
from tests.fencing.import_gate import check_file, tool_key, tool_modules
rows = load_inventory()
print('inventory rows:', len(rows))
print('pins:', len(TOOLS_NOT_YET_CONVERTED), sorted(TOOLS_NOT_YET_CONVERTED))
print('F3d3:', SLICE_STATUS['F3d3'], '| unfenced F3d3 rows:', sum(1 for r in rows if r['slice']=='F3d3' and not r['fenced']))
print('still pending:', sorted({r['slice'] for r in rows if not r['fenced']}))
print('F3e rows:', sorted(r['symbol'] for r in rows if r['slice']=='F3e'))
for p in tool_modules():
    if tool_key(p) in ('citations.py','proposal.py'):
        print(f'  {tool_key(p)}: {len(check_file(p))} raw')
"
```
Expected exactly:
```
inventory rows: 20
pins: 5 ['audit.py', 'citations.py', 'history.py', 'proposal.py', 'references.py']
F3d3: complete | unfenced F3d3 rows: 0
still pending: ['F3e', 'F4', 'F5']
F3e rows: ['_do_fetch_and_ingest'-ish, '_write_citekey_cache']
  citations.py: <below 15> raw
  proposal.py: <below 14> raw
```

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "test(fence): F3d3b converted two sites and claimed nothing more (F3d3b Task 5)"
```

---

## Self-review notes

| Requirement | Task |
|---|---|
| `propose_citation_impl` -> effect operation (spec §7) | 3 |
| `_propose_artifact_edit_impl` -> effect operation (spec §7) | 2 |
| The citekey row reaches a slice whose mechanism can fence it | 1 |
| The two public wrappers and all nine of their tests | 2 |
| F3d3a's two exact-equality pins restated | 2 Step 8, 3 Step 6 |
| Provenance parity for the hardcoded `origin_kind` | 3 Step 1 |
| Slice status flips only once the inventory agrees | 4 |
| Pins shrink, neither retires | 4 Step 3, 5 |

Four things a reviewer should push on:

1. **The row reassignment is the slice's central claim, and it is arguable.** It rests on
   `ports/effects.py:118-119` saying the exclusive-mutex mechanism arrives in F3e, on
   §3.5 naming `_insert_citation_locked` as the effect that must own its whole sequence,
   and on a measured deadlock. Task 1 Step 1 makes the implementer reproduce the reason
   before acting. If a reviewer rejects the argument, the consequence is not a smaller
   slice — it is that F3d3 cannot close until the accept-time operation exists.
2. **`tests/fencing` is RED across Tasks 2 and 3**, on
   `test_the_inventory_matches_the_tree_exactly` only. Revision 0 got the mechanism wrong
   here and named the slice-status gate, which actually reads the STORED JSON and stays
   green until regeneration. If the red test is a different one, that is a plan defect.
3. **`invocation.turn_selection` vs `ctx.turn_selection` is a real bug waiting to be
   written.** `ToolContext` is mutable and shared; the snapshot exists for this call site.
   Task 2 Step 2's test checks the RECEIVER via the AST. Two weaker forms were tried and
   rejected while writing this plan: a source grep (satisfied by a comment) and a
   `__code__.co_names` walk (records `turn_selection` for `ctx.` and `invocation.` alike,
   so it passes against unconverted source). Both were verified to fail to discriminate
   before the AST form was chosen. This project has now shipped a comment-satisfiable
   test twice; the third time it was caught before writing.
4. **Every total after 3788 is DERIVED.** The baseline was measured at `fc618425`. The
   per-task numbers assume the only collateral is what this plan names; revision 0's
   totals were wrong for exactly that reason. Falsify them.
