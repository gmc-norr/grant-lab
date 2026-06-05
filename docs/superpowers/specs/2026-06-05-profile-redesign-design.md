# Profile redesign: organizational units, individuals, and eligibility headlines

**Date:** 2026-06-05
**Status:** Design — approved, pending implementation plan
**Supersedes/extends:** profile sections of [2026-04-14-call-monitor-design.md](2026-04-14-call-monitor-design.md)

## Problem

Profiles drive the entire value proposition — calls are ranked against them. But
today all three kinds (`org`/`group`/`user`) share one creation flow that is
shaped for a **research group**:

- The new-profile form ([profile_new.html](../../../src/call_manager/web/templates/profile_new.html))
  asks for a member list (ORCIDs / name+affiliation) and bootstraps from those
  members' PubMed publications.
- `kind` is otherwise **decorative**: it is interpolated as a one-word label into
  the bootstrap and ranking prompts ([prompt.py:162](../../../src/call_manager/ranking/prompt.py))
  and nothing else keys off it — no eligibility logic, no form branching, no
  differentiated bootstrap.

This breaks down at both ends of the spectrum:

- **Organizations are under-served.** "What the organization does" can only land
  in the free-text `description` (1–2 paragraphs) plus `areas[]` tags. There is
  no structure for the things funders actually screen organizations on:
  organizational/legal type, capabilities/infrastructure (biobanks, cohorts,
  clinical-trial units, core facilities), patient populations, scale, or
  networks. The member-publications bootstrap cannot even populate an org. And
  `budget_min/max` + `trl_min/max` are project-shaped fields that fit an
  institution-level unit poorly.
- **Individuals are shoehorned** into a plural "members" flow, and the single
  most eligibility-relevant attribute of a person — **career stage / role** — is
  captured nowhere structured. Career stage and applicant type (solo PI vs
  consortium vs institution) are exactly what large classes of calls (ERC StG/
  CoG/AdG, Marie Curie, junior-investigator awards, consortium-only EU calls)
  gate on.

Net: eligibility — the dimension where org-vs-individual matters most — is left
entirely to an LLM guessing from one word plus prose, with no structured signal.

## Goals

1. Model organizations as **organizational units at any level** (lab, group,
   department, clinic, institution), general → specific.
2. Capture the **eligibility and organizational "headlines"** funders screen on,
   and feed them into ranking.
3. Give each kind a **fitting creation/bootstrap flow** instead of one
   group-shaped form.
4. Stay within the existing single-table, JSONB-`structured` pattern; minimal
   schema churn.

## Non-goals (v1 — explicitly deferred)

- **Parent/child inheritance** of facts between units. We reserve a
  `parent_profile_id` column but implement no resolution/cascade logic.
- **Deterministic eligibility gate.** Call-side eligibility data is only
  LLM-extracted prose today; a hard gate would risk silently dropping real
  matches (a missed funding call is the worst outcome for this tool). Eligibility
  stays an LLM judgement, now enriched with structured profile facts.
- **Call-side structured eligibility extraction** (applicant-type/career-stage
  tags on calls). Prerequisite for any future hard gate; out of scope here.
- **Per-profile access control.** Unchanged from current (owner-or-admin edit).

## Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Org granularity | Any organizational unit at any level | User's domain: lab → group → dept → clinic → institution |
| Hierarchy | Flat now; reserve `parent_profile_id` | YAGNI for 5–15 profiles; avoid migration churn later |
| Eligibility effect | Enrich the LLM; **no hard gate** | Call-side data unreliable; false hard-excludes = missed funding |
| Field storage | JSONB-first (Approach A) | Fields are fed to the LLM, not queried/filtered; matches existing pattern |
| `kind` taxonomy | Two kinds: `organizational_unit` + `individual` | "group" is just one *level* of unit; removes org-vs-group ambiguity |

## Data model

Single `profiles` table retained. Changes:

- **`kind`**: convert from the PostgreSQL enum (`profile_kind`) to `Text` +
  `CheckConstraint('organizational_unit','individual')`. This makes the value
  change a clean data migration and aligns with the newer Text+Check convention
  used by `Workspace.status`, `TurnJob.status`, etc. (the model review flagged the
  enum-vs-text inconsistency).
- **`level`**: `Text` nullable, `CheckConstraint('lab','group','department','clinic','institution','other')`.
  Set for `organizational_unit`; `NULL` for `individual`.
- **`parent_profile_id`**: `BigInteger` nullable, `ForeignKey('profiles.id', ondelete='SET NULL')`,
  indexed. Reserved for future hierarchy; **no v1 logic reads it**.
- **`structured`** (JSONB): gains the namespaced keys below. Existing keys
  (`areas`, `budget_min/max`, `trl_min/max`, `must_include`, `hard_exclusions`)
  are unchanged.
- `budget_currency`, `embedding`, `embedding_model`, `is_active`, `created_by`,
  timestamps: unchanged.

### `structured` field catalog

Existing (kept):

```
areas: text[]
budget_min, budget_max: int | null
trl_min, trl_max: int(1-9) | null
must_include: text[]
hard_exclusions: text[]          # topic exclusions (existing semantics)
```

New (all optional; read via `.get()` with defaults so existing rows keep working):

```
eligibility:
  applicant_types: text[]        # solo_pi | consortium_coordinator | consortium_partner | institution
  career_stage:    text | null   # phd_student | postdoc | junior_pi | senior_pi | group_leader   (individuals)
  role:            text | null   # free text, optional
  years_since_phd: int  | null   # optional
org:
  org_type:           text | null   # research_organisation | university_hospital | public_body | sme | other
  capabilities:       text[]        # biobank, clinical_trials_unit, imaging_core, registry, cohort, ...
  patient_populations:text[]        # free tags
  scale:              text | null   # free text, e.g. "~400 researchers"
funding:
  instruments:       text[]         # project_grant | fellowship | mobility | infrastructure | programme
  funder_exclusions: text[]         # e.g. "defence", "industry-only"
geo:
  country:           text | null    # ISO-ish, e.g. "SE"
  host_institution:  text | null    # free text
```

Validation lives in the route layer (as today), not the DB — enums above are
validated against allow-lists in `profiles.py`; free-text/tag lists are bounded
by the existing `_MAX_LIST_ENTRIES` / length caps.

## Creation + bootstrap (kind-branched)

`SeedInput` ([seed.py](../../../src/call_manager/profiles/seed.py)) gains `level`
and the new structured seed inputs; `kind` values become the two-kind set.

**Step 0 — choose kind** (individual vs organizational unit).

**Individual:**
- Form: name, self ORCID (single), `career_stage`/`role`, `host_institution`,
  optional pasted grants.
- Bootstrap: PubMed-of-self (by ORCID) + the entered career stage → LLM drafts
  description and suggests `areas`, `funding.instruments`,
  `eligibility.applicant_types`.

**Organizational unit:**
- Form: name, `level`, `org_type`, optional members (for small labs/groups),
  plus a **mission / strategy / capabilities** free-text seed, optional pasted
  grants.
- Bootstrap: feed the mission/capabilities text (+ member publications **only if
  members were provided**) → LLM drafts description and suggests `org.*`,
  `areas`, `funding.instruments`. Member-publications bootstrap is no longer
  required for an org to be useful.

**Bootstrap prompt** ([bootstrap_prompt.py](../../../src/call_manager/profiles/bootstrap_prompt.py))
becomes kind-aware: different instructions and a different requested-field set per
kind. `BOOTSTRAP_PROMPT_VERSION` bumped.

## Edit form (kind-aware rendering)

[profile_edit.html](../../../src/call_manager/web/templates/profile_edit.html)
renders sections conditionally:

- **Individuals:** career-stage/role, host, years-since-PhD; areas; funding;
  exclusions; budget/TRL prominent.
- **Organizational units:** level, org_type, capabilities, patient_populations,
  scale; areas; funding; exclusions; budget/TRL collapsed-optional for
  higher-level units (institution/clinic), prominent for lab/group.

The POST handler ([profiles.py](../../../src/call_manager/web/routes/profiles.py))
parses and validates the new fields into the namespaced `structured` shape.

## Ranking integration

[prompt.py](../../../src/call_manager/ranking/prompt.py): the PROFILE block gains
a kind-aware eligibility/org/funding/geo section. Criterion assignment is
explicit:

- **`eligibility`** criterion instruction is extended to weigh
  `eligibility.applicant_types` / `eligibility.career_stage` /
  `org.org_type` / `geo.country` / `geo.host_institution` against the call's
  stated eligibility.
- **`funding.funder_exclusions`** feeds the **`eligibility`** criterion as
  context ("the applicant will not pursue these funders/instruments") — it is a
  funder/instrument-fit concern, **not** a topic concern, so it does **not** go to
  the topic `exclusions` criterion (which stays bound to `hard_exclusions` and
  hard-scores topic matches ≤10). `funding.instruments` and `org.capabilities` /
  `org.patient_populations` are added as context to the `topic_fit` /
  `eligibility` reasoning.
- **No new gate** — the existing LLM `eligibility` rating (pass/unclear/fail)
  already flows into the score.

### New fields are prompt-only; the embedding is unchanged (deliberate)

`build_profile_text` ([similarity.py](../../../src/call_manager/embeddings/similarity.py))
stays `description + areas`. The new fields are **prompt-only** and do **not**
enter the embedding vector. Consequence to accept explicitly: the embedding
pre-gate ([rank_pair.py](../../../src/call_manager/ranking/rank_pair.py)) can
short-circuit a pair to FAIL *before* the LLM runs, so the new fields cannot
rescue a call the embedding gate hard-fails.

- This is correct for the eligibility/funding/geo fields — they are **not
  topical**, so they should not move a topic-similarity vector.
- It is a **known limitation** for the two topical org fields
  (`org.capabilities`, `org.patient_populations`): a relevant call could be
  embedding-hard-failed before the LLM sees those capabilities. The hard-fail
  band is narrow (cosine `< 0.20`; the `[0.20, 0.30)` UNKNOWN band already
  reaches the LLM), and `must_include` remains the escape hatch, so we accept
  this for v1 rather than expand the embedding input and its invalidation logic.
  A follow-up may add `org.capabilities` to `build_profile_text` **with** the
  matching edit-time re-embed trigger; out of scope here.

### Re-ranking after this change

There are two distinct triggers, and they behave differently:

- **Editing a profile** already re-ranks: the edit handler always schedules
  `backfill_profile` ([profiles.py](../../../src/call_manager/web/routes/profiles.py)),
  which enqueues jobs for every call in the window (its `ON CONFLICT` guards only
  *active jobs*, not existing `call_rankings`), so existing rows are re-scored and
  the LLM sees the new fields. This holds even when only the new (non-description)
  fields change — the re-embed is skipped (keyed off `description`), but the
  backfill re-rank still runs.
- **A code-only prompt-template change** (this redesign bumps `prompt_version`)
  does **not** auto-rerank. `prompt_version` is a debug/provenance label;
  `reconcile_missing_rankings` only fills *missing* pairs, and feed/digest do not
  filter by version. **Deploy step (required):** after deploying, trigger a full
  re-rank via `POST /admin/rerank`
  ([admin.py](../../../src/call_manager/web/routes/admin.py)), or a one-off
  `DELETE FROM call_rankings` followed by the reconciler. This must be called out
  in the rollout runbook, not assumed.

## Migration & backward compatibility

One Alembic migration.

**Upgrade:**

1. Convert `kind` to Text+Check; data map: `org`→`organizational_unit`,
   `group`→`organizational_unit`, `user`→`individual`. Drop the `profile_kind`
   enum type once the column no longer uses it.
2. Add `level` (set `'group'` for rows migrated from `group`; `NULL` for rows
   migrated from `org` — the owner picks the real level on next edit).
3. Add `parent_profile_id` (FK, indexed) — nullable, unused by v1 logic.
4. New `structured` keys need no migration — absent keys default via `.get()`.

**Downgrade (explicitly lossy):** `organizational_unit`→`group`,
`individual`→`user`; revert `kind` to the `profile_kind` enum; drop `level`,
`parent_profile_id`, and the new `structured.*` namespaces. This is **not a
faithful inverse** — the original `org` vs `group` distinction is unrecoverable
(both collapsed to `organizational_unit` on upgrade), and `level` /
`parent_profile_id` / new fields are dropped. The round-trip test asserts the
*schema* round-trips and that downgrade runs cleanly, **not** that pre-upgrade
`kind` values are restored. Treat downgrade as a break-glass schema rollback,
not a data-preserving operation.

**Rollout:** after deploy, run the explicit re-rank step (see "Re-ranking after
this change" above) — the `prompt_version` bump does not re-rank on its own.

Existing profiles remain fully functional throughout: untouched `structured`
keys, owner sets `level`/new fields at leisure.

## Components touched

- `db/models.py` — `Profile` columns + checks. The DB `kind` column becomes
  Text+Check; the Python `ProfileKind` is kept as a `StrEnum` but with the two
  new members (`ORGANIZATIONAL_UNIT`, `INDIVIDUAL`) for type-safe app-side use,
  and a parallel `ProfileLevel` StrEnum for `level`. The old `profile_kind`
  PostgreSQL enum *type* is dropped after the column is converted.
- `alembic/versions/` — one migration (above).
- `profiles/seed.py` — `SeedInput`/`DraftProfile` shape, two-kind `kind`.
- `profiles/bootstrap_prompt.py` + `profiles/bootstrap.py` — kind-aware drafting.
- `web/routes/profiles.py` — kind-branched create, kind-aware edit, validation.
- `web/templates/profile_new.html` + `profile_edit.html` + `profile_detail.html`
  — kind-branched rendering.
- `ranking/prompt.py` — PROFILE block + eligibility instruction; `prompt_version`.

Each unit keeps a single clear purpose; the `structured` shape is the contract
between bootstrap, the edit form, and the ranking prompt.

## Testing

- Migration: upgrade `kind`/`level` data mapping (`org`/`group`→unit,
  `user`→individual; `group`→`level='group'`, `org`→`level=NULL`). Downgrade runs
  cleanly and the schema round-trips; the test does **not** assert pre-upgrade
  `kind` values are restored (downgrade is documented-lossy).
- Kind-branched bootstrap: individual seeds from ORCID; org seeds from
  mission text with no members; field suggestions match kind.
- Validation: new enum allow-lists reject garbage; list caps enforced.
- Per-kind form rendering (individual vs unit sections present/absent).
- Ranking prompt: includes the new fields per kind; `funder_exclusions` appears
  in the eligibility-criterion context, not the topic-`exclusions` criterion.
- **Re-rank on edit:** editing *only* the new (non-description) fields schedules a
  `backfill_profile` re-rank (so the LLM re-evaluates) and does **not** schedule a
  re-embed.
- **Embedding unchanged:** `build_profile_text` output is unaffected by the new
  fields (still `description + areas`).
- Backward-compat: a pre-migration profile (old `structured`, no new keys) ranks
  without error.

## Open questions

None outstanding — all design decisions resolved during brainstorming.
