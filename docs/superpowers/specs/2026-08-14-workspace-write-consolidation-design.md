# `workspace_write` consolidation — design

Date: 2026-08-14
Status: approved (design), revision 1
Predecessor: Slice C2 (`docs/superpowers/plans/2026-08-13-workspace-retirement-slice-c2-mutex-writers.md`, merged `eef35ed`)

## 1. Why

Slice C2 put six proposal-assist write routes under the per-workspace commit
mutex. It got the design right — the composite lock graph is acyclic, the
linearization point is real — but it expressed that design as a six-step
ceremony **hand-written at six call sites**, and the ceremony was subtly wrong
at four of them before review caught it.

Measured on `eef35ed`:

| Repeated construct | Occurrences |
| --- | --- |
| `except CommitMutexTimeout` → `HTTPException(429, …, Retry-After: 2)` | 6, byte-identical |
| `except RowLockTimeout` → `HTTPException(429, …, Retry-After: 2)` | 6, byte-identical |
| `with bounded_lock_waits(session, timeout_seconds=…, message=…)` | 6 |
| `session.commit()` inside that block | 6 |

`update_settings` is 51 lines from the end of validation to `return`. Five of
them do the route's actual work. The rest is ceremony and the comments
explaining it.

The cost is not aesthetic. Every defect Slice C2's review wave found was a
defect *in a copy of the ceremony*, not in the design it implements:

- four of six routes wrapped the bound too narrowly to reach `session.commit()`,
  answering 500 where the slice contracts for 429
- two of six diffed against workspace state loaded before the mutex
- the fix for that then blinded the runtime gates protecting the first defect,
  requiring a static gate (gate 16) to re-arm the rule

An invariant enforced by six hand-assembled copies plus a test that watches them
is weaker than an invariant that cannot be expressed wrongly. This slice makes
it the second kind.

## 2. Scope

**In:** the six routes Slice C2 touched.

| Route | File | Line (`eef35ed`) |
| --- | --- | --- |
| `update_settings` | `src/call_manager/web/api/settings.py` | 80 |
| `add_member` | `src/call_manager/web/api/settings.py` | 158 |
| `remove_member` | `src/call_manager/web/api/settings.py` | 232 |
| `update_rubric` | `src/call_manager/web/api/rubric.py` | 156 |
| `retry_setup` | `src/call_manager/web/api/rubric.py` | 258 |
| `activate` | `src/call_manager/web/api/rubric.py` | 348 |

**Out, deliberately:**

- The other 21 `acquire_commit_mutex*` call sites (spec §10 residual 8 of the
  Slice C design). They share this ceremony *minus the bound*, so they are
  natural adopters — but adopting them is Slice C3 and is **not**
  behaviour-neutral: background callers that today wait indefinitely would begin
  raising, and none of C2's gates cover them. This slice must leave C3 easier,
  not perform it.
- Any change to a timeout value. `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS = 2` and
  `SETUP_JOB_LOCK_TIMEOUT_SECONDS = 2` keep their values and their derivations.
- Any change to authorization, validation, response bodies, or status codes.

**The binding constraint: this slice is behaviour-neutral**, with exactly one
documented exception (§6). Every status code, response body, header and timeout
stays identical. That is what makes Slice C2's runtime gates a true safety net
rather than something to be rewritten alongside the code they guard.

## 3. The unit

A single context manager, in a new module
`src/call_manager/proposal_assist/web/_writes.py` — sibling of `_perms.py`,
which is where the routes already get `require_owner`.

```python
@contextmanager
def workspace_write(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    timeout_seconds: float,
    what: str,
) -> Iterator[None]:
```

In order, it:

1. calls `acquire_commit_mutex_live(session, workspace_id)`, translating
   `CommitMutexTimeout` to `HTTPException(429, BUSY_ERROR_MESSAGE,
   headers={"Retry-After": "2"})`;
2. enters `bounded_lock_waits(session, timeout_seconds=timeout_seconds,
   message=f"timed out waiting for a row lock in the {what} transaction")`;
3. yields to the route body;
4. on a clean exit, calls `session.commit()` **inside** the bound;
5. translates the resulting `RowLockTimeout` to the same 429.

`WorkspaceDeleted` propagates untouched to `main.py`'s handler, which answers
404 — the documented re-delete/straddle contract. An `HTTPException` raised by
the body also propagates untouched and **nothing is committed**, which is what
preserves `retry_setup`'s 404/409 and `activate`'s 409.

### What it deliberately does not do

- **Authorization.** `require_owner` stays in the route, above the `with`. Slice
  C2 established that authorization precedes semantic validation which precedes
  any lock; folding authz into the manager would hide that ordering.
- **Validation.** Same reason. Note `add_member` and `remove_member` validate
  *inside* the block on purpose (their validation is a DB read that must not run
  before the mutex); that stays exactly as it is.
- **Row locks.** The route still writes its own `.with_for_update()`.
- **Freshness re-reads.** See §4 — this was in an earlier draft of this design
  and is removed for a concrete reason.

### Call-site shape

```python
ws = require_owner(workspace_id=workspace_id, user=user, session=session)
title = _validated_title(body.title)

with workspace_write(
    session, ws.id,
    timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
    what="settings-update",
):
    ws.title = title
    ws.status = body.status
    ws.deadline = deadline
    ws.citation_style = body.citation_style

return _settings_out(ws)
```

### The exact arguments per route

Behaviour-neutrality includes the bound's message, which every route builds
today as `"timed out waiting for a row lock in the {what} transaction"`. The
manager builds the same string from `what`, so these values are not free
choices — they reproduce the existing strings byte-for-byte:

| Route | `timeout_seconds` | `what` |
| --- | --- | --- |
| `update_settings` | `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` | `"settings-update"` |
| `add_member` | `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` | `"add-member"` |
| `remove_member` | `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` | `"remove-member"` |
| `update_rubric` | `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` | `"rubric-update"` |
| `retry_setup` | `SETUP_JOB_LOCK_TIMEOUT_SECONDS` | `"setup-retry"` |
| `activate` | `SETUP_JOB_LOCK_TIMEOUT_SECONDS` | `"activate"` |

`timeout_seconds` stays an explicit argument rather than being derived from a
`row="workspace" | "setup_job"` enum. Two reasons. The constant names *which row
family this transaction waits on first*, which is load-bearing information a
reader needs at the call site; and `activate` waits on **both** families
(`SETUP_JOB` then `workspaces`), so an enum naming one row would be a lie there.
The manager removes the ceremony, not the policy.

## 4. Why the manager does not own the freshness re-read

Two routes re-read the workspace under the mutex before diffing against it:
`update_rubric` (`rubric.py:240`) and `activate` (`rubric.py:415`). An earlier
draft gave `workspace_write` a `reread: bool = False` parameter that would run
`session.refresh(ws, with_for_update=True)` immediately after opening the bound.

**That would introduce a deadlock.** `activate`'s re-read sits *after* its
setup-job `SELECT ... FOR UPDATE`, deliberately: the normative order on this
branch is M → J → W and it matches `phases/setup.py`'s `_m3`, which writes the
rubric while holding job → workspace. Hoisting the re-read above the job SELECT
inverts against `_m3` and opens a real AB-BA cycle (Postgres 40P01). A parameter
whose implementation must run at the top of the block cannot express a step
whose correctness depends on being in the middle of it.

The re-reads therefore stay in the route bodies, with their comments, where
their position is visible. The distinction is also principled rather than
accidental: a route needs the re-read when it reads **attributes of the `ws`
instance loaded before the mutex**. `update_rubric` and `activate` diff against
`ws.rubric_json`; `add_member` queries membership fresh inside the transaction
and reads nothing cached, so it needs nothing.

## 5. What replaces gate 16

`tests/proposal_assist/test_bounded_region_encloses_commit.py` (gate 16) asserts
by AST walk that `session.commit()` sits lexically inside each route's
`with bounded_lock_waits(...)` block. After this slice neither construct appears
in the routes, so the gate must change or it silently stops covering anything.

It is replaced by a **stronger and simpler** invariant, in the same file, over
the same six-route table:

1. each of the six routes contains exactly one `with workspace_write(...)` block; and
2. no route function in `settings.py` or `rubric.py` calls `session.commit()` at all.

The old rule becomes unwriteable: a route cannot put its commit outside the
bound, because it does not write a commit. Rule 2 is what keeps that true — it
is the rule that fails if someone reintroduces a hand-rolled transaction.

Constraints carried over unchanged from gate 16, because they were earned:

- **AST only.** `ast.parse`, matching on parsed nodes. No regex, no `in`, no
  `str.index` over source text — this repository has shipped a source-text
  assertion that was a tautology.
- **Explicit expected set.** The six-route table is asserted, so a route that
  disappears or is renamed fails rather than being silently uncovered, and a new
  bounded writer in one of these two modules fails until it is listed.
- **Scope stated honestly.** The walk covers only these two modules. That limit
  is measured and written down, not assumed.
- **Proven per route.** Six mutations — remove the `workspace_write` wrap from
  each route in turn — each must fail that route's own case and no other, with
  the file's md5 confirmed changed before the run and restored after.

`retry_setup` keeps its special status and it should be restated in the new
gate's docstring: it has no reachable commit-time contention (its bound opens
with the job row's `.with_for_update()` and it writes no `WorkspaceEvent`), so
for that route the static gate is the **only** enforcement there is.

## 6. The one behaviour change, and how it is pinned

`add_member` returns early from inside its bounded block on two no-op paths:
the target is already the owner (`settings.py:201`) and the target is already a
member (`settings.py:209`). Today those paths exit the `with` without
committing. Under a commit-on-clean-exit manager they commit.

**This is accepted, not worked around.** Restructuring the route does not avoid
it: the lookups must stay inside the mutex (hoisting them is forbidden by that
route's own comment), so any shape still exits the block cleanly. The
alternatives are worse — a bespoke "nothing to write" signal for one route, or
sniffing `session.dirty`/`session.new` before committing, which is *dangerous*:
after an autoflush those sets are empty while the transaction holds real writes,
so that rule would silently discard data.

Committing on clean exit is also the contract that already exists in this stack:
`with session.begin():` in SQLAlchemy commits on clean exit, early `return`
included. `workspace_write` **is** a transaction scope, so behaving differently
would be the surprise. And the asymmetry favours it — a future partial write on
an early-return path gets committed (visible, and greppable in the audit trail)
rather than silently discarded behind a 200.

Both paths write nothing today, so the observable delta is an empty commit.
That is pinned by a new test asserting each of the two paths leaves **no
`WorkspaceEvent` row and no `WorkspaceViewer` row**. If someone later adds a
write above those returns, that test goes red and says so.

## 7. Testing

**The acceptance criterion is that Slice C2's runtime gates pass untouched.**
`tests/proposal_assist/test_api_writers_mutex_straddle.py` (28 tests) must not
be edited by this slice: the six wedged-mutex 429 gates, the four
held-workspace-row gates, the setup-job-row gates, and the two commit-time
users-FK gates all assert externally-visible behaviour, so they are exactly the
right net for a refactor that claims to change none of it. An edit to that file
during this slice is a signal that behaviour moved.

New tests:

1. **`workspace_write` itself**, DB-backed, direct rather than through a route:
   a wedged mutex raises `HTTPException` 429 with `Retry-After: 2` and
   `BUSY_ERROR_MESSAGE`; a held row does the same; a clean exit commits; an
   `HTTPException` from the body propagates and commits nothing; a workspace
   deleted under the mutex raises `WorkspaceDeleted`.
2. **The rewritten gate 16**, per §5, with its six mutations.
3. **The `add_member` no-op pin**, per §6.

Unchanged gates that must stay green: `test_row_lock_ceilings.py`,
`tests/test_api_settings.py`, `tests/test_api_rubric.py`, `ruff check`, and the
OpenAPI drift gate — no route docstring may change, since FastAPI publishes them
as operation descriptions and `frontend/src/api/schema.gen.ts` is a hard CI gate.

Full suite baseline on `eef35ed`: **3381 passed, 1 skipped**. It must end at
3381 plus the new tests, with no test removed.

## 8. Migration order

One route per commit, six commits, the suite green at each. The manager and its
own tests land first, in their own commit, before any route moves. The gate
rewrite lands with the **last** route — until then the old gate still guards the
routes that have not moved yet, and rewriting it first would leave the
un-migrated routes uncovered mid-slice.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| The gate rewrite is vacuous — it passes whatever the routes do | Six per-route mutations, md5-confirmed, each failing only its own case |
| `activate`'s M → J → W order is disturbed | The re-read stays in the body (§4); the straddle suite's lock-order gates are untouched and must pass |
| A route's early return commits a future partial write | §6's pin; the failure mode is a visible commit, not silent loss |
| The manager swallows an exception it should propagate | Explicit test that a body `HTTPException` propagates and commits nothing |
| A docstring edit drifts the OpenAPI schema | Drift gate run before merge; no route docstring is in scope |

## 10. Success criteria

1. All six routes use `workspace_write`; no route in the two modules calls
   `session.commit()`.
2. `test_api_writers_mutex_straddle.py` passes **unedited**.
3. The rewritten gate 16 fails under each of six per-route mutations.
4. Full suite ≥ 3381 passed with nothing removed; `ruff` clean; OpenAPI drift clean.
5. `update_settings`'s ceremony is gone: the route reads as validation, a `with`,
   four assignments, and a return.
