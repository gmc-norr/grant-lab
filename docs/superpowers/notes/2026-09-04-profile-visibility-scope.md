# Profile visibility — scope

**Status:** scoped, not decided. Nothing here is implemented. The central question
is a policy call for the human; the engineering follows from the answer.

**Why this exists.** While verifying an unrelated finding in Call Match Plan B I
claimed the app had no profile ownership model. That was wrong — `profiles.py:342`
enforces `created_by == user.id or user.is_admin` on writes, and spec §4.3 names
that rule as canonical. But measuring it turned up something the correction did not
dispose of: **profile *reads* are org-wide, and some profiles describe people.**

---

## 1. Measured exposure

Every row below was read from the source on `master` `9365f123`, not inferred.

| Surface | What it returns | To whom | Filter |
| --- | --- | --- | --- |
| `GET /api/profiles` (`profiles.py:116-138`) | id, name, kind, level, areas_count, budget_currency, **full `description`** — for **every** profile | any logged-in user | `is_active` only |
| `GET /api/profiles/{id}` (`:140-205`) | the above plus all of `_flatten_structured` (below) | any logged-in user | `is_active` only |
| `GET /api/calls?profile=N` (`dashboard.py:127-131`) | that profile's match scores across every call | any logged-in user | `is_active` only |
| `GET /api/calls/{id}?profile=N` → `_criterion_detail` (`call_detail_view.py:27`) | renders `profile.structured` — budget range, TRL range — **into the call detail page** | any logged-in user | `is_active` only |
| `PUT /me/subscriptions` (`me.py:205-255`) | subscribe to any profile | any logged-in user | `is_active` only |
| `GET /api/calls` facets (`calls.py:90-92`) | profile **id + name** only | any logged-in user | — |

`_flatten_structured` (`profiles.py:48-77`) returns:

```
areas, budget_min/max, trl_min/max, must_include, hard_exclusions,
org_type, capabilities, patient_populations, scale,
career_stage, role, years_since_phd, applicant_types,
funding_instruments, funder_exclusions, country, host_institution
```

Writes are correctly gated (`profiles.py:342-343`, 403 unless creator or admin).
**This is a read-visibility question only.**

---

## 2. Three measured facts that constrain any fix

**2.1 — Some profiles are people.** `ProfileKind` (`db/models.py:227-229`) is
`organizational_unit | individual`. On an `individual` profile, `career_stage`,
`role`, `years_since_phd` and `host_institution` describe a named colleague. Whether
that is a problem depends on how many such profiles exist and what people put in
them — **not measurable from here** (see §6), and the first thing to check.

**2.2 — "Subscribers can read it" is self-granting, so it is not a control.**
`PUT /me/subscriptions` validates only `Profile.is_active`. Any user can subscribe
to any active profile and would thereby grant themselves the visibility. A
subscriber-based rule only becomes meaningful if subscription itself is gated, which
is a second change with its own UX cost (someone must approve requests).

**2.3 — There are two leak surfaces, not one.** Narrowing `/api/profiles` alone
leaves `?profile=N` on the calls API returning another profile's scores, and
`_criterion_detail` rendering its budget and TRL ranges into a call detail page. Any
fix has to cover both, and the calls-API half is the one that is easy to forget
because it does not look like a profile endpoint.

---

## 3. The decision — the human's, not the agent's

Read §2.1 first: if every profile is an org unit, this is close to a non-issue and
**Option A** is right.

### Option A — leave it, write down that it is deliberate

Org-wide read is intentional: a single-hospital internal tool, accounts restricted
to an allowed email domain and admin-approved, profiles describing research
directions people are trying to get funded. Discoverability is a feature — it is how
you find who else works on something.

- **Work:** a paragraph in `docs/ARCHITECTURE.md` and one architecture test pinning
  that `list_profiles` has no owner filter **on purpose**, so a future reader does
  not "fix" it. ~half a day.
- **Cost:** none, unless §2.1 turns up personal detail people did not expect to be
  public.

### Option B — split the payload: names public, detail scoped

Everyone keeps seeing every profile's **name, kind, level** (so discovery, the
dashboard picker and the facets still work). **`description` and `structured`** go
to creator + admin + subscribers.

- Needs 2.2 solved, or it is decorative. Simplest honest version: **gate the detail
  on creator + admin only**, and treat "I want to see this group's profile" as a
  conversation, not a button.
- **Work:** a `ProfileListItemOut` without `description`; a `can_read_detail`
  predicate beside the existing `can_edit`; `get_profile` returns a thin body to
  non-readers; `?profile=N` on both calls endpoints rejects a profile the caller
  cannot read; `_criterion_detail` gets a redacted branch. Plus the frontend's
  profile list and detail pages, which today assume the full payload.
- **Size:** ~2-3 days with gates. Touches 6 backend files and 3 frontend routes.
- **Cost:** breaks the "browse another group's matches" behaviour, which may be
  load-bearing for collaboration. Ask before assuming it is not.

### Option C — a per-profile visibility flag

`profiles.visibility ∈ ('org','restricted')`, defaulting to `org` so nothing changes
until someone opts in. The creator flips it.

- **Work:** Option B's mechanism, plus a migration, plus the flag in the profile
  editor, plus a decision about what an `individual` profile defaults to.
- **Size:** ~4-5 days. Also the only option that scales if the answer differs per
  group, which in a hospital it usually does.
- **Cost:** a new concept in the product, and a default that will be wrong for
  somebody.

**Recommendation:** answer §2.1 first, then **A** if profiles are all org units,
**C** if individual profiles carry real personal detail. B is the awkward middle —
it pays most of C's implementation cost without C's ability to say "this one is
fine, that one is not."

---

## 4. Whichever option: the gates that must exist

Written now so they are not invented later to fit the code.

1. **Both surfaces, not one.** A test that a caller who may not read profile P gets
   neither P's `description` from `/api/profiles` **nor** P's scores from
   `/api/calls?profile=P` **nor** P's budget range from
   `/api/calls/{id}?profile=P`. *What trivially passes:* testing only
   `/api/profiles`. Three assertions, one test.
2. **The list still works.** Names and the dashboard picker must keep functioning
   for profiles the caller cannot read in detail, or discovery dies silently.
3. **Mutation:** delete the check from `_criterion_detail` alone. If the suite stays
   green, gate 1 is testing one surface and calling it three.
4. For Option C: **the default is `org`**, asserted against a profile created
   through the real endpoint — not against a hand-built row.

---

## 5. Explicitly not in scope

- **Write authorization.** Already correct (`profiles.py:342-343`), already the
  spec's canonical rule (§4.3). Do not touch it.
- **Call Match Plan B.** Its Task 8 gate asserts a `profile_id` **exists and is
  active** — deliberately not an ownership rule, because a decision row is private
  to its author and requiring edit rights would break shared-profile triage. If
  Option B or C lands, Plan B's gate becomes a special case of it and should be
  revisited *then*, not pre-emptively.
- **Anything about `users`.** Account data is a separate surface.

---

## 6. What I could not measure, and why

Everything below needs the live database, whose URL is in `.env` — a file this work
must not read.

- **How many `individual` profiles exist, and what is in them.** This is the whole
  decision. One query:

  ```sql
  SELECT kind, count(*) FROM profiles WHERE is_active GROUP BY kind;
  SELECT count(*) FROM profiles
   WHERE is_active AND kind = 'individual'
     AND (structured->'eligibility'->>'years_since_phd' IS NOT NULL
          OR structured->'eligibility'->>'career_stage' IS NOT NULL);
  ```

- **Whether descriptions contain anything people would not say in a corridor.**
  Not answerable by query; worth asking two or three profile owners.
- **How often `?profile=` is used against a profile the caller has not subscribed
  to.** No access log analysis was done.
