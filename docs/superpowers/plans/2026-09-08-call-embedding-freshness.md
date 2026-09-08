# Implementation plan — call embedding freshness

Spec: `docs/superpowers/specs/2026-09-08-call-embedding-freshness.md`.
Parent design (the profile half this mirrors):
`docs/superpowers/specs/2026-09-06-matching-freshness-design.md`.
Base: master `51c0fcb6`. Six tasks, one migration, one commit per task.

**The spec is the one home for every number.** This plan does not restate the
baseline counts or the dev-database measurements; it points at §7 and §3 of the
spec. Do not copy a number here to make arithmetic close — re-measure and, if it
moved, name the term that changed.

---

## Measured while authoring this plan

Not predictions. Re-run any of these if you doubt them; each is one command.

```
alembic heads                     -> 0079 (head)          so 0080, down_revision "0079"
Call columns present              -> id title summary full_text embedding embedding_model
Call.embedding_fingerprint        -> absent (this slice adds it)
similarity.py imports             -> stdlib only; no cycle from fingerprint.py
```

**Every mutation anchor named below occurs exactly once** in its file (checked by
`str.count`, all 9 returned 1). Re-check before mutating anyway: an anchor that
an earlier task moved is the failure mode this repo has already hit, and a
`replace` on a stale anchor writes nothing while reporting success.

**Task 2's helper body was executed as written**, against a duck-typed row:

```
build_call_text(Row(title="T", summary="S", full_text="F"))  ->  'T\n\nS\n\nF'
_digest(...)                                                 ->  'v2:93101e4c9dbe76f23...'
changes when summary changes                                 ->  True
starts with 'v2:'                                            ->  True
```

So Gate 2's three legs are already known to hold for that implementation. Every
other gate below is still `PREDICTED (not run)` — the code does not exist.

---

## The mutation protocol

Every gate below is accepted only after it has been run against something it
must reject. That means editing source, running, and putting it back. Get this
wrong and you commit the mutant.

```bash
set -o pipefail                     # `pytest | tail` exits 0 on FAILURE without it
R=/home/parlar_ai/dev/grant-lab
S="$R/.mutation-backups"; mkdir -p "$S"
F=$R/src/grant_lab/embeddings/refresh.py

cp "$F" "$S/refresh.py.bak"         # a COPY, never `mv`, never `trap ... EXIT`
python3 - <<'PY'
from pathlib import Path
p = Path("<absolute path>")
s = p.read_text()
a = "<the exact anchor>"
assert s.count(a) == 1, f"ANCHOR MISS {s.count(a)}"   # abort BEFORE writing
p.write_text(s.replace(a, "<the mutant>"))
PY
timeout 900 uv run pytest <target> -q --timeout=300 2>&1 | tail -6
cp "$S/refresh.py.bak" "$F"         # explicit ABSOLUTE cp back
git diff --stat "$F"                # prove the restore landed
```

Five rules that have each cost a session in this repo:

- **`trap ... EXIT` does not work here.** It fires when the *shell* exits, not
  between commands, and a second `trap ... EXIT` **replaces** the first. Verified:
  `bash -c 'trap "echo A" EXIT; trap "echo B" EXIT; true'` prints only `B`.
- **`set -o pipefail` or the pipe eats the failure.** Verified: `false | tail -1`
  exits 0.
- **The anchor assertion goes before the write.** A python edit script whose
  `assert` fires after `write_text` has already landed a partial edit.
- **A mutation must produce a SPLIT.** Everything red means coupled tests.
  Nothing red means a hollow gate. Record the observed red set, not the expected
  one.
- **Judge on FAILED, never ERROR.** A broken import reddens the suite as
  collection ERRORs with zero FAILED, which reads like a pass.

**Re-run unmutated before every commit.** `git diff --stat` on the mutated file
must be empty.

---

## Task 1 — the column

**Files:** `alembic/versions/0080_call_embedding_fingerprint.py` (new),
`src/grant_lab/db/models/calls.py`, `docs/ARCHITECTURE.md` (generated).

Migration `0080`, `down_revision = "0079"` — confirm `0079` is still the head
before writing it, do not trust this line.

```python
def upgrade() -> None:
    op.add_column("calls", sa.Column("embedding_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "embedding_fingerprint")
```

**No `UPDATE ... SET embedding_fingerprint = 'legacy'`.** Spec D4: NULL means
stale, and one full re-embed of the corpus is seconds at the measured size. This
omission is the deliberate half of the decision — 0075 grandfathered
`call_rankings` and pointedly did not grandfather `profiles`, and wrote down why.

Mirror `profiles.embedding_fingerprint`'s comment on the model column, naming the
NULL-means-stale contract so a reader does not "fix" it later.

**Gate 1.** `uv run alembic upgrade head` then `downgrade -1` then `upgrade head`
against a scratch database. Round-trips clean.

**What trivially passes it:** running only `upgrade`. The `downgrade` leg is the
half that catches a missing `drop_column`.

Regenerate the architecture map in this same commit:
`uv run python scripts/gen_architecture.py`.

---

## Task 2 — the fingerprint helper

**File:** `src/grant_lab/profiles/fingerprint.py`.

Add beside `embedding_fingerprint`:

```python
def call_embedding_fingerprint(call) -> str:
    """What a call's stored `embedding` describes.

    Hashes build_call_text's OUTPUT, exactly as embedding_fingerprint hashes
    build_profile_text's. That is what makes editing the composer — or
    CALL_TEXT_FULL_TEXT_LIMIT — invalidate every call vector, even though such
    an edit changes no column. A hash over title/summary/full_text could not.
    """
    return _digest(build_call_text(call))
```

`build_call_text` comes from `grant_lab.embeddings.similarity`, which this module
already imports `build_profile_text` from — no new dependency edge.

**The module lives under `profiles/` and this function is not about profiles.**
Deliberately not moved: 16 references across 14 files, no behavioural gain. The
module docstring already covers rankings as well as profiles; it is misnamed
already. Record it as a naming debt, not a blocker.

**Gate 2.** Two calls with identical embeddable text hash equally; changing
`summary` changes the hash; the returned string starts `v2:`.

**What trivially passes it:** any function that returns a constant would fail the
second leg, so the *changing* assertion is the load-bearing one. A test that only
asserts the prefix passes on `lambda call: "v2:"`.

---

## Task 3 — write it where the vector is written

**File:** `src/grant_lab/embeddings/refresh.py`, `refresh_call_embedding`.

Three edits, all inside that one function:

1. Success path — beside `call.embedding = result.vector` and
   `call.embedding_model = embed_client.model`, add
   `call.embedding_fingerprint = call_embedding_fingerprint(call)`.
2. `force=True` failure path — beside `call.embedding = None` and
   `call.embedding_model = None`, add `call.embedding_fingerprint = None`.
3. Docstring — state that the fingerprint moves with the vector, always.

Spec D6. A fingerprint that outlives its vector claims "fresh" about a row that
has none, and the widened scan in Task 4 would then skip it forever.

**Gate 3.** After a successful embed, `call.embedding_fingerprint ==
call_embedding_fingerprint(call)`.
**Gate 4.** After a `force=True` embed that raises `LLMError`, all three of
`embedding`, `embedding_model`, `embedding_fingerprint` are `None`.

**Mutation M1** — anchor the fingerprint assignment on the success path and
delete it. Expected: Gate 3 red, Gate 4 green. **Record the observed set.**

**Mutation M2** — leave the `force=True` path clearing only the vector and the
model. Expected: Gate 4 red, Gate 3 green.

Two mutations reddening two *different* single tests is what shows the two
assignments are doing two jobs. One mutation reddening both means the gates are
coupled and neither is pinning what it names.

---

## Task 4 — widen the call half (the dangerous one)

**File:** `src/grant_lab/embeddings/refresh.py`,
`_refresh_missing_embeddings_batch` and `refresh_missing_embeddings_tick`.

### 4a. The query

Drop the `if not force:` narrowing on `call_q`. Select the columns the decision
needs and **not** `Call.embedding`:

```python
call_q = select(
    Call.id,
    Call.title,
    Call.summary,
    Call.full_text,
    Call.embedding_fingerprint,
    Call.embedding_model,
    (Call.embedding.is_(None)).label("embedding_is_null"),
)
```

Decide per row in Python: stale when the vector is NULL, **or** the model differs
from `embed_client.model`, **or** `embedding_fingerprint != call_embedding_fingerprint(row)`.
A `_digest_for_call_row` helper mirroring the existing `_digest_for_row` keeps the
duck-typing deliberate rather than accidental — `build_call_text` reads `.title`,
`.summary`, `.full_text`, all of which the Row exposes.

`full_text` is bounded by `build_call_text` at 8000 chars; the measured maximum in
the corpus is far below that (spec §3), so no SQL-side `left()` is needed. Say so
in a comment rather than leaving the reader to wonder.

### 4b. `calls_seen`

Add `calls_seen` to both summary dicts — rows **examined**, not rows worked —
and add it to the `totals` accumulation loop.

### 4c. The termination argument

This is the whole reason the task is dangerous. Replace:

```python
examined = summary["calls_embedded"] + summary["calls_failed"] + summary["profiles_seen"]
```

with a sum over `calls_seen` and `profiles_seen`, and **rewrite the comment above
it**. The comment currently asserts "the call half is SQL-narrowed, so
calls_embedded + calls_failed IS the number of call rows examined". After 4a that
sentence is false, and leaving it there hands the next reader a proof of
termination that no longer holds.

**Gate 5 (G1 in the spec).** A changed call re-embeds on the next tick. This is
the reproduction — it is already known to fail on master, and §1 of the spec
records the exact output. The observable is **the embed count on `FakeEmbedder`**,
never the vector: `FakeEmbedder.embed` returns a constant `[0.1] * 1024`, so a
vector comparison fails identically on correct and broken code.

**Gate 6 (G2).** An **unchanged** call does not re-embed. Same test, same counter:
seed two calls, change one, assert the second tick embedded exactly **one**.

**Gate 7 (G3).** A stale call behind a full page of fresh calls is still reached.
`batch_size=2`, five calls, the stale one last — the call-side twin of
`test_a_stale_profile_behind_a_full_fresh_page_is_still_reached`.

**Gate 8 (G4).** Editing `build_call_text` invalidates every call fingerprint.
Monkeypatch the composer, run the tick, assert every call re-embedded. This is
route 3 from the spec and the reason D2 hashes output.

**Mutation M3** — restore the SQL narrowing (`if not force:` back on `call_q`).
Expected: Gate 5, 7 and 8 red; Gate 6 green.
**Mutation M4** — mark every row stale unconditionally. Expected: Gate 6 red;
Gates 5, 7, 8 green. This is the negative control, and it is the one that catches
a "fix" that simply re-embeds the world.
**Mutation M5** — leave `examined` summing `calls_embedded + calls_failed`.
Expected: Gate 7 red **only**. If M5 reddens nothing, Gate 7's corpus fits in one
page and the gate is vacuous — grow the corpus, do not accept it.

M5 is the mutation that matters. Everything else in this task is mechanical; the
drain loop is where the bug would be reintroduced silently.

---

## Task 5 — the call cursor

**Files:** `src/grant_lab/embeddings/refresh.py` (the flag),
`src/grant_lab/scheduler.py` (`_embeddings_tick`).

### 5a. `call_scan_exhausted`

Mirror `profile_scan_exhausted`: set on the non-force drain only, overwritten by
every batch, so what survives the loop is the terminating batch's answer.

```python
totals["call_scan_exhausted"] = summary["calls_seen"] == 0
```

Document, as the profile flag already does, that the joint `scan_exhausted`
cannot stand in for it: a tick held at `max_batches` by the **profile** half
reports False while the call scan ran off the end of its own table on the first
batch.

### 5b. The scheduler

`_embeddings_tick` today passes `after_profile_id` and nothing else, so the call
half restarts from id 0 every tick. Add an `embeddings_after_call_id` nonlocal
alongside the existing profile one, pass it, and reset it on
`call_scan_exhausted` — **only** on that flag:

```python
embeddings_after_call_id = 0 if summary["call_scan_exhausted"] else summary["max_call_id"]
```

A cursor that never returns to 0 is a worse bug than the one being fixed: a call
that goes stale *behind* the high-water mark becomes permanently unreachable.
That is the reasoning already written next to the profile cursor; do not restate
it there, extend it.

**Gate 9 (G6).** A capped tick resumes the call cursor instead of restarting at
0. Use the existing `tiny_tick` technique — monkeypatch only `batch_size` and
`max_batches` (1 and 3) on the real `refresh_missing_embeddings_tick`, so the
production closure and the real database are what is measured. `tiny_tick`
records `after_profile_id`; it needs to record `after_call_id` too, or a twin
fixture that does.

**Gate 10 (G7).** An exhausted call scan resets the cursor to 0, so a call that
goes stale behind it is revisited on the next tick.

**Gate 11 (G8).** A tick capped by the **profile** half still resets the **call**
cursor — the mirror of the existing GATE 9c, which pins the same bug in the
opposite direction. Corpus: enough always-stale profiles to hold the drain at the
cap, plus a small number of fresh calls.

**Mutation M6** — key the call cursor on the joint `scan_exhausted`. Expected:
Gate 11 red only. This is the exact regression GATE 9c was written for on the
profile side; if it reddens nothing, Gate 11's corpus is not actually holding the
drain at the cap — check the recorded cursor sequence, which is why `tiny_tick`
yields the list rather than a bool.

**Mutation M7** — do not pass `after_call_id` at all (today's behaviour).
Expected: Gate 9 red only.

**Also confirm the four existing cursor gates in
`tests/test_scheduler_embedding_cursor.py` still pass unchanged.** Their premise
is a call half that keeps producing rows, which Task 4 makes more true, not less
— but GATE 9c's fixture asserts specific cursor sequences and must be re-read,
not assumed.

---

## Task 6 — verification and the commit

Run, in this order, and paste the real output into the commit message:

```bash
cd /home/parlar_ai/dev/grant-lab
uv run ruff check .
uv run ruff format --check <only the files this slice touched>   # never a DIRECTORY
uv run python scripts/gen_architecture.py && git diff --quiet docs/ARCHITECTURE.md
uv run python scripts/gen_no_build.py --check
set -o pipefail; uv run pytest -n 16 --dist loadfile -q --timeout=300
```

**`ruff format` on a directory reformatted 57 untouched files** into a working
tree once. Name the files.

The expected total is the spec §7 baseline plus the tests this slice adds.
**Measure it; do not compute it and then adjust the baseline to match.** The
`1 skipped` must stay exactly `1` — it is the real-SDK test gated on
`ANTHROPIC_API_KEY`, and `0 skipped` means a live paid API call ran.

Frontend is untouched by this slice. Say so in the commit rather than running it
and implying a relationship.

### Deployment note for the commit body

The first `embeddings:refresh-missing` tick after this deploys re-embeds **every
call**, because migration 0080 leaves every fingerprint NULL and NULL means
stale. That is the deliberate choice in spec D4, and it is one pass over the
corpus against a local Ollama. Say the number measured at deploy time, not the
number in the spec.

---

## Sequencing

Tasks 1→2→3→4→5 are strictly ordered; each depends on the one before.

**Task 4 must not ship without Task 5.** Alone, it makes the call half examine
every row while the cursor restarts at 0 every night — a ceiling of
`batch_size × max_batches` rows per tick with nothing beyond it ever reached. At
the measured corpus size no test would notice, which is exactly why the ordering
is written down here rather than left to judgement.

---

## What would make this plan wrong

Recorded so a reviewer can attack the premises rather than the prose:

- **If some other writer of call text exists.** The spec claims `upsert_call` is
  the only one, from a grep that found **zero** `.summary =` / `.full_text =`
  assignments anywhere in `src/`. A raw SQL `UPDATE calls SET summary = ...`
  would not have shown up in that grep's first form — the second form searched
  for it and also found nothing. Re-run both before trusting route analysis.
- **If the corpus is much larger in production than on the dev box.** Every
  cost argument here rests on the spec §3 measurement (58 calls, 0.08 MB). If
  production is orders of magnitude larger, D4 (no grandfathering) and D5
  (Python-side decision over a full scan) are both worth revisiting — D4 first,
  because a corpus-wide re-embed on first tick is the visible cost.
- **If `FINGERPRINT_VERSION` is bumped in the same release.** D3 shares the tag.
  A bump plus this migration is one re-embed, not two — but only if they land
  together. Landing them in different releases buys an unplanned second pass.
