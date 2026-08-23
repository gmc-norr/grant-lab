# Fence F3e1a2 — the containing statement is the order

**Status:** design, revision 4
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
> sink reached from a top-level statement at index *i* > *j* **through an explicitly
> allowlisted path**?

The allowlist is the whole design, and §2.1 explains why it had to become one.

Nothing else changes:

- the acquisition must still be a standalone `ast.Expr` **directly in the function
  body** — an acquisition inside an `if`, a `try` or a `with` still counts for nothing;
- `acquire_commit_mutex_live` anywhere in the function still disqualifies it;
- nested `def`s are still separate qualnames, analysed on their own pass;
- the comparison is still `stmt_index <= first_acq → unfenced`, unchanged.

### 2.1 Why it is sound, and why the exceptions had to become an allowlist

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

### The count of such constructs is an OPEN SET, and that is the real finding

Three review rounds each found a new one, and every one was an over-report:

| Round | Construct | Why it defers |
|---|---|---|
| 1 | `lambda` body | runs on call; the lambda can escape |
| 1 | generator-expression body | runs on consumption; the genexp can escape |
| 2 | generator function (own-scope `yield`) | body does not start until `next()`; every `yield` hands the transaction back |
| 3 | **`type X = sink()`** (PEP 695) | RHS is lazy — verified: it runs only on `__value__` access |

Revisions 1, 2 and 3 each responded by adding the newly-found construct to a
**denylist**: credit everything, except these. That is the same shape as the effect
enumeration that got F2 demoted from a gate, and it fails the same way. Python 3.12
added `ast.TypeAlias`; a later version will add something else; and a denylist's
default for anything unheard-of is **credit it** — the unsafe direction.

**So revision 4 inverts the polarity.** The descent from a top-level statement to a
sink is credited only along an **allowlist** of node types and fields that have been
deliberately vetted as running with their statement. Anything else — an unrecognised
node, a field nobody considered, a construct a future Python adds — yields
`stmt_index = None` and fails closed.

This closes the set. The question stops being *"did we remember to exclude X?"*, whose
wrong answer over-reports, and becomes *"did we deliberately include X?"*, whose wrong
answer under-reports and costs a pin. That is the same trade the whole checker is built
on, applied one level up.

Measured on a prototype of the earlier rules, every one of these measured
`fenced: true`:

```python
deferred = lambda: write_and_commit_files(repo, {}, "m")   # escapes, runs later
gen = (write_and_commit_files(repo, {}, "m") for item in items)
return {"go": lambda: write_and_commit_files(repo, {}, "m")}
register((write_and_commit_files(repo, {}, "m") for i in items))
yield 1; sha = write_and_commit_files(repo, {}, "m")       # mutex may be gone
type Deferred = write_and_commit_files(repo, {}, "m")      # lazy until __value__
```

Generator functions remain a **disqualifier** rather than a position rule (§4.2),
because a `yield` compromises the whole body, not one position in it.

### Eager constructs that the allowlist must deliberately include

Under an allowlist the burden flips: these run with their statement, so they are
credited **on purpose**, and each is a case in §5.3.

- **List, set and dict comprehensions** — own scope, but eager.
- **A `lambda`'s DEFAULTS** — verified eager: `lambda x=sink(): x` calls `sink()` when
  the lambda is created. Only the lambda's *body* defers. Revision 3 deferred the whole
  `ast.Lambda`, which is safe but imprecise; the allowlist must distinguish the fields.
- **A nested `def`'s defaults and decorators** — verified eager, and they execute in the
  **enclosing** scope, not the nested one. §4.3 covers the two consequences.

**The genexp boundary is an intentional under-report.** Python evaluates a generator
expression's *outermost iterable* immediately — verified: `(elem(x) for x in outer())`
calls `outer()` at creation. A sink in that one position is eager and could safely be
credited. The allowlist does not include it: the shape does not occur in the tree, and
distinguishing it would add reasoning this design refuses to do.

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

All in `tests/fencing/git_fence.py`. Revisions 1–3 called this "two lines" and then
"two places"; after §2.1's inversion it is honestly four, and the plan should be
scoped for that.

**`sink_calls`.** Today a sink that is not a bare top-level expression gets
`stmt_index = None` ("position unknown"), and `unprotected_sinks` reports it as
order-unprovable. After this slice it gets **the index of the top-level body statement
that contains it** — but only when every step of the descent was allowlisted.

**The allowlist walk** (§2.1). `_iter_own_nodes` (`git_fence.py:136`) descends into
everything except nested `def`/`class`. It is replaced by a descent that carries a
`credited` flag, cleared the moment it steps through a node or field not on the
allowlist. The allowlist names node types **and the fields within them** — §4.3 needs
`FunctionDef.decorator_list` credited while `FunctionDef.body` is not, so a node-type
allowlist alone cannot express it.

A sink reached with `credited` still set records its containing statement index;
otherwise it records `None` and fails closed. Deferred constructs (`Lambda.body`,
`GeneratorExp`, `TypeAlias.value`) are simply absent from the allowlist rather than
enumerated as exclusions — that is the point of the inversion.

**The generator disqualifier** (§4.2) and **the field-aware scope boundary** (§4.3).

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

**Own-scope means the allowlist walk of §4, not a nested-scope skip.** `_iter_own_nodes`
guards only its *children*: handed an `ast.FunctionDef` directly it yields the node
**and descends into its body**. `sink_calls` avoids this by skipping nested scopes
before it calls the walk (`git_fence.py:234`), and `_collect_functions` never reaches
the walk with a `def`. A yield scan written without that skip attributes a nested
generator's `yield` to its enclosing function — confirmed on a prototype, where a
correctly-fenced outer function measured unfenced because an unrelated nested `def`
contained a `yield`.

But a *plain* nested-scope skip is wrong too, in the other direction — see §4.3.

### 4.3 The nested-scope boundary is field-aware, and today it is not

A nested `def` is not wholly a separate scope. Its **defaults and decorators are
evaluated in the enclosing function**, when the `def` statement runs. Verified both
ways:

```python
def f():
    def inner(x=sink()):    # sink() runs when f() runs, in f's scope
        return x
def f():
    def inner(x=(yield 1)): # this yield makes F a generator
        return x
```

`inspect.isgeneratorfunction(f)` is `True` for the second. Two consequences, one of
them a defect in the code as merged:

**4.3.1 — Discovery gap (pre-existing, `git_fence.py:233-234`).** `sink_calls` skips a
   `FunctionDef` body statement entirely, so a sink in a nested `def`'s default or
   decorator is **not found at all** — confirmed: both shapes yield zero holders today.
   That is worse than unfenced; the site would simply be absent from the inventory. No
   real site currently takes that shape (8 `write_and_commit_files` call sites plus 1
   `_run_git` commit equal the 9 inventory rows exactly), so fixing it is inert — but
   the hole should not be left open next to the one this slice closes.
**4.3.2 — Yield gap.** A `yield` in a nested `def`'s default belongs to the enclosing
   function and makes it a generator. A scan that skips the whole `FunctionDef` never
   sees it, and §4.2's disqualifier misses — an over-report.

Both are fixed by the same thing: the walk crosses a nested `def`/`lambda` boundary
**field-aware**. `decorator_list`, `args.defaults` and `args.kw_defaults` belong to the
enclosing scope and are traversed; `body` does not and is not. For a `lambda`,
`args.defaults` / `args.kw_defaults` are traversed and `body` is not.

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

### 5.3 New positive cases — **15**, each must be fenced

Acquisition as a standalone top-level statement at index 0, sink inside the top-level
statement at index 1: `ast.Assign`; `ast.Try` body; `ast.Try` `finally`; `ast.If` body;
`ast.If` `orelse`; `ast.With`; `ast.For`; `ast.While`; `ast.Return`; `async with` in an
`async def`; and — pinning the eager/deferred boundary from the eager side — an
`ast.ListComp` and an `ast.SetComp`.

Plus three that pin the scope boundaries from the fenced side:

- **a correctly fenced function containing an unrelated nested `def` that is itself a
  generator** — the outer stays fenced (the case a naive yield scan fails, §4.2);
- **a sink in a nested `def`'s default**, at a statement after the acquisition — eager
  in the enclosing scope, so fenced, and it must be **discovered at all** (§4.3.1);
- **a sink in a `lambda`'s default** — eager, so fenced, unlike the same lambda's body.

### 5.4 New negative cases — **18**, each must NOT be fenced

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
- **a `yield` in a nested `def`'s default** — it belongs to the enclosing function and
  makes it a generator (§4.3.2); verified with `inspect.isgeneratorfunction`
- **`type X = sink()`** — PEP 695, RHS lazy until `__value__`; verified to measure
  falsely fenced under revision 3's rule
- **a sink reached through a node type absent from the allowlist** — driven with a
  construct the allowlist does not name, asserting the default is `None` and not
  credit. This is the test that pins the *inversion itself*: without it, a future
  allowlist rewrite could flip the default back to credit-by-default and nothing would
  go red.

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
3. **invert the allowlist default** — credit any descent not explicitly denied. Every
   §5.4 deferred case, the type-alias case, and the unknown-node case flip to falsely
   fenced. This is the mutation that proves §2.1's inversion is real and not just
   prose describing a denylist.
4. drop the §4.2 generator disqualifier — all §5.4 generator cases flip to falsely
   fenced
5. drop the nested-scope skip in `sink_calls` — the nested-`def` case flips (F3e1a's
   mutation 6, re-verified against the new code)
6. **credit `FunctionDef.body` as well as its decorators and defaults** — the §4.3
   field distinction collapses, and the nested-`def`-body cases flip to falsely fenced
7. **credit `Lambda.body` as well as `Lambda` defaults** — the lambda-body negatives
   flip to falsely fenced while the lambda-default positive is unaffected, pinning
   that the two fields are genuinely treated differently

**Completeness coverage — these move toward UNDER-reporting**, so they prove a case is
pinned rather than that the checker is safe. Kept, and labelled as what they are:

8. remove `ast.ListComp`/`ast.SetComp` from the allowlist — the two eager positives in
   §5.3 flip to unfenced, pinning the boundary from the eager side
9. drop §4.2's nested-scope skip in the yield scan — the §5.3 unrelated-nested-generator
   positive flips to unfenced, pinning the trap named in §4.2
10. remove `FunctionDef.decorator_list` / `args.defaults` from the allowlist — the
    §5.3 nested-`def`-default positive flips to unfenced (and, before §4.3.1's fix,
    would vanish as a holder entirely)

### 5.6 Real-tree probe

`sink_calls` over `src/` still returns exactly nine holders, and
`fenced_qualnames` over `src/` still returns the empty set.

§4.3.1 widens discovery, so the nine-count is now a **two-sided** check: it must not
grow (no new site appears) and it must not shrink. It is inert today because the raw
call sites already account for exactly nine — 8 `write_and_commit_files` call sites
plus 1 `_run_git` commit — so nothing is currently hiding in a default or a decorator.
The plan must re-derive that arithmetic rather than assume it.

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

**Revision 4** — two review findings, both verified as counterexamples before folding,
neither rejected. Both were over-reports, which makes it **five over-reports found
across four rounds**, and that trend is what revision 4 actually responds to.

- **High.** `type X = sink()` (PEP 695, `ast.TypeAlias`) is a fourth deferred context —
  verified lazy until `__value__`, and verified to measure **falsely fenced** under
  revision 3's rule.
- **High.** A nested `def`'s defaults and decorators execute in the **enclosing** scope.
  Two consequences, both verified: a `yield` there makes the *enclosing* function a
  generator, which revision 3's nested-scope skip never sees (over-report); and
  `sink_calls` **loses a sink there entirely** today — zero holders, not an unfenced
  one — which is a pre-existing discovery gap at `git_fence.py:233-234`. Also verified:
  a `lambda`'s defaults are eager while its body is not, so the two need different
  treatment.
- **The structural change.** Revisions 1–3 each answered a finding by adding one entry
  to a **denylist**, whose default for anything unheard-of is *credit it* — the unsafe
  direction, and an open set, which is what got F2 demoted from a gate. §2.1 now
  **inverts the polarity**: descent is credited only along an explicit allowlist of node
  types **and fields**, and anything unrecognised fails closed. §5.4 gains a test for an
  unallowlisted node, and safety mutation 3 flips the default back to prove the
  inversion is real.
- §4 is honest that this is no longer a two-line change, and §5.6's nine-count becomes
  a two-sided check because §4.3.1 widens discovery.

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
