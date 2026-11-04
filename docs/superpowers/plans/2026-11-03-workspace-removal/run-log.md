# Workspace removal — run log

Measured numbers only. Every line was produced by a command in the plan.

## Task 0 — baseline (2026-11-04)

- BASE = `fca2c678` (master tip; branch `chore/workspace-removal` in `.worktrees/workspace-removal`)
- Backend suite (`uv run pytest -n 16 --dist loadfile -q`): **6178 passed, 1 skipped, 6 xfailed** in 175 s, exit 0
- Collected tests: **6185** in **614** files (per-file list: `/tmp/wr-base-per-file.txt`)
- Frontend: `tsc --noEmit` exit 0; `lint` exit 0; vitest **157 files, 1371 tests** passed
- `e2e:match`: **38 passed** (mocked API, static server on :4173)
- Real-backend base: port 8000 held by the live app (`ss -ltn | grep -c ':8000 '` = 1), so not run locally.
  Master's last CI run: `41259468734`, head `dfb6dbae`, conclusion **success**.
- G1 must-reject baseline: `proposal_assist` outside the package = **192 lines in 29 files**
- Python files in `src/`: **311**; files in `tests/proposal_assist` + `tests/fencing`: **396**
