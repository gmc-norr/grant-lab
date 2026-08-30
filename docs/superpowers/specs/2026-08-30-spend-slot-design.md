# `spend_slot` — one door for hosted-LLM spend

**Date:** 2026-08-30
**Status:** design, approved in outline
**Follows:** the LLM spend ledger, merged `b95bef49` (18 commits, nine outside-review rounds)

## 1. Why this exists

The ledger branch centralized **storage**. It did not centralize **control flow**.

Measured on `716ed622`:

| | count |
|---|---|
| hand-written `record_spend(...)` call sites | **20**, across **8** files |
| hand-written `check_global_daily_token_cap(...)` sites | **14** |
| `return`/`raise` statements per pass runner | coherence 15, evaluator 17, uncited_claims 19, review 15 |

Every caller must independently remember three things: check the cap before the
call, record on *every* exit, and get the status right for each exit kind. There
are 15–19 exits per runner.

Nine review rounds found ~30 real defects. Sorted by shape:

| shape | count |
|---|---|
| missed one of many exit paths | ~11 |
| cap checked in the wrong place, or not at all | ~5 |
| ways to fool the AST gate | ~14 |
| a genuine concurrency race | **0** |

**Six** of those defects were regressions introduced while fixing others.

The AST gate (`tests/fencing/spend_guard.py`) exists *because* the obligation is
manual. It is a smoke detector for a design that leaks.

### What this design is NOT aimed at

Serializing hosted calls ("only spend when nothing else is in flight") was
considered and rejected. Zero of the ~30 findings were races. A lock makes calls
take turns; it does not make a `return` on line 340 record anything. It would
also serialize review's deliberate six-call fan-out.

Replacing the ledger with Anthropic's Usage & Cost Admin API
(`/v1/organizations/usage_report/messages`) was also considered and rejected as a
*replacement*: all of this app's traffic uses one API key, so the API cannot
distinguish one hospital research group's workspace from another's, and this app
has per-workspace caps and a per-workspace admin view. Its data also lags
~5 minutes. It remains valuable as an independent **reconciliation** check and is
specified separately — see §9.

## 2. The guarantee

**You cannot make a hosted-LLM call without the ledger knowing.**

```python
with spend_slot(
    session_factory,
    settings,
    workspace_id=job.workspace_id,
    kind="coherence",
    ref_id=job.id,
) as slot:
    done = await adapter.send_turn(handle, instruction)
    slot.report_done(done)          # or slot.report(tokens_in=..., tokens_out=...)

job.input_tokens = slot.tokens_in     # display cache — still the caller's job
job.output_tokens = slot.tokens_out
```

`spend_slot` is a **synchronous** context manager. Its `__enter__` and `__exit__`
do only synchronous database work, so it composes with both the async runners and
the three synchronous spenders.

That a plain `with` inside `async def` observes every termination was verified by
execution, not assumed:

| body ends with | `__exit__` sees | re-raised correctly |
|---|---|---|
| normal return | `None` | — |
| `raise RuntimeError` | `RuntimeError` | yes |
| `asyncio.wait_for` timeout | `TimeoutError` | yes |
| `raise SystemExit` | `SystemExit` | yes |
| `task.cancel()` | `CancelledError` | yes — `task.cancelled()` stays `True` |

This is the single biggest simplification. The "catch `BaseException`, record,
re-raise, but flip the job to `failed` only for an `Exception`" dance that
consumed review rounds 4 through 7 becomes free: `__exit__` runs for every one of
those cases, and returning `False` re-raises with correct semantics.

## 3. The row's lifecycle — reserve on enter, settle on exit

| moment | `status` | tokens |
|---|---|---|
| cap denies, before any call | `denied` | 0, 0 |
| slot opens | `in_flight` | 0, 0 |
| body returns | `ok` | as reported |
| body raises `Exception` | `failed` | as reported so far |
| body raises `TimeoutError` / `asyncio.TimeoutError` | `timeout` | as reported so far |
| body raises any other `BaseException` | `failed`, then **re-raised** | as reported so far |
| worker killed (`SIGKILL`) | stays `in_flight` | 0, 0 |

The last row is new behaviour. Today a `kill -9` between the hosted call and the
record leaves **no evidence at all** that a call was made.

**This makes `llm_spend` no longer append-only**, contradicting its current
docstring and the `LlmSpend` model docstring. Both must be rewritten in Slice 1;
leaving a docstring that promises append-only would be exactly the
"comment that promises a guarantee the code does not deliver" defect this project
hit three times.

Each phase commits on its own short-lived session from `session_factory`. The
reserve commit must land before the hosted call starts, or the guarantee is void.

## 4. Exactly-once becomes a constraint, not a habit

`llm_spend` gains:

```
attempt_id  UUID  NOT NULL  UNIQUE
```

The slot mints an `attempt_id` in `__enter__`. Settling is an UPDATE on that key.

Today "exactly once" is guaranteed by **placement**: each `record_spend` sits
directly below a conditional `UPDATE ... WHERE status='running'` that is the
call's linearization point. That is correct, and it is also a rule a human must
apply correctly at 20 sites — the class of thing that broke repeatedly.

With `attempt_id`, `outcomes.py` stops writing a second row. Its two sites become
**settle**, not record:

```sql
UPDATE llm_spend SET status = :s, tokens_in = :i, tokens_out = :o
 WHERE kind = :kind AND ref_id = :ref AND status = 'in_flight'
```

For chat, `ref_id` is the `TurnJob` id and one `TurnJob` is one hosted call, so
this is unambiguous. The reaper, the orphan sweep, the delete path, and
`web/api/turns.py` all reach `record_terminal_outcome`; all of them settle a row
that already exists rather than appending one that might duplicate.

**Known limit, stated deliberately:** citation spend uses the operation name as
`ref_id` (`op`, a string), so several citation calls share one `ref_id`. Settling
by `ref_id` is therefore ambiguous for citations. Citations have no reaper and no
post-terminal reconciliation path, so nothing settles them from outside the slot.
The migration must not assume `(kind, ref_id)` is unique.

## 5. What the slot does NOT own

Being explicit, because "one wrapper for everything" is not achievable and a spec
that implies it would mislead its implementer.

**The 6 admission cap checks stay as they are.** `web/coherence.py`,
`web/evaluator.py`, `web/review.py`, `web/uncited_claims.py`, `turns/submit.py`
and `web/api/admin.py` check the cap to refuse a *request* with a 429. They make
no hosted call, so there is no slot to open.

**The 2 `outcomes.py` record sites become settles, not slots.** They are
reconciliation: the reaper recording for a call it never made,
and `stamp_post_terminal_usage` recording usage that arrived after the turn was
already terminal.

**The display-cache columns stay caller-side.** Each family writes its own
`job.input_tokens` / `job.output_tokens`. The slot stays a money object and does
not import five job ORM models — and the families disagree on column naming
(`tokens_in` on the event, `input_tokens` on the row) so there is no single
convention to encode. The existing AST rule "a function that stamps a token field
must record spend" keeps watching this.

Net: **18 of 20** record sites and **8 of 14** cap checks go through the slot.

## 6. Interface

Module location: **`src/call_manager/spend.py`** (top level). The current module
is `call_manager.proposal_assist.spend`. Two of the three new spenders
(`proposal_assist/phases/setup.py`, `proposal_assist/extractors/refresh.py`) are
inside `proposal_assist` and would be fine where it is — but `ranking/` is not,
and `ranking` importing a `proposal_assist` module inverts the dependency:
ranking is the older, independent subsystem and nothing in it currently imports
proposal_assist. That single caller is the whole reason to move the module.

The import-fence gate has pinned exemptions keyed by path, so moving the module
requires re-deriving those pins with `_fingerprints_for_file` rather than
hand-editing them (see `reference_call_monitor_gotchas`).

```python
class SpendCapReached(Exception):
    """The org ceiling was already reached. No hosted call was made."""


@dataclass
class Slot:
    attempt_id: uuid.UUID
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    sdk_session_id: str | None = None

    def report(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        model: str | None = None,
        sdk_session_id: str | None = None,
    ) -> None:
        """Record what the call actually consumed. Safe to call more than once;
        the last non-None value for each field wins.

        Keyword-only scalars, NOT a `TurnDoneEvent`. Four of the ten families
        (ranking, setup extraction, refresh extraction, citations) have no such
        object — they get a dict envelope back from `chat()`. A convenience
        `report_done(event)` for the six SDK families is a thin wrapper over
        this, not the primary interface."""


@contextmanager
def spend_slot(
    session_factory: sessionmaker,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    kind: SpendKind,
    ref_id: str | uuid.UUID | int | None = None,
) -> Iterator[Slot]:
    ...
```

`SpendKind` gains `"ranking"`, `"setup_extract"`, `"refresh_extract"`.
`SpendStatus` gains `"in_flight"` and `"denied"`. Both `CHECK` constraints on
`llm_spend` must be widened in the same migration.

### Denial

`__enter__` raises `SpendCapReached` after writing the `denied` row. It does not
try to fail the caller's job: it cannot know how to fail a coherence job versus
refuse a citation request versus terminalize a chat turn. Each caller catches it
and calls the terminalizer it already has:

```python
try:
    with spend_slot(...) as slot:
        ...
except SpendCapReached:
    _fail(s, job, "global daily token cap reached")
    s.commit()
    return
```

This keeps the current behaviour, including the `denied` ledger row, which the
tests merged in `1ce97197` pin.

## 7. Slices

Each slice merges independently with the suite green.

**Slice 1 — schema and the slot. Converts nothing.**
Migration: `attempt_id` + unique index, widened `kind`/`status` CHECKs.
Move `spend.py` to top level, re-derive the import-fence pins, rewrite both
append-only docstrings. Build `spend_slot`, `Slot`, `SpendCapReached` with unit
tests for all seven lifecycle rows in §3 — including a test that a `SIGKILL`
equivalent (process exits without `__exit__`) leaves an `in_flight` row.

**Slice 2 — the four pass runners.**
coherence, evaluator, uncited_claims, review. 12 record sites and 5 cap checks
collapse into 5 slots (review opens one per sub-call). Their tests already pin
success, failure, timeout, cancellation and cap-denial per family, so this slice
is largely proven by tests that already exist.

**Slice 3 — chat, compaction, citations; `outcomes.py` becomes settle.**
The hardest slice. `record_terminal_outcome` and `stamp_post_terminal_usage`
switch from appending to settling the `in_flight` row. The reaper, orphan sweep,
delete path and `web/api/turns.py` all route through here.

**Slice 4 — the three spenders currently outside the ledger.**
`ranking/rank_pair.py:197` (the hosted branch; `:141` is the local Ollama call
and gets no slot), `phases/setup.py`, `extractors/refresh.py`. All three share a
`chat()` protocol matched by *both* the hosted Anthropic client and the local
Ollama client, so the caller declares which it is — a local call opens no slot.
Today `ranking/` counts attempts but no tokens, and the other two record nothing
at all, so "every hosted call writes a row" is currently false app-wide.

**Slice 5 — flip the AST gate.**
New rule: a call to a hosted client method must be lexically inside a
`with spend_slot(...)`. This is a far simpler question than the current gate's
"which assignment forms stamp a token field", and it is the rule that actually
matches the guarantee. Keep the existing stamp rule for the display cache.

## 8. Testing

The rules this project already learned, restated because they are what makes the
difference between a gate and a decoration:

- **Every fix is proven by breaking it.** Disable the thing, name the test that
  goes red, confirm it fails for the stated reason.
- **Mutate by line number** when the target text is not unique.
- **A gate that fires on correct code gets disabled.** Every new rule needs a
  control test showing it stays green on the legitimate shape it most resembles.
- **A test that passes for the wrong reason is worse than no test.** The review
  cap test in `1ce97197` passed with the fix deleted, because wave-1 denied
  itself and consolidation was skipped for having nothing to consolidate. Assert
  the exact set of calls that reached the model, not just that some did.
- **Assertion messages must not contain the string the assertion greps for.** The
  chat cap test asserted `"global" in job.error` while its own failure message
  said "global"; the mutant stayed green.

Slice-specific:

- Slice 1 must include a test that the `attempt_id` unique constraint actually
  refuses a second row — executed against the database, not asserted from the
  model definition.
- Slice 3 must include a test where the worker never reaches `__exit__` and the
  reaper settles the `in_flight` row, asserting exactly one row for that
  `ref_id`.
- Slice 5's gate must be run against a file it should reject, and a file it must
  not.

## 9. Out of scope — the reconciliation check

Poll `/v1/organizations/usage_report/messages` (bucket `1d`, Stockholm-day
aligned) and compare against `sum_spend_today`. Alert when the gap exceeds a
threshold.

This is the only check that is not the application grading its own homework: the
ledger, the gate and the tests all verify what the code *did* record, and none of
them can see a hosted call nobody thought to record. That is precisely the blind
spot the original six violations lived in.

It is specified separately because it needs an Admin API credential
(`sk-ant-admin01-…`; workspace keys are rejected), which is a distinct
security decision, and because neither piece of work blocks the other.

## 10. Open risk

Slice 3 changes the exactly-once mechanism for chat turns, which is the most
concurrency-sensitive path in the application (the conditional `running→completed`
UPDATE is the turn's linearization point, and the reaper races it by design). The
`attempt_id` constraint makes double-recording impossible, but **losing** a record
is a different failure and the constraint does not prevent it. Slice 3 should get
its own outside review before merge, independent of the per-slice reviews.
