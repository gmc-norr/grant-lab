# Fence F3e1a2 — the containing statement is the order

**Status:** design, revision 1
**Slice:** `F3e1a2` — between `F3e1a` (merged, `2086ac19`) and `F3e1b` (pending)
**Amends:** `2026-08-21-fence-f3e1-git-mechanism-design.md` §5.1
**Touches:** `tests/fencing/git_fence.py` and its tests. No `src/` change. No row moves.

---

## 1. Why this exists

F3e1a shipped a deliberately conservative checker. Its dry run then found the thing
that conservatism costs:

> The conservative checker credits only a bare top-level sink expression, but all
> nine real sinks capture or nest their sink, so spec §3.6's "F3e1b → 4 rows, all
> `true`, complete" is unreachable as the checker stands.

That is correct, and it is not a small gap. Measured on the tree at `2086ac19`:

| Site | Slice | Top-level statement containing the sink |
|---|---|---|
| `checkpoint_artifact` | F3e1b | `ast.Assign` |
| `save_section` | F3e1b | `ast.Assign` |
| `insert_citation_for_user` | F3e1b | `ast.Assign` |
| `insert_from_annotation` | F3e2 | `ast.Assign` |
| `_insert_citation_locked` | F3e1b | `ast.Try` |
| `build_bibliography` | F3e2 | `ast.If` |
| `post_init_spec._git` | F3e2 | `ast.With` |
| `run_review_pass` | F4 | `ast.AsyncWith` |
| `Phase0Worker._init_repo` | F5 | nested (`_run_git`) |

**Not one of the nine is a bare top-level expression statement.** The checker can
credit exactly zero of them, so no slice that owns a `git_commit` row can ever reach
`complete` under §3.6's bidirectional rule.

**The narrower fix was considered and rejected.** Crediting an `ast.Assign` whose
value is the sink covers 4 of the 9 and **3 of F3e1b's 4** — `_insert_citation_locked`
sits in a `try:` and would still need a pin and a documented exception to §3.6. That
is the same hole, one size smaller, and F3e2 would hit it again immediately.

## 2. The rule

Replace the sink-position question. Today the checker asks:

> Is the **sink** a bare top-level expression statement?

Ask instead:

> Is the **acquisition** a standalone top-level statement at index *j*, and is every
> sink inside a top-level statement at index *i* > *j*?

Nothing else changes:

- the acquisition must still be a standalone `ast.Expr` **directly in the function
  body** — an acquisition inside an `if`, a `try` or a `with` still counts for nothing;
- `acquire_commit_mutex_live` anywhere in the function still disqualifies it;
- nested `def`s are still separate qualnames, analysed on their own pass;
- the comparison is still `stmt_index <= first_acq → unfenced`, unchanged.

### 2.1 Why it is sound

A function body's statements execute in order. To reach any code inside statement
*i*, control passed through statement *j* < *i*. Python has no goto and no way to
skip a sibling statement. If statement *j* raises, statement *i* is never reached.
If the sink never runs, there was no effect to fence.

The nesting inside statement *i* — `try`, `if`, `with`, `for`, `while`, a
comprehension, an `await` — is irrelevant to that argument. All of it happens after
*j* completes.

**This is the failure mode the checker has already had, and it is avoided here by
construction.** Revision 2 of the plan compared *AST preorder* to execution order and
was wrong: preorder visits an outer call before its arguments, while Python evaluates
the arguments first. **Sibling statement index is not preorder.** It is the one
ordering relation in a Python function body that is literally execution order.

### 2.2 What stays conservative

The acquisition side is untouched, and it is what carries the risk. An acquisition
inside a branch may not run; an acquisition passed as a call argument runs *after*
its sibling arguments. Both still measure unfenced. `_insert_citation_locked` and
`post_lock_spec` today write the acquisition **inside** a `try:` — that shape stays
unfenced, and F3e1b's conversion must hoist the acquisition above the `try:`, which
is where it belongs anyway.

## 3. Why this is safe to land on its own

**`acquire_commit_mutex_at_generation` has zero production callers** (verified at
`2086ac19`: the only occurrence in `src/` is its own definition in `fencing.py:115`).

Therefore every one of the nine rows measures `fenced: false` **regardless of what
this change does to the sink-position rule** — a function with no acquisition at all
fails the positive rule before position is ever consulted.

So the change is **provably inert on today's tree**, and that inertness is the slice's
central gate: the inventory must be byte-identical after the change.

That is why this is its own slice rather than F3e1b's first task. This predicate has
been wrong four times — vacuous, order-blind, wrong-events, and the branch-nested-`def`
attribution bug F3e1a's own review caught. Every one of those errors was found inside a
larger change. An inert, standalone slice gives the adversarial pass exactly one target
and a gate that says *you moved nothing*.

## 4. What changes in the code

One function: `sink_calls` in `tests/fencing/git_fence.py`.

Today a sink that is not a bare top-level expression gets `stmt_index = None`
("position unknown"), and `unprotected_sinks` reports it as order-unprovable. After
this slice it gets **the index of the top-level body statement that contains it**.

The ordering check in `unprotected_sinks` reads `stmt_index` and needs no edit at all.

Docstrings stating the old rule must be corrected in the same commit —
`git_fence.py` module docstring, the `stmt_index` semantics note, `sink_calls`, and
`unprotected_sinks`.

### 4.1 The `None` branch becomes unreachable — decide it, do not leave it

Every sink discovered by walking `func.body` is inside some body statement, so after
this change `stmt_index` is always an `int`. The `None` path becomes dead code in the
safety-critical predicate — which is exactly the defect F3e1a's Task 7 review raised
as Critical (two guard functions that no test called).

**Decision: keep `int | None` and the fail-closed branch, and make it reachable.**
It is the correct default for any future caller that finds a sink outside a body
statement (a decorator expression, a default argument — neither is discovered today).
The plan must drive that branch with a synthetic entry and assert it reports
unfenced, so the fail-closed path is tested rather than assumed.

## 5. How it is proven

The widening direction is the dangerous one: **over-reporting** lets a slice claim a
conversion it did not make. Under-reporting costs a pin and a sentence. Every proof
below targets over-reporting.

### 5.1 Inertness (the slice gate)

`fence_inventory.json` is unchanged, all nine `git_commit` rows still `fenced: false`,
`SLICE_STATUS` unchanged apart from the new key, pins still 5, and `src/` untouched.

### 5.2 The existing 15 tests must all keep their current verdict

Traced by hand against the new rule; every one still passes, several for a **new
reason**. Two are worth naming because they look like they should flip and do not:

- `test_an_acquisition_nested_as_a_call_argument_is_not_fenced` — the sink is an
  argument of the acquisition, so both live in statement 0. The check is
  `stmt_index <= first_acq`, so `0 <= 0` still reports unfenced. It survives on the
  strict inequality, not on `None`.
- `test_an_acquisition_inside_try_is_not_fenced_conservatively` — the *acquisition* is
  inside the `try`, so the function has no top-level acquisition and fails before
  position is consulted.

Because no existing test flips, **no existing test pins the widened behaviour.** The
new tests below are the only thing that does.

### 5.3 New positive cases — each must be fenced

Acquisition as a standalone top-level statement at index 0, sink inside the top-level
statement at index 1: `ast.Assign`; `ast.Try` body; `ast.Try` `finally`; `ast.If` body;
`ast.If` `orelse`; `ast.With`; `ast.For`; `ast.While`; `ast.Return`; `async with` in an
`async def`; and a generator with a `yield` between the acquisition and the sink.

### 5.4 New negative cases — each must NOT be fenced

- sink inside a top-level statement at index *i* < *j*
- two sinks straddling the acquisition — one nested before it, one nested after
- acquisition inside the same top-level statement as the sink
- acquisition inside a branch, sink nested in a later statement
- `acquire_commit_mutex_live` present alongside a correct acquisition
- a sink in a nested `def` whose parent has the acquisition

### 5.5 The mutation harness must be extended

F3e1a's six mutations stay and must still bite. The widening needs its own, each
naming the single test that reddens:

1. change `stmt_index <= first_acq` to `<` — the same-statement argument case must flip
2. attribute a nested sink to statement index 0 instead of its container — the
   sink-before-acquisition-inside-a-block case must flip
3. drop the nested-scope skip — the nested-`def` case must flip (F3e1a's mutation 6,
   re-verified against the new code)

### 5.6 Real-tree probe

`sink_calls` over `src/` still returns exactly nine holders, and
`fenced_qualnames` over `src/` still returns the empty set.

### 5.7 The design was prototyped before this spec was committed

The change is two lines, so it was applied to a scratch copy of `git_fence.py` at
`2086ac19` and measured rather than argued. The prototype was reverted; the tree is
unchanged. What it showed:

- **`153 passed`** — the whole of `tests/fencing/`, including all 15 checker tests and
  the inventory measurement gate. §5.2's hand-trace is confirmed by running: **no
  existing test flips.**
- **All ten §5.3 positive shapes measure fenced** — `Assign`, `try` body, `try`
  `finally`, `if` body, `if` `orelse`, `with`, `for`, `while`, `return`, and a
  generator with a `yield` between. An `async with` inside an `async def` too.
- **All five §5.4 negative shapes measure unfenced** — including the two that matter
  most: a sink nested in a statement *before* the acquisition, and two sinks
  straddling it.
- **Real tree: exactly nine holders, `fenced_qualnames` empty.** §3's inertness claim
  and §5.6's probe both hold.

This is evidence the design behaves as described, not that it is correct. The point
of §5.5's mutations is that a passing prototype and a load-bearing test are different
things — this predicate has passed its own tests while wrong four times.

## 6. What this does not do

- **Does not convert anything.** No `src/` change; `acquire_commit_mutex_at_generation`
  still has zero production callers when this lands.
- **Does not move a row or a pin.**
- **Does not relax the acquisition side.** An acquisition in a branch, a `try`, or a
  call argument still counts for nothing, and that is deliberate — §2.2.
- **Does not make the checker sound.** It remains a cheap ratchet over one function at
  a time. It knows nothing about transitive callers, and spec §5.2's runtime
  two-ordered-lock cases remain the real proof. `fenced: false` still does not mean
  broken.
- **Does not settle `ENCLOSING_ACQUISITIONS`.** That record says 14 while spec §13
  criterion 9 says seven. It is inert today and stays F3e1b's problem.

## 7. Slice status

`SLICE_STATUS` gains `"F3e1a2": "complete"` in the landing commit, following
`F3b`/`F3c`/`F3d1`/`F3e1a`: a key that owns no inventory row. Note that
`test_each_slice_matches_its_declared_status` iterates **rows**, so a zero-row key
asserts nothing — it exists so a future row naming this slice cannot fall through
with an unknown status.

## 8. After this lands

F3e1b becomes plannable: its four sites can reach `fenced: true` once converted,
provided each conversion places `acquire_commit_mutex_at_generation` as a standalone
statement in the function body ahead of the statement holding the sink.

`_insert_citation_locked` is the one that changes shape: the acquisition must move
above its `try:`. The other three take the acquisition directly above their
assignment.
