# Plan C + matching freshness — outside code review, 2026-09-06

Reviewer: `gpt-6-astra` via GitHub Copilot, against a 251,751-char package covering 14
source files at `master` `6f8ab1ee`. The reviewer could not run anything; every finding
below is READ or INFERRED grade **as delivered**, and each was verified here against the
code afterwards.

This is the first outside look at the Plan C code. The 2026-09-05 architecture review
stopped at `4e6212e2` and predates it entirely.

## Disposition: 4 of 5 confirmed, 1 was a defect in my review package

| # | Finding | Verdict |
|---|---|---|
| 1 | Anonymous QR cache thrash | **CONFIRMED** |
| 2 | Concurrent transfers race on a stale owner | **CONFIRMED** |
| 3 | Profile creation is an existence oracle for private names | **CONFIRMED** |
| 4 | An embedding-model swap leaves rankings marked current | **CONFIRMED** |
| 5 | The round-trip guarantee is unverifiable from the package | **CONFIRMED, my error** |

---

### 1 — HIGH — anonymous requests can repeatedly force the cold QR path

**Reviewer:** alternating anonymous `GET /b/<slug>` between two published boards whose
disjoint URL sets exceed `maxsize=256` lands every request on the cold path; the global
encode lock then serialises that work while other request threads wait.

**Verified here.** `qr.py:313-314` is `with _encode_lock: return _cached_qr_for(url)` —
the lock is taken **per URL, globally**, so any thread encoding blocks every other
thread wanting a QR. `board_limiter` is 60 requests / 15 min per IP
(`main.py:881-882`), so one IP can force on the order of **350 seconds of lock-held CPU
per window**.

**What makes this a real finding rather than a re-raise:** `qr.py:95-118` already
describes this exact scenario, in almost these words — *"two published boards x up to
192 distinct worst-case urls each can exceed `maxsize=256` and land every request on the
cold path"* — and then concludes the costs are **"ONE-TIME, COLD ... not a per-request
or per-viewer multiplier"**. That conclusion holds only while the working set fits the
cache. Two max-shape boards do not fit, and nothing stops an anonymous caller
alternating between them. The module analysed the right scenario and drew the wrong
conclusion.

**Mitigation the reviewer did not have:** boards are admin-approved, so an attacker
cannot create the two boards. The precondition is that the institution has published
them — plausible, not attacker-controlled.

### 2 — HIGH — concurrent transfers authorise against an obsolete owner

**Verified here.** `grep -c with_for_update src/grant_lab/web/api/profiles.py` returns
**0**. `_profile_for_edit` loads the row unlocked, so two transfers can both read owner
A, both pass authorisation, and the second computes `outgoing_lost_read` for A when the
real outgoing owner is already B.

### 3 — HIGH — profile creation confirms private profile names

**Verified here.** `profiles.py:365-369` is
`select(Profile.id).where(Profile.name == body.name, Profile.is_active.is_(True))` with
**no `readable_profile_ids` scoping**, returning 409 `"profile name already exists"`.

The sharpest part: `profiles.py:250-256`, eight lines above the endpoint that leaks it,
states the rationale it violates — *"A 403 confirms the profile exists, and profile
NAMES are the thing worth not confirming."* The detail route returns 404 for exactly
this reason; creation then confirms the same fact by another door.

**Fixing the query alone is not enough.** `db/models/profiles.py:52-56` carries
`uq_profiles_active_name`, a partial UNIQUE index on `name` where `is_active`. Postgres
would raise on insert even with a scoped pre-check, so a real fix needs a migration to
owner-scoped uniqueness.

### 4 — HIGH — an embedding-model swap leaves rankings marked current

**Verified here.** `fingerprint.py:110-118` — `rank_fingerprint` includes
`embedding_fingerprint` but **not** `embedding_model`, and `embedding_fingerprint`
(`:66`) digests `build_profile_text(profile)`, which is text only.

So swapping `EMBEDDING_MODEL` re-embeds every profile (the tick's
`embedding_model != current_model` arm fires), leaves both fingerprints byte-identical,
and reconcile therefore treats every ranking as current — while its stored
`embed_similarity`, and the score derived from it, describe the previous vectors.

**Not hypothetical:** this repo has already swapped embedding models once
(mxbai-embed-large → bge-m3).

The freshness design's own asymmetry is the cause: the *embedding* repair predicate
knows about model identity; the *ranking* one inherits only a text hash.

### 5 — MEDIUM — the round-trip guarantee was not checkable, and that is my fault

The reviewer correctly reported that `embeddings/similarity.py` — which defines
`build_profile_text`, whose output the embedding fingerprint hashes — **was not in the
package**. Fourteen files were included and that one was omitted. The central JSONB
round-trip claim was therefore unverifiable, and saying so was the right answer.

Its secondary observation, that a forced-refresh failure clears
`embedding_fingerprint` and so changes `rank_fingerprint` without profile content
changing, it correctly self-limits as intentional invalidation rather than instability.
No defect.

**Carry into the next package:** `embeddings/similarity.py`, and the test files whose
behaviour a guarantee depends on.

## What the settled file successfully prevented

Nothing in the five findings re-raises grandfathering, the one-time Sonnet-summary loss,
the unmeasured-at-scale caveat, `/healthz`, the `_TICK_MAX_BATCHES` bound, or
`embeddings/cli.py`. The `--settled` file earned its place.
