# Fence F3d3b — the propose sites and the citekey cache

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert all three F3d3 inventory rows onto the ports so `SLICE_STATUS["F3d3"]` can flip to `complete`.

**Architecture:** Two tool impls (`propose_citation_impl`, `_propose_artifact_edit_impl`) stop opening sessions and call `Effects` operations F3d1 already built. One domain helper (`_write_citekey_cache`) has its owner-transaction branch converted to a new narrow `Effects` operation, leaving its SAVEPOINT branch — which never commits — untouched. One ~12-line collab-aware read moves out of `tools/` into `citations/core.py`, the home F3d3a created.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest. No migrations. No frontend. No new dependency.

---

## Global Constraints

- **GC-1 — behaviour parity is the bar.** Every error string, every returned dict shape,
  and the order in which checks run must survive. The ONE sanctioned change is the
  deletion/stale race: a converted site now answers `DELETED_ERROR_MESSAGE` or
  `BUSY_ERROR_MESSAGE` where it used to answer its own target error. That is the fence
  working. Anything else is a regression — report it, do not absorb it.
- **GC-2 — a tool may hold no Session.** After this slice, neither converted impl calls
  `ctx.session_factory()`. Passing a session into a helper is the same violation wearing
  a different hat.
- **GC-3 — `citations/core.py` is OUTSIDE the import gate** and may hold a Session, name
  `repo_dir`, and import models. That is what it is for. Do not try to make it clean.
- **GC-4 — no re-export shims.** A moved name must not be reachable from
  `tools/citations.py`. `tests/proposal_assist/test_f3d3a_relocation.py` enforces this
  via `DELIBERATELY_IMPORTED_BACK`; adding a name to that set is a reviewed design change,
  not a way to make a test pass.
- **GC-5 — the slice-status gate is BIDIRECTIONAL.** `test_each_slice_matches_its_declared_status`
  requires a `pending` slice to have ALL rows `fenced: false` and a `complete` slice ALL
  `true`. **There is no legal partial state.** So `tests/fencing` is RED from the first
  conversion until Task 7 flips the status. Every task between them must expect that and
  say so. This is the single most likely place for this plan to be wrong about an
  expectation — F3d2 discovered it the hard way.
- **GC-6 — a mutation must fail the NAMED test for the NAMED reason.** A test that goes
  red because collection aborted proves nothing. Read the failure text.
- **GC-7 — GC-10 from the F3d3a plan still applies.** `git checkout <path>` destroys
  uncommitted work in that file and cannot restore an untracked file. Commit before
  mutating, or restore from a copy.
- **Test commands:** full suite `uv run pytest -n 16 --dist loadfile -q`; never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Lint gate is `uv run ruff check .`
  — `ruff format` is NOT a gate. Baseline on master `e7cd8f2f` is **3788 passed, 1
  skipped**. The `1 skipped` is a real-SDK test gated on `ANTHROPIC_API_KEY`; `0 skipped`
  means a live API call ran.

---

## What the survey measured (2026-08-21, against `e7cd8f2f`)

Everything below was produced by running a command, not by reading a summary. Re-derive
anything you intend to rely on.

**The three F3d3 rows, and why they are not alike:**

| row | path | category | what blocks `fenced` |
|---|---|---|---|
| `propose_citation_impl` | `tools/citations.py` | semantic | `session.commit()` at `:281` |
| `_propose_artifact_edit_impl` | `tools/proposal.py` | semantic | `session.commit()` at `:183` |
| `_write_citekey_cache` | `citations/core.py` | `caller_fenced` | `session.commit()` in the `owns_txn=True` branch ONLY |

**`begin_nested()` is NOT a raw commit, and this slice leans on that.** `_RAW_COMMIT_ATTRS`
is `{"commit", "begin"}`; `session.begin_nested()` has attribute `begin_nested` and does
not match. Verified:

```
begin_nested only  -> sites: NONE
commit only        -> sites: {'f'}
port+begin_nested  -> fenced: {(<path>, 'f')}
```

That is correct, not a loophole: `begin()` as a context manager commits on exit, while
`begin_nested()` opens a SAVEPOINT inside a transaction someone else will commit. Task 1
pins the distinction so it is a decision rather than an accident.

**`owns_txn` splits three ways, and only one caller commits:**

| caller | `owns_txn` | commits? |
|---|---|---|
| `insert_citation_impl` → `_insert_citation_locked` (`core.py:319`) | `True` (default) | yes — the branch to convert |
| `accepting.py:207` → `_insert_citation_locked` | `False` (explicit, `:219`) | no, caller commits |
| `collab/kernel.py:177` | `False` | no |
| `collab/kernel.py:552` | `False` | no |

**Only ~12 lines need to leave `tools/`.** Of the 109-line session block in
`propose_citation_impl`, steps 4, 5 and 6 are pure string work over `chunk.text`,
`proposal_text` and `ctx.agent_view_*`. The single session-and-`repo_dir`-dependent piece
is the collab-aware live-draft read at `tools/citations.py:195-207`.

**The ports are already built.** F3d1 shipped `Effects.propose_artifact_edit`,
`Effects.propose_citation_insert`, their DTOs, `Queries.get_reference` and
`Queries.get_reference_chunk`. `ReferenceView` even carries `is_tombstoned`, which is
exactly the check at `citations.py:177`. No Queries method is added by this slice, so
`test_ports_dto_contract.py`'s `CASES` is untouched.

**A new `Effects` operation carries exactly two obligations**, both set-equality gates
that go red the moment the Protocol grows:
- an `EFFECT_MECHANISMS` entry (`ports/effects.py:120`), pinned by
  `test_every_effect_declares_its_mechanism`;
- an `EFFECT_NAMES` lock case (`tests/proposal_assist/test_f3b_lock_cases.py:84`), pinned
  by the set-equality assert at `:97`.

**Caller surface is small.** `propose_citation_impl` is imported by `tools/registry.py`
(×2) and two test modules; `_propose_artifact_edit_impl` only by `tools/proposal.py`
itself. Nothing like F3d2's 36 callers.

---

## Deliberately NOT done here

- **No exemption retires.** `citations.py` and `proposal.py` both still name `repo_dir`,
  which is a filesystem locator that leaves with the capability holders in **F3f**. Pins
  stay at 5. Expect a shrink, not a retirement, and do not report otherwise.
- **The accept-time git insert is not converted.** `_insert_citation_locked` writes
  `proposal.md` and `citations.json` in one git commit under the mutex. Spec §3.5 says
  such an effect needs an operation owning the entire sequence; that is not this slice.
  Only the DB cache write at the end of it moves.
- **The SAVEPOINT branch of `_write_citekey_cache` stays.** Its three callers are inside
  transactions they commit themselves. Converting it would mean a port operation that
  joins a caller's transaction, which is the "hand back a session" shape the whole design
  refuses.
- **`collab/kernel.py` is not touched.** Both its call sites pass `owns_txn=False`.

---

## File Structure

| file | responsibility after this slice |
|---|---|
| `src/call_manager/proposal_assist/ports/effects.py` | gains `SetReferenceCitekey` DTO, one Protocol method, one `EFFECT_MECHANISMS` entry |
| `src/call_manager/proposal_assist/port_impl/effects.py` | gains the `set_reference_citekey` implementation |
| `src/call_manager/proposal_assist/citations/core.py` | gains `read_live_draft_text`; `_write_citekey_cache`'s owner branch calls the port |
| `src/call_manager/proposal_assist/tools/citations.py` | `propose_citation_impl` holds no Session |
| `src/call_manager/proposal_assist/tools/proposal.py` | `_propose_artifact_edit_impl` holds no Session |
| `tests/fencing/detectors.py` | comment only — pins why `begin_nested` is excluded |
| `tests/fencing/inventory.py` | `SLICE_STATUS["F3d3"] = "complete"`, shrunken pins |
| `tests/proposal_assist/test_f3d3b_conversion.py` | new — the slice's own claims |

---

### Task 1: `begin_nested` is deliberately not a raw commit

Three of this slice's tasks depend on the detector treating a SAVEPOINT as not-a-commit.
Today that is true by accident of spelling. Make it a decision, and make it fail loudly if
someone "fixes" it by pattern-matching a prefix.

**Files:**
- Modify: `tests/fencing/detectors.py` (comment only)
- Modify: `tests/fencing/test_detectors.py`

**Interfaces:**
- Consumes: `_RAW_COMMIT_ATTRS`, `find_sites` (existing).
- Produces: nothing importable.

- [ ] **Step 1: Write the failing test**

Append to `tests/fencing/test_detectors.py`:

```python
def test_a_savepoint_is_not_a_raw_commit():
    """F3d3b depends on this, so it is stated rather than left to spelling.

    `session.begin()` as a context manager COMMITS on exit, which is why F3d2 added it
    to _RAW_COMMIT_ATTRS. `session.begin_nested()` opens a SAVEPOINT inside a
    transaction someone else commits — it can never commit on its own, so a function
    whose only write is nested is fenced by whoever owns the transaction.

    The hazard this guards is a well-meant "fix" that matches a PREFIX
    (`attr.startswith("begin")`) and silently reclassifies every SAVEPOINT in the
    codebase as an unfenced commit site.
    """
    from tests.fencing.detectors import find_sites

    path = "src/call_manager/proposal_assist/citations/core.py"
    nested = (
        "def cache(session, ref):\n"
        "    with session.begin_nested():\n"
        "        ref.citekey = 'x'\n"
        "        session.flush()\n"
    )
    assert find_sites(nested, path) == set(), (
        "a SAVEPOINT was recorded as a commit site — _RAW_COMMIT_ATTRS is being "
        "matched by prefix rather than by exact attribute name"
    )


def test_a_begin_context_manager_IS_a_raw_commit():
    """The other side of the same line. Without this, narrowing _RAW_COMMIT_ATTRS to
    {"commit"} to make the test above pass would look like a clean fix."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    path = "src/call_manager/proposal_assist/citations/core.py"
    began = "def w(session):\n    with session.begin():\n        session.add(1)\n"
    assert (KIND_TOOL_COMMIT, "w") in {(s.kind, s.symbol) for s in find_sites(began, path)}
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/fencing/test_detectors.py -q -k "savepoint or begin_context" > /tmp/f3d3b-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-1.txt
```
Expected: `PYTEST_EXIT=0`, **2 passed**. Both pass on first run — they PIN existing
behaviour that nothing else states. Do not manufacture a failing state to satisfy TDD
ritual; say in your report that they passed immediately and why.

- [ ] **Step 3: Record the reason beside the set**

In `tests/fencing/detectors.py`, above `_RAW_COMMIT_ATTRS`:

```python
#: What counts as committing WITHOUT a .commit() node. `begin()` as a context manager
#: commits on exit (F3d2). `begin_nested()` deliberately does NOT appear: it opens a
#: SAVEPOINT inside a transaction its caller commits, so a function whose only write is
#: nested is fenced by whoever owns that transaction. Matched by EXACT attribute name,
#: never by prefix — see test_a_savepoint_is_not_a_raw_commit.
```

- [ ] **Step 4: Prove the pin is armed**

Temporarily change the discovery test in `find_sites` from
`node.func.attr in _RAW_COMMIT_ATTRS` to
`any(node.func.attr.startswith(a) for a in _RAW_COMMIT_ATTRS)`.

```bash
uv run pytest tests/fencing/test_detectors.py -q -k "savepoint" > /tmp/f3d3b-1m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-1m.txt
```
Expected: FAIL on `test_a_savepoint_is_not_a_raw_commit` with
`a SAVEPOINT was recorded as a commit site`. If it fails with anything else, the
mutation did not isolate the claim — record it under PLAN DEFECTS and design one that
does.

Restore from a copy (GC-7), then confirm the tree is clean:
```bash
git diff --exit-code tests/fencing/detectors.py && echo "unchanged from your Step 3 edit? NO — expect the comment. Verify by eye."
```

- [ ] **Step 5: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add tests/fencing/detectors.py tests/fencing/test_detectors.py
git commit -m "test(fencing): a SAVEPOINT is not a raw commit, by decision (F3d3b Task 1)"
```

---

### Task 2: The collab-aware live-draft read moves to `citations/core.py`

The one piece of `propose_citation_impl` that genuinely needs a Session and `repo_dir`.
Everything else in its session block is pure string work.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/core.py`
- Create: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, in `call_manager.proposal_assist.citations.core`:
  `read_live_draft_text(workspace_id: uuid.UUID, repo_dir: Path, *, session_factory) -> str | None`
  — returns the room-canonical draft text when collab is enabled for the draft, the raw
  `proposal.md` bytes when it is not, and `None` when the flag is off and the file does
  not exist. It owns its own session; callers pass none.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
"""F3d3b — the propose sites and the citekey cache go onto the ports.

Spec 2026-08-18 section 7: F3d converts propose_citation_impl and
_propose_artifact_edit_impl to effect operations. This slice also converts the
owner-transaction branch of _write_citekey_cache, which is the third F3d3 row and the
one that keeps the slice from closing.
"""

from __future__ import annotations

import inspect


def test_the_live_draft_read_owns_its_own_session():
    """GC-2. The whole point of moving this out of the tool: a tool that passes a
    Session into a helper has not given up ambient authority, it has laundered it.

    So the signature takes a session_factory, never a session, and the body opens
    exactly one.
    """
    from call_manager.proposal_assist.citations.core import read_live_draft_text

    params = inspect.signature(read_live_draft_text).parameters
    assert "session" not in params, (
        "read_live_draft_text takes a Session — that is the authority this slice "
        "removes, arriving as a parameter instead of an attribute"
    )
    assert "session_factory" in params
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q > /tmp/f3d3b-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-2.txt
```
Expected: `PYTEST_EXIT=1`, `ImportError: cannot import name 'read_live_draft_text'`.

- [ ] **Step 3: Add the function to `citations/core.py`**

The body is the block currently at `tools/citations.py:194-207`, with the session
sourced locally instead of inherited. Read that block before writing — the line numbers
come from a survey, and a survey is not the file.

```python
def read_live_draft_text(
    workspace_id: uuid.UUID,
    repo_dir: Path,
    *,
    session_factory: Any,
) -> str | None:
    """The draft text to probe an anchor against, at propose time.

    Collab-on: the ROOM-CANONICAL text (Slice 3a C1), so an anchor present in the
    durable log but not yet checkpointed to disk is not false-rejected. Flag-off: raw
    disk proposal.md, byte-for-byte as before (parity). Returns None when the flag is
    off and the file does not exist — the caller turns that into its own error string,
    because the message names PROPOSAL_FILENAME and belongs at the tool boundary.

    Owns its session (GC-2). This is a READ and reads are not fenced (spec section 8):
    a workspace deleted mid-turn simply reads as it stood.
    """
    with session_factory() as session:
        if is_collab_enabled(session, workspace_id=workspace_id, artifact="draft"):
            return canonical.canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
            )
    proposal_path = repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return None
    return proposal_path.read_text()
```

Note the session closes BEFORE the disk read. That is deliberate and it is a real
change from the original, which held the session open across the `read_text()`. Holding
a session across filesystem I/O is exactly what the tail-bounding work of Slice C3
exists to prevent. Record it in your report as an intentional, narrow improvement — it
cannot change what is returned.

Add the imports the body needs (`is_collab_enabled`, `canonical`, `PROPOSAL_FILENAME`,
`Path`, `uuid`) only if `core.py` does not already have them. Check first:
```bash
uv run python -c "
import ast
s=open('src/call_manager/proposal_assist/citations/core.py').read()
have={a.asname or a.name.split('.')[0] for n in ast.walk(ast.parse(s)) if isinstance(n,(ast.Import,ast.ImportFrom)) for a in n.names}
print('already imported:', sorted(have & {'is_collab_enabled','canonical','PROPOSAL_FILENAME','Path','uuid','Any'}))
"
```

- [ ] **Step 4: Run the test**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q > /tmp/f3d3b-2b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-2b.txt
uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -3
```
Expected: 1 passed for the new file; **3789 passed, 1 skipped** for the suite (3788 + 1).
Nothing calls the new function yet, so nothing else can move.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/citations/core.py tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "refactor(citations): the live-draft read owns its session, in the core (F3d3b Task 2)"
```

---

### Task 3: `_propose_artifact_edit_impl` goes onto the port

The easy conversion, done first so the harder one has a worked example in the branch.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: `Effects.propose_artifact_edit`, `ProposeArtifactEdit` (F3d1, existing).
- Produces: nothing new. The impl's return shape is unchanged:
  `{"proposed_change_id": str, "status": "pending"}` or `{"error": str}`.

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
the payload for `target_artifact == "draft"`. **So the conversion DELETES the payload
tweak at `tools/proposal.py:176-183`** — it is not dropped, it moved to the port in
F3d1. Confirm that by reading both before you delete anything; if the two disagree on a
key name, stop and report rather than reconciling silently.

- [ ] **Step 2: Write the failing test**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_the_artifact_edit_impl_holds_no_session():
    """GC-2, checked on the source rather than by mocking: a tool that still names
    session_factory has not been converted, whatever its tests say."""
    import inspect

    from call_manager.proposal_assist.tools import proposal as mod

    src = inspect.getsource(mod._propose_artifact_edit_impl)
    assert "session_factory" not in src, "still opens a session"
    assert "ports.effects.propose_artifact_edit" in src, "does not call the port"
```

- [ ] **Step 3: Run it to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k artifact_edit > /tmp/f3d3b-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-3.txt
```
Expected: FAIL on `still opens a session`.

- [ ] **Step 4: Convert**

Replace the body of `_propose_artifact_edit_impl` (`tools/proposal.py:132-185`). Keep
the docstring, the signature's keyword-only shape, and both `not_read_err` returns
exactly as they are:

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
                selection=ctx.turn_selection,
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

Two things to get right, both of which a careless conversion loses:

1. **`selection=ctx.turn_selection` is passed unconditionally.** The old code guarded it
   with `target_artifact == "draft"`; the port impl applies that same guard itself
   (`port_impl/effects.py`, the `cmd.target_artifact == "draft" and cmd.selection` test).
   Guarding again here is harmless but duplicated; NOT passing it at all silently drops
   selection anchors from every draft edit. Verify the port's guard before relying on it.
2. **The lazy `propose_prose_edit` import goes away**, and with it the comment about the
   `proposing <-> proposal` module cycle. `proposing.py` keeps the function — tests and
   `accepting.py` still use it — so this is an unused import removal, not a deletion.

Update the caller(s) of `_propose_artifact_edit_impl` inside `tools/proposal.py` to pass
`ports=` and `invocation=`. Find them:
```bash
grep -n "_propose_artifact_edit_impl(" src/call_manager/proposal_assist/tools/proposal.py
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q > /tmp/f3d3b-3b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-3b.txt
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-3c.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-3c.txt
uv run pytest tests/fencing -q 2>&1 | tail -3
```
Expected: the new file green (**3790, 1 skipped** overall — 3788 + 2 so far), **and
`tests/fencing` RED**. GC-5: `_propose_artifact_edit_impl` now measures `fenced: true`
while `SLICE_STATUS["F3d3"]` is still `pending`, so
`test_each_slice_matches_its_declared_status` fails naming exactly that row. **That is
the expected state and it stays red until Task 7.** Record the failing test names; do
not chase them, and do not flip the status early to get green — the flip is only honest
once all three rows are converted.

If the fencing failure names anything OTHER than a slice-status disagreement, stop and
diagnose: that is a different problem wearing the same colour.

- [ ] **Step 6: Prove the fence is real, not decorative**

Disable the tool's own compensation: temporarily change the `WorkspaceGenerationStale`
handler to `raise`.

```bash
uv run pytest tests/proposal_assist -q -k "propose_artifact or prose_edit" > /tmp/f3d3b-3m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3b-3m.txt
```
Expected: the deleted-workspace case now raises instead of returning an error dict,
proving the port — not leftover tool logic — is what refuses a stale generation. If
nothing changes, the tests do not cover the stale path and the fence is unproven: say so
under PLAN DEFECTS rather than moving on.

Restore from a copy (GC-7).

- [ ] **Step 7: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "refactor(proposal): the artifact-edit propose goes onto the effect port (F3d3b Task 3)"
```

Committing with `tests/fencing` red is deliberate here and GC-5 explains why. Say so in
your report.

---

### Task 4: `propose_citation_impl` goes onto the ports

The larger conversion. Its 109-line session block becomes: two query-port reads, one
call to Task 2's helper, three unchanged blocks of pure string work, and one effect call.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: `Queries.get_reference`, `Queries.get_reference_chunk`,
  `Effects.propose_citation_insert`, `ProposeCitationInsert` (all F3d1);
  `read_live_draft_text` (Task 2).
- Produces: nothing new. Return shape unchanged.

- [ ] **Step 1: Map every check to its new source**

Before editing, write down where each of the impl's checks gets its data. Run:

```bash
sed -n '129,282p' src/call_manager/proposal_assist/tools/citations.py
```

The mapping, verified against `e7cd8f2f`:

| check | was | becomes |
|---|---|---|
| stance/confidence | `_validate_stance_confidence` | unchanged — already imported back |
| uuid parse | inline | unchanged |
| read-time base (`agent_view_head` / `agent_view_proposal`) | `ctx` | unchanged |
| reference exists + workspace-scoped | `session.get(Reference, ...)` + `.workspace_id` check | `ports.queries.get_reference(ws, ref_uuid)` returns `None` for both |
| reference tombstoned | `ref.tombstoned_at is not None` | `view.is_tombstoned` |
| chunk exists | `session.query(ReferenceChunk)...one_or_none()` | `ports.queries.get_reference_chunk(ws, ref_uuid, chunk_index)` |
| live draft text | collab branch + disk branch | `read_live_draft_text(...)`, `None` -> the `does not exist` error |
| quote verify | `find_quote_offsets(chunk.text, ...)` | `find_quote_offsets(chunk_view.text, ...)` — `ReferenceChunkView` carries `text` |
| anchor uniqueness | `insert_marker(proposal_text, ...)` | unchanged, pure |
| section keying | `anchor_occurrences` / `split_sections` over `ctx.agent_view_*` | unchanged, pure |
| the write | `propose_citation_insert(session=...)` + `session.commit()` | `ports.effects.propose_citation_insert(stamp, ProposeCitationInsert(...))` |

**`get_reference` returning `None` covers two old cases** — missing row AND foreign
workspace — because the query is workspace-scoped. Both answered
`{"error": "reference not found"}` before, so parity holds. Confirm the port really is
workspace-scoped by reading `port_impl/queries.py` before relying on it.

- [ ] **Step 2: Write the failing tests**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_the_citation_propose_impl_holds_no_session():
    """GC-2 for the second converted site."""
    import inspect

    from call_manager.proposal_assist.tools import citations as mod

    src = inspect.getsource(mod.propose_citation_impl)
    assert "session_factory" not in src, "still opens a session"
    assert "ports.effects.propose_citation_insert" in src, "does not call the effect port"
    assert "ports.queries.get_reference" in src, "does not read through the query port"


def test_the_tombstone_check_survived_the_conversion():
    """The check most likely to be lost. ReferenceView.is_tombstoned exists precisely
    so the tool can tell 'no such reference' from 'tombstoned, cannot be cited' — two
    different messages to the agent. A conversion that reads only `is None` collapses
    them and the agent is told the wrong thing."""
    import inspect

    from call_manager.proposal_assist.tools import citations as mod

    src = inspect.getsource(mod.propose_citation_impl)
    assert "is_tombstoned" in src
    assert "reference is tombstoned and cannot be cited" in src
```

- [ ] **Step 3: Run them to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k "citation_propose or tombstone" > /tmp/f3d3b-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-4.txt
```
Expected: FAIL on `still opens a session`. The tombstone test passes already (the string
is in the current source) — note that, and keep it: it is a ratchet against the
conversion, not a red-to-green step.

- [ ] **Step 4: Convert**

Rewrite `propose_citation_impl`'s body per the Step 1 table. The signature gains
`ports` and `invocation` keyword-only parameters, matching `submit_review_impl`
(`tools/review_submit.py`) which is the house form for a converted tool.

Keep **every error string byte-for-byte** and keep the checks **in their current order**
— an agent that gets "anchor not found" where it used to get "chunk not found" is a
behaviour change (GC-1). The four errors from the port are the house set:

```python
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
```

`ports/effects.py`'s Protocol docstring is explicit that a tool catching only the first
two DOWNGRADES a transient busy into "internal error", because `_safe_tool` turns every
unnamed exception into that. Catch all three.

Then delete the now-unused imports from `tools/citations.py`. Find them rather than
guessing:
```bash
uv run ruff check src/call_manager/proposal_assist/tools/citations.py --select F401
```

- [ ] **Step 5: Update the two test modules that import it**

```bash
grep -rn "propose_citation_impl" tests/ --include=*.py
```
Expected: `tests/proposal_assist/tools/test_propose_citation.py` and
`tests/proposal_assist/collab/test_canonical_consumers.py`. Both call the impl directly
and must now pass `ports=` and `invocation=`. **Change how they CALL it; do not change
what they ASSERT.** If an assertion has to move for the test to pass, that is a
behaviour change — stop and report it under PLAN DEFECTS.

`test_canonical_consumers.py` is the one to watch: it exists to prove the collab-on
branch reads room-canonical text. That branch now lives in `read_live_draft_text`, so
confirm the test still reaches it rather than passing vacuously.

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q > /tmp/f3d3b-4b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-4b.txt
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-4c.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-4c.txt
```
Expected: **3792 passed, 1 skipped** (3788 + 4 new tests). `tests/fencing` still RED per
GC-5, now naming two rows instead of one.

- [ ] **Step 7: Prove the fence, per site**

Same shape as Task 3 Step 6: disable the `WorkspaceGenerationStale` handler and confirm
the deleted-workspace case raises. Restore from a copy.

- [ ] **Step 8: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/tools/citations.py tests/proposal_assist/
git commit -m "refactor(citations): the citation propose goes onto the ports (F3d3b Task 4)"
```

---

### Task 5: A narrow effect operation for the citekey cache

The third row. Only the `owns_txn=True` branch converts; the SAVEPOINT branch is already
fenced by whoever owns its transaction, and Task 1 pinned why that is legitimate.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/effects.py`
- Modify: `src/call_manager/proposal_assist/port_impl/effects.py`
- Modify: `tests/proposal_assist/test_f3b_lock_cases.py`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: `FenceStamp`, `acquire_shared_lease`, `live_at_generation` (existing).
- Produces, in `ports.effects`:
  - `SetReferenceCitekey(reference_id: uuid.UUID, citekey: str)` — a frozen slots dataclass.
  - `Effects.set_reference_citekey(self, stamp: FenceStamp, cmd: SetReferenceCitekey) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_the_citekey_effect_declares_its_mechanism():
    """Spec 3.5. A named port does not make its internal fence atomic, so every
    operation states the ONE mechanism it uses and never upgrades between them. The
    set-equality gate in test_f3b_lock_cases catches a missing entry, but only after
    someone adds the method — this says what the entry must BE."""
    from call_manager.proposal_assist.ports.effects import EFFECT_MECHANISMS

    assert EFFECT_MECHANISMS["set_reference_citekey"] == (
        "shared lease + live_at_generation in the UPDATE"
    )


def test_only_the_owner_branch_of_the_citekey_cache_left():
    """The claim that lets this slice stay small. The SAVEPOINT branch serves three
    callers (accepting.py and collab/kernel.py twice) that are inside transactions they
    commit themselves; a port operation cannot join those. So it stays, and it is
    fenced by its caller — which is exactly what Task 1 pinned as legitimate."""
    import inspect

    from call_manager.proposal_assist.citations import core

    src = inspect.getsource(core._write_citekey_cache)
    assert "begin_nested" in src, (
        "the SAVEPOINT branch was removed — its three callers are inside transactions "
        "they commit themselves and cannot use a port that opens its own"
    )
    assert "session.commit()" not in src, "the owner branch still commits directly"
    assert "set_reference_citekey" in src, "the owner branch does not call the port"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py -q -k "citekey" > /tmp/f3d3b-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-5.txt
```
Expected: `KeyError: 'set_reference_citekey'` on the first, and
`the owner branch still commits directly` on the second.

- [ ] **Step 3: Add the DTO, the Protocol method and the mechanism entry**

In `ports/effects.py`:

```python
@dataclass(frozen=True, slots=True)
class SetReferenceCitekey:
    """The DB mirror of a citekey the git ledger already holds authoritatively.

    Best-effort by contract: the ledger is canonical, and reconciliation backfills a
    failed cache write. That is WHY it can be a separate transaction from the git
    commit that preceded it, and why it does not belong inside the accept-time
    operation that owns the git sequence.
    """

    reference_id: uuid.UUID
    citekey: str
```

Add to the `Effects` Protocol, in the same order as the DTO list:

```python
    def set_reference_citekey(
        self, stamp: FenceStamp, cmd: SetReferenceCitekey
    ) -> None: ...
```

And to `EFFECT_MECHANISMS`:

```python
        "set_reference_citekey": "shared lease + live_at_generation in the UPDATE",
```

- [ ] **Step 4: Implement it**

In `port_impl/effects.py`. Model it on `submit_review` — an UPDATE with the generation
predicate — not on `_insert_proposed_change`, which is an INSERT and has no target row.

```python
    def set_reference_citekey(self, stamp: FenceStamp, cmd: SetReferenceCitekey) -> None:
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(Reference)
                .where(
                    Reference.id == cmd.reference_id,
                    Reference.workspace_id == stamp.workspace_id,
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(citekey=cmd.citekey)
                .returning(Reference.id)
            ).first()
            if landed is None:
                _explain_refusal(session, stamp, checks=(), fallback=_CITEKEY_TARGET)
```

`Reference` is **not** currently imported in `port_impl/effects.py` — the block at
`:41-48` holds `EvaluatorJob, ProposedChange, ReviewRun, ReviewRunItem, UncitedClaimsJob,
Workspace` and nothing else. Add it, alphabetically. `update` IS already imported
(`:37`), so do not add it again. `_explain_refusal`'s signature is
`(session, stamp, *, checks, fallback)` — verified, and the call above matches it.

Add `_CITEKEY_TARGET` beside the other fallback strings (`_SCORECARD_TARGET` `:64`,
`_CLAIMS_TARGET` `:69`, `_PROPOSE_TARGET` `:70`). Read what the existing ones say
and match their voice; the agent never sees this one (the caller swallows it), but a log
reader does.

- [ ] **Step 5: Add the lock case**

`tests/proposal_assist/test_f3b_lock_cases.py:97` asserts
`set(EFFECT_NAMES) == set(EFFECT_MECHANISMS)`, so Step 3 has already turned that test
red. Add `"set_reference_citekey"` to the `EFFECT_NAMES` list (it holds the five existing
effect names around `:80-85`) and a driving branch beside `propose_citation_insert`'s
(around `:229`). Both line numbers come from a survey — locate them by name.

Read the neighbouring branch and mirror its shape. Do not invent a fixture: the real
builders live in `tests/proposal_assist/f3b_seeds.py` (`seed_workspace`,
`bump_generation` — which does NOT commit — `seed_evaluator_job`, `seed_uncited_job`,
`seed_review_item`, `seed_running_turn`). Check what exists before writing:
```bash
grep -n "^def " tests/proposal_assist/f3b_seeds.py
```

- [ ] **Step 6: Convert the owner branch**

In `citations/core.py`, `_write_citekey_cache` keeps its signature and its
never-propagate contract. Only the `owns_txn=True` arm changes:

```python
    if ref.citekey == citekey:
        return
    try:
        if owns_txn:
            ports.effects.set_reference_citekey(
                invocation.stamp(),
                SetReferenceCitekey(reference_id=ref.id, citekey=citekey),
            )
        else:
            with session.begin_nested():
                ref.citekey = citekey
                session.flush()
    except Exception:  # noqa: BLE001 — cache write must never fail the caller
        ...
```

**Where do `ports` and `invocation` come from?** `_write_citekey_cache` is domain code,
not a tool, so it cannot take them off a `ToolContext`. Thread them as explicit
keyword-only parameters from `_insert_citation_locked`, which already receives `ctx`.
Only the `owns_txn=True` path needs them, so they are optional with a `None` default,
and the owner branch asserts they are present. Read `_insert_citation_locked`'s signature
before choosing names.

**The `session.rollback()` in the owner branch's except arm goes away** with the commit
it was undoing — the port owns its own transaction and rolls itself back. Removing it is
required, not optional: calling `rollback()` on the caller's session after a port failure
would discard the caller's work. Verify no other statement in that arm depends on it.

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_f3d3b_conversion.py tests/proposal_assist/test_f3b_lock_cases.py -q > /tmp/f3d3b-5b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-5b.txt
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-5c.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-5c.txt
```
Expected: **3794 passed, 1 skipped** (3788 + 6), plus whatever the lock case adds — derive
it from what you actually wrote and report your arithmetic. `tests/fencing` still RED,
now naming all three rows.

- [ ] **Step 8: Prove the best-effort contract survived**

The single most valuable mutation in this slice. Make the port raise, and confirm the
caller is unaffected:

```bash
uv run pytest tests/proposal_assist -q -k "citekey_cache or insert_citation" > /tmp/f3d3b-5m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3b-5m.txt
```
with a temporary `raise RuntimeError("boom")` at the top of `set_reference_citekey`.
Expected: **still green**, with a logged warning — the whole point of
`_write_citekey_cache` is that a failure never propagates. If anything goes red, the
conversion broke the contract and the try/except is no longer covering the port call.

Restore from a copy (GC-7).

- [ ] **Step 9: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/ports/effects.py src/call_manager/proposal_assist/port_impl/effects.py src/call_manager/proposal_assist/citations/core.py tests/proposal_assist/
git commit -m "feat(ports): a narrow effect for the citekey cache, and the owner branch uses it (F3d3b Task 5)"
```

---

### Task 6: Wire `ports` and `invocation` into both converted tools

The registry builds every tool. Two impls changed signature in Tasks 3 and 4; this is
the task that makes them callable in production rather than only from tests.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py`
- Modify: `src/call_manager/proposal_assist/tools/proposal.py` (its internal callers)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing new.

- [ ] **Step 1: Find every construction site**

```bash
grep -n "propose_citation_impl\|_propose_artifact_edit_impl" src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/tools/proposal.py
```
Expected: `registry.py:685` and `:760` for the citation tool, plus the internal callers
in `proposal.py`. Read each before editing — the survey found two registry sites, and a
survey is not the file.

- [ ] **Step 2: Mirror the house form**

`submit_review_impl` is already wired this way. Read how the registry passes `ports` and
`invocation` to it and copy that exactly:
```bash
grep -n -B3 -A8 "submit_review_impl" src/call_manager/proposal_assist/tools/registry.py
```

Do not invent a new wiring shape. If the two registry sites for `propose_citation_impl`
differ from each other, that difference is meaningful — read why before collapsing it.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-6.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-6.txt
```
Expected: unchanged from Task 5. `tests/fencing` still RED per GC-5.

- [ ] **Step 4: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/tools/proposal.py
git commit -m "refactor(tools): the registry hands ports and invocation to both propose tools (F3d3b Task 6)"
```

---

### Task 7: F3d3 closes

All three rows are converted. This is the commit where the inventory agrees.

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/fence_inventory.json`
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Regenerate and read the diff before flipping anything**

```bash
uv run python -m tests.fencing.regenerate_inventory
git diff tests/fencing/fence_inventory.json
```
Expected: exactly three `fenced` fields flip `false` -> `true`, and nothing else moves.
Row count stays **20**. `RENAMES` must already be `{}` — this slice renames nothing.

**If fewer than three flipped, do not flip the status.** Find out which row still
measures unfenced and why. The likeliest cause is a raw commit left behind in the
converted qualname; `find_fenced_tool_commits` requires port call AND no raw commit.

- [ ] **Step 2: Flip the status**

In `tests/fencing/inventory.py`:

```python
    "F3d3": "complete",
```

- [ ] **Step 3: Shrink both exemptions**

The conversions removed imports from both tool modules. Get the live sets:

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

Replace `TOOLS_NOT_YET_CONVERTED["citations.py"]` and `["proposal.py"]` with exactly
those sets. Before is **citations.py 15 raw** and **proposal.py 14 raw**; report both
after-counts.

Neither exemption RETIRES — both modules still name `repo_dir`, which is F3f's. If
either set comes back empty, `test_no_exemption_is_stale` will tell you: the entry must
then be DELETED, and that is a bigger claim than this slice makes. Stop and report.

- [ ] **Step 4: Write the closing tests**

Append to `tests/proposal_assist/test_f3d3b_conversion.py`:

```python
def test_all_three_f3d3_rows_are_fenced_and_the_slice_says_so():
    """The claim this slice exists to make. The slice-status gate is bidirectional, so
    this is checkable rather than assertable — but stating it here names WHICH rows,
    which the gate does not."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = {r["symbol"]: r for r in load_inventory() if r["slice"] == "F3d3"}
    assert set(rows) == {
        "propose_citation_impl",
        "_propose_artifact_edit_impl",
        "_write_citekey_cache",
    }
    assert all(r["fenced"] for r in rows.values())
    assert SLICE_STATUS["F3d3"] == "complete"


def test_the_exemption_list_still_has_five_keys():
    """The honest headline, again. Both citations.py and proposal.py are blocked on the
    repo_dir locator, which retires in F3f. This slice SHRINKS two pin sets and retires
    neither."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py",
        "citations.py",
        "history.py",
        "proposal.py",
        "references.py",
    }
```

- [ ] **Step 5: Run everything**

```bash
uv run pytest tests/fencing -q > /tmp/f3d3b-7.txt 2>&1; echo "FENCE_EXIT=$?"; tail -20 /tmp/f3d3b-7.txt
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3b-7b.txt 2>&1; echo "SUITE_EXIT=$?"; tail -5 /tmp/f3d3b-7b.txt
uv run ruff check .
```
Expected: **`FENCE_EXIT=0` — the first green fencing suite since Task 3** — and the full
suite green. Derive the total from the tests you actually added rather than matching a
number here; report your arithmetic beside the run. If they disagree, find which term is
wrong. Do not adjust either to match the other and do not delete a test to reach a number.

- [ ] **Step 6: Prove the status flip is load-bearing**

Set `SLICE_STATUS["F3d3"]` back to `"pending"`.

```bash
uv run pytest tests/fencing -q -k slice_matches > /tmp/f3d3b-7m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3b-7m.txt
```
Expected: FAIL on `test_each_slice_matches_its_declared_status`, naming all three rows as
`fenced=True but slice F3d3 is pending`. This is the same gate that was red from Task 3
to here, now failing from the other direction — which is what "bidirectional" means and
why the flip could not have happened earlier.

Restore from a copy (GC-7).

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/fence_inventory.json tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "test(fence): F3d3 closes — all three rows measure fenced (F3d3b Task 7)"
```

---

### Task 8: Confirm the slice claimed nothing it did not do

**Files:**
- Modify: `tests/proposal_assist/test_f3d3b_conversion.py`

- [ ] **Step 1: Write the closing checks**

```python
def test_the_savepoint_branch_is_still_reached_by_its_three_callers():
    """The claim that kept this slice small, checked against the tree rather than
    asserted. If a later slice converts these, this test tells them what they are
    changing."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[2]
    kernel = (repo / "src/call_manager/proposal_assist/collab/kernel.py").read_text()
    accepting = (repo / "src/call_manager/proposal_assist/proposals/accepting.py").read_text()

    assert kernel.count("_write_citekey_cache(") == 2
    assert "owns_txn=False" in accepting


def test_no_converted_tool_reaches_for_a_session():
    """GC-2 across the whole converted surface, not one impl at a time."""
    import inspect

    from call_manager.proposal_assist.tools import citations, proposal

    for fn in (citations.propose_citation_impl, proposal._propose_artifact_edit_impl):
        src = inspect.getsource(fn)
        assert "session_factory" not in src, f"{fn.__qualname__} still opens a session"
        assert "Session" not in src, f"{fn.__qualname__} still names a Session"
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
  citations.py: <below 15> raw
  proposal.py: <below 14> raw
```

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/test_f3d3b_conversion.py
git commit -m "test(fence): F3d3b converted three sites and claimed nothing more (F3d3b Task 8)"
```

---

## Self-review notes

| Requirement | Task |
|---|---|
| `propose_citation_impl` -> effect operation (spec §7) | 4 |
| `_propose_artifact_edit_impl` -> effect operation (spec §7) | 3 |
| `_write_citekey_cache` fenced so F3d3 can close | 5 |
| Session leaves both tools (GC-2) | 3, 4, 8 |
| The one session+repo_dir read leaves `tools/` | 2 |
| A new effect declares its mechanism | 5 Step 3 |
| A new effect has a lock case | 5 Step 5 |
| Slice status flips in the same commit the inventory agrees | 7 |
| Pins shrink, neither retires | 7 Step 3, 8 |

Four things a reviewer should push on, named here rather than discovered later:

1. **`tests/fencing` is RED from Task 3 to Task 7 and that is unavoidable.** GC-5's gate
   is bidirectional and admits no partial state, so a slice converting three rows is red
   from the first until the last. F3d2 discovered this during execution because its plan
   claimed green at every task. This plan claims red on purpose and Task 7 Step 6 proves
   the flip was load-bearing. If a reviewer wants fewer red commits the answer is to
   squash Tasks 3-7 at merge, not to reorder them.
2. **The slice leans on `begin_nested` not being a raw commit.** That is a real
   distinction, not a loophole — a SAVEPOINT cannot commit on its own — but the whole
   size of this slice depends on it, so Task 1 pins it in both directions before anything
   else moves. If a reviewer disagrees with the distinction, the consequence is that
   `_write_citekey_cache`'s SAVEPOINT branch also needs an operation, and that means an
   operation that joins a caller's transaction, which §5 refuses.
3. **Task 2 changes when the session closes.** The original held a session open across a
   filesystem `read_text()`; the moved version closes it first. It cannot change what is
   returned, but it IS a change, and GC-1 says report rather than absorb. It is called
   out in the task and should appear in the implementer's report.
4. **Every expected total here is DERIVED, not measured.** The baseline 3788 is real
   (`e7cd8f2f`); everything after it is baseline plus the tests each task adds. This plan
   has not been executed. The totals are the first thing a run should falsify, and the
   detailed per-task expectations in Tasks 3-6 are the most likely to be wrong.
