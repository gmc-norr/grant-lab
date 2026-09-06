# Matching freshness: telling "exists" from "current"

**Date:** 2026-09-06
**Status:** rev 3 — design, ready for planning
**Origin:** finding 2 of `docs/superpowers/reviews/2026-09-05-runtime-ownership-and-state-consistency-review.md`
**Base:** `master` at the Plan C merge (`76c70a47`)

## Revision note

Rev 1 was reviewed by three outside models. **All 19 findings were confirmed
against the source; none were refuted.** Rev 1's central repair mechanism did not
work: widening the embedding tick's candidate set would have made the daily drain
stop at the first page containing nothing stale, so the repair could never reach a
stale profile with a higher id. Three models found that independently.

Rev 1 also asserted things about this codebase that are false. A profile **cannot
be renamed** — `ProfileUpdateIn` has 22 fields and `name` is not one of them — so
rev 1's paragraph weighing the cost of re-ranking on rename was defending a
scenario that cannot occur. Rev 1 claimed the endpoint "discards" `embed_text_changed`;
it does not, it passes it into the task and uses it there. Rev 1 counted three
profile-text write sites; there are four.

**Rev 3** applies a second review (one model; the other died in a server-error
retry loop after reading the package, and was killed rather than waited on). Seven
of its eight findings were confirmed against the source. The worst: rev 2's
deferral would have **rolled back**, because `queue.revert_to_pending` does not
commit and every other early exit in the worker does — the job would have stayed
RUNNING, and the stuck-reaper would have burned the very retry the deferral exists
to protect. Rev 2 also forgot the ORM mappings entirely, and its `'legacy'` fix
closed only half the hole it was written for.

One finding was **partly refuted by running the code**: rev 2 called the embedding
tick and ranking reconcile "two independent scheduled jobs". They are ordered on
purpose — 3:30 and 4:00, with a comment at `scheduler.py:514-516` saying so. The
reviewer flagged this as unverifiable from its package and was right to. Rev 3
states the real ordering and the real gap.

Everything below is rev 3.

## The problem

A profile's embedding and its rankings are derived from the profile's content.
When that content changes, both become wrong. Nothing in the system can currently
detect that, because nothing records *which* version of the profile a stored
vector or a stored ranking was built from.

There are three defects, and the third is what makes the first two permanent.

### Hole 1 — an areas-only edit schedules nothing

`src/grant_lab/web/api/profiles.py:524`:

```python
if body.description.strip():
    background.add_task(post_edit.refresh_and_backfill_in_background, ...)
```

Refresh and ranking backfill are both gated on the *description* being non-empty.
But `build_profile_text` (`src/grant_lab/embeddings/similarity.py:59`) composes its
output from three segments — description, `structured["areas"]`, and the org
fields — and joins only the non-empty ones:

```python
parts = [p for p in (description, areas_text, org_text) if p]
return "\n\n".join(parts)
```

A profile with an empty description and non-empty `areas` therefore has real
embedding text. Editing such a profile — including *clearing* the description
while adding areas — schedules no refresh and no backfill. The endpoint computes
`embed_text_changed` correctly at `:521` and uses it *inside* the task at `:530`,
but the decision to schedule the task at all is still made from `body.description`.

### Hole 2 — the obligation is not durable

The refresh is a FastAPI `BackgroundTask` registered *after* `session.commit()`.
A crash, restart or deploy between the commit and the task running loses the
request with no trace. `refresh_and_backfill_in_background` additionally swallows
every exception by design (`post_edit.py:19`, `:41`), so a failure is a log line
and nothing else.

**This spec does not fix hole 2.** It is closed only in the sense that hole 3's
daily repair will eventually detect and correct whatever the request path lost.
A transactional outbox is deliberately out of scope (see "Out of scope"). The
honest promise of this design is *correct within a day* in the normal case — and
up to two daily cycles when the 03:30/04:00 tick ordering slips (see "The
vector-ordering problem") — not *correct within a second*.

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
exists. Both are invisible to the daily repair, forever.

### Hole 4 — a cleared profile can never be repaired (found in review)

This is an existing defect in shipped code, not a consequence of the three above.

`refresh.py:328` selects only profiles with non-blank text:

```python
profile_ids = [p.id for p in profiles if build_profile_text(p).strip()]
```

`refresh_profile_embedding` has a branch that wipes the vector when the text is
blank (`:109-120`) — but the tick excludes exactly the rows that need it. So a
profile whose description, areas and org fields are all cleared keeps its previous
vector, and the *only* path that can clear it is the request path from hole 2.
Lose that task and the profile is gated against future calls by a vector
describing content it no longer has.

## The mechanism

Two fingerprints, **derived at check time from the row itself** — never stored as
a claim about "current".

This is the load-bearing choice. The alternative — maintaining a
`profiles.embed_text_hash` column inside every edit transaction — has **four**
write sites today (`profiles/create.py:25`, `:51`, the constructor at `:68-75`,
and the pair `web/api/profiles.py:481` + `:518`). A fifth writer that forgets to
update it produces a hash asserting "fresh" about a row that is not: a gate that
is green while the property it names is false, which is the failure shape this
codebase has hit repeatedly. A derived fingerprint cannot rot.

### Both fingerprints carry a version tag

Every fingerprint is `"v1:" + sha256(...).hexdigest()`.

The tag is not decoration. Any later change to what goes into the hash — adding a
field, changing `separators`, renaming a key — makes **every** stored fingerprint
mismatch at once, and the next tick re-ranks the entire corpus with no operator
signal. That is "unexplained GPU load rather than an error", the exact failure
mode this design exists to prevent, arriving by a different door.

The rule that goes with the tag: **a change to a hashed payload must bump the
version and ship with an explicit decision** — either "yes, re-rank everything",
or a skip rule for the old version. The version tag makes that a visible, deliberate
act instead of a silent one.

### `embedding_fingerprint(profile)`

`v1:` + `sha256` of `build_profile_text(profile)`.

Hashing the **output text**, not the input fields, is deliberate.
`build_profile_text`'s own docstring states that changing the function changes
every profile's vector and that callers "must re-embed profiles for the new text
to take effect" — a manual step performed by hand in Task 31. Hashing the output
makes it automatic.

### `rank_fingerprint(profile)`

`v1:` + `sha256` of a canonical JSON serialization of:

| Key | Source |
|---|---|
| `description` | `profile.description` |
| `structured` | `profile.structured` — **the whole blob, not an enumerated subset** |
| `budget_currency` | `profile.budget_currency` |
| `name` | `profile.name` |
| `kind` | `profile.kind` |
| `embedding_fingerprint` | `profile.embedding_fingerprint` (the stored one) |

**The whole `structured` blob, deliberately.** Rev 1 enumerated the keys the
ranker reads and got it wrong: it omitted `budget_min`, `budget_max`, `trl_min`
and `trl_max`, which `ranking/criteria.py:111` and `:129` read straight off
`profile.structured`. An enumeration is a list that must be maintained in step
with a pipeline it does not live next to. It rots, and when it rots the symptom is
a ranking that is stale forever. Hashing the blob costs nothing and cannot rot.

**`name` and `kind` are insurance, not cost.** They reach the LLM prompt at
`ranking/prompt.py:218-219`, so they are genuine inputs. But no endpoint can
change them: `ProfileUpdateIn` (`web/api/profiles.py:399-423`) has 22 fields and
neither is among them, and nothing in the codebase assigns `Profile.name` or
`Profile.kind` after `profiles/create.py:68-75`. Including them is free and
guards a future rename endpoint.

**`embedding_fingerprint` is in the payload, and this is subtle** — see the next
section.

Canonical form: `json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, default=str)`, over a payload whose numbers are canonicalized
(integral floats reduced to `int`) and whose empty values are omitted.

## The vector-ordering problem, and why two decisions compose

The ranker does not only read text. It reads the profile's **stored vector**:
`worker.py:174-175` threads the cosine thresholds, `:217` stores
`result.embed_similarity`, `:369` writes an `embedding` gate row.
`post_edit.py:33-34` states the requirement outright — *"the ranking jobs must see
the post-edit vector, not the pre-edit one"*.

The request path honours that ordering. **The repair path orders the two ticks
but does not enforce the ordering.** The embedding tick runs at 03:30 and reconcile
at 04:00 (`scheduler.py:479` and `:517`), and the 30-minute gap is deliberate — the
comment at `:514-516` says reconcile runs after embedding refresh "so we catch any
gaps those left behind on the same day rather than a day late".

Thirty minutes is a convention, not a guarantee. The embedding tick drains up to
`_TICK_MAX_BATCHES` batches and can overrun it, and `misfire_grace_time=6*3600`
lets a missed tick run hours late. Meanwhile the ranking worker drains
continuously, on its own interval, tied to neither. So a pair can still be ranked
against a vector that has not caught up, be stamped with the current text
fingerprint, and become permanently invisible to repair.

Two mechanisms close it, and they were chosen separately but reinforce each other:

**1. The worker defers a pair whose vector is stale.** Before ranking, compare
`profile.embedding_fingerprint` against `embedding_fingerprint(profile)`. On a
mismatch, return the job to PENDING via the existing
`queue.revert_to_pending(session, [job_id])` — which explicitly does *not* bump
`attempts`, for exactly this reason ("the jobs were never actually attempted, so
they must not burn a retry") — **and then commit**. The pair is ranked on a later
tick, once the embedding tick has caught up.

`revert_to_pending` (`queue.py:302-318`) issues an `UPDATE` and returns a rowcount;
it does **not** commit. Every other early exit in the worker commits before
`continue` (`worker.py:139-140`, `:149-159`). Without that commit the deferral is
rolled back when the session closes, the job stays RUNNING on its previously
committed claim, and `requeue_stuck_running` picks it up — bumping `attempts` and,
after enough cycles, dead-lettering the pair. The deferral would burn exactly the
retries it was written to protect.

**2. `embedding_fingerprint` is part of the rank payload.** This catches the case
deferral alone cannot: editing the body of `build_profile_text` changes every
embedding fingerprint while leaving every text field identical, so without this
the existing rankings would keep matching and never be revisited.

They compose without cost. Because (1) guarantees a ranking is only ever written
when the vector is already settled, (2) never causes a second re-rank round — the
embedding fingerprint in the payload is already its final value at the moment the
ranking is written. Including it in the payload *without* deferral would have
re-ranked every profile's 90-day window twice per edit.

**Deferral must be bounded.** If the embedder is down, `force=False` preserves the
old vector rather than NULLing it, so the mismatch persists and the pair would
defer forever. Bound it on job age: if `RankingJob.created_at` is older than
`_STALE_VECTOR_MAX_DEFER_HOURS` (24h, comfortably more than one embedding-tick
cycle), rank anyway and stamp the fingerprint that was actually used. The pair is
then correctly marked stale and re-ranked once the vector recovers. A missing
vector (`embedding IS NULL`) is **not** a mismatch and never defers — that is
today's UNKNOWN-cosine path and it keeps working unchanged.

## The JSONB round-trip hazard

`structured` is JSONB. Postgres does not preserve insertion order across a
round-trip, discards duplicate keys, and normalizes numeric representation. A
fingerprint unstable across save/load would mark **every** profile permanently
stale and re-rank the whole corpus on every tick.

Rev 1 named this hazard and then wrote a gate that cannot catch it. Two
corrections:

- **"Two consecutive loads produce the same hash" is a property of Postgres, not
  of this code.** jsonb emits object keys in a fixed order for a given row, so
  that comparison stays green whether or not `sort_keys=True`. It proves nothing.
- **The comparison that actually happens in production is in-memory-versus-loaded.**
  `web/api/profiles.py:480` hashes a row as loaded from Postgres; `:518` replaces
  `structured` with a freshly built Python dict and `:520` hashes that. Rev 1
  claimed these were "the same representation". They are not. Key insertion order,
  `1` versus `1.0`, `None` versus an absent key, and float repr (`1e+16` versus
  `10000000000000000`) can all differ with no user change — and change 7 then
  schedules a 90-day backfill on **every single save**.

So the canonical form must normalize numbers and omit empties, not merely sort
keys — and gate 1 must be the round-trip comparison, not the two-loads one.

## Changes

### 1. New module `src/grant_lab/profiles/fingerprint.py`

`embedding_fingerprint(profile) -> str` and `rank_fingerprint(profile) -> str`,
plus the private canonicalizer. No I/O, no session. May import
`build_profile_text` from `embeddings.similarity`.

### 2. Migration `0075_freshness_fingerprints.py` **and the ORM mappings**

Both columns need a mapping as well as a migration. Rev 2 listed only the
migration, which would not have run: `Profile` (`db/models/profiles.py`) has no
`embedding_fingerprint` and `CallRanking` (`db/models/ranking.py`) has no
`profile_fingerprint`, and every read and write below goes through the ORM. Add
`Mapped[str | None] = mapped_column(Text, nullable=True)` to each, and regenerate
`docs/ARCHITECTURE.md` in the same commit.

The migration itself:

- `profiles.embedding_fingerprint TEXT NULL`
- `call_rankings.profile_fingerprint TEXT NULL`
- `UPDATE call_rankings SET profile_fingerprint = 'legacy'` — one statement,
  no Python, no LLM call.

The `'legacy'` sentinel is what bounds the grandfather clause. Rev 1 used
`IS NULL`, which is open-ended: a NULL written *after* deploy would be exempt
forever, so a pre-0075 pair whose profile is later edited would stay invisible to
the very repair this spec adds. With the sentinel, `'legacy'` means "written
before we tracked this, assumed current", and NULL afterwards means a **bug** and
is treated as stale. The version prefix keeps the two vocabularies apart: real
fingerprints always start `v1:`.

If `call_rankings` is large enough that a single `UPDATE` is an unacceptable lock,
batch it by primary key. Measure the row count before deciding.

`profiles.embedding_fingerprint` is deliberately **not** stamped. NULL there means
stale, and every profile is re-embedded on the first tick after deploy. That is a
deliberate asymmetry with the ranking side, and it is justified by cost: a full
re-embed is bounded by the profile count (tens) against a local Ollama, seconds of
work. A full re-rank is profile count multiplied by 90 days of calls.

### 3. Write site — embeddings, `src/grant_lab/embeddings/refresh.py`

In `refresh_profile_embedding`, the fingerprint travels with the vector and model
in every branch:

| Branch | Line today | `embedding` | `embedding_model` | `embedding_fingerprint` |
|---|---|---|---|---|
| text blank → invalidate | `:114` | `None` | `None` | `None` |
| `LLMError`, `force=True` | `:135` | `None` | `None` | `None` |
| `LLMError`, `force=False` | — | untouched | untouched | **untouched** |
| success | `:138-139` | vector | model | `embedding_fingerprint(profile)` |

The `force=False` failure path is the review's "normal failure case": the old
vector is preserved and the fingerprint keeps describing it *truthfully*, so the
row is now detectably stale. That is closed by this row, not by a new branch.

### 4. Write site — rankings, `src/grant_lab/ranking/worker.py`

Capture `rank_fingerprint(profile)` into a local at `:169`, where `profile` is
handed to `rank_pair`, and write that local into the `CallRanking` at `:245`.

**The rationale is robustness, not a live bug.** Rev 1 claimed a concurrent edit
during `rank_pair` would be mis-stamped. That is false in this code: `profile` is
loaded at `:131` and the session is not committed, refreshed or expired before
`:245` — the ledger write at `:203-211` uses a separate session on purpose — so
hashing at either point returns the same value today. Capture early because it is
unambiguous and survives a future refactor that introduces a commit or a
`session.refresh` in between. Gate 9 is written against that real failure, not the
imaginary one.

**Retained analysis must not inherit the new fingerprint.** When a run produces
neither summary nor notes, `worker.py:238-243` copies the previous ranking's
analysis triple. If the profile changed since, that retained advice describes the
old profile. Preserve it only when the previous ranking's `profile_fingerprint`
equals the captured one; otherwise let the triple go to NULL.

### 5. Repair arm — embedding tick, `src/grant_lab/embeddings/refresh.py`

The tick cannot evaluate the fingerprint in SQL, because `build_profile_text` is
Python over JSONB. Three coupled changes:

**(a) Widen the candidate set.** When `force is False`, drop the
`or_(embedding IS NULL, embedding_model != current_model)` narrowing at `:321-323`
and decide in Python. The fingerprint arm applies **only** when `force is False`;
force mode must keep re-embedding every embeddable row as it does now.

**(b) Fix the drain loop — this is the change rev 1 got backwards.** The outer
loop at `:222-229` breaks on `processed == 0`, where `processed` counts rows
*embedded or failed*, and the break happens **before** the cursor is carried
forward:

```python
if processed == 0:
    break
cursor_profile_id = summary["max_profile_id"]   # never reached
```

With a widened candidate set, a page containing nothing stale yields
`processed == 0` and stops the drain, discarding the advanced cursor. Every
subsequent tick restarts at `after_profile_id=0` and halts at the same wall, so a
stale profile past the first page is never repaired. This bites whenever the
profile count exceeds `batch_size` (200).

The fix: the batch returns rows **examined**, not only rows worked, and the drain
breaks on `examined == 0` — a genuinely exhausted scan — while advancing the
cursor across pages that needed no work. Rev 1's "preserve termination" framing
was the wrong worry: the danger is stopping too early, not never stopping.

**The per-tick cap is a second, weaker version of the same trap.**
`_TICK_MAX_BATCHES = 200` (`refresh.py:147`) bounds the drain at `:200`, and a tick
that hits the cap restarts from `after_profile_id=0` tomorrow. Once the scan is
widened, a long prefix of fresh profiles consumes that allowance every day, and a
stale profile beyond it is never reached. At 200 batches x 200 rows the headroom is
40,000 profiles, so at this project's scale (tens) it cannot bite — but the design
must not silently depend on that. Carry the profile cursor across capped ticks and
reset it only on an exhausted scan, and pin it with small `batch_size` and
`max_batches` values rather than a realistic corpus.

**(c) Include blank-but-populated profiles (hole 4).** The `:328` filter must stop
excluding a profile whose text is blank while `embedding_fingerprint` or
`embedding_model` is still set, so the existing invalidation branch can fire from
the tick. A profile that is blank *and* already cleared stays excluded, so the
scan still terminates.

The staleness decision, written against the selected columns:

```python
stale = (
    p.embedding_is_null
    or p.embedding_model != current_model
    or p.embedding_fingerprint != embedding_fingerprint(p)
)
```

**Do not load the vector.** Select `id`, `description`, `structured`,
`embedding_model`, `embedding_fingerprint`, and a computed `embedding IS NULL` as
`embedding_is_null`. Rev 1's snippet read `p.embedding is None`, which would
lazy-load the 1024-dim vector — the exact regression the constraint two lines
below it forbade. Parsing those vectors is the measured dominant cost of profile
loads in this codebase.

### 6. Repair arm — ranking reconcile, `src/grant_lab/ranking/queue.py`

Build `{profile_id: rank_fingerprint(p)}` in Python for active profiles.
**Return 0 immediately when that map is empty** — an empty `VALUES` list is a
syntax error, so a database with no active profiles would otherwise raise on every
daily tick, where today the `CROSS JOIN` degrades to zero rows.

The statement replaces the `CROSS JOIN profiles p` arm rather than sitting beside
it:

```sql
INSERT INTO ranking_jobs (call_id, profile_id, status)
SELECT c.id, p.id, 'pending'
FROM calls c
CROSS JOIN (VALUES (:pid_0, :fp_0), (:pid_1, :fp_1), ...)
         AS want(profile_id, fingerprint)
JOIN profiles p ON p.id = want.profile_id
WHERE p.is_active = true
  AND c.published_at >= :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM call_rankings cr
    WHERE cr.call_id = c.id AND cr.profile_id = p.id
      AND ((cr.profile_fingerprint = 'legacy' AND cr.ranked_at >= p.updated_at)
           OR cr.profile_fingerprint = want.fingerprint)
  )
  AND NOT EXISTS (
    SELECT 1 FROM ranking_jobs rj
    WHERE rj.call_id = c.id AND rj.profile_id = p.id
      AND rj.status = 'failed' AND rj.updated_at > :dead_letter_cutoff
  )
ON CONFLICT (call_id, profile_id)
  WHERE status IN ('pending', 'running') DO NOTHING
```

**`cr.ranked_at >= p.updated_at` is what bounds the legacy exemption.** Rev 2's
arm was unconditional, so a pre-0075 pair stayed exempt *forever* — including after
its profile was edited and its background task was lost, which is precisely the
case this spec exists to repair. Rev 2 believed the `'legacy'` sentinel had closed
that; the sentinel only stopped a post-deploy NULL from being exempt. It did
nothing about edits.

`Profile.updated_at` carries `onupdate=func.now()`, so any edit moves it past
`ranked_at` and revokes the exemption for that profile's pairs alone. It is a
coarser signal than a fingerprint — a visibility toggle also bumps it — but the
cost is bounded (one re-rank of one edited profile's window, once) and the row
gets a real fingerprint afterwards, so it never recurs. Untouched profiles stay
grandfathered, which is what was asked for.

The dead-letter suppression is unchanged, so a pair that is stale *and* terminally
failed does not re-enqueue every tick.

Nothing new is needed to force a full re-rank. `POST /api/admin/rerank`
(`web/api/admin.py:381`) already calls `backfill_profile` for every active
profile, and `backfill_profile` (`ranking/backfill.py:39-70`) has no
`NOT EXISTS call_rankings` guard, so it re-enqueues regardless. The SPA exposes it
with a confirmation dialog and a `rerank_estimate`
(`frontend/src/routes/AdminDashboard.tsx:54`).

### 7. Worker — defer on a stale vector

Between the `is_active` check and `rank_pair`, compare the stored
`embedding_fingerprint` against `embedding_fingerprint(profile)`. On mismatch —
and only when a vector is actually present — call `revert_to_pending` and
`continue`, unless `RankingJob.created_at` is older than
`_STALE_VECTOR_MAX_DEFER_HOURS`. Log the deferral; a pair deferring repeatedly is
an embedder problem, and it should be visible as one.

### 8. Hole 1 — `src/grant_lab/web/api/profiles.py:524`

Replace the description guard with a rank-fingerprint comparison: compute
`old_rank_fp` beside `old_embed_text` at `:480` and `new_rank_fp` beside
`new_embed_text` at `:520`, and schedule when they differ.

Because the embedding inputs are a subset of the ranking inputs, this schedules a
superset of today's behaviour and never less. A PUT that changes nothing schedules
nothing — an improvement, since today such a PUT enqueues 90 days of ranking jobs
whenever the description happens to be non-empty.

This comparison is in-memory-versus-loaded (see the round-trip section). It is
safe only because the canonicalizer normalizes numbers and omits empties, and gate
3 tests exactly that path.

## Out of scope

- **A transactional outbox for hole 2.** The obligation stays an in-process
  `BackgroundTask` registered after commit. Closing it properly means a job table
  written in the edit's transaction and a drainer, which is a larger change with
  its own failure modes. This spec's promise is *correct within a day*.
- **`model` / `prompt_version` drift.** `call_rankings` stores both and reconcile
  ignores both. A ranking from an older prompt version is invisible to repair for
  exactly the same reason a stale one is. Same shape, separate finding.
- **UI staleness marker.** No "may be out of date" badge.
- **Review finding 1** (lock loss leaves `/healthz` at 200) and **finding 2**
  (Git/Postgres rollback window).
- **Call-side freshness.** `refresh_call_embedding` has the same gap. Calls are
  ingested and rarely edited, so exposure is much smaller. Deferred, not
  overlooked.

## Testing

Every gate is proved by mutation: revert the fix, confirm the named test fails,
and confirm it fails for the stated reason rather than through an import error.

| # | Gate | Mutation that must redden it |
|---|---|---|
| 1 | Fingerprint of an in-memory profile equals the fingerprint after commit + fresh load in a new session. Fixture must contain nested dict keys in non-sorted order and a number whose representation is **asserted to differ** across the round-trip before the fingerprints are compared | drop `sort_keys=True`; separately, drop numeric canonicalization |
| 2 | An areas-only edit (empty description) schedules refresh + backfill | restore `if body.description.strip():` |
| 3 | A PUT with an identical body, after a real save/reload cycle, schedules nothing. Seed the differing-representation number directly, not via a previous PUT that would already have normalized it | remove numeric canonicalization from the canonicalizer |
| 4 | The tick re-embeds a profile whose text changed while its vector and model are both present and current | drop the fingerprint arm |
| 5 | With more than `batch_size` profiles and the only stale one at the **highest** id, the tick still embeds it | restore the `processed == 0` break |
| 6 | The staleness decision never loads the vector | select `Profile` wholesale; the test must redden |
| 7 | Force mode is unchanged: every embeddable row is re-embedded regardless of fingerprint | apply the fingerprint arm under `force=True` |
| 8 | A profile cleared to blank while still holding a vector is picked up and invalidated (hole 4) | restore the unconditional `build_profile_text(p).strip()` exclusion |
| 9 | The worker hashes the profile the ranker saw, and persistence keeps that value | move the hash to **after** a `session.refresh(profile)` that follows `rank_pair`; the test must redden |
| 10 | Retained analysis is dropped when the previous ranking's fingerprint differs | keep the triple unconditionally |
| 11 | Reconcile re-enqueues a pair whose ranking fingerprint differs | drop the fingerprint condition |
| 12 | Reconcile skips a `'legacy'` pair whose profile has not been touched | remove the `'legacy'` arm |
| 12b | Reconcile **re-enqueues** a `'legacy'` pair whose profile was edited after `ranked_at` | remove the `cr.ranked_at >= p.updated_at` condition |
| 13 | Reconcile treats a **NULL** (post-deploy, i.e. buggy) fingerprint as stale | treat NULL as legacy |
| 14 | Reconcile issues no enqueue `INSERT` when the active-profile SELECT returns nothing | remove the early return |
| 15 | The worker defers a pair whose vector is stale, without bumping `attempts`. Assert PENDING and `attempts` from a **fresh session**, so an uncommitted revert cannot pass | drop the commit after `revert_to_pending` |
| 16 | Deferral is bounded: a job older than the cap ranks anyway | remove the age bound |
| 17 | A missing vector (`embedding IS NULL`) does **not** defer | treat NULL embedding as a mismatch |
| 18 | `force=True` embedding failure NULLs the fingerprint with vector and model | leave the fingerprint set |
| 19 | `force=False` embedding failure leaves all three untouched | NULL the fingerprint |

Gates 1, 3 and 5 are the ones that matter most. 1 and 3 guard against a
fingerprint that changes when the profile did not — which re-ranks the corpus
daily, presenting as GPU load rather than an error. 5 guards the repair actually
reaching the rows it exists for, which is what rev 1 got wrong.

## Risks

- **Gate 1 passing on a fixture too simple to fail.** The fixture must contain
  nested non-sorted keys, an integral float, and an empty collection. A
  strings-only fixture makes gates 1 and 3 vacuous.
- **A future payload change re-ranking everything silently.** Mitigated by the
  version tag and the rule attached to it, not eliminated.
- **The `UPDATE call_rankings SET profile_fingerprint = 'legacy'` migration**
  taking a long lock on a large table. Measure the row count first.
- **The embedding tick's cursor logic** is the most intricate existing code being
  touched. The comment at `:330-332` documents an infinite-loop bug already fixed
  there once, and change 5(b) alters the same loop's exit condition.
- **The 03:30 / 04:00 ordering is a convention, not a lock.** An embedding tick
  that overruns 30 minutes, or misfires within its 6-hour grace, lets reconcile run
  first. The promise is "correct within a day" in the normal case and up to two
  cycles when that ordering slips.
- **Deferral masking an embedder outage.** Bounded at 24h, but the deferral log
  line is the only signal; if nothing watches it, a persistent outage shows up as
  rankings that are a day late rather than as an alert.
