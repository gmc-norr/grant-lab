# Fence F3e1a2 — a narrow allowlist for a position, not a proof of a lock

**Status:** design, revision 7
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
| `Phase0Worker._init_repo` | F5 | `ast.If` (`_run_git`) |

**Not one of the nine is a bare top-level expression statement.** The checker can
credit exactly zero of them, so no slice that owns a `git_commit` row can ever reach
`complete` under §3.6's bidirectional rule.

**The narrower fix was considered and rejected.** Crediting an `ast.Assign` whose
value is the sink covers 4 of the 9 and **3 of F3e1b's 4** — `_insert_citation_locked`
sits in a `try:` and would still need a pin and a documented exception to §3.6. That
is the same hole, one size smaller, and F3e2 would hit it again immediately.

## 2. What this checker actually proves — read this before §3

**It proves lexical order. It does not prove the mutex is held.**

Revisions 1–4 of this spec called the rule "sound". That was wrong, and the error
survived four review rounds inside a §2.1 titled *"Why it is sound"*. The commit mutex
is **transaction-scoped** — `acquire_commit_mutex` says so plainly: *"Released
automatically at COMMIT or ROLLBACK — there is no explicit release"*
(`drafting/locking.py:195-203`). So this shape measures **fenced** and is **not**:

```python
acquire_commit_mutex_at_generation(s, w, generation=stamp.generation)
s.commit()                                  # mutex released HERE
write_and_commit_files(repo, {}, "m")       # runs with no mutex at all
```

Verified: `{'f'}`. Same for `s.rollback()`. And worse, this is equally false-fenced and
**this intraprocedural checker cannot catch it**, because the commit is behind a call.
An interprocedural analysis over known helpers could; this one deliberately looks at a
single function and nothing else:

```python
acquire_commit_mutex_at_generation(s, w, generation=stamp.generation)
helper(s)                                   # helper() commits — invisible from here
write_and_commit_files(repo, {}, "m")
```

So the claim this checker is entitled to make is narrow:

> The acquisition call **precedes** the sink call in execution order, along a path
> made only of constructs that run with their statement.

That is **evidence, not proof** — a cheap ratchet, exactly as the parent spec's §5.1
already says. Three consequences, all binding:

1. **§5.2 of the parent spec is mandatory, not optional.** The runtime two-ordered-lock
   cases are what actually prove an effect is fenced. A `fenced: true` measurement is
   necessary for a slice to claim `complete`; it is not sufficient, and F3e1b's plan
   must carry the runtime proof per converted site.
2. **A visible transaction ending is disqualified** (§4.4) — a cheap ratchet over the
   cases that *are* visible, not a claim to catch them all.
3. **`fenced: true` means "the cheap check found nothing wrong"**, not "this site is
   safe". F3e1a already documents that `fenced: false` does not mean broken; the
   converse now needs saying too, and §7 says it.

## 3. The rule

Discovery and crediting are **separate**, and pull in opposite directions.

**Discovery is broad.** Every sink call reachable in the function's own scope is found
and its qualname becomes a holder — including sinks in places that will never be
credited. An unfenced site must be *visible*; a site missing from the inventory is
worse than one measuring `false`.

**Crediting is narrow.** A sink gets a position — its containing top-level statement
index — only if **every step** of the descent from that statement to the call is on the
allowlist in §4.1. Otherwise `stmt_index = None` and the existing fail-closed branch
reports it unfenced.

Then, unchanged from F3e1a: the acquisition must be a standalone `ast.Expr` directly in
the function body at index *j*, every sink must have an index *i* > *j*, and the
comparison is still `stmt_index <= first_acq → unfenced`.

Plus three disqualifiers, each overriding any position: `acquire_commit_mutex_live`
anywhere (F3e1a, unchanged), an own-scope `yield` (§4.3), and a visible transaction
ending (§4.4).

### 3.1 Why the exclusions became an allowlist

Four review rounds each found a construct that *looks* like it runs with its statement
and does not. Every one was an over-report — the direction that lets a slice claim a
conversion it never made:

| Round | Construct | Why it defers |
|---|---|---|
| 1 | `lambda` body | runs on call; the lambda can escape |
| 1 | generator-expression body | runs on consumption; the genexp can escape |
| 2 | generator function (own-scope `yield`) | body does not start until `next()` |
| 3 | `type X = sink()` (PEP 695) | RHS lazy — verified, runs only on `__value__` |
| 4 | `s.commit()` between the two | releases the transaction-scoped mutex (§2) |

Revisions 1–4 each answered by adding one entry to a **denylist**: credit everything,
except these. A denylist's default for anything unheard-of is **credit it** — the unsafe
direction, over an open set. That is the shape of the effect enumeration that got F2
demoted from a gate, and it was failing the same way, once per round.

**So the polarity is inverted.** Descent is credited only along an explicit allowlist,
and anything unrecognised fails closed. The question stops being *"did we remember to
exclude X?"*, whose wrong answer over-reports, and becomes *"did we deliberately include
X?"*, whose wrong answer costs a pin.

The exclusions then **disappear from the design**. There is no deferred-context flag,
no `ast.Lambda`/`ast.GeneratorExp`/`ast.TypeAlias` special case: those are simply not on
the list, along with everything else nobody vetted. Revisions 2–4 specified a `deferred`
flag; revision 5 deletes it as redundant machinery.

## 4. What changes in the code

All in `tests/fencing/git_fence.py`. Revisions 1–3 called this "two lines"; it is four
changes, and the plan should be scoped for that.

### 4.1 The allowlist — the canonical table

Each entry is a **(node type, field)** pair. A node-type-only list cannot express this:
§4.2 needs `FunctionDef.decorator_list` walked for discovery while `FunctionDef.body`
is never credited.

| Node type | Credited field | Why it runs with the statement |
|---|---|---|
| `ast.Assign` | `value` | RHS evaluated when the assignment runs |
| `ast.Expr` | `value` | the statement *is* the expression |
| `ast.If` | `body` | the taken branch runs with the statement |
| `ast.Try` | `body` | runs first, before any handler |
| `ast.With` | `body` | runs after `__enter__`, within the statement |
| `ast.AsyncWith` | `body` | as `With` |

**Six entries, and they are exactly what the nine production paths need** — derived
independently from the tree, not chosen: the full set of `node.field` steps across all
nine sites is exactly `Assign.value`, `Expr.value`, `If.body`, `Try.body`, `With.body`,
`AsyncWith.body`.

**Everything else is uncredited, and that is deliberate**, including constructs that are
genuinely eager: `If.orelse`, `Try.handlers`, `Try.finalbody`, `For.body`, `While.body`,
`Return.value`, `AnnAssign.value`, `AugAssign.value`, every comprehension, and every
function or lambda default. Each is an under-report costing a pin. **Do not add an entry
because it is eager** — add one only when a real site needs it, as a reviewed diff, with
its own §5.3 case. The list's smallness is the safety property; growing it "for
completeness" undoes this revision.

### 4.2 The walk contract

**Added alongside `_iter_own_nodes` (`git_fence.py:136`), not replacing it.**
`_iter_own_nodes` has a second consumer, `_collect_functions` (`git_fence.py:176`),
which iterates it as **plain AST nodes** (`for child in …`, `child is stmt`,
`isinstance(child, …)`). Changing it to yield tuples breaks that caller silently — it
would compare a tuple against `stmt` and `isinstance`-test a tuple, both simply False,
so qualname discovery would go quietly empty rather than error. `_iter_own_nodes` and
`_collect_functions` are therefore **left untouched**, and the new walk is a separate
function used by `sink_calls` and the §4.3 yield scan.

The two walks must agree on one thing — the nested-scope boundary — and the nine-holder
real-tree probe (§5.6) plus the existing nested-`def` tests are what pin that agreement.

**`sink_calls`'s pre-skip is REMOVED.** Today `sink_calls` skips a `FunctionDef` /
`AsyncFunctionDef` / `ClassDef` body statement before it walks
(`git_fence.py:233-235`), which is exactly why a sink in a nested `def`'s default or
decorator is invisible. That `continue` goes; the boundary moves *inside* the new
walk, which walks a nested definition's `decorator_list` and defaults while never
entering its `body`. Revision 5's mutation 10 assumed the pre-skip survived — it does
not, and §5.5 is corrected.

The new walk yields `(node, credited)` and obeys:

- **Monotonic.** `credited` starts `True` at the top-level statement and can only go
  `False`. It never returns to `True` deeper in the tree — a credited field beneath an
  uncredited one is still uncredited.
- **Discovery does not stop at `False`.** The walk keeps descending so sinks are still
  found; only their position is lost. It descends into `Lambda`, `GeneratorExp`,
  `TypeAlias`, comprehensions and every unlisted field, all with `credited=False`.
- **A definition's ONLY unwalked field is a FUNCTION's body.** Revision 6 enumerated
  `decorator_list`, `args.defaults` and `args.kw_defaults`; that enumeration was
  incomplete, in the same open-set way §3.1 describes. Everything else a `def` or
  `class` statement carries executes in the enclosing scope when that statement runs,
  and **all of it must be walked** (uncredited). Verified invisible under revision 6's
  enumeration — zero holders for every one:

  | Shape | Runs when |
  |---|---|
  | nested `def` / `lambda` argument defaults | the `def`/`lambda` statement runs |
  | nested `def` argument and **return annotations** | same, unless the scanned file has `from __future__ import annotations` — which the checker cannot rely on |
  | `class` **body** statements | the `class` statement runs |
  | `class` **bases** and **keywords** (e.g. `metaclass=`) | same |
  | `def` / `class` **decorators** | same |

  Three definition kinds, three different rules, and the asymmetry is deliberate:
  a nested `def`'s **body** is skipped (own qualname, separate pass); a `lambda`'s body
  is **walked** (anonymous — nothing else would ever visit it, so skipping loses the
  sink); a `class`'s body is **walked** (it runs at definition time; methods inside are
  `FunctionDef`s and stop at their own body by the first rule).

  This closes a pre-existing discovery gap: today `sink_calls` skips a `FunctionDef`
  statement whole, so a sink in any of the above yields **zero holders** — invisible,
  not merely unfenced.
- **A `FunctionDef` handed to a walk directly is a trap.** `_iter_own_nodes` guards
  only its *children*, so passing it a `def` descends into the body. `sink_calls`
  avoided this with the pre-skip that is now being removed, so the new walk must handle
  a `def` **as its entry node**, not only as a child. A prototype of §4.3 hit exactly
  this and marked a correctly fenced function unfenced.

### 4.3 The generator disqualifier

Independent of position: if the function has an **own-scope** `ast.Yield` or
`ast.YieldFrom`, it is unfenced. §3.1 is the reason — a generator body does not begin
until `next()`, so the *acquisition* is deferred too, and every `yield` hands the
transaction back to a caller who may end it.

It fires even when the `yield` sits **after** the sink. Deliberately over-broad: a
correct generator costs a pin, and deciding which yields precede which sinks is the
branch reasoning this design refuses to do.

"Own-scope" uses §4.2's field-aware walk, so a `yield` in a nested `def`'s **default**
is seen — it makes the *enclosing* function a generator (verified with
`inspect.isgeneratorfunction`) — while a `yield` in that def's **body** is not.

### 4.4 The transaction-ending disqualifier

If a call to `.commit()` or `.rollback()` appears in the function's own scope, it is
unfenced. §2 is the reason.

**This is a ratchet over the visible cases and nothing more.** It cannot see a helper
that commits. It is stated as partial in the code, not as a guarantee, and it does not
license dropping the parent spec's §5.2 runtime proof. Matching on attribute name means
an unrelated `.commit()` also disqualifies — an under-report, which is the safe
direction and needs no special handling.

## 5. How it is proven

The widening direction is the dangerous one: **over-reporting** lets a slice claim a
conversion it did not make. Under-reporting costs a pin and a sentence.

### 5.1 Inertness (the slice gate)

`fence_inventory.json` byte-identical, all nine `git_commit` rows still `fenced: false`,
`SLICE_STATUS` unchanged apart from the new key, pins still 5, `src/` untouched.

Inert because `acquire_commit_mutex_at_generation` has **zero production callers**
(verified at `2086ac19`: the only occurrence in `src/` is its definition at
`fencing.py:115`), so every row fails the positive rule before position is consulted.

### 5.2 The existing 15 tests keep their current verdict

Confirmed by running, not only by tracing (§5.7). Two look like they should flip and do
not: `test_an_acquisition_nested_as_a_call_argument_is_not_fenced` survives on the strict
`stmt_index <= first_acq`, not on `None`; and
`test_an_acquisition_inside_try_is_not_fenced_conservatively` fails on the *acquisition*
side before position matters. Since none flips, **none pins the new behaviour** — §5.3
and §5.4 are the only things that do.

### 5.3 Positive cases — **7**, each must be fenced

Acquisition standalone at index 0, sink reached from the statement at index 1:

1. `Assign.value` — `sha = sink(...)`
2. `Expr.value` — the bare call (F3e1a's shape, still credited)
3. `If.body`
4. `Try.body`
5. `With.body`
6. `AsyncWith.body` in an `async def`
7. a correctly fenced function containing an unrelated nested `def` that is itself a
   generator — the outer stays fenced (the case a naive yield scan fails, §4.3)

One per allowlist entry, plus the scope-boundary case. There is deliberately **no**
positive for `For`, `While`, `Return`, comprehensions or defaults: §4.1 does not credit
them, and a positive test for one would be a request to widen the list.

### 5.4 Negative cases — **30**, each must NOT be fenced

*Order and acquisition position (F3e1a's rules, re-verified against the new walk):*

- sink reached from a top-level statement at index *i* < *j*
- two sinks straddling the acquisition — one before, one after
- acquisition inside the same top-level statement as the sink
- acquisition inside a branch, sink in a later statement
- `acquire_commit_mutex_live` alongside a correct acquisition
- `acquire_commit_mutex_live` inside a `lambda`, alongside a correct acquisition and an
  ordered sink — verified to disqualify today; the walk changes must not excuse it
- a sink in a nested `def`'s **body** whose parent has the acquisition

*Uncredited paths (the allowlist's default):*

- sink in a `lambda` **body**, returned
- sink in a generator expression, returned
- sink in a `lambda` nested inside a returned container literal
- sink in a generator expression passed as a call argument
- `type X = sink()` — PEP 695, verified falsely fenced under revision 3
- **sink under a node type absent from the allowlist**
- **sink under an unlisted FIELD of a listed node** — `If.orelse` is the case to use.
  The unknown-*node* test does not prove field granularity; without this one, an
  implementation could allowlist by node type alone and pass everything else.

*Discovery — uncredited but MUST still be found (§4.2's fix):*

These are the cases that drive the pre-existing invisible-sink defect. Each asserts
**both** that the outer function is unfenced **and** that it is still a holder with
`stmt_index is None`. Without them the defect survives every other test on this list,
and revision 5's mutation 11 had nothing to redden.

- **sink in a nested `def`'s default** — `def inner(x=sink()): ...`
- **sink in a nested `def`'s decorator** — `@register(sink())`
- **sink in a `lambda`'s default** — `lambda x=sink(): x`
- **sink in a nested `async def`'s default**
- **sink in a nested `def`'s argument annotation** — `def inner(x: sink()): ...`
- **sink in a nested `def`'s return annotation** — `def inner() -> sink(): ...`
- **sink in a `class` body** — `class C: x = sink()`
- **sink in a `class` base** — `class C(sink()): ...`
- **sink in a `class` keyword** — `class C(metaclass=sink()): ...`
- **sink in a `class` decorator** — `@register(sink())` on a `class`

(All four are eager in the enclosing scope — verified — yet none is credited, because
defaults and decorators are not on §4.1's six-entry list. Eager is not a reason to
credit; §4.1.)

*Disqualifiers:*

- generator: `yield` between acquisition and sink
- generator: `yield` **after** the sink — §4.3 is a disqualifier, not a position rule
- `yield from` rather than `yield`
- `async def` with an own-scope `yield`
- a `yield` in a nested `def`'s **default** — belongs to the enclosing function
  (verified with `inspect.isgeneratorfunction`)
- a transaction ending between the acquisition and the sink (§4.4), **parameterised over
  both `s.commit()` and `s.rollback()`** — both verified falsely fenced under revisions
  1–4, and §4.4 disqualifies both, so testing only `commit` would leave `rollback`
  unpinned

Every uncredited and disqualified case must ALSO assert the qualname is **still a sink
holder**, so a future change that fixes a verdict by losing the site is caught. §3 is
the reason: an invisible sink is worse than an unfenced one.

### 5.5 Mutations

F3e1a's six stay and must still bite. Each below names **the tests that redden** —
several flip a group by design, and that does not make them weaker.

**Safety — each moves the checker toward OVER-reporting**, this change's risk:

1. `stmt_index <= first_acq` → `<` — the same-statement argument case falsely fences
2. attribute a nested sink to **`len(body)`** instead of its container — the
   sink-before-acquisition and straddling cases falsely fence. (Revision 1 specified
   mutating to `0`. **Inert**: a sink nested before the acquisition already has index 0.)
3. **invert the allowlist default** — credit unless explicitly denied. Every uncredited
   case in §5.4 falsely fences. This is what proves §3.1's inversion is real and not
   prose describing a denylist.
4. **credit by node type, ignoring the field** — the `If.orelse` case falsely fences,
   proving the table's second column is load-bearing.
5. **credit `FunctionDef.body`** — the nested-`def`-body case falsely fences, collapsing
   §4.2's field distinction.
6. **credit `Lambda.body`** — the lambda-body negatives falsely fence.
7. drop the §4.3 generator disqualifier — all generator cases falsely fence
8. drop the §4.4 transaction disqualifier — the `s.commit()` case falsely fences
9. **make `credited` non-monotonic** — let a listed field beneath an unlisted one
   restore credit; a sink at `Lambda.body` → `Assign.value` falsely fences

**Discovery and completeness — these move toward UNDER-reporting or affect only
visibility.** They prove a case is pinned, not that the checker is safe:

10. **restore `sink_calls`'s pre-skip** (`git_fence.py:233-235`) — the four §5.4
    discovery cases lose their holder entirely, reproducing the pre-existing defect.
    It does **not** falsely fence, which is why this sits here and not above; mutation 5
    is the safety mutation for that boundary. (Revisions 3–5 wrote this as "drop the
    nested-scope skip", which assumed the pre-skip survived. §4.2 removes it, so the
    mutation is its restoration.)
11. stop walking a nested definition's `decorator_list` / `args.defaults` /
    `args.kw_defaults` inside the walk — the same four §5.4 discovery cases lose their
    holder, by a different route than mutation 10
12. remove one entry from §4.1's allowlist table — **name the expected test set per
    entry rather than claiming one test each.** The table's paths *compose*: a sink in
    an `if` is reached by `If.body` → `Expr.value`, so removing `Expr.value` flips the
    bare-expression positive **and** the `If.body`, `Try.body`, `With.body` and
    `AsyncWith.body` positives, since all four reach their call through a statement
    node. Removing `Assign.value` flips only the assignment positive. The plan states
    the expected flip set for each of the six.

### 5.6 Real-tree probe

`sink_calls` over `src/` returns exactly nine holders and `fenced_qualnames` returns the
empty set.

§4.2 widens discovery, so the nine-count is a **two-sided** check: it must not grow and
must not shrink. Inert today because the raw call sites already account for exactly nine
— 8 `write_and_commit_files` call sites plus 1 `_run_git` commit — so nothing is hiding
in a default or decorator. The plan must re-derive that arithmetic, not assume it.

### 5.7 What the prototypes showed, and why they are exploratory only

Three throwaway prototypes were run against `git_fence.py` at `2086ac19` and reverted;
the tree is unchanged. They are recorded for what they got wrong.

**Prototype 1 — revision 1's rule.** `153 passed`, no existing test flipped, every listed
positive measured fenced, every listed negative did not, real tree unchanged. **All true,
and the rule was unsound**: nothing in the run touched a `lambda`, because revision 1's
case list did not contain one.

**Prototype 2 — the deferred fix.** Deferred cases unfenced and still holders, eager
cases still fenced, `153 passed`, nine holders. **It also credited a generator as
fenced**, unnoticed, because revision 2's case list said that was correct.

**Prototype 3 — the generator disqualifier.** All generator negatives unfenced and still
holders; `153 passed`; nine holders. **It also credited `type X = sink()` and
`s.commit()` between the two**, because revision 3's case list did not ask.

**Each prototype confirmed the list it was given and was blind to what the list omitted
— three times running.** That is the argument for §5.3/§5.4/§5.5 being locked as failing
tests **before** implementation, and for fresh results in a later commit. It is not an
argument against prototyping: probing found §4.2's `FunctionDef` trap in seconds, in a
walker that had been read carefully across three revisions.

## 6. What this does not do

- **Does not convert anything.** No `src/` change; Primitive B still has zero production
  callers when this lands.
- **Does not move a row or a pin.**
- **Does not relax the acquisition side.** An acquisition in a branch, a `try`, or a call
  argument still counts for nothing.
- **Does not prove the mutex is held** — §2. It proves lexical order along vetted
  constructs, and the parent spec's §5.2 runtime proof remains mandatory.
- **Does not credit anything merely because it is eager** — §4.1. The allowlist grows
  only when a real site needs it.
- **Does not settle `ENCLOSING_ACQUISITIONS`.** That record says 14 while spec §13
  criterion 9 says seven. Inert today; stays F3e1b's problem.

## 7. `fenced` means less than it looks like, in BOTH directions

F3e1a established that **`fenced: false` does not mean broken** — the checker
under-reports by design, and three correct sites will stay pinned.

Revision 5 adds the converse, which matters more: **`fenced: true` does not mean safe.**
It means the cheap static check found nothing wrong along a vetted path. A transaction
that ends inside a called helper is invisible to it (§2). A slice claims `complete` on
the measurement **plus** the parent spec's §5.2 runtime two-ordered-lock proof — never on
the measurement alone.

Both sentences belong in `git_fence.py`'s module docstring, next to each other — **and
the word "provably" has to go from the places that already claim more than the checker
delivers.** Two exist today and both are read by someone deciding whether a site is
safe:

- `git_fence.py:366` — `fenced_qualnames`: *"Return the set of qualnames whose git-sink
  calls are **provably** fenced."*
- `live_mutex_gate.py:1` — the gate's module docstring: *"every function holding a git
  sink must be pinned or **provably** fenced."*

Both become "passes the lexical check" or equivalent. Leaving them is not a wording
nit: `live_mutex_gate.py` is the gate a future slice reads to decide it is done, and
§2 is the reason it must not say "provably".

## 8. Slice status

`SLICE_STATUS` gains `"F3e1a2": "complete"` in the landing commit, following
`F3b`/`F3c`/`F3d1`/`F3e1a`: a key owning no inventory row. Note
`test_each_slice_matches_its_declared_status` iterates **rows**, so a zero-row key
asserts nothing — it exists so a future row naming this slice cannot fall through with an
unknown status.

## 9. After this lands

F3e1b becomes plannable. Each conversion must place
`acquire_commit_mutex_at_generation` as a standalone statement in the function body ahead
of the statement holding the sink, and land the sink on one of §4.1's six paths. All four
F3e1b sites already qualify: three are `Assign.value`, and `_insert_citation_locked` is
`Try.body` — its acquisition must move above the `try:`, where it belongs anyway.

And per §2 and §7, each converted site carries its runtime two-ordered-lock proof. The
measurement is the ratchet; the runtime case is the proof.

## 10. Changelog

**Revision 7** — the walk contract's field enumeration was incomplete, in exactly the
open-set way §3.1 warns about. Found while reviewing the implementation plan, whose
prototype measured **zero holders** for six shapes revision 6's contract could not
reach: `class` bodies, bases, keywords and decorators, and function argument and return
annotations. All execute in the enclosing scope when their statement runs, and a class
base can carry an own-scope `yield`.

§4.2 no longer enumerates `decorator_list` / `defaults` / `kw_defaults`. It states the
rule that closes the set: **a definition's only unwalked field is a FUNCTION's body**,
with `lambda` and `class` bodies walked for the reasons given there. §5.4's discovery
group grows from 4 cases to 10, and the negatives from 24 to 30.

Also: §2's "no static checker can ever catch it" was too absolute — an interprocedural
analysis over known helpers could. It now says **this intraprocedural checker cannot**,
which is the true and still-sufficient claim.


**Revision 6** — five findings, all verified against the code before folding, none
rejected. Two were High, and both were internal contradictions in revision 5 rather than
new facts about Python.

- **High — the walk contract contradicted its own mutation, twice.** §4.2 required the
  new walk to reach a nested definition's decorators and defaults, but `sink_calls`
  `continue`s past those statements *before* walking (`git_fence.py:233-235`), so it
  never would; and mutation 10 assumed that pre-skip survived. §4.2 now states the
  pre-skip is **removed** and the boundary moves inside the walk, and mutation 10 is
  its **restoration**. Separately: `_iter_own_nodes` has a second consumer,
  `_collect_functions` (`git_fence.py:176`), which iterates it as plain nodes — yielding
  tuples would make `child is stmt` and `isinstance(child, …)` silently False and empty
  qualname discovery **without an error**. So the new walk is **added alongside**;
  `_iter_own_nodes` and `_collect_functions` are untouched.
- **High — nothing tested the discovery fix.** Mutation 11 expected a nested-default
  sink to vanish, but no §5.4 case put a sink in a nested default or decorator; the
  yield-default case has its sink elsewhere, so holder visibility never moved. §5.4
  gains a **Discovery** group of four cases — nested-`def` default, nested-`def`
  decorator, `lambda` default, nested-`async def` default — each asserting the function
  is unfenced **and still a holder with `stmt_index is None`**. Without them the
  pre-existing invisible-sink defect survived every listed test.
- **Medium — the docstring update was incomplete.** §7 mandated only the module
  docstring while `fenced_qualnames` (`git_fence.py:366`) and `live_mutex_gate.py:1`
  both still say "**provably** fenced". Both are now named. The gate's docstring matters
  most: it is what a later slice reads to decide it is done.
- **Low — `rollback` was unpinned.** §4.4 disqualifies `.commit()` and `.rollback()`;
  §5.4 and mutation 8 exercised only `commit`. The case is now parameterised over both.
- **Low — mutation 12's "one test per entry" is impossible.** The table's paths compose:
  a sink in an `if` is reached by `If.body` → `Expr.value`, so removing `Expr.value`
  flips five positives, not one. The plan now names the expected flip set per entry.

**Revision 5** — four findings, all verified as counterexamples before folding, none
rejected. Two were High.

- **High — the soundness claim was false, and had survived four rounds.** `acquire();
  s.commit(); sink()` measures fenced; the mutex is transaction-scoped and `commit()`
  releases it. Verified, along with `rollback()` and the unfixable case where a helper
  commits. §2 is new and replaces "Why it is sound": the checker proves **lexical order,
  not lock-held-ness**, the parent spec's §5.2 runtime proof becomes **mandatory**, §4.4
  adds a partial ratchet over visible endings, and §7 states the converse of F3e1a's
  rule — **`fenced: true` does not mean safe.**
- **High — the allowlist was never enumerated.** Revision 4 described a mechanism and
  left the table to implementation judgement, which is a placeholder in a safety
  mechanism. §4.1 now gives the canonical (node, field) table: **six entries**, derived
  independently from the nine production paths and matching the reviewer's derivation
  exactly. §4.2 states the walk contract, including monotonicity. The stale
  `deferred`-flag machinery from revisions 2–4 is **deleted** — under an allowlist the
  exclusions are simply absent, which is the point.
- **Medium — mutation 5 was not a safety mutation.** Dropping the `sink_calls` scope skip
  while `FunctionDef.body` stays uncredited adds a holder with `stmt_index=None`; it does
  not falsely fence. Moved to discovery coverage (now 10); crediting `FunctionDef.body`
  is the real safety mutation.
- **Medium — promised field behaviour was untested.** §5.4 gains a sink under an
  **unlisted field of a listed node** (`If.orelse`), and mutation 4 credits by node type
  alone to prove the second column is load-bearing. The rest of the gap dissolved instead
  of being filled: `DictComp`, keyword-only defaults and nested-`def` decorators are not
  on the six-entry allowlist at all, so they need negatives, not positives.
- Case counts shrank with the allowlist: **7 positives** (one per entry, plus the scope
  case), **30 negatives**, **12 mutations** (9 safety, 3 discovery/completeness).

**Revision 4** — `ast.TypeAlias` (PEP 695) is a deferred context; a nested `def`'s
defaults and decorators execute in the enclosing scope, which both hid a `yield` from the
generator scan and lost sinks from discovery entirely. Denylist inverted to an allowlist.

**Revision 3** — a `yield` can drop the mutex; generator functions disqualified. Mutation
policy split into safety and completeness. The genexp boundary restated as an intentional
under-report.

**Revision 2** — `lambda` and generator-expression bodies are deferred and were falsely
fenced. `stmt_index = None` reinterpreted as their home rather than dead code.

**Revision 1** — the containing-statement rule, replacing F3e1a's bare-expression rule.
