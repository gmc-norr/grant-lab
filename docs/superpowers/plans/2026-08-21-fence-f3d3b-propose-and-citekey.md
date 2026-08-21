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

Spec 2026-08-18 section 7 (as amended 2026-08-21) assigns TWO rows to F3d3, both DB-only
writes on mechanism 1. It previously assigned three; _write_citekey_cache moved to F3e
because it is reached only from inside a transaction already holding the workspace commit
mutex EXCLUSIVELY, so the operation that fences it must own the whole git-then-rows
sequence — mechanism 2, which section 3.5 and EFFECT_MECHANISMS place in F3e.
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

- [ ] **Step 4: Move the identity, and reconcile the source of truth**

The governing design assigns all three rows to F3d3 (`docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md:403`)
and describes F3e as "the ingest pipeline" (`:477`). **Both were amended on 2026-08-21**
in the same commit as this plan's revision 1: §7 now allocates two rows to F3d3 and two to
F3e with a revision note, and F3e is retitled "the ingest pipeline, and the mechanism-2
effects". Read that amendment before doing this step — if the spec on disk does NOT say
that, the reassignment has no source of truth and you must stop rather than proceed on the
plan's word alone.

Also fix the now-stale commentary above `F3D_IDENTITIES` in `tests/fencing/inventory.py:310`:
it opens "The eight identities spec §7's F3d owns". After this move F3d owns seven and
F3e owns one, and the set still holds eight entries — so the sentence is wrong in a way
that reads as correct. Restate it as "The eight identities spec §7 pins by
(slice, kind, symbol) — seven F3d's, one F3e's since F3d3b."

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

- [ ] **Step 5: Edit the JSON row's slice BY HAND, then prove regeneration is a no-op**

**Regeneration will not do this for you.** `merge()` deliberately carries the stored slice
forward — `tests/fencing/inventory.py:297-300`:

```python
            "slice": (
                prev["slice"] if prev is not None
                else _SLICE_FOR_KIND.get(s.kind, _DEFAULT_SLICE)
            ),
```

`slice` is a HUMAN-owned field, like `fenced` was before F3d2 measured it. A first draft
of this task changed only `F3D_IDENTITIES` and expected regeneration to follow; it does
not, and the row stays `F3d3` while the ratchet says `F3e` — which
`test_every_f3d_identity_is_still_in_the_inventory` catches as a missing identity, with a
message that does not explain why.

So edit the row in `tests/fencing/fence_inventory.json` directly: find the object whose
`symbol` is `_write_citekey_cache` and change `"slice": "F3d3"` to `"slice": "F3e"`.
Change nothing else on that row.

Then prove regeneration agrees rather than fights:

**Not `git diff --exit-code`.** The edit is deliberate and uncommitted, so a diff against
HEAD returns 1 no matter what regeneration does, and the `&&` success branch can never
run. A first draft of this step did exactly that, and so did revision 0's Task 1 — the
same mistake twice, which is why it is spelled out here. Compare the file against ITSELF,
before and after:

```bash
cp tests/fencing/fence_inventory.json /tmp/f3d3b-inv-before.json
uv run python -m tests.fencing.regenerate_inventory
diff -q /tmp/f3d3b-inv-before.json tests/fencing/fence_inventory.json \
  && echo "REGENERATION IS A NO-OP — merge() carries the hand-edited slice forward" \
  || echo "REGENERATION CHANGED THE FILE — see the diff below"
diff /tmp/f3d3b-inv-before.json tests/fencing/fence_inventory.json
```
Expected: the NO-OP echo, and the second `diff` printing nothing. If regeneration reverts
your edit, `merge()` does NOT preserve the stored slice the way this step claims — stop,
because the whole task rests on it.

Then confirm the change against HEAD is exactly one line:
```bash
git diff --stat tests/fencing/fence_inventory.json
```
Expected: 1 insertion, 1 deletion. `fenced` stays `false`, `path` stays
`citations/core.py`, row count stays **20**, `RENAMES` stays `{}`.

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

- [ ] **Step 7: Fix `_make_ctx()` — the registry closure tests are consumers too**

`_safe_tool` raises `RuntimeError` when `pass_ports=True` and `ctx.ports is None`
(`registry.py:292-297`), BEFORE the impl is called and OUTSIDE its try/except. So every
registry-level test that drives a converted tool through its closure breaks the moment
you wire it — and a grep for the impl's NAME cannot find them, because they go through
`_collect_tools`.

Measured: `tests/proposal_assist/test_tools_registry.py:46` defines `_make_ctx()` with no
`ports`, and it has **7 call sites**. But that file also builds **8 bare `ToolContext(...)`
inline**, so fixing the helper is not enough — enumerate both:

```bash
grep -n "_make_ctx()" tests/proposal_assist/test_tools_registry.py
grep -n "ToolContext(" tests/proposal_assist/test_tools_registry.py
```

Two tests drive tools this slice converts, and they get their context DIFFERENTLY:

| test | line | context |
|---|---|---|
| `test_propose_citation_closure_returns_error_envelope_for_bad_ref` | `:336` | `_make_ctx()` |
| `test_propose_edit_closure_returns_pending_envelope` | `:275` | its OWN inline `ToolContext(...)` |

A revision of this plan claimed 8 `_make_ctx` call sites and that both tests used it. Both
were wrong — the first because `grep -c "_make_ctx("` counts the `def` line. **Count call
sites with `_make_ctx()`, not `_make_ctx(`.**

Fix BOTH contexts. `ToolContext` takes `ports` and `lifecycle_generation`; give each a
real bundle and a generation:

```python
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=None,          # type: ignore[arg-type]
        lifecycle_generation=0, # Workspace.lifecycle_generation has server_default 0
        ports=build_ports(pg_session_factory),
    )
```

`lifecycle_generation` is NOT optional decoration: `Invocation.stamp()` raises
`UnfencedInvocation` when it is `None` (`ports/invocation.py`), and `_safe_tool` builds the
invocation before calling the tool. A context with ports but no generation fails at the
port call instead of at the guard, which is a more confusing failure.

There is no earlier commit to copy from — `git log -S "_make_ctx"` on that file shows no
F3d2 change to this helper. A revision of this plan told the implementer to `git show <the
F3d2 commit>`, a literal placeholder for a commit that does not exist.

- [ ] **Step 8: Fix the nine existing wrapper tests**

```bash
grep -rn "propose_edit_impl\|propose_spec_edit_impl" tests/ --include=*.py
```
Each call site must now supply `ports=` and `invocation=`.

**Their contexts have no generation.** `tests/proposal_assist/tools/test_propose_edit.py`
(and the citation one at `:81`) build a `ToolContext` without `lifecycle_generation`, which
defaults to `None` — and `Invocation.stamp()` raises `UnfencedInvocation` on `None`, at the
port call rather than at the fixture. So each converted call site needs THREE things, not
two: the real context, `ports=build_ports(pg_session_factory)`, and an invocation built
from a context whose `lifecycle_generation` is set (`0` matches
`Workspace.lifecycle_generation`'s server default). Set it on the context before
`snapshot_invocation(ctx)`; do not hand-build an `Invocation`. **Change how they CALL; do not
change what they ASSERT.** If an assertion has to move for a test to pass, that is a
behaviour change — stop and record it under PLAN DEFECTS.

- [ ] **Step 9: Restate F3d3a's fenced-set literal**

`tests/proposal_assist/test_f3d3a_relocation.py:170-186`
(`test_no_tool_module_gained_a_port_call`) asserts the fenced set equals exactly F3d2's
five pairs and that zero F3d3 rows are fenced. Both are now false. **This must be edited
in THIS commit** or the suite is red for a reason nothing in the commit fixes.

Add the new pair to the literal set and change the F3d3 count assertion to `== 1`. Update
the docstring: its claim was "F3d2's five and no more — F3d3a converts nothing", which was
true of F3d3a and is not true here. Do NOT weaken it into something derived — the point of
that test is that the set is written down.

- [ ] **Step 10: WRITE the error-path tests — they do not exist**

Measured: `tests/proposal_assist/tools/test_propose_edit.py`,
`test_propose_spec_edit.py` and `coherence/test_propose_provenance.py` are the only three
modules that call these wrappers, and **not one of them contains a stale, deleted, or
timeout case** (`grep -c 'Stale\|MutexTimeout\|DELETED_ERROR\|BUSY_ERROR'` returns 0 for
all three). So there is nothing for a mutation to turn red. A first draft of this task
asked for exactly that mutation; it would have "passed" by changing nothing.

The three `except` arms added in Step 4 are new tool-boundary behaviour and need their own
tests. Follow the house pattern in `tests/proposal_assist/test_f3d2_conversions.py`:
`_ExplodingPorts` (`:45`), `_ctx_and_invocation(...)`, `build_ports`, and the
`f3b_seeds.py` builders. Read that file first and mirror it — do not invent fixtures.

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
class _RaisingEffects:
    """An Effects whose propose_artifact_edit raises one chosen port error."""

    def __init__(self, exc):
        self._exc = exc

    def propose_artifact_edit(self, stamp, cmd):
        raise self._exc


class _Ports:
    def __init__(self, effects):
        self.effects = effects


# Verified against ports/errors.py:27-36 and :63. WorkspaceGenerationStale requires BOTH
# keyword arguments — `WorkspaceGenerationStale(wid)` raises TypeError at COLLECTION, so
# a plan that guesses here does not even produce a red test. CommitMutexTimeout is a
# plain RuntimeError subclass and takes no arguments.
_WID = uuid.uuid4()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WorkspaceGenerationStale(_WID, expected=_G, actual=_G + 1), DELETED_ERROR_MESSAGE),
        (CommitMutexTimeout(), BUSY_ERROR_MESSAGE),
        (EffectRejected("this workspace is no longer accepting proposed changes"),
         "this workspace is no longer accepting proposed changes"),
    ],
)
async def test_the_artifact_edit_answers_every_port_error_as_an_error_dict(exc, expected):
    """The three arms Step 4 added. ports/effects.py's Protocol docstring says a tool
    catching only the first two DOWNGRADES a transient busy into "internal error",
    because _safe_tool turns every unnamed exception into that — so CommitMutexTimeout
    is the arm most worth pinning, and the one no existing test covered.
    """
    ctx, invocation = _ctx_and_invocation(uuid.uuid4())
    out = await _propose_artifact_edit_impl(
        ctx,
        ports=_Ports(_RaisingEffects(exc)),
        invocation=invocation,
        target_artifact="draft",
        head="abc",
        sections={0: "sha"},
        section_index=0,
        new_text="x",
        not_read_err="unread",
    )
    assert out == {"error": expected}
```

`_ctx_and_invocation` and `_G` come from `tests/proposal_assist/test_f3d2_conversions.py`
(`:19`, `:28`) — import them or copy them; do not re-derive. That helper builds a
`ToolContext` with `repo_dir=None` and `session_factory=None`, which is FINE here because
`_propose_artifact_edit_impl` touches neither: it validates `head`/`sections` and calls
the port. It is NOT fine for `propose_citation_impl` — see Task 3 Step 7.

- [ ] **Step 11: Run everything**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-2b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-2b.txt
uv run pytest tests/fencing -q 2>&1 | tail -3
```
Expected: **3794 passed, 1 skipped** (3790 + this task's 4: the invocation-snapshot test
plus the three-arm error-path parametrisation) and `tests/fencing` **RED on
`test_the_inventory_matches_the_tree_exactly` only** — the JSON now disagrees with a fresh
scan, and it stays that way until Task 4 regenerates.

`test_each_slice_matches_its_declared_status` should still PASS: it reads the stored JSON,
which has not been regenerated. If it fails, the JSON was regenerated early — find out by
whom. If anything OUTSIDE `tests/fencing` is red, that is collateral this plan did not
predict: record it under PLAN DEFECTS with the failing test names.

- [ ] **Step 12: Prove the port is what refuses, not leftover tool logic**

Now that a stale case exists, the mutation has something to bite. Temporarily change the
`WorkspaceGenerationStale` arm to `raise`.

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k "answers_every_port_error" > /tmp/f3d3b-2m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-2m.txt
```
Expected: FAIL on the `WorkspaceGenerationStale` parametrisation only — the other two
still return their dicts. A mutation that reddens all three means the arms are not
independent and you have collapsed them.

Restore from a copy (GC-6), then **re-run the full suite and confirm it is green at the
Step 11 total before committing.** A mutation left half-restored is the most common way a
task commits a broken tree while its own report says green.

- [ ] **Step 13: Commit**

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
```

- [ ] **Step 3: Run them to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k "citation_propose or tombstone" > /tmp/f3d3b-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-3.txt
```
Expected: FAIL on `does not call the effect port`. Only ONE test is selected here — the
tombstone test does not exist yet.

The behavioural tombstone test is NOT written here. It calls the tool with `ports=` and
`invocation=`, which do not exist until Step 4 — writing it now produces a `TypeError`,
which is a red test that proves nothing about tombstones. It lands in Step 7 with the
other tests that need the converted signature.

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

- [ ] **Step 7: Write this tool's error-path tests too**

Same gap as Task 2 Step 10, same reason: `test_propose_citation.py` and
`test_canonical_consumers.py` contain no stale, deleted or timeout case, so there is
nothing for a mutation to redden. Add the three-arm parametrisation for
`propose_citation_impl`, mirroring the one Task 2 added — a `_RaisingEffects` whose
`propose_citation_insert` raises, and the same three expectations
(`DELETED_ERROR_MESSAGE`, `BUSY_ERROR_MESSAGE`, the `EffectRejected` string passed
through).

`propose_citation_impl` reaches the port only after its reads succeed, so this test needs
a seeded workspace, Reference and ReferenceChunk plus a real `ctx.agent_view_*` snapshot.

**Do NOT use `_ctx_and_invocation` from the F3d2 module here.** It builds a `ToolContext`
with `repo_dir=None`, `session_factory=None` and no read snapshot
(`test_f3d2_conversions.py:28-40`), so `propose_citation_impl` returns
`"you haven't read the proposal yet — call read_proposal first"` and never reaches the
reference lookup, the tombstone branch, or the port. Confirmed by running it. It is fine
for `_propose_artifact_edit_impl`, which touches neither field.

Lift the arrangement from `tests/proposal_assist/tools/test_propose_citation.py`'s
happy-path test instead: it already builds a real repo, a `ToolContext` with a live
`session_factory` and `repo_dir`, `_seed_reference(...)` (`:51`), and the
`ctx.agent_view_head` / `agent_view_proposal` / `agent_view_sections` snapshot. Read that
file and reuse its fixtures; do not write a new arrangement.

Write the behavioural tombstone test here too, on that same arrangement:

```python
@pytest.mark.asyncio
async def test_a_tombstoned_reference_is_refused_and_writes_nothing(
    pg_session_factory, tmp_path
):
    """The check most likely to be lost, tested by BEHAVIOUR.

    ReferenceView.is_tombstoned exists so the tool can tell 'no such reference' from
    'tombstoned, cannot be cited' — two different messages to the agent. A conversion
    that reads only `is None` collapses them, and a source grep for the message would be
    satisfied by the COMMENT explaining it. This project has shipped a
    comment-satisfiable test twice; do not make it three.

    Seed a real tombstoned Reference, call the tool, assert the exact string AND that no
    ProposedChange row landed.
    """
    from call_manager.db.models import ProposedChange
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.citations import propose_citation_impl

    # The REAL context FIRST — it creates the workspace and returns its id. _build_ctx is
    # at test_propose_citation.py:81 and gives a real repo, a live session_factory and
    # Settings(). _ctx_and_invocation is NOT usable here (see the note above): its
    # ToolContext has repo_dir=None, session_factory=None and no read snapshot, so the
    # tool answers "call read_proposal first" and never reaches the tombstone branch.
    ctx, ws_id, repo = _build_ctx(pg_session_factory, tmp_path)

    # _seed_reference (test_propose_citation.py:51) is the house way to build a Reference
    # plus its chunks. Do NOT reach for f3b_seeds.py — it has no Reference builder
    # (verified: seed_workspace, bump_generation, seed_evaluator_job, seed_uncited_job,
    # seed_review_item, seed_running_turn, and nothing else).
    ref_id = _seed_reference(pg_session_factory, ws_id, chunks=[(0, "s", "quote text")])
    with pg_session_factory() as sess:
        sess.get(Reference, ref_id).tombstoned_at = datetime.now(UTC)
        sess.commit()

    await read_proposal_impl(ctx)          # populates ctx.agent_view_* — required
    # _build_ctx leaves lifecycle_generation unset, and Invocation.stamp() raises
    # UnfencedInvocation on None (generation 0 is REAL, so it tests for None not falsiness).
    ctx.lifecycle_generation = 0
    invocation = snapshot_invocation(ctx)

    out = await propose_citation_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        reference_id=str(ref_id),
        anchor="anchor",
        chunk_index=0,
        evidence_quote="q",
    )
    assert out == {"error": "reference is tombstoned and cannot be cited"}

    with pg_session_factory() as sess:
        assert sess.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 0
```

`_build_ctx`, `_seed_reference`, `read_proposal_impl`, `snapshot_invocation` and
`build_ports` all have to be imported into the new test module; `Reference`, `datetime`
and `UTC` too. Check each import resolves before running — `ruff check` catches the unused
ones but not the missing ones.

- [ ] **Step 8: Run everything**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-3b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-3b.txt
uv run pytest tests/fencing -q 2>&1 | tail -3
```
Expected: **3799 passed, 1 skipped** (3794 + this task's 5: the port-write test, the
behavioural tombstone test, and the three-arm error-path parametrisation),
`tests/fencing` still RED on
`test_the_inventory_matches_the_tree_exactly` only.

- [ ] **Step 9: Prove the port is what writes, and the tool still validates**

Two mutations, both restored from a copy afterwards (GC-6):

1. `WorkspaceGenerationStale` arm -> `raise`. Expected: FAIL on that parametrisation of
   the Step 8 test ONLY. If all three redden, the arms are not independent.
2. Make `ports.effects.propose_citation_insert` a no-op returning a fixed UUID. Expected:
   the tests that assert a row LANDED go red, the tests that assert an early
   `{"error": ...}` stay green. That separates "the port does the write" from "the tool
   still validates" — and it is the check that would catch a conversion which quietly
   stopped calling the port at all.

Restore both from a copy (GC-6), then **re-run the full suite and confirm it is green at
the Step 8 total before committing.**

- [ ] **Step 10: Commit**

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
green at **3801 passed, 1 skipped** (3799 + 2). Derive that yourself from the tests you
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
Expected exactly — every line literal, no placeholders:
```
inventory rows: 20
pins: 5 ['audit.py', 'citations.py', 'history.py', 'proposal.py', 'references.py']
F3d3: complete | unfenced F3d3 rows: 0
still pending: ['F3e', 'F4', 'F5']
F3e rows: ['_write_citekey_cache', 'fetch_reference_impl._do_fetch_and_ingest']
```
(The F3e symbol is the full qualname `fetch_reference_impl._do_fetch_and_ingest` — a
nested function is a different identity, which is why F3d2's plan had to learn it the hard
way. `sorted()` puts the underscore first.)

The two raw counts cannot be predicted here — they depend on exactly which imports the
conversions removed. Print them and RECORD them; do not treat them as an expectation:
```bash
uv run python -c "
from tests.fencing.import_gate import check_file, tool_key, tool_modules
for p in tool_modules():
    if tool_key(p) in ('citations.py','proposal.py'):
        print(f'  {tool_key(p)}: {len(check_file(p))} raw')
"
```
Both must be **strictly below** their before-counts (citations.py 15, proposal.py 14) and
both must be **non-zero** — a zero would mean an exemption retired, which GC-3 forbids
this slice from claiming.

The full suite here is the FINAL total: **3803 passed, 1 skipped** (3801 from Task 4 +
this task's 2). Revision 1 of this plan stopped at Task 4's number and never stated the
final one, which is how a plan ends up with two totals that disagree.

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

### Revision 2 (2026-08-21) — five more findings, all confirmed against the tree

An independent read of revision 1 found five defects. Each was reproduced before acting.

1. **P1 — the inventory move could not happen.** Revision 1 changed only
   `F3D_IDENTITIES` and expected regeneration to follow, but `merge()` deliberately
   carries the stored slice forward (`inventory.py:297-300`). The JSON row is now edited
   by hand and regeneration is required to be a NO-OP, which proves the mechanism rather
   than assuming it.
2. **P1 — two mutation proofs had nothing to bite.** Measured: none of the five test
   modules that call these four impls contains a stale, deleted or timeout case. The
   mutations would have "passed" by changing nothing. Tasks 2 and 3 now WRITE the
   three-arm error-path tests first, and the mutations reden one arm each.
3. **P2 — the reassignment contradicted the governing design.** §7 assigned all three
   rows to F3d3 and §7's F3e was "the ingest pipeline". Arguing *from* the spec's
   mechanism note while contradicting its allocation is not an argument. The design was
   amended in the same commit as this revision, and Task 1 Step 4 makes the implementer
   verify the amendment is on disk before proceeding.
4. **P2 — the tombstone ratchet was comment-satisfiable**, in a plan whose own
   self-review rejects that class. Now it seeds a tombstoned Reference, calls the tool,
   and asserts both the exact string and that no row landed.
5. **P3 — the "Expected exactly" block was not executable** (a placeholder symbol, two
   `<below N>` counts) and the final total was never stated. Fixed; the chain is now
   3788 → 3790 → 3794 → 3799 → 3801 → **3803**.

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
