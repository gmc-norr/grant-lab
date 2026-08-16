# Test-Suite Parallelization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the local full-suite wall-clock from ~11 minutes to under 2 by running pytest under `pytest-xdist`, without changing what the suite proves.

**Architecture:** Each xdist worker is a separate process, and `pg_container` in `tests/conftest.py` is session-scoped — so every worker automatically starts its own `pgvector/pgvector:pg16` container on its own port and owns its database exclusively. Isolation therefore requires **no conftest change**. The work is: add the dependency without breaking three CI gates, shake out tests that turn out to be parallel-unsafe, and measure the worker count instead of guessing it.

**Tech Stack:** Python 3.12, `uv`, pytest 8.3+, pytest-xdist (new), testcontainers Postgres, Alembic.

**Spec:** `docs/superpowers/specs/2026-08-16-test-suite-parallelization-design.md`

## Global Constraints

- **Serial baseline that every run is compared against: `3357 passed, 1 skipped`.** Exact-count equality, not "no failures" — a silently uncollected test must not be able to hide.
- **Distribution mode is `--dist loadfile`.** Never `--dist loadgroup`: `LoadGroupScheduling._split_scope` returns the full nodeid for *unmarked* tests, so any use of it scatters all 434 test files per-test and destroys the within-file ordering `loadfile` exists to keep.
- **Never add `-n` or `--dist` to `[tool.pytest.ini_options] addopts` in `pyproject.toml`.** CI (`.github/workflows/ci.yml:47`) runs `uv run pytest -v` on a 2-4 core `ubuntu-latest` runner; inheriting parallelism there would make CI slower or break it. Parallelism is opt-in on the command line only.
- **Never change `[tool.uv] exclude-newer` (`pyproject.toml:75`, currently `"2026-08-03T00:00:00Z"`).** It is a deliberate 7-day supply-chain cooldown. If a package will not resolve, report it — do not bump the date.
- **`no-build-package` in `pyproject.toml` is GENERATED, never hand-edited.** Regenerate with `uv run python scripts/gen_no_build.py` after any `uv.lock` change; CI enforces sync via `--check`.
- Work on a branch. Never commit to `master` directly.
- Python commands run through `uv run`.

## Environment notes (read before running anything)

- **The full suite takes ~11 minutes and the Bash tool caps a foreground command at 10 minutes.** Run it detached and poll:
  ```bash
  cd /home/parlar_ai/dev/call-monitor
  nohup uv run pytest -q > /tmp/suite-<label>.log 2>&1 &
  # then poll until the process is gone:
  until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done; tail -3 /tmp/suite-<label>.log
  ```
  Note `pgrep -q` does **not** exist on Linux — it errors and the loop exits immediately. Use `> /dev/null` as above.
- **Never pipe a suite run through `tail` to get its exit code** — the pipeline's status is `tail`'s (always 0). Redirect to a file, then `echo $?`.
- **`pytest` exits 4 on "no tests ran."** A path typo reads as success to any check that only inspects the exit code. Always confirm a nonzero collected count.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `pyproject.toml` | `[dependency-groups] dev` gains `pytest-xdist`; `no-build-package` regenerated | 1 |
| `uv.lock` | Resolved lock including pytest-xdist and its `execnet` dependency | 1 |
| whichever test files the shake-out implicates | Removing cross-test coupling | 2 |
| `CLAUDE.md` | Documents the parallel invocation for humans and agents | 3 |

---

### Task 1: Add pytest-xdist and record the shake-out

Adding a dependency to this repo touches three CI gates that have nothing to do with pytest. Get them green in the same commit.

**Files:**
- Modify: `pyproject.toml` (`[dependency-groups] dev`, and the generated `no-build-package` list)
- Modify: `uv.lock`

**Interfaces:**
- Consumes: nothing.
- Produces: a working `uv run pytest -n <N> --dist loadfile` invocation, and a **recorded list of parallel-only failures** that Task 2 consumes. Write that list to `docs/superpowers/plans/shakeout-findings.md` so it survives context loss.

- [ ] **Step 1: Branch**

```bash
cd /home/parlar_ai/dev/call-monitor
git checkout -b test-parallelization
git log --oneline -1   # expect: 265d2bd docs(plan): implementation plan for test-suite parallelization
                       # (if master has moved on, that is fine — re-derive the baseline in Step 2)
```

- [ ] **Step 2: Establish the serial baseline on THIS commit**

Do not trust the number in this plan — re-derive it, because the plan may be older than the branch.

```bash
cd /home/parlar_ai/dev/call-monitor
nohup uv run pytest -q > /tmp/suite-serial-baseline.log 2>&1 &
until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done
tail -2 /tmp/suite-serial-baseline.log
```

Expected: `3357 passed, 1 skipped` and a wall-clock around 660-690s. **If the counts differ, stop and report** — every later comparison is against this number, so proceeding with a different baseline makes the whole task meaningless.

- [ ] **Step 3: Add the dependency**

```bash
cd /home/parlar_ai/dev/call-monitor
uv add --dev pytest-xdist
```

Expected: `pyproject.toml`'s `dev` group gains a `pytest-xdist>=...` line and `uv.lock` gains `pytest-xdist` plus its `execnet` dependency.

If resolution fails because no version predates the cooldown, **stop and report**. Do not edit `exclude-newer`.

- [ ] **Step 4: Regenerate the wheels-only allowlist**

`no-build-package` is generated from `uv.lock` and CI fails if it drifts.

```bash
cd /home/parlar_ai/dev/call-monitor
uv run python scripts/gen_no_build.py
git diff --stat pyproject.toml
```

Expected: `pyproject.toml` changed, with `execnet` and `pytest-xdist` added to `no-build-package`.

- [ ] **Step 5: Verify all three dependency gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv lock --check                              ; echo "LOCK_EXIT=$?"
uv run python scripts/gen_no_build.py --check ; echo "NOBUILD_EXIT=$?"
uv run pip-audit                             ; echo "AUDIT_EXIT=$?"
uv run ruff check .                          ; echo "RUFF_EXIT=$?"
```

Expected: all four exit 0. If `pip-audit` reports an advisory against the new package, stop and report — do not bump `exclude-newer` to chase a fix version.

- [ ] **Step 6: Confirm xdist is importable and collection is unchanged**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run python -c "import xdist; print(xdist.__version__)"
uv run pytest --collect-only -q 2>/dev/null | tail -1
```

Expected: a version string, then `3358 tests collected`. The collected count must match the baseline's `3357 passed + 1 skipped`.

- [ ] **Step 7: Run the shake-out**

This is discovery, not a pass/fail gate. `-n 8` is a probe, not the adopted value — Task 3 decides that.

```bash
cd /home/parlar_ai/dev/call-monitor
nohup uv run pytest -n 8 --dist loadfile -q > /tmp/suite-shakeout.log 2>&1 &
until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done
tail -5 /tmp/suite-shakeout.log
grep -E "^(FAILED|ERROR)" /tmp/suite-shakeout.log | sort -u
```

- [ ] **Step 8: Record the findings**

Create `docs/superpowers/plans/shakeout-findings.md` with the wall-clock, the summary line, and the **full deduplicated list** of `FAILED`/`ERROR` nodeids from Step 7. If the list is empty, write that explicitly — Task 2 needs to know the difference between "nothing failed" and "nobody looked."

For each failure also record whether it reproduces **serially**:

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest <failing-nodeid> -q > /tmp/one.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/one.log
```

A test that fails serially too is a pre-existing defect, not a parallelism problem — label it as such. A test that passes serially and fails in parallel is Task 2's work.

- [ ] **Step 9: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add pyproject.toml uv.lock docs/superpowers/plans/shakeout-findings.md
git commit -m "test: add pytest-xdist and record the parallel shake-out

Regenerated no-build-package (generated from uv.lock; CI checks sync).
exclude-newer untouched. No addopts change -- CI stays serial.

Shake-out at -n 8 --dist loadfile: <summary line>."
```

---

### Task 2: Make the suite parallel-safe

**Files:**
- Modify: whichever test files `docs/superpowers/plans/shakeout-findings.md` names
- Read: `docs/superpowers/plans/shakeout-findings.md`

**Interfaces:**
- Consumes: Task 1's `shakeout-findings.md` failure list, and the `--dist loadfile` invocation.
- Produces: a parallel run whose counts equal the serial baseline.

**If Task 1's findings list is empty, this task is a verification-only no-op.** Run Step 4 to confirm, record that no fixes were required, and report DONE with no commit. Do not invent work.

- [ ] **Step 1: Classify each failure**

For each nodeid in the findings file, decide which of these it is:

1. **Shared mutable filesystem path** — two workers writing the same directory.
2. **Cross-file coupling** — the test depends on state another file's test created.
3. **Pre-existing serial failure** — already labelled as such in Task 1 Step 8; not this task's problem, report it and leave it alone.
4. **Infra flake, not a code defect** — this project has a documented one: the testcontainers Postgres becoming unreachable mid-run, surfacing as `psycopg.OperationalError: connection refused`, or a lifespan-startup `UndefinedTable` / `relation "..." does not exist` when migrations have not finished applying. Both are environmental.

**How to tell category 4 from a real coupling bug — do not guess.** Re-run the *same nodeid* alone three times:

```bash
cd /home/parlar_ai/dev/call-monitor
for i in 1 2 3; do uv run pytest <failing-nodeid> -q > /tmp/flake-$i.log 2>&1; echo "run $i EXIT=$?"; done
```

A coupling bug is deterministic under the conditions that trigger it; an infra flake is not. If it fails intermittently with one of the two signatures above, label it infra, record it in the findings file, and move on — **but say so explicitly in your report rather than silently dropping it.** If it fails every time with an assertion about data another test created, it is category 2.

Parallelism is expected to *reduce* this flake, since N databases share the load instead of one absorbing all of it. If category 4 failures are *more* frequent in parallel than serially, that is a finding worth escalating, not absorbing.

**Known non-suspects — do not "fix" these.** `WORKSPACES_DIR=/tmp/test_workspaces` appears in `tests/proposal_assist/test_drafting_factory.py:19,40,59` and `tests/proposal_assist/test_tools_claude_adapter_wiring.py:31`, and several files pass literal `/tmp/...` as a `repo_dir` value. These were checked when the spec was written: they only feed `Settings()` and inert struct fields — nothing writes to those paths. Changing them is churn. Only act if a nodeid in the findings file actually implicates one.

- [ ] **Step 2: Fix category 1 — shared filesystem paths**

Point the test at pytest's per-test `tmp_path` fixture instead of a fixed directory. The pattern this repo already uses (`tests/test_api_workspaces.py:49`):

```python
def test_something(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
```

`tmp_path` is unique per test per worker, so two workers cannot collide.

- [ ] **Step 3: Fix category 2 — cross-file coupling**

Make the dependent test self-sufficient: give it its own fixture or seed step rather than inheriting a neighbour's state.

**There is no escape hatch, deliberately.** Do NOT mark tests with `@pytest.mark.xdist_group` and switch to `--dist loadgroup`. Verified in xdist's source: `LoadGroupScheduling._split_scope` returns the group name only for *marked* tests and the **full nodeid** for everything else, so each unmarked test becomes its own scope — that is `--dist load` behaviour for all 434 files, destroying the within-file ordering guarantee this plan depends on. Pinning two files would cost the guarantee everywhere.

If you hit a case where making the test self-sufficient is genuinely impractical, **stop and escalate.** That is a design revision, not an implementation choice.

- [ ] **Step 4: Verify parallel now matches serial**

```bash
cd /home/parlar_ai/dev/call-monitor
nohup uv run pytest -n 8 --dist loadfile -q > /tmp/suite-parallel-verify.log 2>&1 &
until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done
tail -3 /tmp/suite-parallel-verify.log
```

Expected: `3357 passed, 1 skipped` — identical counts to Task 1 Step 2's serial baseline.

- [ ] **Step 5: Verify serial still passes**

The fixes must not have broken the invocation CI uses.

```bash
cd /home/parlar_ai/dev/call-monitor
nohup uv run pytest -q > /tmp/suite-serial-recheck.log 2>&1 &
until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done
tail -3 /tmp/suite-serial-recheck.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: `3357 passed, 1 skipped`, and `RUFF_EXIT=0`.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add -A
git commit -m "test: make <N> tests parallel-safe

<one line per test: what coupled it, and what made it self-sufficient>

Parallel (-n 8 --dist loadfile) and serial both report 3357 passed, 1 skipped."
```

---

### Task 3: Measure the worker count, document it, and prove the acceptance bar

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: a parallel-safe suite from Task 2.
- Produces: the adopted `-n` value, documented, with three consecutive green runs behind it.

- [ ] **Step 1: Time the three candidates**

Each is a full suite run (~1-4 minutes each once parallel). Run them one at a time — concurrent runs would compete for cores and Docker and corrupt the measurement.

```bash
cd /home/parlar_ai/dev/call-monitor
for N in 4 8 16; do
  nohup uv run pytest -n $N --dist loadfile -q > /tmp/suite-n$N.log 2>&1 &
  until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 10; done
  echo "=== -n $N ==="; tail -2 /tmp/suite-n$N.log
done
```

Record all three wall-clock times and all three count lines. **Every one of the three must report `3357 passed, 1 skipped`** — a configuration that is fast but loses tests is not a candidate.

- [ ] **Step 2: Apply the adoption rule**

The floor is `-n 4`, then promote by adjacent comparison:

- adopt **`-n 8`** only if it beats `-n 4` by at least 15%;
- adopt **`-n 16`** only if it *also* beats `-n 8` by at least 15%.

If neither promotion qualifies, the answer is `-n 4` — not `-n 8`. Compute the percentages explicitly and write them down; do not eyeball it.

- [ ] **Step 3: Document the invocation in `CLAUDE.md`**

Add this to the `## Stack` section, directly after the Python/`uv`/`ruff`/`pytest` bullet, substituting the adopted N:

```markdown
- **Fast local test runs:** `uv run pytest -n <ADOPTED_N> --dist loadfile` (~<X>m vs ~11m serial).
  Each xdist worker starts its own testcontainers Postgres, so workers cannot see each
  other's data. `--dist loadfile` keeps every file's tests on one worker — do **not**
  switch to `--dist loadgroup`, which scatters all unmarked tests per-test.
  Deliberately NOT in `addopts`: CI runs on a 2-4 core runner where this is slower.
```

- [ ] **Step 4: Prove repeatability — three consecutive runs**

Parallel failures are frequently intermittent, so one green run is not evidence.

```bash
cd /home/parlar_ai/dev/call-monitor
for i in 1 2 3; do
  nohup uv run pytest -n <ADOPTED_N> --dist loadfile -q > /tmp/suite-accept-$i.log 2>&1 &
  until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 10; done
  echo "=== run $i ==="; tail -2 /tmp/suite-accept-$i.log
done
```

Expected: all three report `3357 passed, 1 skipped`. **Any variation between runs — a different count, a different failure, an error in one run only — fails this task.** Report it rather than re-running until it goes green.

- [ ] **Step 5: Confirm serial is untouched**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -n "addopts" pyproject.toml || echo "no addopts — correct"
nohup uv run pytest -q > /tmp/suite-serial-final.log 2>&1 &
until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done
tail -2 /tmp/suite-serial-final.log
```

Expected: no `addopts` line, and `3357 passed, 1 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add CLAUDE.md
git commit -m "test: adopt -n <ADOPTED_N> for local parallel runs

Measured, one full suite per configuration:
  -n 4:  <T4>s
  -n 8:  <T8>s
  -n 16: <T16>s
Serial baseline: <TS>s.

Adoption rule (spec 2.3): floor -n 4, promote only on a >=15% gain over the
next-smaller N. <state which promotions qualified and which did not>.

Three consecutive runs at -n <ADOPTED_N>: 3357 passed, 1 skipped each.
Serial unchanged at 3357 passed, 1 skipped; no addopts, so CI is unaffected."
```

---

## Done when

- `uv run pytest -n <ADOPTED_N> --dist loadfile` reports **3357 passed, 1 skipped** three times running.
- `uv run pytest` (serial, as CI runs it) reports **3357 passed, 1 skipped**.
- `uv lock --check`, `uv run python scripts/gen_no_build.py --check`, `uv run pip-audit`, and `uv run ruff check .` all exit 0.
- `CLAUDE.md` documents the invocation and the adopted worker count.
- No `addopts` entry exists in `pyproject.toml`.
