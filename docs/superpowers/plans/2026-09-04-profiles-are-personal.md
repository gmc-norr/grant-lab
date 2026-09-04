# Profiles are personal — private by default, shared explicitly

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A profile belongs to the person who made it. Others see it only if the
owner shares it — with named colleagues, or org-wide. Reading, subscribing and
receiving its digest all move behind **one** predicate, so there is no surface left
where a profile leaks by a route nobody thought about.

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

  Each task states its **delta**. Re-measure before asserting a total. **Never adjust
  an expected number to make the arithmetic close.**
- **Frontend:** `pnpm -C frontend test` (whole suite). **Do NOT write `pnpm test run`**
  — the extra `run` is a filename filter matching nothing. **Lint:**
  `pnpm -C frontend lint` (oxlint; there is no eslint config).
- **Never run `ruff format` on a directory.** Format by filename. `ruff check src
  tests alembic scripts` is the gate.
- **Generated files.** `docs/ARCHITECTURE.md` (`scripts/gen_architecture.py`),
  `.env.example` (`scripts/gen_env_example.py`), `frontend/src/api/schema.gen.ts`
  (`pnpm -C frontend gen:api`). Regenerate in the same commit. **Verify with
  `test -n "$(git diff --name-only -- <file>)"`, never `git diff --stat`** —
  measured, `--stat` exits 0 with empty output and can never fail.
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
| `created_by` is NOT NULL, set at creation | `profiles/create.py:74`, `models.py:104` | Every profile has an owner; no null-owner branch needed |
| The sharing pattern already exists | `WorkspaceViewer`; predicate `workspaces.py:1473-1479`; migration `0015_workspace_viewers.py` | Copy it |
| **404, not 403, for an unreadable resource** | `workspaces.py:427-428`: *"non-owner must not be able to distinguish … (404) from 'owner only' (403)"* | A 403 confirms the profile exists. Precedent, not invention |
| The digest is built from **subscriptions** | `assemble.py:82-96` | Field-hiding alone is decorative — §Task 5 |
| Anyone may subscribe to anything | `me.py:214-226` (`is_active` only), `Settings.tsx:207` offers every profile | Subscription must be gated by the read rule |
| `_criterion_detail` renders `profile.structured` into the **call** page | `call_detail_view.py:27` | The surface that does not look like a profile endpoint |
| `best_profile_id` is already owner-scoped | `calls.py:294-303` (`Profile.created_by == user.id`) | Already safe; §Task 4 decides whether to widen it |
| Alembic head | `0069_collab_doc_epoch.py`, 69 migrations | This plan's migration is **0070** |

---

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/db/models.py` | `ProfileViewer`; `Profile.visibility` (modify) |
| `alembic/versions/0070_profile_visibility.py` | table, column, subscriber backfill (create) |
| `src/call_manager/web/profile_access.py` | `readable_profiles()` — the only definition (create) |
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
    reading without one, and the OWNER never has a row. See readable_profiles().
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
# The owner is excluded: readable_profiles() grants them access via created_by,
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

1. **Day one is a no-op.** A profile created through `POST /api/profiles` — the real
   endpoint, not a hand-built row — has `visibility == 'org'`. *What trivially
   passes:* asserting the column exists. Assert the **value**, from the endpoint.
2. **The backfill preserves access.** Fixture: profile P owned by A; subscriptions
   for B and C; user D unrelated. After migrating: `profile_viewers` holds exactly
   `{(P,B), (P,C)}` — **not** A (owner excluded), **not** D.
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
| V5 | drop `ON CONFLICT DO NOTHING`, run the migration twice | **record what happens.** If it still succeeds, the clause is inert here too and the comment above must say so rather than claim it is load-bearing |

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/migrations/test_0070_profile_visibility.py -q
uv run alembic check
uv run python scripts/gen_architecture.py
test -n "$(git diff --name-only -- docs/ARCHITECTURE.md)" || { echo "generator did not run"; exit 1; }
ruff check src tests alembic scripts
```

---

### Task 2: `readable_profiles()` — one definition, and a gate that keeps it one

**Files:** `web/profile_access.py` (create) ·
`tests/architecture/test_profile_access_single_source.py` (create)

**Interfaces:** consumed by Tasks 3, 4, 5. Produces nothing else.

The whole plan's correctness reduces to "every surface calls this and nothing
reimplements it". A second copy that drifts is how six of the seven surfaces end up
right and the seventh ends up shipping.

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

`tests/architecture/test_profile_access_single_source.py` uses `ast` to assert that
outside `web/profile_access.py` and its own tests, **no module** references
`ProfileViewer` or compares `Profile.visibility`. Every consumer must go through the
function.

*What trivially passes:* a grep with an exemption list that happens to cover every
current file — the "absence already true" shape. So the test carries a **positive**
half too: at least six modules must import `readable_profile_ids`, named explicitly.
When Task 3-5 wire a surface up, this count is the thing that notices if one is
skipped.

- [ ] **Step 3: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V6 | drop the `created_by` arm | an owner reading their own `private` profile |
| V7 | drop the `visibility == 'org'` arm | every day-one test in Tasks 3-5 |
| V8 | drop the `is_admin` arm | an admin reading a private profile |
| V9 | drop the `profile_viewers` arm | a shared-with colleague reading it |
| V10 | re-implement the predicate inline in `profiles.py` | the single-source gate |
| V11 | remove one module from the importer list | the single-source gate's positive half |

V6 matters more than it looks: the owner has no viewer row by construction (Task 1),
so an implementation that checks only `profile_viewers` locks owners out of their own
private profiles — and every *other* test would pass, because they all use `org`.

---

### Task 3: The profiles API

**Files:** `web/api/profiles.py` (modify) · `tests/web/test_profile_access.py` (create)

- [ ] **Step 1: List and detail**

`list_profiles` (`profiles.py:116-138`) filters on `readable_profile_ids(user)`.
`get_profile` (`:140-205`) returns **404** for an unreadable id — not 403.

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

`GET/PUT /api/profiles/{id}/viewers` (a list of user ids) and
`PUT /api/profiles/{id}/visibility`. Both gated on the **edit** rule, both 404 for a
profile the caller cannot read, 403 for one they can read but not edit.

Gate: B (a viewer, not the owner) gets **403** from both — otherwise anyone shared
with can re-share, and the owner is not the owner.

- [ ] **Step 4: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V12 | `get_profile` raises 403 instead of 404 | gate 1's status assertion |
| V13 | `list_profiles` filter removed | gate 1 |
| V14 | the viewers endpoint gated on *read* not *edit* | Step 3's gate |
| V15 | `get_profile` returns 404 for **everyone** | gates 2-5 — this is the mutation that proves gate 1 is not passing on a dead endpoint |

---

### Task 4: The calls API and the dashboard

**Files:** `web/dashboard.py` (modify) · `web/call_detail_view.py` (modify) ·
`web/api/calls.py` (modify) · `tests/web/test_profile_access.py` (extend)

The surfaces here do not look like profile endpoints, which is exactly why they are
the ones a fix misses.

- [ ] **Step 1: `resolve_profile_selection`** (`dashboard.py:127-131`) adds the
  readable check to its existing `is_active` check. An unreadable id resolves to
  `None` — i.e. it degrades to the unranked view, the same as a stale or deactivated
  id does today. **It must not 404**, or `?profile=N` becomes an existence oracle for
  every id a user can type.

- [ ] **Step 2: The facet list** (`dashboard.py:416`) filters on
  `readable_profile_ids`. Today it is `select(Profile).where(Profile.is_active)` —
  every active profile's **name**, offered to everyone.

- [ ] **Step 3: `_criterion_detail`** (`call_detail_view.py:27`) is only reached for
  a readable profile. Step 1 already guarantees that on the main path, because
  `profile_obj` is loaded from the resolved id — **but assert it rather than assume
  it**, and make the function defensive: it is called with a `Profile` object and
  cannot see how that object was chosen.

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

7. C: `GET /api/calls?profile={P_secret}` returns the **unranked** list — no
   `P_secret` scores, and **200 not 404**.
8. C: `P_secret` absent from the facet list; `P_org` present.
9. C: `GET /api/calls/{id}?profile={P_secret}` shows no `P_secret` budget or TRL
   range from `_criterion_detail`.
10. B: all three work for `P_priv` — the shared profile is usable, not just visible.

- [ ] **Step 6: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V16 | remove the check from `_criterion_detail` **only** | gate 9. **If the suite stays green, the seven-surface gate tests fewer than seven surfaces and says seven** |
| V17 | remove it from `resolve_profile_selection` only | gate 7 |
| V18 | remove it from the facet query only | gate 8 |
| V19 | `resolve_profile_selection` raises 404 instead of degrading | gate 7's status assertion — the existence oracle |

V16, V17 and V18 must be run **separately**. Removing all three at once and watching
something go red proves only that at least one gate exists.

---

### Task 5: Subscriptions and the digest — the leak field-hiding cannot close

**Files:** `web/api/me.py` (modify) · `digest/assemble.py` (modify) ·
`frontend/src/routes/Settings.tsx` (modify) · `tests/web/test_profile_access.py`
(extend) · `tests/test_digest_assemble.py` (extend)

**A subscriber receives, every Monday, the ranked list of calls matching a profile.**
That reveals its content whether or not they can read its `description`. Gating the
fields and not the subscription would be a lock on one door of two.

- [ ] **Step 1: `update_subscriptions`** (`me.py:214-226`) validates
  `is_active` **and** readable. An unreadable id is dropped from the valid set —
  matching how the endpoint already drops inactive ids, rather than 400-ing, so a
  stale browser tab does not break a save.

- [ ] **Step 2: `assemble.py:82-96`** joins the readable predicate, so a profile
  whose access was revoked stops appearing in the next digest. **Revocation must
  take effect without touching `subscriptions`** — the row stays, and the user
  starts receiving it again if re-shared. Deleting subscription rows on revoke would
  silently discard their `digest_enabled` preference.

- [ ] **Step 3: `Settings.tsx`** — the picker lists readable profiles only. It
  already renders `p.name` from the list endpoint, so Task 3 Step 1 mostly does this
  for free; the gate is what confirms "mostly".

- [ ] **Step 4: Gates**

11. C: `PUT /me/subscriptions` naming `P_secret` → `P_secret` **not** in the returned
    `subscribed` set, and no row written.
12. B subscribes to `P_priv`; A revokes B's viewer row; **B's next assembled digest
    has no `P_priv` section, and B's `subscriptions` row still exists.**
13. Re-share; B's digest contains `P_priv` again — proving revocation is a filter,
    not a delete.
14. The Settings picker renders `P_org` and not `P_secret` for C.

Gate 12 is the one this task exists for. *What trivially passes:* asserting the
subscription row is gone — which is the behaviour we are explicitly avoiding.

- [ ] **Step 5: Mutations**

| # | Mutation | Reddens |
| --- | --- | --- |
| V20 | `update_subscriptions` drops the readable check | gate 11 |
| V21 | `assemble.py` ignores readability | gate 12 |
| V22 | revoke **deletes** the subscription row | gates 12 and 13 |
| V23 | `assemble.py` filters on `profile_viewers` alone | every `org` profile vanishes from every digest — the loudest possible day-one break, and the reason V7 in Task 2 matters |

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
cd /home/parlar_ai/dev/call-monitor
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
- `tests/architecture/full_edition_routes.txt`: **105 → 108**. Three added paths —
  `/api/profiles/{profile_id}/viewers`, `/api/profiles/{profile_id}/visibility`, and
  nothing else. **PREDICTED (not run):** the file holds unique paths and `GET`+`PUT
  /viewers` is one line, so this is **2**, not 3 — **measure it and write the real
  number.** This exact arithmetic was wrong three different ways in Call Match Plan B.
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
- **Is not retroactive.** It changes who can read from now on. Digests already sent
  cannot be recalled.
- **Does not add profile groups or roles.** A flat viewer list, exactly like
  `workspace_viewers`. If per-department roles are ever wanted, that is a new
  design, and this plan is deliberately not a half-step toward one.
- **Does not amend Call Match Plan B.** Plan B's Task 8 gate widens from
  *exists-and-active* to *exists-active-and-readable* **in the merge commit** that
  brings this to master, not before — so the two plans never disagree in the tree at
  the same time.
