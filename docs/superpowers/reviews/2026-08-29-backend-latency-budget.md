# Backend Latency Budget — and the Rust/Cython Question

**Date:** 2026-08-29
**Question asked:** which parts of the backend (excluding LLM calls) are worth converting to Rust or Cython to speed up UI↔backend interaction?
**Answer:** **none of them.** Not one line.
**Method:** 60 agents; a real profiling harness (testcontainers Postgres + the production `create_app` + seeded data + cProfile); every candidate above 10% of any request adversarially judged.
**Code changes:** none. This is a measurement artifact.

---

## 1. The gate this went through

Before any mechanism was proposed:

- **Governing principle.** A rewrite only pays if the thing rewritten is a *measured*
  majority of the time a human waits.
- **What would kill it.** If the time is in DB round-trips, subprocess spawns, or
  already-native libraries rather than app-level Python CPU, Rust and Cython are the
  wrong tools and the answer is "don't".
- **The trivial baseline.** Delete the work instead of speeding it up.

The baseline won. That is the whole report in one line.

---

## 2. Scale sanity check, first

| | |
|---|---|
| `src/` Python | 54,571 lines (42,132 non-blank/comment) |
| This box | 57.4 M simple statements/s; 26.6 M function calls/s |
| **Running every line in the project once** | **≈ 0.7 ms** (derived from the two measured figures) |
| Whole-app import | 3.35 s — **once at boot**, not per request |
| One `git` subprocess spawn | 1.33 ms floor, degrading to 7.36 ms at 32 threads |

Codebase *size* is not a latency variable. Executing all 42,132 lines costs less
than one `git` command. What costs time is **boundary crossings** (DB, subprocess,
network) and **N** (loops over lots of data).

---

## 3. Where the time actually goes

Measured with the harness's breakdown loop: 300 calls seeded, n=40 per endpoint,
no profiler. `SOCK` timed at the psycopg socket, `GIT` at the subprocess, `PY` by
subtraction (and it **still contains the framework floor**).

```
  WALL ms  SOCK ms  GIT ms    PY ms  SOCK%   GIT%    PY%  ENDPOINT
    42.25    18.10    0.00    24.15  42.8%   0.0%  57.2%  /api/calls (50 rows)   <- SPINNER
    41.01     8.15    2.15    30.71  19.9%   5.2%  74.9%  /draft                 <- SPINNER
    11.41     3.13    0.00     8.29  27.4%   0.0%  72.6%  /cockpit-meta          <- SPINNER + GATE
    42.22    14.06    0.00    28.16  33.3%   0.0%  66.7%  /problems (15s poll)
    15.97     2.28    4.92     8.76  14.3%  30.8%  54.9%  /history/{sha}
     2.82     0.00    0.00     2.82   0.0%   0.0% 100.0%  /__perf_noop  <- framework floor
```

### Only three endpoints ever show a human a spinner

Proved structurally, by instrumenting the real `<App/>` tree and **breaking things
to see what stopped**:

- App boot and the calls dashboard = **2 requests**, zero per-row fan-out.
- Opening a cockpit = **13 requests** (12 HTTP + 1 WebSocket).
- **There is exactly one waterfall in the entire app.** Hanging
  `GET /cockpit-meta` drops the cockpit from 13 requests to **7** — five
  bottom-panel queries *and the collab WebSocket* never fire. Control: hanging
  `/draft` instead still yields 12, so `/draft` is a leaf, not a gate.
- **Typing costs zero HTTP.** The keystroke path is pure WebSocket → pycrdt, which
  is already Rust.

### The fixed tax is large in relative terms

On `cockpit-meta` — the app's only true gate — **24.7% is a do-nothing framework
floor** and **18.9% is the membership check**. 43.6% spent before any work happens.

---

## 4. The verdict: do not write Rust or Cython

Every candidate clearing 10% of any request, and why the rewrite dies:

| Candidate | Measured share | Why it dies |
|---|---|---|
| `pgvector.Vector.from_text` | **22.9%** of `/api/calls` @200 rows | Highest share in the packet — but `defer()` removes the work entirely, measured **59.5–62.7%** off the same request, for one line |
| `markdown_bridge._escape_text` | 11.9% of `/draft` | A **16.1× pure-Python fix** exists; after it there is nothing left to take, and 687 FFI crossings per doc would cost more |
| `validate_citations_deterministic` | 86.9% of `/citation-audit` | **94.1% is DB round-trips** — with rows pre-cached the checks are 3.28 ms of 55.29 ms. Rust ceiling **6.6%** |
| `_citation_map` on `/draft` | 30.8–72.7% | cProfile: sqlalchemy 42.2% + psycopg 39.9%. Rust ceiling **0.17–0.59%** |
| `gitio.blob_sha` + `read_blob` | 29.5% of `/history/{sha}` | It is `fork`+`exec`. Cython cannot touch a subprocess |
| `markdown_to_pm` | 28.6% of `/draft`, log-empty branch only | 86.4% is markdown-it-py, 10.4% prosemirror-py; **our code is 0.8–2.1%**. And the branch self-extinguishes at first room hydration |
| `ydoc_to_pm_json` | 6.2–12.4% | **67.2% of its tottime is pycrdt's own Python wrapper**, one level below us |
| `_normalize_with_map` | 5.5% @120 cites, **~1.9% at the real 7-citation scale** | The only textbook Cython shape in the repo — on a request nobody watches |
| Collab keystroke path | **0% HTTP** | Already pycrdt (Rust) + one INSERT |
| Diff rendering | **0.000 ms backend** | `grep -rn difflib src/` = 0 hits. The diff is 233 lines of **browser JavaScript** (`frontend/src/cockpit/diff.ts`), 1.7–2.0 ms |

**The pattern is uniform:** every component above 10% is a DB round-trip, a
subprocess, a third-party pure-Python library, or work that should not happen at
all. The one place our own Python dominates is `_escape_text`, and a one-line pure
Python change beats anything Rust would buy.

### Already-native, already fine

pydantic-core `to_json` = **0.320 ms** at 200 rows (0.52%). Full
`serialize_response` = 1.391 ms (2.14%). gzip = 3.03 ms (2.12%). Response-model
re-validation = 0.001 ms. Total already-native ≈ **5%** of `/api/calls`.

**Do not swap in orjson:** measured **−0.036 to +0.741 ms**, indistinguishable from
zero. pydantic-core's Rust encoder is already on that path.

---

## 5. What would actually make the UI faster

Ranked by measured win per unit of risk.

### 5.1 `defer(Call.embedding)` — one line, the largest win in the packet

`dashboard.py:283-297` issues `select(Call, CallRanking)` with no `defer` /
`load_only`. `calls.py:404-416` then builds `CallOut` from **eight scalar fields**:
`id, title, deadline, deadline_local_date, published_at, source_name, source_slug,
aggregate_score`. `embedding`, `full_text` and `raw` are never read.

So every row ships a 1024-dim vector as **9,217 characters of text**, and
`pgvector.Vector.from_text` — a pure-Python listcomp with no compiled extension —
parses all 1024 floats. Verified directly for this document:

```
one 1024-dim vector as text = 9217 chars
  parsing  50 rows' embeddings:    7.33 ms   <- thrown away
  parsing 200 rows' embeddings:   28.40 ms   <- thrown away
```

(Independently reproduced: the harness agent measured 7.04 / 28.18 ms.)

**Interleaved A/B on the same route in the same process, byte-identical payloads asserted:**

| | before → after |
|---|---|
| limit=200 | 123.0 → 45.9 · 131.2 → 53.1 · 95.5 → 36.3 ms |
| limit=50 | 37.5 → 20.6 · 33.9 → 19.2 · 40.1 → 27.8 ms |

**30.7–62.7% off the only endpoint the calls dashboard waits on.** It also removed
**21 ms of "database time"** (33.4 → 12.2 ms socket) — most of the DB cost was
transferring vectors nobody reads. Risk: near zero.

> This is why the answer to the Rust question is "no". The single biggest
> Python-CPU item in the app is not slow code. It is **fast code running on data
> that should never have been loaded.**

### 5.2 Remove the `cockpit-meta` waterfall — the biggest *perceived* win

`routes/Cockpit.tsx:69` returns a spinner until `useCockpitMeta` resolves, so five
bottom-panel queries **and the collab WebSocket** are serialized behind an
8-scalar-field payload. Hoist the phase check so `BottomPanel` and `CollabEditor`
mount optimistically, or fold `current_phase` into a payload already being fetched.

**NOT MEASURED:** the size of the win — the harness is in-process and has no
network RTT. On a real network this saves one full round trip for six requests plus
the WebSocket handshake. No backend work of any kind substitutes for it.

### 5.3 Batch the citation N+1 — for server capacity, not for the user

`citations/audit.py` + `defer(ReferenceChunk.embedding)`: **55.287 → 11.441 ms
(4.83×)** at 40 citations. Drives `/citation-audit` (86.9%) and `/problems` (85.2%,
polled every 15 s per open cockpit).

**Reality check, do not skip it:** production scale is **7 citations across 16
workspaces**; at 12 citations the whole validator is 17.6 ms. Nothing renders behind
`/problems` — it fills a tab badge. Do this for CPU, not latency. Same pattern
applies to `_citation_map` on `/draft` (nine `session.get(Reference)`) and
`reader.find_by_id` (an O(markers × rows) scan a dict index fixes in 3 lines).

### 5.4 Cache `canonical_artifact_markdown`

Round trip **12.223 ms**; cache hit **0.000144 ms**. Key on
`(workspace_id, artifact, head_sha)` — `get_draft` already computes `head` at
`workspaces.py:283` before calling it at :294. Serves `get_spec`, `turns.py:154`,
`annotations.py:125`, `staleness.py:34` too. **Risk: moderate** — this function
defines canonical normal form; a wrong key serves a stale SHA basis.

### 5.5 `_escape_text` → chained `str.replace`, backslash first

Still unfixed in the tree — `markdown_bridge.py:368` reads
`return "".join(_INLINE_ESCAPE.get(ch, ch) for ch in text)`.

```
WHOLE-DOC 24719 chars  genexpr      1.5247 ms/call
WHOLE-DOC 24719 chars  translate    2.1506 ms/call     <-- SLOWER
WHOLE-DOC 24719 chars  chained      0.0945 ms/call     <-- 16.1x
fuzz mismatches: 0   (200,000 cases)
```

> **Do NOT ship `str.translate`.** Two adversarial judges recommended it; one
> reported a "measured 20.4% end-to-end win". The micro-benchmark says it is a
> **regression** on both string shapes, and the A/B ran on a box at load average
> 8–10 with ~10 competing Postgres containers. Treat that A/B as drift until
> re-run. See Hole 4.

Caveat: `/draft` is a **leaf, not a gate** (proved), so this shortens the editor
pane's own spinner and nothing else.

### 5.6 Delete the pointless git spawn

`web/api/annotations.py:101`:

```python
current_head(repo)  # validate the repo has a commit; raises on pre-draft
```

The return value is discarded. **2.15 ms = 10.5% of `GET /annotations`** for a
fork/exec that computes a boolean. Also: `write_and_commit_files` runs
`git rev-parse HEAD` *again* after committing, when `git commit` could report the
sha — **4.221 ms of redundant rev-parse per checkpoint**.

### 5.7 Batch the two spawns on `/history/{sha}`

2 spawns = 3.59 ms = 29.5%. A `git cat-file --batch`, or pygit2 (measured 0.088 +
0.172 ms, saving 3.63 ms / 23.9%). **But:** pygit2 measurably *loses* on
`list_commits` at 500 commits (3.259 vs 2.960 ms), it is not installed, and all its
numbers come from a throwaway venv. A port must reproduce `gitio.py`'s rollback
semantics, `GIT_CEILING_DIRECTORIES` pinning, NUL-framing subject cap and the 30 s
timeout — none of that is measured.

### Explicitly do not do

- **orjson for responses** — measured indistinguishable from zero.
- **Rewrite gzip** — already C, 2%.
- **Optimize `POST /sections/{index}`** — measured **dead**: it returns 409 for any
  workspace whose draft has ever been read, because `collab/flip.py:40` flips collab
  on during the first `GET /draft`.

---

## 6. The holes — what could change this answer

Recorded so nobody reads this report as more certain than it is.

| # | Hole | Why it matters |
|---|---|---|
| **1** | **No concurrency measurement anywhere.** **103 of 113 routes are plain `def`**, so they run in Starlette's threadpool (capacity 40). Every number here is single-request, serial, one user. Isolated git spawn latency already degrades **4.8× from 1 to 32 threads**. | **This is the biggest hole and it could invert the answer** — threadpool saturation is a latency source no per-function rewrite can fix |
| 2 | The TestClient floor (1.76–2.94 ms of httpx + anyio portal) contaminates every `PY ms` figure — 24.7% of cockpit-meta | Re-measure over a real uvicorn socket |
| 3 | Production volumes unknown. Real: **7 citations, 16 workspaces, repos of 1–15 commits.** Everything at 40/120 citations and 200/500/1000 commits is synthetic | Several "wins" may be for scale you don't have |
| 4 | The `str.translate` contradiction is live — one judge's A/B contradicts the micro-benchmark | Re-run three-armed and interleaved on an unloaded box |
| 5 | `markdown_to_pm` is 10.46 ms in-request vs 3.649 ms isolated — a 2.9× gap, two hypotheses tested and refuted | All `/draft` CPU fractions are ranges, not points |
| 6 | **The WebSocket keystroke path has never been timed.** "It's pycrdt + one INSERT" is a *reading*, not a measurement | Highest-frequency interactive path in the app |
| 7 | POST paths thinly covered (`accept` measured at 90.18 ms; `checkpoint`'s denominator is admittedly inflated) | |
| 8 | APScheduler was `.pause()`d throughout. ~25 interval jobs share the process and pool in production | |
| 9 | `jsonable_encoder` cost unmeasured on the five routes that actually take that path | |
| 10 | Everything measured a **loopback** Postgres. A production DB on another host has strictly higher socket time | Moves every fraction *away* from Python — **nothing here overstates the case for Rust; everything understates the case against it** |

---

## 7. Conclusion

The backend is not slow, and where it is slow it is not slow for reasons Rust or
Cython address. The slowest endpoint a user waits on is **42 ms**, of which roughly
half is the database and a quarter is framework floor. The single largest
Python-CPU item in the whole application is a pure-Python vector parser doing
perfect work on **9,217 characters per row of data the response never contains**.

The correct response to that is not a faster parser. It is `defer()`.

**If you want the UI to feel faster, in order:** stop loading the embeddings; break
the `cockpit-meta` waterfall; then measure the app under concurrency, because
Hole 1 is the only thing here big enough to change the conclusion.
