# Decision notes — the user's own reason, attached to the call they hid

Date: 2026-09-07
Status: design approved, not yet planned.
Origin: suggestion 4 of an outside product review ("lightweight feedback"), the
last of that review's four *Useful Additions* to be built. The other three are
board health (merged), saved-call follow-up (blocked — see §8) and visible
matching status (deferred).

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
not reasoned.

| Fact | Where | Evidence |
| --- | --- | --- |
| The column exists | `call_decisions.note`, `Text`, nullable | created by `alembic/versions/0071_call_decisions.py` |
| Nothing writes it | `web/api/calls.py` | `grep -n '\bnote\b'` returns no hits |
| It was created for exactly this | `CallDecision.note` comment | *"NOTHING WRITES THIS IN RELEASE 1 — the column is created now so the note UI is a frontend change later, not a migration."* |
| The spec that created it named the purpose | `specs/2026-09-03-call-match-release-1-design.md` | table row: `note \| text, nullable \| free text, the user's reasoning` |
| Hides already feed the golden set | `ranking/golden_set.py` | the `dismissed_high_score` candidate query, `aggregate_score >= 60` + `EXISTS` a `hidden` decision |
| Golden-set candidates are already anonymous | `CandidateRow` in `ranking/golden_set.py` | fields are `call_id, profile_id, call_title, profile_name, aggregate_score, embed_similarity, reason` — no user |
| Four triage views, one query | `GET /api/calls?view=` | `Literal["archive", "inbox", "saved", "hidden"]` |
| The view filter never reaches the decision row | `web/dashboard.py::_decision_predicate` | it is a `scalar_subquery()` over `CallDecision.call_id`, not a join — so `note` is **not** in the result set today |
| The card leaves the list on hide | `useHideCall` in `frontend/src/api/calls.ts` | `onSuccess: () => invalidateDecisions(qc)` |
| There is no toast system | `frontend/src` | `grep` for `notifications\|showNotification` returns nothing outside tests |
| The keyboard guard already exists | `isEditableTarget` in `TriageFeed.tsx` | covers `<input>`, `<textarea>` and non-false `[contenteditable]` |

**The single most important consequence: this needs no migration.** The column
has been in the database since 0071. The work is an endpoint, a response field
and a frontend.

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

Rejected alternative: notes only in the Saved and Hidden tabs. Zero risk to the
triage loop, and trivial to build — but people do not go back, so the capture
rate would be near zero, and a reason is most accurate in the two seconds after
the decision. The tab-side editing is built anyway (§5), so that option is a
subset of this one rather than a real fork.

### D3 — The note is private to its author; the golden-set page shows it unattributed

`call_decisions` is keyed `(user_id, profile_id, call_id)` and a profile can be
readable by more than its owner. So "who sees this" is a real question, not a
formality — `notes/2026-09-04-profile-visibility-scope.md` and the merged
"profiles are personal" work settled the surrounding rules.

The answer:

- The author sees their own notes, in Saved and Hidden.
- Colleagues who can read the same profile do **not** see them.
- The golden-set labelling page shows the note **text** with **no name**.

That last point preserves a property the code already has: the candidate miner
deliberately does not carry a user id (§2), so it reports "a hide exists on this
pair" and not "Pär hid this". Showing the text without the name adds the useful
half and keeps that property intact.

The reason for privacy is not squeamishness. It is that people write honest
notes when nobody is watching, and honest notes are the entire value here. A
shared, attributed note is a different feature — a team annotation — and it can
be added later on top of this one.

### D4 — Un-deciding destroys the note

`CallDecision` has no `undecided` state; undecided is the **absence of a row**,
and the model docstring says so explicitly. Un-hiding is a `DELETE`. The note
lives on that row, so it dies with it. Re-hiding the same call later starts
blank.

This is stated here because it is the behaviour a reader would most reasonably
expect to be otherwise, and because the alternative — keeping orphaned notes
alive for rows that no longer exist — would require either a soft-delete or a
second table, and would quietly break the "undecided = no row" invariant that
the inbox query (`NOT EXISTS`) is built on.

### D5 — A note requires a decision

`PUT .../note` on a call with no decision row for that `(user, profile)` returns
**404**, not a 201 that invents a decision. There is no state the row could
carry: `state` is `NOT NULL` and constrained to `saved | hidden`, so writing a
note would force this endpoint to guess a decision the user did not make.

### D6 — Naming: `decision_note`, not `note`

Three distinct things in this codebase are called some form of "note", and two
of them already sit on the objects this feature touches:

| Name | What it is | Where |
| --- | --- | --- |
| `applicant_note_count` | count of `CallRanking.applicant_notes` — the **model's** inferred things-to-check | already on `CallOut` |
| `reason` | why a pair became a golden-set **candidate** (`dismissed_high_score`, `cosine_disagreement`, `escalation_disagreement`) | already on `CandidateRow` |
| the new one | the **user's** own words | this spec |

So: the API field is `decision_note`, and the golden-set candidate field is
`user_notes` (plural — see D7). Neither is `note` and neither is `reason`. A
`CandidateRow.reason` that sometimes meant the mining reason and sometimes the
human's reason would be a defect with a very long fuse.

Note that `CandidateRow` is also the name of a local component in
`AdminGoldenSet.tsx`, which renders `c.reason` in a `<Code>` element. The
frontend change must not read as "the reason moved".

### D7 — Several people can hide the same call

The unique constraint is `(user_id, profile_id, call_id)`, so one **user** has
at most one note per call per profile — but several users who can read the same
profile may each hide the same call, and the miner's `EXISTS` fires on any of
them. The golden-set candidate therefore carries `user_notes: list[str]`, newest
first, non-null only, capped at 3. Unattributed, per D3.

### D8 — Length

`max_length=1000` on the request model, in Pydantic, not in the database. About
150 words: generous for a sentence or two of reasoning, bounded so a paste
cannot be unbounded. The column stays `Text`, so raising the cap later is a
one-line change and never a migration.

Precedent in `web/api`: `max_length=200` for search and display names,
`max_length=1024` for passwords. 1000 sits deliberately between them; a reason
is longer than a name and shorter than an essay.

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
- Does **not** touch `state`, `decided_at` or `decided_by`. A note is not a new
  decision, and the Saved and Hidden lists order by `decided_at` (Phase 2
  decision D12, recorded in `CallDecision.__table_args__` — not in the release-1
  spec) — bumping it would silently reorder the list under the user
  as they annotate it.

Rejected alternative: an optional body on `POST /calls/{id}/hide`. It fails on
two counts. The hide fires on a keypress and the note arrives seconds later, so
a second request is needed regardless. And the release-1 spec froze that
endpoint's wire contract on purpose (D14: *"hide is a re-skin of an endpoint
that predates the state column and keeps its wire contract"*).

### New response field

`CallOut.decision_note: str | None`.

`_decision_predicate` filters with a scalar subquery and never joins the
decision row (§2), so the note is genuinely absent from today's result set and
has to be fetched. Populate it via a **correlated scalar subselect**, added only
when `profile_id` is not null and the view is `saved` or `hidden`:

- `inbox` has no decision row by definition, so the column would always be null.
- `archive` may have one, but the archive is the full edition's browsable
  firehose and adding a per-row subselect to it buys nothing.
- Conditional construction means the two hot views pay literally nothing.

A `LEFT JOIN` would also work and cannot duplicate rows (the unique constraint
forbids it), but it is unconditional and therefore harder to keep off the views
that do not want it.

**This is an added per-row cost on a page that was just optimised.** `8fee390e`
(*perf(dashboard): stop loading the three columns the calls feed never renders*)
took 71-78% off THIS query's row-load, and `7afd8c23` did the same for the two
board queries last week. Adding a subselect back is the same class of decision in the
opposite direction, so it is bounded to two views and must be measured, not
assumed — see §7.

### Changed response field

`CandidateRow` in `ranking/golden_set.py` gains `user_notes: list[str]` (D7).
Only the `dismissed_high_score` query can populate it; `cosine_disagreement` and
`escalation_disagreement` do not require a decision to exist, so those return an
empty list rather than a null, and the UI renders nothing.

## 5. The UI

**The strip** — `TriageFeed.tsx`. Plain markup. There is no notification library
in this frontend and `pnpm add` is blocked by the seven-day cooldown
(`minimumReleaseAge: 10080` minutes, in `frontend/pnpm-workspace.yaml`), so
introducing one is not on the table and is not wanted. One strip,
top of the list, replaced by the next decision, dismissed on navigation.

It must not steal focus. The whole value of the feed is a keyboard loop that
never stops, and a strip that grabs the caret would convert `h h h` into typed
text in the first box. `isEditableTarget` handles the other half (keys are inert
*while* the box has focus) and already exists.

**Saved and Hidden rows** — the note renders under the title, click to edit.
This is where a note gets corrected or added late, and it is what makes D2's
rejected alternative a free subset rather than a lost option.

**Golden set** — `AdminGoldenSet.tsx`, in the candidates table, beneath the
existing `reason` code. Unattributed (D3).

### Edition

`/admin/golden-set` is in `MATCH_EXCLUDED` in `AdminNav.tsx` and is mounted only
in `router.tsx`, not `router.simple.tsx`. **The golden-set half of this feature
is full-edition only.** The strip and the Saved/Hidden editing land in both.

This is not a limitation to design around — it is a fact to state, because a
reader who tests in the match edition will not find the third surface and will
reasonably conclude it was not built.

## 6. What this does not do

- No reason codes or dropdowns (D1).
- No attribution, and no cross-user visibility (D3).
- No undo button on the strip. Undo is a real and separate want; bundling it
  here would mean the strip has two jobs and the note has to survive a delete,
  which D4 says it does not.
- No note on an undecided call (D5).
- No change to `state`, `decided_at`, `decided_by`, or to the hide/save wire
  contracts.
- No migration.

## 7. Gates

Each of these must be run against something it should reject before it counts.
Green on good input alone proves nothing.

1. **The note survives a round trip** and comes back on the `saved` and `hidden`
   views. Must reject: a build that populates the subselect on `inbox`.
2. **404 without a decision** (D5). Must reject: an implementation that creates
   a decision row to hang the note on. Assert the row count is unchanged.
3. **Un-hide destroys the note** (D4). Must reject: a soft-delete. Assert that
   re-hiding returns `decision_note: null`.
4. **A note write does not move `decided_at`.** Must reject: a blanket
   `updated_at = now()`. Assert the timestamp is byte-identical across the
   write, and assert the Saved list order is unchanged.
5. **Another user's note is invisible.** Two users, one shared readable profile,
   both hide the same call, both write a note. Each sees only their own.
   Must reject: a query keyed on `(profile_id, call_id)` without `user_id` —
   which is the natural mistake, because the miner *is* keyed that way.
6. **The golden-set candidate is unattributed.** Assert no user id, email or
   name reaches the candidate response. Must reject: a join that carries
   `users.email` along for the ride.
7. **The keyboard loop survives the strip.** Focus the strip's input, press
   `h`, assert no decision was made. Then blur, press `h`, assert one was.
   Note the false-pass here: a test that never focuses the input passes whether
   or not the guard exists.
8. **Query count does not grow per row.** The subselect must not become an N+1.
   Capture the SQL and assert the statement count for a `hidden` page is the
   same as for an `inbox` page.

Measure the `saved` / `hidden` page latency before and after, at a full page, and
record both numbers in the commit — §4 adds cost to a query that master
`7afd8c23` just made cheaper, and an unmeasured regression there would undo work
that was measured.

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
