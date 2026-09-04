# Profiles are personal — scope

**Decided 2026-09-04 by the human:** *"all profiles should be personal even if used
for an organization."* Shape chosen: **private by default, the owner shares
explicitly** — with named colleagues or org-wide.

**Status:** scoped and designed, **not implemented**. One sequencing question for the
human at the end.

**Why this exists.** Verifying an unrelated Call Match finding turned up that profile
*reads* are org-wide. Writes were already correct (`profiles.py:342-343`); this is a
read-visibility change, and the decision above turns it into a small ownership model.

---

## 1. What is exposed today — measured on `master` `9365f123`

| Surface | Returns | Filter today |
| --- | --- | --- |
| `GET /api/profiles` (`profiles.py:116-138`) | every profile: id, name, kind, level, areas_count, budget_currency, **full `description`** | `is_active` |
| `GET /api/profiles/{id}` (`:140-205`) | the above plus all of `_flatten_structured` | `is_active` |
| `GET /api/calls?profile=N` (`dashboard.py:127-131`) | that profile's match scores across every call | `is_active` |
| `GET /api/calls/{id}?profile=N` → `_criterion_detail` (`call_detail_view.py:27`) | renders `profile.structured` — budget and TRL ranges — into the call detail page | `is_active` |
| `PUT /me/subscriptions` (`me.py:205-255`) | subscribe to **any** profile | `is_active` |
| `GET /api/calls` facets (`calls.py:90-92`) | profile **id + name** only | — |

`_flatten_structured` (`profiles.py:48-77`) returns `areas`, budget/TRL ranges,
`must_include`, `hard_exclusions`, `org_type`, `capabilities`,
`patient_populations`, `scale`, `career_stage`, `role`, `years_since_phd`,
`applicant_types`, `funding_instruments`, `funder_exclusions`, `country`,
`host_institution`. `ProfileKind` (`db/models.py:227-229`) is
`organizational_unit | individual`, so on an individual profile several of those
describe a named colleague.

---

## 2. The two facts that shape the design

**2.1 — The digest is the profile's output, so field-hiding alone is not a control.**
`digest/assemble.py:82-96` builds a user's weekly email from their **subscribed**
profiles. A subscriber receives, every Monday, the ranked list of calls matching that
profile — which reveals its content whether or not they can read the `description`
field. `Settings.tsx:207` currently offers **every** active profile as a checkbox
("Check the profiles whose Monday-morning digest you want emailed to you"), and
`me.py:214-226` validates only `is_active`. **Subscription must be gated by the same
predicate as reading**, or the change is decorative.

**2.2 — There is already a sharing pattern; do not invent a second one.**
`workspace_viewers` (`WorkspaceViewer`: `workspace_id`, `user_id`, `added_at`) with
the predicate at `workspaces.py:1473-1479`:

```python
viewer_ids = select(WorkspaceViewer.workspace_id).where(WorkspaceViewer.user_id == user.id)
... or_(Workspace.owner_id == user.id, Workspace.id.in_(viewer_ids))
```

Profiles mirror it exactly. This matters beyond tidiness: spec §4.3 makes a point of
*"no second permission model appears"*, and boards (Plan C) are built on the profile
rule, so a divergent shape here propagates.

---

## 3. Design

### 3.1 Schema

- **`profile_viewers`** — `(profile_id, user_id)` composite PK, `added_at`, both FKs
  `ON DELETE CASCADE`. A byte-for-byte analogue of `workspace_viewers`.
- **`profiles.visibility`** — `String(10)` + `CheckConstraint("visibility IN
  ('private','org')")`, following `ck_ranking_remote_attempts_outcome` rather than a
  native enum (no `ALTER TYPE` when a third value appears).

`org` is retained deliberately: the human's instruction was "personal … **even if
used for an organization**", i.e. a department profile is one person's, shared
onward — not that org-wide sharing must become impossible. Making it a deliberate
act is the change; making it unavailable is not.

### 3.2 The read predicate — one function, used everywhere

```python
def readable_profiles(user) -> Select:
    """Profile ids `user` may read. The ONLY definition -- every surface calls this.
    Mirrors workspaces.py:1473-1479 (owner OR explicit viewer), plus the org escape
    hatch and the admin override that profiles.py:151/342 already use for editing."""
```

`created_by == user.id` **OR** `user.is_admin` **OR** `visibility == 'org'` **OR**
`EXISTS profile_viewers(profile_id, user.id)`.

Editing stays exactly as it is (`created_by` or admin) — narrower than reading, and
out of scope.

### 3.3 Migration `0070_profile_visibility` (or `0071` if Plan B lands first)

1. Create `profile_viewers`; add `visibility` defaulting to **`'org'`**.
   **Nothing changes on day one.** Every profile keeps today's behaviour; owners
   narrow the ones they want.
2. **Backfill a viewer row for every existing subscriber**
   (`INSERT … SELECT user_id, profile_id FROM subscriptions`), excluding the owner.
   Without this, the first owner who flips to `private` silently cancels colleagues'
   Monday digests with no notification and no audit trail. Preserving current access
   is the conservative direction; the owner can then remove people deliberately.
   Same reasoning as Call Match Plan B's dismissal fan-out.

### 3.4 Enforcement — all six surfaces

| File | Change |
| --- | --- |
| `web/api/profiles.py` | `list_profiles` and `get_profile` filter on `readable_profiles` |
| `web/dashboard.py:127-131` | `resolve_profile_selection` adds the readable check |
| `web/dashboard.py:416` | the facet list filters too |
| `web/call_detail_view.py:27` | `_criterion_detail` is only reached for a readable profile |
| `web/api/me.py:214-226` | `update_subscriptions` rejects unreadable ids (**§2.1**) |
| `digest/assemble.py:82-96` | skip profiles the recipient can no longer read, so revoking access stops the email |

### 3.5 New endpoints

`GET/PUT /api/profiles/{id}/viewers` and `PUT /api/profiles/{id}/visibility`, both
gated on the **edit** rule (`created_by` or admin). Frontend: a "Who can see this"
card on the profile detail page.

---

## 4. Gates, written before the code

1. **All six surfaces, one test.** A user who may not read profile P gets: no P in
   `/api/profiles`; 404 on `/api/profiles/P`; no P in the facet list; P rejected by
   `?profile=P`; no P budget range from `_criterion_detail`; 4xx from
   `PUT /me/subscriptions` naming P; and no P section in their assembled digest.
   *What trivially passes:* testing `/api/profiles` alone. Seven assertions.
2. **Default is `org`, asserted on a profile created through the real endpoint** —
   not a hand-built row. This is what proves nothing breaks on day one.
3. **The migration preserves access.** Fixture: profile owned by A, `subscriptions`
   rows for B and C. After migration, `profile_viewers` holds B and C and **not** A
   (the owner needs no row). Flip to `private`; B still gets the digest; D does not.
4. **Editing is unchanged.** A viewer who is not the creator still gets **403** from
   `PUT /api/profiles/{id}`. Reading is wider than editing; this gate stops the two
   collapsing into one.

**Mutations that must redden:**

| # | Mutation | Reddens |
| --- | --- | --- |
| V1 | remove the check from `_criterion_detail` **only** | gate 1. If the suite stays green, gate 1 tests one surface and claims six |
| V2 | remove it from `update_subscriptions` only | gate 1's subscription assertion |
| V3 | `assemble.py` ignores readability | gate 3's "B still gets the digest / D does not" |
| V4 | migration skips the subscriber backfill | gate 3 |
| V5 | default `visibility` to `'private'` | gate 2 — the day-one break, caught by the gate that exists to catch it |
| V6 | `readable_profiles` returns viewer-only, dropping the `created_by` arm | an owner reading their own private profile |

---

## 5. Not in scope

- **Write authorization.** Already correct, already spec-canonical (§4.3). Untouched.
- **`users` data.** A different surface.
- **Boards (Plan C).** They build on the *edit* rule, which does not move.
- **Retroactive privacy.** This changes who can read from now on. It does not
  un-send digests already delivered, and it cannot.

---

## 6. Interaction with Call Match Plan B — read before sequencing

Plan B's Task 8 gate 5 currently asserts a `profile_id` **exists and is active**.
Under this change the rule becomes **exists, is active, and is readable by the
caller** — because `resolve_profile_selection` is exactly where §3.4 adds the check.

- **If this lands first:** Plan B's gate is written once, correctly, and Task 8 needs
  a one-line amendment before it is built.
- **If Plan B lands first:** Plan B ships a gate that is right for today and needs
  widening afterwards, and every `?profile=` surface Plan B adds (`?view=`, the
  counts endpoint, `/calls/{id}/save`) becomes another place to retrofit the check.

**Recommendation: this first.** It is the smaller change, it is a privacy change
rather than a feature, and Plan B adds three new surfaces that would otherwise each
need retrofitting. Estimated **4-5 days** with the gates above; Plan B is unblocked
the moment it merges.

---

## 7. What could not be measured here

Needs the live database, whose URL is in `.env` — which this work must not read.

```sql
SELECT kind, count(*) FROM profiles WHERE is_active GROUP BY kind;
SELECT p.id, p.name, count(s.user_id) AS subscribers
  FROM profiles p LEFT JOIN subscriptions s ON s.profile_id = p.id
 WHERE p.is_active GROUP BY p.id, p.name ORDER BY subscribers DESC;
```

The second one sizes §3.3 step 2 and tells you how many people would be affected the
first time an owner flips a well-subscribed profile to `private`. Worth knowing
before, not after.
