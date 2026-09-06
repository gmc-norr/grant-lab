# Matching freshness: telling "exists" from "current"

**Date:** 2026-09-06
**Status:** design, approved for planning
**Origin:** finding 2 of `docs/superpowers/reviews/2026-09-05-runtime-ownership-and-state-consistency-review.md`
**Base:** `master` at the Plan C merge (`76c70a47`)

## The problem

A profile's embedding and its rankings are derived from the profile's text. When
that text changes, both become wrong. Nothing in the system can currently detect
that, because nothing records *which* version of the profile a stored vector or a
stored ranking was built from.

There are three defects, and the third is what makes the first two permanent.

### Hole 1 — an areas-only edit schedules nothing

`src/grant_lab/web/api/profiles.py:524`:

```python
if body.description.strip():
    background.add_task(post_edit.refresh_and_backfill_in_background, ...)
```

The refresh and the ranking backfill are both gated on the *description* being
non-empty. But `build_profile_text` (`src/grant_lab/embeddings/similarity.py:59`)
composes its output from three segments — description, `structured["areas"]`, and
the org fields — and joins only the non-empty ones:

```python
parts = [p for p in (description, areas_text, org_text) if p]
return "\n\n".join(parts)
```

So a profile with an empty description and non-empty `areas` has real embedding
text. Editing such a profile — including *clearing* the description while adding
areas — schedules no refresh and no backfill. The endpoint even computes
`embed_text_changed` correctly two lines earlier and then discards it.

### Hole 2 — the obligation is not durable

The refresh is a FastAPI `BackgroundTask` registered *after* `session.commit()`.
A crash, restart or deploy between the commit and the task running loses the
request with no trace. `refresh_and_backfill_in_background` additionally swallows
every exception by design (`post_edit.py:19`, `:41`), so a failure is a log line
and nothing else.

### Hole 3 — no repair path can see staleness

Both repair predicates ask about *existence*, not *currency*.

Embedding repair, `src/grant_lab/embeddings/refresh.py:322`:

```python
or_(Profile.embedding.is_(None), Profile.embedding_model != current_model)
```

Ranking reconcile, `src/grant_lab/ranking/queue.py:153`:

```sql
AND NOT EXISTS (
  SELECT 1 FROM call_rankings cr
  WHERE cr.call_id = c.id AND cr.profile_id = p.id
)
```

A stale-but-present vector matches the current model. A stale-but-present ranking
exists. Both are therefore invisible to the daily repair forever.

Fixing hole 3 makes holes 1 and 2 self-healing: whatever the request path fails to
schedule, the daily tick detects and repairs. Holes 1 and 2 are still fixed here,
because self-healing within a day is not the same as correct within a second.

## The mechanism

Two fingerprints, **derived at check time from the row itself** — never stored as
a claim about "current".

This is the load-bearing choice. The alternative — maintaining a
`profiles.embed_text_hash` column inside every edit transaction — has three write
sites today (`profiles/create.py:25`, `profiles/create.py:51`, and the pair
`web/api/profiles.py:481` + `:518`). A fourth writer that forgets to update it
produces a hash asserting "fresh" about a row that is not. That is a gate that is
green while the property it names is false, which is the failure shape this
codebase has hit repeatedly. A derived fingerprint cannot rot: it is recomputed
from the source of truth every time it is compared.

### `embedding_fingerprint(profile)`

`sha256` hex of `build_profile_text(profile)`.

Hashing the **output text**, not the input fields, is deliberate. The docstring of
`build_profile_text` already states that changing the function changes every
profile's vector, and that callers "must re-embed profiles for the new text to
take effect" — a manual step performed by hand in Task 31. Hashing the output
makes that automatic: edit the function, every fingerprint changes, the daily tick
re-embeds.

### `rank_fingerprint(profile)`

`sha256` hex of a canonical JSON serialization of every profile field the ranking
pipeline reads.

Measured by grep over `src/grant_lab/ranking/`, those are:

| Field | Read at |
|---|---|
| `description` | prompt + criteria |
| `structured` (areas, org, eligibility, funding, geo, must_include, hard_exclusions) | prompt + criteria gates |
| `budget_currency` | `ranking/criteria.py:115` |
| `name` | `ranking/prompt.py:218` |
| `kind` | `ranking/prompt.py:219` |

`name` and `kind` are in the LLM prompt, so a rename genuinely changes the input
and genuinely invalidates the ranking. They are included. The cost of that
decision is bounded by machinery that already exists: ranking runs on the local
GPU, and the paid Anthropic escalation is capped per day (`remote_daily_cap`,
enforced at `ranking/worker.py:166`).

The embedding inputs are a strict subset of the ranking inputs, so
`rank_fingerprint` changing is implied by `embedding_fingerprint` changing. The
converse does not hold, which is why there are two functions rather than one.

Canonical form: `json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`.

### Two new columns

| Column | Meaning |
|---|---|
| `profiles.embedding_fingerprint TEXT NULL` | the text the stored `embedding` describes |
| `call_rankings.profile_fingerprint TEXT NULL` | the profile the ranking describes |

Migration `0075` (latest on master is `0074_boards_settings_bounds`). Additive,
nullable, **no backfill**. `NULL` means "unknown", and that is honest: for rows
written before this change we genuinely do not know.

## The JSONB round-trip hazard

`structured` is a JSONB column. Postgres does not preserve key order, discards
duplicate keys, and normalizes numeric representation. A fingerprint that is not
stable across a save/load round-trip would mark **every** profile permanently
stale and re-rank the whole corpus on every daily tick — a worse failure than the
one being fixed, and one that would present as unexplained GPU load rather than as
an error.

Two rules follow, and both are pinned by tests:

1. **`sort_keys=True` is load-bearing**, not tidiness.
2. **Every fingerprint that is stored or compared is computed from a
   database-loaded `Profile`.** All three sites already satisfy this:
   `refresh_profile_embedding` does `session.get(Profile, profile_id)`, the
   ranking worker loads the profile from its session, and reconcile will load
   active profiles. The one in-memory use is the update endpoint's
   schedule-or-not decision, which compares two same-representation values and
   stores neither.

## Changes

### 1. New module `src/grant_lab/profiles/fingerprint.py`

`embedding_fingerprint(profile) -> str` and `rank_fingerprint(profile) -> str`.
No I/O, no session, no imports from `web/` or `ranking/`. It may import
`build_profile_text` from `embeddings.similarity`.

### 2. Migration `0075_freshness_fingerprints.py`

Adds the two nullable columns. Downgrade drops them.

### 3. Write site — embeddings, `src/grant_lab/embeddings/refresh.py`

In `refresh_profile_embedding`, the fingerprint travels with the vector and the
model in all three existing branches:

| Branch | Line today | `embedding` | `embedding_model` | `embedding_fingerprint` |
|---|---|---|---|---|
| text is blank → invalidate | `:114` | `None` | `None` | `None` |
| `LLMError` and `force=True` | `:135` | `None` | `None` | `None` |
| success | `:138-139` | vector | model | `embedding_fingerprint(profile)` |

The `LLMError` / `force=False` path leaves all three untouched, which is the
existing behaviour: the old vector is preserved and the fingerprint keeps
describing it truthfully. That row is now detectably stale, which is the point —
the review's "normal failure case" (an embedding outage preserving a previous
vector with no record that it is out of date) is closed by this row, not by a new
branch.

### 4. Write site — rankings, `src/grant_lab/ranking/worker.py`

Capture the fingerprint **before** the LLM call, at `:169` where `profile` is
handed to `rank_pair`, into a local. Write that local into the `CallRanking` at
`:245`.

Hashing at write time instead would be a real defect: `rank_pair` performs the
LLM call and can take many seconds, and an edit landing inside that window would
be stamped as already-ranked. The ranking would then describe text the ranker
never saw, and would be invisible to the repair — the exact class of bug this
spec exists to remove.

### 5. Repair arm — embedding tick, `src/grant_lab/embeddings/refresh.py`

The tick cannot evaluate the fingerprint in SQL, because `build_profile_text` is
Python over JSONB. The profile phase therefore moves its staleness decision into
Python, where the existing blank-text filter at `:328` already lives:

```python
stale = (
    p.embedding is None
    or p.embedding_model != current_model
    or p.embedding_fingerprint != embedding_fingerprint(p)
)
```

Two constraints on the implementation:

- **Do not load the vector.** Select only `id`, `description`, `structured`,
  `embedding_model`, `embedding_fingerprint`, and a computed
  `embedding IS NULL`. Parsing 1024-dim vectors is the measured dominant cost of
  profile loads in this codebase; loading them to decide whether to replace them
  would be a regression.
- **Preserve termination.** `after_profile_id` / `max_profile_id` and the
  "paged past the end of the embeddable set" cursor advance at `:329-333` exist
  so the CLI's "no progress ⇒ break" loop terminates. Widening the candidate set
  must not break that, and the force-mode path must keep behaving as it does now.

### 6. Repair arm — ranking reconcile, `src/grant_lab/ranking/queue.py`

Compute `{profile_id: rank_fingerprint(p)}` in Python for active profiles, pass it
as a `VALUES` list, and require the stored ranking's fingerprint to match:

```sql
AND NOT EXISTS (
  SELECT 1 FROM call_rankings cr
  WHERE cr.call_id = c.id AND cr.profile_id = p.id
    AND (cr.profile_fingerprint IS NULL
         OR cr.profile_fingerprint = want.fingerprint)
)
```

`cr.profile_fingerprint IS NULL` is the **grandfather clause**. Without it, every
ranking written before migration `0075` is stale on the first tick after deploy,
and the whole 90-day window is enqueued at once. With it, the deploy is inert and
only edits made from that point mark anything stale.

Nothing new is needed to force a full re-rank: `POST /api/admin/rerank`
(`web/api/admin.py:381`) already calls `backfill_profile` for every active
profile, and `backfill_profile` has no `NOT EXISTS call_rankings` guard, so it
re-enqueues regardless of existing rankings. The SPA already exposes it with a
confirmation dialog and a `rerank_estimate`
(`frontend/src/routes/AdminDashboard.tsx:54`).

The existing dead-letter suppression is unchanged and still applies, so a pair
that is stale *and* terminally failed does not re-enqueue every tick.

### 7. Hole 1 — `src/grant_lab/web/api/profiles.py:524`

Replace the description guard with a fingerprint comparison. The endpoint already
computes `old_embed_text` at `:480` and `new_embed_text` at `:520` for exactly
this shape; add `old_rank_fp` and `new_rank_fp` beside them and schedule when the
rank fingerprint changed.

Because the embedding inputs are a subset of the ranking inputs, this schedules a
superset of what is scheduled today, and never less. A PUT that changes nothing
schedules nothing — which is an improvement, since today such a PUT enqueues 90
days of ranking jobs whenever the description happens to be non-empty.

## Out of scope

- **`model` / `prompt_version` drift.** `call_rankings` already stores both, and
  reconcile ignores both. A ranking produced by an older prompt version is
  invisible to repair for exactly the same reason a stale one is. Same shape,
  separate finding, not fixed here.
- **UI staleness marker.** No "may be out of date" badge. The repair replaces the
  row; surfacing the interim state is a product decision, not a correctness one.
- **Review finding 1** (lock loss leaves `/healthz` at HTTP 200) and **finding 2**
  (Git/Postgres rollback window). Untouched.
- **Call-side freshness.** `refresh_call_embedding` has the same
  exists-vs-current gap. Calls are ingested and rarely edited, so the exposure is
  much smaller. Deliberately deferred, not overlooked.

## Testing

Every gate is proved by mutation: revert the fix, confirm the named test fails,
and confirm it fails for the stated reason and not because of an import error.

| # | Gate | Mutation that must redden it |
|---|---|---|
| 1 | Fingerprint is stable across a JSONB save/load round-trip, and across two consecutive loads | drop `sort_keys=True` |
| 2 | An areas-only edit (empty description) schedules refresh + backfill | restore `if body.description.strip():` |
| 3 | A PUT that changes nothing schedules nothing | schedule unconditionally |
| 4 | The embedding tick re-embeds a profile whose text changed but whose vector and model are both present and current | drop the fingerprint arm from the Python filter |
| 5 | The embedding tick still terminates, and force mode is unchanged | — (regression pin on existing behaviour) |
| 6 | The tick does not load the embedding column when deciding | assert on the emitted SQL / deferred attribute |
| 7 | Reconcile re-enqueues a pair whose ranking fingerprint differs | drop the fingerprint condition |
| 8 | Reconcile does **not** re-enqueue a pair whose ranking fingerprint is `NULL` | remove the `IS NULL` arm |
| 9 | The worker stamps the fingerprint the ranker actually saw | move the hash to write time; a test that edits the profile during `rank_pair` must fail |
| 10 | A `force=True` embedding failure NULLs the fingerprint with the vector and model | leave the fingerprint set |
| 11 | A `force=False` embedding failure leaves all three untouched | NULL the fingerprint |

Gate 1 is the one that matters most. Without it, a round-trip instability makes
every profile permanently stale and the system re-ranks its whole corpus daily.

## Risks

- **Gate 1 failing silently in production but not in tests**, if the test fixture
  round-trips `structured` differently from the live path. The test must go
  through a real session commit and a fresh `session.get`, not a constructed
  object.
- **Reconcile's `VALUES` list growing.** It is one row per active profile. If the
  profile count ever reaches thousands, the join should be revisited; at the
  current scale it is not a concern.
- **The embedding tick's cursor logic** is the most intricate existing code being
  touched. The pre-existing comment at `:330-332` documents an infinite-loop bug
  already fixed there once.
