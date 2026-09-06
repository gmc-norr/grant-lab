# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four defects the 2026-09-06 outside review confirmed in the Plan C and freshness code.

**Architecture:** Four independent fixes. Two need a migration (owner-scoped profile-name uniqueness; QR columns on `board_items`), one is a row lock in a shared helper, one adds a key to a hash payload and bumps its version. No task depends on another. A fifth, Task 0, is a one-line docs fix that must land first because the base commit is RED without it.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-09-06-review-fixes-design.md`. The spec is INPUT. If it is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit.

> **Revision 2, 2026-09-06.** A full dry run executed every task of revision 1 in a throwaway
> worktree and found **21 defects in this plan** — 6 Critical, 6 High, 5 Medium, 4 Low. The
> most serious: Task 1's load-bearing gate passed *before* the fix existed (a tautology), Task 2
> as written broke the nightly ranking reconcile with an `AttributeError` in 13 tests, and the
> stated baseline was measured on a tree two commits old. Every fixture name, every mutation
> expectation and every code block below has now been **run**. Where a number appears, it was
> measured; where something is still unverified, it says so in those words.
> Ledger: `/home/parlar_ai/review-fixes-run/PLAN-DEFECTS.md`.

## Global Constraints

- **Base:** `master` at **`a8e31f98`**. Branch `fix/review-2026-09-06`, worktree `.worktrees/review-fixes`.
  (Revision 1 said `9432d83e`. `a8e31f98` is that commit plus one docs-only commit adding this
  plan file — 730 insertions, nothing else. Use `a8e31f98`.)
- **Measured baseline: `1 failed, 5411 passed, 1 skipped`. The base commit is RED.**
  Measured with `uv run pytest -n 16 --dist loadfile` on a worktree at `a8e31f98`, in
  148.53s. The failure is
  `tests/architecture/test_investigations_index.py::test_every_investigation_is_indexed`,
  and it is **pre-existing and deterministic** (`1 failed, 2 passed in 1.26s` run alone).
  Cause: commit `03fad6db` added `docs/superpowers/reviews/2026-09-06-plan-c-and-freshness-code-review.md`
  without adding its line to `docs/superpowers/INVESTIGATIONS.md`, whose completeness that test
  pins. **Task 0 closes it, and must land before any other task** — otherwise Task 3 Step 6 and
  Task 4 Step 7, which both run `tests/architecture`, produce a red that looks like a broken
  architecture gate and is not.
- **Expected end state: `5424 passed, 1 skipped`.** That is baseline `5411` + `1` (the Task 0
  fix moves the failing test to passing) + **`12` new tests**: 2 in Task 1, 3 in Task 2
  (two fingerprint gates + the Row-parity gate), 3 in Task 3, 4 in Task 4. The dry run measured
  `5421 passed` with **+10** — it exercised Task 2's two fingerprint gates directly in Python
  rather than as pytest cases, so the real build lands two more than it did.
  **Re-measure the base yourself, carry the +12 delta against what you measured, and never
  adjust a number to make the arithmetic close.** If your total differs, name the term that changed.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic call ran — stop and record it. Never export `ANTHROPIC_API_KEY`.
- **Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** Set any env var inline in the one command that needs it.
- **`docs/superpowers/specs/` byte-identical at every commit.**
- **Migrations are `0076` then `0077`, in that order.** Head is `0075`; verified by listing
  `alembic/versions/`. Task 3 owns `0076`, Task 4 owns `0077`. **Build Task 3 before Task 4** or
  the revision chain breaks. Both migrations were round-tripped on a real Postgres 16 during the
  dry run (`upgrade head` → `downgrade -1` → `upgrade head`, all exit 0).
- **`docs/ARCHITECTURE.md` is generated.** `uv run python scripts/gen_architecture.py` in the same commit as any model change. Never hand-edit.
- **Gates:** `uv run pytest -n 16 --dist loadfile` (never `-n auto` — `psutil` is absent so xdist resolves it to **72**; never `--dist loadgroup`); `uv run ruff check src tests alembic scripts`. Never `ruff format` a directory.
- **False-pass signature:** a broken import shows as mass teardown ERRORs with zero FAILED. **Judge every mutation on FAILED, never on ERROR.** A `no tests ran` result is also not a pass.
- **Flake signature:** testcontainers Postgres occasionally drops under load and surfaces as `connection refused`. Re-run the affected file alone before recording it as a defect.

## Fixtures — the verified list. Do not re-derive this.

Revision 1 told you to "check these yourself" in three places. The dry run did, and **eleven of
the twelve fixture names it named do not exist.** This is the answer; use it.

`tests/conftest.py` is the **only** conftest in scope for a file under `tests/`. Its complete
fixture list at `a8e31f98`:

```
_require_explicit_event_loops, _isolate_golden_set_files, _isolate_settings_from_dotenv,
test_client_factory, _test_workspaces_root, _workspaces_dir_is_never_the_real_one,
pg_container, pg_url, db_engine, migrated_engine, pg_session_factory, profile_factory,
_close_helper_entered_lifespans
```

| Name revision 1 used | Reality |
|---|---|
| `session_factory` | **absent.** Real name is `pg_session_factory` (`tests/conftest.py:151`), a **factory**: `with pg_session_factory() as s:` |
| `session` | **absent.** Wrap `pg_session_factory` in a module-local fixture, or use it directly. |
| `client`, `client_unauth` | **absent.** `client_unauth` exists at `tests/proposal_assist/conftest.py:129`, package-scoped to `tests/proposal_assist/` and invisible under `tests/`. |
| `auth_headers`, `auth_headers_for` | **absent — and the mechanism is different.** This app does not authenticate API requests with a header at all. |
| `make_user`, `make_profile`, `make_call`, `board`, `publish`, `owner_profile` | **absent** under `tests/`. |
| `profile_factory` | **exists** (`tests/conftest.py:184`). Signature `profile_factory(*, name=, kind=, level=, description=, structured=) -> (profile_id, user_id)`. |

**Authentication is a signed session cookie, not a header.** The idiom, from
`tests/test_api_profile_transfer.py:41-45` and `tests/test_api_profiles_create.py:17-27`:

```python
signer: SessionSigner = app.state.session_signer
client = TestClient(app)
client.cookies.set("session", signer.make(user_id=uid))
```

A search-and-replace of `auth_headers` for some other fixture name cannot fix this — there is no
header to pass. Every `headers=...` kwarg in revision 1's test code is deleted below.

**The repo already says so.** `tests/test_api_profile_transfer.py:9-11` — the module revision 1
pointed at — opens with a docstring stating it in as many words:

> "FIXTURES -- this repo has NO `db_session`, `client` or `client_unauth`. The session fixture
> is `pg_session_factory` (tests/conftest.py:151), a FACTORY used as `with pg_session_factory() as s:`."

**The idiom is module-local helpers, not fixtures.** Every test file below defines its own
`_setup(monkeypatch, pg_url) -> app`, `_client_for(app, uid)`, `_user(session, email)` and
`_profile(...)`, and takes `(monkeypatch, pg_url, pg_session_factory)`. The code blocks in this
plan are the **verified** versions — they were run.

## Release timing — read before deferring Task 2

Task 2 bumps `FINGERPRINT_VERSION`, which invalidates every stored fingerprint and forces one full re-embed and re-rank. **Migration `0075` already forces exactly that on the first night after it deploys**, because it deliberately leaves `profiles.embedding_fingerprint` NULL.

So Task 2 is **free if it ships before the first production deploy** and a second unplanned corpus re-rank if it ships after. That is a release-sequencing fact, not a reason to reorder the tasks.

*(Revision 1 said "Task 3" in this section's heading and body. The task that bumps
`FINGERPRINT_VERSION` is Task 2. Corrected.)*

## File structure

| File | Responsibility | Task |
|---|---|---|
| `docs/superpowers/INVESTIGATIONS.md` | index the 2026-09-06 review — closes the red base | 0 |
| `src/grant_lab/web/api/profiles.py` | `FOR UPDATE` in `_profile_for_edit`; scoped name pre-check | 1, 3 |
| `src/grant_lab/profiles/fingerprint.py` | `embedding_model` in the payload, `v2` | 2 |
| `src/grant_lab/ranking/queue.py` | the narrowed SELECT must carry `embedding_model` | 2 |
| `src/grant_lab/db/models/ranking.py` | the `"v1:"` comment on `profile_fingerprint` | 2 |
| `src/grant_lab/db/models/profiles.py` | the `"v1:"` comment (Task 2); the index (Task 3) | 2, 3 |
| `alembic/versions/0076_owner_scoped_profile_names.py` | drop the global index, add the owner-scoped one | 3 |
| `alembic/versions/0077_board_item_qr.py` | three QR columns + backfill | 4 |
| `src/grant_lab/db/models/boards.py` | `BoardItem` QR columns, and its docstring | 4 |
| `src/grant_lab/web/api/boards.py` | render and store at approval | 4 |
| `src/grant_lab/boards/public.py` | return the stored QR | 4 |
| `src/grant_lab/web/api/public_boards.py` | read the columns, encode nothing | 4 |
| `tests/web/test_api_public_board.py` | `_approve()` bypasses the API this task changes | 4 |

---

### Task 0: the base commit is red — index the review

**Files:**
- Modify: `docs/superpowers/INVESTIGATIONS.md`

**Why this is a task and not a footnote.** `tests/architecture/test_investigations_index.py`
pins that every file in `docs/superpowers/{notes,reviews}/` is linked from
`INVESTIGATIONS.md`. Commit `03fad6db` added this plan's own review of record and did not add
its line, so **`a8e31f98` fails one test before anyone touches it**. Tasks 3 and 4 both run
`tests/architecture` explicitly and both expect PASS. Leaving this red puts a diagnostic trap
directly on the plan's path: the obvious reading — "my model change broke an architecture gate"
— would be wrong.

This is a docs file and NOT under `docs/superpowers/specs/`, so it crosses no line. It is
outside every other task's declared file list, so it gets its own commit.

- [ ] **Step 1: Confirm the red, so you are fixing the thing you think you are**

Run: `uv run pytest tests/architecture/test_investigations_index.py -v`
Expected (measured): `1 failed, 2 passed`, with
`AssertionError: these investigations are not listed ... ['reviews/2026-09-06-plan-c-and-freshness-code-review.md']`

- [ ] **Step 2: Add the entry**

Append to the reviews section of `docs/superpowers/INVESTIGATIONS.md`, matching the format of
the entries around `:107` and `:120` — a bold markdown link, then an *italicised question* the
review answers, then what it found. The italic matters: `test_the_index_is_not_merely_a_file_listing`
counts `*` characters against the number of files on disk.

```markdown
**[reviews/2026-09-06-plan-c-and-freshness-code-review.md](reviews/2026-09-06-plan-c-and-freshness-code-review.md)**
*Does the Plan C board code and the matching-freshness release hold up to an outside reader?*
**4 of 5 confirmed.** The first outside look at the Plan C code, by `gpt-6-astra` via GitHub
Copilot against a 251,751-char package at `6f8ab1ee` — the 2026-09-05 architecture review
stopped at `4e6212e2` and predates it entirely. Confirmed: anonymous `GET /b/<slug>` can force
the cold QR path repeatedly through a global per-URL encode lock; two concurrent owner transfers
both authorize against a stale owner; profile creation is an existence oracle for private
profile NAMES via its 409; and an embedding-model swap leaves every ranking marked current
while its stored cosine describes the old vectors. The fifth was a defect in the review package,
not the code. Fixed by `docs/superpowers/plans/2026-09-06-review-fixes.md`.
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/architecture -v
```
Expected: **all PASS.** This is the first point in the plan at which any "Expected: PASS" on
`tests/architecture` is achievable.

```bash
git add docs/superpowers/INVESTIGATIONS.md
git commit -m "docs(investigations): index the 2026-09-06 Plan C and freshness review"
```

From here the baseline is `5412 passed, 1 skipped`.

---

### Task 1: F2 — the transfer race

**Files:**
- Modify: `src/grant_lab/web/api/profiles.py:136` (`_profile_for_edit`)
- Test: `tests/test_api_profile_transfer_concurrency.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_profile_for_edit` returns a row locked `FOR UPDATE` for the life of the caller's transaction.

**Blast radius: SEVEN callers across TWO modules, not five.** Revision 1 said "its five callers".
Measured at `a8e31f98`:

| Site | What it is |
|---|---|
| `profiles.py:598`, `:632`, `:694`, `:739` | the four sharing operations |
| `profiles.py:794` | transfer |
| `boards.py:162` | inside `_board_for_edit`, which **10 board routes** call |
| `boards.py:235` | `create_board` |

So every board write also takes this lock, and board writes on the **same profile** now
serialise. That is intended — a board's edit permission *is* its profile's edit permission
(`boards.py:155`) — but it is a behaviour change revision 1 did not name. The dry run
implemented this lock and ran the full suite: **no board test broke**.

**Why the helper and not the handler.** Patching only the transfer route leaves `add_profile_viewer` racing it. All five profile callers mutate sharing state on the same row, so the lock belongs where they all pass.

- [ ] **Step 1: Write the failing test**

Two real sessions and two real transactions. A fixture that shares one session proves nothing — SQLAlchemy would serve the second read from the identity map and no lock would be exercised.

This block was **run** — 5 times in each implementation state, no flake in either direction.
Write it as printed.

```python
"""Two concurrent sharing mutations on one profile row.

FIXTURES: this repo has no `client`, `session_factory`, `owner_profile`,
`auth_headers` or `make_user`. Auth is a signed session COOKIE, not a header.
Module-local helpers, in the idiom of tests/test_api_profile_transfer.py:32-66.
"""

import threading

from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.main import create_app

# Hard bound on every join AND on the barrier. pyproject.toml's 120s
# pytest-timeout uses method=signal, which interrupts only the MAIN thread --
# a worker wedged inside a blocked `SELECT ... FOR UPDATE` is not interrupted
# by it and can hold an xdist worker's Postgres connection open. A bounded
# join turns a future regression into a legible failure instead of a stall.
JOIN_TIMEOUT = 30


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return c


def _user(session, email):
    u = User(email=email, name=email, status="active")
    session.add(u)
    session.flush()
    return u


def _profile(session, owner):
    p = Profile(name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="", structured={}, created_by=owner.id, visibility="private")
    session.add(p)
    session.flush()
    return p


def _run_two(fns):
    """Fire both under a barrier, joining with a HARD TIMEOUT.

    The barrier needs its own timeout as well as the joins: a bare
    threading.Barrier(2) blocks FOREVER if one thread dies before reaching it,
    which is the realistic way this harness hangs -- an exception in setup, not
    in the lock. (The row lock itself is not a deadlock hazard here: the
    barrier sits BEFORE the request, so no thread ever waits on it while
    holding or requesting the lock. Measured across 12 runs, longest 7.0s.)
    """
    barrier = threading.Barrier(2, timeout=JOIN_TIMEOUT)
    errors = []

    def wrap(fn):
        def go():
            try:
                barrier.wait()
                fn()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        return go

    ts = [threading.Thread(target=wrap(f)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=JOIN_TIMEOUT)
    return [t.is_alive() for t in ts], errors


def test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner(
    monkeypatch, pg_url, pg_session_factory
):
    """Exactly ONE transfer may succeed. Without FOR UPDATE both readers load
    owner A, both pass the authorization check, and both commit -- the profile
    silently moves twice and each response reports outgoing_lost_read for an
    owner that no longer holds it.

    COUNT the successes. Do NOT assert that some 200 response named the final
    owner: the last thread to commit writes its own target and returns that
    same value in its own body, so such a winner exists BY CONSTRUCTION whether
    or not the lock is there. That assertion is a tautology about
    last-writer-wins, and it passed the unfixed tree.

    The loser's status is 404, not 403, and that is correct: under READ
    COMMITTED the blocked SELECT ... FOR UPDATE re-reads the newly committed
    row, so the loser sees a private profile it no longer owns, and
    `_profile_for_edit` checks can_read_profile BEFORE ownership
    (profiles.py:150-153). "Reading stays WIDER than editing"
    (profiles.py:144-145). Do not "correct" this to 403.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner@umu.se")
        b = _user(s, "b@umu.se")
        c = _user(s, "c@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, oid, b_email, c_email = p.id, owner.id, b.email, c.email

    results = []
    client = _client_for(app, oid)

    def transfer(email):
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": email})
        results.append((email, r.status_code, r.json() if r.status_code == 200 else None))

    alive, errors = _run_two([lambda: transfer(b_email), lambda: transfer(c_email)])
    assert not any(alive), f"HUNG: threads still alive after {JOIN_TIMEOUT}s"
    assert not errors, errors

    with pg_session_factory() as s:
        final = s.get(Profile, pid).created_by

    ok = [r for r in results if r[1] == 200]
    assert len(ok) == 1, (
        f"both transfers authorized against owner {oid}: "
        f"statuses={[r[1] for r in results]}, final={final}"
    )


def test_a_transfer_and_a_viewer_add_do_not_error(monkeypatch, pg_url, pg_session_factory):
    """A no-500, no-hang smoke check across two DIFFERENT endpoints that share
    _profile_for_edit.

    It is NOT the control proving the lock lives in the helper. Its only
    observable is a raised exception, and a 403 or 404 is a RESPONSE, not an
    exception -- so it stays green under an implementation that locked only the
    transfer handler, which is exactly the mutant such a control would exist to
    catch. Measured identical in both states: codes=[('viewer', 200),
    ('transfer', 200)]. Step 5's grep is the real structural control.

    Do NOT "strengthen" this to `len(ok) == 1`: the winner is genuinely
    order-nondeterministic between two different endpoints, so that assertion
    would be flaky.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner2@umu.se")
        b = _user(s, "b2@umu.se")
        v = _user(s, "v2@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, oid, b_email, v_email = p.id, owner.id, b.email, v.email

    client = _client_for(app, oid)
    codes = []
    alive, errors = _run_two([
        lambda: codes.append(("transfer", client.put(
            f"/api/profiles/{pid}/owner", json={"email": b_email}).status_code)),
        lambda: codes.append(("viewer", client.post(
            f"/api/profiles/{pid}/viewers", json={"email": v_email}).status_code)),
    ])
    assert not any(alive), f"HUNG: threads still alive after {JOIN_TIMEOUT}s"
    assert not errors, f"a concurrent sharing mutation raised: {errors}"
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py -v`

Expected: **`test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner` FAILS
with `statuses=[200, 200]`** — both transfers authorized against the same owner, and the
profile moved twice. The second test PASSES; it passes in both states by design (see its
docstring).

Measured on the unmodified base tree, 5 runs, no flake:
```
results=[('b@umu.se', 200, {'owner': {'user_id': 2, ...}, 'outgoing_lost_read': True}),
         ('c@umu.se', 200, {'owner': {'user_id': 3, ...}, 'outgoing_lost_read': True})]
n_ok=2 statuses=[200, 200]
```

If it ERRORs on a missing fixture instead, that is the false-pass signature, not a pass.

- [ ] **Step 3: Implement**

In `_profile_for_edit`, load the row `FOR UPDATE`. Read the existing body first and keep its 404/403 logic exactly; only the load changes:

```python
    # FOR UPDATE, not a bare get. All SEVEN callers of this helper (the four
    # sharing operations and transfer here, plus boards.py's _board_for_edit
    # and create_board) mutate state gated on this row, and without the lock
    # two of them can both read owner A, both pass the authorization check
    # below, and the second then computes its effects for an owner that no
    # longer holds the profile.
    #
    # The lock lives here rather than in the transfer handler on purpose:
    # locking one caller would leave the other six racing it.
    p = session.execute(
        select(Profile).where(Profile.id == profile_id).with_for_update()
    ).scalar_one_or_none()
```

`select` **is** already imported in this module (verified at `a8e31f98`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py tests/test_api_profile_transfer.py tests/web/test_api_boards_crud.py tests/web/test_api_board_items.py -v`

Expected: PASS, including the pre-existing transfer tests **and the board routes**, which now
take this lock through `_board_for_edit`. Measured in the dry run: no board test broke.

- [ ] **Step 5: Confirm the lock ordering — this is Task 1's real structural control**

The lock now composes with a second one. Grep **both** modules, not just `profiles.py`:

```bash
grep -n 'with_for_update' src/grant_lab/web/api/profiles.py src/grant_lab/web/api/boards.py
```

Expected, measured, exactly **two** hits:
- `profiles.py` — one, in `_profile_for_edit` (the change you just made).
- `boards.py:180` — `session.refresh(board, with_for_update=True)` inside `_lock_board_row`,
  which pre-dates this task.

**The resulting order is `profiles` → `boards`, and it is consistent.** `_lock_board_row` has
exactly two call sites (`boards.py:345` in `update_board`, `:636` in `approve_items`), and both
run `_board_for_edit` — hence `_profile_for_edit` — immediately before. Nothing takes the boards
lock first and the profiles lock second, so there is no ABBA cycle. A **third** hit, or a
`_lock_board_row` call that is not preceded by `_board_for_edit`, means that claim needs
re-checking before this ships.

*(Revision 1's version of this step grepped only `profiles.py`, expected "exactly one hit", and
would have reported success while missing `boards.py:180` entirely — the one second lock that
actually exists.)*

- [ ] **Step 6: Prove the gate bites**

Revert the change to a plain `session.get(Profile, profile_id)`.
Run: `uv run pytest tests/test_api_profile_transfer_concurrency.py -v`

Expected: `test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner` **FAILS**
with `both transfers authorized against owner 1: statuses=[200, 200]`.
Measured: 5 mutant runs, 5 failures; 5 fixed runs, 5 passes with `statuses=[200, 404]`.

**Restore** and re-run.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/web/api/profiles.py tests/test_api_profile_transfer_concurrency.py
git commit -m "fix(profiles): sharing mutations serialize on the profile row"
```

---

### Task 2: F4 — an embedding-model swap must invalidate rankings

**Files:**
- Modify: `src/grant_lab/profiles/fingerprint.py:23` and `:110-118`
- Modify: **`src/grant_lab/ranking/queue.py`** (the narrowed SELECT and its "Verified:" comment)
- Modify: `src/grant_lab/db/models/ranking.py:159` and `src/grant_lab/db/models/profiles.py:115` (the `"v1:"` comments)
- Modify: `tests/test_ranking_worker_fingerprint.py:54` (the one real `v1:` assertion)
- Test: `tests/test_profile_fingerprint.py` (extend), `tests/test_ranking_queue_row_parity.py` (new)

**Interfaces:**
- Consumes: `Profile.embedding_model`.
- Produces: `FINGERPRINT_VERSION = "v2"`; `rank_fingerprint`'s payload gains `"embedding_model"`. Every fingerprint string is now `v2:`-prefixed.

> **CRITICAL — revision 1 broke the nightly reconcile.** `rank_fingerprint` is **not** always
> called with a full ORM `Profile`. `src/grant_lab/ranking/queue.py:168-178` builds a
> **column-limited SELECT** — seven columns, deliberately excluding the 1024-dim vector — and
> calls `rank_fingerprint` on the resulting SQLAlchemy `Row` (`queue.py:192`). A `Row` raises
> `AttributeError` for a column it does not carry.
>
> Measured: applying revision 1's Step 3 verbatim and running the ranking tests gave
> **13 failures**, 12 of them `AttributeError: embedding_model`, across
> `tests/test_ranking_queue.py`, `tests/test_ranking_reconcile_staleness.py` and
> `tests/test_ranking_worker_fingerprint.py`. `reconcile_missing_rankings` is the daily
> reconcile tick, so in production every active profile would raise and the scheduler's ranking
> enqueue would die silently in a background job at 03:30.
>
> Step 3b below is the fix, and Step 1's third gate is what stops the next person re-breaking it.

**What this costs.** The version bump invalidates every stored fingerprint, forcing one full re-embed and re-rank. Migration `0075` already forces that on its first night, so this is free before the first deploy — see "Release timing" above.

**What the new key does and does not buy** — measured, so nobody re-derives it:

| Question | Measured |
|---|---|
| `embedding_model=None` vs `""` → same digest? | **Same.** `_canonical` (`fingerprint.py:46`) drops any key whose value is `None`/`""`/`[]`/`{}`, so an unset model contributes nothing. That is correct: an absent model is not a distinguishable model. Benign in practice — `ck_profiles_embedding_model_paired` (`db/models/profiles.py:61`) enforces `(embedding IS NULL) = (embedding_model IS NULL)`, so `""` cannot arise from the normal write path. |
| `None` vs `"bge-m3"` → different? | **Different.** The first-deploy NULL→named transition does invalidate. |
| `"mxbai-embed-large"` vs `"bge-m3"` → different? | **Different.** The gate that matters works. |
| `v1` vs `v2` of the same profile → different? | **Different, but only in the prefix.** `FINGERPRINT_VERSION` is prepended in `_digest` and never mixed into the hashed blob, so the sha256 half is byte-identical. |

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_profile_fingerprint.py`:

```python
def test_a_model_swap_changes_the_rank_fingerprint():
    """The embedding repair predicate knows about model identity; the ranking
    one inherited only a text hash. So swapping EMBEDDING_MODEL replaced every
    vector, left both fingerprints byte-identical, and reconcile treated every
    ranking as current while its stored cosine described the old vectors.

    This repo has already swapped embedding models once (mxbai -> bge-m3)."""
    a = _profile()
    # An OPAQUE sentinel. Deliberately not "v1:"/"v2:"-prefixed: its content is
    # irrelevant here, and a version-shaped literal would contradict Step 4's
    # rule against hardcoding the version two steps later.
    a.embedding_fingerprint = "sentinel-same"
    a.embedding_model = "mxbai-embed-large"
    b = _profile()
    b.embedding_fingerprint = "sentinel-same"
    b.embedding_model = "bge-m3"
    assert rank_fingerprint(a) != rank_fingerprint(b)


def test_the_version_tag_is_v2():
    p = _profile()
    assert embedding_fingerprint(p).startswith("v2:")
    assert rank_fingerprint(p).startswith("v2:")
```

And create `tests/test_ranking_queue_row_parity.py` — **the gate revision 1 was missing**. It is
the only one that catches the `queue.py` break *as itself* rather than as collateral damage in
seven unrelated reconcile tests, and it protects the next person who adds a key to
`rank_fingerprint`, which the version tag exists to make routine:

```python
"""The gate for the invariant `ranking/queue.py:163-167` states in a comment and
nothing executes: the narrowed Row must produce a byte-identical rank_fingerprint
to a loaded Profile.

It reads the column list out of reconcile_missing_rankings' own source, so it
cannot drift from the code it guards.
"""

import inspect
import re

import pytest
from sqlalchemy import select

from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking import queue as Q


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


def test_the_narrowed_row_and_the_loaded_profile_agree(session):
    # created_by is NOT NULL -- a bare created_by=None dies at commit() with an
    # IntegrityError, so the User is required.
    u = User(email="rg@umu.se", name="rg", status="active")
    session.add(u)
    session.flush()
    p = Profile(name="Hepatology", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="Liver disease", structured={"areas": ["liver"]},
                budget_currency="SEK", created_by=u.id, visibility="private")
    session.add(p)
    session.commit()

    loaded = session.get(Profile, p.id)
    # The function is reconcile_missing_rankings (queue.py:113), NOT enqueue_reconcile.
    src = inspect.getsource(Q.reconcile_missing_rankings)
    block = src.split("profiles = session.execute(", 1)[1].split(").where(Profile.is_active", 1)[0]
    cols = re.findall(r"Profile\.(\w+)", block)
    assert cols, "could not read queue.py's column list"
    row = session.execute(
        select(*[getattr(Profile, c) for c in cols]).where(Profile.id == p.id)
    ).one()
    assert rank_fingerprint(row) == rank_fingerprint(loaded), (
        f"queue.py's narrowed SELECT {cols} does not carry every column "
        f"rank_fingerprint reads"
    )
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_profile_fingerprint.py tests/test_ranking_queue_row_parity.py -v`
Expected: the two fingerprint tests FAIL — the model swap gives equal hashes, and the prefix is
`v1:`. The Row-parity test **PASSES** at this point: it is a regression gate for the change you
are about to make, and it only bites once `embedding_model` is in the payload. Its red comes in
Step 5's third mutation.

- [ ] **Step 3: Implement in `fingerprint.py`**

```python
FINGERPRINT_VERSION = "v2"
```

and in `rank_fingerprint`'s payload, after `"embedding_fingerprint"`:

```python
            "embedding_fingerprint": profile.embedding_fingerprint,
            # The MODEL as well as the fingerprint. embedding_fingerprint
            # digests build_profile_text's output, which is text only, so
            # swapping EMBEDDING_MODEL replaces every vector while leaving that
            # hash byte-identical -- and reconcile would then treat every
            # ranking as current while its stored embed_similarity describes
            # the previous vectors. The embedding repair predicate already
            # knows about model identity; this is what carries it into ranking
            # freshness.
            #
            # Every caller must supply this column. ranking/queue.py passes a
            # column-limited Row, not a Profile; direct attribute access here is
            # deliberate so a missing column fails loudly at the boundary
            # instead of silently hashing None. tests/test_ranking_queue_row_parity.py
            # is the gate.
            "embedding_model": profile.embedding_model,
```

Update the module docstring's version note: adding this key is exactly the deliberate, visible payload change the version tag exists to force.

- [ ] **Step 3b: Widen the narrowed SELECT in `ranking/queue.py` — DO NOT SKIP**

Add one column to the deliberate list at `queue.py:168-178`. **Add the column; do not replace
the list with a wholesale `select(Profile)`** — the narrowing exists to avoid pulling the
1024-dim vector, and `queue.py:159-162` records that an earlier revision of a plan reintroduced
the wholesale load here three tasks later.

```python
            Profile.embedding_fingerprint,
            Profile.embedding_model,
```

Then amend the "Verified:" comment at `queue.py:163-167` so it stays true and names its gate:

```python
    # Verified: the Rows produce byte-identical rank_fingerprints to loaded
    # Profile objects (`kind` is String + CHECK, not a native enum, so both
    # sides give the same str through `getattr(x, "value", x)`), and the
    # emitted SQL carries no `profiles.embedding AS` column. That claim is no
    # longer only a comment: tests/test_ranking_queue_row_parity.py reads this
    # column list out of this function's source and executes it. Adding a key
    # to rank_fingerprint's payload without adding the column here reddens it.
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_profile_fingerprint.py tests/test_ranking_reconcile_staleness.py \
              tests/test_embeddings_refresh_fingerprint.py tests/test_ranking_queue.py \
              tests/test_ranking_worker_fingerprint.py tests/test_ranking_queue_row_parity.py -v
```

Expected: PASS. Measured with the fix in place: `44 passed in 8.93s` over the first five of
those files.

*(Revision 1's command omitted `tests/test_ranking_queue.py` — 7 of the 13 failures — and
`tests/test_ranking_worker_fingerprint.py`, which holds the only real `v1:` assertion. It also
attributed any red at this step to "a literal `v1:` string", which points at the wrong cause
entirely.)*

**There is exactly ONE hardcoded `v1:` assertion in the whole suite**, grepped:

```
tests/test_ranking_worker_fingerprint.py:54:    assert cr.profile_fingerprint.startswith("v1:")
```

Change it to reference the constant, not a hardcoded `v2:`:

```python
from grant_lab.profiles.fingerprint import FINGERPRINT_VERSION, rank_fingerprint
...
    assert cr.profile_fingerprint.startswith(f"{FINGERPRINT_VERSION}:")
```

**Every other `"v1:"` in `tests/` is an opaque sentinel** — an arbitrary stored value whose
content is irrelevant (`"v1:something_else_entirely"`, `"v1:deliberately_not_the_current_text"`,
`"v1:aaa"`). **Leave all nine alone.** "Update every literal `v1:`" as a blanket instruction
churns lines that are not about the version at all.

Two **source comments** also state the stored format as `"v1:"` and are the comments a future
reader trusts about the column. Update both:
- `src/grant_lab/db/models/ranking.py:159` — "…: "v1:" + sha256 of the canonical…"
- `src/grant_lab/db/models/profiles.py:115` — "What `embedding` describes: "v1:" + sha256…"

(`src/grant_lab/profiles/fingerprint.py:8` is covered by Step 3's docstring update.)

- [ ] **Step 5: Prove all three gates bite**

Run each mutation, restoring between. Judge on **FAILED**, never on ERROR.

| Mutation | Must redden | Measured |
|---|---|---|
| remove `"embedding_model"` from `rank_fingerprint`'s payload | `test_a_model_swap_changes_the_rank_fingerprint` | ✅ confirmed |
| set `FINGERPRINT_VERSION = "v1"` | `test_the_version_tag_is_v2` | ✅ confirmed |
| drop `Profile.embedding_model,` back out of `queue.py`'s SELECT | `test_the_narrowed_row_and_the_loaded_profile_agree` | ✅ confirmed — `KeyError: 'embedding_model'` / `AttributeError: embedding_model`, `1 failed in 4.56s`; restoring gives `1 passed in 4.53s` |

**Restore all three** and re-run.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/grant_lab/profiles/fingerprint.py src/grant_lab/ranking/queue.py \
        src/grant_lab/db/models/ranking.py src/grant_lab/db/models/profiles.py \
        tests/test_profile_fingerprint.py tests/test_ranking_queue_row_parity.py \
        tests/test_ranking_worker_fingerprint.py
git commit -m "fix(freshness): a model swap invalidates rankings, not just vectors"
```

`src/grant_lab/db/models/profiles.py` here is a **comment-only** edit (`:115`). It does not
change the schema, so `gen_architecture.py` is not required for this task — but if
`docs/ARCHITECTURE.md` moves when you regenerate in Task 3, that is why.

---

### Task 3: F3 — profile names stop confirming what a caller cannot read

**Files:**
- Create: `alembic/versions/0076_owner_scoped_profile_names.py`
- Modify: `src/grant_lab/db/models/profiles.py:52-57` (the index — the `Index(` call opens at `:52` and its `),` closes at `:57`)
- Modify: `src/grant_lab/web/api/profiles.py:365-369` (the pre-check)
- Modify: `docs/ARCHITECTURE.md` (generated)
- Test: `tests/test_api_profile_name_privacy.py`, `tests/test_migration_0076_owner_scoped_profile_names.py`

**Interfaces:**
- Consumes: nothing.
- Produces: index `uq_profiles_active_owner_name` on `(created_by, name)` where `is_active = true`. `uq_profiles_active_name` is gone.

**Scoping the query alone changes NOTHING an attacker can observe.** Revision 1 said the leak
"would move from a 409 to a 500". Measured — scoped pre-check with the index left global:

```
gate8 status=409 body={"error":{"code":"conflict","message":"profile name already exists"}}
```

**409, not 500.** `src/grant_lab/web/api/profiles.py:395-396` wraps the create in a blanket
converter:

```python
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e
```

so the INSERT's `IntegrityError` becomes **byte-identical** to the pre-check's own refusal and
the existence oracle survives intact. **The index is the entire fix; the pre-check is scoped so
that it and the schema say the same thing.** This matters: a builder who read revision 1's
rationale, ran short of time and shipped "just the handler scoping" as a partial improvement
would ship a change with **zero** security effect while believing it had traded a 409 for a 500.

`profiles.py:395-396` is the single most load-bearing line in this task's blast radius. It is
cited by neither the spec nor revision 1, and it is why Step 7's mutation table below is not
what revision 1 claimed.

- [ ] **Step 1: Write the failing tests**

`"research_group"` is **not** a `ProfileKind`. `ProfileKind`
(`src/grant_lab/db/models/profiles.py:31-33`) has exactly two members,
`organizational_unit` and `individual`. Revision 1's body used `"research_group"` and only
avoided a visible break because `create_profile` silently coerces
(`profiles.py:337`: `kind = body.kind if body.kind in valid_kinds else ProfileKind.ORGANIZATIONAL_UNIT.value`).
Use `"organizational_unit"`, matching `tests/test_api_profiles_create.py:31`.

`ProfileCreateIn` (`profiles.py:295-315`) defaults every field but `name`, so the two-key body
below is accepted — no 422. `user` **is** in scope at the pre-check
(`profiles.py:326`, `user: User = Depends(get_current_user)`), so Step 5 compiles as written.
Both checked, so you do not have to.

These three are **one set**, but not in the way revision 1 claimed — see Step 7.

```python
"""Creation must not confirm a profile name the caller cannot read.

FIXTURES: no `client`, `session`, `make_user`, `make_profile` or
`auth_headers_for` exist. Auth is a signed session COOKIE. Module-local
helpers, in the idiom of tests/test_api_profile_transfer.py and
tests/test_api_profiles_create.py.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return c


def _user(session, email):
    u = User(email=email, name=email, status="active")
    session.add(u)
    session.flush()
    return u


def _profile(session, owner, *, name, visibility="private"):
    p = Profile(name=name, kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="", structured={}, created_by=owner.id, visibility=visibility)
    session.add(p)
    session.flush()
    return p


def test_a_name_held_by_an_unreadable_private_profile_does_not_409(
    monkeypatch, pg_url, pg_session_factory
):
    """GATE 8 -- the load-bearing one. profiles.py:253-258 states the rule this
    closes: "A 403 confirms the profile exists, and profile NAMES are the thing
    worth not confirming: someone can walk /api/profiles/1..N and ENUMERATE who
    is working on what." The detail route returns 404 for that reason; creation
    used to confirm the same fact by another door."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        other = _user(s, "other@umu.se")
        _profile(s, other, name="Hepatology", visibility="private")
        me = _user(s, "me@umu.se")
        s.commit()
        me_id = me.id

    client = _client_for(app, me_id)
    with client:
        r = client.post("/api/profiles",
                        json={"name": "Hepatology", "kind": "organizational_unit"})
    assert r.status_code != 409, "creation confirmed a profile the caller cannot read"
    assert r.status_code < 400, r.text


def test_a_duplicate_name_the_caller_DOES_own_still_409s(
    monkeypatch, pg_url, pg_session_factory
):
    """GATE 9 -- a BEHAVIOUR gate, not a mechanism gate. It pins that your own
    duplicate still 409s, which gate 8 alone would let you break by simply
    deleting the check.

    It does NOT catch a deleted pre-check on its own: profiles.py:395-396
    converts the resulting IntegrityError into the SAME 409 with the SAME
    message, so the two implementations are indistinguishable from here. The
    duplicate refusal is doubly enforced -- pre-check AND unique index -- and
    only removing BOTH reddens this. See Step 7."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        me = _user(s, "me@umu.se")
        _profile(s, me, name="Hepatology")
        s.commit()
        me_id = me.id

    client = _client_for(app, me_id)
    with client:
        r = client.post("/api/profiles",
                        json={"name": "Hepatology", "kind": "organizational_unit"})
    assert r.status_code == 409


def test_two_owners_may_both_hold_the_same_active_name(pg_session_factory):
    """GATE 10. Without this, keeping the index global passes gate 9 and the
    schema still enforces the oracle at the DATABASE level -- where
    profiles.py:395-396 quietly re-serves it as the very same 409, so gate 8
    would go red again with no visible change in the response."""
    with pg_session_factory() as s:
        a = _user(s, "a@umu.se")
        b = _user(s, "b@umu.se")
        _profile(s, a, name="Hepatology")
        _profile(s, b, name="Hepatology")
        try:
            s.commit()  # must not raise IntegrityError
        except IntegrityError as e:
            pytest.fail(f"the global index is still in force: {str(e)[:200]}")
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_api_profile_name_privacy.py -v`
Expected, measured: gate 8 FAILS with 409; gate 10 FAILS with an `IntegrityError` on
`uq_profiles_active_name`; **gate 9 PASSES** (it passes in every state but the compound mutation
in Step 7 — that is what makes it a behaviour gate).

- [ ] **Step 3: Write the migration**

The block below **compiles as printed** — revision 1's omitted `import sqlalchemy as sa` while
using `sa.text(...)` twice, which raises `NameError` for anyone extracting and running it.

```python
"""owner-scoped profile-name uniqueness

A globally-unique active name let any authenticated caller confirm the NAME of a
private profile they cannot read, by POSTing it and reading the 409 -- eight lines
below a comment in the same file stating that profile names are the thing worth not
confirming.

Relaxing a uniqueness constraint cannot fail on existing rows in either direction of
data, so this migration is safe whatever is already stored.

Revision ID: 0076
Revises: 0075
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_profiles_active_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_owner_name",
        "profiles",
        ["created_by", "name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    # Re-creating the GLOBAL index can fail if two owners now hold the same
    # active name -- which this migration exists to permit. Deliberately not
    # papered over: a downgrade must be a decision about that data, not a
    # silent failure at 3am.
    op.drop_index("uq_profiles_active_owner_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_name",
        "profiles",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
```

Read `alembic/versions/0075_freshness_fingerprints.py` first and match its import and header style.

**Round-tripped on a real Postgres 16** during the dry run — `upgrade head` → `downgrade -1` →
`upgrade head`, all exit 0, with the partial predicate `WHERE (is_active = true)` intact in both
directions. `op.drop_index` **does** work against an index created with `postgresql_where`;
`DROP INDEX` does not need the predicate.

- [ ] **Step 4: Update the ORM index**

Replace the `uq_profiles_active_name` Index in `db/models/profiles.py` (keep the surrounding comment about inactive profiles reusing a name, and add why it is owner-scoped):

```python
        # Partial UNIQUE on active names, scoped to the OWNER. Inactive
        # profiles (is_active=false) can reuse the name -- that's why it's
        # partial. See alembic/versions/0009_* and 0076_*.
        #
        # `created_by` is in the key because a GLOBAL unique active name made
        # creation an existence oracle: POST a guessed name, read the 409, and
        # you have confirmed a private profile you cannot read. Note the oracle
        # lived HERE, not in the handler -- web/api/profiles.py:395-396 turns
        # this index's IntegrityError into the very same 409 the pre-check
        # raises, so scoping that pre-check alone changed nothing observable.
        # The 409 can now only ever name a profile the caller already owns.
        Index(
            "uq_profiles_active_owner_name",
            "created_by",
            "name",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
```

- [ ] **Step 5: Scope the handler's pre-check**

At `profiles.py:365-369`, add the owner predicate so the pre-check and the index agree:

```python
        existing = session.execute(
            select(Profile.id).where(
                Profile.name == body.name,
                Profile.is_active.is_(True),
                # Scoped to the caller, matching uq_profiles_active_owner_name.
                # The pre-check and the index must agree: a mismatch is how
                # this defect happened -- the handler and the schema each
                # enforced something, and neither enforced what :253-258 says.
                Profile.created_by == user.id,
            )
        ).scalar_one_or_none()
```

`user` **is** in scope here (`profiles.py:326`, `user: User = Depends(get_current_user)`).

- [ ] **Step 6: Regenerate and run**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/test_api_profile_name_privacy.py tests/architecture \
              tests/test_migration_0076_owner_scoped_profile_names.py -v
```

Expected: PASS. `tests/architecture` only passes here because **Task 0 landed first** — at
`a8e31f98` it is red for an unrelated reason (see Global Constraints). Write the migration test
in the shape of `tests/test_migration_0075_freshness_fingerprints.py`, asserting the old index is
gone and the new one exists.

- [ ] **Step 7: Prove the set bites**

Run each mutation, restoring between. This table is **measured**, and it is not what revision 1
claimed.

| Mutation | Must redden | Measured |
|---|---|---|
| restore the unscoped pre-check | gate 8 | ✅ `1 failed, 2 passed`, `gate8 status=409` |
| **delete the pre-check AND make the index non-unique** (`unique=False` in both the ORM `Index(...)` and migration `0076`'s `create_index`) | gate 9 | ✅ `1 failed, 2 passed`, `gate9 status=200` |
| keep the index global (revert step 4 and the migration's `upgrade`) | gate 10 | ✅ `2 failed, 1 passed` — gates 8 **and** 10 both redden |

**Why mutation 2 is compound, and why that is honest.** Revision 1 claimed "delete the pre-check
entirely" reddens gate 9. Measured, it reddens **nothing**: `3 passed`, `gate8 status=200`,
`gate9 status=409`. With the pre-check gone the INSERT trips
`uq_profiles_active_owner_name` and `profiles.py:395-396` converts the `IntegrityError` into the
same 409 with the same message. The refusal is **doubly enforced**, so no single-point mutation
can redden gate 9 — and `db/models/profiles.py:48-49` already says the index is the real
enforcer ("Closes the race in the handler that pre-checks, releases the session, then inserts").
Removing both enforcers is the mutation that discriminates, and it was run: gate 9 alone goes
red, gates 8 and 10 stay green.

The set is **sound** — no mutation slips past all three gates, and mutation 3 is caught twice
over. It was revision 1's *description* of why, not the tests, that was wrong. **Do not delete
gate 9**: it is the only thing standing between this change and an implementation that stops
refusing duplicates altogether.

If gate 8 stays green under mutation 1, the fixture's other profile is not actually unreadable by the caller and the test proves nothing.

- [ ] **Step 8: Commit**

```bash
uv run ruff check src tests alembic
git add alembic/versions/0076_owner_scoped_profile_names.py \
        src/grant_lab/db/models/profiles.py src/grant_lab/web/api/profiles.py \
        docs/ARCHITECTURE.md tests/test_api_profile_name_privacy.py \
        tests/test_migration_0076_owner_scoped_profile_names.py
git commit -m "fix(profiles): a name collision no longer confirms a profile you cannot read"
```

---

### Task 4: F1 — the anonymous board encodes nothing

> **The strongest task in this plan.** All four of its gates were mutation-tested and all four
> bite; its OpenAPI claim was verified byte-identical; migration `0077` round-trips. It needs
> exactly two corrections, both below: `_approve()` in Step 6b and the `qr_for` import in Step 5.

**Files:**
- Create: `alembic/versions/0077_board_item_qr.py`
- Modify: `src/grant_lab/db/models/boards.py` (`BoardItem` — columns **and its docstring**)
- Modify: `src/grant_lab/web/api/boards.py:680` (the `BoardItem(...)` construction) **and its imports**
- Modify: `src/grant_lab/boards/public.py` (`PublicBoardRow`, `public_board_items`)
- Modify: `src/grant_lab/web/api/public_boards.py:132`
- Modify: **`tests/web/test_api_public_board.py`** (`_approve()` bypasses the route this task changes)
- Modify: `docs/ARCHITECTURE.md` (generated)
- Test: `tests/test_boards_qr_at_approval.py`

**Interfaces:**
- Consumes: `qr_for` from `grant_lab.boards.qr`.
- Produces: `BoardItem.qr_path: Mapped[str | None]`, `BoardItem.qr_modules: Mapped[int]` (default 0), `BoardItem.qr_source_url: Mapped[str | None]`. `PublicBoardRow` gains `qr_modules: int` and `qr_path: str`.

**No frontend step is needed, and that is measured, not assumed.** Every `PublicBoard*`
component schema in `app.openapi()` was dumped before and after the change: **identical, same
md5** (`e55518f5332d545a3c18bab0e785a1d2`). No `schema.gen.ts` regeneration.

**The defect.** `public_boards.py:132` calls `qr_for(row.url)` on the anonymous path, and `qr.py:313-314` takes `_encode_lock` **per URL, globally**. With `maxsize=256` and a 192-item board ceiling, two published boards with disjoint URL sets thrash the cache and land every request on the cold path. `qr.py:95-118` describes that scenario and concludes the cost is "ONE-TIME, COLD" — true only while the working set fits the cache, which two max-shape boards do not.

- [ ] **Step 1: Write the failing tests**

Revision 1 warned "do NOT use the fixture names above verbatim" and was right. This is the
**verified** version — it was run, and all four gates were mutation-tested against it. Note
`profile_factory` is a real fixture (`tests/conftest.py:184`) returning `(profile_id, user_id)`;
everything else is a module-local helper in `tests/web/test_api_public_board.py`'s idiom
(`:59`, `:76`, `:173`).

```python
"""The QR is rendered at approval, in an authenticated request -- never on the
anonymous /b/<slug> path.

FIXTURES: `client_unauth` is defined at tests/proposal_assist/conftest.py:129,
package-scoped, and INVISIBLE here. `board`, `auth_headers`, `make_call`,
`publish` and `session` do not exist. The anonymous client is a plain
`with TestClient(app) as c:`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.boards.qr import qr_for
from grant_lab.db.models import Board, BoardItem, Call, Source
from grant_lab.main import create_app

FUTURE = datetime.now(UTC) + timedelta(days=30)


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return c


def _make_board(pg_session_factory, *, profile_id, created_by, slug, is_published=True):
    with pg_session_factory() as s:
        b = Board(slug=slug, title="Corridor board", profile_id=profile_id,
                  created_by=created_by, is_published=is_published, orientation="landscape",
                  items_per_page=8, max_pages=3, page_seconds=30)
        s.add(b)
        s.commit()
        return b.id


def _make_call(pg_session_factory, *, external_id, url):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id=external_id, url=url,
                 title=f"Call {external_id}", deadline=FUTURE)
        s.add(c)
        s.commit()
        return c.id


def _approve_via_api(app, *, board_id, owner_id, call_ids):
    client = _client_for(app, owner_id)
    with client:
        return client.post(f"/api/boards/{board_id}/items", json={"call_ids": call_ids})


def test_the_public_route_encodes_nothing(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 1 -- the load-bearing one. Asserting the response is CORRECT would
    pass with the encode still on the hot path. Assert on cache MISSES."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate1 profile")
    slug = "g" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    cid = _make_call(pg_session_factory, external_id="qr-1", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text

    qr_for.cache_clear()
    before = qr_for.cache_info().misses
    with TestClient(app) as c:
        rr = c.get(f"/api/public/boards/{slug}")
    assert rr.status_code == 200, rr.text
    assert qr_for.cache_info().misses == before, "the anonymous route ran the encoder"


def test_approval_stores_the_qr_and_its_source_url(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 2."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate2 profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id,
                           slug="h" * 16)
    cid = _make_call(pg_session_factory, external_id="qr-2", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        item = s.scalar(select(BoardItem).where(BoardItem.board_id == board_id,
                                                BoardItem.call_id == cid))
        assert item.qr_modules > 0
        assert item.qr_path
        assert item.qr_source_url == "https://example.org/call/1"


def test_an_overlong_url_stores_no_qr_and_the_card_still_renders(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3. qr.py returns modules=0 above _MAX_URL_BYTES (=800, qr.py:124);
    the frontend already treats qr_modules === 0 as "no <svg>". The url below is
    920 bytes."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate3 profile")
    slug = "i" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    long_url = "https://example.org/" + "x" * 900
    cid = _make_call(pg_session_factory, external_id="qr-3", url=long_url)
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        item = s.scalar(select(BoardItem).where(BoardItem.call_id == cid))
        assert item.qr_modules == 0

    with TestClient(app) as c:
        body = c.get(f"/api/public/boards/{slug}").json()
    card = [i for i in body["items"] if i["url"].startswith("https://example.org/x")][0]
    assert card["qr_modules"] == 0
    assert card["title"]  # the card still renders


def test_a_url_changed_after_approval_serves_no_qr_rather_than_a_stale_one(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4. A stored QR is derived data with the same exists-versus-current
    problem the freshness release just fixed for embeddings. A QR pointing
    somewhere the call no longer lives is worse than no QR -- it is on a wall."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate4 profile")
    slug = "j" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    cid = _make_call(pg_session_factory, external_id="qr-4", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        s.get(Call, cid).url = "https://example.org/call/MOVED"
        s.commit()

    with TestClient(app) as c:
        body = c.get(f"/api/public/boards/{slug}").json()
    card = [i for i in body["items"] if i["url"].endswith("MOVED")][0]
    assert card["qr_modules"] == 0, "served a QR pointing at the old url"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_boards_qr_at_approval.py -v`
Expected: gate 1 FAILS (misses increased — measured `before=0 after=1`, `assert 1 == 0`, "the
anonymous route ran the encoder"); gates 2-4 FAIL on a missing `qr_path` attribute.

- [ ] **Step 3: Migration `0077`**

Three nullable-or-defaulted columns, then a backfill that renders each approved item's QR.
`board_items` is empty in production today (boards shipped 2026-09-06 and have not deployed), so
the backfill is a no-op there — write it for the general case anyway and **measure the row count
before running it elsewhere.** Round-tripped on a real Postgres 16 in the dry run.

```python
"""store a board item's QR at approval instead of rendering it on the public path

Revision ID: 0077
Revises: 0076
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("board_items", sa.Column("qr_path", sa.Text(), nullable=True))
    op.add_column(
        "board_items",
        sa.Column("qr_modules", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("board_items", sa.Column("qr_source_url", sa.Text(), nullable=True))

    from grant_lab.boards.qr import qr_for

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT bi.board_id, bi.call_id, c.url FROM board_items bi "
            "JOIN calls c ON c.id = bi.call_id"
        )
    ).all()
    for board_id, call_id, url in rows:
        qr = qr_for(url or "")
        conn.execute(
            sa.text(
                "UPDATE board_items SET qr_path = :p, qr_modules = :m, qr_source_url = :u "
                "WHERE board_id = :b AND call_id = :c"
            ),
            {"p": qr.path or None, "m": qr.modules, "u": url or None,
             "b": board_id, "c": call_id},
        )


def downgrade() -> None:
    op.drop_column("board_items", "qr_source_url")
    op.drop_column("board_items", "qr_modules")
    op.drop_column("board_items", "qr_path")
```

- [ ] **Step 4: The ORM columns — and the docstring they falsify**

On `BoardItem`, after `approved_at`:

```python
    # The QR, rendered ONCE at approval by an admin in an authenticated,
    # rate-limited request -- not on the anonymous /b/:slug path, where
    # qr.py's per-URL global _encode_lock made two alternating max-shape
    # boards a cache-thrash amplifier.
    qr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0 means "no QR" -- the signal the frontend already honours
    # (qr_modules === 0 => no <svg>).
    qr_modules: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # The Call.url this QR was rendered from. NOT bookkeeping: a stored QR is
    # derived data with the same exists-versus-current problem the freshness
    # release fixed for embeddings, and a QR on a corridor wall pointing where
    # the call no longer lives is worse than no QR. The public route compares
    # this and serves qr_modules=0 on a mismatch.
    qr_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**The class docstring at `db/models/boards.py:125-131` now says something false and must be
amended in the same edit.** It currently opens:

> "One approved call on one board. **Stores a `call_id` and nothing about the call.** D8: the
> board shows LIVE call data … **A snapshot would keep showing a deadline the funder has since
> fixed**, and on a hospital wall a wrong date is worse than an unreviewed title."

`qr_source_url` **is** a snapshot of `Call.url` — precisely what D8 rules out. Storing it is
still right (the whole task is about not encoding on the anonymous path), but the two statements
have to be reconciled, not left contradicting each other. `docs/ARCHITECTURE.md` is generated
from this file. Replace "Stores a `call_id` and nothing about the call" with something that says:
everything **displayed** is joined live from `calls`, and D8 still holds for all of it; the one
stored copy is `qr_source_url`, which is never displayed and exists only so a stale QR can be
**withdrawn** — a mismatch serves `qr_modules=0`, never a stale code.

- [ ] **Step 5: Render at approval**

`boards.py:680` builds `BoardItem(...)` from a bare `cid` with no `Call` loaded. Load the URLs for `new_ids` first, then render:

```python
            urls = dict(
                session.execute(
                    select(Call.id, Call.url).where(Call.id.in_(new_ids))
                ).all()
            )
            for offset, cid in enumerate(new_ids):
                url = urls.get(cid) or ""
                qr = qr_for(url)
                session.add(
                    BoardItem(
                        board_id=board.id,
                        call_id=cid,
                        position=next_position + offset,
                        approved_by=user.id,
                        qr_path=qr.path or None,
                        qr_modules=qr.modules,
                        qr_source_url=url or None,
                    )
                )
```

**`qr_for` is NOT imported in `boards.py`** — grepped at `a8e31f98`, the name appears nowhere in
the file, and the block above does not compile without it. Add it alongside
`from grant_lab.boards.queue import board_queue` (`boards.py:49`):

```python
from grant_lab.boards.qr import qr_for
```

`Call` **is** already imported (`boards.py:50`), so the rest of the block is fine.

- [ ] **Step 6: Return the stored QR, and compare the URL**

In `boards/public.py`, add `qr_modules: int` and `qr_path: str` to `PublicBoardRow`, select `BoardItem` alongside `Call` and `Source.name`, and build each row with the **comparison**:

```python
        PublicBoardRow(
            ...,
            url=call.url,
            # Serve the stored QR only while it still describes this url.
            qr_modules=(item.qr_modules if item.qr_source_url == call.url else 0),
            qr_path=(item.qr_path or "") if item.qr_source_url == call.url else "",
        )
```

The `select(Call, Source.name)` becomes `select(Call, Source.name, BoardItem)` and the
comprehension unpacks `for call, source_name, item in rows`.

In `public_boards.py`, delete the `qr = qr_for(row.url)` line and read `row.qr_modules` / `row.qr_path` instead. **Then remove the now-unused `qr_for` import at `public_boards.py:46`** — `ruff check` fails until you do. Confirmed: removing it gives `All checks passed!`.

- [ ] **Step 6b: Fix `_approve()` in `tests/web/test_api_public_board.py` — DO NOT SKIP**

`tests/web/test_api_public_board.py:134-141` defines `_approve()`, called **11 times** in that
module, which inserts a `BoardItem` **directly** and never through
`POST /api/boards/{id}/items`. This task moves the QR render into that route, so every item the
helper builds lands with `qr_modules = 0` and `qr_source_url = NULL`, the new comparison in
`public.py` is False for all of them, and the public route serves the "no QR" sentinel for every
card.

Measured with Task 4 fully implemented and this helper untouched: **4 failures**, all
pre-existing tests in that module —
`test_a_published_board_returns_its_cards`, `test_every_item_carries_a_qr_path`,
`test_the_qr_points_at_the_funder_url_not_this_app`,
`test_an_over_long_url_costs_one_qr_not_the_whole_board`.

Step 7 *does* run that file, so you would see the reds — but "my change broke the public board"
is the wrong diagnosis. The fix is nine lines, verified (`19 passed in 28.66s`):

```python
def _approve(pg_session_factory, *, board_id, call_id, approved_by, position=0):
    with pg_session_factory() as s:
        # The QR is rendered at approval now, in the API. This helper bypasses
        # that route, so it must render what the route renders -- otherwise
        # qr_source_url stays NULL and the public route serves the qr_modules=0
        # sentinel for every card this helper built.
        url = s.get(Call, call_id).url
        qr = qr_for(url)
        s.add(
            BoardItem(
                board_id=board_id, call_id=call_id, position=position,
                approved_by=approved_by,
                qr_path=qr.path or None, qr_modules=qr.modules, qr_source_url=url or None,
            )
        )
        s.commit()
```

plus widening the import at `tests/web/test_api_public_board.py:29`:

```python
from grant_lab.boards.qr import _MAX_URL_BYTES, qr_for
```

**Five other test files also build `BoardItem(...)` directly**: `tests/test_boards_alerts.py`,
`tests/web/test_api_board_items.py`, `tests/test_boards_queue.py`, `tests/test_scheduler.py`,
`tests/web/test_api_boards_crud.py`. Measured: **none of them asserts on `qr_modules`/`qr_path`,
so all five pass unchanged.** Leave them, but know they now create QR-less items — which matters
for anything added to them later.

- [ ] **Step 7: Regenerate, run, and prove the gates bite**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/test_boards_qr_at_approval.py tests/web/test_api_public_board.py \
              tests/test_boards_qr.py tests/architecture -v
```

Then each mutation, restoring between. All four were run; all four redden their named gate —
**the only fully honest mutation table in revision 1 of this plan.**

| Mutation | Must redden | Measured |
|---|---|---|
| restore `qr = qr_for(row.url)` in `get_public_board` | gate 1 | ✅ `2 failed, 2 passed` — gate 1 **and** gate 4, the latter expected: a live encode always matches the current url |
| drop the three `qr_*` kwargs from the `BoardItem(...)` | gate 2 | ✅ `1 failed, 3 passed` |
| store `qr.path` unconditionally, ignoring `qr.modules` | gate 3 | ✅ `1 failed, 3 passed` |
| drop the `item.qr_source_url == call.url` comparison | gate 4 | ✅ `1 failed, 3 passed` |

Restored: `4 passed in 9.29s`.

- [ ] **Step 8: Full suite and commit**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: **`5424 passed, 1 skipped`** — the `5411` you measured at `a8e31f98`, plus 1 for
Task 0's fix, plus the **12** tests this plan adds (2 + 3 + 3 + 4). Measure the total; do not
predict it. If it differs, **find the term that changed and name it — never adjust the number to
make the arithmetic close.** `0 skipped` means a live API call ran — stop.

```bash
uv run ruff check src tests alembic scripts
git add -A
git commit -m "fix(boards): the anonymous board reads a stored QR instead of encoding one"
```

---

## Self-review notes

**Spec coverage.** F2 → Task 1, F4 → Task 2, F3 → Task 3, F1 → Task 4. All twelve spec gates appear: 1-5 in Task 4, 6-7 in Task 1, 8-10 in Task 3, 11-12 in Task 2. Task 0 answers to no spec finding; it exists because the base commit is red.

**Task independence.** No task consumes another's output. **Task 0 must land first** (the base is
red and Tasks 3 and 4 both assert `tests/architecture` passes). Tasks 3 and 4 each add a
migration (`0076`, `0077`) and must land in that order for the revision chain. Tasks 1 and 2 are
free to move.

**Discriminating controls — corrected, and each one measured.**
- **Task 1's structural control is Step 5's grep**, not its second test. That test's only
  observable is a raised exception; a 403 or 404 is a response, so it stays green under the very
  mutant it was supposed to catch. It is a no-500/no-hang smoke check and is now named as one.
  Task 1's *behavioural* control is `len(ok) == 1`, which discriminates 10/10 with no flake —
  revision 1's `winners` assertion was a tautology that passed the unfixed tree.
- **Task 3's gates 8/9/10 are a sound set but not the set revision 1 described.** Gate 9 is a
  behaviour gate; the duplicate refusal is doubly enforced (pre-check + unique index) and only
  removing both reddens it. Gate 10's mutation reddens gates 8 and 10 both.
- **Task 4's gate 3 is the control proving gate 2 did not simply store everything
  unconditionally**, and it holds under mutation.
- **Task 2's third gate** (`test_the_narrowed_row_and_the_loaded_profile_agree`) is new in
  revision 2. It is the only gate that catches the `ranking/queue.py` break as itself; without
  it the defect surfaces as an `AttributeError` in seven unrelated reconcile tests — this repo's
  documented "mass errors, wrong diagnosis" shape.

**Fixtures were the known risk, and revision 1 lost that bet.** Eleven of its twelve assumed
fixture names do not exist, including two it did not even hedge. The verified list is now in
**"Fixtures — the verified list"** above, and every test code block in this plan has been run
rather than assumed. Still: extract and run the blocks before trusting them. A missing fixture
ERRORs, which is the false-pass signature, not a failing test.

**Line citations.** Re-verified against `a8e31f98`. Corrections carried into revision 2:
`db/models/profiles.py`'s `uq_profiles_active_name` `Index(` opens at **`:52`** and closes at
**`:57`**; `web/api/profiles.py`'s create pre-check is **`:365-369`**; the blanket
`except IntegrityError -> 409` is **`:395-396`**; the "names are the thing worth not confirming"
comment is **`:253-258`**. Exact as written in revision 1 and re-confirmed: `qr.py:313-314`,
`qr.py:95-118`, `_MAX_URL_BYTES = 800` at `qr.py:124`, `boards.py:680`, `profiles.py:136`,
`public_boards.py:132`, `fingerprint.py:23` and `:110-118`, alembic head `0075`.

**Type consistency.** `qr_for(url) -> QrCode` with `.modules` and `.path` is used in Task 4 steps
5, 6 and 6b as defined in `boards/qr.py:181`. `FINGERPRINT_VERSION` is defined in Task 2 and
referenced by its own tests and by `tests/test_ranking_worker_fingerprint.py:54`.
`_profile_for_edit(session, *, profile_id: int, user: User) -> Profile` keeps its signature in
Task 1 — only the load inside it changes, which is why all seven callers inherit the lock.
