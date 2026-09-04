# Profiles are personal — private by default, shared explicitly

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A profile belongs to the person who made it. Others see it only if the
owner shares it — with named colleagues, or org-wide. Reading, subscribing and
receiving its digest all move behind **one** predicate.

An earlier draft ended that sentence with *"so there is no surface left where a
profile leaks by a route nobody thought about"*. An outside review then found three
such routes in the plan itself, one of them a complete bypass. **The claim is now
narrower and checkable:** ten named surfaces, each with a gate and a separately-run
mutation, plus one **accepted** residual leak (the name oracle, see "does not do").
"No surface left" is not a thing a plan can assert about itself — it is what the
single-source gate in Task 2 exists to keep true as the code grows.

**Architecture:** Mirror the sharing model the repo already has.
`profile_viewers(profile_id, user_id, added_at)` is a byte-for-byte analogue of
`workspace_viewers`, and the read rule copies the owner-or-viewer predicate at
`workspaces.py:1473-1479`, widened by an `org` escape hatch and the admin override
that profile *editing* already uses. Editing is unchanged and stays narrower than
reading. Everything defaults to `org`, so day one is a no-op.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL 16;
React 19 + Vite + react-router-dom + Mantine; pytest (+ testcontainers Postgres);
vitest + jsdom; Playwright.

---

## Global Constraints

- **Test command:** `uv run pytest -n 16 --dist loadfile` (~114 s). **Never
  `-n auto`** — `psutil` is absent, so xdist resolves it to 72 workers.
- **The `1 skipped` is the signal, not the total.** It is a real-SDK test gated on
  `ANTHROPIC_API_KEY`. `0 skipped` means a live billed API call ran — stop and
  record it. **Do not export `ANTHROPIC_API_KEY`.**
- **Measured baselines on `9365f123` (2026-09-04):**

  | Suite | Command | Baseline |
  | --- | --- | --- |
  | Backend | `uv run pytest -n 16 --dist loadfile` | `4915 passed, 1 skipped` |
  | Architecture | `uv run pytest tests/architecture -q` | `73 passed` |
  | Frontend | `pnpm -C frontend test` | `135 files, 954 tests` |

  **No task predicts its own pass-count delta, deliberately.** An earlier draft said
  "each task states its delta" and then no task did — a reviewer caught the plan
  asserting a discipline it did not follow. Measure the total when the work is done
  and state what you measured. **Never adjust an expected number to make the
  arithmetic close.**
- **Frontend:** `pnpm -C frontend test` (whole suite). **Do NOT write `pnpm test run`**
  — the extra `run` is a filename filter matching nothing. **Lint:**
  `pnpm -C frontend lint` (oxlint; there is no eslint config).
- **Never run `ruff format` on a directory.** Format by filename. `ruff check src
  tests alembic scripts` is the gate.
- **Generated files.** `docs/ARCHITECTURE.md` (`scripts/gen_architecture.py`),
  `.env.example` (`scripts/gen_env_example.py`), `frontend/src/api/schema.gen.ts`
  (`pnpm -C frontend gen:api`), and **`tests/architecture/full_edition_routes.txt`**.
  Regenerate in the same commit. **Verify with
  `test -n "$(git diff --name-only -- <file>)"`, never `git diff --stat`** —
  measured, `--stat` exits 0 with empty output and can never fail.

  **The routes baseline is NOT produced by `gen_routes.py`** — that generates
  `routes.gen.ts` and `spa_routes.py`, a different pair. Its command lives in a
  comment at `test_edition_surface.py:124-131` and must be run by hand, in **Task 3**,
  in the commit that adds the endpoints:

  ```bash
  APP_EDITION=full DRAFTING_ENABLED=true SPA_MODE=disabled uv run python -c \
    "from call_manager.main import create_app; \
     print('\n'.join(sorted({r.path for r in create_app().routes})))" \
    > tests/architecture/full_edition_routes.txt
  ```
- **Never read, write, copy or back up `.env`.** If a test needs a variable, set it
  inline in the command.
- **`docs/superpowers/specs/` is INPUT.** Byte-identical to `f0aaed0c` at every
  commit. A wrong spec is a `PLAN-DEFECTS.md` entry, not an edit.
- **Every gate must be mutation-tested** in the same task. *A verification that
  reddens nothing is a finding, not a pass.* Do mutate → test → restore **inside one
  shell invocation** and restore with `cp` from a saved copy — never
  `git checkout --` while the fix under test is uncommitted.
- **Name what trivially passes each gate before writing it.** The five hollow shapes:
  a subset check; an absence already true; a mutation naming a test that lacks the
  assertion; a weaker property than the one claimed; an expected output that is what
  the *failure* prints. The two prior slices produced nine and eleven of these.
- **Day one must be a no-op.** At every commit, a user who has changed no setting
  sees exactly what they saw before. That is what `visibility` defaulting to `'org'`
  buys, and Task 1's gate 2 is what proves it.

---

## Scope

**In:** who may **read** a profile, **subscribe** to it, and **receive its digest**.

**Out:** who may **edit** one. Already correct (`profiles.py:342-343`), already the
canonical rule the spec builds boards on (§4.3: *"'Can edit the profile' is not a new
concept … Boards reuse that rule exactly, so no second permission model appears."*).
Do not touch it. Reading becomes **wider** than editing, never narrower.

This lands **before** Call Match Plan B (`feat/call-match-r1b`, plan written, unbuilt).
Plan B's Task 8 gate asserts a `profile_id` *exists and is active*; after this it is
*exists, is active, and is readable*, added in the same function this plan changes.
**Amend Plan B's Task 8 in the merge commit, not before.**

---

## Measured facts this plan rests on

Raw probes: `~/profile-visibility-run/MEASUREMENTS.md`. Cite by anchor string, not by
these line numbers, once the code moves.

| Fact | Where | Why it matters |
| --- | --- | --- |
| Ownership exists and is enforced on writes | `profiles.py:342-343` (403), `:151` (`can_edit`) | The model to extend, not replace |
| `created_by` is NOT NULL, set at creation | `profiles/create.py:74`; declaration `models.py:295` (**not** `:104` — that is a comment inside `User`) | Every profile has an owner; no null-owner branch needed |
| The sharing pattern already exists | `WorkspaceViewer`; predicate `workspaces.py:1473-1479`; migration `0015_workspace_viewers.py` | Copy it |
| **404, not 403, for an unreadable resource** | `workspaces.py:427-428`: *"non-owner must not be able to distinguish … (404) from 'owner only' (403)"* | A 403 confirms the profile exists. Precedent, not invention |
| The digest is built from **subscriptions** | `assemble.py:82-96` | Field-hiding alone is decorative — §Task 5 |
| Anyone may subscribe to anything | `me.py:214-226` (`is_active` only), `Settings.tsx:207` offers every profile | Subscription must be gated by the read rule |
| **`GET /me/settings` returns every active profile's id + name** | `me.py:91-117` — a **different endpoint** from `list_profiles`, and the one `Settings.tsx` actually calls via `useAccountSettings()` | An eighth surface. An earlier draft claimed Settings was fixed "for free" by Task 3; all three reviewers caught that it is not |
| **`resolve_profile_selection` has TWO arms** | `dashboard.py:88-95` (bare visit, stored default) and `:127-131` (explicit `?profile=`) — both `is_active` only | An earlier draft patched only the second. See below |
| **`set_default_profile` accepts any active profile** | `dashboard.py:137-156`, via `POST /me/default-profile`; 404 for missing/inactive, 200 otherwise | An existence oracle **and a full bypass**: store an unreadable profile as your default, then a bare `/api/calls` returns its scores through the ungated first arm |
| `_criterion_detail` renders `profile.structured` into the **call** page | `call_detail_view.py:27` | The surface that does not look like a profile endpoint |
| `best_profile_id` is already owner-scoped | `calls.py:294-303` (`Profile.created_by == user.id`) | Already safe; §Task 4 decides whether to widen it |
| `create_profile` name check is global | `profiles.py:250-254`, index `uq_profiles_active_name` | A **residual oracle this plan does not close** — see "does not do" |
| `full_edition_routes.txt` is **not** produced by `gen_routes.py` | regeneration command in a comment at `test_edition_surface.py:124-131` | Task 3 must regenerate it by hand; `gen_routes.py --check` is a different file |
| Alembic head | `0069_collab_doc_epoch.py`, 69 migrations | This plan's migration is **0070** |

---

## Outside review, round 1 — 2026-09-04

Three model families reviewed the plan before any code existed (`gpt-5.6-sol` 15,
`grok-4.6` 7, `claude-sonnet-5` 11; 148k-char package, 33 findings). **All three
answered the same two questions**: name a leak this plan does not close, and name a
gate that stays green when broken. Every finding below was settled by running
something.

**Three surfaces the plan missed — and together they were a complete bypass.**
All three reviewers found them independently:

| Missed | Measured |
| --- | --- |
| `GET /me/settings` (`me.py:91-117`) | Returns **every** active profile's id and name. The plan claimed the Settings picker was fixed "for free" by Task 3 — but `Settings.tsx` calls `useAccountSettings()`, a **different endpoint** |
| `resolve_profile_selection`'s **bare-visit arm** (`dashboard.py:88-95`) | A second `is_active`-only lookup. The plan cited only `:127-131` |
| `set_default_profile` (`dashboard.py:137-156`) | Accepts any active profile. So: store `P_secret` as your default, load `/api/calls` with **no** `?profile=`, receive its scores. **The plan implemented exactly as written would not have worked** |

**Three of the plan's own gates could not have worked:**

- **V16** removed "the check inside `_criterion_detail`" — whose signature is
  `(name, *, call, profile, ranking)`, with **no user and no session**. It cannot
  hold such a check. The mutation was impossible against a check that could not exist.
- **V5** ran the migration twice to test `ON CONFLICT` — the second run dies at
  `create_table` and never reaches the INSERT.
- **The single-source gate** banned all references to `ProfileViewer`, which the same
  plan's viewer CRUD, migration and fixtures all require. **Red on correct code.**

**Gates that would have stayed green while broken:** gate 6 (editing stays narrower
than reading) had **no mutation at all**; gate 18 likewise; gate 2's fixture never
subscribed the owner, so its own V3 reddened nothing — and the plan **said so in the
V3 row and never fixed the fixture**. Grok added two "absence already true" fixtures:
gate 7 asserted no scores appear where no `CallRanking` rows existed, and gate 12
asserted no digest section where `assemble_for_user` could not have emitted one.

**A defect that would have destroyed data.** Task 5 says a revoked subscription row
must survive. But `to_remove = existing_active_ids - valid_ids`: filtering only
`valid_ids`, as the plan instructed, puts the revoked row in `to_remove` and
**deletes it** — losing `digest_enabled` with it. Two reviewers found it; no gate
saved settings between revoke and assemble, so it would have shipped.

**Also fixed:** `update_profile` was left as a 403-vs-404 existence oracle; the
Definition of done ran in the **main checkout**, not this worktree, so the whole block
could pass without reading the branch; `full_edition_routes.txt` is **not** produced
by `gen_routes.py` and no task regenerated it; the route arithmetic said "105 → 108"
and derived 2-not-3 two sentences later; the `/viewers` response had two incompatible
shapes and `ProfileDetailOut` had no `visibility` field for the UI to read; and the
plan asserted "each task states its delta" while no task did.

**Accepted, not fixed:** the `create_profile` name oracle. See "does not do" — the
alternative puts a correctness constraint on a column users can flip.

**Refuted:** nothing. Every finding checked out. That is itself unusual and worth
recording: the previous two rounds each refuted three or four.

Raw findings: `.superpowers/review/findings-pv/pv-<model>.md`.
Settled points: `.superpowers/review/pv-settled.md`.

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/db/models.py` | `ProfileViewer`; `Profile.visibility` (modify) |
| `alembic/versions/0070_profile_visibility.py` | table, column, subscriber backfill (create) |
| `src/call_manager/web/profile_access.py` | `readable_profile_ids()` — the only definition (create) |
| `src/call_manager/web/api/profiles.py` | list/detail filter; viewers + visibility endpoints (modify) |
| `src/call_manager/web/dashboard.py` | `resolve_profile_selection`; the facet list (modify) |
| `src/call_manager/web/call_detail_view.py` | `_criterion_detail` reached only for a readable profile (modify) |
| `src/call_manager/web/api/me.py` | `update_subscriptions` rejects unreadable ids (modify) |
| `src/call_manager/digest/assemble.py` | skip profiles the recipient can no longer read (modify) |
| `tests/migrations/test_0070_profile_visibility.py` | the backfill gate (create) |
| `tests/web/test_profile_access.py` | the seven-surface gate (create) |
| `tests/architecture/test_profile_access_single_source.py` | one predicate, no second definition (create) |

**Frontend**

| File | Responsibility |
| --- | --- |
| `frontend/src/routes/ProfileVisibilityCard.tsx` | "Who can see this" (create) |
| `frontend/src/routes/ProfileDetail.tsx` | mount the card; tolerate a thin payload (modify) |
| `frontend/src/routes/ProfilesList.tsx` | tolerate a list without `description` (modify) |
| `frontend/src/api/profiles.ts` | viewers + visibility mutations (modify) |
| `frontend/src/routes/Settings.tsx` | the picker lists readable profiles only (modify) |
| `frontend/src/api/schema.gen.ts` | regenerated |

---

### Task 1: The schema, and a migration that changes nothing

**Files:** `db/models.py` (modify) · `alembic/versions/0070_profile_visibility.py`
(create) · `tests/migrations/test_0070_profile_visibility.py` (create) ·
`docs/ARCHITECTURE.md` (regenerate)

**Interfaces:** produces `ProfileViewer` and `Profile.visibility`, consumed by Task 2.

- [ ] **Step 1: The model** — a deliberate copy of `WorkspaceViewer`

```python
class ProfileViewer(Base):
    """A user the profile's owner has explicitly let read it.

    Deliberately identical in shape to workspace_viewers (migration 0015): same
    composite PK, same CASCADEs, same added_at. The read predicate in
    web/profile_access.py copies workspaces.py:1473-1479 for the same reason --
    spec 4.3 requires that no second permission model appears, and boards are
    built on the profile rule.

    Absence of a row is not "no access": visibility='org' and is_admin both grant
    reading without one, and the OWNER never has a row. See readable_profile_ids().
    """

    __tablename__ = "profile_viewers"

    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

On `Profile`, in `__table_args__` and the column list:

```python
CheckConstraint("visibility IN ('private','org')", name="ck_profiles_visibility"),
...
# 'org'  -- every active user may read it. The DEFAULT, so this migration is a
#           no-op for existing rows and nobody's Monday changes.
# 'private' -- owner, admins and explicit profile_viewers rows only.
# String + CHECK rather than a native enum: matches ck_profiles_kind two lines
# up and ck_ranking_remote_attempts_outcome, and avoids ALTER TYPE if a third
# value ever appears.
visibility: Mapped[str] = mapped_column(
    String(10), nullable=False, server_default=text("'org'")
)
```

- [ ] **Step 2: Migration 0070** (`down_revision = "0069"`; head measured
  `0069_collab_doc_epoch.py`, 69 migrations). Follow `0015_workspace_viewers.py`
  verbatim for the table.

```python
# Backfill: every CURRENT subscriber becomes an explicit viewer.
#
# Without this the first owner who flips a profile to 'private' silently cancels
# their colleagues' Monday digest -- no email, no warning, no audit row. Copying
# today's access forward is the conservative direction; the owner can then remove
# people deliberately, which is a decision with a person behind it.
#
# The owner is excluded: readable_profile_ids() grants them access via created_by,
# and a row would make "remove this viewer" able to lock an owner out of their
# own profile.
op.execute("""
    INSERT INTO profile_viewers (profile_id, user_id)
    SELECT s.profile_id, s.user_id
      FROM subscriptions s
      JOIN profiles p ON p.id = s.profile_id
     WHERE p.created_by <> s.user_id
    ON CONFLICT DO NOTHING
""")
```

`ON CONFLICT DO NOTHING` here **is** load-bearing, unlike the one in Call Match Plan
B: `subscriptions` is keyed `(user_id, profile_id)` and `profile_viewers` on
`(profile_id, user_id)`, so the SELECT cannot duplicate — but the migration may be
re-run against a partially migrated database. **Prove it, do not assert it:** the
mutation table below runs without the clause and records what actually happens.

`downgrade()` drops the table and the column. It cannot restore who was private;
say so in the docstring rather than implying a round trip.

- [ ] **Step 3: Gates**

1. **Day one is a no-op, for BOTH new and pre-existing rows.**
   1a. A profile created through `POST /api/profiles` — the real endpoint, not a
       hand-built row — has `visibility == 'org'`.
   1b. **A profile that existed BEFORE the upgrade is `'org'` afterwards, and is
       still readable by an unrelated active user.** Create it, run `alembic upgrade`,
       then assert.

   *What trivially passes:* asserting the column exists (1a's target), and — a
   reviewer's mutation — setting **existing** rows to `private` in the migration
   while leaving the column default `'org'`. That passes 1a and every backfill gate
   while silently making every current profile unreadable. 1b is the only assertion
   that catches it.
2. **The backfill preserves access.** Fixture: profile P owned by A; subscriptions
   for B, C **and A** (owners routinely subscribe to their own profile's digest);
   user D unrelated. After migrating: `profile_viewers` holds exactly
   `{(P,B), (P,C)}` — **not** A (owner excluded), **not** D.

   **A's own subscription is load-bearing in this fixture**, and the first draft
   omitted it while its own V3 row admitted the omission and never fixed it. Without
   A subscribed, dropping the owner-exclusion clause changes nothing observable and
   V3 reddens nothing — a gate that documents its own vacuity in the mutation table
   beside it.
3. **The owner has no row and still reads.** Explicit, because it is the assumption
   the exclusion in step 2 rests on.
4. `visibility='maybe'` raises an `IntegrityError` (the CHECK is real).

- [ ] **Step 4: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V1 | `server_default` `'org'` → `'private'` | gate 1 — **the day-one break, caught by the gate that exists for it** |
| V2 | drop the backfill INSERT | gate 2 (`profile_viewers` empty) |
| V3 | drop `WHERE p.created_by <> s.user_id` | gate 2 — A appears, 3 rows not 2. Fixture must have the owner subscribed to their own profile, or this reddens nothing |
| V4 | drop the CHECK constraint | gate 4 |
| V5 | drop `ON CONFLICT DO NOTHING`, then **run the backfill INSERT alone twice** against an already-upgraded schema | **record what happens.** *Retargeted:* the first draft said "run the migration twice", which a reviewer showed fails at `create_table` before ever reaching the INSERT — it would have proved nothing about conflict handling while looking like a real result. If the INSERT still succeeds twice, the clause is inert and the comment above must say so instead of claiming it is load-bearing |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/migrations/test_0070_profile_visibility.py -q
uv run alembic check
uv run python scripts/gen_architecture.py
test -n "$(git diff --name-only -- docs/ARCHITECTURE.md)" || { echo "generator did not run"; exit 1; }
ruff check src tests alembic scripts
```

---

### Task 2: `readable_profile_ids()` — one definition, and a gate that keeps it one

**Files:** `web/profile_access.py` (create) ·
`tests/architecture/test_profile_access_single_source.py` (create)

**Interfaces:** consumed by Tasks 3, 4, 5. Produces nothing else.

The whole plan's correctness reduces to "every surface calls this and nothing
reimplements it". A second copy that drifts is how nine of the ten surfaces end up
right and the tenth ends up shipping — and the first draft of this plan listed
**seven**, missing three that an outside review found.

- [ ] **Step 1: The predicate**

```python
def readable_profile_ids(user) -> Select:
    """Subquery of profile ids `user` may READ.

    Four arms, in the order they are cheap to evaluate:
      created_by == user.id   -- the owner. NEVER has a profile_viewers row.
      user.is_admin           -- same override profiles.py:342 uses for editing.
      visibility == 'org'     -- the default, so this is most profiles today.
      profile_viewers         -- explicitly shared.

    READING IS WIDER THAN EDITING, always. Editing stays created_by-or-admin
    (profiles.py:342-343, spec 4.3). Do not collapse the two: a colleague a
    profile is shared with must be able to read it and must not be able to
    change it.
    """
```

Return a `Select` of ids, not a list — every caller composes it into a bigger query,
and materialising it makes the facet query O(profiles) round trips.

Also `can_read_profile(session, user, profile_id) -> bool`, implemented **in terms of**
the subquery, for the single-object callers.

- [ ] **Step 2: The single-source gate**

`tests/architecture/test_profile_access_single_source.py`, using `ast`.

**It bans a second READ PREDICATE, not the ORM.** An earlier draft said "no module
references `ProfileViewer` or compares `Profile.visibility`", which a reviewer showed
**can never pass**: Task 1 declares `ProfileViewer` in `models.py`, migration 0070
inserts into it, Task 3's viewer CRUD must read and write it, and the fixtures must
create rows. The gate would have been red on correct code, which means it would have
been deleted or exempted into meaninglessness on day one.

The rule is: **outside the exempt set, no module may compare `Profile.visibility` or
build a `select` over `ProfileViewer` for an access decision.** Exempt, each for a
stated reason: `db/models.py` (declares them), `alembic/versions/0070_*` (backfills),
`web/api/profiles.py`'s viewer-CRUD functions **by name** (they manage the rows; they
do not decide access), and the tests that build fixtures.

**The positive half asserts CALLS, not imports, and names modules exactly.** A
reviewer pointed out that an import survives the removal of its call — leave
`assemble.py`'s import in place, delete the `.where(...)` that uses it, and an
import-counting gate stays green while the digest leaks. And "at least six" is a
floor over a set that has exactly six members, so it tolerates one being dropped if a
seventh is ever added.

So: `ast`-walk each of these six functions and assert each contains a **call** to
`readable_profile_ids`:

```
web/api/profiles.py :: list_profiles, get_profile, update_profile
web/dashboard.py    :: resolve_profile_selection, set_default_profile, build_dashboard_view
web/api/me.py       :: get_settings, update_subscriptions, set_default
digest/assemble.py  :: assemble_for_user
```

Exact names, exact set. When Task 3-5 wire a surface up, this is what notices if one
is skipped — and it is the gate that would have caught the three surfaces the first
draft of this plan missed entirely.

- [ ] **Step 3: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V6 | drop the `created_by` arm | an owner reading their own `private` profile |
| V7 | drop the `visibility == 'org'` arm | every day-one test in Tasks 3-5 |
| V8 | drop the `is_admin` arm | an admin reading a private profile |
| V9 | drop the `profile_viewers` arm | a shared-with colleague reading it |
| V10 | re-implement the predicate inline in `profiles.py` | the single-source gate |
| V11 | **keep** the import in `assemble.py`, delete the call | the positive half. An import-counting gate stays green here — that is the whole point of asserting calls |
| V11b | delete one function from the pinned name list | the gate must fail with "pinned function no longer exists", not silently pass over a renamed function |

V6 matters more than it looks: the owner has no viewer row by construction (Task 1),
so an implementation that checks only `profile_viewers` locks owners out of their own
private profiles — and every *other* test would pass, because they all use `org`.

---

### Task 3: The profiles API

**Files:** `web/api/profiles.py` (modify) · `tests/web/test_profile_access.py` (create)

- [ ] **Step 1: List and detail**

`list_profiles` (`profiles.py:116-138`) filters on `readable_profile_ids(user)`.
`get_profile` (`:140-177`) returns **404** for an unreadable id — not 403.

**`update_profile` (`:314-343`) gets the same 404, before its existing 403.** All
three reviewers caught that an earlier draft left it alone as "editing, out of
scope". It is out of scope as *authorization* — the rule stays `created_by`-or-admin
— but as an **oracle** it is squarely in scope: today it 404s for a missing or
inactive id and 403s for every active profile the caller does not own, so `C` learns
`P_secret` exists by trying to edit it. The order becomes:

```python
if p is None or p.is_active is False:        raise 404   # unchanged
if not can_read_profile(session, user, p):   raise 404   # NEW — same 404, no oracle
if p.created_by != user.id and not user.is_admin: raise 403   # unchanged rule
```

Reading is still wider than editing: `B`, who the profile is shared with, reaches the
403. Only `C` sees the 404. **That distinction is gate 6, and it now has a mutation.**

```python
# 404, not 403. workspaces.py:427-428 already made this call for the same reason:
# "non-owner must not be able to distinguish 'no proposal-spec.md yet' (404) from
# 'owner only' (403) for any workspace UUID they can name." A 403 confirms the
# profile exists, and profile NAMES are the thing worth not confirming: someone
# can walk /api/profiles/1..N and learn who is working on what.
```

Same 404 for the not-found case, so the two are genuinely indistinguishable —
including in timing terms as far as a single `session.get` allows.

- [ ] **Step 2: Gates** — the seven-surface test starts here

`tests/web/test_profile_access.py`. Fixture: owner A; colleague B (shared with);
stranger C; admin D. Profiles: `P_org` (visibility `org`), `P_priv` (private, B is a
viewer), `P_secret` (private, no viewers).

1. C: `P_secret` absent from `GET /api/profiles`; `GET /api/profiles/{P_secret}` →
   **404**.
2. B: `P_priv` present and fully readable.
3. A: `P_secret` readable (owner, no viewer row).
4. D: `P_secret` readable (admin).
5. Everyone: `P_org` readable — **the day-one assertion**.
6. **B gets 403 from `PUT /api/profiles/{P_priv}`.** Reading is wider than editing;
   without this the two rules can silently collapse into one.

*What trivially passes:* only asserting C's absence. That is true if the endpoint
returns nothing at all. Gates 2-5 are what prove the filter is a filter and not a
wall.

- [ ] **Step 3: The sharing endpoints**

Two paths, with explicit and **asymmetric** models — a reviewer caught the first
draft calling `/viewers` "a list of user ids" in one place and saying its response
"carries name and email" in another:

| Op | Request | Response |
| --- | --- | --- |
| `GET /api/profiles/{id}/viewers` | — | `{viewers: [{user_id, name, email}]}` — names, because a list of `[3, 17]` is unusable in a UI |
| `PUT /api/profiles/{id}/viewers` | `{user_ids: [int]}` | the same `GET` shape, so the client can reconcile from the server's answer (the pattern `update_subscriptions` already uses) |
| `PUT /api/profiles/{id}/visibility` | `{visibility: "private" \| "org"}` | `{visibility}` |

**`ProfileDetailOut` also gains `visibility`.** Without it Task 6's segmented control
has no way to render its own current state — it would have to guess, or fetch a
second endpoint that does not exist. Measured: `profiles.py:81-114` has no such field
today.

All three ops are gated on the **edit** rule: 404 for a profile the caller cannot
read, 403 for one they can read but not edit.

Gates, **both halves**: `B` (a viewer, not the owner) gets **403** — otherwise
anyone shared with can re-share and the owner is not the owner. And `C` gets **404**
for `P_secret` **and** for a nonexistent id, on all three operations.

*The exact mutation that slips past the B-only version*, named by two reviewers:
guard everything with `if not can_edit: raise 403`. B's asserted 403 still passes,
and C now has a clean oracle on the very endpoints this plan adds.

- [ ] **Step 3b: Regenerate the route baseline — in THIS task**

Adding two paths makes `tests/architecture/full_edition_routes.txt` stale, and
**nothing else in this plan regenerates it**. `gen_routes.py --check` in the
Definition of done covers `routes.gen.ts` and `spa_routes.py`, a different pair — a
reviewer caught that the final check would fail with no upstream step to prevent it.
The command lives in a comment at `test_edition_surface.py:124-131`:

```bash
APP_EDITION=full DRAFTING_ENABLED=true SPA_MODE=disabled uv run python -c \
  "from call_manager.main import create_app; \
   print('\n'.join(sorted({r.path for r in create_app().routes})))" \
  > tests/architecture/full_edition_routes.txt
git diff -- tests/architecture/full_edition_routes.txt   # read it: exactly 2 added lines
```

**Read the diff, do not just run the command.** A third line means a route was added
that this plan did not intend.

- [ ] **Step 4: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V12 | `get_profile` raises 403 instead of 404 | gate 1's status assertion |
| V13 | `list_profiles` filter removed | gate 1 |
| V14 | the viewers endpoint gated on *read* not *edit* | Step 3's B-403 gate |
| V14b | all three sharing operations guarded by a bare `if not can_edit: raise 403` | Step 3's **C-404** gate. Without that gate this mutation is invisible |
| V15 | `get_profile` returns 404 for **everyone** | gates 2-5 — proves gate 1 is not passing on a dead endpoint |
| V15b | **widen `update_profile`'s check to `readable_profile_ids`** | **gate 6.** An earlier draft had no mutation for gate 6 at all, so the exact regression it exists to catch — reading and editing collapsing into one rule — would have slipped through the whole plan |
| V15c | `update_profile` raises 403 instead of the new 404 for C | the oracle assertion above |

---

### Task 4: The calls API and the dashboard

**Files:** `web/dashboard.py` (modify) · `web/call_detail_view.py` (modify) ·
`web/api/calls.py` (modify) · `tests/web/test_profile_access.py` (extend)

The surfaces here do not look like profile endpoints, which is exactly why they are
the ones a fix misses.

- [ ] **Step 1: `resolve_profile_selection` has TWO arms. Patch both.**

An earlier draft cited only `dashboard.py:127-131` and called it done. All three
reviewers found the other one, and together they form a **complete bypass of this
task as it was written**:

```python
# dashboard.py:88-95 -- BARE VISIT. No ?profile= at all. Honours the STORED default.
if raw_profile is None:
    stored = session.scalar(select(User.default_profile_id).where(User.id == user_id))
    if stored is None: return None
    return session.scalar(
        select(Profile.id).where(Profile.id == stored, Profile.is_active.is_(True)))
                                                       # ^^^^^^^^^ is_active ONLY

# dashboard.py:127-131 -- EXPLICIT ?profile=. The arm the first draft patched.
```

And `set_default_profile` (`:137-156`, via `POST /me/default-profile`) accepts **any
active profile**, so the attack is: store `P_secret` as your default, then load a
bare `/api/calls`. No `?profile=` is ever sent, the patched arm is never reached, and
the response carries `P_secret`'s scores. **Implemented exactly as first written,
this task's guarantee was false.**

All three sites change:

| Site | Change | Why this shape |
| --- | --- | --- |
| `:88-95` stored-default arm | add the readable check; unreadable → `None` | degrade, as for a deactivated default |
| `:127-131` explicit arm | add the readable check; unreadable → `None` | **must not 404**, or `?profile=N` becomes an oracle for every id a user can type |
| `:137-156` `set_default_profile` | add the readable check, raising the **same 404** it already raises for missing/inactive | one status for all three cases, so storing is not an oracle either |

A stored default that later becomes unreadable degrades to the unranked view on the
next visit. The `users.default_profile_id` row is **left alone** — same reasoning as
subscriptions in Task 5: revocation is a filter, not a delete, so re-sharing restores
the preference.

- [ ] **Step 2: The facet list** (`dashboard.py:416`) filters on
  `readable_profile_ids`. Today it is `select(Profile).where(Profile.is_active)` —
  every active profile's **name**, offered to everyone.

- [ ] **Step 3: `_criterion_detail` — the check goes at the CALLER, and the mutation
  moves with it**

An earlier draft said "make the function defensive" and paired it with mutation V16,
"remove the check from `_criterion_detail` only". All three reviewers showed that is
impossible: the signature is

```python
def _criterion_detail(name: str, *, call: Call, profile: Profile, ranking: CallRanking)
```

— **no `user`, no `session`.** It cannot evaluate readability, so there is no
function-local check for V16 to remove, and a "defensive" branch there could never be
reached independently of the resolver. The plan was asking for a mutation that cannot
be performed against a check that cannot exist.

**The enforcement point is the call site** — `calls.py:355-359`, which passes
`profile_obj`. `profile_obj` comes from the resolved id, so Step 1 already secures it;
what Step 3 adds is the **gate and the mutation at that site**, plus a one-line
assertion that `profile_obj` is `None` whenever the resolved id is `None`, so the two
can never drift apart.

Do **not** add a `readable: bool` parameter. It would be passed `True` by the only
caller forever, and a parameter that is never `False` in production is a gate whose
red branch is unreachable — the "absence already true" shape with an argument.

- [ ] **Step 4: `best_profile_id` — a decision, not a fix** (`calls.py:294-303`)

It is already `Profile.created_by == user.id` — **owner-scoped, so already safe**,
and narrower than the new read rule.

**Recommendation: widen it to `readable_profile_ids`.** The point of sharing a
profile with a colleague is that it becomes usable to them, and "best fit" is the
main thing that consumes a profile on the call page. Leaving it owner-only means a
shared profile is visible but never chosen, which will read as a bug.

**This is a behaviour change beyond the privacy fix, so it is flagged rather than
assumed.** If the human prefers owner-only, keep the line as-is and add a comment
saying it is deliberate — a bare `created_by` check will otherwise look like a
surface someone forgot to widen.

- [ ] **Step 5: Gates** (extending Task 3's file, same fixture)

**The fixture must carry `CallRanking` rows for `P_secret`.** A reviewer caught that
without them "no `P_secret` scores appear" is **already true** whatever the code does
— the "absence already true" shape. Seed at least one ranked call per profile, and
have gate 10 assert B *does* see `P_priv`'s score, so the fixture is proven live.

7. C: `GET /api/calls?profile={P_secret}` → **200**, the unranked list, and
   **`effective.profile_id is None`**. Assert the resolved value, not only the
   absence of scores.
7b. **The bypass gate.** C calls `POST /api/me/default-profile` with `P_secret`
   (→ **404**), then `GET /api/calls` **with no `?profile=`** → unranked,
   `effective.profile_id is None`. **This is the gate for the hole all three
   reviewers found**; without it the first draft's patch would have passed everything.
8. C: `P_secret` absent from the facet list; `P_org` present.
9. C: `GET /api/calls/{id}?profile={P_secret}` shows no `P_secret` budget or TRL range.
10. B: all four work for `P_priv`, **and B sees a real score** — the shared profile is
    usable, not merely visible, and the fixture is not empty.
10b. A's stored default is `P_priv`; A revokes B; **B's next bare `/api/calls`
    degrades to unranked, and B's `users.default_profile_id` row still holds `P_priv`**
    — revocation filters, it does not delete the preference.

- [ ] **Step 6: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V16 | remove the check at the `_criterion_detail` **call site** (`calls.py:355-359`) | gate 9. *Retargeted:* the old V16 named a check inside `_criterion_detail`, which cannot hold one |
| V17 | remove it from the **explicit** arm (`:127-131`) only | gate 7 |
| **V17b** | remove it from the **stored-default** arm (`:88-95`) only | **gate 7b.** The first draft patched only the explicit arm, so this is the mutation that represents the plan's own original bug |
| **V17c** | remove it from `set_default_profile` only | gate 7b's 404 assertion |
| V18 | remove it from the facet query only | gate 8 |
| V19 | `resolve_profile_selection` raises 404 instead of degrading | gate 7's status assertion — the oracle |
| V19b | revocation **deletes** `users.default_profile_id` | gate 10b |

**Run V16, V17, V17b, V17c and V18 separately.** Removing several and watching
something go red proves only that at least one gate exists — and the three-arm bug
this task now fixes is exactly what a combined run would have hidden.

---

### Task 5: Subscriptions and the digest — the leak field-hiding cannot close

**Files:** `web/api/me.py` (modify) · `digest/assemble.py` (modify) ·
`frontend/src/routes/Settings.tsx` (modify) · `tests/web/test_profile_access.py`
(extend) · `tests/test_digest_assemble.py` (extend)

**A subscriber receives, every Monday, the ranked list of calls matching a profile.**
That reveals its content whether or not they can read its `description`. Gating the
fields and not the subscription would be a lock on one door of two.

- [ ] **Step 1: `update_subscriptions`** (`me.py:205-255`) — **filter BOTH sides of
  the diff, not just `valid_ids`**

An earlier draft said only "an unreadable id is dropped from the valid set, matching
how the endpoint already drops inactive ids". Two reviewers showed that is exactly
backwards, and it silently does the thing Step 2 forbids:

```python
existing_active_ids = {...subscriptions joined to ACTIVE profiles...}
to_remove = existing_active_ids - valid_ids      # me.py:243
```

Inactive ids survive because they never enter `existing_active_ids`. An
unreadable-but-**active** id does enter it, and filtering only `valid_ids` drops it
out — so it lands in `to_remove` and **the row is deleted**. One innocent settings
save after a revocation destroys the subscription and its `digest_enabled`
preference, and gates 12-13 never save settings in between, so they stay green.

**Treat unreadable exactly like inactive: exclude it from `existing_active_ids` too**,
so it is invisible to the diff in both directions and the row is left untouched.

- [ ] **Step 2: `assemble.py:82-96`** joins the readable predicate, so a profile
  whose access was revoked stops appearing in the next digest. **Revocation must
  take effect without touching `subscriptions`** — the row stays, and the user
  starts receiving it again if re-shared. Deleting subscription rows on revoke would
  silently discard their `digest_enabled` preference.

- [ ] **Step 3: `get_settings` (`me.py:91-117`) — the endpoint the picker actually
  uses, and the one an earlier draft claimed was fixed "for free"**

It is a **separate query** from `list_profiles`:

```python
profiles = session.execute(
    select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
).scalars()          # every active profile's id and name, to every user
```

`Settings.tsx` calls `useAccountSettings()` → `GET /me/settings`, **not**
`GET /api/profiles`. So Task 3 never reaches it, and the first draft's gate 14 would
have failed on correct code while the leak shipped. All three reviewers found this
independently; it is the clearest instance in this plan of a claim that sounded
plausible and was never checked.

Filter it on `readable_profile_ids`. **Gate it on the API response, not the rendered
picker** — a frontend-only fix leaves the JSON leaking to anyone with a browser
console.

- [ ] **Step 4: Gates**

**The digest fixture must contain ranked, matching calls for `P_priv`.** A reviewer
caught that `assemble_for_user` emits a section only when there are matching calls
(`if not ordered: continue`) — so with a users-and-profiles-only fixture, "no
`P_priv` section" is **already true** whether or not the code checks readability.
Gate 13 is what proves the fixture is live: it must show the section *appearing*.

11. C: `PUT /me/subscriptions` naming `P_secret` → `P_secret` not in the returned
    `subscribed` set, and no row written.
11b. **C: `GET /me/settings` contains no `P_secret` id or name.** Asserted on the
    **JSON**, not the rendered picker.
12. B subscribes to `P_priv`; A revokes B's viewer row; **B's next assembled digest
    has no `P_priv` section, and B's `subscriptions` row still exists.**
12b. **Revoke, then B saves settings, then assemble.** The row must *still* exist and
    the section must still be absent. Without the save in the middle, the
    `to_remove` defect in Step 1 passes gate 12 untouched.
13. Re-share; **B's digest contains the `P_priv` section again** — proving revocation
    is a filter, not a delete, and proving the fixture can produce a section at all.
14. The Settings picker renders `P_org` and not `P_secret` for C — the UI half of 11b.

Gate 12 is the one this task exists for. *What trivially passes:* asserting the
subscription row is gone (the behaviour we are avoiding), or asserting an absent
section in a fixture that could never have produced one.

- [ ] **Step 5: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V20 | `update_subscriptions` drops the readable check from `valid_ids` | gate 11 |
| **V20b** | filter `valid_ids` but **not** `existing_active_ids` — the first draft's exact instruction | **gate 12b.** Gate 12 alone stays green; this is the mutation that represents the plan's own original bug |
| V20c | `get_settings` keeps returning every active profile | gate 11b (and **not** gate 14, if 14 mocks the API — run it against both and record which reddens) |
| V21 | `assemble.py` ignores readability | gate 12 |
| V22 | revoke **deletes** the subscription row | gates 12 and 13 |
| V23 | `assemble.py` filters on `profile_viewers` alone | every `org` profile vanishes from every digest — the loudest day-one break, and why V7 in Task 2 matters |

---

### Task 6: The frontend — "Who can see this"

**Files:** `routes/ProfileVisibilityCard.tsx` + test (create) ·
`routes/ProfileDetail.tsx`, `routes/ProfilesList.tsx`, `api/profiles.ts` (modify) ·
`api/schema.gen.ts` (regenerate)

- [ ] **Step 1: The card**, on the profile detail page, visible only when
  `can_edit`. A segmented control — *Only me and admins* / *Everyone here* — plus,
  when private, the viewer list with add and remove.

  **Name people, not ids.** A viewer list of `[3, 17]` is unusable; the `GET
  /viewers` response carries name and email. Reuse whatever the admin users page
  already does for picking a user rather than inventing a second picker.

- [ ] **Step 2: Both list and detail must tolerate a smaller world.** They currently
  assume every profile is fetchable. After Task 3 a bookmarked
  `/profiles/{id}` can 404 for a profile that used to load — the realistic case is a
  colleague following an old link after a re-share, not an attacker. Render "This
  profile isn't shared with you" rather than a crash or a bare "not found".

- [ ] **Step 3: Gates**

15. The card is absent when `can_edit` is false — asserted on a payload where the
    rest of the page **does** render, so it is not passing on a blank page.
16. Flipping the control calls `PUT /visibility` with the right value.
17. `/profiles/{id}` 404 renders the "not shared with you" copy, not a crash.
18. Removing the last viewer from a `private` profile leaves it private and does not
    silently flip it to `org`.

- [ ] **Step 4: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V24 | render the card unconditionally | gate 15 |
| V25 | the control sends `org` for both positions | gate 16 |
| V26 | the 404 branch returns `null` | gate 17 |
| **V27** | removing the last viewer also resets `visibility` to `'org'` | **gate 18.** An earlier draft left gate 18 with no mutation at all — a reviewer noted the plan was violating its own global rule three gates after stating it |

- [ ] **Step 5: Verify**

```bash
pnpm -C frontend test && pnpm -C frontend lint && pnpm -C frontend build
pnpm -C frontend gen:api
test -n "$(git diff --name-only -- frontend/src/api/schema.gen.ts)" || { echo "generator did not run"; exit 1; }
```

---

### Task 7: Browser coverage and CI

**Files:** `frontend/e2e/` (modify) · `.github/workflows/ci.yml` (modify if the new
spec needs wiring)

- [ ] **Step 1:** A Playwright walk against `page.route` mocks: owner sees the card;
  flips to private; adds a viewer; a non-viewer's profile list does not contain it.
- [ ] **Step 2:** Pin scenario titles by exact string and the `expect(` count by
  **equality**, not a floor. Plan A pinned `>= 18` against a measured 22, and a floor
  with four spare assertions is four assertions anyone can delete. **Measure the
  number and write what the command prints.**
- [ ] **Step 3: Mutations.** V27: delete a scenario — the title pin reddens. V28:
  strip one scenario's assertions, keep its title — the count reddens. Then, for each
  scenario, mutate the **production behaviour** it protects; a scenario whose
  behavioural mutation reddens nothing asserts nothing, whatever its count says.

---

## Definition of done

From a clean tree, in this order:

```bash
# THE WORKTREE, not the main checkout. An earlier draft said
# /home/parlar_ai/dev/call-monitor, which holds master -- so the whole block could
# run green without ever reading this branch's code or its new tests. A reviewer
# caught it; it is the "git diff base..HEAD run ON base" shape wearing a cd.
cd /home/parlar_ai/dev/call-monitor/.worktrees/profile-visibility
ruff check src tests alembic scripts
uv run alembic check
uv run pytest -n 16 --dist loadfile
uv run python scripts/gen_routes.py --check
uv run python scripts/gen_architecture.py && git diff --exit-code docs/ARCHITECTURE.md
uv run python scripts/gen_env_example.py && git diff --exit-code .env.example
pnpm -C frontend gen:api && git diff --exit-code frontend/src/api/schema.gen.ts
pnpm -C frontend test
pnpm -C frontend lint
pnpm -C frontend build
pnpm -C frontend build:match && pnpm -C frontend check:bundle
pnpm -C frontend e2e:match
```

(`--exit-code` here asserts **no drift after regenerating**, which is the opposite of
the per-task "this file must change" check and is correct in this position.)

Expectations:

- **`1 skipped` — exactly one.** `0 skipped` means a live Anthropic call ran.
- **The pass total will exceed `4915`.** State the number you measured and the
  per-task deltas producing it. Do not copy a predicted total into this section.
- `tests/architecture/full_edition_routes.txt`: **105 → 107 — PREDICTED (not run).**
  Two added paths, because the file holds unique **paths** and `GET`+`PUT /viewers`
  collapse to one line:

  ```
  /api/profiles/{profile_id}/viewers
  /api/profiles/{profile_id}/visibility
  ```

  **Measure it and write the real number.** An earlier draft wrote "105 → 108" and
  then derived 2-not-3 two sentences later without correcting the headline — all
  three reviewers caught the contradiction. This exact arithmetic was wrong three
  different ways in Call Match Plan B, which is why it is labelled rather than
  trusted.
- `check_match_bundle.mjs` exits 0 and still reports **0** modules under the 14
  excluded paths.
- `docs/superpowers/specs/` unchanged: `git diff --quiet f0aaed0c -- docs/superpowers/specs/`.
- `.env` untouched.

Then a manual smoke, because no gate proves the product is usable:

```bash
pnpm -C frontend build && pnpm -C frontend package:match
SPA_MODE=prod uv run uvicorn call_manager.main:create_app --factory
```

As user A: open a profile, flip it to private, add B as a viewer. As B: the profile
is listed and readable, `PUT` is refused, the digest picker offers it. As C: it is
absent everywhere and `/profiles/{id}` says it is not shared with them.

---

## What this plan deliberately does not do

- **Does not change who may edit a profile.** `created_by`-or-admin, untouched
  (`profiles.py:342-343`, spec §4.3). Reading becomes wider than editing; that is
  the intended asymmetry.
- **Does not make anything private.** Everything defaults to `org`; the product
  changes the day an owner flips a switch, not the day this merges. Whether to
  narrow anything by default is a separate decision, and should be taken with the
  subscriber counts from §7 of the scope note in hand.
- **Does not gate `boards` (Plan C).** They build on the edit rule, which does not
  move. A published board is public **by design** and is governed by admin
  publication, not by this.
- **Does not close the profile-NAME oracle in `create_profile`.** `profiles.py:250-254`
  checks `select(Profile.id).where(Profile.name == body.name, Profile.is_active)` and
  409s, backed by the DB-wide partial unique index `uq_profiles_active_name`. So a
  user who **guesses an exact name** learns it exists and is active. All three
  reviewers raised it; two rated it critical.

  **Accepted, and the claim narrowed rather than the code changed.** Closing it means
  scoping active-name uniqueness by visibility — a partial index over a mutable
  column, so a profile flipping to `private` could collide with a name created while
  it was hidden, and creation would start failing for reasons the user cannot see.
  That is a worse product than the oracle. The oracle also requires guessing the
  exact string, which is a far weaker capability than enumerating `/profiles/1..N`,
  the thing this plan actually closes.

  **Task 3's comment must therefore not claim names are unconfirmable.** It says
  names cannot be *enumerated*. If exact-name confirmation ever matters, the fix is a
  rate limit on `POST /api/profiles`, not an index change — cheaper, and it does not
  put a correctness constraint on a column users can flip.
- **Is not retroactive.** It changes who can read from now on. Digests already sent
  cannot be recalled.
- **Does not add profile groups or roles.** A flat viewer list, exactly like
  `workspace_viewers`. If per-department roles are ever wanted, that is a new
  design, and this plan is deliberately not a half-step toward one.
- **Does not amend Call Match Plan B.** Plan B's Task 8 gate widens from
  *exists-and-active* to *exists-active-and-readable* **in the merge commit** that
  brings this to master, not before — so the two plans never disagree in the tree at
  the same time.
