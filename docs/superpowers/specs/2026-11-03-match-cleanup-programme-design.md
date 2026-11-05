# Match clean-up programme — design

Date: 2026-11-03. All numbers measured at `dfb6dbae` unless labelled otherwise.

## Goal

A developer new to the project can read the match app's code and change it safely
within a week.

The user set this bar on 2026-11-03: other developers will join or take over the code.

## Decisions (user, 2026-11-03)

- **Step by step, in place. Not a rewrite.** The base is sound: the 2026-10-31
  usability review recommends keeping FastAPI, PostgreSQL, the ingestion/ranking
  pipeline and the React match UI. The problems are on the surface (see Baseline).
  A rewrite would have to rediscover fixes that each came from a real bug: the
  S1/S3/S5/S7 security fixes, the password-reset race, the EU portal's SEDIA request
  rules, ranking freshness, the hosted-ranking daily cap. It would also have to keep
  the same database, so the data design stays either way.
- **The proposal workspace leaves master.** It matters for a future version, months
  away. It is frozen on a `workspace` branch. See
  `2026-11-03-workspace-removal-design.md`.
- **Users see the same app.** No new features in this programme.
- **Out of scope:** the Azure + HTTPS deployment, and the bundle-size items of
  `docs/research/2026-11-02-match-and-spa-size-review.md` (see the last section).
- **Each piece has its own spec, plan and branch** under `.worktrees/`. Merging,
  pushing and redeploying the live app happen only when the user says so.

## Baseline

| Measure | Value | How measured |
|---|---|---|
| Python source | 71,067 lines, 311 files | `git ls-files 'src/*.py' \| xargs cat \| wc -l` |
| … of which `proposal_assist/` | 38,414 lines | same, scoped to the folder |
| Python tests | 156,450 lines, 661 files | `git ls-files 'tests/*.py'` |
| … of which `tests/proposal_assist/` | 80,031 lines | same, scoped |
| Frontend source (non-test `.ts`/`.tsx`) | 38,624 lines, 183 files | `git ls-files` minus `.test.` files |
| Python files over 1,000 lines | 12 | `wc -l`, filtered `> 1000` |
| Non-blank Python lines that are comments / docstrings | 16% / 16% | `ast` scan of `src/` |
| Comment or docstring lines citing old work | 411 | lines matching `Task \d\|T\d-\d+\|review 20\d\d\|Plan [A-Z]\b\|\b[0-9a-f]{8}\b\|§\d` |
| Python type checker in CI | none | `pyproject.toml`, `.github/workflows/ci.yml` |
| Python test coverage | not measured | no `pytest-cov` |
| Ruff rule sets | E, F, I, B, UP, SIM, ASYNC | `[tool.ruff.lint] select` |
| Frontend checks | `tsc`, `oxlint --deny-warnings` | `frontend/package.json` |

The comment counts are a rough measure: the regex also matches any 8-character hex
word.

## Pieces

| # | Piece | Done when |
|---|---|---|
| 1 | Move the workspace out | It is on the pushed `workspace` branch. Master has no workspace code, tests, screens or libraries, and one build. A small browser suite still runs against the real backend. All test suites pass. Operational commands and generated files match the new build. See its own spec. |
| 2 | Safety tools | A Python type checker runs in CI over `src/` and passes. CI measures Python test coverage (line, branch, and per package) and records the numbers. |
| 3 | Clean the code | The duplicated logic named in the 2026-10-31 and 2026-11-02 reviews is merged. Each file has one clear job. A file over about 600 lines is a review trigger, not a hard limit: it is either split along a real boundary, or the review records why it stays whole. Comments explain the current behaviour and invariants, not history. The reason behind unusual security or concurrency code stays, written in the present tense. No comment cites old tasks, reviews or plans. Each package starts with a short note on what it does. |
| 4 | Slim the tests | Every removed test that guarded a regression (a permission, a race, a failure path) names either its reason for going or the surviving test that checks the same thing. Line coverage, branch coverage and per-package coverage are not below piece 2's numbers. Each test name states the behaviour it checks. |
| 5 | Docs | A newcomer following only the README, on a fresh machine, gets the app running and the tests passing. A short guide explains how the parts fit. **Handover exercise:** a developer who has not worked on the code follows the docs to trace one call from ingestion to display, makes a small change, and finds and runs the checks that cover it. |

The match web screens are part of pieces 2–5. For example, `TriageFeed.tsx` is
780 lines.

**Amendment (2026-11-04, user decision):** piece 2 also measures the frontend. Piece 4's
"not below piece 2's numbers" means the floors in `coverage-floors.json`, both the
`backend` and `frontend` sections.

**Amendment (2026-11-05, user decision):** piece 3 deletes well-covered code, which lowers
a group's percentage without any code losing its tests. Piece 3 may therefore lower a
floor, but only when no group has more uncovered lines or branches than at the slice's
start (`2026-11-05-clean-code-design.md`, "Rules for every slice"). Piece 4's bar is the
floors as they stand after piece 3.

## Order

1. **Piece 1 first.** It roughly halves the code and tests every later piece touches.
2. **Piece 2 before pieces 3 and 4.** The type checker is the safety net for
   refactoring, and the coverage number is the safety net for deleting tests.
3. **Piece 5 last.** Docs describe the final shape.

## The 2026-11-02 size review

Its measurements were checked against the source and the live app, and hold. Its
bundle-size items are not goals of this programme, because they gain little here:

- The live app gzips its assets (239,950 bytes for the main JS, measured over HTTP
  from the running app). It serves them with
  `Cache-Control: public, max-age=31536000, immutable`, so each browser downloads
  them once per rebuild.
- The users are few, and on the hospital network.
- Loading routes on demand adds a failure: `scripts/package_spa.sh` runs `rm -rf` on
  the served folder, so a tab left open across a rebuild would request chunks that
  no longer exist.

Its backend items 1–2 feed piece 3: the save/hide/undo endpoints in
`web/api/calls.py`, and the repeated profile input fields. So does its frontend
item 5, the decision and note code. Its backend items 3–4 are low value. They
would rewrite working, tested security code (the ownership-exclusion helper and the
SSRF-guarded downloads) to save about 20 lines each.
