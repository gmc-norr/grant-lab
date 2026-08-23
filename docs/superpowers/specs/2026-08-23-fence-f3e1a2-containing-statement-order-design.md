# Fence F3e1a2 — the containing statement is the order

**Status:** design, revision 3
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
> sink that **runs with** its containing top-level statement at index *i* > *j*?

Nothing else changes:

- the acquisition must still be a standalone `ast.Expr` **directly in the function
  body** — an acquisition inside an `if`, a `try` or a `with` still counts for nothing;
- `acquire_commit_mutex_live` anywhere in the function still disqualifies it;
- nested `def`s are still separate qualnames, analysed on their own pass;
- the comparison is still `stmt_index <= first_acq → unfenced`, unchanged.

### 2.1 Why it is sound, and the exception that makes "runs with" load-bearing

A function body's statements execute in order. To reach any code inside statement
*i*, control passed through statement *j* < *i*. Python has no goto and no way to
skip a sibling statement. If statement *j* raises, statement *i* is never reached.
If the sink never runs, there was no effect to fence.

Most nesting inside statement *i* — `try`, `if`, `with`, `for`, `while`, an `await`,
a list/set/dict comprehension — is irrelevant to that argument. All of it runs when
statement *i* runs, which is after *j* completes.

**Some constructs do not, and the mutex makes that fatal.** The commit mutex is a
**transaction-scoped** advisory lock — `acquire_commit_mutex` states it plainly:
*"Released automatically at COMMIT or ROLLBACK — there is no explicit release"*
(`drafting/locking.py:195-203`). Primitive B adds *"Never hold this across context
assembly or an LLM call."* So any construct that hands control back to a caller
between the acquisition and the sink can lose the mutex before the sink runs.

Three constructs do that:

1. **`lambda` bodies** — run on call, and the lambda can escape the function.
2. **generator-expression bodies** — run on consumption, and the genexp can escape.
3. **generator functions** — a body containing an own-scope `yield` or `yield from`.

The third is the worst of the three and revision 2 got it wrong in the safe-looking
direction. A generator function's body **does not begin executing until the caller
calls `next()`** — verified: creating the generator runs none of it. So the
*acquisition itself* is deferred, and every `yield` thereafter returns control to a
caller that may `COMMIT` or `ROLLBACK` and drop the mutex before the sink resumes.
Revision 2 listed a generator with a `yield` between acquisition and sink as a
**positive**. It is a negative.

Revision 1 claimed all nesting runs with its statement. That was **false**, and false
in the dangerous direction. Measured on a prototype of revision 1's rule, all of these
measured `fenced: true`:

```python
deferred = lambda: write_and_commit_files(repo, {}, "m")   # escapes, runs later
gen = (write_and_commit_files(repo, {}, "m") for item in items)
return {"go": lambda: write_and_commit_files(repo, {}, "m")}
register((write_and_commit_files(repo, {}, "m") for i in items))
yield 1; sha = write_and_commit_files(repo, {}, "m")       # mutex may be gone
```

Each is an **over-report** — the direction that lets a slice claim a conversion it did
not make. So: a sink inside an `ast.Lambda` or `ast.GeneratorExp` has **no provable
position** (`stmt_index` is `None`, and the existing fail-closed branch reports it
unfenced — §4.1), and a function with an own-scope `yield` is **disqualified outright**,
like `acquire_commit_mutex_live` (§4.2).

**The genexp boundary is an intentional under-report, not an exact one.** Python
evaluates a generator expression's *outermost iterable* immediately — verified:
`(elem(x) for x in outer())` calls `outer()` at creation and `elem` only on
consumption. A sink in that one position is therefore eager and could safely be
credited. Treating the whole `GeneratorExp` as deferred marks it unfenced instead.
That is a deliberate simplification in the safe direction, consistent with the rest of
this checker: the shape does not occur in the tree, and distinguishing it would add
branch reasoning this design refuses to do.

**List, set and dict comprehensions are eager** in Python 3 — they have their own
scope but run at their statement — so they are credited, and that is correct.

**One failure mode is avoided here by construction.** Plan revision 2 compared *AST
preorder* to execution order and was wrong: preorder visits an outer call before its
arguments, while Python evaluates the arguments first. **Sibling statement index is
not preorder.** It is the one ordering relation in a Python function body that is
literally execution order — which is what makes the *statement-level* claim sound
even though the *expression-level* one was not.

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

Two places in `tests/fencing/git_fence.py`, both small.

**`sink_calls`.** Today a sink that is not a bare top-level expression gets
`stmt_index = None` ("position unknown"), and `unprotected_sinks` reports it as
order-unprovable. After this slice it gets **the index of the top-level body statement
that contains it** — unless it is deferred, which is the second place.

**The walk.** `_iter_own_nodes` (`git_fence.py:136`) currently yields nodes only. It
must also report whether the node lies inside an `ast.Lambda` or `ast.GeneratorExp`,
the flag being set on crossing either boundary and inherited by everything beneath.
Whether that is a new helper or an added parameter is the implementer's call.

**It has a second caller, and that one must not change.** `_collect_functions`
(`git_fence.py:176`) uses the same walk to find nested `def`/`class` boundaries inside
a control-flow statement. A `def` inside a `lambda` is not expressible, but a `def`
inside a genexp's body is not either — so the flag is irrelevant there and
`_collect_functions` must keep discovering exactly the qualnames it does today. The
plan pins this with the existing nested-`def` tests plus the nine-holder real-tree
probe, both of which run through `_collect_functions`.

**`has_live` is unaffected and must stay that way.** It uses `ast.walk(func)`
(`git_fence.py:329`), not this walker, so it already descends into lambdas — an
`acquire_commit_mutex_live` inside a lambda disqualifies the function today and must
continue to. The `deferred` flag must not be plumbed into it: for the disqualifier,
deferred execution is not an excuse.

The ordering check in `unprotected_sinks` reads `stmt_index` and needs no edit at all.

### 4.2 The generator disqualifier

A second, independent check in `unprotected_sinks`, sitting beside `has_live`: if the
function has an **own-scope** `ast.Yield` or `ast.YieldFrom`, it is unfenced, whatever
its acquisition and sink positions are. §2.1 is the reason — its whole body is
deferred, and every `yield` is a point where a caller can end the transaction.

This is a **disqualifier, not a position rule**, so it fires even when the `yield`
sits *after* the sink. That over-broadness is deliberate and cheap: a generator that
really does acquire, sink, then yield measures unfenced and costs a pin. Reasoning
about which yields precede which sinks is exactly the branch analysis this design
refuses to do.

**Own-scope means the existing walk, and there is a trap in it.** `_iter_own_nodes`
guards only its *children*: handed an `ast.FunctionDef` directly it yields the node
**and descends into its body**. `sink_calls` avoids this by skipping nested scopes
before it calls the walk (`git_fence.py:234`), and `_collect_functions` never reaches
the walk with a `def`. A yield scan written without that skip attributes a nested
generator's `yield` to its enclosing function — confirmed on a prototype, where a
correctly-fenced outer function measured unfenced because an unrelated nested `def`
contained a `yield`. The plan must pin this with a test, not a comment.

Docstrings stating the old rule must be corrected in the same commit —
`git_fence.py` module docstring, the `stmt_index` semantics note, `sink_calls`, and
`unprotected_sinks`.

### 4.1 The `None` branch is not dead — it is where deferred sinks go

Revision 1 of this spec argued that `stmt_index = None` became unreachable and should
be kept as a defensive fallback. That was wrong for the same reason §2.1 was wrong:
it assumed every sink runs with its containing statement.

`None` now has a **natural, reachable, load-bearing meaning**: the sink sits inside a
deferred context, so its position relative to the acquisition is genuinely unprovable.
The fail-closed branch in `unprotected_sinks` already reports exactly that, and needs
no edit.

This is what the walk must do, and the distinction matters: a deferred sink is
**still discovered** — it stays a sink holder, it stays in the nine, and its function
still appears in `unprotected_sinks`. It is only its *position* that is unknown. A
walk that instead stopped descending at `ast.Lambda` would lose the sink entirely,
turning an unfenced site into an invisible one. That is worse than the bug this slice
fixes.

Concretely: the walk carries a `deferred` flag, set on crossing an `ast.Lambda` or
`ast.GeneratorExp` and inherited by everything beneath. A sink found with the flag set
records `None`; otherwise it records its containing top-level statement index.

## 5. How it is proven

The widening direction is the dangerous one: **over-reporting** lets a slice claim a
conversion it did not make. Under-reporting costs a pin and a sentence. Every proof
below targets over-reporting.

### 5.1 Inertness (the slice gate)

`fence_inventory.json` is unchanged, all nine `git_commit` rows still `fenced: false`,
`SLICE_STATUS` unchanged apart from the new key, pins still 5, and `src/` untouched.

### 5.2 The existing 15 tests must all keep their current verdict

Every one still passes, several for a **new reason** — confirmed by running, not only
by tracing (§5.7). Two are worth naming because they look like they should flip and
do not:

- `test_an_acquisition_nested_as_a_call_argument_is_not_fenced` — the sink is an
  argument of the acquisition, so both live in statement 0. The check is
  `stmt_index <= first_acq`, so `0 <= 0` still reports unfenced. It survives on the
  strict inequality, not on `None`.
- `test_an_acquisition_inside_try_is_not_fenced_conservatively` — the *acquisition* is
  inside the `try`, so the function has no top-level acquisition and fails before
  position is consulted.

Because no existing test flips, **no existing test pins the widened behaviour.** The
new tests below are the only thing that does.

### 5.3 New positive cases — **13**, each must be fenced

Acquisition as a standalone top-level statement at index 0, sink inside the top-level
statement at index 1: `ast.Assign`; `ast.Try` body; `ast.Try` `finally`; `ast.If` body;
`ast.If` `orelse`; `ast.With`; `ast.For`; `ast.While`; `ast.Return`; `async with` in an
`async def`; and — pinning the eager/deferred boundary from the eager side — an
`ast.ListComp` and an `ast.SetComp`.

Plus one that pins §4.2's scope trap from the fenced side: **a correctly fenced
function containing an unrelated nested `def` that is itself a generator.** The outer
function must stay fenced. This is the case a naive yield scan fails.

### 5.4 New negative cases — **15**, each must NOT be fenced

- sink inside a top-level statement at index *i* < *j*
- two sinks straddling the acquisition — one nested before it, one nested after
- acquisition inside the same top-level statement as the sink
- acquisition inside a branch, sink nested in a later statement
- `acquire_commit_mutex_live` present alongside a correct acquisition
- **`acquire_commit_mutex_live` inside a `lambda`**, alongside a correct acquisition and
  a properly ordered sink — verified to disqualify today (§4), and the point is that
  adding the `deferred` flag must not accidentally excuse it
- a sink in a nested `def` whose parent has the acquisition
- **sink inside a `lambda` that is returned** (deferred)
- **sink inside a generator expression that is returned** (deferred)
- **sink inside a `lambda` nested in a returned container literal** (deferred, so the
  flag must be inherited through arbitrary nesting, not only at the top of the value)
- **sink inside a generator expression passed as a call argument** (deferred)
- **a generator function with a `yield` between the acquisition and the sink** (§4.2 —
  this was revision 2's positive)
- **a generator function whose `yield` comes AFTER the sink** — still unfenced, pinning
  that §4.2 is a disqualifier and not a position rule
- **`yield from` rather than `yield`**
- **an `async def` with an own-scope `yield`** (an async generator)

Each deferred and generator case must ALSO assert that the qualname is **still a sink
holder** — `sink_calls` returns it — so a future change that fixes the fencing verdict
by losing the site is caught. §4.1 is the reason: an invisible sink is worse than an
unfenced one.

### 5.5 The mutation harness must be extended

F3e1a's six mutations stay and must still bite. The widening needs its own. Each names
**the tests that redden** — some flip one, some flip a set, and a mutation that flips
several is not thereby weaker. (Revision 2 said "the single test that reddens",
inherited from F3e1a where that happened to hold. Mutations 2, 3 and 5 below each flip
a group by design.)

**Safety mutations — each moves the checker toward OVER-reporting**, which is this
change's risk:

1. change `stmt_index <= first_acq` to `<` — the same-statement argument case flips to
   falsely fenced
2. attribute a nested sink to **`len(body)`** instead of its containing statement index
   — the sink-before-acquisition and the straddling cases flip to falsely fenced.
   (Revision 1 specified mutating to index `0`. That mutation is **inert**: a sink
   nested before the acquisition already has index 0, so nothing changes and no test
   flips. It also mutated toward under-reporting, the harmless direction.)
3. never set the `deferred` flag — all four §5.4 deferred cases flip to falsely fenced
4. drop the §4.2 generator disqualifier — all four §5.4 generator cases flip to falsely
   fenced
5. drop the nested-scope skip in `sink_calls` — the nested-`def` case flips (F3e1a's
   mutation 6, re-verified against the new code)

**Completeness coverage — these move toward UNDER-reporting**, so they prove a case is
pinned rather than that the checker is safe. Kept, and labelled as what they are:

6. set the `deferred` flag on `ast.ListComp`/`ast.SetComp` as well — the two eager
   positives in §5.3 flip to unfenced, pinning the boundary from the eager side
7. drop §4.2's nested-scope skip in the yield scan — the §5.3 unrelated-nested-generator
   positive flips to unfenced, pinning the trap named in §4.2

### 5.6 Real-tree probe

`sink_calls` over `src/` still returns exactly nine holders, and
`fenced_qualnames` over `src/` still returns the empty set.

### 5.7 What the prototypes showed, and why they are exploratory only

Two throwaway prototypes were run against `git_fence.py` at `2086ac19` and reverted;
the tree is unchanged. They are recorded because of what the second one found, not as
evidence the design is correct.

**Prototype 1 — revision 1's rule.** `153 passed` across `tests/fencing/`, no existing
test flipped, all of revision 1's listed positive shapes measured fenced, its listed
negatives did not, and the real tree still yielded exactly nine holders with an empty
fenced set. **All of that was true and the rule was still unsound.** Nothing in the run
touched a `lambda` or a generator expression, because revision 1's case list did not
contain one — so the probe confirmed the cases it was given and said nothing about the
case that mattered. The hole was found by review, not by this run.

**Prototype 2 — the deferred fix of §4.1.** The four §5.4 deferred cases measure
unfenced **and remain sink holders**; the eager comprehensions and every §5.3 positive
still measure fenced; `153 passed` again; the real tree still gives nine holders and an
empty fenced set. **It also credited a generator function as fenced**, which nobody
noticed, because revision 2's case list said that was correct.

**Prototype 3 — the §4.2 generator disqualifier.** All four generator negatives measure
unfenced and remain holders; `yield from` and async generators included; the eager and
deferred verdicts are unchanged; `153 passed`; nine holders, empty fenced set.

**Prototype 3 found a defect in itself, which is the useful part.** Written the obvious
way, the yield scan marked a correctly fenced function unfenced because an unrelated
nested `def` contained a `yield`: `_iter_own_nodes` guards its children but descends
into a `FunctionDef` handed to it directly. That is now §4.2's named trap and a §5.3
positive. It was found by probing a case, not by reading the walker — which had been
read several times by then.

**These runs are exploratory, and the ordering matters.** In all three, the case list
and the result were produced together — a prototype cannot fail a list written after it
ran. The plan must therefore **lock §5.3, §5.4 and §5.5 as failing tests first**, then
implement, then produce fresh results in a later commit. Prototype 1 is the argument
for that discipline, not against prototyping: it passed everything it was asked and was
wrong anyway. Prototype 2 repeated the lesson one revision later.

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
- **Does not credit a deferred sink.** A sink inside a `lambda` or a generator
  expression measures unfenced no matter where the acquisition sits — §2.1. If a real
  site ever takes that shape, it needs a runtime proof and a pin, not a checker change.
- **Does not credit a generator function at all.** Any own-scope `yield` disqualifies
  it, even a `yield` after the sink — §4.2. Correct generators exist; they cost a pin.
- **Does not settle `ENCLOSING_ACQUISITIONS`.** That record says 14 while spec §13
  criterion 9 says seven. It is inert today and stays F3e1b's problem.

## 6.1 Changelog

**Revision 3** — three review findings, all verified against the code before folding,
none rejected.

- **High.** Revision 2 listed a generator function with a `yield` between acquisition
  and sink as a **positive**. The commit mutex is transaction-scoped — *"Released
  automatically at COMMIT or ROLLBACK"* (`drafting/locking.py:195-203`) — so a `yield`
  hands control to a caller who can drop it before the sink runs. Verified that a
  generator body does not execute at all until `next()`, which makes the *acquisition*
  deferred too. Now a disqualifier (§4.2), with four generator negatives in §5.4, a
  §5.3 positive pinning the nested-`def` scope trap, and safety mutation 4.
- **Medium.** §5.5 claimed every mutation targets over-reporting, but the
  eager-comprehension mutation only causes under-reporting. Mutations are now split
  into **safety** (1–5) and **completeness coverage** (6–7). "The single test that
  reddens" is also corrected — several mutations flip a group by design.
- **Low.** §2.1 called the genexp boundary exact. It is not: Python evaluates a
  generator expression's outermost iterable immediately (verified), so a sink in that
  one position is eager. Blanket deferral is now stated as a deliberate under-report.

Found while folding, not by review: written the obvious way, the §4.2 yield scan
misattributes a nested generator's `yield` to its enclosing function, because
`_iter_own_nodes` descends into a `FunctionDef` passed to it directly. Named in §4.2,
pinned by a §5.3 positive and completeness mutation 7.

**Revision 2** — four review findings, all verified against the code before folding,
none rejected.

- **High.** Revision 1 claimed all nesting runs with its containing statement. It does
  not: `lambda` and generator-expression bodies are deferred and can run after the
  transaction ends, and `_iter_own_nodes` descends into both. Reproduced — four shapes
  measured falsely `fenced: true` under revision 1's rule. The rule now reads "runs
  with", §4.1 is rewritten around it, and §5.4 gains four deferred negatives plus a
  holder assertion.
- **Medium.** §5.5's mutation 2 was **inert** — a sink nested before the acquisition
  already has index 0, so mutating to 0 changes nothing — and it mutated toward
  under-reporting, the harmless direction. Now mutates to `len(body)`, and every
  mutation in §5.5 is stated to target over-reporting.
- **Medium.** §5.7 presented a prototype whose case list was written after it ran.
  Recast as exploratory, with the plan required to lock the cases as failing tests
  first. Prototype 1 is now recorded as the cautionary case: it passed everything and
  was unsound.
- **Low.** §5.3 said ten positives and listed eleven; §5.4 said five negatives and
  listed six. Counts are now stated and correct: **13** positives, **11** negatives.

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
