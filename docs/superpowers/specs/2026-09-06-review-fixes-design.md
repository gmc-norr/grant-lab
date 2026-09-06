# Four defects from the first outside look at Plan C

**Date:** 2026-09-06
**Status:** design, awaiting review
**Origin:** `docs/superpowers/reviews/2026-09-06-plan-c-and-freshness-code-review.md`
**Base:** `master` at `03fad6db`

Four confirmed defects from the first outside review of the Plan C code. Each was
verified here against the source before being written down; the review document carries
the evidence and is not repeated.

Two of them are cheap and two need a migration. They ship together because they share a
release: three of the four are only fully closed by a schema change, and the fourth is
free **only if it lands before the first production deploy**.

## Why now, and what the timing is worth

**F4's fix costs nothing today and gets expensive later.** It adds a key to the
fingerprint payload, which bumps `FINGERPRINT_VERSION` and invalidates every stored
fingerprint — a full re-embed and a full re-rank.

That already happens on the first night after `0075` deploys, because `0075`
deliberately leaves `profiles.embedding_fingerprint` NULL. **Two full re-ranks and one
is free.** Ship F4 before the first deploy and it costs nothing; ship it after and it is
a second unplanned re-rank of the corpus.

---

## F1 — anonymous requests can repeatedly force the cold QR path

### What is wrong

`web/api/public_boards.py:132` calls `qr_for(row.url)` on the anonymous request path.
`qr.py:313-314` takes `_encode_lock` **per URL, globally**, so any thread encoding
blocks every other thread wanting a QR. With `maxsize=256` and a permitted board shape
of 192 items, two published boards with disjoint URL sets thrash the cache and land
every request on the cold path.

`qr.py:95-118` already describes this scenario almost verbatim and concludes the cost is
"ONE-TIME, COLD". That holds only while the working set fits the cache. Two max-shape
boards do not fit, and nothing stops an anonymous caller alternating between them.

### The fix: pay at approval, not at display

An item is approved by an admin, in an authenticated, rate-limited request that is
already doing real work. That is where the ~45 ms encode belongs.

`board_items` gains three columns:

| Column | Meaning |
|---|---|
| `qr_path TEXT NULL` | the rendered path, or `NULL` if the URL was over `_MAX_URL_BYTES` |
| `qr_modules INT NOT NULL DEFAULT 0` | `0` means "no QR" — the signal the frontend already honours |
| `qr_source_url TEXT NULL` | the `Call.url` the QR was rendered from |

`approve_items` (`web/api/boards.py:580`) computes and stores all three.
`get_public_board` reads them and **encodes nothing**.

**`qr_source_url` is the point, not bookkeeping.** A stored QR is derived data with the
same exists-versus-current problem this codebase just spent a release fixing for
embeddings. Storing the URL it was made from means a changed `Call.url` is *detectable*
rather than silently wrong. The public route compares it and, on a mismatch, serves
`qr_modules=0` — no QR rather than a QR pointing somewhere the call no longer lives.
Re-rendering is an admin action, not something the anonymous path does.

This removes the class rather than tuning it: no cache on the hot path, no global lock
held during an anonymous request, no thrash to bound.

### Migration

Backfill existing rows by rendering each approved item's QR. `board_items` is empty in
production today — boards shipped on 2026-09-06 and have not been deployed — so this is
a no-op there, and the code is written for the general case rather than relying on that.

---

## F2 — concurrent transfers authorise against an obsolete owner

### What is wrong

`grep -c with_for_update src/grant_lab/web/api/profiles.py` returns **0**.
`_profile_for_edit` (`:136`) loads the row unlocked. Two transfers can both read owner
A, both pass authorisation, and the second computes `outgoing_lost_read` for A when the
real outgoing owner is already B — so the wrong person is reported, and one transfer's
ownership write is lost.

### The fix

`_profile_for_edit` loads the row `FOR UPDATE`.

All **five** of its callers (`:598`, `:632`, `:694`, `:739`, `:794`) are the four
sharing operations and transfer — precisely the set that must not interleave. Locking
inside the helper serialises them against the same row, which is the guarantee, rather
than patching transfer alone and leaving `add_profile_viewer` racing it.

Lock ordering: this takes a `profiles` row lock inside a request that holds no other.
The documented order is `mutex < jobs < workspaces < calls`; `profiles` is not in it and
no sharing path acquires a second row lock, so no new ordering edge is introduced. The
plan must confirm that rather than assume it.

---

## F3 — profile creation confirms private profile names

### What is wrong

`profiles.py:365-369` checks
`select(Profile.id).where(Profile.name == body.name, Profile.is_active.is_(True))`
with **no readability scoping**, and returns 409 `"profile name already exists"`.

Eight lines above, `:250-256` states the rule this breaks:

> A 403 confirms the profile exists, and profile **NAMES** are the thing worth not
> confirming.

The detail route returns 404 for exactly that reason. Creation then confirms the same
fact by another door.

### The fix, and why the query alone is not enough

`db/models/profiles.py:52-56` carries `uq_profiles_active_name` — a partial UNIQUE index
on `name` where `is_active`. **Postgres itself would raise on insert even with a scoped
pre-check**, so scoping the query alone moves the leak from a 409 to a 500.

The index becomes owner-scoped: unique on `(created_by, name)` where `is_active = true`.
The handler's pre-check is scoped to match, so a 409 can only ever name a profile the
caller already owns and can see.

Two users may then each have a "Hepatology" profile. That is consistent with profiles
being personal and private by default. Relaxing a uniqueness constraint cannot fail on
existing rows, so the migration is safe in either direction of data.

**The pre-check and the index must agree.** A mismatch is how this defect happened: the
handler and the schema each enforced something, and neither enforced the same thing the
privacy comment described.

---

## F4 — an embedding-model swap leaves rankings marked current

### What is wrong

`fingerprint.py:110-118` — `rank_fingerprint` includes `embedding_fingerprint` but not
`embedding_model`. `embedding_fingerprint` (`:66`) digests `build_profile_text(profile)`,
which is text only.

So changing `EMBEDDING_MODEL`:

1. The tick's `embedding_model != current_model` arm fires and re-embeds every profile.
2. `build_profile_text` output is unchanged, so `embedding_fingerprint` is
   byte-identical.
3. `rank_fingerprint` is therefore byte-identical.
4. Reconcile treats every ranking as current — while its stored `embed_similarity`, and
   the score derived from it, describe the **previous** vectors.

The embedding repair predicate knows about model identity. The ranking one inherits only
a text hash. **This repo has already swapped embedding models once** (mxbai-embed-large →
bge-m3), so it is a path that has been walked.

### The fix

`embedding_model` joins the `rank_fingerprint` payload, and `FINGERPRINT_VERSION` goes
`v1` → `v2`.

The version bump is required, not decorative: the payload changed, so every stored
fingerprint mismatches. That is the rule the version tag exists to enforce — a payload
change must be a deliberate, visible act. See the timing note at the top for why this is
free today.

`'legacy'` is unaffected: it is a sentinel, not a versioned digest, and reconcile
compares it by equality.

---

## Out of scope

- **The `--settled` list from the review round.** Grandfathering, the one-time Sonnet
  summary loss, unmeasured-at-scale, `/healthz`, the `_TICK_MAX_BATCHES` bound and
  `embeddings/cli.py` are all unchanged and deliberately so.
- **Re-rendering a stale QR automatically.** A mismatched `qr_source_url` serves no QR;
  regenerating it is an admin action. Making that automatic is a separate decision about
  who is allowed to change what appears on a corridor wall.
- **`Call.url`'s unbounded length.** `_MAX_URL_BYTES` bounds what is rendered, not what
  is stored. Pre-existing.

## Testing

Every gate proved by mutation: revert the fix, confirm the named test fails, and confirm
it fails on FAILED rather than through an import ERROR.

| # | Gate | Mutation that must redden it |
|---|---|---|
| 1 | The public board route encodes **nothing** — assert on `qr_for.cache_info()` misses staying at 0 across a request | restore `qr_for(row.url)` in `get_public_board` |
| 2 | Approval stores `qr_path`, `qr_modules` and `qr_source_url` | drop the store from `approve_items` |
| 3 | A URL over `_MAX_URL_BYTES` stores `qr_modules=0` and still renders its card | store the path unconditionally |
| 4 | A `Call.url` changed after approval serves `qr_modules=0`, not a stale QR | drop the `qr_source_url` comparison |
| 5 | Alternating two 192-item boards costs zero encodes on every request after the first approval | — regression pin; must pass with gate 1's mutation reverted |
| 6 | Two concurrent transfers: exactly one succeeds, and the loser sees the **current** owner | remove `with_for_update` |
| 7 | A concurrent transfer and `add_profile_viewer` serialise | lock in the transfer handler only, not in `_profile_for_edit` |
| 8 | Creating a profile whose name matches a **private** profile the caller cannot read **succeeds** | restore the unscoped pre-check |
| 9 | Creating a duplicate name the caller **does** own still 409s | drop the pre-check entirely |
| 10 | Two different owners may both hold the same active name | keep the index global |
| 11 | Changing `EMBEDDING_MODEL` changes `rank_fingerprint` for an unedited profile | remove `embedding_model` from the payload |
| 12 | Every fingerprint is `v2:`-prefixed | leave `FINGERPRINT_VERSION` at `v1` |

Gates 8, 9 and 10 are one set: 8 proves the leak is closed, 9 proves the constraint still
does its job, 10 proves the scope actually changed. Any one alone passes a wrong
implementation.

## Risks

- **Gate 1 is the load-bearing one for F1.** Asserting "the response is correct" would
  pass with the encode still on the hot path. It must assert on cache misses or on the
  encode function not being called.
- **The QR backfill migration** renders one QR per approved item. Empty in production
  today; a large `board_items` elsewhere would make it slow. Measure the row count
  before running it.
- **F4's version bump invalidates every fingerprint**, by design. If this lands after the
  first production deploy it is a second full re-rank rather than a free one.
- **Gate 7** requires two sessions and real row locks; a fixture that shares one session
  proves nothing.
