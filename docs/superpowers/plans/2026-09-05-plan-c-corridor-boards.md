# Plan C — Corridor Boards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the corridor board — `boards` and `board_items`, an admin approval queue that applies the profile owner's hides, a userless public endpoint, and an unattended `/b/:slug` screen — plus profile ownership transfer, which the board's alerting depends on.

**Architecture:** Two new match-edition tables and one new column on `subscriptions`. The approval queue **calls** `dashboard._decision_predicate` with the profile owner's id rather than growing a second copy; the public query is genuinely userless and joins `board_items → calls` only. QR codes are encoded server-side with `segno` and travel as SVG path data inside the cached payload, so a network drop cannot leave dead QRs on a wall. The `/b/:slug` screen is a third pathless React layout with its own fetch loop that reads HTTP status directly, because the shared `unwrap()` cannot tell a 404 from a 502.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · Alembic · PostgreSQL 16 · `segno` (new) · React 19 + Vite · Mantine 9 · TanStack Query · react-router 7 · `openapi-fetch`.

---

## Global Constraints

Every task's requirements implicitly include this section.

### Boundaries — crossing one is an ESCALATE, not a decision

1. **`master` must be `05bd151c` at every moment.** Never merge, never push, never open a PR, never delete a worktree, never run a finishing/cleanup skill. The human merges.
2. **NEVER read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** A live API key was leaked this way once. If a test needs an env var, set it inline in the command. `.env.example` is a different, generated, committed file and is fine.
3. **`docs/superpowers/specs/` is INPUT.** Byte-identical at every commit. A wrong spec is a `PLAN-DEFECTS.md` entry, not an edit.
4. **The backend suite's `1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic API call ran — stop and record it. Never export `ANTHROPIC_API_KEY`.
5. **The `full` edition keeps working and the `match` edition stays clean.** `tests/architecture/` must stay green. A board is a **MATCH-edition feature**: its routers go in `_ALWAYS`, and no boards module may import anything under `grant_lab.proposal_assist`.
6. **`docs/ARCHITECTURE.md` is generated.** Any model change regenerates it in the SAME commit: `uv run python scripts/gen_architecture.py`. Never hand-edit it.

### Measured baselines — these numbers, not the ones in any other document

Measured in this worktree at `05bd151c` on 2026-09-05. Raw output in `/home/parlar_ai/plan-c-run/PLAN-AUTHORING-MEASUREMENTS.md`.

| Gate | Command | Baseline |
| --- | --- | --- |
| Backend | `uv run pytest -n 16 --dist loadfile -q` | **5137 passed, 1 skipped**, exit 0 (126 s) |
| Frontend unit | `pnpm vitest run` (in `frontend/`) | **140 files, 1062 tests**, exit 0 |
| Frontend build | `pnpm -C frontend build` | exit 0 |
| Backend lint | `uv run ruff check src tests alembic scripts` | `All checks passed!` |
| Frontend lint | `pnpm lint` (in `frontend/`) | exit 0 |
| Full-edition route paths | `wc -l tests/architecture/full_edition_routes.txt` | **109** |
| Migration head | `ls alembic/versions/ \| sort \| tail -2` | **0071** |

**Every task states a DELTA against these.** Never restate an absolute total copied from anywhere, including this table — re-derive it.

### Environment set-up a fresh worktree needs

```bash
cd <worktree>
uv sync --python 3.12
cd <worktree>/frontend && pnpm install --frozen-lockfile
```

**Both are required.** `frontend/node_modules` is gitignored, so a fresh worktree has none and `pnpm vitest run` fails with `ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL Command "vitest" not found` — which is a **missing install**, not the dependency cooldown. `pnpm install --frozen-lockfile` is a lockfile install and the 7-day `minimumReleaseAge` cooldown does not block it (measured: `Done in 1.5s`).

Verify the interpreter before measuring anything:

```bash
cd <worktree> && uv run python -c "import sys; print(sys.executable)"
```

It must resolve under the worktree. If it does not, stop and fix it.

### Fixture names — measured, because a wrong one costs a whole task

There is **no `db_session`, no `client` and no `client_unauth` fixture in this repo.**
Measured in `tests/conftest.py` at `05bd151c`:

| Fixture | Line | Shape |
| --- | --- | --- |
| `pg_session_factory` | `:151` | a **factory** — `with pg_session_factory() as s:` |
| `pg_url` | `:117` | the testcontainers URL, for `monkeypatch.setenv("DATABASE_URL", pg_url)` |
| `profile_factory` | `:184` | returns a `(profile_id, user_id)` **tuple**; takes no `visibility` or `created_by` |
| `test_client_factory` | `:68` | creates entered `TestClient`s and closes their lifespans |

The authenticated-request pattern is `_setup(monkeypatch, pg_url) -> app`, then a
`TestClient` with `app.state.session_signer.make(user_id=uid)` in the `session` cookie —
copy it from `tests/test_api_profile_detail.py:8-20`, which is the shortest live example.

`ProfileKind` is a `StrEnum` with exactly two values, `organizational_unit` and
`individual` (`db/models/profiles.py:31-33`). There is no `research_group`.

**The first draft of this plan used `db_session` throughout.** It was caught by opening
`tests/conftest.py` rather than by reasoning, which is the point.

### False-pass signatures in this repo

- **`pytest` exits 4 on "no tests ran".** Exit 4 is not success.
- **A broken import reddens the suite as teardown ERRORs with ZERO FAILED**, which reads like a passing mutation. **Judge on FAILED, never on ERROR.**
- **NEVER run `ruff format` on a directory** — it reformatted 57 untouched files into a working tree once. Format explicit file paths only, and only files you created or edited.
- **`pnpm test run` runs NOTHING** (it becomes `vitest run run`, a filter matching nothing). Use `pnpm vitest run`.
- **`grep -c` exits 1 on no match, and `| cat` masks exit codes.** Do not chain them.
- **Kill background processes by PID, never by pattern** — `pkill -f` matches the caller's own command line.
- **Clear `__pycache__` after restoring any mutated file.** A same-size restore inside the same second leaves a stale `.pyc` and the tests run the OLD code.
- **`git diff base..HEAD` run *on* base compares nothing**, and `git stash` stashes nothing on a committed tree.
- **A skipped test is green**, and so is one gated on an env var nobody sets.

### Measurement discipline

- **Before writing any gate, name what trivially passes it**, then run it against one input it must reject and record that it did. Two lines of evidence: `against good input: PASS` / `against <known-bad>: FAIL, because <reason>`.
- **When you quote a command beside a number, quote it EXACTLY as run, pipes and all**, and re-run that exact string.
- **Never adjust an expected number to make the arithmetic close.** Find the term that changed and name it.
- **Re-derive a count; never increment one.**
- **A verification that reddens nothing is a finding**, not a pass.
- **Your cwd resets between tool calls.** Absolute paths everywhere; `git -C <abs>` for every git command. Subagent git operations default to the MAIN tree's cwd, so force `cd`, use `git -C <abs worktree>`, and verify the branch before any commit.
- **A "prove the gate BITES" step's expected failing set is a FLOOR, not an exact set.** Every `Record:` line below names the tests a mutation is *known* to redden. Seeing MORE red is the gate being stronger than predicted — record the real set and move on. Seeing FEWER, or a different test, is the finding. The dry run hit this three times (Tasks 4, 5 and 6 each named one test where two fail).

### Counting rules this repo's gates impose

Three ways a task's test delta is not the number of test names it wrote. All three were measured, and two of them cost the dry run a debugging cycle.

- **A pin added to `PREDICATE_PINS` or `DELEGATION_PINS` is worth TWO tests, not one.** `test_pinned_function_still_exists` is parametrized over `(*PREDICATE_PINS, *DELEGATION_PINS)` (`tests/architecture/test_profile_access_single_source.py:484-486`) and `test_pinned_delegate_still_delegates` over `DELEGATION_PINS.items()` (`:517-519`), so one dict entry becomes two test IDs. `EXEMPT_PROFILE_FUNCTIONS` is **not** parametrized over — an entry there is worth zero, which is why Task 1's delta is clean and Task 3's is not.
- **Adding a router to `_ALWAYS` requires a matching `_ALWAYS_ROUTERS` edit** in `tests/architecture/test_edition_surface.py:242-249`. `test_the_always_mounted_routers_are_exactly_the_reviewed_set` (`:279-284`) AST-parses `_ALWAYS` and compares **element for element, in order**, so the new entry goes in the same position in both. It adds no tests; it just goes red if you forget. Its own failure message states the intent: "adding one is a product decision: does the match edition need it?"
- **The `schema.gen.ts` drift check is CI-ONLY.** `.github/workflows/ci.yml:112-115` runs `bash scripts/gen_openapi_types.sh` then `git diff --exit-code frontend/src/api/schema.gen.ts`. **`pytest` cannot see a stale generated file** — the whole backend suite stays green. Any task that changes a response model regenerates it in the same commit, including tasks whose route SET does not change.

### What the match-edition process-cut gate does and does not prove

`tests/architecture/test_match_edition_process_cut.py` boots a subprocess and reads `sys.modules` **after `create_app()`, which is BEFORE the FastAPI lifespan runs.** Measured 2026-09-05 under `APP_EDITION=match DRAFTING_ENABLED=false`: **0** proposal-assist modules after `create_app()`, **41** after the lifespan's seven unguarded imports (`main.py:212, 229-233, 251`), and **pycrdt loads**.

- It **is** a real gate for anything reachable from `create_app()` — routers, dependencies, module-level imports. That is exactly where every board module lives, so it bites for this plan and must stay green.
- **Do not write "a match boot imports zero proposal-assist modules" into this plan, a docstring or a commit message.** It is false. Say "constructing the match app imports none", which is what the gate measures.
- Fixing that gate is **out of scope for Plan C.**

### Regeneration steps a change of each kind requires, in the SAME commit

| If a task changes… | Run |
| --- | --- |
| any ORM model | `uv run python scripts/gen_architecture.py` |
| `pyproject.toml` dependencies | `uv lock` (CI runs `uv lock --check`) |
| any route | `bash scripts/gen_openapi_types.sh` |
| `frontend/routes.json` | `uv run python scripts/gen_routes.py` |
| the full edition's route set | regenerate `tests/architecture/full_edition_routes.txt` **and** bump the `109` literal in `test_edition_surface.py::test_the_baseline_is_not_empty` (`:175`) **by hand** |
| any response model (even with no new route) | `bash scripts/gen_openapi_types.sh` — the drift check is CI-only |
| `_ALWAYS` in `web/api/__init__.py` | add the same element, in the same position, to `_ALWAYS_ROUTERS` (`test_edition_surface.py:242-249`) |

`scripts/gen_openapi_types.sh` pins `EMBEDDING_MODEL=bge-m3`, `--python 3.12` and `APP_EDITION=full` for reasons its own header records. Always invoke that script; never a hand-rolled equivalent.

### Commit convention

**Commit titles name the change, not the activity.** `fix(collab): refuse a stale Y.Doc on reconnect` is findable; `Refactor and enhance codebase for clarity` is not. A title that would fit any commit in the repo is the tell.

---

## Where the note overrides the spec — carry these, do not "fix" them

Two deviations are deliberate. Both quote the spec beside them so a later reader does not
"correct" the code back.

### Deviation 1 (D2) — the approval queue is NOT userless

> **Spec §6:** "The same query without the `board_items` join and with a `min_score`
> filter over `call_rankings` feeds the admin approval queue."

**The note overrides this.** The queue applies the **profile owner's** hides:
`user_id = profiles.created_by WHERE profiles.id = boards.profile_id` — **not**
`boards.created_by`, because §4.3 lets any profile *editor* create a board, so an admin
can build a board on a profile they do not own. Only D1's **public** query stays userless.

This makes the queue the **first caller of `dashboard._decision_predicate` outside
`web/dashboard.py`**. Measured at `05bd151c`: `_decision_predicate` (`dashboard.py:64`) has
exactly one call site today, `dashboard.py:322`, inside `_base_call_filters`.

**It is NOT a third caller of `_base_call_filters`.** An earlier revision of this plan said
so and told Task 4 to rewrite that function's docstring accordingly; the dry run measured
that `_base_call_filters` still has exactly two callers — `count_decision_views`
(`dashboard.py:367`) and `build_dashboard_view` (`:503`) — so its enumeration
(`dashboard.py:313-315`) is **true today**, and the instruction would have made it false.
`_base_call_filters` adds only the `source` and free-text filters the queue has no use for,
and inheriting a future dashboard-only filter is coupling, not reuse. Task 4 therefore
calls `_decision_predicate` directly and puts the caller note on **that** function, whose
docstring enumerates no callers at all. See `PLAN-DEFECTS.md` D6.

### Deviation 2 (D7) — orientation is STORED, not inferred from the viewport

> **Spec §6:** "The board renderer picks the default from the viewport aspect ratio and
> the legibility floors (170 px QR, 34 px title) rather than trusting a single number to
> work everywhere."

**The note overrides this.** The author picks portrait or landscape when creating the
board; `items_per_page` follows (4 or 8) and stays an override. The approval cap is
computed from the **stored** value so it is always truthful, and the renderer **obeys**
the stored setting. §6 itself forbids "the display silently truncating a list an admin
approved", which is unavoidable if the renderer decides capacity after approval.

### One spec instruction that is not implementable as written

> **Spec §6:** "The public page is a route under `PreAuthLayout` at `/b/:slug` — that
> layout is already auth-free, so no new auth architecture is introduced."

`PreAuthLayout` is a centred **`maw="22rem"`** card (`frontend/src/routes/PreAuthLayout.tsx:17`)
— 352 px, with a `BrandMark`. Eight 170 px QRs do not fit. §6's *requirement* is
"auth-free, no new auth architecture", and Task 9 satisfies it with a **third pathless
layout** sibling to `AppShell`/`SimpleShell` and `PreAuthLayout` — the same pattern
`PreAuthLayout` itself uses (`router.tsx:162-174`). Only the layout's name is dropped.
Recorded in `PLAN-DEFECTS.md` S1.

---

## Decisions this plan took that the note left open

The note's "Still open" section lists eight items and nobody is available to answer them.
Each is decided below with its reasoning, and each is implemented by a named task.
**Any of these can be overruled in review**; they are separated here so that is cheap.

### Open-1 (BEFORE the schema task) — D9's flag: a NEW boolean, not `digest_enabled`

**Decision:** add `subscriptions.board_alerts_enabled boolean NOT NULL DEFAULT true`.

**Why.** `digest_enabled` is read at `digest/assemble.py:95` to decide whether a profile's
ranked calls appear in the weekly email. Reusing it means muting the weekly digest also
mutes the corridor-board alert — the note's own words: "a different thing with a different
urgency". An owner who has muted a weekly summary still owns a public hospital screen.
The note calls the second boolean "the cheaper honest option"; this plan takes it.

`server_default=text("true")` is load-bearing, not decoration: `scripts/seed_dev_data.py:328`
inserts a `Subscription` with only `user_id` and `profile_id`, so without the server default
every seeded profile would silently opt out of board alerts. See `PLAN-DEFECTS.md` N2.

**Implemented by:** Task 2 (migration 0073), Task 8 (the alert).

### Open-2 — D9's trigger: live approved cards below the board's stored `items_per_page`

**Decision:** the alert fires for a **published** board whose count of *renderable*
approved items (deadline in the future) is **below its stored `items_per_page`**,
evaluated on the existing digest schedule.

**Why.** It is the only one of the note's three candidates that describes the harm D5
names — an underfull screen nobody but a passer-by can see. "A deadline dropped an item"
is a *cause* of that state, so firing on both double-notifies one event. "Waiting-count
threshold" is a different and less urgent condition: a board can be completely full with
200 calls waiting. It is also the cheapest of the three — it reads `boards`, `board_items`
and `calls` only, where the note measured that the waiting-count trigger additionally
touches `call_rankings` and `call_decisions`. And it is evaluated at send time against
live rows, so it cannot fire off a stale snapshot.

**Unpublished boards never alert.** An unpublished board is not on a wall; §4.3's slug
resolves to a 404.

**Implemented by:** Task 8.

### Open-3 — D9's escalation: escalate to active admins, and make the suppression visible

**Decision, two halves:**

1. When the computed recipient is undeliverable — `User.status != 'active'` **or**
   `digest_disabled_at IS NOT NULL`, the two conditions `digest/send.py:38` filters on —
   the alert is sent to **every active admin** instead.
2. The suppression is **recorded and shown in-app** on the board's detail screen
   ("the owner cannot be notified by email"), not merely re-routed.

**Why escalate rather than make transfer a precondition of disabling a user.** Blocking
`admin_disable_user` until every owned profile is transferred puts a new failure mode into
the offboarding path — a security-relevant route — and it does **nothing** for the
bounce-disabled owner, who has not left and whom the note explicitly says D10 must not
"fix" by transfer. Escalation covers both cases with one mechanism. §4.3 already gives
every admin edit rights on every profile, so an escalated admin can act.

**Why half 2 is not optional.** The note: "Nothing anywhere reports that a notification had
no recipient… a delivery the system knows it suppressed is the one case where an in-app
surface is not optional."

**Implemented by:** Task 8.

### Open-4 — D7's flip: REFUSE a downward flip that would strand approved items

**Decision:** changing orientation landscape → portrait on a board whose approved count
exceeds the new capacity returns **409** naming the number to remove
(`"this board has 24 approved calls; portrait holds 12 — remove 12 first"`).
Portrait → landscape only *raises* capacity and is always allowed.

**Why.** §6 forbids "the display silently truncating a list an admin approved: they would
have no way to see the loss." Auto-dropping the lowest-positioned items on flip is exactly
that loss, moved from render time to save time — the human still never sees it. Refusing
and naming the count keeps the decision where D7 put it: with a human, before the cap
matters.

**Implemented by:** Task 5.

### Open-5 — D4's boundary: a private profile's board MAY be published

**Decision:** publication is allowed regardless of `Profile.visibility`. The admin's
publish confirmation states what becomes public and what does not.

**Why.** D4 already establishes that `visibility` governs reading the profile *through the
app's read surfaces*, and §6's "deliberately not public" list includes "No profile text or
profile identity beyond the board's own title" — so a published board reveals no profile
content whatever its visibility. Refusing publication would let a non-admin's personal
privacy setting veto an admin's publication decision, the exact authority inversion D4
rejects for the already-published case; the same answer for both keeps one rule.

The carried caution stands: the board's **title** is free text and may name the group. The
title field carries that warning at the point it is set (Task 5), and the publish
confirmation repeats it (Task 6).

**Implemented by:** Tasks 5 and 6.

### Open-6 — the outgoing owner keeps read access only if the transfer says so

**Decision:** `PUT /api/profiles/{profile_id}/owner` takes `keep_read_access: bool`,
default **`false`**. When `true`, the transfer writes a `ProfileViewer` row for the
outgoing owner **in the same transaction**. The UI's warning is computed from the actual
post-transfer predicate, never from a fixed sentence.

**Why.** D10 settles the mechanism and leaves the policy open. Granting read silently
contradicts "profiles are personal"; revoking it silently is the surprise the note warns
about ("the transfer UI must say so before the act rather than after"). An explicit,
defaulted-off flag makes the act deliberate and recorded, and reuses the existing
`ProfileViewer` row rather than inventing a state.

**Why the warning must be computed.** `web/profile_access.py:61` short-circuits on
`if user.is_admin` and returns every profile, so an **admin** outgoing owner loses nothing.
The note records that rev 4 stated the revocation unconditionally and was wrong.

**Implemented by:** Tasks 1 and 3.

### Open-7 — transfer clears `default_profile_id` only when it would become unreadable

**Decision:** after the ownership write, if the outgoing owner's
`users.default_profile_id` equals the transferred profile **and**
`can_read_profile(session, old_owner, profile_id)` is now `False`, set it to `NULL`.
Otherwise leave it.

**Why.** A stale default the user can still read — because they are an admin, or the
profile is `'org'`, or Open-6 just wrote them a viewer row — is a preference they chose,
and `dashboard.resolve_profile_selection` already honours it. A default they cannot read
is a broken preference. Deciding with `can_read_profile` rather than a hand-written
condition is required by `tests/architecture/test_profile_access_single_source.py`, which
bans a second copy of the arms.

**The new owner's `default_profile_id` is NOT set.** `profiles/create.py:81-82` only sets
it when it is already `None`, and D10 does not ask for it. Not doing it is the smaller act.

**Implemented by:** Task 3.

### Open-8 — any `status == 'active'` user may receive a profile, looked up by email

**Decision:** the recipient is identified by **email** and must have `status == 'active'`.
Not restricted to existing `ProfileViewer`s. An unknown, inactive or otherwise ineligible
address gets one generic `400 "could not transfer profile"`.

**Why.** D10 already forbids an inactive recipient, and `status == 'active'` is the exact
test the codebase uses everywhere else for a usable account (`profiles.py:610`,
`digest/send.py:38`, `admin_users.py:225`). Restricting to existing viewers would make
transfer **impossible for the case that motivated D10** — a departed owner's private
profile, which by construction has no viewer rows: `profiles.py:612` makes the owner's own
row a no-op and an unshared private profile has no others. The endpoint is already
owner-or-admin gated, so the act is privileged.

**Why email and why the vague refusal.** It mirrors `POST /api/profiles/{id}/viewers`
(`profiles.py:586`), whose docstring (`:599-601`) gives the reason: "one GENERIC 400, the
same answer every time, so this endpoint is not a directory of who has an account."

**Implemented by:** Task 3.

### Open-9 (not in the note's list; forced by measurement) — QR codes are encoded server-side

**Decision:** add `segno` to the backend and encode with **`segno.make_qr`**. The public
payload carries per item `qr_path` (an SVG `d` string built from `segno`'s documented
`q.matrix`) and `qr_modules` (the module count). React composes
`<svg viewBox="-4 -4 n+8 n+8"><path d={qr_path}/></svg>`.

**`make_qr`, never `make`.** `error="m"` constrains the correction LEVEL, not the symbol
FAMILY, and Micro QR M4 supports level M — so `segno.make` returns a Micro QR for a short
URL. Many phone cameras and a lot of scanner hardware cannot read one, and on a corridor
wall that is a silent per-card failure that looks exactly like a working QR. Measured here
with segno 1.6.6:

```
$ uv run python -c "import segno; [print(repr(u), 'len', len(u),
    '| make(error=m) ver', segno.make(u, error='m').version, 'micro', segno.make(u, error='m').is_micro,
    '| make_qr(error=m) ver', segno.make_qr(u, error='m').version, 'micro', segno.make_qr(u, error='m').is_micro)
    for u in ['https://a.se/', 'https://a.se/x', 'https://example.test/x']]"
'https://a.se/' len 13 | make(error=m) ver M4 micro True | make_qr(error=m) ver 1 micro False
'https://a.se/x' len 14 | make(error=m) ver 1 micro False | make_qr(error=m) ver 1 micro False
'https://example.test/x' len 22 | make(error=m) ver 2 micro False | make_qr(error=m) ver 2 micro False
```

**Both segno commands in this section were run in a worktree where Task 7 Step 1 had
already run `uv add segno`.** `segno` is not installed at `05bd151c`, so re-deriving them
before Task 7 needs `uv add segno` first (or a throwaway venv). Both reproduce verbatim.

A 13-character URL is the trigger. Every fixture in this plan is longer than that, which is
exactly why no other test in Task 7 can see the bug — a Micro QR round-trips perfectly.
`PLAN-DEFECTS.md` D14.

**Why server-side at all.** Measured: no QR encoder exists in this repo on either side,
and `pnpm add` is blocked by the 7-day cooldown, so the frontend cannot gain one.
`uv add segno` resolves and installs (`+ segno==1.6.6`, pure Python, zero runtime deps).

**Why in the payload and not a separate image endpoint.** D6 requires the last-good copy
to keep rendering through a transport failure. A payload of titles plus eight QR image
URLs would render eight broken QRs beside eight live titles the moment the network drops —
worse than a stale board, which is what D6 exists to allow.

**Why `q.matrix` and not segno's SVG serializer.** Regex-parsing a library's output pins
a private format. `q.matrix` is documented, is `int` 0/1, and excludes the quiet zone
(measured: `matrix rows: 33`, `symbol_size(scale=1, border=0) == (33, 33)`), so the
renderer supplies the 4-module quiet zone through the viewBox.

**Why an attribute and never `dangerouslySetInnerHTML`.** The payload can then never inject
markup. Prod CSP is `default-src 'self'` / `script-src 'self'` (`web/middleware.py:20`),
which an inline `<path d=…>` composed in React does not touch.

**Cost, re-measured 2026-09-05 (the authoring estimate of 7998 bytes did NOT reproduce):**

```
$ uv run python -c "
import segno
BASE='https://www.vinnova.se/en/calls-for-proposals/'
def qr_path(q):
    parts=[]
    for y,row in enumerate(q.matrix):
        x=0
        while x < len(row):
            if row[x]:
                run=1
                while x+run < len(row) and row[x+run]: run+=1
                parts.append(f'M{x} {y}h{run}v1h-{run}z'); x+=run
            else: x+=1
    return ''.join(parts)
u = BASE + 'a'*83
q = segno.make_qr(u, error='m'); p = qr_path(q)
print('url len', len(u), 'modules', len(q.matrix), 'path bytes', len(p), 'x24', len(p)*24)"
url len 129 modules 49 path bytes 9100 x24 218400
```

**9100 bytes** of path for a 129-character URL at 49 modules — **218,400 bytes (213 KB) of
path data** for a full 24-item board. `segno.make(u, error='m')` gives the identical
numbers at this length; the `make_qr` choice costs nothing here. The dry run additionally
measured the whole serialized HTTP body at **226,040 bytes (221 KB)** with the endpoint
built; that figure is not re-derived here because it needs the endpoint. Task 7's gate
stays at **512 KB**, which is 2.3× headroom over the measured body. `PLAN-DEFECTS.md` D15.

Note `BASE` is **46** characters, so the 129-character fixture is `BASE + "a" * 83`. An
earlier draft wrote `"a" * 84`, which is 130.

**Implemented by:** Task 7.

---

## File Structure

**New backend files**

| File | Responsibility |
| --- | --- |
| `src/grant_lab/db/models/boards.py` | `Board` and `BoardItem` ORM models. Nothing else. Must be imported by `db/models/__init__.py` or `scripts/gen_architecture.py` asserts. |
| `src/grant_lab/boards/__init__.py` | Empty marker for the service package. |
| `src/grant_lab/boards/queue.py` | The D2 approval queue query. Imports `dashboard._decision_predicate`; owns no predicate of its own. |
| `src/grant_lab/boards/public.py` | The D1 userless public query + payload assembly. Touches no decision table and no user. |
| `src/grant_lab/boards/qr.py` | `qr_path_for(url) -> QrCode`. The only module that imports `segno`. |
| `src/grant_lab/boards/alerts.py` | The D9 trigger, recipient resolution and escalation. |
| `src/grant_lab/web/api/boards.py` | The authenticated boards router (`_ALWAYS`). |
| `src/grant_lab/web/api/public_boards.py` | The one unauthenticated route. Separate file so "this is the public surface" is visible in a directory listing. |
| `alembic/versions/0072_boards.py` | `boards` + `board_items`. |
| `alembic/versions/0073_subscription_board_alerts.py` | `subscriptions.board_alerts_enabled`. |

**Modified backend files**

| File | Change |
| --- | --- |
| `src/grant_lab/db/models/__init__.py` | Import and re-export `Board`, `BoardItem`. |
| `src/grant_lab/db/models/profiles.py` | Add `Subscription.board_alerts_enabled`. |
| `src/grant_lab/web/dashboard.py` | `_decision_predicate` docstring (`:65-77`): name its out-of-package caller. **Not** `_base_call_filters`'s — see Task 4 Step 4. |
| `src/grant_lab/web/api/__init__.py` | Add both routers to `_ALWAYS`. |
| `src/grant_lab/web/api/profiles.py` | Add `transfer_profile_owner`. **It must live here** — see Task 3. |
| `src/grant_lab/digest/send.py` | Send the board alert alongside the digest. |
| `pyproject.toml` + `uv.lock` | `segno`. |

**New frontend files**

| File | Responsibility |
| --- | --- |
| `frontend/src/routes/BoardScreen.tsx` | The `/b/:slug` page: paging, empty slots, header. |
| `frontend/src/routes/BoardCard.tsx` | One card: title, funder, days-to-deadline, budget, QR. |
| `frontend/src/routes/BoardLayout.tsx` | The third pathless layout — full-bleed, no shell, no `useSession`. |
| `frontend/src/api/publicBoard.ts` | The D6 fetch loop. Reads `response.status` directly; never calls `unwrap`. |
| `frontend/src/routes/BoardsList.tsx`, `BoardDetail.tsx`, `BoardQueue.tsx` | The authenticated board management screens. |
| `frontend/src/api/boards.ts` | Typed client for the authenticated board routes. |
| `frontend/src/routes/ProfileTransferCard.tsx` | The D10 transfer UI. |

**Modified frontend files:** `routes.json`, `router.tsx`, `router.simple.tsx`,
`router.test.tsx`, `router.simple.test.tsx`, `routes/ProfileDetail.tsx`,
and the two generated files `src/api/schema.gen.ts`, `src/routes.gen.ts`.

---

## Task order, and why transfer comes first

Tasks 1-3 are the **D10 slice**. They ship before any board task.

The note leaves the sequencing to the plan author. Transfer goes first because **Open-3's
escalation answer assumes ownership is fixable**: an escalation that routes a board alert
to admins is a stopgap, and the real repair — moving the profile to someone who can act —
must exist before a board can put a queue behind an unreachable owner. Shipping a board
whose queue can become unaddressable is the exact failure D10 exists to prevent. It is
also the riskiest slice (it touches the permission model seven other surfaces read from),
so it should be reviewable on its own rather than buried under board work.

| # | Task | Depends on |
| --- | --- | --- |
| 1 | Transfer: the read-access consequence, computed | — |
| 2 | Schema: `boards`, `board_items`, `subscriptions.board_alerts_enabled` | — |
| 3 | Transfer: the endpoint | 1 |
| 4 | The D2 approval queue query | 2 |
| 5 | Board CRUD + settings + the D7 flip refusal | 2 |
| 6 | Approve / un-approve / reorder / publish | 2, 4, 5 |
| 7 | QR encoding + the D1 public endpoint | 2, 6 |
| 8 | The D9 alert | 2, 5 |
| 9 | `/b/:slug` — the unattended screen | 7 |
| 10 | The authenticated board screens + the transfer UI | 3, 5, 6 |

Tasks 1 and 2 are independent and may run in parallel. So may 8 and 9 once 7 lands.

---

# Tasks

---

### Task 1: The read-access consequence of a transfer, computed not asserted

**Files:**
- Create: `src/grant_lab/boards/__init__.py` (empty)
- Modify: `src/grant_lab/web/api/profiles.py` (add `transfer_read_effect`, near `_profile_for_edit` at `:126`)
- Test: `tests/test_profile_transfer_effect.py`

**Interfaces:**
- Consumes: `grant_lab.web.profile_access.can_read_profile(session, user, profile_id) -> bool` (`profile_access.py:76`)
- Produces:
  ```python
  def transfer_read_effect(session, *, profile: Profile, outgoing: User) -> bool:
      """True when `outgoing` will LOSE read access if `profile` leaves them."""
  ```
  Task 3 calls it to decide the warning and Open-7's `default_profile_id` clear.

**Why this is its own task.** D10's single most reviewable claim is "an admin previous
owner loses nothing" — the note records that rev 4 stated the revocation unconditionally
and was **wrong**. A fixed warning sentence is the defect; a computed one is the fix. It
gets its own gate before any endpoint can render it.

**Why it lives in `web/api/profiles.py`.** It must not hand-roll the read arms.
`tests/architecture/test_profile_access_single_source.py::test_no_module_builds_a_second_read_predicate`
bans a `ProfileViewer` or `Profile.visibility` reference in every module except
`db/models/profiles.py` and `web/profile_access.py`, plus four named functions inside
`web/api/profiles.py`. This function references neither — it calls `can_read_profile` —
so it needs **no** exemption. Task 3's endpoint, which writes viewer rows, does.

- [ ] **Step 1: Write the failing test**

`tests/test_profile_transfer_effect.py`:

```python
"""What a transfer costs the OUTGOING owner, and it is not one sentence.

`readable_profile_ids` short-circuits on `if user.is_admin` (profile_access.py:61)
and returns every profile, so an admin previous owner loses nothing. The
decisions note records that an earlier revision stated the revocation
unconditionally and was wrong. This pins the difference.

FIXTURES -- READ THIS BEFORE COPYING A NAME. This repo has NO `db_session`.
The session fixture is `pg_session_factory` (tests/conftest.py:151) and it is a
FACTORY, used as `with pg_session_factory() as s:`. `profile_factory` (:184)
returns a `(profile_id, user_id)` TUPLE and accepts no `visibility` or
`created_by`, so these cases build their own rows. `ProfileKind` is a StrEnum
whose only values are `organizational_unit` and `individual`
(db/models/profiles.py:31-33). Measured against tests/conftest.py at 05bd151c.
"""

from grant_lab.db.models import Profile, ProfileKind, ProfileViewer, User
from grant_lab.web.api.profiles import transfer_read_effect


def _owner_and_profile(session, email, visibility, *, is_admin=False):
    u = User(email=email, name=email, status="active", is_admin=is_admin)
    session.add(u)
    session.flush()
    p = Profile(
        name=f"p-{email}",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        level="group",
        description="",
        structured={},
        created_by=u.id,
        visibility=visibility,
    )
    session.add(p)
    session.flush()
    return u, p


def test_a_non_admin_owner_of_a_private_profile_loses_read(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "owner@umu.se", "private")
        assert transfer_read_effect(s, profile=p, outgoing=u) is True


def test_an_admin_previous_owner_loses_nothing(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "adminowner@umu.se", "private", is_admin=True)
        assert transfer_read_effect(s, profile=p, outgoing=u) is False


def test_an_org_profile_stays_readable_by_everyone(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "orgowner@umu.se", "org")
        assert transfer_read_effect(s, profile=p, outgoing=u) is False


def test_a_viewer_row_keeps_read_even_on_a_private_profile(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "vowner@umu.se", "private")
        s.add(ProfileViewer(profile_id=p.id, user_id=u.id))
        s.flush()
        assert transfer_read_effect(s, profile=p, outgoing=u) is False
```

The fourth case is the one that makes this non-trivial: it is D10's own stale-viewer-row
hazard seen from the other side, and a `visibility == 'private'` shortcut would get it wrong.

- [ ] **Step 2: Run it and confirm it fails for the RIGHT reason**

```bash
cd <worktree> && uv run pytest tests/test_profile_transfer_effect.py -q
```

Expected: **1 collection ERROR**, `ImportError: cannot import name 'transfer_read_effect'`
— not four failures. Step 1's file puts that import at **module level**, so pytest never
reaches a test body and reports one error for the module:

```
ERROR tests/test_profile_transfer_effect.py
!!!! Interrupted: 1 error during collection !!!!
1 error in 1.29s
```

**This is the one place an ERROR is the right red** — there is no function yet. Same
situation as Task 2 Step 2, which says the same thing. Confirm the ImportError names
`transfer_read_effect`: a DIFFERENT ImportError (a typo in a fixture import, say) looks
identical and means something else entirely.

An earlier draft of this step expected "4 FAILED" and warned that ERRORs meant trouble —
so it fired its own alarm on the correct result. `PLAN-DEFECTS.md` D1.

- [ ] **Step 3: Implement**

In `src/grant_lab/web/api/profiles.py`, immediately after `_profile_for_edit` (ends `:144`):

```python
def transfer_read_effect(session, *, profile: Profile, outgoing: User) -> bool:
    """True when `outgoing` will LOSE read access if `profile` leaves them.

    Computed, never asserted. `readable_profile_ids` short-circuits on
    `if user.is_admin` (profile_access.py:61) and returns every profile, so an
    admin previous owner loses nothing; `visibility='org'` and an explicit
    ProfileViewer row each keep access too. The decisions note records that an
    earlier revision stated the revocation unconditionally and was wrong, so the
    transfer UI must render THIS, not a fixed sentence.

    Asks the single-source predicate as if the transfer had already happened, by
    excluding only the owner arm -- which is the one arm the transfer removes.
    """
    if profile.created_by != outgoing.id:
        return False  # not the owner; the transfer takes nothing from them
    return not _readable_ignoring_ownership(session, outgoing, profile.id)
```

and the helper it needs, also in this file:

```python
def _readable_ignoring_ownership(session, user: User, profile_id: int) -> bool:
    """`can_read_profile` with the owner arm neutralised.

    Implemented by asking the real predicate about a user whose id cannot match
    `created_by` -- rather than by rewriting the arms here, which
    tests/architecture/test_profile_access_single_source.py forbids and which is
    how a second read rule gets born.
    """
    probe = SimpleNamespace(id=-1, is_admin=user.is_admin)
    if can_read_profile(session, probe, profile_id):
        return True
    viewer = session.scalar(
        select(ProfileViewer.user_id).where(
            ProfileViewer.profile_id == profile_id,
            ProfileViewer.user_id == user.id,
        )
    )
    return viewer is not None
```

Add `from types import SimpleNamespace` to the imports.

**`_readable_ignoring_ownership` references `ProfileViewer`, so it needs an exemption.**
Add to `tests/architecture/test_profile_access_single_source.py::EXEMPT_PROFILE_FUNCTIONS`:

```python
    "_readable_ignoring_ownership": (
        "reads ONE viewer row to answer a hypothetical, after can_read_profile has "
        "already answered the org/admin arms; decides nothing about the live request"
    ),
```

- [ ] **Step 4: Run the test, then the gate that guards the exemption**

```bash
cd <worktree> && uv run pytest tests/test_profile_transfer_effect.py -q
cd <worktree> && uv run pytest tests/architecture/test_profile_access_single_source.py -q
```

Expected: `4 passed` and the architecture file green.

- [ ] **Step 5: Prove both gates BITE**

**Gate A — the four behavioural tests.**
- *What trivially passes it:* `return False` unconditionally passes three of the four
  cases. That is why case 1 exists.
- Mutate: make `transfer_read_effect` `return profile.visibility == "private"`.

```bash
cd <worktree> && cp src/grant_lab/web/api/profiles.py /tmp/pf.bak
# apply the mutation, then:
cd <worktree> && uv run pytest tests/test_profile_transfer_effect.py -q
```

Record: `against good input: PASS` / `against the visibility shortcut: FAIL on
test_an_admin_previous_owner_loses_nothing and
test_a_viewer_row_keeps_read_even_on_a_private_profile, because both are private
profiles that stay readable`.

**Gate B — the exemption is narrow.**
- *What trivially passes it:* adding `web/api/profiles.py` to `EXEMPT_MODULES` instead of
  the function to `EXEMPT_PROFILE_FUNCTIONS` — the whole file would go unscanned.
- Mutate: put a bare `select(ProfileViewer.profile_id)` at module scope in
  `web/api/boards.py` (create it empty for the probe) and run
  `uv run pytest tests/architecture/test_profile_access_single_source.py -q`.

Record: `against good input: PASS` / `against a module-level ProfileViewer select in
web/api/boards.py: FAIL, because the negative half scans module scope, not just function
bodies`.

**Restore and clear caches after every mutation:**

```bash
cd <worktree> && cp /tmp/pf.bak src/grant_lab/web/api/profiles.py && rm /tmp/pf.bak
cd <worktree> && find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
cd <worktree> && git status --porcelain    # must show ONLY the files this task owns
```

A same-size restore inside the same second leaves a stale `.pyc` and the tests run the OLD
code — the cache clear is not optional.

- [ ] **Step 6: Full backend suite**

```bash
cd <worktree> && uv run pytest -n 16 --dist loadfile -q
```

**Expected DELTA: +4 passed** against the 5137 measured at `05bd151c`, and **`1 skipped`
unchanged**. Do not adjust the expected number to close the arithmetic; if it differs,
name the term that changed.

- [ ] **Step 7: Lint and commit**

```bash
cd <worktree> && uv run ruff check src tests alembic scripts
cd <worktree> && uv run ruff format src/grant_lab/web/api/profiles.py tests/test_profile_transfer_effect.py
cd <worktree> && git add src/grant_lab/web/api/profiles.py \
  src/grant_lab/boards/__init__.py \
  tests/test_profile_transfer_effect.py \
  tests/architecture/test_profile_access_single_source.py
cd <worktree> && git commit -m "feat(profiles): compute what a transfer costs the outgoing owner

An admin previous owner loses nothing — readable_profile_ids short-circuits on
is_admin — so the transfer warning must be computed, not a fixed sentence."
```

`ruff format` takes **explicit file paths only**. Never a directory.

---

### Task 2: `boards`, `board_items`, and the board-alert flag

**Files:**
- Create: `src/grant_lab/db/models/boards.py`
- Create: `alembic/versions/0072_boards.py`
- Create: `alembic/versions/0073_subscription_board_alerts.py`
- Modify: `src/grant_lab/db/models/__init__.py`
- Modify: `src/grant_lab/db/models/profiles.py` (`Subscription`, `:158-172`)
- Modify: `docs/ARCHITECTURE.md` (**generated** — regenerate, never hand-edit)
- Test: `tests/test_models_boards.py`, `tests/test_migration_0072_boards.py`, `tests/test_migration_0073_board_alerts.py`

**Interfaces:**
- Produces: `Board`, `BoardItem` (importable from `grant_lab.db.models`), and
  `Subscription.board_alerts_enabled: Mapped[bool]`.

**Measured facts this task rests on:**
- Migration head is **0071** (`ls alembic/versions/ | sort | tail -2`). These are 0072 and 0073.
- `scripts/gen_architecture.py:74-76` asserts every `.py` in `db/models/` is imported by
  `db/models/__init__.py`. A new module that is not listed makes the generator **assert**,
  reddening `tests/architecture/test_architecture_map_is_current.py`.
- `Call.url` is **NOT NULL** (`db/models/calls.py:107`) and `Call.deadline` **is** nullable (`:111`).

**Two migrations, not one.** They are independent changes with independent revert stories:
0072 creates two tables Plan C owns end to end; 0073 alters a table three other subsystems
read. Folding them together means reverting the alert flag also drops the boards.

- [ ] **Step 1: Write the failing model test**

`tests/test_models_boards.py`:

```python
"""The board schema, as §4.3 fixes it plus D7's stored orientation.

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. There is no `db_session` in this repo.
`profile_factory` (:184) returns a `(profile_id, user_id)` tuple.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import Board, BoardItem, Subscription


def test_board_columns_match_the_spec():
    cols = {c.name for c in inspect(Board).columns}
    assert cols == {
        "id", "slug", "title", "profile_id", "is_published", "min_score",
        "orientation", "items_per_page", "max_pages", "page_seconds",
        "created_by", "published_by", "published_at", "created_at", "updated_at",
    }


def test_board_items_primary_key_is_board_and_call():
    pk = {c.name for c in inspect(BoardItem).primary_key}
    assert pk == {"board_id", "call_id"}


def test_orientation_is_constrained_to_two_values(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile A")
    with pg_session_factory() as s:
        s.add(Board(slug="x" * 16, title="t", profile_id=pid,
                    orientation="diagonal", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_slug_is_unique(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile B")
    with pg_session_factory() as s:
        for _ in range(2):
            s.add(Board(slug="s" * 16, title="t", profile_id=pid,
                        orientation="landscape", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_a_slug_under_16_chars_is_refused(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile C")
    with pg_session_factory() as s:
        s.add(Board(slug="s" * 15, title="t", profile_id=pid,
                    orientation="landscape", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_subscription_carries_its_own_board_alert_flag():
    cols = {c.name for c in inspect(Subscription).columns}
    assert "board_alerts_enabled" in cols
    assert "digest_enabled" in cols, "the board flag is ADDED, never a rename"
```

The last assertion is load-bearing: Open-1's whole point is that these are two flags, and
a "simplification" that renames one into the other must go red.

- [ ] **Step 2: Run it and confirm it fails**

```bash
cd <worktree> && uv run pytest tests/test_models_boards.py -q
```

Expected: collection **ERROR**, `ImportError: cannot import name 'Board'`. This is the one
place an ERROR is the right red — there is no module yet.

- [ ] **Step 3: Write the models**

`src/grant_lab/db/models/boards.py`:

```python
"""The corridor board and its approved queue.

MATCH-edition tables. Nothing here may import from grant_lab.proposal_assist --
tests/architecture/test_match_edition_process_cut.py boots a match process in a
fresh interpreter and asserts that CONSTRUCTING the app (create_app()) imports
zero proposal_assist modules. It measures construction, not boot: the lifespan
that runs afterwards imports seven of them unguarded (main.py:212, 229-233, 251).
Everything in this plan is reachable from create_app(), so the gate does bite
here -- but do not restate it as "a match boot imports none".

`orientation` is STORED rather than inferred from the viewport. Spec §6 said the
renderer picks it from the aspect ratio; the 2026-09-05 decisions note (D7)
overrides that, because §6 also caps approval at `items_per_page * max_pages`
and forbids "the display silently truncating a list an admin approved". If the
renderer decides orientation, the cap is unknowable at approval time. See the
plan's "Deviation 2".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index,
    Integer, String, Text, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base

# Landscape 1920x1080 is a 2x4 grid of 8; portrait 1080x1920 is 1x4 of 4 (spec
# §6). `items_per_page` remains an override for a screen that measures
# differently, so this is a DEFAULT, not the value.
ORIENTATION_ITEMS_PER_PAGE = {"landscape": 8, "portrait": 4}


class Board(Base):
    __tablename__ = "boards"
    __table_args__ = (
        CheckConstraint(
            "orientation IN ('landscape','portrait')", name="ck_boards_orientation"
        ),
        # >= 16 chars, per §4.3. Enforced in the DB so a hand-written INSERT
        # cannot create a guessable public URL.
        CheckConstraint("char_length(slug) >= 16", name="ck_boards_slug_length"),
        CheckConstraint("items_per_page > 0", name="ck_boards_items_per_page"),
        CheckConstraint("max_pages > 0", name="ck_boards_max_pages"),
        Index("ix_boards_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # RESTRICT, per §4.3: deleting a profile must not silently blank a wall.
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    min_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    orientation: Mapped[str] = mapped_column(
        String(16), nullable=False, default="landscape", server_default=text("'landscape'")
    )
    items_per_page: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8, server_default=text("8")
    )
    max_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    page_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=text("30")
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BoardItem(Base):
    """One approved call on one board.

    Stores a `call_id` and nothing about the call. D8: the board shows LIVE call
    data, so the public query joins `calls` and a funder's correction reaches the
    wall with no renewed approval. A snapshot would keep showing a deadline the
    funder has since fixed, and on a hospital wall a wrong date is worse than an
    unreviewed title.

    `Call` has NO status or withdrawn column (measured), so "the board drops
    withdrawn calls" is not implementable in release 1 and must not be claimed.
    The only automatic removal is the deadline filter.
    """

    __tablename__ = "board_items"
    __table_args__ = (Index("ix_board_items_board_position", "board_id", "position"),)

    board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True
    )
    call_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

In `src/grant_lab/db/models/profiles.py`, inside `Subscription` after `digest_enabled`
(`:167-169`):

```python
    # SEPARATE from digest_enabled, deliberately. digest_enabled is read at
    # digest/assemble.py:95 to decide whether a profile's ranked calls appear in
    # the weekly email. Muting a weekly summary must not also mute the alert that
    # a public corridor screen has gone underfull -- a different thing with a
    # different urgency (decisions note D9, plan Open-1).
    #
    # server_default is load-bearing: scripts/seed_dev_data.py:328 inserts a
    # Subscription with only user_id and profile_id, so without it every seeded
    # profile would silently opt out of board alerts.
    board_alerts_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
```

In `src/grant_lab/db/models/__init__.py`, add the import in the package's subsystem order
(boards are a MATCH-edition table, so they belong with `calls`/`profiles`/`ranking`):

```python
from grant_lab.db.models.boards import (
    ORIENTATION_ITEMS_PER_PAGE,
    Board,
    BoardItem,
)
```

and add `"Board"`, `"BoardItem"`, `"ORIENTATION_ITEMS_PER_PAGE"` to `__all__` if the file
declares one.

- [ ] **Step 4: Write both migrations**

`alembic/versions/0072_boards.py` — `down_revision = "0071"`, creating both tables with
every constraint above. `alembic/versions/0073_subscription_board_alerts.py` —
`down_revision = "0072"`:

```python
def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "board_alerts_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    # NOT a round trip: dropping this destroys every owner's board-alert
    # preference, which a re-add cannot restore. Same reasoning as
    # 0070_profile_visibility.py's downgrade note.
    op.drop_column("subscriptions", "board_alerts_enabled")
```

- [ ] **Step 5: Migration round-trip tests**

Follow the shape of `tests/test_migration_0071_call_decisions.py` — same fixtures, same
`upgrade`/`downgrade` structure. Assert for 0072: both tables exist, `boards.slug` has a
unique index, the two CHECK constraints reject `'diagonal'` and a 15-char slug, and
`board_items` has the composite PK. For 0073: the column exists, is `NOT NULL`, and an
INSERT that omits it yields `True`.

The "INSERT that omits it" case is the one that pins the `server_default`, which is what
`PLAN-DEFECTS.md` N2 says the seed path depends on.

- [ ] **Step 6: Regenerate the architecture map — SAME commit**

```bash
cd <worktree> && uv run python scripts/gen_architecture.py
cd <worktree> && uv run pytest tests/architecture/test_architecture_map_is_current.py -q
```

- [ ] **Step 7: Prove the generator gate BITES**

- *What trivially passes it:* a new model module the generator never sees. It would be
  absent from `ARCHITECTURE.md` and the diff would still match.
- Mutate: comment out the `from grant_lab.db.models.boards import ...` line in
  `db/models/__init__.py` and run the gate.

```bash
cd <worktree> && uv run pytest tests/architecture/test_architecture_map_is_current.py -q
```

Record: `against good input: PASS` / `against boards.py unimported by models/__init__.py:
FAIL, because scripts/gen_architecture.py:74-76 asserts "model modules not imported by
models/__init__.py: ['boards.py']"`. Restore the line and clear `__pycache__`.

- [ ] **Step 8: Full suite, lint, commit**

```bash
cd <worktree> && uv run pytest -n 16 --dist loadfile -q
cd <worktree> && uv run ruff check src tests alembic scripts
```

**Expected DELTA: +6 (models) + the migration tests you actually wrote.** Step 1's file
defines **six** test functions — an earlier draft said +5 against its own six-test code
block (`PLAN-DEFECTS.md` D2). Verify with
`uv run pytest tests/test_models_boards.py --collect-only -q | tail -1` before trusting
either number. Count the migration tests with
`uv run pytest tests/test_migration_0072_boards.py tests/test_migration_0073_board_alerts.py --collect-only -q | tail -1`
and carry that number — do not estimate it. `1 skipped` unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): boards and board_items, with orientation stored on the board

Spec §4.3's two tables plus D7's stored orientation, and a second boolean on
subscriptions so muting the weekly digest does not mute the board alert."
```

---

### Task 3: `PUT /api/profiles/{profile_id}/owner`

**Files:**
- Modify: `src/grant_lab/web/api/profiles.py` — `transfer_profile_owner`, **and** `ProfileDetailOut` (`:85`) + `get_profile` (`:177`), which gain `outgoing_lost_read`
- Modify: `tests/architecture/test_profile_access_single_source.py` (three pin sets)
- Modify: `tests/architecture/full_edition_routes.txt` + the `109` literal in `test_edition_surface.py` (`:175`) — **one** new path, the PUT; the detail-payload field adds no route
- Modify: `frontend/src/api/schema.gen.ts` (**generated**) — changed by BOTH the new route and the new detail field
- Test: `tests/test_api_profile_transfer.py`

**Interfaces:**
- Consumes: `_profile_for_edit` (`profiles.py:126`), `transfer_read_effect` (Task 1),
  `can_read_profile` (`profile_access.py:76`)
- Produces: `PUT /api/profiles/{profile_id}/owner`, body
  `{"email": str, "keep_read_access": bool = false}`, response
  `{"owner": {"id": int, "name": str, "email": str}, "outgoing_lost_read": bool}`
- Produces, **and this half is not optional**: `outgoing_lost_read: bool` on
  `ProfileDetailOut`, i.e. on `GET /api/profiles/{profile_id}`, computed in `get_profile`
  with the Task-1 helper as `transfer_read_effect(session, profile=p, outgoing=user)`.

**Why the detail payload too.** Open-6's whole reason for existing is the note's sentence
"the transfer UI must say so **before the act** rather than after". A field that only
appears on the `PUT` **response** exists solely *after* the transfer has happened, so
Task 10's card would have nothing to read at render time and its gate
(`it("does NOT warn an admin owner, because they lose nothing")`) could not be built at
all. An earlier draft produced the value only on the response. `PLAN-DEFECTS.md` D17.

`transfer_read_effect` returns `False` when the caller is not the owner, so the field is
honest for every reader of the detail payload, not just the owner.

**Why this endpoint must live in `web/api/profiles.py` and nowhere else.**
`tests/architecture/test_profile_access_single_source.py::test_no_module_builds_a_second_read_predicate`
forbids any reference to `ProfileViewer` outside two exempt modules and four exempt
functions, and `EXEMPT_FUNCTION_MODULE` is **only** `web/api/profiles.py` (`:208`).
Transfer deletes and may create `ProfileViewer` rows. Anywhere else would need a
whole-**module** exemption — strictly broader than the four narrow function exemptions
that exist today. This is a schema-of-the-gate constraint, not a style preference.

**What the transaction must do, in order** (all four are D10 requirements or plan
decisions; all four are in ONE transaction):

1. **Delete the incoming owner's `ProfileViewer` row if present.** D10's stale-row hazard:
   `profiles.py:612` only refuses to add a viewer row for whoever is owner *at that
   moment*, so an active non-owner may hold one legitimately. Transfer to that person
   leaves it behind, and the `ProfileViewer` docstring's invariant
   (`db/models/profiles.py:135-136`, "the OWNER never has a row") stops being true.
2. **Set `profile.created_by = new_owner.id`.**
3. **Create the new owner's `Subscription` if absent**, mirroring `profiles/create.py:79`
   — otherwise D9 notifies nobody. **Never touch the old owner's row:**
   `digest/assemble.py:86-88` is explicit that deleting it destroys their `digest_enabled`
   preference and a re-share cannot restore it, and `assemble.py:97` already filters the
   digest through `readable_profile_ids`, so their section simply stops appearing.
4. **Open-6:** if `keep_read_access`, add a `ProfileViewer` row for the outgoing owner.
   **Open-7:** then, if their `users.default_profile_id` is this profile and
   `can_read_profile(session, outgoing, profile.id)` is now `False`, set it to `NULL`.

Step 4 is computed **after** step 2 so it asks the real post-transfer predicate.

- [ ] **Step 1: Write the failing tests**

`tests/test_api_profile_transfer.py`. **Thirteen** cases, each pinning one decision (an
earlier draft's prose said "nine" over a twelve-name list; the thirteenth is D17's):

```python
def test_owner_can_transfer_to_an_active_user(monkeypatch, pg_url, pg_session_factory, ...): ...           # happy path
def test_admin_can_transfer_a_profile_they_do_not_own(monkeypatch, pg_url, pg_session_factory, ...): ...   # D10: the point
def test_a_stranger_gets_404_not_403(monkeypatch, pg_url, pg_session_factory, ...): ...                    # no existence oracle
def test_a_reader_who_cannot_edit_gets_403(monkeypatch, pg_url, pg_session_factory, ...): ...              # reading is wider
def test_an_inactive_recipient_is_refused_generically(monkeypatch, pg_url, pg_session_factory, ...): ...   # Open-8
def test_an_unknown_email_gets_the_SAME_generic_400(monkeypatch, pg_url, pg_session_factory, ...): ...     # not a directory
def test_the_incoming_owners_viewer_row_is_deleted(monkeypatch, pg_url, pg_session_factory, ...): ...      # D10 stale row
def test_the_new_owner_gets_a_subscription_row(monkeypatch, pg_url, pg_session_factory, ...): ...          # D9 depends on it
def test_the_old_owners_subscription_row_SURVIVES(monkeypatch, pg_url, pg_session_factory, ...): ...       # assemble.py:86-88
def test_keep_read_access_writes_a_viewer_row_for_the_outgoing_owner(monkeypatch, pg_url, pg_session_factory, ...): ...  # Open-6
def test_a_now_unreadable_default_profile_id_is_cleared(monkeypatch, pg_url, pg_session_factory, ...): ...       # Open-7
def test_a_still_readable_default_profile_id_is_LEFT(monkeypatch, pg_url, pg_session_factory, ...): ...          # Open-7
def test_the_detail_payload_says_whether_the_owner_would_lose_read(monkeypatch, pg_url, pg_session_factory, ...): ...  # D17
```

The last two of the Open-7 pair are a pair on purpose: a rule that clears unconditionally
passes the first and fails the second, and one that never clears does the reverse.

`test_an_unknown_email_gets_the_SAME_generic_400` must assert the **response bodies are
byte-equal** to the inactive-recipient case, not merely both 400. Two different messages
are the directory the endpoint must not be.

`test_the_detail_payload_says_whether_the_owner_would_lose_read` reads
`GET /api/profiles/{id}` as a **non-admin owner of a private profile** (expects `True`) and
as an **admin owner of the same** (expects `False`), so a hardcoded `True` fails. It must
not go through the transfer at all — the point is the value existing *before* the act.

**`test_a_stranger_gets_404_not_403` must assert the 404 BODY, not just the status.**
Measured: with no route registered at all, this app answers
`404 {"error":{"code":"not_found","message":"Not Found"}}` — so a test whose only
assertion is `r.status_code == 404` is **GREEN before a line of the endpoint is written**,
and stays green if the route is later deleted, unregistered or renamed. Measured with the
plan's original twelve cases and no endpoint: `11 failed, 1 passed`, and the one that
passed was this one. The real refusal comes from `_profile_for_edit`
(`profiles.py:139/141`), which raises `HTTPException(404, detail="profile not found")`, and
`main.py:548` renders `exc.detail` into the envelope's `message`. So:

```python
    assert r.status_code == 404 and b"profile not found" in r.content
```

Better still, put a **positive control in the same test**: the owner's identical request
returns 200, so "the route exists" is inside the gate rather than assumed. `PLAN-DEFECTS.md` D3.

- [ ] **Step 2: Run and confirm they fail**

```bash
cd <worktree> && uv run pytest tests/test_api_profile_transfer.py -q
```

Expected: **13 FAILED, 0 passed.** Not ERROR — nothing here is imported at module scope
that does not exist yet, so the failures are assertion failures against FastAPI's own
404 for a route that is not registered.

**If the stranger case PASSES, its body assertion is missing** — go back to Step 1. That
is the one case that can be green before the endpoint exists, and the reason it asserts
the body.

`test_the_detail_payload_says_whether_the_owner_would_lose_read` fails differently from
the rest: `GET /api/profiles/{id}` already exists, so it fails on the missing key rather
than on a 404. That is the correct red for it.

- [ ] **Step 3: Implement**

Add to `src/grant_lab/web/api/profiles.py`, after `set_profile_visibility` (`:643`):

```python
class ProfileTransferIn(BaseModel):
    email: str
    keep_read_access: bool = False


class ProfileTransferOut(BaseModel):
    owner: ProfileViewerOut
    outgoing_lost_read: bool


@router.put("/profiles/{profile_id}/owner", response_model=ProfileTransferOut)
def transfer_profile_owner(
    request: Request,
    profile_id: int,
    body: ProfileTransferIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Move a profile to another user. Owner-or-admin, per profiles.py:400.

    The admin arm is not a convenience: the case that motivated this is an owner
    who can no longer log in, so only an admin can act.

    Ownership is load-bearing in SEVEN places (profile_access.py:69,
    profiles.py:142/196/400/544/612, calls.py:641), which is why this is one
    transaction and not four endpoints.

    Recipient eligibility is `status == 'active'` and nothing narrower: an
    unshared private profile has no viewer rows at all, so a
    viewers-only rule would make transfer impossible for exactly the case it
    exists for. The refusal is ONE generic 400 for unknown, inactive and
    ineligible alike -- mirroring add_profile_viewer (:599-601), so this is not a
    directory of who has an account.
    """
```

Body follows the four ordered steps above, inside a single `with sf() as session:` block
with one `session.commit()`.

**And the other half — the detail payload (D17).** In the same commit, add
`outgoing_lost_read: bool` to `ProfileDetailOut` (`profiles.py:85`) and compute it in
`get_profile` (`:177`), which already holds `p`, `user` and `session` at `:196`:

```python
        outgoing_lost_read = transfer_read_effect(session, profile=p, outgoing=user)
```

`get_profile` is in `EXPECTED_PREDICATE_PINS` for calling `can_read_profile`; adding a call
to `transfer_read_effect` does not disturb that, and the function gains **no**
`ProfileViewer` or `Profile.visibility` reference, so it needs no exemption — the helper
owns those (Task 1).

- [ ] **Step 4: Update the three pin sets in `test_profile_access_single_source.py`**

`transfer_profile_owner` references `ProfileViewer`, so it needs:

```python
# EXEMPT_PROFILE_FUNCTIONS
    "transfer_profile_owner": (
        "moves ownership and the viewer rows that follow it; gates on "
        "_profile_for_edit and asks can_read_profile for the read consequence"
    ),

# DELEGATION_PINS
    "web/api/profiles.py::transfer_profile_owner": "_profile_for_edit",

# EXPECTED_DELEGATION_PINS
        "web/api/profiles.py::transfer_profile_owner",
```

`EXPECTED_DELEGATION_PINS` (`:174`) is the deliberate second literal — the comment at
`:154-156` says why: "changing PREDICATE_PINS alone cannot pass". **Both must change, in
this one diff.**

**This one dict entry is worth TWO tests, not one.** `test_pinned_function_still_exists`
is parametrized over `(*PREDICATE_PINS, *DELEGATION_PINS)` (`:484-486`) and
`test_pinned_delegate_still_delegates` over `DELEGATION_PINS.items()` (`:517-519`), so
adding `web/api/profiles.py::transfer_profile_owner` creates:

```
tests/architecture/test_profile_access_single_source.py::test_pinned_function_still_exists[web/api/profiles.py::transfer_profile_owner]
tests/architecture/test_profile_access_single_source.py::test_pinned_delegate_still_delegates[web/api/profiles.py::transfer_profile_owner]
```

The `EXEMPT_PROFILE_FUNCTIONS` entry adds **zero** — nothing is parametrized over that
dict, which is why Task 1's delta is clean and this one is not. `PLAN-DEFECTS.md` D4.

- [ ] **Step 5: Regenerate the route artifacts — SAME commit**

```bash
cd <worktree> && bash scripts/gen_openapi_types.sh
cd <worktree> && APP_EDITION=full DRAFTING_ENABLED=true SPA_MODE=disabled \
  DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db \
  SESSION_SECRET=test-secret-at-least-16-chars ALLOWED_EMAIL_DOMAINS=umu.se \
  APP_BASE_URL=http://testserver \
  uv run python -c "from grant_lab.main import create_app; print('\n'.join(sorted({r.path for r in create_app().routes})))" \
  > tests/architecture/full_edition_routes.txt
cd <worktree> && wc -l tests/architecture/full_edition_routes.txt
```

Then bump `test_edition_surface.py::test_the_baseline_is_not_empty`'s literal (`:175`)
**by hand** to whatever `wc -l` just printed. **Do not compute it** — the measured 109 plus
one new path is 110, and if `wc -l` says anything else, find the term that changed.

The `outgoing_lost_read` field on `ProfileDetailOut` changes `schema.gen.ts` but **not** the
route set, so only the PUT moves this file. `gen_openapi_types.sh` picks up both.

- [ ] **Step 6: Run every gate this touched**

```bash
cd <worktree> && uv run pytest tests/test_api_profile_transfer.py tests/architecture -q
cd <worktree> && cd frontend && pnpm vitest run && pnpm lint && pnpm -C . build
```

`schema.gen.ts` changed, so the frontend must still typecheck and build.

- [ ] **Step 7: Prove three gates BITE**

**Gate A — the generic-refusal test.**
- *What trivially passes it:* asserting `status == 400` on both cases. Two different
  messages both give 400.
- Mutate: return `detail="no such user"` for the unknown-email branch.
- Expected: `test_an_unknown_email_gets_the_SAME_generic_400` FAILS on the **body**
  comparison, because that is what the assertion reads.

**Gate B — the delegation pin.**
- *What trivially passes it:* adding only the `EXEMPT_PROFILE_FUNCTIONS` entry. The
  negative half goes green and nothing pins that the route still gates.
- Mutate: delete the `_profile_for_edit(...)` call from `transfer_profile_owner` and
  inline `session.get(Profile, profile_id)`.
- Expected: `test_profile_access_single_source.py` FAILS on the delegation pin.
  **Record whether it also stays green on the thirteen behavioural tests** — if it does,
  that is exactly the hole outside review 2026-09-04 (Sol #9) found for `_profile_for_edit`
  and the reason this pin exists.
- **This gate's own evidence depends on the stranger case asserting the body** (Step 1).
  The dry run measured, with the tightened assertion:
  `FAILED test_a_stranger_gets_404_not_403` and
  `FAILED test_a_reader_who_cannot_edit_gets_403` — **two** behavioural failures. With a
  status-only stranger assertion it would have been one, and this gate would have read as a
  weaker catch than it is. `PLAN-DEFECTS.md` D5.

**Gate C — the route baseline.**
- *What trivially passes it:* regenerating `full_edition_routes.txt` without touching the
  literal. The set comparison goes green.
- Mutate: regenerate the file but leave the literal at 109.
- Expected: `test_the_baseline_is_not_empty` FAILS, because the literal is bumped by hand
  precisely so a regeneration cannot wave a route through.

Restore after each; clear `__pycache__`; `git status --porcelain` must show only this
task's files.

- [ ] **Step 8: Full suite and commit**

**Expected DELTA: +15** — thirteen endpoint cases **plus two** from Step 4, because
`DELEGATION_PINS` is parametrized by both `test_pinned_function_still_exists` and
`test_pinned_delegate_still_delegates`, so one dict entry is two test IDs.

An earlier draft carried **+12**: it counted twelve cases (while its prose said nine) and
missed the parametrization entirely. The dry run measured **+14** for the twelve-case
version — 12 + 2 — which is the arithmetic that names the term. This plan adds D17's
thirteenth case, hence +15. Re-derive with `--collect-only` on both files rather than
trusting any of these numbers, and if it differs, name the term that changed.
`1 skipped` unchanged.

```bash
cd <worktree> && git commit -m "feat(profiles): transfer a profile to another owner

created_by is load-bearing in seven places, so ownership moves in one
transaction: the incoming owner's viewer row is deleted, their subscription
created, and the outgoing owner's subscription deliberately left alone."
```

---

### Task 4: The D2 approval queue query

**Files:**
- Create: `src/grant_lab/boards/queue.py`
- Modify: `src/grant_lab/web/dashboard.py` — the **`_decision_predicate`** docstring (`:65-77`). **Not** `_base_call_filters`'s (`:308-321`), which is true today and must stay true; see Step 4 and `PLAN-DEFECTS.md` D6.
- Test: `tests/test_boards_queue.py`

**Interfaces:**
- Consumes: `dashboard._decision_predicate(view, *, user_id, profile_id)` (`dashboard.py:64`)
- Produces:
  ```python
  @dataclass
  class QueueRow:
      call: Call
      score: int
      already_approved: bool

  def board_queue(session, *, board: Board, limit: int, offset: int) -> tuple[list[QueueRow], int]:
      """Candidates for `board`, best score first, plus the total waiting count."""
  ```

**The deviation this task implements** (§6 quoted in "Deviation 1" above): the queue is
**not** userless. It applies the hides of `profiles.created_by WHERE profiles.id =
boards.profile_id` — **not** `boards.created_by`, because §4.3 lets any profile editor
create a board, so an admin can build one on a profile they do not own.

**It REUSES the predicate; it must not fork it.** Re-measured at `05bd151c`:
`_decision_predicate` with `view="archive"` is exactly
`Call.id.notin_(rows.where(CallDecision.state == "hidden").scalar_subquery())` at
**`dashboard.py:91`** (line 90 is the `raise ValueError` tripwire above it) — which is
"apply this user's hides on this profile" and nothing else.

> The dry run reported this citation as off by one. It is not: the dry run measured its own
> worktree, where its D6 fix had already lengthened `_decision_predicate`'s docstring. At
> `05bd151c` the archive return is line 91. Re-derived here; `PLAN-DEFECTS.md` D19 item 1 is
> **rejected**.

- [ ] **Step 1: Write the failing test**

`tests/test_boards_queue.py`:

```python
"""The queue applies the PROFILE OWNER's hides, and nobody else's.

Spec §6 says the same userless query feeds the queue. The 2026-09-05 decisions
note (D2) overrides that: profiles default to visibility='org'
(db/models/profiles.py:121-123), so a userless queue plus "respect everybody's
triage" would let any logged-in user keep a call off a public wall. Keying off
one named person -- the owner -- is the only rule that respects triage at all.
"""

def test_a_call_the_owner_hid_is_absent_from_the_queue(pg_session_factory, ...): ...
def test_a_call_a_COLLEAGUE_hid_is_still_in_the_queue(pg_session_factory, ...): ...
def test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue(pg_session_factory, ...): ...
def test_a_hide_on_a_DIFFERENT_profile_does_not_suppress(pg_session_factory, ...): ...
def test_below_min_score_is_absent(pg_session_factory, ...): ...
def test_a_past_deadline_is_absent(pg_session_factory, ...): ...
def test_an_already_approved_call_is_flagged_not_dropped(pg_session_factory, ...): ...
def test_the_waiting_count_counts_UNAPPROVED_candidates_only(pg_session_factory, ...): ...
def test_a_call_the_owner_SAVED_is_still_in_the_queue(pg_session_factory, ...): ...
```

**Nine cases, and the ninth is not padding.** `test_a_call_the_owner_SAVED_is_still_in_the_queue`
has the profile owner **save** a call on the board's own profile and asserts it is still
offered. Without it nothing in this file creates a non-hidden `CallDecision` row, so the
`state == "hidden"` clause inside `_decision_predicate` is **invisible to every test here**
— a hand-rolled copy that drops it passes the whole file. The dry run proved that by
running exactly that mutation and watching the state clause survive untouched.
`PLAN-DEFECTS.md` D8.

`test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue` is the specific case D2 calls
out: build the board with an **admin** who is not the profile owner, have that admin hide
a call, and assert it is still offered. A rule keyed on `boards.created_by` passes every
other test in this file.

`test_an_already_approved_call_is_flagged_not_dropped` is D3: approval and the queue are
different states, and the bulk UI needs to show what is already up.

- [ ] **Step 2: Run and confirm it fails**

```bash
cd <worktree> && uv run pytest tests/test_boards_queue.py -q
```

Expected: collection ERROR (`grant_lab.boards.queue` does not exist), then FAILED once the
module exists but is empty. **Do not read the ERROR as a pass.**

- [ ] **Step 3: Implement**

`src/grant_lab/boards/queue.py`:

```python
"""The admin approval queue for one board.

WHOSE HIDES. Plan B closed with F-10: `_decision_predicate` requires a user_id
and §4.3's board is profile-scoped with no user in scope. The two halves get
different answers. The PUBLIC board (boards/public.py) applies NO decisions --
spec §6, and correct semantics anyway, since a board is the institution's
published list and not any individual's inbox. This QUEUE applies the PROFILE
OWNER's, which is the 2026-09-05 decisions note D2 and a deliberate override of
§6's "the same query ... feeds the admin approval queue".

Key off `profiles.created_by`, NOT `boards.created_by`. §4.3 gives board creation
to any profile EDITOR, and "can edit" is `created_by == user.id or user.is_admin`
(profiles.py:196), so an admin can build a board on a profile they do not own.
Keying off the board's creator would then apply a passing admin's inbox
housekeeping to somebody else's corridor wall.

This CALLS dashboard._decision_predicate. It does not copy it. A second copy is
how the rail and the feed drift apart under a filter nobody re-tested -- see that
function's own docstring.
"""

from grant_lab.web.dashboard import _decision_predicate
```

with `board_queue` composing:

```python
    owner_id = session.scalar(
        select(Profile.created_by).where(Profile.id == board.profile_id)
    )
    filters = [
        _decision_predicate("archive", user_id=owner_id, profile_id=board.profile_id),
        CallRanking.aggregate_score >= board.min_score,
        Call.deadline.is_not(None),
        Call.deadline > func.now(),
    ]
```

`Call.deadline.is_not(None)` is redundant in SQL (a bare `>` already excludes NULL) and is
written anyway: it states the intent at the site, and the same pair is read back by the
public query and by the frontend's local expiry check.

- [ ] **Step 4: Put the caller note on `_decision_predicate`, and LEAVE `_base_call_filters` alone**

**Do not touch `_base_call_filters`'s docstring.** An earlier draft of this plan told you
to rewrite `dashboard.py:313-315` to name `boards.queue.board_queue` as a third caller.
That is the exact failure the sentence exists to prevent — it would have made a true
docstring false. Re-measured at `05bd151c`:

```
$ grep -rn "_base_call_filters" src/
src/grant_lab/web/dashboard.py:305:def _base_call_filters(
src/grant_lab/web/dashboard.py:341:    THREE calls to `_base_call_filters`, one per view -- not one shared "the list
src/grant_lab/web/dashboard.py:367:        filters = _base_call_filters(
src/grant_lab/web/dashboard.py:503:    base_filters = _base_call_filters(

$ grep -rn "_decision_predicate" src/
src/grant_lab/web/dashboard.py:64:def _decision_predicate(view: View, *, user_id: int, profile_id: int | None):
src/grant_lab/web/dashboard.py:322:    filters = [_decision_predicate(view, user_id=user_id, profile_id=profile_id)]
src/grant_lab/web/dashboard.py:459:    # for the ORDER BY. It cannot fire without a profile: `_decision_predicate`
```

`_base_call_filters` has **two** callers and keeps them. `_decision_predicate` has **one**
call site (`:322`, inside `_base_call_filters`) and its docstring (`:65-77`) enumerates
none. Step 3 makes `board_queue` its second, and its first outside `web/dashboard.py`.

**The decision, stated so it is not re-litigated.** The alternative was to make
`board_queue` genuinely call `_base_call_filters(view="archive", user_id=owner_id,
profile_id=board.profile_id, source_id=None, q=None)`, which would have made the old
Step-4 text true. This plan **rejects** that: with `source_id` and `q` both `None` it
returns exactly `[_decision_predicate("archive", ...)]` — the same thing Step 3 builds, via
two `None`s the queue never uses — and it silently subscribes the corridor board to any
future dashboard-only filter. Reuse of the predicate is the guarantee D2 wants; reuse of
the dashboard's filter *assembly* is coupling. `PLAN-DEFECTS.md` D6, option (a).

So: append to `_decision_predicate`'s docstring (`dashboard.py:65-77`), inside the existing
triple quotes:

```
    CALLERS. `_base_call_filters` (:322) and, from OUTSIDE this module,
    `grant_lab.boards.queue.board_queue`. The board queue is the odd one: it
    passes the PROFILE OWNER's user_id rather than the requesting user's, which
    is exactly why it CALLS this instead of forking it (2026-09-05 decisions
    note D2). A second copy is how the rail and the feed drift apart under a
    filter nobody re-tested.
```

**This is not documentation tidying.** An out-of-package caller of a private function is
the thing a later reader will "clean up"; the note is what stops them.

- [ ] **Step 5: Run the tests**

```bash
cd <worktree> && uv run pytest tests/test_boards_queue.py -q
```

- [ ] **Step 6: Prove the gate BITES on the exact confusion D2 names**

- *What trivially passes it:* keying the predicate on `boards.created_by`. That is the
  mistake D2 exists to prevent, and it passes most of this file.
- Mutate: `owner_id = board.created_by`.

```bash
cd <worktree> && uv run pytest tests/test_boards_queue.py -q
```

Record: `against good input: PASS` / `against owner_id = board.created_by: FAIL on at
least test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue AND
test_a_call_the_owner_hid_is_absent_from_the_queue — the wrong key does not merely ADD the
admin's hides, it drops the OWNER's, so the positive case goes red too`.

**Do not carry a pass-count literal here.** An earlier draft said "passes seven of the
eight" and named one failing test; the dry run measured `2 failed, 6 passed` on eight
tests. With the ninth case added the arithmetic moves again. Re-derive it; the gate is
stronger than either number claimed. `PLAN-DEFECTS.md` D7.

Second mutation, for the reuse claim: replace the `_decision_predicate` call with an
inline `Call.id.notin_(select(CallDecision.call_id).where(CallDecision.user_id ==
owner_id))` — a hand-rolled copy that drops **two** clauses: `state == "hidden"` and
`CallDecision.profile_id == profile_id`.

Record: `against a hand-rolled copy missing the state and profile filters: FAIL on
test_a_hide_on_a_DIFFERENT_profile_does_not_suppress (the dropped profile_id) and
test_a_call_the_owner_SAVED_is_still_in_the_queue (the dropped state) — two tests, two
dropped clauses, one each`.

**An earlier draft attributed both failures to the state clause and named two tests that
stay green.** Neither `test_an_already_approved_call_is_flagged_not_dropped` nor
`test_a_call_a_COLLEAGUE_hid_is_still_in_the_queue` creates a saved decision, so neither
can see the state clause; the dry run measured exactly one failure from this mutation, and
it was the profile one. The ninth case (Step 1) is what makes the state clause visible at
all. `PLAN-DEFECTS.md` D8.

To isolate the state clause on its own, use a mutation that **keeps** `profile_id` and
drops only `CallDecision.state == "hidden"`; that must fail on
`test_a_call_the_owner_SAVED_is_still_in_the_queue` alone. If either mutation reddens
**nothing**, the tests do not cover saves and that is a finding to write down, not a pass.

- [ ] **Step 7: Full suite, lint, commit**

**Expected DELTA: +9** — the eight original cases plus D8's
`test_a_call_the_owner_SAVED_is_still_in_the_queue`. The docstring edit in Step 4 adds no
tests. `1 skipped` unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): the approval queue applies the profile owner's hides

A deliberate override of spec §6's userless queue: profiles default to 'org', so
a userless queue that respected everyone's triage would let any logged-in user
keep a call off a public wall. Calls _decision_predicate rather than forking it."
```

---

### Task 5: Board CRUD, settings, and the D7 flip refusal

**Files:**
- Create: `src/grant_lab/web/api/boards.py`
- Modify: `src/grant_lab/web/api/__init__.py` (the eager import block `:14-21`, and `_ALWAYS` `:35-42`)
- Modify: `tests/architecture/test_edition_surface.py` — **two** literals: `_ALWAYS_ROUTERS` (`:242-249`) **and** the `109` route-count literal (`:175`)
- Modify: `tests/architecture/full_edition_routes.txt`
- Modify: `frontend/src/api/schema.gen.ts` (**generated**)
- Test: `tests/web/test_api_boards_crud.py`

**Interfaces:**
- Consumes: `_profile_for_edit` (`profiles.py:126`), `ORIENTATION_ITEMS_PER_PAGE` (Task 2)
- Produces:
  - `GET /api/boards` — boards on profiles the caller can read
  - `POST /api/boards` — create; body `{title, profile_id, orientation}`
  - `GET /api/boards/{board_id}` — settings + counts
  - `PUT /api/boards/{board_id}` — settings; **409 on a capacity-shrinking flip**
  - `DELETE /api/boards/{board_id}` — creator or admin (§4.3)

**Permissions come from ONE place.** §4.3: "Boards reuse that rule exactly, so no second
permission model appears." That is satisfied by **calling `_profile_for_edit`** and by
nothing weaker. Import it as-is:

```python
from grant_lab.web.api.profiles import _profile_for_edit
```

The leading underscore is kept deliberately — it reads as "this is `profiles.py`'s gate,
borrowed", which is exactly right, and renaming it would churn six pin entries in
`test_profile_access_single_source.py` for no gain.

**`boards.py` must import nothing under `grant_lab.proposal_assist`.** It goes in
`_ALWAYS` (both editions) and `web/api/__init__.py` is a package `__init__`, so an eager
import there runs on every `import grant_lab.web.api.<anything>`. That is what made
`APP_EDITION=match` a route cut rather than a process cut once before (`api/__init__.py:7-13`).

**Slug generation:** `secrets.token_urlsafe(16)` — 16 bytes, 22 URL-safe characters, over
the §4.3 floor of 16 characters and under the model's `String(64)`. Not a UUID: §4.3 asks
for "random, URL-safe, ≥16 chars", and a corridor URL a human may have to read aloud is
better short.

- [ ] **Step 1: Write the failing test**

`tests/web/test_api_boards_crud.py`:

```python
def test_a_profile_editor_can_create_a_board(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_an_admin_can_create_a_board_on_a_profile_they_do_not_own(monkeypatch, pg_url, pg_session_factory, ...): ...  # §4.3
def test_a_reader_who_cannot_edit_the_profile_gets_403(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_a_stranger_gets_404_not_403(monkeypatch, pg_url, pg_session_factory, ...): ...            # no existence oracle
def test_a_new_board_is_unpublished(monkeypatch, pg_url, pg_session_factory, ...): ...             # §4.3 "nothing is public until this flips"
def test_the_slug_is_at_least_16_chars_and_url_safe(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_two_boards_get_different_slugs(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_choosing_portrait_sets_items_per_page_to_4(monkeypatch, pg_url, pg_session_factory, ...): ...       # D7
def test_choosing_landscape_sets_items_per_page_to_8(monkeypatch, pg_url, pg_session_factory, ...): ...      # D7
def test_items_per_page_can_still_be_overridden(monkeypatch, pg_url, pg_session_factory, ...): ...           # D7 "remains an override"
def test_flipping_to_portrait_over_capacity_is_REFUSED_with_the_count(monkeypatch, pg_url, pg_session_factory, ...): ...  # Open-4
def test_flipping_to_portrait_under_capacity_is_allowed(monkeypatch, pg_url, pg_session_factory, ...): ...   # Open-4
def test_flipping_to_landscape_is_always_allowed(monkeypatch, pg_url, pg_session_factory, ...): ...          # Open-4: capacity only rises
def test_the_creator_can_delete_a_board(monkeypatch, pg_url, pg_session_factory, ...): ...                   # §4.3
def test_an_admin_can_delete_someone_elses_board(monkeypatch, pg_url, pg_session_factory, ...): ...          # §4.3
def test_a_non_creator_non_admin_editor_cannot_delete(monkeypatch, pg_url, pg_session_factory, ...): ...     # §4.3: delete is narrower
def test_a_non_admin_cannot_publish_through_the_settings_route(monkeypatch, pg_url, pg_session_factory, ...): ...
```

The last one matters: `is_published` is admin-only (§4.3) and `PUT /api/boards/{id}` is
editor-accessible. A settings route that accepts `is_published` in its body is a
privilege escalation with no new endpoint. The test sends `{"is_published": true}` as a
non-admin editor and asserts the board is **still unpublished** afterwards — reading the
row, not just the status code, because a 200 that silently ignores the field and a 200
that honours it look identical from outside.

`test_flipping_to_portrait_over_capacity_is_REFUSED_with_the_count` asserts the response
**body names the number to remove**, not merely that it is a 409. Open-4's whole point is
that the human can see the loss.

**`test_a_stranger_gets_404_not_403` must assert the 404 BODY here too** — same vacuity as
Task 3 Step 1, same fix. A route that does not exist yet answers
`404 {"error":{"code":"not_found","message":"Not Found"}}` (measured), so a status-only
assertion is green before any of this task is written, and stays green if the router is
never registered — which is a real failure mode, because Step 3's `_ALWAYS` edit is easy to
forget. The refusal you actually want comes from `_profile_for_edit`
(`profiles.py:139/141`), so assert `b"profile not found" in r.content`, and add the owner's
identical request returning 200 as a positive control in the same test.
`PLAN-DEFECTS.md` D3.

- [ ] **Step 2: Run and confirm failure**

```bash
cd <worktree> && uv run pytest tests/web/test_api_boards_crud.py -q
```

Expected: **17 FAILED, 0 passed** — every case, on a 404 from FastAPI for a route that is
not registered. Not ERROR. **If the stranger case PASSES, its body assertion is missing**;
go back to Step 1.

- [ ] **Step 3: Implement the router, and register it**

The router itself opens:

```python
router = APIRouter(tags=["boards"])
```

**No `prefix`.** `build_api_router` already mounts every `_ALWAYS` sub-router under `/api`
— `web/api/__init__.py:100-101` constructs `APIRouter(prefix="/api", ...)` and `:105`
does `r.include_router(sub)` — and every existing sub-router is bare (`auth.py:20`,
`calls.py:36`, `profiles.py:35`, all `APIRouter()`). So paths in this file are written
`"/boards"`, **not** `"/api/boards"`, even though every path in this plan is quoted with
the `/api`. Writing `APIRouter(prefix="/api", ...)` — the natural reading of the Produces
block above — mounts everything at `/api/api/boards`, and the symptom is a 404 on all 17
tests, **indistinguishable from "the router was never registered"**. It cost the dry run a
full test cycle plus a traceback read. `PLAN-DEFECTS.md` D10.

In `src/grant_lab/web/api/__init__.py`, add to the eager import block (`:14-21`) and to
`_ALWAYS` (`:35-42`), with a one-line reason:

```python
from grant_lab.web.api import (
    admin,
    admin_users,
    auth,
    boards,
    calls,
    me,
    profiles,
    public_boards,
)
...
_ALWAYS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    calls.router,
    profiles.router,
    # Boards are a MATCH-edition feature: the corridor screen is release 1's
    # public face. Eager import is safe ONLY while neither module imports
    # anything under grant_lab.proposal_assist -- this file is a package
    # __init__, so an import here runs in every process.
    boards.router,
    public_boards.router,
)
```

(`public_boards` is created in Task 7; add it there, not here, so each task's route
regeneration is its own.)

**And the second literal, in the SAME diff.** `tests/architecture/test_edition_surface.py`
holds `_ALWAYS_ROUTERS` (`:242-249`), and
`test_the_always_mounted_routers_are_exactly_the_reviewed_set` (`:279-284`) AST-parses
`_ALWAYS` and compares it to that list **element for element, in order**:

```python
_ALWAYS_ROUTERS = [
    "auth.router",
    "me.router",
    "admin.router",
    "admin_users.router",
    "calls.router",
    "profiles.router",
    # Boards are a MATCH-edition feature: the corridor screen is release 1's
    # public face.
    "boards.router",
]
```

Editing `_ALWAYS` alone is red, and the plan's earlier draft did not mention this file at
all. The gate's own failure message says why it exists: "adding one is a product decision:
does the match edition need it? If yes, add it to `_ALWAYS_ROUTERS` with that reasoning."
The comment above is that reasoning. Task 7 hits the same gate for `public_boards.router`.
`PLAN-DEFECTS.md` D9.

The flip refusal:

```python
    new_cap = new_items_per_page * board.max_pages
    if new_cap < approved_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this board has {approved_count} approved calls; "
                f"{body.orientation} holds {new_cap} — remove "
                f"{approved_count - new_cap} first"
            ),
        )
```

Spec §6 forbids "the display silently truncating a list an admin approved: they would have
no way to see the loss." Dropping the lowest-positioned items here is that same loss moved
to save time, so the flip refuses and names the count.

- [ ] **Step 4: Regenerate route artifacts and run the gates**

Same three commands as Task 3 Step 5 (`gen_openapi_types.sh`, regenerate
`full_edition_routes.txt`, bump the literal by hand from `wc -l`).

```bash
cd <worktree> && uv run pytest tests/web/test_api_boards_crud.py tests/architecture -q
```

- [ ] **Step 5: Prove three gates BITE**

**Gate A — the privilege-escalation test.**
- *What trivially passes it:* asserting only `response.status_code == 403`. A route that
  returns 200 and quietly ignores the field also passes a status-only assertion — and so
  does one that returns 200 and **honours** it.
- Mutate: make `PUT /api/boards/{id}` copy `body.is_published` onto the row.
- Expected: `test_a_non_admin_cannot_publish_through_the_settings_route` FAILS on the
  **row read**, not on the status code. Record which assertion fired.

**Gate B — the flip refusal.**
- *What trivially passes it:* refusing every orientation change. That passes the
  over-capacity case and the always-allowed-to-landscape case is what catches it.
- Mutate: refuse unconditionally when `orientation` changes.
- Expected: FAIL on `test_flipping_to_landscape_is_always_allowed` and
  `test_flipping_to_portrait_under_capacity_is_allowed`.

**Gate C — the match edition stays a process cut.**
- *What trivially passes it:* nothing — until someone adds a convenience import.
- Mutate: add `from grant_lab.proposal_assist.collab.permissions import signal_user_revocation`
  at module scope in `web/api/boards.py`.

```bash
cd <worktree> && uv run pytest tests/architecture/test_match_edition_process_cut.py -q
```

Expected: **2 failed, 2 passed** in that file — `test_a_match_boot_imports_no_proposal_assist_module_at_all`
with a non-zero count, **and** `test_a_match_boot_loads_none_of_the_heavy_proposal_assist_dependencies`.
The dry run measured:

```
AssertionError: a match boot imported pycrdt, which only the full edition needs.
  probe: {'proposal_assist': 61, 'grant_lab': 156, 'pycrdt': True,
          'claude_agent_sdk': False, 'prosemirror': True, 'routes': 50}
AssertionError: a match boot imported 61 proposal_assist modules.
```

**`claude_agent_sdk` stays `False`.** That import pulls pycrdt and prosemirror but not the
SDK. The second test loops over three deps (`test_match_edition_process_cut.py:87-88`) and
fails on the first that flips, so expect it to name **pycrdt** — an earlier draft listed all
three as expected flips. `PLAN-DEFECTS.md` D11.

This gate boots a **subprocess**, so an in-process check would have proven nothing. It
measures after `create_app()` and before the lifespan — see "What the match-edition
process-cut gate does and does not prove" in Global Constraints. A module-scope import in
`web/api/boards.py` is reachable from `create_app()` (the router is in `_ALWAYS`, eagerly
imported by the package `__init__`), so it is squarely inside what this gate can see.

Restore after each; clear `__pycache__`; `git status --porcelain`.

- [ ] **Step 6: Full suite, lint, commit**

**Expected DELTA: +17.** `1 skipped` unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): create, configure and delete a board

Orientation is chosen at creation and items_per_page follows it, so the approval
cap is truthful. A flip that would strand approved calls is refused with the
number to remove rather than silently truncating."
```

---

### Task 6: Approve, un-approve, reorder, publish

**Files:**
- Modify: `src/grant_lab/web/api/boards.py`
- Modify: `tests/architecture/full_edition_routes.txt` + the literal
- Modify: `frontend/src/api/schema.gen.ts` (**generated**)
- Test: `tests/web/test_api_board_items.py`

**Interfaces:**
- Consumes: `board_queue` (Task 4), `_profile_for_edit`
- Produces:
  - `GET /api/boards/{board_id}/queue` — the D2 candidate list + `waiting_count`
  - `GET /api/boards/{board_id}/items` — approved, in `position` order
  - `POST /api/boards/{board_id}/items` — bulk approve; body `{call_ids: list[int]}` **or** `{min_score: int}`
  - `PUT /api/boards/{board_id}/items` — bulk reorder; body `{call_ids: list[int]}` in the new order
  - `DELETE /api/boards/{board_id}/items/{call_id}` — un-approve
  - `PUT /api/boards/{board_id}/publish` — **admin only**; body `{is_published: bool}`

**The bulk affordances are required, not nice-to-have.** §4.3: "the admin queue must be
built for bulk: a checklist ordered by score, an 'approve everything at or above N'
action, and a visible count of what is waiting. An approval UI that costs a minute per
call will be abandoned, and an abandoned gate is worse than no gate." The note is blunt
that D2 alone is not a defence against the abandoned gate — it shortens the queue *only if
that owner actually triages*.

**The cap belongs here.** §6: "Capacity is `items_per_page` × `max_pages`, 24 by default,
and approval is capped at it… the admin queue refuses the 25th approval and names what to
drop." Computed from the **stored** `items_per_page` (D7), so it is always truthful.

**Un-approving is NOT admin-only.** §4.3 gives approve/reorder to any profile editor; only
`is_published` is admin-gated. D3: the owner can take a card down deliberately, on the
board — a hide never removes a card that is already up.

- [ ] **Step 1: Write the failing test**

```python
def test_approving_writes_approved_by_and_approved_at(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_bulk_approve_by_call_ids(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_approve_everything_at_or_above_N(monkeypatch, pg_url, pg_session_factory, ...): ...           # §4.3 bulk
def test_the_queue_reports_a_waiting_count(monkeypatch, pg_url, pg_session_factory, ...): ...          # §4.3 bulk
def test_approving_past_capacity_is_refused_and_names_the_overflow(monkeypatch, pg_url, pg_session_factory, ...): ...  # §6
def test_capacity_uses_the_STORED_items_per_page_not_a_default(monkeypatch, pg_url, pg_session_factory, ...): ...      # D7
def test_a_bulk_approve_that_would_exceed_capacity_approves_NONE(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_an_editor_who_is_not_admin_can_approve(monkeypatch, pg_url, pg_session_factory, ...): ...     # §4.3
def test_an_editor_who_is_not_admin_can_UNapprove(monkeypatch, pg_url, pg_session_factory, ...): ...   # D3
def test_an_editor_who_is_not_admin_CANNOT_publish(monkeypatch, pg_url, pg_session_factory, ...): ...  # §4.3
def test_an_admin_can_publish(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_publishing_records_published_by_and_published_at(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_unpublishing_is_admin_only_too(monkeypatch, pg_url, pg_session_factory, ...): ...             # §4.3 "(and un-publish)"
def test_publishing_a_board_on_a_PRIVATE_profile_is_allowed(monkeypatch, pg_url, pg_session_factory, ...): ...  # Open-5
def test_reorder_rewrites_positions_and_rejects_an_unknown_call_id(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_a_hide_after_approval_does_NOT_remove_the_item(monkeypatch, pg_url, pg_session_factory, ...): ...      # D3
```

`test_a_bulk_approve_that_would_exceed_capacity_approves_NONE` is the one that makes the
cap honest: a partial bulk approve is the silent truncation §6 forbids, just performed by
the server instead of the display.

`test_a_hide_after_approval_does_NOT_remove_the_item` is D3 stated as a test: approve a
call, then have the owner hide it, then read `/items` and assert it is still there. It is
the exact behaviour a later reader would "fix".

- [ ] **Step 2: Run and confirm failure**

- [ ] **Step 3: Implement**

Publish is a separate route with `Depends(require_admin)` — **not** a field on the
settings route. §4.3 separates them because "I picked these calls" and "I put them on a
public wall" are different claims. A field on a shared route makes the second reachable by
whoever can make the first.

Publishing sets `published_by` and `published_at`; **unpublishing leaves them**, because
they record who made it public and when, and that question survives the board coming down.

- [ ] **Step 4: Regenerate route artifacts** (three commands, as Task 3 Step 5)

- [ ] **Step 5: Prove two gates BITE**

**Gate A — the capacity cap.**
- *What trivially passes it:* hardcoding capacity as `8 * max_pages`. That is right for
  every landscape board, and `test_capacity_uses_the_STORED_items_per_page_not_a_default`
  is the portrait board that catches it.
- Mutate: `cap = 8 * board.max_pages`.
- Expected: FAIL on **at least two** — `test_capacity_uses_the_STORED_items_per_page_not_a_default`
  **and** `test_a_bulk_approve_that_would_exceed_capacity_approves_NONE`. The dry run
  measured `2 failed, 31 passed`: both fixtures are portrait boards, so both see the wrong
  cap. An earlier draft named only the first. `PLAN-DEFECTS.md` D11.

**Gate B — publish is admin-only.**
- *What trivially passes it:* `Depends(get_current_user)` instead of `require_admin`.
  `tests/architecture/test_auth_allowlist.py` stays **green** on that swap — its own
  docstring says so: "This is an AUTHENTICATION gate, not an authorization one."
- Mutate: swap the dependency.
- Expected: **at least two** API tests FAIL — `test_an_editor_who_is_not_admin_CANNOT_publish`
  and `test_unpublishing_is_admin_only_too`, since both routes lose the same guard — while
  `tests/architecture/test_auth_allowlist.py` stays at **10 passed**. The dry run measured
  that green half verbatim. **Record both halves** — the green half is the finding: this
  authorization rule is held by the API tests alone.

- [ ] **Step 6: Full suite, lint, commit**

**Expected DELTA: +16.** `1 skipped` unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): bulk approval, reordering, and admin-only publishing

Approval is capped at the stored items_per_page x max_pages and a bulk approve
that would exceed it approves none, because a partial approve is the silent
truncation §6 forbids performed by the server instead of the display."
```

---

### Task 7: QR encoding and the userless public endpoint

**Files:**
- Create: `src/grant_lab/boards/qr.py`
- Create: `src/grant_lab/boards/public.py`
- Create: `src/grant_lab/web/api/public_boards.py`
- Modify: `pyproject.toml`, `uv.lock`
- Modify: `src/grant_lab/web/api/__init__.py`
- Modify: `src/grant_lab/main.py` (a board IP limiter beside the two at `:842-845`)
- Modify: `tests/architecture/test_auth_allowlist.py` (`PUBLIC_ROUTES`, `:38-50`)
- Modify: `tests/architecture/test_edition_surface.py` — **two** literals again: `_ALWAYS_ROUTERS` (`:242-249`), for `public_boards.router`, **and** the route-count literal (`:175`). Same gate as Task 5 Step 3; it fires a second time here.
- Modify: `tests/architecture/full_edition_routes.txt`
- Test: `tests/test_boards_qr.py`, `tests/web/test_api_public_board.py`

**Interfaces:**
- Produces:
  ```python
  # boards/qr.py
  @dataclass(frozen=True)
  class QrCode:
      modules: int   # matrix width, EXCLUDING the quiet zone
      path: str      # an SVG `d` string in module units

  def qr_for(url: str) -> QrCode: ...
  ```
  ```python
  # GET /api/public/boards/{slug}  ->
  {
    "title": str,
    "generated_at": str,                 # ISO-8601
    "orientation": "landscape" | "portrait",
    "items_per_page": int,
    "max_pages": int,
    "page_seconds": int,
    "items": [{
      "title": str, "source_name": str, "deadline_local_date": str | None,
      "budget_min": int | None, "budget_max": int | None, "currency": str | None,
      "url": str, "qr_modules": int, "qr_path": str,
    }]
  }
  ```

**The payload is a closed list.** §6: "Returns exactly the card fields and nothing else."
Deliberately absent, per §6's "what is deliberately not public": score numbers, LLM
rationale, criterion chips, `quoted_evidence`, `applicant_notes`, any profile identity
beyond the board's own title, and any call whose deadline has passed. There is **no**
`call_id` and no `profile_id` in the payload — nothing that lets a scraper join back.

**`deadline_local_date` comes from `web/filters.py:37`.** Its docstring names it "the
SINGLE shared localization helper used by both the digest and the JSON API… prevents them
from ever diverging". A second Stockholm conversion on the board would be that divergence.

**This query is genuinely userless.** §6, and D1: "a board is the institution's published
list, not any individual's inbox." It touches `board_items`, `calls` and `sources`, and
**no decision table at all**. `build_dashboard_view` cannot be reused — its `user_id` is a
required keyword-only argument (`dashboard.py:377-390`) and borrowing an admin's id "would
silently apply one person's hides to a corridor wall."

**Unpublished → 404**, indistinguishable from a nonexistent slug (§6). The same 404 for
both, from the same code path, or the endpoint becomes a slug oracle.

**Rate limit: 60 per 15 minutes per IP.** Reuses `SlidingWindowLimiter`
(`auth/rate_limit.py:7`), whose constructor is
`(max_requests, window_seconds, *, name="unnamed", clock=time.monotonic)` and whose
`allow(key) -> bool` is at `:38`; key it with `client_ip(request)`
(`web/client_ip.py:19`). Sixty is chosen against D6: a 15-minute window over a ~5-minute
cache is ~3 requests per legitimate screen, so 60 is 20× headroom for several screens
behind one NAT while still bounding a scraper.

- [ ] **Step 1: Add the dependency**

```bash
cd <worktree> && uv add segno
cd <worktree> && uv lock --check
```

Measured in a throwaway project so `pyproject.toml` was never touched during authoring:
`Resolved 2 packages`, `+ segno==1.6.6` — pure Python, zero runtime dependencies.
CI runs `uv lock --check` (`.github/workflows/ci.yml:35`), so `uv.lock` must be in this
commit.

- [ ] **Step 2: Write the failing QR test**

`tests/test_boards_qr.py`:

```python
"""QR codes are encoded server-side, from segno's documented matrix.

No QR encoder exists on either side of this repo and `pnpm add` is blocked by
the 7-day minimumReleaseAge cooldown, so the frontend cannot gain one. The
payload therefore carries path data, not an image URL: D6 requires the
last-good copy to keep rendering through a transport failure, and eight broken
QR images beside eight live titles is worse than a stale board.

Built from `q.matrix` rather than by parsing segno's SVG serializer, because a
regex over a library's output pins a private format.
"""

def test_a_qr_encodes_the_url_and_round_trips(): ...
def test_the_matrix_excludes_the_quiet_zone(): ...
def test_the_path_is_only_move_line_and_close_commands(): ...
def test_a_longer_url_needs_more_modules(): ...
def test_the_path_contains_no_angle_bracket_or_quote(): ...
def test_the_symbol_is_never_a_micro_qr(): ...
```

`test_a_qr_encodes_the_url_and_round_trips` parses the emitted path back into a **matrix**
(a path parser, not a QR decoder — see Step 4) and compares it to
**`segno.make_qr(url, error="m").matrix`** — the SAME call `qr_for` makes. A test that only
checks the path is non-empty would pass on a QR that encodes the wrong string, which is the
failure that matters, because a wrong QR on a wall looks exactly like a right one.

**It must be `make_qr(url, error="m")`, not `segno.make(url)`.** Those are different
encoders, and an earlier draft compared the two. `segno.make()` with no `error` picks the
smallest symbol that fits **including a Micro QR**, and Micro QR M4 only supports level L.
Re-measured with segno 1.6.6: for `https://a.se/x` (14 chars) `make()` gives **M4 / level L
/ 17 rows** where `make_qr(error="m")` gives **version 1 / 21 rows**; for
`https://example.test/x` (22 chars) they agree. So the round-trip gate's verdict would be a
property of how long the fixture URL happens to be, and a later reader who shortens one
turns it red for no reason a traceback explains. `PLAN-DEFECTS.md` D12.

`test_the_symbol_is_never_a_micro_qr` uses **`https://a.se/` (13 characters)** as its
fixture and asserts `is_micro is False`. That fixture is deliberate: every other URL in this
task is long enough to hide the bug D14 found, and a Micro QR round-trips perfectly, so no
other test in this file can see it. `PLAN-DEFECTS.md` D14.

`test_the_path_contains_no_angle_bracket_or_quote` is the injection gate: the path is
rendered into an SVG **attribute**, and this pins that the generator can never emit
something that escapes it.

- [ ] **Step 3: Implement `qr_for`**

```python
def qr_for(url: str) -> QrCode:
    """The QR for one call's URL, as SVG path data in module units.

    `q.matrix` is documented, is int 0/1, and EXCLUDES the quiet zone (measured:
    a 33-module symbol has 33 matrix rows and symbol_size(scale=1, border=0) ==
    (33, 33)). The 4-module quiet zone the QR spec requires is supplied by the
    renderer's viewBox, not by this path.

    Error level M: spec §6 puts the QR at 170 px on a screen someone steps up to,
    where the extra correction of Q/H buys nothing and costs modules.

    `make_qr`, NOT `make`. `error="m"` constrains the correction LEVEL, not the
    symbol FAMILY, and Micro QR M4 supports level M -- so `segno.make(url,
    error="m")` returns a 17x17 Micro QR for a 13-character URL (measured, segno
    1.6.6: 'https://a.se/' -> version M4, is_micro True, 17 rows; `make_qr` gives
    version 1, 21 rows). Many phone cameras and much scanner hardware cannot read
    a Micro QR, and on a corridor wall that is a silent per-card failure that
    looks exactly like a working QR. `make_qr` refuses the family outright;
    `make(url, error="m", micro=False)` is equivalent.
    """
    q = segno.make_qr(url, error="m")
    parts: list[str] = []
    for y, row in enumerate(q.matrix):
        x = 0
        while x < len(row):
            if row[x]:
                run = 1
                while x + run < len(row) and row[x + run]:
                    run += 1
                parts.append(f"M{x} {y}h{run}v1h-{run}z")
                x += run
            else:
                x += 1
    return QrCode(modules=len(q.matrix), path="".join(parts))
```

- [ ] **Step 4: Write the failing endpoint test**

```python
def test_a_published_board_returns_its_cards(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_an_unpublished_board_is_404(monkeypatch, pg_url, pg_session_factory, ...): ...                        # §6
def test_a_nonexistent_slug_is_the_SAME_404(monkeypatch, pg_url, pg_session_factory, ...): ...                 # §6, byte-equal body
def test_no_authentication_is_required(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_a_past_deadline_call_is_absent(monkeypatch, pg_url, pg_session_factory, ...): ...                     # §6
def test_the_payload_carries_no_score_or_rationale(monkeypatch, pg_url, pg_session_factory, ...): ...          # §6
def test_the_payload_carries_no_profile_identity(monkeypatch, pg_url, pg_session_factory, ...): ...            # §6
def test_the_payload_carries_no_call_id(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_items_come_back_in_position_order(monkeypatch, pg_url, pg_session_factory, ...): ...                  # §6
def test_the_OWNERS_hides_do_NOT_suppress_a_card(monkeypatch, pg_url, pg_session_factory, ...): ...            # D1
def test_an_ADMINS_hides_do_NOT_suppress_a_card(monkeypatch, pg_url, pg_session_factory, ...): ...             # D1
def test_a_call_edited_after_approval_shows_the_NEW_title(monkeypatch, pg_url, pg_session_factory, ...): ...   # D8
def test_deadline_local_date_matches_web_filters(monkeypatch, pg_url, pg_session_factory, ...): ...            # the shared helper
def test_every_item_carries_a_qr_path(monkeypatch, pg_url, pg_session_factory, ...): ...
def test_the_qr_points_at_the_funder_url_not_this_app(monkeypatch, pg_url, pg_session_factory, ...): ...       # §6
def test_the_rate_limiter_refuses_the_61st_request_in_a_window(monkeypatch, pg_url, pg_session_factory, ...): ...
```

`test_the_OWNERS_hides_do_NOT_suppress_a_card` is D1 as a test, and it is the direct
inverse of Task 4's queue tests. Together they pin F-10's asymmetry — which the note calls
"the single most likely thing to be 'fixed' by a later reader".

`test_a_nonexistent_slug_is_the_SAME_404` compares **response bodies**, not just status
codes. Two different 404 messages make the endpoint a slug oracle.

`test_the_qr_points_at_the_funder_url_not_this_app` **rebuilds the matrix from `qr_path`
and asserts it equals `segno.make_qr(call.url, error="m").matrix`** — a re-encode, not a
decode. §6: "A QR that dead-ends on a login screen is worse than no QR."

**Why not a decode.** Turning a module matrix back into the encoded STRING needs a QR
*decoder*, and segno is an encoder only — it has no `decode`. Measured, nothing in this
environment can do it either:

```
$ for m in pyzbar zxing cv2 qrcode PIL numpy zxingcpp qreader; do
    uv run python -c "import $m" 2>/dev/null && echo "$m PRESENT" || echo "$m ABSENT"; done
pyzbar ABSENT   zxing ABSENT   cv2 ABSENT   qrcode ABSENT
PIL PRESENT     numpy PRESENT  zxingcpp ABSENT   qreader ABSENT
```

PIL and numpy read pixels; neither decodes QR. Adding a decoder is a new dependency for a
test. The re-encode has the same discriminating power for the failure that matters — a QR
pointing at the wrong URL — because two different strings do not produce the same matrix.
Step 2's wording ("back to a matrix") was already fine; this one said "the decoded string".
`PLAN-DEFECTS.md` D13.

- [ ] **Step 5: Implement, register, and allowlist the route**

`public_boards.py` opens `router = APIRouter(tags=["public-boards"])` — **no `prefix`**, for
the same reason as Task 5 Step 3 (`web/api/__init__.py:100-101` supplies `/api`). Its path
is written `"/public/boards/{slug}"`.

Register it in the eager import block and `_ALWAYS` (`web/api/__init__.py:14-21`, `:35-42`)
**and add `"public_boards.router"` to `_ALWAYS_ROUTERS`** in
`tests/architecture/test_edition_surface.py:242-249`, in the same position, with its own
one-line reason. This is the second time this gate fires; see Task 5 Step 3.
`PLAN-DEFECTS.md` D9.

Add to `tests/architecture/test_auth_allowlist.py::PUBLIC_ROUTES`:

```python
        # The corridor board. Deliberately unauthenticated: it is a screen on a
        # wall, and §6's payload carries no score, no rationale and no profile
        # identity. Rate-limited at 60/15min per IP and cached ~5 minutes.
        ("GET", "/api/public/boards/{slug}"),
```

That file's docstring calls adding an entry "the reviewed act" — this comment is the review.

- [ ] **Step 6: Regenerate route artifacts** (three commands, as Task 3 Step 5)

- [ ] **Step 7: Prove four gates BITE**

**Gate A — the auth allowlist actually notices a public route.**
- *What trivially passes it:* nothing; but it is worth proving it is armed.
- Mutate: add a second unauthenticated route, `GET /api/public/boards/{slug}/debug`, and
  do **not** allowlist it.
- Expected: `test_auth_allowlist.py` FAILS naming that path. Record it. If it does not
  fire, the gate is not covering the new router and that is a finding.

**Gate B — the QR encodes the right string.**
- *What trivially passes it:* asserting `len(path) > 0`. Every QR passes that.
- Mutate: `segno.make_qr(url[:-1], error="m")` — one character short.
- Expected: `test_a_qr_encodes_the_url_and_round_trips` and
  `test_the_qr_points_at_the_funder_url_not_this_app` both FAIL. If only the first fires,
  the endpoint test is not **re-encoding and comparing** and should be.
- **Watch `test_the_symbol_is_never_a_micro_qr` under this mutation.** Its fixture is 13
  characters, so `url[:-1]` makes it 12 — still short. Under the ORIGINAL `segno.make`
  implementation this mutation is what exposed D14 in the first place. With `make_qr` it
  stays green, which is the point.

**Gate C — the public query is userless.**
- *What trivially passes it:* a query that happens to return the right rows because no
  test data has hides on it. That is why Tasks 4 and 7 seed hides in opposite directions.
- Mutate: add `_decision_predicate("archive", user_id=owner_id, profile_id=board.profile_id)`
  to `boards/public.py`'s filters — the *plausible* wrong move, since Task 4 does exactly
  that one file over.
- Expected: FAIL on `test_the_OWNERS_hides_do_NOT_suppress_a_card`.

**Gate D — unpublished is indistinguishable from nonexistent.**
- *What trivially passes it:* both returning 404 with different bodies.
- Mutate: return `detail="board is not published"` for the unpublished case.
- Expected: FAIL on `test_a_nonexistent_slug_is_the_SAME_404` on the **body** comparison.

- [ ] **Step 8: Measure the payload size, and gate it**

```bash
cd <worktree> && uv run pytest tests/web/test_api_public_board.py -q -k payload_size
```

Add a test that builds a full 24-item board with realistic URLs and asserts the serialized
payload is **under 512 KB**.

Build the fixture URL as `"https://www.vinnova.se/en/calls-for-proposals/" + "a" * 83` —
that base is **46** characters, so this is 129. (An earlier draft wrote `"a" * 84`, which
is 130.) Assert `len(long_url) == 129` in the test so the fixture cannot drift, and print
the real byte count.

**Re-measured 2026-09-05, and the authoring estimate did NOT reproduce:** 129 chars → 49
modules → **9100 bytes of path**, so **218,400 bytes (213 KB) of path data** for 24 items.
The command is in Open-9. An earlier draft said 7998 bytes / ≈192 KB and added "Task 7's
gate is written against that number" — it was not the number this plan's own `qr_for`
produces. The dry run additionally measured the whole serialized body at **226,040 bytes
(221 KB)**. 512 KB is 2.3× that, and the point of the assertion is that a future change
which doubles the encoding cost is noticed. `PLAN-DEFECTS.md` D15.

- [ ] **Step 9: Full suite, lint, commit**

**Expected DELTA: +6 (qr) +16 (endpoint) +1 (payload size) = +23.** The qr file gains
`test_the_symbol_is_never_a_micro_qr` over the earlier draft's five, which is where +22
became +23. The `_ALWAYS_ROUTERS` and `PUBLIC_ROUTES` edits add no tests. `1 skipped`
unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): the public board endpoint, with server-side QR codes

The query is genuinely userless — no decision table, so nobody's hides reach a
corridor wall. QRs travel as SVG path data inside the cached payload because
D6's last-good copy must survive a network drop with its QRs intact."
```

---

### Task 8: The D9 alert

**Files:**
- Create: `src/grant_lab/boards/alerts.py`
- Modify: `src/grant_lab/digest/send.py`
- Modify: `src/grant_lab/web/api/boards.py` (surface the suppression on board detail)
- Modify: `frontend/src/api/schema.gen.ts` (**generated**) — `BoardOut` gains `owner_undeliverable`
- Test: `tests/test_boards_alerts.py`

**The route SET does not change**, so `tests/architecture/full_edition_routes.txt` and the
count literal do **not** move. Every other route-touching task in this plan moves both;
this one moves neither. Say so out loud, because a worker who regenerates the baseline here
for nothing will then have to explain a no-op diff.

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class BoardAlert:
      board_id: int
      shortfall: int              # items_per_page minus renderable items
      recipient_ids: list[int]    # the owner, or the escalation set
      owner_undeliverable: bool   # Open-3 half 2: shown in-app

  def boards_needing_attention(session) -> list[BoardAlert]: ...
  ```

**The trigger (Open-2):** a **published** board whose count of renderable approved items —
`board_items` joined to `calls`, `Call.deadline` in the future — is below the board's
**stored** `items_per_page`. Reads `boards`, `board_items`, `calls`. Nothing else.

**The recipient (D9):** `profiles.created_by WHERE profiles.id = boards.profile_id` — the
same person D2 keys the queue off. **Not `boards.created_by`**, which may be an admin who
built the board for somebody else; telling them a queue they do not own is short tells the
wrong person.

**The channel already exists.** `profiles/create.py:79` inserts
`Subscription(user_id=owner_id, profile_id=p.id, digest_enabled=True)` inside the same
atomic create as the profile row, so `subscriptions` is a per-(user, profile) row for every
profile owner today, keyed the same way the board is. **It is a convention, not a
constraint** — no CHECK, FK or unique index enforces it, and
`scripts/seed_dev_data.py:328` writes its row in a *separate* transaction after its own
commit. So `boards_needing_attention` **asserts the row exists rather than trusting it**,
and a missing row is reported the same way an undeliverable owner is, not skipped.

**The flag is `board_alerts_enabled` (Open-1)**, not `digest_enabled`.

**The escalation (Open-3):** when the owner has `User.status != 'active'` **or**
`digest_disabled_at IS NOT NULL` — the two conditions `digest/send.py:38` filters on — the
recipients become every active admin, and `owner_undeliverable` is `True` so the board
detail screen can say so. `send.py:183-184` sets `digest_disabled_at` once
`digest_bounce_count` reaches `BOUNCE_DISABLE_THRESHOLD` (**3**, `send.py:18`) while
leaving `status == 'active'`, so the bounce-disabled owner is a real, distinct case: they
can still log in and triage, and transfer is the wrong remedy for someone who has not left.

- [ ] **Step 1: Write the failing test**

```python
def test_an_underfull_published_board_alerts_its_PROFILE_OWNER(pg_session_factory, ...): ...
def test_a_FULL_board_does_not_alert(pg_session_factory, ...): ...
def test_an_UNPUBLISHED_underfull_board_does_not_alert(pg_session_factory, ...): ...
def test_the_shortfall_counts_against_the_STORED_items_per_page(pg_session_factory, ...): ...  # D7
def test_an_item_whose_deadline_passed_counts_as_MISSING(pg_session_factory, ...): ...
def test_the_BOARD_CREATOR_is_not_notified_when_they_are_not_the_owner(pg_session_factory, ...): ...
def test_board_alerts_enabled_false_suppresses_the_alert(pg_session_factory, ...): ...         # Open-1
def test_digest_enabled_false_does_NOT_suppress_the_board_alert(pg_session_factory, ...): ...  # Open-1
def test_a_DISABLED_owner_escalates_to_active_admins(pg_session_factory, ...): ...             # Open-3
def test_a_BOUNCE_DISABLED_owner_escalates_too(pg_session_factory, ...): ...                   # Open-3
def test_a_bounce_disabled_owner_is_still_status_active(pg_session_factory, ...): ...          # the distinction
def test_a_MISSING_subscription_row_is_reported_not_skipped(pg_session_factory, ...): ...      # D10 convention
def test_owner_undeliverable_is_surfaced_on_the_board_detail_route(monkeypatch, pg_url, pg_session_factory, ...): ...   # Open-3 half 2
```

`test_digest_enabled_false_does_NOT_suppress_the_board_alert` is **the** test for Open-1.
If this plan's flag decision is overruled in review, this is the test that must change, and
naming it this way makes that obvious.

`test_a_MISSING_subscription_row_is_reported_not_skipped` is D10's "assert it rather than
trust it". A `for sub in subscriptions:` loop skips a missing row silently and passes every
other test in this file.

- [ ] **Step 2: Run and confirm failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Regenerate the OpenAPI types — SAME commit**

```bash
cd <worktree> && bash scripts/gen_openapi_types.sh
cd <worktree> && git diff --stat frontend/src/api/schema.gen.ts   # must be non-empty
```

**`pytest` cannot see a stale `schema.gen.ts`.** Adding `owner_undeliverable` to `BoardOut`
changes the OpenAPI schema, but the drift check is **CI-only** —
`.github/workflows/ci.yml:112-115` runs the generator and then
`git diff --exit-code frontend/src/api/schema.gen.ts`. The dry run followed this task
literally, saw `13 passed` and a green full suite, committed, and would have found out on
CI. Task 10 then consumes the field from the frontend and would have been typechecking
against a schema that does not declare it. The regenerated diff should be exactly:

```
+            owner_undeliverable: boolean;
```

`PLAN-DEFECTS.md` D16.

- [ ] **Step 5: Prove three gates BITE**

**Gate A — the two flags are genuinely separate.**
- *What trivially passes it:* reading `digest_enabled`. Both columns default to `True`, so
  a fresh fixture cannot tell them apart.
- Mutate: filter on `Subscription.digest_enabled.is_(True)`.
- Expected: FAIL on `test_digest_enabled_false_does_NOT_suppress_the_board_alert` **and**
  on `test_board_alerts_enabled_false_suppresses_the_alert`. Both, because the two tests
  set the two columns in opposite directions — that pairing is what makes the mutation
  visible at all.

**Gate B — the recipient is the profile owner.**
- *What trivially passes it:* `boards.created_by`. In most fixtures they are the same
  person, which is why one test deliberately makes them different.
- Mutate: `recipient = board.created_by`.
- Expected: FAIL on `test_the_BOARD_CREATOR_is_not_notified_when_they_are_not_the_owner`.

**Gate C — a missing subscription is reported.**
- *What trivially passes it:* iterating subscription rows. No row, no iteration, no alert,
  green.
- Mutate: build the recipient list by iterating `select(Subscription)...` instead of
  resolving the owner first and then looking their row up.
- Expected: FAIL on `test_a_MISSING_subscription_row_is_reported_not_skipped`. **If it
  reddens nothing, the test is not deleting the row and the gate is hollow** — fix the
  test, and record that the first version was hollow.

- [ ] **Step 6: Full suite, lint, commit**

Because `schema.gen.ts` moved, the frontend must still typecheck and build:

```bash
cd <worktree>/frontend && pnpm vitest run && pnpm lint
cd <worktree> && pnpm -C frontend build
```

**Expected DELTA: +13** backend, frontend unchanged. `1 skipped` unchanged. Watch
`digest/send.py` — its tests
(`tests/test_digest_send.py`) must stay green; a board alert that changes the digest's own
recipient set is a defect, not a feature.

```bash
cd <worktree> && git commit -m "feat(boards): tell the profile owner when a board has gone underfull

Its own flag on subscriptions, not digest_enabled: muting a weekly summary must
not mute the alert that a public screen is short. A disabled or bounce-disabled
owner escalates to active admins and the suppression is shown in-app."
```

---

### Task 9: `/b/:slug` — the unattended screen

**Files:**
- Create: `frontend/src/routes/BoardLayout.tsx`, `BoardScreen.tsx`, `BoardCard.tsx`
- Create: `frontend/src/api/publicBoard.ts`
- Modify: `frontend/routes.json`, and both generated artifacts
- Modify: `frontend/src/router.tsx`, `frontend/src/router.simple.tsx`
- Modify: `frontend/src/router.test.tsx`, `frontend/src/router.simple.test.tsx`
- Test: `frontend/src/routes/BoardScreen.test.tsx`, `BoardCard.test.tsx`, `frontend/src/api/publicBoard.test.ts`

**The manifest entry is `"/b"`, not `"/b/:slug"`.** `is_spa_route`
(`scripts/gen_routes.py:75-91`) matches exact-or-subtree
(`path == r or path.startswith(r.rstrip("/") + "/")`, `:89`), so `"/b"` owns `/b/anything`
the way `"/calls"` owns `/calls/1` today.

**Ignore `frontend/routes.json`'s own `_comment`.** It says spa_routes are "matched EXACTLY
(not by prefix)", which contradicts the function above and the behaviour the whole app
relies on. A worker who reads the file they are editing is told the opposite of the truth;
that comment predates the `/calls/1` sub-path behaviour. `PLAN-DEFECTS.md` D19 item 8.

Then:

```bash
cd <worktree> && uv run python scripts/gen_routes.py
```

which rewrites `src/grant_lab/web/spa_routes.py` and `frontend/src/routes.gen.ts`. CI runs
`scripts/gen_routes.py --check` (`.github/workflows/ci.yml:41`).

**BOTH routers must mount it.** A board is a match-edition feature, so `router.simple.tsx`
carries it too — and `router.test.tsx:30` is an **equality**
(`expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES))`), so a manifest entry with no
route in `router.tsx` goes red as loudly as the reverse.
`router.simple.test.tsx:12-28`'s `RELEASE_1_ROUTES` is a written-out **allowlist** whose
comment calls adding a line "the reviewed act"; add `"/b/:slug"` to it.

**Not under `PreAuthLayout`** — see "One spec instruction that is not implementable as
written". `BoardLayout` is a third pathless layout, full-bleed, no shell, no `useSession`.

**D6, and it is four different behaviours, not a cache header:**

| Outcome | Behaviour |
| --- | --- |
| 200 | render it; remember it as the last good copy; next fetch in ~5 min |
| transport failure or 5xx | keep rendering the last good copy — a stale board beats a browser error on a hospital wall |
| **404 or 410** | **blank the board.** This is the unpublish path and it must take effect |
| last good copy older than **60 minutes** | stop showing cards, whatever the reason |

Two different numbers, and the note is explicit that the plan must name both: the refresh
interval is **~5 minutes**; the maximum age of a last-good copy is **60 minutes**. They are
not the same bound and neither implies the other.

**`unwrap()` cannot express this, and the load-bearing reason is the redirect.** Measured:
`unwrap` calls `redirectToLogin()` on a 401 (`frontend/src/api/client.ts:48-50`) and
`redirectToNoAccess()` on a 403 `account_inactive` (`:60-61`) — **full-page navigations**,
which on an unattended corridor screen means the board is replaced by a login page and
never comes back. That alone disqualifies it.

Secondarily, `ApiError` (`client.ts:8-19`) carries `code` and `userMessage` and **no
status**, so the four-way branch above cannot be expressed on the object it throws. Note
that a 404 from this API *is* distinguishable by its `code` (`"not_found"`) where a 502
gives `"network_error"` — so do not write "a 404 and a 502 are indistinguishable"; it
invites a reviewer to "correct" the design back to `unwrap`. The redirect is the reason.
`PLAN-DEFECTS.md` D19 item 6.

`publicBoard.ts` therefore uses `fetch` directly and reads `response.status`.

**Deadlines expire locally too.** Even while serving a stale copy the page drops any card
whose deadline has passed, using `deadlineLocalView` from
`frontend/src/routes/Deadline.tsx:27` — which returns `{state, date, days}` from the same
Europe/Stockholm `YYYY-MM-DD` string the API sends, and whose `days` is "Closes in N days".
Otherwise §6's "no past deadlines" fails exactly when nobody is watching.

**D5 — empty slots, no reflow.** Slots are `items_per_page` from the payload: landscape 8,
portrait 4. Three approved calls on a portrait board is three cards and **one** empty slot;
on a landscape board, **five**. Zero is the board title and one quiet line reading
**"No calls are currently posted"** — *not* "No open calls right now", which would be a
false claim made in public, because zero *approved* items does not mean zero open calls.
No reflow: §6 pins card, type and QR sizes to measured legibility floors (170 px QR, 34 px
title), and sizing cards to queue depth makes those floors a function of how many calls
happen to be approved.

**Header:** board title plus **the nearest deadline across all pages**, in the urgent
colour, big enough to read at 4 m. §6: "That is the hook that makes someone stop. Without
it the board is invisible to anyone not already standing at it." **Across all pages**, not
the current page.

**Footer:** `Sida N av M` and a progress bar (§6, and the Swedish string is §6's).

**QR:** `<svg viewBox={`-4 -4 ${m+8} ${m+8}`}><path d={qr_path}/></svg>` — an attribute,
never `dangerouslySetInnerHTML`. The `-4` supplies the quiet zone the payload omits.

- [ ] **Step 1: Write the failing tests**

`frontend/src/api/publicBoard.test.ts` — the D6 state machine, which is where the risk is:

```ts
it("a 200 replaces the last good copy", ...)
it("a 500 keeps rendering the last good copy", ...)
it("a network rejection keeps rendering the last good copy", ...)
it("a 404 BLANKS the board", ...)          // rev 1 broke unpublishing; both reviewers caught it
it("a 410 BLANKS the board", ...)
it("a last good copy older than 60 minutes stops showing cards", ...)
it("never navigates away on any status", ...)   // unwrap's 401 redirect must not be reachable
```

`frontend/src/routes/BoardScreen.test.tsx`:

```ts
it("renders 8 slots on a landscape board and 4 on a portrait one", ...)
it("three calls on a portrait board leaves ONE empty slot", ...)
it("three calls on a landscape board leaves FIVE empty slots", ...)
it("zero approved items shows 'No calls are currently posted'", ...)
it("does NOT say 'No open calls right now'", ...)
it("the header shows the nearest deadline ACROSS ALL PAGES, not the current page", ...)
it("a card whose deadline has passed is dropped even from a stale copy", ...)
it("the footer shows Sida N av M", ...)
it("pages advance after page_seconds", ...)
it("renders no score, no rationale and no profile name", ...)
```

`BoardCard.test.tsx`:

```ts
it("renders the QR path as an attribute, not as HTML", ...)
it("the viewBox includes a 4-module quiet zone", ...)
it("shows 'Closes in N days' from deadlineLocalView", ...)
it("shows the funder name and the budget", ...)
```

`it("the header shows the nearest deadline ACROSS ALL PAGES, not the current page")` is
the specific §6 sentence a per-page implementation would silently violate: put the nearest
deadline on page 3 and assert it is in the header while page 1 is showing.

- [ ] **Step 2: Run and confirm failure**

```bash
cd <worktree>/frontend && pnpm vitest run src/routes/BoardScreen.test.tsx src/routes/BoardCard.test.tsx src/api/publicBoard.test.ts
```

**Not `pnpm test run`** — that becomes `vitest run run` and runs nothing.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run every frontend gate**

```bash
cd <worktree>/frontend && pnpm vitest run
cd <worktree>/frontend && pnpm lint
cd <worktree> && pnpm -C frontend build
cd <worktree>/frontend && pnpm run build:match && pnpm run check:bundle
cd <worktree> && uv run python scripts/gen_routes.py --check
```

`check:bundle` reads `dist/module-manifest.json` and asserts no module under an excluded
path is in the match build. `BoardScreen` imports `Deadline.tsx` and `@mantine/core`;
neither is excluded. **Run it rather than reasoning about it** — the script's own header
records that string-sentinel gates shipped 181 kB of cockpit code while reporting clean.

- [ ] **Step 5: Prove four gates BITE**

**Gate A — the 404 blanks the board.**
- *What trivially passes it:* treating every non-200 as a transport failure. That is
  literally rev 1 of the decisions note, and it means an admin can unpublish a board and
  the screen keeps showing it forever. **The emergency stop would not stop anything.**
- Mutate: fold the 404 branch into the 5xx branch.
- Expected: FAIL on both `a 404 BLANKS the board` and `a 410 BLANKS the board`.

**Gate B — the manifest/router equality.**
- *What trivially passes it:* adding `"/b"` to `routes.json` and forgetting `router.tsx`.
- Mutate: remove the `/b/:slug` route from `router.tsx` only.
- Expected: `router.test.tsx` FAILS on the set equality. Record whether
  `router.simple.test.tsx` also fires — it should not, since that file pins the *other*
  router, and knowing which one bit is the point.

**Gate C — the empty-state wording.**
- *What trivially passes it:* asserting some empty-state text renders.
- Mutate: change the string to "No open calls right now".
- Expected: FAIL on `does NOT say 'No open calls right now'`. That test exists because the
  wrong sentence is a **false claim made in public**, not a copy nit.

**Gate D — local deadline expiry on a stale copy.**
- *What trivially passes it:* filtering deadlines only on fresh data. A test that never
  advances the clock past a deadline never notices.
- Mutate: apply the expiry filter only in the 200 branch.
- Expected: FAIL on `a card whose deadline has passed is dropped even from a stale copy`.

- [ ] **Step 6: Full backend suite too, then commit**

`scripts/gen_routes.py` rewrote `src/grant_lab/web/spa_routes.py`, so `tests/test_gen_routes.py`
and `tests/test_spa_serving.py` are in scope.

**Expected DELTA: backend +0 (no new backend tests; existing ones must stay green),
frontend +21** against the measured 1062.

```bash
cd <worktree> && git commit -m "feat(boards): the /b/:slug corridor screen

Last-good-copy for transport failures only and bounded to 60 minutes: a 404 is a
failed fetch, so folding it in would mean an unpublished board kept showing
forever. Deadlines expire locally even while a stale copy is on screen."
```

---

### Task 10: The board management screens and the transfer UI

**Files:**
- Create: `frontend/src/api/boards.ts`
- Create: `frontend/src/routes/BoardsList.tsx`, `BoardDetail.tsx`, `BoardQueue.tsx`
- Create: `frontend/src/routes/ProfileTransferCard.tsx`
- Modify: `frontend/src/routes/ProfileDetail.tsx`, `routes.json` (+ generated), both routers and both router tests
- Test: a `.test.tsx` beside each new component **except `BoardsList`** — see below

**The queue screen is the one that decides whether the gate survives.** §4.3: "An approval
UI that costs a minute per call will be abandoned, and an abandoned gate is worse than no
gate because the board silently stops updating." So it ships with all three affordances
§4.3 names, not two of them:

1. a checklist ordered by score,
2. an "approve everything at or above N" action,
3. a visible count of what is waiting.

**The transfer card renders a COMPUTED warning, read BEFORE the act.** It reads
`outgoing_lost_read` from **`GET /api/profiles/{id}`** — the profile detail payload the card
is already mounted on (Task 3's second half) — **not** from the `PUT .../owner` response,
which only exists once the transfer has happened. A warning derived from the response is
the after-the-fact version the note explicitly rejects: "the transfer UI must say so before
the act rather than after". An earlier draft produced the value only on the PUT response, so
this card had nothing to read at render time and its admin gate could not have been built.
`PLAN-DEFECTS.md` D17.

A hardcoded "you will lose access to this profile" is wrong for an admin, and the note
records that exact error being made once.

**The title field carries D4's caution** at the point the title is set: a board title is
free text an author chose and may name the group even when the profile is private.

- [ ] **Step 1: Write the failing tests**

```ts
// BoardQueue.test.tsx
it("orders candidates by score, highest first", ...)
it("shows the waiting count", ...)
it("approve-everything-at-or-above-N approves the whole band in one request", ...)
it("shows already-approved candidates as approved rather than hiding them", ...)   // D3
it("refuses to submit a selection that would exceed capacity, naming the overflow", ...)
it("shows remaining capacity from the board's stored items_per_page", ...)         // D7

// BoardDetail.test.tsx
it("a non-admin sees no publish control", ...)                                     // §4.3
it("an admin sees a publish control", ...)
it("the publish confirmation names what becomes public", ...)                      // Open-5
it("shows that the owner cannot be notified when owner_undeliverable is true", ...) // Open-3

// ProfileTransferCard.test.tsx
it("warns a non-admin owner that they will lose read access", ...)                 // Open-6
it("does NOT warn an admin owner, because they lose nothing", ...)                 // D10
it("keep-read-access is off by default", ...)                                      // Open-6
it("shows one generic error for an unknown and for an inactive address", ...)      // Open-8
```

`it("does NOT warn an admin owner, because they lose nothing")` is the frontend half of
Task 1's gate. A hardcoded warning passes the first test and fails only this one.

**`BoardsList` ships without a test, deliberately.** The Files list above used to promise
"one `.test.tsx` beside each new component" while listing test blocks for only three of the
four, which made the sentence and the +14 delta contradict each other. This plan resolves it
by narrowing the sentence rather than by padding the count: `BoardsList` is a list-and-link
with no branching a wrong implementation would get past a test that only asserts "a row
renders per board". If review wants it covered, two cases (empty state; a link per board)
raise the delta to **+16** — say which, and re-derive. `PLAN-DEFECTS.md` D18.

- [ ] **Step 2: Run and confirm failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Every gate**

```bash
cd <worktree>/frontend && pnpm vitest run
cd <worktree>/frontend && pnpm lint
cd <worktree> && pnpm -C frontend build
cd <worktree>/frontend && pnpm run build:match && pnpm run check:bundle
cd <worktree> && uv run python scripts/gen_routes.py --check
cd <worktree> && uv run pytest -n 16 --dist loadfile -q
```

- [ ] **Step 5: Prove two gates BITE**

**Gate A — the computed warning.**
- *What trivially passes it:* rendering the warning unconditionally.
- Mutate: render it whenever the user is the owner, ignoring `outgoing_lost_read`.
- Expected: FAIL on `does NOT warn an admin owner, because they lose nothing`.

**Gate B — publish is not merely hidden.**
- *What trivially passes it:* hiding the button with CSS. The control is gone from the
  screen and present in the DOM.
- Mutate: render the publish control always but with `display: none`.
- Expected: `a non-admin sees no publish control` must **still FAIL**. If it passes, the
  assertion is using a visibility query where it needs `queryByRole(...)` returning null —
  fix it, and record that the first version was hollow. Server-side authorization (Task 6)
  is the real control either way; this test pins that the UI does not *offer* it.

- [ ] **Step 6: Commit**

**Expected DELTA: frontend +14.** Backend unchanged.

```bash
cd <worktree> && git commit -m "feat(boards): the approval queue, board settings and profile transfer screens

The queue ships all three affordances §4.3 names — score order, approve-at-or-above-N,
and a waiting count — because an approval UI that costs a minute per call gets
abandoned, and an abandoned gate is worse than no gate."
```

---

## Self-review of this plan against its inputs

**Spec §4.3 coverage.** `boards` and `board_items` with every column → Task 2. The
approve/reorder-to-any-profile-editor and publish-admin-only split → Tasks 5 and 6.
Delete-by-creator-or-admin → Task 5. "Boards reuse that rule exactly" → Tasks 5 and 6 call
`_profile_for_edit`. The bulk affordances → Task 6 (backend) and Task 10 (UI).
`approved_by` / `published_by` recorded separately → Tasks 2 and 6.

**Spec §6 coverage.** The 2×4 grid and the portrait 1×4 → Tasks 2 (stored) and 9
(rendered). QR ≥170 px at `Call.url` → Tasks 7 and 9. Title 34 px, header with the nearest
deadline, `Sida N av M` footer, `vh` sizing → Task 9. The approval cap → Task 6. `max_pages`
default 3 → Task 2. No summary line, no criterion data, no score, no past deadlines,
unpublished → 404 → Task 7's payload tests and Task 9's render tests. The public endpoint's
exact field list, its userlessness and its rate limit → Task 7. `/b/:slug` auth-free → Task
9 (under its own layout; deviation recorded).

**Decisions-note coverage.** D1 → Task 7. D2 → Task 4. D3 → Tasks 6 and 4. D4 → Open-5,
Tasks 5/6/10. D5 → Task 9. D6 → Task 9. D7 → Tasks 2, 5, 6, 9. D8 → Tasks 2 and 7. D9 →
Task 8. D10 → Tasks 1, 3, 10. All eight "Still open" items → the Open-1..8 section, each
with a named implementing task.

**Numbers.** Every baseline in Global Constraints was measured in this worktree at
`05bd151c` and its raw output is in `/home/parlar_ai/plan-c-run/PLAN-AUTHORING-MEASUREMENTS.md`.
The **per-task deltas were re-derived after the dry run** — six reproduced exactly and four
did not; see "What the dry run changed" at the end of this file for which term moved in each.
Every `file.py:NN` was opened, not copied — including from the decisions note, one of whose
citations had already gone stale (`PLAN-DEFECTS.md` N1). Per-task figures are **DELTAS**
against the measured 5137 / 1062; the route-count arithmetic (109 + 1 per new path) is
stated so it can be checked, and each task re-derives it with `wc -l` rather than
incrementing.

**Type consistency.** `transfer_read_effect` (Task 1) → surfaced by Task 3 on **BOTH**
`GET /api/profiles/{id}` (so Task 10's card can warn **before** the act) **and** the
`PUT .../owner` response (so the caller learns what actually happened) → read by Task 10. `board_queue` / `QueueRow` (Task 4) → Task 6.
`qr_for` / `QrCode{modules, path}` (Task 7) → serialized as `qr_modules` / `qr_path` →
read by Task 9. `ORIENTATION_ITEMS_PER_PAGE` (Task 2) → Tasks 5, 6, 8, 9.
`BoardAlert.owner_undeliverable` (Task 8) → Task 10.

## Known gaps, stated rather than hidden

- **Tasks 5, 6, 8, 9 and 10 give test NAMES with their intent, not full bodies** for the
  routine cases. Every non-obvious case — the ones a wrong implementation would pass —
  carries a sentence saying what it must catch and why. Tasks 1 and 2, where the shape is
  least obvious from the surrounding code, carry full bodies.
- **The DELTAs for Tasks 5-10 are the count of listed test names.** Any task that splits or
  merges a case must re-derive its delta with `--collect-only` rather than adjust the
  number here.
- **`e2e/` is untouched.** A Playwright case for the corridor screen is worth having and is
  not in this plan; `frontend/e2e/match.spec.ts` is the file it would join.
- **`BoardsList` ships untested** (Task 10, `PLAN-DEFECTS.md` D18). Named, not hidden.
- **The match-edition process-cut gate pins CONSTRUCTION, not boot.** Measured: the FastAPI
  lifespan imports seven proposal-assist modules with no edition guard (`main.py:212,
  229-233, 251`), so **41** of them and `pycrdt` are loaded after a match boot even though
  the gate reports zero after `create_app()`. Every module this plan adds is reachable from
  `create_app()`, so the gate does its job here — but fixing it is **out of scope for Plan C**
  and no commit message or docstring in this plan may claim a clean match boot.
- **A QR decoder is not available and is not added.** Task 7's two "does the QR point at the
  right URL" tests re-encode and compare matrices instead (`PLAN-DEFECTS.md` D13). A QR that
  segno encodes and re-encodes identically but that a real phone camera cannot read is
  outside what any test here can see; `test_the_symbol_is_never_a_micro_qr` is the one
  scanner-compatibility property that is pinned, and it is pinned because it was the one
  that actually bit.

---

## What the dry run changed

The whole plan was **executed for real** in a throwaway worktree (`.worktrees/plan-c-dry`,
branch `dry/plan-c`, ten commits, code discarded) specifically to find the plan's defects
before a build depends on them. It found **19**, on top of the four input defects phase 1
had already recorded. Every one has an outcome below — fixed, or rejected with the
measurement that rejects it.

**Every number and every `file.py:NN` folded in was re-derived here against the worktree,
not copied from the defect list.** That caught three cases where the correction was wrong
in the opposite direction from the original error.

### ESCALATE

**None, in either phase.** Nothing found required merging, pushing, editing
`docs/superpowers/specs/`, reading `.env`, hand-editing `docs/ARCHITECTURE.md`, or moving
`master` off `05bd151c`. The one item that touches an identity line — the match-edition
process-cut gate pinning construction rather than boot — is **recorded and declared out of
scope for Plan C**, in Global Constraints and in Known gaps, with the plan forbidden from
restating the false version of what it proves.

### The three that would have shipped a real bug

| | What it was | Where it is fixed |
| --- | --- | --- |
| **D14** | `segno.make(url, error="m")` returns a **Micro QR** for a 13-character URL. Many phone cameras cannot read one; on a wall it looks exactly like a working QR, and every other test round-trips it perfectly. | Open-9 and Task 7 Step 3 use `segno.make_qr`; Task 7 Step 2 adds `test_the_symbol_is_never_a_micro_qr` with a 13-char fixture |
| **D17** | The transfer warning had nothing to read at render time — `outgoing_lost_read` existed only on the `PUT` **response**, i.e. after the act Open-6 says it must precede. | Task 3 now surfaces it on `GET /api/profiles/{id}` too; Task 10 and the self-review chain updated |
| **D12** | The QR round-trip gate compared against a **different encoder** (`segno.make(url)` vs the implementation's `error="m"`), so its verdict was a property of the fixture URL's length. | Task 7 Step 2 compares against the same `segno.make_qr(url, error="m")` call |

### Every defect and its outcome

| ID | Sev | Outcome |
| --- | --- | --- |
| **S1** | — | Already carried before the dry run: §6's `PreAuthLayout` is a 352 px card, so Task 9 uses a third pathless layout. Dry run **confirmed** both halves. No change. |
| **S2** | — | Already carried: no QR encoder exists on either side. Open-9. **Amended** by D14 and D15. |
| **N1** | — | Already carried: `seed_dev_data.py:328`, not the note's `:302`. **Re-verified** here. |
| **N2** | — | Already carried: the seed omits the flag, so `server_default=text("true")` is load-bearing. **Re-verified.** |
| **N3** | — | Nothing to do. |
| **D1** | Important | **FIXED.** Task 1 Step 2 expected "4 FAILED" and warned that ERRORs meant trouble — but its own Step 1 puts the import at module level, so the only possible outcome is **one collection error**, and the step fired its own alarm on the correct result. Now reads like Task 2 Step 2, which had it right. |
| **D2** | Minor | **FIXED.** Task 2 Step 8: `+5` → `+6`. Step 1's own code block defines six test functions. |
| **D3** | Important | **FIXED, in two tasks.** `test_a_stranger_gets_404_not_403` asserted only the status, and a missing route also answers 404 — so it was **green before the endpoint existed** and would stay green if the route were deleted or unregistered. Re-derived here: a missing route returns `404 {"error":{"code":"not_found","message":"Not Found"}}`, while `_profile_for_edit` (`profiles.py:139/141`) raises `detail="profile not found"` and `main.py:548` renders it into `message`. Tasks 3 **and** 5 now assert the body plus a positive control. |
| **D4** | Important | **FIXED.** Task 3 `+12` → `+15`. `DELEGATION_PINS` is parametrized by two tests (`test_profile_access_single_source.py:484-486` and `:517-519`), so one dict entry is two test IDs; `EXEMPT_PROFILE_FUNCTIONS` is parametrized over nothing, which is why Task 1 was clean. Promoted to a Global Constraints counting rule. (+15 = 13 cases after D17 + 2.) |
| **D5** | Minor | **FIXED.** Task 3 Gate B said "the four behavioural tests"; Task 3 has thirteen. Also records that this gate's evidence *depends on* D3's fix — with a status-only stranger assertion it reads as a one-test catch instead of two. |
| **D6** | Important | **FIXED — option (a), and the rejected option is written down.** Task 4 Step 4 told you to add `boards.queue.board_queue` to `_base_call_filters`'s caller enumeration, but Step 3 calls `_decision_predicate`. Re-derived: `_base_call_filters` has exactly two callers (`:367`, `:503`) and its docstring is **true today**; the instruction would have made it false — the exact failure its own closing sentence names. `_decision_predicate` has one call site (`:322`) and enumerates none. Step 4 now targets `_decision_predicate` and leaves `_base_call_filters` alone. Option (b) — make `board_queue` genuinely call `_base_call_filters` — is **rejected** in the plan text, with the reason: with `source_id` and `q` both `None` it returns the same list via two `None`s the queue never uses, and subscribes the corridor board to any future dashboard-only filter. |
| **D7** | Minor | **FIXED by deletion, not correction.** Task 4 Step 6's "passes seven of the eight tests" did not reproduce (the dry run measured `2 failed, 6 passed`), and the ninth case from D8 moves it again. The pass-count literal is **removed** rather than swapped for another guess; the two failing tests are named, with an explicit "do not carry a pass-count literal here". |
| **D8** | Important | **FIXED.** Task 4's eight cases could not see the `state == "hidden"` clause at all — nothing in the file created a non-hidden `CallDecision` — and Step 6's second mutation named two tests that stay green plus a cause the one real failure did not have. Adds `test_a_call_the_owner_SAVED_is_still_in_the_queue`, carries `+9`, and rewrites the record line as two dropped clauses with one test each. |
| **D9** | Important | **FIXED, in Tasks 5 and 7 and in Global Constraints.** Adding a router to `_ALWAYS` also requires the `_ALWAYS_ROUTERS` literal in `test_edition_surface.py`; the plan named neither. **The defect's own citations were off** — re-derived: `_ALWAYS_ROUTERS` is at `:242-249` (not `:246`) and the test at `:279-284` (not `:283`). It is a **list compared element-for-element in order**, so the plan now says to add the entry in the same position in both. |
| **D10** | Minor | **FIXED, with a corrected citation.** A sub-router must carry **no `prefix`** — `build_api_router` supplies `/api`. The defect cited `web/api/__init__.py:106-107`; re-derived, the `APIRouter(prefix="/api", ...)` is at **`:100-101`** and `include_router` at `:105`. Task 5 Step 3 and Task 7 Step 5 now say so, and name the symptom (404 on every test, indistinguishable from an unregistered router). |
| **D11** | Minor | **FIXED, plus a general rule.** Three "prove the gate BITES" steps named one failing test where two fail (Tasks 4, 5, 6). Each now names the real set, `claude_agent_sdk` is dropped from Task 5 Gate C's expected flips (that import pulls pycrdt and prosemirror but not the SDK), and Global Constraints adds: **an expected failing set is a FLOOR — more red is the gate being stronger, fewer red is the finding.** |
| **D12** | **Critical** | **FIXED.** See the table above. Re-measured: `https://a.se/x` (14 chars) gives M4/level-L/17 rows under `make()` and version 1/21 rows under `error="m"`; `https://example.test/x` (22 chars) agrees. Different encoders, and the disagreement is a function of URL length. |
| **D13** | Important | **FIXED.** `test_the_qr_points_at_the_funder_url_not_this_app` asked for a **decoded string**; segno is an encoder only and nothing in this environment decodes QR (re-derived: pyzbar, zxing, cv2, qrcode, zxingcpp, qreader all ABSENT; PIL and numpy present but cannot decode). Reworded to **re-encode and compare matrices**, which has the same discriminating power for a QR pointing at the wrong URL. Named in Known gaps as a real limit. |
| **D14** | **Critical** | **FIXED.** See the table above. |
| **D15** | Minor | **FIXED, and re-derived rather than copied.** The plan claimed 7998 bytes / ≈192 KB and said "Task 7's gate is written against that number". Measured here with the plan's own `qr_for`: a 129-char URL gives 49 modules and **9100 bytes** of path, **218,400 bytes (213 KB)** for 24 items. The dry run's whole-body figure (226,040 bytes / 221 KB) is carried as **the dry run's measurement**, flagged as not re-derived. The exact command is now printed beside the number. Also fixes an off-by-one: the base is 46 chars, so 129 is `+ "a" * 83`, not 84. |
| **D16** | Important | **FIXED.** Task 8 changes `BoardOut` but never regenerated `schema.gen.ts`, and **`pytest` cannot see a stale generated file** — the drift check is CI-only (`ci.yml:112-115`). New Step 4 regenerates it in the same commit, the Files list gains the file, the "route set does NOT move" is stated explicitly so nobody regenerates the baseline for nothing, and it is a Global Constraints rule. |
| **D17** | **Critical** | **FIXED.** See the table above. Task 3's Files, Interfaces, Step 1 (a thirteenth case), Step 3 (the `get_profile` half), Step 5 and Step 8; Task 10's transfer-card paragraph; and the self-review's type-consistency chain. |
| **D18** | Minor | **FIXED — option (a), stated.** The Files sentence promised a test per component while listing three of four. Narrowed to "except `BoardsList`", with the reason (a list-and-link with no branching worth pinning), the alternative priced (+16 with two cases), and the omission named in Known gaps. The `+14` delta was right; the sentence was wrong. |
| **D19.1** | — | **REJECTED.** The plan's `dashboard.py:91` for the archive predicate is **correct**. Re-derived at `05bd151c`: line 90 is the `raise ValueError` tripwire, line 91 is `return Call.id.notin_(...)`. The dry run measured its own worktree, where its D6 fix had already lengthened that docstring. The plan now carries the re-derivation beside the citation so this does not get "corrected" a third time. |
| **D19.2** | — | **FIXED.** `add_profile_viewer`'s generic-400 sentence is `:599-601`; line 598 is blank. Corrected in Open-8 and in Task 3's docstring. |
| **D19.3** | — | **REJECTED as stated, citation widened anyway.** The claim was that "the OWNER never has a row" is at `db/models/profiles.py:135`, not `:136`. Re-derived: the sentence spans 135-136 and the quoted phrase is on **136**; 137 is the closing `"""`. The plan's `:136` pointed at the phrase. Now written `:135-136`, which is unambiguous. |
| **D19.4** | — | **REJECTED.** `dashboard.py:377-390` for `build_dashboard_view`'s signature is **correct**: `def build_dashboard_view(` is at 377 and `user_id` at 380. The defect's "def is at 386" is the dry worktree again. |
| **D19.5** | — | **FIXED.** `digest/assemble.py:86-88`, not `:86-89` — 89 is `profiles = (`. |
| **D19.6** | — | **FIXED, and the reasoning re-ordered.** Task 9 now leads with the disqualifying fact — `unwrap` performs **full-page navigations** on 401 (`client.ts:48-50`) and on 403 `account_inactive` (`:60-61`), catastrophic on an unattended wall — and explicitly warns **not** to claim a 404 and a 502 are indistinguishable, because a 404 does carry `code: "not_found"`. The over-strong claim invited a reviewer to "correct" the design back to `unwrap`. |
| **D19.7** | — | **FIXED** with D11 (`claude_agent_sdk` does not flip). |
| **D19.8** | — | **FIXED.** Task 9 now warns that `frontend/routes.json`'s own `_comment` says spa_routes are "matched EXACTLY (not by prefix)", which contradicts `is_spa_route` (`gen_routes.py:75-91`, the exact-or-subtree match at `:89`) and the behaviour `/calls/1` relies on. The file you are editing tells you the opposite of the truth. |
| **D20** | Minor | **FIXED — found while folding, not by the dry run.** Task 3's prose said "**Nine** cases" directly above a list of **twelve**. Now "Thirteen", matching the list after D17's addition. |

### Deltas that moved, and the term that moved each

Six of ten reproduced exactly in the dry run and are unchanged. The four that moved:

| Task | Was | Now | The term |
| --- | --- | --- | --- |
| 2 | +5 models | **+6** models | the plan's own Step 1 block always had six tests (D2) |
| 3 | +12 | **+15** | +2 from the `DELEGATION_PINS` parametrization (D4), +1 for D17's detail-payload case |
| 4 | +8 | **+9** | the ninth case that makes the `state == "hidden"` clause visible (D8) |
| 7 | +22 | **+23** | `test_the_symbol_is_never_a_micro_qr` (D12/D14) |

Unchanged: Task 1 **+4**, Task 5 **+17**, Task 6 **+16**, Task 8 **+13** backend, Task 9
backend **+0** / frontend **+21**, Task 10 frontend **+14**.

**These are still deltas, not totals, and they are still to be re-derived.** Every task
says so; the dry run's own end-state totals (5248 backend / 1099 frontend) include six
extra tests it wrote while working around defects and must not be carried anywhere.

### What the dry run confirmed

Worth recording, because a defect list read alone makes a plan look worse than it is.

- **The measured baselines are exact.** 5137 passed / 1 skipped, 140 frontend files / 1062
  tests, migration head 0071, 109 route paths — all re-measured in the dry worktree.
- **Every "prove the gate BITES" mutation reddened something**, and 22 of ~26 named exactly
  the right test. Tasks 8 and 9's four-gate blocks were perfect.
- **The fixture warning earned its space.** `pg_session_factory` is a factory,
  `profile_factory` returns a tuple, `ProfileKind` has two values — all three correct, and
  each would have cost a task.
- **Both deviations from the spec held up**, and Deviation 1's override is right even though
  its citation was not (D6): the queue does key off the profile owner.
