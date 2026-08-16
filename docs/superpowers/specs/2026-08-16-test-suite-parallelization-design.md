# Test-suite parallelization — design

**Date:** 2026-08-16
**Status:** approved, not yet implemented
**Goal:** cut the local full-suite wall-clock from ~11.5 minutes to under 2, without
changing what the suite proves.

## 1. The problem, measured

The full suite on `fix-circular-fk` runs **3357 passed, 1 skipped in 686s (11:26)**,
single-process, on a machine with **72 cores**. One core is busy; 71 are idle.

Profiling `tests/test_api_workspaces.py` (61 tests, 35.31s) by phase:

| Phase | Total | Share | Per test |
|---|---|---|---|
| `call` (test bodies) | 25.9s | 74% | 0.424s |
| `teardown` | 4.5s | 13% | 0.073s |
| `setup` (session fixtures: container + Alembic) | 2.58s | 7% | one-time |
| collection/import | ~2s | 6% | one-time |

The test bodies dominate, and inside them one call dominates: **`create_app()` costs
0.325s** against an average `call` phase of 0.424s — **~77% of every test is rebuilding
the FastAPI app**. A cProfile of three `create_app()` calls attributes 1.849s of 1.925s
to `include_router` → `add_api_route` → `create_model_field`: **321 routes producing
3,672 pydantic model fields**. That is FastAPI/pydantic construction cost, not
application logic, and it is paid again for every test that builds an app.

Two candidate savings were measured and **rejected**:

- **Per-test `migrated_engine.dispose()`** (`tests/conftest.py:95`). Removing it entirely
  moved the 61-test file from 35.31s to 34.36s — **~3%**. Not worth the isolation risk;
  the line stays.
- **Git subprocesses** in test setup: `git init` + `add` + `commit` measured at **0.008s**.
  Negligible.

## 2. Approach

**Run the suite in parallel with `pytest-xdist`, one Postgres per worker.** The app
rebuild is not eliminated — it is paid on N cores simultaneously instead of one.

This was chosen over making the app itself reusable across tests. Sharing one
`create_app()` attacks the 77% directly and would be ~4x serially, but every test
monkeypatches its own environment *before* calling `create_app()`, so a shared app can
only serve tests whose configuration matches. That means auditing and editing a large
share of the 434 test files, with real risk of hidden coupling. Parallelism gets a
bigger win for changes confined to one file. **After parallelization the full run is
expected to be 1-2 minutes, at which point app-reuse has little left to buy** — it is
explicitly not planned.

### 2.1 Isolation comes free

`pg_container` (`tests/conftest.py:25`) is already **session-scoped**, and xdist workers
are **separate processes** that each run their own session fixtures. Every worker
therefore starts its own `pgvector/pgvector:pg16` container on its own
testcontainers-assigned random port, applies its own Alembic migrations, and owns its
database exclusively.

Consequences:
- **No change to the isolation model.** The per-test TRUNCATE teardown
  (`conftest.py:77-80`) still governs a database only that worker touches.
- **No cross-process coordination** — no file locks, no shared container, no
  worker-id-keyed database names.
- Startup cost is paid per worker but concurrently: the measured session `setup` is
  2.58s, so worker startup adds roughly 3s of wall-clock, not N x 3s.

### 2.2 Distribution mode: `--dist loadfile`

Not the xdist default. Default `--dist load` hands out individual tests, which destroys
within-file ordering. `loadfile` sends every test in a file to the same worker,
preserving that order while still parallelizing across files. With 434 test files and
8-16 workers there is ample work to distribute.

**This is a precaution, not a proven necessity, and the distinction matters.** An earlier
draft justified it with `tests/test_web_auth.py::test_login_form_renders` failing in
isolation. That test no longer exists — it was deleted on 2026-06-27 with the rest of the
Jinja web stack in the full-React migration (`cf6ab4a`). No live same-file dependency has
been identified in the current tree.

The historical example would not have supported `loadfile` even when it existed: it was
the *first* test in its file and depended on an earlier test in a *different* file having
migrated the session database. That is **cross-file** coupling, which `loadfile` does not
address — it guarantees ordering only *within* a file. Per-worker databases (2.1) remove
that particular hazard by giving every worker its own migrated database, but the general
point stands: `loadfile` is cheap insurance against within-file ordering assumptions, and
nothing more. Adopt it because it costs nothing at this file count, not because a known
test demands it.

### 2.3 Worker count is measured, not assumed

72 cores does not mean 72 workers: each worker carries a Postgres container, and the
suite becomes database- and Docker-bound well before it becomes CPU-bound. Time the full
suite at **-n 4, -n 8, and -n 16**.

Adoption rule, so "the knee" is not a judgement call. **The floor is -n 4**, then promote
by adjacent comparison:

- adopt **-n 8** only if it beats -n 4 by at least 15%;
- adopt **-n 16** only if it also beats -n 8 by at least 15%.

So if neither promotion qualifies, the answer is -n 4 — not -n 8. Record all three
timings in the implementation's commit message so the choice is auditable and
re-derivable on different hardware.

### 2.4 CI is not changed

`.github/workflows/ci.yml:47` runs `uv run pytest -v` on `ubuntu-latest` — a 2-4 core,
~7GB runner where 8 Postgres containers would contend for RAM and cores and likely run
*slower*, if they start at all. Optimizing CI is a separate question with a different
answer (probably one shared container with a database per worker, at -n 2).

Parallelism is therefore **opt-in and local**: no `addopts` entry in `pyproject.toml`,
because that would silently change CI too. `uv run pytest` stays serial and keeps
working exactly as today. The parallel invocation is documented in `CLAUDE.md`.

## 3. Risks and handling

| Risk | Handling |
|---|---|
| Tests sharing a hardcoded filesystem path. `WORKSPACES_DIR=/tmp/test_workspaces` appears in `tests/proposal_assist/test_tools_claude_adapter_wiring.py:31` and `test_drafting_factory.py:19,40`; several files pass literal `/tmp/...` as a `repo_dir` value. | The shake-out run exposes any that actually collide. Fix by pointing at `tmp_path`. Paths used only as inert struct values need no change. |
| Hidden coupling between tests in different files | **Fix the coupling — there is no escape hatch in this design.** See 3.1 for why the obvious one is not usable. |
| Docker/RAM exhaustion at high worker counts | Bounded by 2.3's measurement — if -n 16 is not faster than -n 8, it is not adopted. |
| Known flake: the session Postgres drops under full-suite load (`connection refused`) | Expected to **improve**: N databases share the load instead of one absorbing all of it. If a `connection refused` appears in a parallel run, it is triaged as infra per the existing rule, not as a coupling bug. |
| A parallel-only failure is misread as flake and waved through | The acceptance bar in section 4 is exact-count equality, and the run is repeated. A test that fails only in parallel is a real defect in test isolation until proven otherwise. |

### 3.1 Why cross-file coupling must be fixed rather than pinned

The natural fallback — mark the coupled files with `@pytest.mark.xdist_group` and switch
to `--dist loadgroup` — **is not available, because it silently downgrades the entire
rest of the suite.** Verified in xdist's source: `LoadGroupScheduling._split_scope`
returns the group name only for tests carrying the mark, and returns the **full nodeid**
for everything else. Each unmarked test therefore becomes its own scope and is
distributed individually — that is `--dist load` behaviour for all 434 files, which is
exactly the within-file ordering guarantee 2.2 adopts `loadfile` to keep. Pinning two
files would cost the guarantee everywhere.

So if the shake-out run surfaces cross-file coupling, the response is to make the
dependent test self-sufficient (give it its own fixture/seed rather than inheriting a
neighbour's state). If a case appears where that is genuinely impractical, **stop and
revise this design** rather than switching distribution mode. The revision would need
machinery `loadfile` does not have: a `pytest_collection_modifyitems` hook assigning
every file its own `xdist_group` by default so `loadgroup` reproduces `loadfile`, with
coupled files sharing one group. That is not built now — nothing has been shown to need
it, and it is meaningful complexity to carry on speculation.

## 4. Acceptance

1. **Exact parity.** The parallel run reports **3357 passed, 1 skipped** — identical to
   the serial baseline on the same commit. Not "no failures": the same counts, so a
   silently skipped or uncollected test cannot hide.
2. **Repeatability.** Three consecutive parallel runs at the adopted worker count, all
   with the same counts. Parallel failures are frequently intermittent, so one green run
   is not evidence.
3. **Serial still works.** `uv run pytest` with no flags produces the same counts, since
   CI depends on it.
4. **A stated speedup.** The adopted configuration's wall-clock is recorded against the
   686s baseline.

## 5. Scope boundary

In scope: the `pytest-xdist` dependency, the documented parallel invocation, worker-count
measurement, and fixes to whichever tests prove parallel-unsafe.

Out of scope, deliberately: sharing `create_app()` across tests (section 2); removing the
per-test `dispose()` (section 1, measured at ~3%); any CI change (section 2.4); the
suite's warning volume, which dropped from 2134 to 263 when the `users`/`profiles` FK
cycle was broken and is tracked separately.
