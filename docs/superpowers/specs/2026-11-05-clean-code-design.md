# Piece 3: clean the code — design

Date: 2026-11-05. Numbers measured at master `88bdeea2` unless labelled otherwise.
Programme: `2026-11-03-match-cleanup-programme-design.md` (piece 3's done-check is in its
Pieces table).

## Goal

A developer new to the project can read the match app and change it safely. For piece 3
that means four things:

- each rule is written down once;
- comments explain what the code does now, and why;
- each package says what it is for;
- no file or function is too long to hold in your head without a written reason.

Users see the same app. The API stays the same.

## Decisions (user, 2026-11-05)

- **Three slices, one spec.** Each slice has its own plan, branch, reviews, draft PR and
  deploy:
  - slice 1: repeated and dead code;
  - slice 2: comments and package notes;
  - slice 3: splitting big files and functions.
- **Order 1 → 2 → 3.** Removing code first leaves fewer comments to rewrite, and shorter
  comments leave fewer files to split.
- **Clean it, then lock it.** Each slice ends with an architecture test that keeps its
  result true. The repo has drifted back before: CLAUDE.md records a test count that
  stayed wrong for weeks.
- **Functions too.** A Python function over 150 lines gets the same rule as a file over
  600 lines.

## Baseline

| Measure | Value | How measured |
|---|---|---|
| Python source | 25,303 lines, 120 files | `git ls-files 'src/*.py' \| xargs cat \| wc -l` |
| Frontend source (non-test, without `schema.gen.ts`) | 7,979 lines, 70 files | `git ls-files` minus `.test.`/`.spec.` files |
| Python files over 600 lines | 8 | `wc -l`, see slice 3 |
| Frontend files over 600 lines | 2 (+ the generated `schema.gen.ts`) | same |
| Python functions over 150 lines | 17 | `ast`, `end_lineno - lineno + 1` |
| Comment and docstring lines, `src/` + `scripts/` | 8,504 | `tokenize` comments + `ast` docstrings |
| … matching the programme's old-work regex | 174 lines in 58 files | `Task \d\|T\d-\d+\|review 20\d\d\|Plan [A-Z]\b\|\b[0-9a-f]{8}\b\|§\d` |
| … matching any broad history pattern | 477 lines in 83 files | the regex above, plus review/plan/task words, decision and finding ids such as `(D9)` or `M13`, "used to", "no longer", ISO dates |
| Frontend comment lines (non-test) | 1,654 | `grep -cE '^\s*(//\|/?\*)'` |
| Python packages without a docstring note | 13 of 16 | `ast.get_docstring` of each `__init__.py` |
| Frontend folders without a note | 6 of 6 | `src/`, `api/`, `components/`, `routes/`, `shell/`, `theme/` |

The broad patterns over-count: an ISO date or the word "plan" is sometimes current
behaviour. They are a reading list, not a defect count.

## Slice 1: repeated and dead code

Each rule ends up in one place. The endpoints, their status codes and their error
messages do not change.

1. **Decision writes in `web/api/calls.py`.**
   - Today:
     - `hide_call` and `save_call` are the same upsert, differing only in the state
       value.
     - `unhide_call` and `unsave_call` are the same state-scoped delete.
   - After:
     - one shared "set decision" upsert and one shared "undo decision" delete;
     - the four endpoints stay, as thin callers;
     - `hide_call` and `save_call` keep calling `_readable_active_profile_or_404`
       themselves, before the shared upsert. The check stays visible at the endpoint,
       and the delegation pins in
       `tests/architecture/test_profile_access_single_source.py` keep passing
       unchanged.
   - These rules stay:
     - a set goes through `_readable_active_profile_or_404`;
     - an undo deletes only the caller's own row, on that profile, in that state;
     - an undo deliberately does not check readability, so a decision stays undoable
       after its profile is deactivated or unshared;
     - the note is cleared only on a real state flip.
2. **Profile inputs in `web/api/profiles.py`.**
   - Today:
     - `ProfileCreateIn` and `ProfileUpdateIn` both declare the same 13 structured
       fields;
     - two 13-line dictionaries pass those fields to `parse_structured`.
   - After:
     - one shared field declaration;
     - one conversion into the existing parser.
   - Validation errors stay the same, and so does the split between strict user input
     and lenient bootstrap data.
   - The OpenAPI field order may change. `schema.gen.ts` is regenerated, and only its
     order may differ.
3. **Scheduler configuration.**
   - Today, `build_scheduler` receives `Settings` and also ten values taken from it.
   - After, it receives `Settings` once, while the clients are still injected
     explicitly.
   - The freshness rule stays: enqueue and drain use the same effective scorer
     configuration.
4. **Dead code.**
   - `sources/extraction.py` (251 lines) and `tests/test_sources_extraction.py`
     (302 lines) are removed. No production module imports `sources.extraction`; only
     its own test does.
   - `pymupdf` and `trafilatura` are imported nowhere else. They leave the
     dependencies, along with their entries in the no-build allowlist in
     `pyproject.toml`. That list is regenerated with `scripts/gen_no_build.py`.
   - **`lxml` becomes a direct dependency.** `sources/eu_portal.py` imports it, but it
     was installed only as a dependency of `trafilatura`. Without this, removing
     `trafilatura` would break the EU source.
   - **Kept, not dead:** the calls list's `min_score` and `view` query parameters. Both
     are working API behaviour with regression tests, and `archive` is the default view.
     The API stays the same, so they stay, even though no match screen sends
     `min_score`.
   - **Removed (user decision, 2026-11-05): the CSP `ws_origin` token**
     (`main.py::_derive_ws_origin`, used by `web/middleware.py`).
     - It adds a same-origin `ws(s):` source to `connect-src` in non-dev mode. No
       frontend code opens a WebSocket any more.
     - Removing it tightens the production header to `connect-src 'self'`. The PR
       lists this change.
     - The startup check of the `APP_BASE_URL` host must survive. The prototype found
       that `_derive_ws_origin` was its only home: `_derive_form_action_origin`
       validates the Google discovery URL, and only when Google sign-in is on. So the
       check stays, as `main.py::_validate_app_base_url`, called unconditionally from
       `create_app`, with the same errors. A new test pins that wiring. Before, only the
       ws-token assertions guarded it, by accident.
5. **Feed decision code** in `TriageFeed.tsx`, `TriageCard.tsx`,
   `DecisionNoteStrip.tsx` and `CallDecisionActions.tsx` (1,630 lines together).
   - Repeated save/hide/undo dispatch and mutation state are merged only where the same
     rule is written twice.
   - A move that removes no rule is skipped, and the slice records why.
   - Feed cursor and keyboard handling stay in the feed.
   - These protections stay:
     - writes are bound to their profile, call and decision;
     - note saves are serialized;
     - the note is cleared only on a real state flip;
     - focus is safe during refetches;
     - background fetching is kept separate from mutation blocking.
   - **Outcome of the prototype (2026-11-05): skipped.**
     - Two merges were built and tested at `3c1c0f31`:
       - a shared hook plus one combined undo: +3 lines, 5 rules de-duplicated;
       - the combined undo alone: −6 lines, 2 rules.
     - The repeated rules are one-line expressions, so a shared home costs as much as it
       saves.
     - The larger duplicates cannot be merged without a test change or a visible
       change: the `expectedState` binding on note saves, the note-editor toggle, and
       the error wording and clearing.
   - **A real bug found on the way** (spec rule: its own test and fix commit, reported
     to the user).
     - In the feed, a failed save or hide leaves its red alert ("Your call is
       unchanged") up after a successful undo.
     - `TriageFeed::decide` resets the decision mutations, but the undo paths do not.
       The call page clears its error correctly.
   - The call page's decision code has test gaps. The worst: swapping its Save and Hide
     endpoints passes every test. These go to piece 4.
6. **Lock:** an architecture test that `sources/extraction.py`, `pymupdf` and
   `trafilatura` stay gone from the code and the dependencies.

## Slice 2: comments and package notes

**The rule for every comment and docstring in scope:**

- Say what the code does now, and why, when the why is not obvious.
- Keep the reason behind unusual security or concurrency code, in the present tense.
  For example: "undo skips the read check, so a decision stays undoable after its
  profile is unshared".
- Remove the history:
  - task, review and plan names;
  - commit hashes;
  - decision and finding ids;
  - "this used to …".

  Git and the documents under `docs/` keep it.
- Shorten a long story to the rule it protects.

**In scope:**

- `src/grant_lab/`;
- `frontend/src/`, except test files and the generated `schema.gen.ts`;
- `scripts/`;
- `.github/workflows/ci.yml`;
- the stale comments in that code already listed after pieces 1 and 2.

**Out of scope:**

- tests, which belong to piece 4;
- `alembic/` migrations, which are history by design.

**Generated types:** FastAPI turns route docstrings into OpenAPI descriptions, and
`frontend/src/api/schema.gen.ts` embeds them. At `88bdeea2`, 27 of its lines match
`Task \d|T\d-\d+|review 20|\(D\d+\)|Plan [A-Z]\b|§\d`, for example "(D7)", "(D18)"
and "(Task 5)". Slice 2 therefore regenerates
`schema.gen.ts` and is never hand-edited. Its diff may change only description comments.
The OpenAPI drift check must pass.

**Package notes:**

- **Python:** every package's `__init__.py` gets a docstring of 2–6 lines: what the
  package does, and where to start reading.
- **Frontend:** each of `frontend/src/`, `api/`, `components/`, `routes/`, `shell/` and
  `theme/` gets a short `README.md` with the same content.

**Method:** one reader goes through every comment in the in-scope files. This is a
judgment pass, not a search-and-replace. The 477-line broad list is the reading order,
not the whole job.

**Locks:**

- An architecture test fails when an in-scope comment cites old work in a clear shape:
  - a task or plan name;
  - a review date;
  - a decision or finding id in brackets;
  - a commit hash.

  Its patterns are chosen so the cleaned tree has zero hits and no false alarms. It is
  run against planted bad comments, and the run log records them. Vague history words
  such as "used to" are left to the reader, because a pattern would also flag good
  comments.
- An architecture test fails when a Python package has no docstring note or a frontend
  folder has no `README.md`.

## Slice 3: big files and functions

Slice 3 starts by measuring again, because slices 1 and 2 change the sizes. The likely
cuts at `88bdeea2`:

| File | Lines | Likely split |
|---|---|---|
| `web/api/boards.py` | 1,316 | boards (list, create, read, update, delete, publish, preview) and board items (queue, list, approve, reorder, remove) |
| `web/api/calls.py` | 1,045 | reading calls (list, detail) and decisions (save, hide, undo, note, hidden list, counts) |
| `web/api/profiles.py` | 976 | profiles (create, read, update) and sharing (viewers, visibility, owner transfer) |
| `scheduler.py` | 866 | the single-instance lock (`SchedulerLock`, probe, enforce, watchdog) and the job wiring |
| `sources/eu_portal.py` | 821 | measure again; one source adapter may stay whole, with its reason recorded |
| `web/dashboard.py` | 762 | measure again |
| `embeddings/refresh.py` | 679 | measure again |
| `main.py` | 640 | the lifespan in its own module |
| `frontend/src/routes/TriageFeed.tsx` | 773 | parts with a real seam |
| `frontend/src/routes/BoardDetail.tsx` | 649 | parts with a real seam |

The 17 Python functions over 150 lines get the same treatment: split into named steps,
or record why they stay whole. The longest are:

- `scheduler.py::build_scheduler` (531);
- `ranking/worker.py::drain_queue` (387);
- `main.py::create_app` (343);
- `digest/assemble.py::assemble_for_user` (325);
- `web/dashboard.py::build_dashboard_view` (281).

**Proof that behaviour did not change:**

- The OpenAPI document is the same before and after, compared with keys sorted; route
  order may differ. Operation ids stay the same, because function names and paths stay.
  `schema.gen.ts` is regenerated, and its diff may change only the order of entries.
- The full backend and frontend suites pass, and so do both browser suites.
- mypy reports 0 errors, and the floors hold.
- Tests change only where code moved or a helper was renamed. See "Tests that may
  change" under "Rules for every slice".
- `docs/ARCHITECTURE.md` is regenerated in the same commit.

**Lock:** an architecture test fails when any of these is over its limit and not on a
short allowlist:

- a Python file in `src/` over 600 lines;
- a non-test frontend file over 600 lines (`schema.gen.ts` is exempt);
- a Python function over 150 lines.

Each allowlist entry carries a written reason. An entry whose target is now under the
limit also fails, so the list cannot go stale.

## How each slice ships

1. A fresh branch and worktree under `.worktrees/`, from master. Measure that slice's
   baseline first: test totals, coverage, sizes, comment hits.
2. Subagent-driven execution with a review after each task. Reviewers prove claims by
   mutation, not only by reading.
3. Before the draft PR, all of these pass:
   - the full backend suite;
   - vitest;
   - `e2e:match` (production bundle, API mocked);
   - mypy;
   - ruff;
   - the coverage floors.
4. The slice's new lock is seen red once, against a planted bad case. The run log
   records how.
5. A final whole-branch review on the most capable model, then its fixes.
6. A draft PR, with CI green except `dependency-review`, which fails on this private
   repo.
   - **The real-backend smoke runs here:** CI's `e2e (prod)` and `e2e (dev)` jobs,
     which run `scripts/e2e_run.sh` with a real backend and browser. `e2e (prod)` runs
     under the production CSP.
   - It cannot run locally. The script binds port 8000, which belongs to the live app,
     and its preflight refuses to start while the app runs. Piece 1 used the same
     arrangement.
   - Every coverage report that lowers a floor, CI's included, must first pass the
     per-line check against the slice baseline. CI's reports are read against the
     pull request's merge commit, `refs/pull/N/merge`.
7. The user decides on the merge and deploy. The deploy recipe is unchanged: backup,
   stop, fast-forward master, sync/build/package, start, push master.

## Rules for every slice

- No new features and no user-visible change. The API is unchanged.
- **No code loses its tests (user decision, 2026-11-05).** The measure is the count of
  *uncovered* lines and branches per group, not the percentage.
  - Deleting well-covered code lowers a group's percentage while no remaining line loses
    coverage. For example, deleting `sources/extraction.py` (84/86 lines, 23/24 branches
    covered) takes `sources` from 92.8% to 92.0% of lines (465/505) and from 84.6% to
    83.3% of branches (165/198), measured from CI's report at `88bdeea2`. Its floors are
    92.8 and 84.6.
  - A floor may therefore go down, but only when **no surviving line or branch became
    uncovered**. Totals are not enough: deleting `extraction.py` also removes 2
    uncovered lines and 1 uncovered branch, which would let 2 lines elsewhere in
    `sources` lose their tests while the group's total stays level.
  - The check is per line. It is `scripts/compare_uncovered.py`, added in slice 1 with
    its own tests, and it reuses the floors checker's grouping.
    - Every line uncovered after the slice must match a line uncovered before it, in
      the same group.
    - Every uncovered branch after must match one before, by the source text of its
      from-line and to-line.
    - Matching first lines up the two revisions of each file with a line diff. A line
      the diff keeps is "in place". A branch is in place when both its lines are.
    - An uncovered item at the same place as an equal uncovered item before is
      matched.
    - An uncovered item in place that was not uncovered before is new. The line was
      there and lost its tests. A same-text line elsewhere cannot excuse it.
    - A line that was uncovered before and is still in place is covered now. It
      excuses nothing.
    - Only code the diff did not keep can have moved: lines that were deleted on the
      "before" side, and lines that were added on the "after" side. Only these match
      by text, counted as a multiset: in the same file first, then in other files of
      the group. Matches in another file are listed for review as moves.
    - Deleted code is simply absent from the "after" side, so it can offset nothing.
    - A move across groups counts as new.
    - Every file measured before that still exists after must be in the after report.
      Otherwise the check fails as bad input. A file the slice deleted may be absent.
    - Limit: when same-text lines sit close together, the diff picks which one stayed.
      It can be wrong in either direction. A wrong pick can report a moved line as new,
      or let a lost line match. A line reported as new is fixed with a test or explained
      in the run log, never by a lower floor.
  - **Frontend reports:** the check needs Istanbul's `coverage-final.json`, so slice 1
    adds the `json` reporter to `frontend/vite.config.ts`.
  - **Frontend branches:** v8 lists no branch arms inside a function that never ran.
    So an arm whose from-line was uncovered before is not counted as new: that code had
    no tests to lose. This is checked before the in-place rule. The frontend branch percentage can even rise while lines lose
    their tests, so it cannot detect lost tests. The per-line check can.
  - The run log records the script's output for both sections. The check is shown
    going red on a planted lost test. Any new uncovered line or branch is fixed with a
    test, never by a lower floor.
  - New floors are written with `--update` from the slice's two local runs before the
    draft PR. The PR's CI report is then checked against them; if it measures lower,
    the floors are rewritten from all three reports. Each of the three must first pass
    the per-line check; a report that has not passed it never lowers a floor. The run log records, per group:
    uncovered counts before and after, and the old and new floors.
  - Piece 4's bar ("not below piece 2's numbers") becomes the floors as they stand after
    piece 3.
- **Tests that may change.** Tests are piece 4's job, so a slice changes a test only in
  these five cases:
  1. **A test that patches or imports a moved or renamed symbol** is re-pointed to the
     new module path or name. Its assertions do not change.
  2. **An architecture pin that names a file, function or delegation path** moves with
     the code. Examples:
     - `test_profile_access_single_source.py` pins handlers as
       `web/api/calls.py::hide_call`;
     - `test_boards_permission_single_source.py` scans `web/api/boards.py` alone
       (`_SOURCE`). A split must make it scan every file that holds board routes. Its
       hand-written route set (`_EXPECTED_ROUTES`) is what catches a scan that sees
       too few.

     A pin keeps asserting the same permission check on the same routes; only the path
     or helper name in it changes. No pin is deleted or weakened, and no route leaves
     its coverage. Every changed pin is shown red once against a mutation that drops
     its check, and the run log records it.
  3. **A test whose only subject is deleted dead code** goes with it. Planned:
     - `tests/test_sources_extraction.py`;
     - in `tests/test_security_headers.py`:
       - `test_build_csp_prod_has_same_origin_ws_token` and
         `test_build_csp_http_base_gets_ws_token`, which test only the `ws_origin`
         parameter;
       - the one `ws_origin` assertion in `test_build_csp_dev_output_unchanged`.
  4. **`build_scheduler` call sites (user decision, 2026-11-05).** 15 calls in 8 test
     files (measured by the prototype) pass the ten values that slice 1 removes from the
     signature. They change only in how they pass the same values: through the
     `Settings` they build, with `settings.model_copy(update={...})`. Every assertion
     stays. The reviewer checks each call, value by value. In
     `tests/test_config_escalation_settings.py`, two tests also read the removed
     parameters' defaults with `inspect.signature`. Only that part goes; their pins on
     `Settings`, `drain_queue` and `AnthropicClient` stay.
  5. **Consequences of the user-approved `ws_origin` removal.**
     - `tests/test_spa_serving.py::test_prod_mode_serves_index_for_spa_routes` pins the
       production CSP. Its expected `connect-src 'self' ws://testserver` becomes
       `connect-src 'self'`.
     - `test_ws_origin_injection_rejected_at_startup` and
       `test_ws_origin_whitespace_netloc_rejected_at_startup` guard the surviving
       `APP_BASE_URL` check. They are renamed to `test_app_base_url_…`, and their
       assertions do not change.

  **New tests are allowed** when they guard something the slice depends on:
  - a gate's own tests;
  - a characterization test written *before* a refactor, to close a gap that would
    otherwise let the refactor break something unnoticed;
  - a bug's failing test.

  A new test never replaces or weakens an existing one.

  Any other test change is out of scope. If a refactor seems to need one, the refactor
  is reconsidered first, and the change is reported to the user.
- **A real bug** found on the way gets its own failing test and its own fix commit, and
  is reported to the user. It is never folded into a clean-up commit.
- Merging, pushing and deploying happen only on the user's word.

## Out of scope

- Tests, which are piece 4, and migrations.
- The 2026-11-02 size review's backend items 3–4: the ownership-exclusion helper and
  the shared source download. The programme rates them low value. The download item
  disappears anyway with `sources/extraction.py`.
- The bundle-size items. See the programme spec.
- The piece 2 gate follow-ups (M1–M6 of the piece 2 final review), which stay on their
  own list.
- Documentation beyond the package notes, which is piece 5.
