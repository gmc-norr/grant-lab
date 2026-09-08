# EU Portal ingestion — the v3.0 API is gone, and the replacement is a different shape

**Status:** built. Base: master `060a2022`. Branch `fix/eu-portal-search-api`.
**Origin:** "can you try to understand why the eu portal fails?" — the source had
never once succeeded.

---

## 1. What was actually wrong

`eu_portal` had **never** ingested a row. Measured on the live dev database:

```
last_fetched_at        NULL
consecutive_failures     14
```

The single HORIZON call visible in the dashboard is a seeded dev fixture
(`url = https://ec.europa.eu/seed/dev-only-001`), not an ingested record. Only
`vinnova` was feeding the corpus.

The cause is one line. The adapter posted to:

```
https://ec.europa.eu/info/funding-tenders/opportunities/api/v3.0/notices/search
→ 404
```

while `https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home`
returns 200. The portal is up; the API moved.

The old code carried a comment saying the version was pinned "so a portal-side
rev to v4 is a deliberate code change". The rev happened. Nothing was
deliberate, because **nothing surfaced the failure** — a counter incremented in
a table no screen renders. That is the more general finding, and it is not
fixed by this slice: see §6.

## 2. Where it went

The portal's own SPA reads from the Commission's shared SEDIA search API:

```
POST https://api.tech.ec.europa.eu/search-api/prod/rest/search?apiKey=SEDIA
```

Live on 2026-09-08: **1214** open + forthcoming grant topics.

Three properties of that API are easy to get wrong, and all three were found by
running it, not by reading about it:

**`apiKey=SEDIA` is not a credential.** It is the database selector; the
portal's public JavaScript sends the same literal string. There is nothing to
configure, store or rotate.

**The `query` and `languages` multipart parts must be typed
`application/json`.** Sent untyped, the API answers

```
HTTP 500 {"type":"throwable","message":"An internal error occurred"}
```

which reads as an outage and is actually a request the server declined to
parse. This cost the most time to find, so it gets its own gate (G1). In httpx
the typing is the 3-tuple `(None, value, "application/json")` — the leading
`None` suppresses the filename parameter, matching curl's
`-F 'query=...;type=application/json'`.

**Every useful field lives under `metadata`, as a single-element list, and the
top-level `title` is `None`.** The old parser read `item["title"]`. Against the
new payload that key exists and holds `None`, so every record would be dropped
as incomplete — silently, as an `info` log, with the run reported as a success
that found nothing. Mutating the fix back to `item.get("title")` reddens **25
of 32** tests; that breadth is the point, because the failure mode is "zero
rows", not "one wrong field".

## 3. Which slice of the index to take

`type` partitions SEDIA. Measured live with the open+forthcoming status filter
applied:

| `type` | rows | what it is |
|---|---|---|
| 1 | 1214 | grant topics — **this is what we want** |
| 2 | 25 | EuropeAid procurement tenders (service contracts) |
| 8 | 168 | legacy "competitive-call" rows |
| 0 | 1539 | rows keyed by opaque UUID |

Only `type=1` carries `descriptionByte`. Sampled records from `type=2` and
`type=8` have **no body at all** and deadlines already in the past
(`DIGITAL-2021-SKILLS-01-SPECIALISED`, deadline 2025-10-31, still flagged
open). Including them would add rows the ranker cannot judge and a user cannot
apply to — noise, not coverage.

Status codes are undocumented in the payload. Decoded by cross-checking
`startDate` against the wall clock over 100 live records: every `31094502` had
already started (24/24) and every `31094501` but one had not (75/76). So
`31094501` = forthcoming, `31094502` = open.

## 4. The page scrape is gone

This is the largest change, and it is a deletion.

The old adapter fetched each topic-detail HTML page separately to fill
`full_text` — serially, blocking, one HTTP request per call. That needed a
per-tick scrape cap (300), a wall-clock budget (300s), a `needs_body`
predicate, and a second injected transport, all so a first run would not take
hours.

**The search response already contains the body.** `metadata.descriptionByte`
is present on 100/100 sampled records, median 4945 characters. So a full ingest
is now **13 requests** (1214 topics at the server's hard page ceiling of 100),
not 13 + 1214.

`needs_body` is still accepted, because the `SourceAdapter` protocol passes it,
and ignored, because there is no second request to skip. A gate asserts the
request count stays at one per page (G3) — that is what would catch a
regression reintroducing per-call fetching.

**Do not flatten the description with `trafilatura`,** even though this package
already depends on it and uses it in `sources/extraction.py`. Trafilatura is
tuned for article pages; measured on a real `descriptionByte` it returned 4115
characters out of 5058 and **dropped every `<ul>` bullet**. The bullets are
where the expected outcomes are — the single most rankable part of a call. lxml
with block-level line breaks keeps all of it.

`topicConditions` and `supportInfo` are deliberately **not** appended to the
body. Both are administrative boilerplate repeating near-verbatim across calls
(2 distinct openings across 5 sampled records). Folding them in would push the
same thousands of characters into every call's embedding and make the corpus
*less* separable, which is the opposite of the point.

## 5. Two field mappings that are easy to get subtly wrong

**Budget must be the per-grant contribution, scoped to this topic.** Two traps,
one inside the other.

The first: `budgetOverview` offers both a yearly envelope (`budgetYearMap`,
e.g. 51 251 165) and per-grant bounds (`min`/`maxContribution`).
`Call.budget_min/max` are matched against the *same fields on a research
profile*, where they mean "the size of project this group wants to run". The
envelope is the programme's total. Storing it would compare a programme against
a project.

The second was caught only by running the finished adapter against the live
API: `budgetTopicActionMap` is keyed by the **parent call's** topics, so a
topic's record carries all its siblings' budgets too. Reading the whole map
gave `HORIZON-CL5-2027-03-D5-05` and `-D5-06` the identical range
2 000 000 – 13 500 000 — the parent call's span, belonging to neither topic.
Each action's `action` string begins with the identifier it belongs to, and on
a 100-record sample **every** topic matched at least one action that way. After
scoping, 12 consecutive live topics yield 10 distinct budget pairs.

Zero means "not published", not "zero euros", so it maps to NULL. A wrong
number here is worse than a missing one: the profile match would use it
silently.

**Keywords must drop the identifiers.** `metadata.keywords` leads with the
topic's own identifier and its parent call's identifier before any subject
term. Those are already stored as `external_id`; repeating them would put the
identifier into the embedding text twice and tell the ranker nothing.

**There is no abstract.** Item-level `summary` and `content` both hold the
parent call's *title* ("MSCA COFUND 2027"), which describes nothing. Rather
than synthesise one, `summary` quotes the first 500 characters of the body
verbatim, cut on a word boundary and marked with an ellipsis. Coverage is
100/100; a heuristic that hunted for a "Scope:" section reached only 99/100 and
invented structure the payload does not promise.

**Programme name is derived from the identifier prefix.**
`metadata.frameworkProgramme` is an opaque numeric code (`43108390`) with no
label anywhere in the payload. The prefix of `identifier` tracks it exactly: all
89 records coded `43108390` were the 89 whose identifier began `HORIZON`. A
prefix table needs no reference-data call to stay in sync, and an unmapped
programme degrades to its own prefix rather than to NULL.

## 6. Deliberately out of scope

**Nothing surfaces a dead source.** This is the finding with the longest reach
and it is *not* fixed here. `sources.consecutive_failures` reached 14 with
`last_fetched_at` still NULL and no screen, digest or alert said so. The same
silence would hide the next endpoint move. `STALE_SOURCE_ALERT_DAYS` exists and
is keyed on `last_fetched_at` being *old* — a source that has never fetched at
all has a NULL there, so it is unclear whether that alert can fire for this
case. Worth its own slice; needs measuring first.

**Re-ingesting the existing corpus.** The upsert is idempotent and ingestion
enqueues ranking jobs, so the first run after this merge will rank ~1200 new
calls against the active profile. That is a scheduling and cost question
(`RANKING_ESCALATION_MIN_SCORE`, the 100/day remote cap), not an adapter
question, and it should be a decision, not a side effect.

**A reaper for calls that vanish from the feed.** The pre-existing TODO stands
unchanged; this slice neither helps nor hinders it.

## 7. Gates

Each was run against something it must reject. `RED` lists what actually went
red when the fix was mutated away — observed, not predicted.

| # | Gate | Mutation | RED |
|---|---|---|---|
| G1 | multipart parts are typed `application/json` | drop the type from the `query` part | 1 |
| G2 | query filters to `type=1`, status open+forthcoming | widen `type` to include `2` | 1 |
| G3 | one request per page; no per-call scrape | — (asserts request count) | — |
| G4 | title is read from `metadata`, not top level | read `item["title"]` (the pre-fix parser) | **25** |
| G5 | `<ul>` bullets survive flattening | swap lxml for `trafilatura.extract` | 4 |
| G6 | budget is per-grant, not the yearly envelope | read `budgetYearMap` | 2 |
| G7 | budget excludes sibling topics of the parent call | drop the owner filter | 2 |
| G8 | keywords drop the identifiers | keep every entry | 2 |
| G9 | conditions boilerplate stays out of the body | append `topicConditions` | 1 |

Two mutations were run and **did not** redden, which is recorded because it
changed the code:

- *"read the top-level title"* written as `item.get("title") or _first(...)` is
  a no-op — the `or` falls through. Rewritten as `item.get("title")` alone, it
  reddens 25 tests. A mutation that does not fail is not evidence about the
  gate; it is usually evidence about the mutation.
- *"drop the `+0000` offset normalisation"* changed nothing, because Python
  3.12's `fromisoformat` accepts that form directly (verified against the
  interpreter, alongside `+00:00` and `Z`). The regex was dead code justified by
  a false comment about pre-3.11 behaviour, so it was **removed**.

## 8. Baseline

`eu_portal` test count, collected on this branch's base and on the branch:

```
060a2022  tests/test_sources_eu_portal.py   15 collected
branch    tests/test_sources_eu_portal.py   32 collected   (+17)
```

Backend total on the base is **5530 passed, 1 skipped**. That figure was
measured at `51c0fcb6`; `git diff --stat 51c0fcb6 060a2022` is two documents
and nothing else, so it carries to this base rather than being re-asserted.
Expected total on the branch is therefore 5547.

The `1 skipped` is the real-SDK test gated on `ANTHROPIC_API_KEY`. It stays 1 —
`0 skipped` would mean a live paid API call ran.
