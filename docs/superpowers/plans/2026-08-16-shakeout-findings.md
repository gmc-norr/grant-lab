# pytest-xdist shake-out findings

**Date:** 2026-08-16
**Branch:** `test-parallelization`, off `master` @ `8478c542c05f298db45ca6b1c702c6b6c749a97f`
**Task:** Task 1 of the test-suite-parallelization plan (adds `pytest-xdist`, discovery only — no fixes here)

## Dependency added

- `pytest-xdist==3.8.0` (+ `execnet==2.1.2`) added to `[dependency-groups] dev` in `pyproject.toml`.
- `no-build-package` regenerated via `uv run python scripts/gen_no_build.py` — `execnet` and `pytest-xdist` added to the wheels-only allowlist.
- `exclude-newer` (`2026-08-03T00:00:00Z`) untouched; resolution succeeded without it.
- All four dependency gates pass: `uv lock --check` (0), `uv run python scripts/gen_no_build.py --check` (0), `uv run pip-audit` (0, no known vulnerabilities), `uv run ruff check .` (0).
- `[tool.pytest.ini_options] addopts` in `pyproject.toml` is unchanged — CI's `uv run pytest -v` stays serial.

## Serial baseline (Step 2)

Command: `uv run pytest -q`

```
3357 passed, 1 skipped, 263 warnings in 704.17s (0:11:44)
```

Exact match to the required baseline (`3357 passed, 1 skipped`). Wall-clock (704s / 11:44) is slightly above the plan's "around 660-690s" estimate but well within normal run-to-run variance on a shared machine; the pass/skip counts are the binding comparison, per the brief, and they match exactly.

Collection check (Step 6): `uv run pytest --collect-only -q` reports `3358 tests collected`, matching `3357 passed + 1 skipped`. `import xdist` succeeds, version `3.8.0`.

## Shake-out run (Step 7)

Command: `uv run pytest -n 8 --dist loadfile -q`

```
3357 passed, 1 skipped, 283 warnings in 101.60s (0:01:41)
```

Pass/skip counts are **identical** to the serial baseline: `3357 passed, 1 skipped`. Wall-clock dropped to 101.60s (0:01:41) — reported for completeness only; per the task instructions this is a probe at a fixed, unrepresentative worker count (`-n 8` on a 72-core box) and is **not** a recommendation for the adopted worker count. Task 3 measures and picks that value.

### FAILED/ERROR nodeids

**Empty. Zero `FAILED` or `ERROR` nodeids were produced by the shake-out run.**

This is a checked result, not an unchecked default. Verification performed on `/tmp/suite-shakeout.log` (5432 lines):

1. Brief's exact command: `grep -E "^(FAILED|ERROR)" /tmp/suite-shakeout.log | sort -u` → no output.
2. Looser sweep for the literal strings `FAILED` / `ERROR` anywhere in the log, filtered for warning/traceback noise → no output.
3. Grep for pytest's `short test summary`, `= FAILURES =`, `= ERRORS =` section headers, and lines starting with bare `FAILED`/`ERROR` → no output (no such sections were emitted at all, meaning pytest itself did not consider any test failed or errored).
4. Cross-checked the terminal summary line directly: `3357 passed, 1 skipped, 283 warnings in 101.60s` — no `failed`/`error` component present.
5. Confirmed no xdist worker crashed silently: total pass/skip count matches the serial baseline exactly, which would not hold if a worker had died mid-run and dropped its assigned tests.

Because the list is empty, the "does it reproduce serially" step (Step 8's second part) is not applicable — there is nothing to reproduce.

### Observation (not a failure, recorded for context)

The shake-out run's warning count is modestly higher than serial (283 vs 263 total warnings; 272 vs 262 `ResourceWarning`s specifically, both about `psycopg.Connection` objects being garbage-collected while still open). This is **pre-existing test-hygiene debt, not a parallelism-induced defect**: the same `ResourceWarning` class appears in the serial baseline log too, in comparable proportion to the test count. It does not affect pass/fail status in either run and produced no `FAILED`/`ERROR` nodeid. Left as-is; not in scope for this task or for Task 2 (which consumes parallel-only *failures*, and this is neither parallel-only nor a failure).

## Bottom line for Task 2

**Nothing failed under `-n 8 --dist loadfile`.** The list of parallel-only failures Task 2 is meant to consume and fix is empty. Task 2 can proceed directly to whatever verification/closure work it has, with no test fixes required from this shake-out. Task 3 should still do its own worker-count measurement independent of this run's `-n 8` figure.

## Gotcha encountered during this task (for whoever runs Task 2/3 detached polling loops)

The brief's suggested poll idiom `until ! pgrep -f "bin/pytest" > /dev/null 2>&1; do sleep 20; done`, when itself executed as part of an `eval`-wrapped background script, can self-match: `pgrep -f` searches full command lines, and the wrapping shell process's own argv contains the literal substring `bin/pytest` (because the pgrep pattern is part of that same script's text). Two long-dead orphaned polling loops from an unrelated earlier session were observed still spinning on the box for exactly this reason. This run used `while kill -0 <real-pytest-pid> 2>/dev/null; do sleep 20; done` instead (PID-existence check, no text matching, cannot self-match) and confirmed completion correctly both times. Recommend the same substitution for Task 2/3's own detached runs.
