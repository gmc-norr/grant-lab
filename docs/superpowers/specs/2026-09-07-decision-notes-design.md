# Decision notes — the user's own reason, attached to the call they hid

Date: 2026-09-07
Status: rev 3, design approved, not yet planned.
Origin: suggestion 4 of an outside product review ("lightweight feedback"), the
last of that review's four *Useful Additions* to be built. The other three are
board health (merged), saved-call follow-up (blocked — see §8) and visible
matching status (deferred).

**Rev 2** answers an outside review of rev 1: six Medium findings and four
open questions, all confirmed against the code, none refuted. §9 records what
changed and why. Rev 1 got one load-bearing mechanism wrong.

**Rev 3** settles the edition question rev 2 left open. The deployment is
`match` only and `full` is the next version, so §5's table has one shipping
column — and the golden-set page must be mounted in `match` for this feature to
close its loop. Rev 2 under-quoted that work; §10 measures it.

## 1. The problem

A hide is the strongest signal this system collects about a mismatch, and it is
recorded as one bit. The user knows *why* — wrong field, budget too small, the
group is ineligible, someone already applied — and today there is nowhere to put
that. It evaporates.

It evaporates in the one place it is worth most. `ranking/golden_set.py`
already mines high-scoring calls a human hid as evidence that **the model scored
this too high**, via `dismissed_high_score` (calls scoring ≥ 60 with a `hidden`
decision on the same profile). Those candidates are the raw material for the
golden set — the golden set that `reviews/2026-09-03-matching-method-and-release-split-review.md`
found **empty** (`golden_set_last.json` is `n_fixture: 0`), which is why nearly
every ranking-tuning question in this repo is currently unanswerable.

The person who later labels a candidate sees the call title, the profile name,
the score and the mining reason. They do not see why a human rejected it. They
guess.

## 2. What already exists

Measured against master at `7afd8c23` on 2026-09-07. Every claim below was run,
not reasoned. Rows marked **(rev 2)** correct or add to rev 1.

| Fact | Where | Evidence |
| --- | --- | --- |
| The column exists | `call_decisions.note`, `Text`, nullable | created by `alembic/versions/0071_call_decisions.py` |
| Nothing writes it | `web/api/calls.py` | `grep -c '\bnote\b'` returns 0 |
| It was created for exactly this | `CallDecision.note` comment | *"NOTHING WRITES THIS IN RELEASE 1 — the column is created now so the note UI is a frontend change later, not a migration."* |
| The spec that created it named the purpose | `specs/2026-09-03-call-match-release-1-design.md` | table row: `note \| text, nullable \| free text, the user's reasoning` |
| Hides already feed the golden set | `ranking/golden_set.py` | the `dismissed_high_score` query: `aggregate_score >= 60` + `EXISTS` a `hidden` decision |
| The other two candidate queries need no decision | `candidates_cosine_disagreement`, `candidates_escalation_disagreement` | neither mentions `CallDecision` |
| Golden-set candidates carry no user | `CandidateRow` in `ranking/golden_set.py` | `call_id, profile_id, call_title, profile_name, aggregate_score, embed_similarity, reason` |
| Four triage views, one query | `GET /api/calls?view=` | `Literal["archive", "inbox", "saved", "hidden"]` |
| The view **filter** is a subquery, never a join | `web/dashboard.py::_decision_predicate` | `scalar_subquery()` over `CallDecision.call_id` |
| **(rev 2) But the saved/hidden ORDERING already JOINS the decision row** | `build_dashboard_view`, the `decision_ordered` branch | `.join(CallDecision, call_id & user_id & profile_id & state)` then `.order_by(CallDecision.decided_at.desc(), Call.id.desc())` |
| **(rev 2) `decision_ordered` implies the join exists** | same function | `profile_obj is None` sets `profile_id = None`, so `profile_id is not None` and `profile_obj is not None` are the same condition; `decision_ordered` can only be true inside that branch |
| **(rev 2) The full router does NOT mount `TriageFeed`** | `router.tsx` index route is `<Dashboard/>` | `TriageFeed` is imported only by `router.simple.tsx` |
| **(rev 2) `CallDetail` IS mounted in both routers**, and can hide | `calls/:id` in both; `useHideCall` + `useUnhideCall` | it already keeps `useState<{id, profileId}>` to make Undo delete the right row |
| **(rev 2) `CallRow` and `CallReadingPane` offer no decisions** | grep for hide/save | no hits |
| **(rev 2) `CandidateOut` is the serialization boundary** | `web/api/admin_golden_set.py` | a Pydantic model with seven fixed fields; a new dataclass field is silently dropped |
| **(rev 2) The hide/save upserts do NOT touch `note`** | both `on_conflict_do_update` calls | `set_` is `{state, decided_at, decided_by}` — so today a note survives a flip |
| There is no toast system | `frontend/src` | `grep` for `notifications\|showNotification` returns nothing outside tests |
| The keyboard guard already exists | `isEditableTarget` in `TriageFeed.tsx` | covers `<input>`, `<textarea>` and non-false `[contenteditable]` |
| A query-count gate pattern already exists | `tests/web/test_calls_list_query_count.py` | `test_the_list_costs_the_same_number_of_statements_at_1_and_10_calls`, plus its own vacuity guard |

**The single most important consequence: this needs no migration.** The column
has been in the database since 0071. The work is an endpoint, a response field,
a serializer field and a frontend.

## 3. Decisions

### D1 — Free text, not reason codes

The note is free text. There is no closed vocabulary of reasons.

Rejected alternative: a fixed list (`wrong field` / `too small` / `not eligible`
/ `bad timing`). It is countable, which is genuinely attractive, and it is what
a later revision may well want. It loses now for two reasons. It needs a new
column and therefore a migration, which contradicts the column that already
exists for this. And nobody in this repo knows what the right vocabulary is —
**a wrong vocabulary is worse than none**, because it silently coerces every
answer into the nearest wrong box and the mistake is invisible in the
aggregate.

The correct order is to read a few hundred free-text notes and let them name the
categories. That is the same principle the matching-method review applied to
ranking: *no filter without an audit sample*.

### D2 — The note is written on a strip, immediately after the decision

Pressing `h` removes the card from the list (§2). There is therefore no card to
attach a note to, and an inline editor on the card would be racing its own
disappearance.

Instead the feed renders one strip at the top of the list, replacing nothing:

```
Hidden "ERC Synergy Grant 2027" — why?  [________________]
```

It is **ignorable**. `j` / `k` / `s` / `h` keep working while it is on screen;
the next decision replaces it. Clicking into it is opt-in, and `isEditableTarget`
already stands the global key listener down for a focused `<input>` — the guard
this needs is the guard that is already there.

The precedent is in the codebase: `CallDetail` already renders an
after-the-fact Undo affordance and already remembers `{id, profileId}` for it,
precisely so a later action lands on the row it was made for.

Rejected alternative: notes only in the Saved and Hidden tabs. Zero risk to the
triage loop, and trivial to build — but people do not go back, so the capture
rate would be near zero, and a reason is most accurate in the two seconds after
the decision. The tab-side editing is built anyway (§5), so that option is a
subset of this one rather than a real fork.

### D3 — Private from other researchers; readable by administrators, unattributed

**(revised in rev 2.)** Rev 1 called this "private" and also promised the
golden-set page would show the text. Both cannot be true, and the second is the
one that carries the feature's value. The honest contract:

- The author sees their own notes.
- **Other researchers never see them**, including colleagues who can read the
  same profile.
- **Administrators, on the golden-set page, can read the text.** They cannot see
  who wrote it: no user id, email or display name is carried.
- **This is disclosed where the note is captured**, in one line next to the
  input. A privacy property the user cannot see is not a privacy property.

Unattributed is not the same as anonymous, and this spec does not claim
anonymity. Free text can name its author, describe a meeting only one person
attended, or be identifying by style. What the system guarantees is that it
**adds no author metadata** — see gate 6, which is written against the fields,
not against the prose.

The reason for keeping it away from other researchers is not squeamishness. It
is that people write honest notes when their colleagues are not watching, and
honest notes are the entire value here. A shared, attributed team annotation is
a different feature and can be built on top of this one.

`CandidateRow` already carries no user id, so the golden-set half of this
preserves a property the code has rather than inventing one.

### D4 — Un-deciding destroys the note

`CallDecision` has no `undecided` state; undecided is the **absence of a row**,
and the model docstring says so explicitly. Un-hiding is a `DELETE`. The note
lives on that row, so it dies with it. Re-hiding the same call later starts
blank.

This is stated because it is the behaviour a reader would most reasonably expect
to be otherwise, and because the alternative — keeping orphaned notes alive for
rows that no longer exist — would require either a soft-delete or a second
table, and would quietly break the "undecided = no row" invariant that the inbox
query (`NOT EXISTS`) is built on.

### D5 — A note requires a decision

`PUT .../note` on a call with no decision row for that `(user, profile)` returns
**404**, not a 201 that invents a decision. There is no state the row could
carry: `state` is `NOT NULL` and constrained to `saved | hidden`, so writing a
note would force this endpoint to guess a decision the user did not make.

### D6 — Naming: `decision_note` on the way out, `note` on the way in

Three distinct things in this codebase are called some form of "note", and two
of them already sit on the objects this feature touches:

| Name | What it is | Where |
| --- | --- | --- |
| `applicant_note_count` | count of `CallRanking.applicant_notes` — the **model's** inferred things-to-check | already on `CallOut` |
| `reason` | why a pair became a golden-set **candidate** (`dismissed_high_score`, `cosine_disagreement`, `escalation_disagreement`) | already on `CandidateRow` and rendered as `<Code>` in `AdminGoldenSet.tsx` |
| the new one | the **user's** own words | this spec |

So: the response field is `decision_note`, and the golden-set field is
`user_notes` (plural — D7). Neither is `note` and neither is `reason`. A
`CandidateRow.reason` that sometimes meant the mining reason and sometimes the
human's reason would be a defect with a very long fuse.

**The request body is `{"note": ...}`, and the asymmetry is deliberate**
(question raised in the rev-1 review). The collision that forces
`decision_note` exists on `CallOut`, which carries `applicant_note_count`
alongside it. The request body has no such neighbour: it is a single-field body
on an endpoint already named `/note`, where `{"decision_note": ...}` only
stutters. Naming is for readers at the point of reading, and the two points of
reading are different.

### D7 — Several people can hide the same call

The unique constraint is `(user_id, profile_id, call_id)`, so one **user** has
at most one note per call per profile — but several users who can read the same
profile may each hide the same call, and the miner's `EXISTS` fires on any of
them. The golden-set candidate therefore carries `user_notes: list[str]`.

- **Only notes on `hidden` decisions are mined.** A `saved` decision can carry a
  note too (D2 offers the strip on save), and letting one into
  `dismissed_high_score` would file "we should apply for this" as evidence that
  the model scored it too high. The state filter is applied **before** the cap,
  not after, or a saved note could push a hidden one out of the window.
- Ordered by `CallDecision.decided_at DESC`, tie-broken by `CallDecision.id
  DESC`. There is no note-edit timestamp and this spec does not add one, so
  "newest" means **newest decision**, not newest edit. A note edited today on a
  decision made in March sorts as March.
- Capped at 3. Unattributed (D3).

### D8 — Length, and empty is null

`max_length=1000` on the request model, in Pydantic, not in the database. About
150 words: generous for a sentence or two of reasoning, bounded so a paste
cannot be unbounded. The column stays `Text`, so raising the cap later is a
one-line change and never a migration.

Precedent in `web/api`: `max_length=200` for search and display names,
`max_length=1024` for passwords. 1000 sits deliberately between them; a reason
is longer than a name and shorter than an essay.

**A note that is empty or only whitespace is stored as `NULL`, not as `""`.**
`note.strip() or None`. Otherwise "has a note" has two answers, the golden-set
list renders blank entries that consumed a slot in D7's cap, and every consumer
has to remember to check both.

### D9 — A flip clears the note **(new in rev 2)**

Today the upserts do not touch `note` (§2), so hiding a call you had saved keeps
the note you wrote while saving it, and vice versa. That is the wrong default.

Both endpoints' `on_conflict_do_update` gain `note=None`. Rationale: the code
already states that a flip **is a new decision** — it moves `decided_at` and
`decided_by` for exactly that reason. D4 already establishes that a note belongs
to its decision and dies with it. A flip is the same principle applied to the
case rev 1 missed; leaving the note would attach one decision's reasoning to a
different decision, and D7's ordering would then date it by the new decision.

Rejected alternative: preserve on flip (zero code change). It loses because
`hide → save → hide` would resurrect a note the user wrote about a state they
have twice left, and hand it to the golden set as fresh evidence.

This changes the two endpoints' **behaviour**, not their wire contracts: no
request or response field moves.

### D10 — What the strip actually does **(new in rev 2)**

Rev 1 specified an input and an endpoint and nothing between them. The contract:

- **It appears only after the decision request succeeds** — on the mutation's
  success, never optimistically. A failed hide must not leave a strip soliciting
  a note for a decision that did not happen.
- **It is bound to the `(call_id, profile_id)` it was created for**, held in
  state, never read from "the currently focused call". This is the `CallDetail`
  Undo precedent, and it is the whole defence against a late write landing on
  the next call.
- **Enter or blur commits. Escape reverts and closes.** No autosave per
  keystroke, and no debounced autosave — a timer that outlives the strip is the
  same bug as an unbound write.
- **An in-flight write survives replacement.** Typing a note, pressing Enter and
  immediately pressing `h` on the next call leaves the PUT in flight; it carries
  its own ids and completes. The strip is replaced, the request is not
  cancelled.
- **On failure the strip stays, keeps the typed text, and shows the error
  inline.** It does not retry on its own. Losing what someone just typed is
  worse than showing them a failure.
- **Navigating away discards an uncommitted draft.** No draft persistence, no
  `localStorage`. Committed notes live on the server; anything else is a second
  source of truth.

## 4. The wire

### New endpoint

```
PUT /api/calls/{call_id}/note?profile_id=N
body: {"note": "wrong field — this is materials science"}   # or null to clear
```

- 404 if there is no decision row for `(user, profile, call)` — D5.
- 404 if the profile is unreadable, via the existing
  `_readable_active_profile_or_404`, whose docstring already explains why 404
  and not 403 (an existence oracle otherwise).
- Idempotent. Writing the same note twice is a 200 both times.
- Whitespace-only becomes `NULL` (D8).
- Does **not** touch `state`, `decided_at` or `decided_by`. A note is not a new
  decision, and the Saved and Hidden lists order by `decided_at` (Phase 2
  decision D12, recorded in `CallDecision.__table_args__` — not in the
  release-1 spec) — bumping it would silently reorder the list under the user as
  they annotate it. This is the natural mistake, because both existing decision
  endpoints *do* set `decided_at=func.now()` in their `set_`; gate 4 exists for
  it.

Rejected alternative: an optional body on `POST /calls/{id}/hide`. It fails on
two counts. The hide fires on a keypress and the note arrives seconds later, so
a second request is needed regardless. And the release-1 spec froze that
endpoint's wire contract on purpose (D14: *"hide is a re-skin of an endpoint
that predates the state column and keeps its wire contract"*).

### New response field — read it off the join that is already there

**(rewritten in rev 2. Rev 1 was wrong.)**

Rev 1 claimed the decision row is absent from today's result set and prescribed
a correlated scalar subselect. That is false. `_decision_predicate` is indeed a
subquery — but `build_dashboard_view` separately **joins** `CallDecision` for
the saved and hidden views, scoped by `call_id`, `user_id`, `profile_id` and
`state`, in order to `ORDER BY CallDecision.decided_at DESC`. Rev 1 read the
filter, generalised to the whole query, and stopped.

So: add `CallDecision.note` to the select list **in the `decision_ordered`
branch only**. No new FROM entry, no subselect, no extra statement.

`decision_ordered` is `view in ("saved", "hidden") and profile_id is not None`,
which is exactly the condition under which the join exists (§2), so this cannot
be reached without its row. `inbox` and `archive` do not join and do not select
the column; `CallOut.decision_note` is `None` there by construction, not by
accident.

`CallOut` gains `decision_note: str | None`.

### Changed serializer — two boundaries, not one

`CandidateRow` (the dataclass in `ranking/golden_set.py`) gains `user_notes:
list[str]`, and **`CandidateOut` (the Pydantic model in
`web/api/admin_golden_set.py`) must gain it too.** A field added only to the
dataclass is dropped silently at serialization — no error, no warning, an
absent key. Then `frontend/src/api/schema.gen.ts` must be regenerated, per the
repo's OpenAPI codegen procedure.

Only `dismissed_high_score` can populate it; `cosine_disagreement` and
`escalation_disagreement` require no decision (§2) and return an empty list, not
a null, so the UI renders nothing rather than branching.

## 5. The UI — and the edition it actually ships in

**(rewritten twice. Rev 1 promised the strip in both editions; it cannot be.
Rev 3 records the human's answer: the deployment is `match` only, and `full`
is the next version, so `match` is the only column that ships today.)**

| Surface | Match — **ships now** | Full — next version |
| --- | --- | --- |
| The strip, after `s` / `h` in the feed | **yes** — `TriageFeed` | no — the full router mounts `<Dashboard/>` at `/` and never imports `TriageFeed` |
| Note on the Saved / Hidden rows | **yes** | no — those views are `TriageFeed`'s |
| Note beside hide/undo on the call page | **yes** | yes — `CallDetail` is mounted in both routers |
| Notes on the golden-set page | **yes, and this requires mounting that page in match** (§10) | yes |

`CallRow` and `CallReadingPane` offer no save or hide at all (§2), so the full
edition's list would get nothing. That is correct rather than a gap: the hide
endpoint requires `profile_id`, and the release-1 spec's D3 deliberately removed
the full archive's global hide. It is also moot for now.

**The strip** — `TriageFeed.tsx`. Plain markup. There is no notification library
in this frontend and `pnpm add` is blocked by the seven-day cooldown
(`minimumReleaseAge: 10080` minutes, in `frontend/pnpm-workspace.yaml`), so
introducing one is not on the table and is not wanted. Behaviour is D10.

It must not steal focus. The whole value of the feed is a keyboard loop that
never stops, and a strip that grabbed the caret would turn `h h h` into typed
text in the first box. `isEditableTarget` handles the other half — keys are
inert *while* the box has focus — and already exists.

**Saved and Hidden rows** — the note renders under the title, click to edit.
This is where a note gets corrected or added late.

**`CallDetail`** — beside the existing hide/undo control.

**Golden set** — `AdminGoldenSet.tsx`, in the candidates table, beneath the
existing `reason` code. Unattributed (D3). Reaching it at all in the shipping
edition is §10's work.

**The disclosure line** (D3) is worded so it is true independent of which
surfaces are mounted: *"An administrator can read this to improve matching.
Your name is not attached."* An administrator has database access regardless of
whether a page exists, so a disclosure phrased around the page would be
misleading in exactly the deployment where it matters.

## 6. What this does not do

- No reason codes or dropdowns (D1).
- **No author attribution anywhere, and no visibility to other researchers.**
  Administrators can read note text on the golden-set page — that is D3, it is
  disclosed at capture, and this spec does not claim the text is anonymous.
- No undo button on the strip. Undo is a real and separate want; bundling it
  here would give the strip two jobs and require the note to survive a delete,
  which D4 says it does not. (`CallDetail` keeps its own existing Undo.)
- No note on an undecided call (D5).
- No note-edit timestamp, so no "newest edit" ordering (D7).
- No draft persistence (D10).
- No change to any request or response shape of hide/save/unhide/unsave. Their
  upserts do gain `note=None` — a behaviour change, not a wire change (D9).
- No migration.

## 7. Gates

Each must be run against something it should reject before it counts. Green on
good input alone proves nothing. The named vacuity trap under each one is the
reason the gate is written the way it is.

1. **The column is projected for saved/hidden and not for inbox/archive.**
   Assert against the **compiled SQL projection** — `call_decisions.note`
   present in the SELECT list for `saved` and `hidden`, absent for `inbox` and
   `archive`. *Vacuity trap (found by the rev-1 review, with a populated
   control): asserting `decision_note is None` on an inbox response passes
   identically with and without a needless subquery, because an inbox row has no
   decision either way. A null assertion cannot see this; only the SQL can.*
   Use a word-boundary match — `calls.embedding` matching `calls.embedding_model`
   is a trap this repo has already hit once.
2. **404 without a decision** (D5). Must reject an implementation that creates a
   decision row to hang the note on: assert `call_decisions` row count is
   unchanged across the failed request.
3. **Un-hide destroys the note** (D4). Must reject a soft-delete: un-hide, hide
   again, assert `decision_note` is `null`.
4. **A note write does not move `decided_at`.** Adverse mutation: add
   `decided_at=func.now()` to the note endpoint's update — the natural mistake,
   since both existing decision endpoints do exactly that. Assert `decided_at`
   is byte-identical across the write, **and** that the Saved list order is
   unchanged. (`CallDecision` has no `updated_at`; rev 1's gate named a column
   that does not exist.)
5. **A flip clears the note** (D9). Save, note, hide, assert `null`. Then the
   reverse. Adverse mutation: drop `note=None` from one `set_` — the gate must
   redden on that one alone, or it is only testing the other.
6. **The candidate response carries no author metadata** (D3). Assert no
   `user_id`, `email`, `name` or `decided_by` key reaches the golden-set HTTP
   response. *This gate is about fields, deliberately. It does not and cannot
   assert that free text is anonymous.* Adverse mutation: join `users.email`
   into the candidate query.
7. **Only hidden notes are mined, and the state filter precedes the cap** (D7).
   Seed one hidden note and four saved notes on the same pair; assert the saved
   ones are absent and the hidden one is present. *Vacuity trap: with fewer
   notes than the cap, filtering after the cap gives the same answer as
   filtering before it — the seed must exceed the cap.*
8. **No N+1 from the new column.** Follow
   `tests/web/test_calls_list_query_count.py`: statement count for the `hidden`
   view at 1 call and at 10 calls must be equal, and the same for `saved`.
   *Vacuity trap (rev-1 error): comparing `hidden` against `inbox` compares two
   different queries and would miss per-row growth entirely.* Carry that file's
   own vacuity guard across.
9. **Another user's note is invisible.** Two users, one shared readable profile,
   both hide the same call, both write a note; each sees only their own. Must
   reject a query keyed on `(profile_id, call_id)` without `user_id` — the
   natural mistake, because the miner *is* keyed that way.
10. **The keyboard loop survives the strip.** Focus the strip's input, press
    `h`, assert no decision was made. Blur, press `h`, assert one was. *Vacuity
    trap: a test that never focuses the input passes whether or not the guard
    exists.*
11. **A late write lands on its own call** (D10). Write a note, then decide the
    next call before the first request resolves; assert the note is on the first
    call and the second is untouched. Adverse mutation: read the id from current
    focus instead of the strip's bound state.
12. **The golden-set field survives serialization** (§4). A full **HTTP
    response** round trip, not a miner unit test — the rev-1 review confirmed by
    direct probe that a field added only to `CandidateRow` is dropped silently
    by `CandidateOut`. Assert `user_notes` is present in the JSON.
13. **The golden-set page is reachable in a match build** (§10). Two halves,
    proved separately: `/api/admin/golden-set` is registered when
    `APP_EDITION=match`, and the nav renders the link. *Vacuity trap: asserting
    only that the link renders passes while the route 404s.*
    `AdminNav.test.tsx`'s *"every admin link the match edition renders is a
    route the match router mounts"* already couples them — keep it green rather
    than adding a weaker gate beside it. Adverse mutation: revert the `_ALWAYS`
    move alone; the API half must redden while the nav half stays green, or the
    two are not independent.

Measure the `saved` / `hidden` page latency before and after, at a full page,
and record both numbers in the commit. Adding a column to an existing join
should be close to free — but `8fee390e` (*perf(dashboard): stop loading the
three columns the calls feed never renders*) took 71-78% off this query's
row-load and `7afd8c23` did the same for the two board queries, and an
unmeasured regression here would quietly undo measured work.

## 8. Why the two sibling suggestions are not in this spec

Recorded so the next reader does not re-derive it.

**Saved-call follow-up** (deadline reminders, calendar export, change
notifications) is partly blocked. `Call` has no `updated_at` and no history —
ingestion overwrites the row — so "tell me when this call's deadline moved" has
nothing to diff against. This matches `notes/2026-09-05-plan-c-board-decisions.md`
D8, which found the same wall from the other side: *"`Call` has no status or
withdrawn column, so 'the board drops withdrawn calls' is not implementable."*
Change tracking is its own project. The calendar-export and reminder halves are
not blocked, and the weekly digest and scheduler both already exist.

**Visible matching status** is half-built and carries one live inconsistency:
`_ranking_counts` computes both a pending and an unresolved-failed count, and
`Dashboard.tsx` renders both, while `TriageFeed.tsx` renders only `pending`. The
feed — the page people actually work in — is the one that hides failures.
Staleness is also already available and unexposed: `CallRanking.profile_fingerprint`
exists and `ranking/queue.py` already uses it to decide what needs re-ranking.
Separately, `ranking_jobs` is the only job table in the schema with no `CHECK`
constraint on its status column.

## 9. What rev 1 got wrong

An outside review of rev 1 raised six Medium findings and four open questions.
All six were confirmed against the code; none was refuted. Kept here because the
errors are more instructive than the corrections.

1. **The subselect solved a problem the existing join already solves.** Rev 1
   read `_decision_predicate`, saw a scalar subquery, and generalised "the query
   never reaches the decision row" to the whole of `build_dashboard_view` — where
   a join to `CallDecision` sits about 390 lines further down, added for the
   ordering. The mechanism was wrong, and the prescription built on it was
   wrong. Reading one function and describing the query it belongs to is not the
   same act.
2. **The full edition never mounts `TriageFeed`.** Rev 1 promised the strip in
   both editions. The full router's index is `<Dashboard/>`. The correction
   turned out to be the more interesting fact: the two editions get opposite
   halves of this feature (§5).
3. **"Private" and "unattributed" were used as one word.** Rev 1's D3 let
   administrators read other people's notes while §6 promised no cross-user
   visibility. It also implied that omitting the author field made the text
   anonymous, which no amount of schema design can guarantee about prose.
4. **`CandidateOut` was invisible in rev 1.** The wire change stopped at the
   internal dataclass; the Pydantic model would have dropped the field silently.
   Gate 12 now requires an HTTP round trip.
5. **Three gates could not establish what they claimed.** Gate 1's null
   assertion was insensitive to the thing it tested; gate 8 compared two
   different queries instead of two page sizes; gate 4 named `updated_at`, a
   column `CallDecision` does not have.
6. **Four behaviours were simply unspecified**: what the strip does on
   success/failure/replacement (now D10), what a flip does to the note (now D9),
   what "newest" orders by (now D7), and whether whitespace is a note (now D8).

The three citation errors caught in rev 1's own self-review — D12's home, the
pnpm cooldown's file, and which commit optimised this query — are corrected in
place above and not repeated here.

## 10. Mounting the golden-set page in the match edition

**Settled by the human on 2026-09-07: the deployment is `match` only; `full` is
the next version; and the golden-set page is to be mounted in `match` now
rather than waiting.** Reading the notes as they arrive was judged worth more
than the gate bookkeeping below.

The alternative — capture in release 1, read them when `full` lands — was
rejected. It costs nothing now and would have opened the page onto months of
real notes instead of three. Recorded because it remains the fallback if the
task list below turns out larger than it measures.

### This is larger than it looks, and rev 2 under-quoted it

Rev 2's estimate ("one allowlist line plus its pinned tests") was wrong. **The
match edition does not serve the golden-set API at all.** `admin_golden_set.router`
sits in `_full_only_routers()` in `web/api/__init__.py`, moved there
deliberately on 2026-09-04 along with `admin.drafting_router` and
`admin_retention.router`, because leaving them in `_ALWAYS` "left six
require_admin-gated proposal-assist endpoints live in a deployment that does not
serve proposal assist". So the page is not merely unlinked; the endpoint behind
it does not exist in a match build.

### What has to change

Backend:

1. `web/api/__init__.py` — move `admin_golden_set.router` out of
   `_full_only_routers()` into `_ALWAYS`, and its import out of the deferred
   block with it.
2. `tests/architecture/test_edition_surface.py` — drop `/api/admin/golden-set`
   from `EXCLUDED_PREFIXES`. **Its comment carries a measured claim** — that the
   three admin prefixes cover "exactly the six PATHS (seven operations) the move
   drops", enumerated inline. Two of those paths are golden-set's. Re-measure
   and rewrite the comment; do not decrement it by arithmetic.
3. `tests/architecture/full_edition_routes.txt` needs no edit — full's surface
   is unchanged — but `test_match_edition_drops_only_excluded_routes` computes
   full-minus-match and must be re-run, because that difference shrinks.

Frontend:

4. `router.simple.tsx` — mount `admin/golden-set`, lazily, matching the sibling
   admin routes.
5. `AdminNav.tsx` — remove `/admin/golden-set` from `MATCH_EXCLUDED` and rewrite
   the comment above it, which names the three excluded destinations and says
   release 1 "mounts only /admin, /admin/users and /admin/users/pending".
6. `AdminNav.test.tsx` — the `MATCH_HIDDEN` constant (three entries), the
   full-edition ordering case, and the match-edition omission case.
7. `tests/architecture/test_match_gates_are_wired.py` — its scenario manifest
   contains the string *"the admin subnav offers only the three sections release
   1 mounts"*. This file pins equality, so the scenario list and any expect
   count move together or it goes red on correct code.

### Two things that make this safer than it sounds

**No damage to the process cut.** `test_match_edition_process_cut.py` asserts a
match boot loads **0** proposal-assist modules, and moving this router does not
change that: `web/api/admin_golden_set.py` imports only `dataclasses`, FastAPI,
Pydantic, SQLAlchemy, `auth.dependencies`, `db.models` and
`ranking.golden_set` — and `ranking/golden_set.py` in turn imports only
`dataclasses`, SQLAlchemy and `db.models`. Nothing from the proposal-assist
half. The module count that gate reports will move; the property it pins will
not. Re-measure the count rather than predicting it.

**One of the gates is self-verifying.** `AdminNav.test.tsx`'s *"every admin link
the match edition renders is a route the match router mounts"* fails if step 5
lands without step 4. The nav and the router cannot drift apart silently, which
is the failure mode this whole change is otherwise exposed to.

### And one thing that does not help

There is no cheaper door through the existing admin dashboard.
`/api/admin/dashboard` stays in `_ALWAYS` and does carry a `golden_set_report` —
but that is `ranking.eval.load_last_report()`, the evaluation summary
(`n_fixture`, `n_compared`, `tau`). It is the scoreboard, not the candidate
list, and it has nowhere to put a note.
