# Call embedding freshness — a changed call keeps the vector of its old text

**Status:** rev 1, unbuilt. Base: master `51c0fcb6`.
**Origin:** the 2026-09-08 whole-project review — the one finding of seven not
fixed in that round (the other six merged as `87e62d96` and `51c0fcb6`).

---

## 1. The defect

Migration 0075 gave **profiles** an `embedding_fingerprint` and taught the daily
tick to treat "the text this vector describes is no longer the row's text" as
stale. **Calls never got one.** The call half of the repair tick still asks only:

```python
or_(Call.embedding.is_(None), Call.embedding_model != current_model)
```

A call whose text changed, whose vector is present, and whose `embedding_model`
matches is invisible to that predicate — permanently.

### Measured, not argued

Reproduced against master `51c0fcb6` by appending the call-side mirror of the
profile GATE 4 test to `tests/test_embeddings_refresh_tick_staleness.py`. Two
calls are created and embedded, one has its `summary` changed, the tick runs
again with a healthy embedder:

```
first tick:  2 embeds        (setup is sound)
second tick: 0 embeds        <- the changed call was never re-examined
E  AssertionError: the changed call must re-embed and the untouched one must not;
E  the tick embedded 0 calls
```

The observable is **the embed count on `FakeEmbedder`, not the vector**. A first
attempt compared vectors and was hollow: `FakeEmbedder.embed` returns a constant
`[0.1] * 1024`, so a vector comparison fails identically on correct and broken
code. That mistake is the reason this section quotes a count.

### How a call reaches that state

`upsert_call` is the **only** writer of `title` / `summary` / `full_text`
(grepped across `src/`; every other `.title =` in the tree is a board, a
reference or a workspace). Ingestion always follows the upsert with
`refresh_call_embedding(..., force=False)`. So the text and the vector go out of
sync only when that embed does not land:

1. **The embedder fails.** `refresh_call_embedding` catches `LLMError` and, with
   `force=False`, returns `False` **without touching `call.embedding` or
   `call.embedding_model`**. Deliberate — it is what stops an Ollama outage from
   blanking the corpus. The row is then text-new, vector-old, model-current.
2. **The process dies between step 1 and step 2** of the orchestrator. Step 1
   commits the upserts in one transaction; step 2 embeds in per-row
   transactions. A crash in between leaves every touched call stale.
3. **`build_call_text` changes.** It composes `title + summary +
   full_text[:CALL_TEXT_FULL_TEXT_LIMIT]` (8000). Changing that function or that
   constant changes what every vector describes while changing **no row at all**.
   No content-level check can see this; only a fingerprint over the function's
   *output* can.

Route 3 is why the design below hashes output, not columns.

### What it costs

The stale vector feeds the cosine pre-gate. A call is then screened against text
it no longer contains — the score is wrong, quietly, with no NULL to notice and
no log line. The `UNKNOWN → fall back to the LLM` safety valve does **not** fire,
because the vector is present. This is strictly worse than a missing embedding.

---

## 2. Two obvious fixes, both wrong

**"Pass `force=True` from the orchestrator."** `force=True` nulls the vector on
embed failure, which does make the row visible to today's predicate. But
`touched_call_ids` is *every* call the adapter returned that has a row — changed
or not; `upsert_call` returns "was newly inserted", never "content differed". So
one Ollama outage would null **the entire feed's embeddings**. That is the
failure mode `force=False` exists to prevent.

**"Store a content hash at upsert time so the tick filter stays pure SQL."**
Cheaper to query, and blind to route 3: editing `build_call_text` changes no
column, so every content hash still matches while every vector is wrong. It also
adds a second column whose only job is to be compared to the first.

---

## 3. The design — mirror 0075 exactly

**D1. `calls.embedding_fingerprint TEXT NULL`, migration 0080.** One column, same
name and shape as `profiles.embedding_fingerprint`.

**D2. A `call_embedding_fingerprint(call)` helper beside `embedding_fingerprint`,
digesting `build_call_text`'s OUTPUT** through the existing `_digest`. Hashing
output rather than inputs is what closes route 3, and it is the reason the
profile helper's docstring gives for its own shape.

**D3. It shares `FINGERPRINT_VERSION`.** The tag is already shared between
`embedding_fingerprint` and `rank_fingerprint`, and that module's docstring is
explicit that a bump re-embeds and re-ranks everything at once. Adding a third
consumer of the same tag keeps one lever; introducing a separate call version
would mean two levers and two ways to forget one.

**D4. No grandfathering. NULL means stale.** 0075 made this exact call for
profiles and wrote down the rule: grandfather when the recompute is expensive
(`call_rankings` got `'legacy'`), leave NULL when it is cheap. Measured on the
live dev database:

```
calls_total                58
calls_with_embedding       58
profiles_total              1
avg_call_text_bytes      1372
max_call_text_bytes      2333
total_call_text_mb       0.08
```

One full re-embed of 58 calls against a local Ollama on the first tick after
deploy. The documented growth rate is ~50 calls/week (`ingestion/upsert.py`), so
this stays seconds for years.

**D5. The call half stops narrowing in SQL** and decides per row in Python,
exactly as the profile half did in 0075 — because `build_call_text` is Python and
cannot be evaluated in SQL. Select the columns needed and **not** `Call.embedding`,
carrying `embedding IS NULL` as a computed boolean instead. The profile half
gives the reason in its own comment — pulling the 1024-dim vector is "the
measured dominant cost of a profile load". That measurement is profile-side;
this slice inherits the pattern, not the number.

**D6. `refresh_call_embedding` writes the fingerprint on success, and clears it
alongside the vector under `force=True`.** Anywhere the vector is set or nulled,
the fingerprint moves with it — a fingerprint that outlives its vector asserts
"fresh" about a row that has none.

---

## 4. The hazard this slice actually turns on

Widening the call half breaks the drain loop's **termination argument**, which is
written down in `refresh_missing_embeddings_tick` and depends on the current
narrowing:

> Termination still holds. The call half is SQL-narrowed, so `calls_embedded +
> calls_failed` IS the number of call rows examined. The profile half is not,
> which is why it contributes `profiles_seen`.

After D5 that sentence is false. A page of *fresh* calls returns
`calls_embedded == 0` and `calls_failed == 0`. The loop computes:

```python
examined = summary["calls_embedded"] + summary["calls_failed"] + summary["profiles_seen"]
if examined == 0:
    totals["scan_exhausted"] = True
    break
```

so a full page of fresh calls, with the profile half already done, reads as
"scan exhausted" and **the drain stops at page one with the rest of the calls
table unrepaired.** This is not a hypothetical: it is the same bug the profile
half already shipped once, which is why `profiles_seen` exists.

**D7. Add `calls_seen`** — rows examined, not rows worked — and make it a term of
`examined`, mirroring `profiles_seen`.

**D8. Add `call_scan_exhausted`**, the call half's own exhaustion, mirroring
`profile_scan_exhausted`. The joint `scan_exhausted` cannot stand in for it, for
the reason already recorded on the profile side: a tick held at `max_batches` by
one half reports False while the other half is long finished.

**D9. The scheduler must carry an `after_call_id` cursor.** `_embeddings_tick`
today passes `after_profile_id` only, so the call half restarts from id 0 every
tick. That is harmless while the call half is SQL-narrowed — the candidate set is
near-empty — and becomes a wall the moment it examines every row: with
`batch_size=200` and `_TICK_MAX_BATCHES=200` a tick examines at most 40 000 call
rows and then restarts from 0 the next night, never reaching row 40 001.

The cursor must reset on `call_scan_exhausted` and only on it, for the reason
`_embeddings_tick`'s existing comment gives about the profile cursor: a cursor
that never returns to 0 makes a row that goes stale *behind* the high-water mark
permanently unreachable — a worse bug than the one being fixed.

---

## 5. Gates

Every flip-set below is `PREDICTED (not run)` — the code does not exist yet. Each
one is a **measurement the implementer performs**, not a claim this document is
entitled to make. Record the observed red set next to each.

| # | Gate | What trivially passes it |
|---|---|---|
| G1 | A changed call re-embeds on the next tick | nothing — this is the reproduction of §1 |
| G2 | An **unchanged** call does **not** re-embed | a tick that re-embeds the world; G1 alone passes on it |
| G3 | A call stale behind a full page of fresh calls is still reached (`batch_size=2`) | any run where the corpus fits in one page |
| G4 | Editing `build_call_text` invalidates every call fingerprint | a content-hash design; this is route 3 |
| G5 | `force=True` clears fingerprint **and** vector together | asserting only the vector |
| G6 | A capped tick resumes the call cursor instead of restarting at 0 | a corpus smaller than one page |
| G7 | An exhausted call scan resets the cursor to 0 | never letting the scan exhaust |
| G8 | A tick capped by the **profile** half still resets the **call** cursor | the mirror of the existing gate 9c |
| G9 | The first tick after migration re-embeds every call (NULL = stale) | a fixture that seeds fingerprints |

G2 and G3 are the ones that catch the two failure directions of D5. G8 is the
mirror of a bug this module has already shipped in the opposite direction.

**A gate is not accepted until it has been run against something it must
reject.** Two lines of evidence per gate, per `measure-dont-predict` Rule 2.

---

## 6. Out of scope, deliberately

**Ranking freshness against call text.** `reconcile_missing_rankings` judges a
ranking current on `profile_fingerprint` alone, so a changed call does not
invalidate its own rankings. Verified that this is *mitigated, not absent*:
`bulk_enqueue_for_calls` enqueues a job for every touched call × active profile
on every ingest run, blocked only by an already-active job — so a re-ingested
call is re-ranked that same run. The permanent-staleness property that makes the
embedding bug worth fixing does not hold here. Revisit only with a measured case.

> **Amended 2026-09-24.** The measured case arrived: that nightly re-enqueue re-ranks
> the whole open feed and re-pays Sonnet for the same pairs daily.
> `2026-09-24-ranking-freshness-design.md` removes it, and so removes the mitigation
> this paragraph relies on. Call-side ranking freshness is carried there by
> `call_rankings.call_fingerprint`, which hashes this slice's
> `calls.embedding_fingerprint`. Build this slice first. The paragraph above stays as
> written, as the record of what held on 2026-09-08.

**A settings/KV table for cursor persistence.** `_embeddings_tick`'s existing
comment already decided this: the cursor is an in-process resume hint, a restart
resets it to 0, and that is "slower but never skips a row". D9 inherits the
decision rather than reopening it.

---

## 7. Baseline

Measured on master `51c0fcb6`, not copied from an earlier revision:

```
backend:  5530 passed, 1 skipped
frontend: 1215 passed
ruff:     clean
CI no-build gate: green
docs/ARCHITECTURE.md: in sync
latest migration: 0079  ->  this slice adds 0080
```

The `1 skipped` is the real-SDK test gated on `ANTHROPIC_API_KEY`. It stays 1.
