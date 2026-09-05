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

### Regeneration steps a change of each kind requires, in the SAME commit

| If a task changes… | Run |
| --- | --- |
| any ORM model | `uv run python scripts/gen_architecture.py` |
| `pyproject.toml` dependencies | `uv lock` (CI runs `uv lock --check`) |
| any route | `bash scripts/gen_openapi_types.sh` |
| `frontend/routes.json` | `uv run python scripts/gen_routes.py` |
| the full edition's route set | regenerate `tests/architecture/full_edition_routes.txt` **and** bump the `109` literal in `test_edition_surface.py::test_the_baseline_is_not_empty` **by hand** |

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

This makes the queue a **third caller** of `dashboard._base_call_filters`, whose docstring
(`dashboard.py:308-321`) forbids *copies* and enumerates its callers. Task 4 calls it and
updates that enumeration in the same commit.

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
(`profiles.py:586`), whose docstring (`:598-601`) gives the reason: "one GENERIC 400, the
same answer every time, so this endpoint is not a directory of who has an account."

**Implemented by:** Task 3.

### Open-9 (not in the note's list; forced by measurement) — QR codes are encoded server-side

**Decision:** add `segno` to the backend. The public payload carries per item
`qr_path` (an SVG `d` string built from `segno`'s documented `q.matrix`) and
`qr_modules` (the module count). React composes `<svg viewBox="-4 -4 n+8 n+8"><path d={qr_path}/></svg>`.

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

**Cost, measured:** 7998 bytes for the longest realistic URL (129 chars, 49 modules);
≈ **192 KB** for a full 24-item board. Task 7's gate is written against that number.

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
| `src/grant_lab/web/dashboard.py` | `_base_call_filters` docstring: name the third caller. |
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

Expected: **4 FAILED**, each `ImportError: cannot import name 'transfer_read_effect'`.
**If you see 4 ERRORs and 0 FAILED, read the traceback** — a collection error is this
repo's false-pass signature and does not mean the test is measuring anything.

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
fresh interpreter and asserts ZERO proposal_assist modules are loaded.

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

**Expected DELTA: +5 (models) + the migration tests you actually wrote.** Count them with
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
- Modify: `src/grant_lab/web/api/profiles.py`
- Modify: `tests/architecture/test_profile_access_single_source.py` (three pin sets)
- Modify: `tests/architecture/full_edition_routes.txt` + the `109` literal in `test_edition_surface.py`
- Modify: `frontend/src/api/schema.gen.ts` (**generated**)
- Test: `tests/test_api_profile_transfer.py`

**Interfaces:**
- Consumes: `_profile_for_edit` (`profiles.py:126`), `transfer_read_effect` (Task 1),
  `can_read_profile` (`profile_access.py:76`)
- Produces: `PUT /api/profiles/{profile_id}/owner`, body
  `{"email": str, "keep_read_access": bool = false}`, response
  `{"owner": {"id": int, "name": str, "email": str}, "outgoing_lost_read": bool}`

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
   (`db/models/profiles.py:136`, "the OWNER never has a row") stops being true.
2. **Set `profile.created_by = new_owner.id`.**
3. **Create the new owner's `Subscription` if absent**, mirroring `profiles/create.py:79`
   — otherwise D9 notifies nobody. **Never touch the old owner's row:**
   `digest/assemble.py:86-89` is explicit that deleting it destroys their `digest_enabled`
   preference and a re-share cannot restore it, and `assemble.py:97` already filters the
   digest through `readable_profile_ids`, so their section simply stops appearing.
4. **Open-6:** if `keep_read_access`, add a `ProfileViewer` row for the outgoing owner.
   **Open-7:** then, if their `users.default_profile_id` is this profile and
   `can_read_profile(session, outgoing, profile.id)` is now `False`, set it to `NULL`.

Step 4 is computed **after** step 2 so it asks the real post-transfer predicate.

- [ ] **Step 1: Write the failing tests**

`tests/test_api_profile_transfer.py`. Nine cases, each pinning one decision:

```python
def test_owner_can_transfer_to_an_active_user(monkeypatch, pg_url, pg_session_factory, ...): ...           # happy path
def test_admin_can_transfer_a_profile_they_do_not_own(monkeypatch, pg_url, pg_session_factory, ...): ...   # D10: the point
def test_a_stranger_gets_404_not_403(monkeypatch, pg_url, pg_session_factory, ...): ...                    # no existence oracle
def test_a_reader_who_cannot_edit_gets_403(monkeypatch, pg_url, pg_session_factory, ...): ...              # reading is wider
def test_an_inactive_recipient_is_refused_generically(monkeypatch, pg_url, pg_session_factory, ...): ...   # Open-8
def test_an_unknown_email_gets_the_SAME_generic_400(monkeypatch, pg_url, pg_session_factory, ...): ...     # not a directory
def test_the_incoming_owners_viewer_row_is_deleted(monkeypatch, pg_url, pg_session_factory, ...): ...      # D10 stale row
def test_the_new_owner_gets_a_subscription_row(monkeypatch, pg_url, pg_session_factory, ...): ...          # D9 depends on it
def test_the_old_owners_subscription_row_SURVIVES(monkeypatch, pg_url, pg_session_factory, ...): ...       # assemble.py:86-89
def test_keep_read_access_writes_a_viewer_row_for_the_outgoing_owner(monkeypatch, pg_url, pg_session_factory, ...): ...  # Open-6
def test_a_now_unreadable_default_profile_id_is_cleared(monkeypatch, pg_url, pg_session_factory, ...): ...       # Open-7
def test_a_still_readable_default_profile_id_is_LEFT(monkeypatch, pg_url, pg_session_factory, ...): ...          # Open-7
```

The last two are a pair on purpose: a rule that clears unconditionally passes the first
and fails the second, and one that never clears does the reverse.

`test_an_unknown_email_gets_the_SAME_generic_400` must assert the **response bodies are
byte-equal** to the inactive-recipient case, not merely both 400. Two different messages
are the directory the endpoint must not be.

- [ ] **Step 2: Run and confirm they fail**

```bash
cd <worktree> && uv run pytest tests/test_api_profile_transfer.py -q
```

Expected: every case **FAILED** with a 405 or 404 from FastAPI (no such route). Not ERROR.

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
    ineligible alike -- mirroring add_profile_viewer (:598-601), so this is not a
    directory of who has an account.
    """
```

Body follows the four ordered steps above, inside a single `with sf() as session:` block
with one `session.commit()`.

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

`EXPECTED_DELEGATION_PINS` is the deliberate second literal (`:154-156`: "changing
PREDICATE_PINS alone cannot pass"). **Both must change, in this one diff.**

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

Then bump `test_edition_surface.py::test_the_baseline_is_not_empty`'s literal **by hand**
to whatever `wc -l` just printed. **Do not compute it** — the measured 109 plus one new
path is 110, and if `wc -l` says anything else, find the term that changed.

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
  **Record whether it also stays green on the four behavioural tests** — if it does, that
  is exactly the hole outside review 2026-09-04 (Sol #9) found for `_profile_for_edit` and
  the reason this pin exists.

**Gate C — the route baseline.**
- *What trivially passes it:* regenerating `full_edition_routes.txt` without touching the
  literal. The set comparison goes green.
- Mutate: regenerate the file but leave the literal at 109.
- Expected: `test_the_baseline_is_not_empty` FAILS, because the literal is bumped by hand
  precisely so a regeneration cannot wave a route through.

Restore after each; clear `__pycache__`; `git status --porcelain` must show only this
task's files.

- [ ] **Step 8: Full suite and commit**

**Expected DELTA: +12** (the twelve cases above) plus whatever `--collect-only` reports if
you split any of them. `1 skipped` unchanged.

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
- Modify: `src/grant_lab/web/dashboard.py` (the `_base_call_filters` docstring, `:308-321`)
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

**It REUSES the predicate; it must not fork it.** Measured, `_decision_predicate` with
`view="archive"` is exactly `Call.id.notin_(<hidden rows for (user_id, profile_id)>)`
(`dashboard.py:91`) — which is "apply this user's hides on this profile" and nothing else.

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
```

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

- [ ] **Step 4: Update `_base_call_filters`'s enumeration of its callers**

`dashboard.py:313-315` currently reads:

```
    One definition, so the rail cannot say "3" over a feed showing 2. ...
    `build_dashboard_view` and `count_decision_views` are the only callers and
    MUST NOT grow separate copies of this
```

That sentence becomes **false** the moment the queue exists. Change it to name the third
caller and say what is different about it:

```
    `build_dashboard_view`, `count_decision_views` and `boards.queue.board_queue`
    are the callers and MUST NOT grow separate copies of this -- a second copy is
    how the rail and the feed drift apart under a filter nobody re-tested.
    `board_queue` is the odd one: it passes the PROFILE OWNER's user_id rather
    than the requesting user's, which is exactly why it CALLS this instead of
    forking it (decisions note D2).
```

**This is not documentation tidying.** A docstring that enumerates its callers and is
wrong is the failure the docstring exists to prevent.

- [ ] **Step 5: Run the tests**

```bash
cd <worktree> && uv run pytest tests/test_boards_queue.py -q
```

- [ ] **Step 6: Prove the gate BITES on the exact confusion D2 names**

- *What trivially passes it:* keying the predicate on `boards.created_by`. That is the
  mistake D2 exists to prevent and it passes seven of the eight tests.
- Mutate: `owner_id = board.created_by`.

```bash
cd <worktree> && uv run pytest tests/test_boards_queue.py -q
```

Record: `against good input: PASS` / `against owner_id = board.created_by: FAIL on
test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue, because that test builds the
board with an admin who is not the profile owner`.

Second mutation, for the reuse claim: replace the `_decision_predicate` call with an
inline `Call.id.notin_(select(CallDecision.call_id).where(CallDecision.user_id ==
owner_id))` — a hand-rolled copy that **drops the `state == "hidden"` filter**.

Record: `against a hand-rolled copy missing the state filter: FAIL on
test_an_already_approved_call_is_flagged_not_dropped and
test_a_call_a_COLLEAGUE_hid_is_still_in_the_queue, because a saved call is also a
CallDecision row`. If that mutation reddens **nothing**, the tests do not cover saves and
that is a finding to write down, not a pass.

- [ ] **Step 7: Full suite, lint, commit**

**Expected DELTA: +8.** `1 skipped` unchanged.

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
- Modify: `src/grant_lab/web/api/__init__.py` (`_ALWAYS`, `:35-42`)
- Modify: `tests/architecture/full_edition_routes.txt` + the literal in `test_edition_surface.py`
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

- [ ] **Step 2: Run and confirm failure**

```bash
cd <worktree> && uv run pytest tests/web/test_api_boards_crud.py -q
```

Expected: every case FAILED with a 404 from FastAPI. Not ERROR.

- [ ] **Step 3: Implement the router, and register it**

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

Expected: FAIL on `test_a_match_boot_imports_no_proposal_assist_module_at_all` with a
non-zero count, **and** on the pycrdt/prosemirror/claude_agent_sdk assertion. Record both.
This gate boots a **subprocess**, so an in-process check would have proven nothing.

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
- Expected: FAIL on `test_capacity_uses_the_STORED_items_per_page_not_a_default`.

**Gate B — publish is admin-only.**
- *What trivially passes it:* `Depends(get_current_user)` instead of `require_admin`.
  `tests/architecture/test_auth_allowlist.py` stays **green** on that swap — its own
  docstring says so: "This is an AUTHENTICATION gate, not an authorization one."
- Mutate: swap the dependency.
- Expected: `test_an_editor_who_is_not_admin_CANNOT_publish` FAILS and
  `tests/architecture/test_auth_allowlist.py` stays green. **Record both halves** — the
  green half is the finding: this authorization rule is held by the API test alone.

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
- Modify: `tests/architecture/full_edition_routes.txt` + the literal
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
```

`test_a_qr_encodes_the_url_and_round_trips` **decodes** the emitted path back to a matrix
and compares it to `segno.make(url).matrix`. A test that only checks the path is non-empty
would pass on a QR that encodes the wrong string — which is the failure that matters,
because a wrong QR on a wall looks exactly like a right one.

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
    """
    q = segno.make(url, error="m")
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

`test_the_qr_points_at_the_funder_url_not_this_app` decodes the path and asserts the
decoded string equals `Call.url`. §6: "A QR that dead-ends on a login screen is worse than
no QR."

- [ ] **Step 5: Implement, register, and allowlist the route**

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
- Mutate: `segno.make(url[:-1], error="m")` — one character short.
- Expected: `test_a_qr_encodes_the_url_and_round_trips` and
  `test_the_qr_points_at_the_funder_url_not_this_app` both FAIL. If only the first fires,
  the endpoint test is not decoding and should be.

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

- [ ] **Step 8: Measure the payload size against the authoring estimate**

```bash
cd <worktree> && uv run pytest tests/web/test_api_public_board.py -q -k payload_size
```

Add a test that builds a full 24-item board with realistic URLs and asserts the serialized
payload is **under 512 KB**. Authoring measured a worst case of 7998 bytes of path for a
129-character URL, ≈192 KB for 24 items; 512 KB is that with room, and the point of the
assertion is that a future change which doubles the encoding cost is noticed.

- [ ] **Step 9: Full suite, lint, commit**

**Expected DELTA: +5 (qr) +16 (endpoint) +1 (payload size) = +22.** `1 skipped` unchanged.

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
- Test: `tests/test_boards_alerts.py`

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

- [ ] **Step 4: Prove three gates BITE**

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

- [ ] **Step 5: Full suite, lint, commit**

**Expected DELTA: +13.** `1 skipped` unchanged. Watch `digest/send.py` — its tests
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
(`path == r or path.startswith(r.rstrip("/") + "/")`), so `"/b"` owns `/b/anything` the way
`"/calls"` owns `/calls/1` today. Then:

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

**`unwrap()` cannot express this.** Measured (`frontend/src/api/client.ts:47-72`): a 404
and a 502 both surface as `ApiError("network_error"|<code>, …)` with no status to branch
on, and a 401 triggers a **full-page navigation to `/login`** — catastrophic on a wall.
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
- Test: one `.test.tsx` beside each new component

**The queue screen is the one that decides whether the gate survives.** §4.3: "An approval
UI that costs a minute per call will be abandoned, and an abandoned gate is worse than no
gate because the board silently stops updating." So it ships with all three affordances
§4.3 names, not two of them:

1. a checklist ordered by score,
2. an "approve everything at or above N" action,
3. a visible count of what is waiting.

**The transfer card renders a COMPUTED warning.** It reads `outgoing_lost_read` from the
endpoint (Task 3), which comes from `transfer_read_effect` (Task 1). A hardcoded "you will
lose access to this profile" is wrong for an admin and the note records that exact error
being made once.

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
Every `file.py:NN` was opened, not copied — including from the decisions note, one of whose
citations had already gone stale (`PLAN-DEFECTS.md` N1). Per-task figures are **DELTAS**
against the measured 5137 / 1062; the route-count arithmetic (109 + 1 per new path) is
stated so it can be checked, and each task re-derives it with `wc -l` rather than
incrementing.

**Type consistency.** `transfer_read_effect` (Task 1) → consumed by Task 3 → surfaced as
`outgoing_lost_read` → read by Task 10. `board_queue` / `QueueRow` (Task 4) → Task 6.
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
