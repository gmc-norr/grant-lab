# Call Match — Release 1 design

Date: 2026-09-03
Status: proposed

## 1. Why

The customer wants the **profile-matching** half of call-manager now. The
proposal-assist half (cockpit, collaborative editor, drafting) is not part of
what they asked for and adds a large amount of surface area they do not need.

The decision is to cut the product in two releases:

- **Release 1 — "Call Match".** Collect calls, rank them against research
  profiles, triage them, mail a weekly digest, and put an institution-level
  ranked list on a public corridor screen.
- **Release 2 — proposal assist.** The existing SPA cockpit, unchanged. Not in
  release 1's build or its API surface.

The existing backend already does the hard part (ingestion, embedding, ranking,
digest). Release 1 is mostly a *simpler front end plus a triage layer*, not new
science.

## 2. What the research says

Two findings from surveying how universities actually solve this, and they
shaped the design:

1. **The weekly email is the proven channel.** Iowa, Auburn, Pitt, Trinity
   Dublin, MUSC, Oregon and UNO all run a weekly "funding opportunities"
   newsletter, several of them in discipline-specific variants. call-manager
   already has a working digest with no frontend dependency. Release 1 should
   treat the digest as a first-class deliverable, not a leftover.
2. **Nobody puts funding calls on digital signage.** A targeted search found no
   prior art. The corridor board is therefore a genuine experiment: cheap to
   build, potentially a differentiator, but release 1 must not depend on it.

Two patterns worth copying, from Instrumentl and Pivot-RP:

- **Instrumentl's forced triage.** Every match is either *saved* or *hidden*.
  The list empties. This turns a feed into an inbox, which is exactly the
  weekly job the customer described.
- **Instrumentl explains the match** by highlighting which fields matched,
  rather than showing a single opaque score. A number invites argument about
  the number; named reasons invite agreement or correction.
- **Pivot-RP** additionally offers change-notification on tracked
  opportunities and sharing with a colleague. Both are in scope (§7).

Signage legibility rules (from digitalsignage.com's typography/viewing-distance
guide and Yodeck's design rules) are treated as hard constraints in §6.

## 3. Scope

### In release 1

Public, no login:

- `GET /b/:slug` — the corridor board.

Behind login:

- `/` — ranked calls for the current profile, as a triage inbox
- `/calls/:id` — call detail
- `/profiles`, `/profiles/new`, `/profiles/:id`, `/profiles/:id/edit`
- `/me` — account settings
- `/admin/users`, `/admin/users/pending`
- `/admin/boards`, `/admin/boards/new`, `/admin/boards/:id` — admin only

Plus the existing weekly email digest, which needs no frontend.

### Explicitly out of release 1

- Every `/cockpit*` route
- `frontend/src/cockpit/`, `frontend/src/editor/`, `frontend/src/collab/`
- `/admin/drafting`, `/admin/golden-set`, `/admin/deleted-workspaces`
- The `workspaces`, `turns`, `annotations`, `rubric`, `references`,
  `citation_audit`, `export` and `files` API routers
- Anything requiring `ANTHROPIC_API_KEY`

"Out" means **absent from the build**, not hidden by a flag. See §5.

## 4. New concepts

### 4.1 Triage decisions

New table `call_decisions`:

| column | type | notes |
| --- | --- | --- |
| `id` | bigint PK | |
| `user_id` | FK → users, ON DELETE CASCADE | |
| `profile_id` | FK → profiles, ON DELETE CASCADE | a decision is per profile |
| `call_id` | FK → calls, ON DELETE CASCADE | |
| `state` | enum `saved` \| `hidden` | no third state; undecided = no row |
| `note` | text, nullable | free text, the user's reasoning |
| `decided_at` | timestamptz | |
| `decided_by` | FK → users | for shared profiles |

Partial unique index on `(user_id, profile_id, call_id)` so one user has at most
one decision per call per profile.

**Undecided is the absence of a row.** This keeps the inbox query simple (a
`NOT EXISTS`) and means a new call automatically arrives undecided without a
backfill.

The dashboard defaults to `state IS NULL` — the inbox. Two more views: Saved,
and Hidden (so a mistake is recoverable). The inbox can reach zero, and that is
the point.

The digest respects decisions: a hidden call never appears in a later digest for
that profile. This is what stops the digest becoming noise by week five.

### 4.2 Match reasons

The dashboard currently renders `aggregate_score` as a banded pill
(`MatchBadge`). Release 1 replaces the number with **named reasons**.

The data for this **already exists**. `call_ranking_criteria`
(`src/call_manager/db/models.py:498`) stores, per (call, profile), one row per
criterion:

- `criterion` ∈ `topic_fit` | `eligibility` | `exclusions` | `timeline` |
  `budget` | `trl`
- `rating` — a canonical string: `"5"`, `"pass"`, `"fail"`, `"unclear"`
- `rationale` — the model's reasoning
- `quoted_evidence` — **text quoted from the call itself**

This is strictly better than Instrumentl's colour-coded field highlighting,
because it carries the quote. The design is therefore:

- A compact row of criterion chips per call — `Topic fit 5/5`, `Eligibility ✓`,
  `Timeline ✓`, `Budget ?` — coloured by rating, not by an aggregate.
- Expanding a chip shows its `quoted_evidence`: the sentence in the call that
  justified the rating. That is the strongest possible answer to "why does this
  match?", because it is the funder's own words, not the model's.
- `rationale` is shown behind the same expansion, clearly labelled as the
  model's reasoning rather than fact.

The band (high / medium / low) is retained for sorting and for the digest, but
the raw aggregate is not shown in the UI.

### 4.3 Boards

New table `boards`:

| column | type | notes |
| --- | --- | --- |
| `id` | bigint PK | |
| `slug` | text, unique | random, URL-safe, ≥16 chars |
| `title` | text | shown in the board header |
| `profile_id` | FK → profiles, ON DELETE RESTRICT | usually an `institution`-level profile |
| `is_published` | bool, default false | nothing is public until this flips |
| `min_score` | int, default 60 | the score threshold; everything at or above it is a candidate |
| `items_per_page` | int, default 8 | grid is 2 x 4 |
| `max_pages` | int, default 3 | hard cap — see the exposure note below |
| `page_seconds` | int, default 30 | dwell time per page |
| `created_by` | FK → users | |
| `created_at` / `updated_at` | timestamptz | |

New table `board_items` — the **approved queue**:

| column | type | notes |
| --- | --- | --- |
| `board_id` | FK → boards, ON DELETE CASCADE | |
| `call_id` | FK → calls, ON DELETE CASCADE | |
| `position` | int | admin-controlled order |
| `approved_by` | FK → users | |
| `approved_at` | timestamptz | |

Primary key `(board_id, call_id)`.

**The board never publishes an unapproved call.** `min_score` *proposes* the
candidate set; an admin ticks what goes up. Rationale: a corridor screen is the
institution's voice, and a single bad rank on a hospital wall is a public
incident, not a bug report.

This is worth stating plainly because the two rules look like they conflict:
"everything above the threshold is included" is automatic, and "an admin
approves each call" is not. **The threshold selects; the human confirms.** The
threshold is a filter on what an admin is asked about, never a publish
decision.

The cost of keeping the gate is that approval is now a recurring chore rather
than a one-off — 24 candidates instead of 8. So the admin queue must be built
for bulk: a checklist ordered by score, an "approve everything at or above N"
action, and a visible count of what is waiting. An approval UI that costs a
minute per call will be abandoned, and an abandoned gate is worse than no gate
because the board silently stops updating.

Only admins may create, edit, approve items for, or publish a board.

## 5. Making "out of release 1" true

Two routers:

- `frontend/src/router.tsx` — the existing full router (release 2)
- `frontend/src/router.simple.tsx` — release 1

`frontend/src/main.tsx` picks one at build time from a Vite env flag
(`VITE_APP_EDITION` = `match` | `full`, default `full`).

This works because the cockpit and admin-drafting routes are *already*
`lazy(() => import(...))` in `router.tsx`. Leaving those imports out of
`router.simple.tsx` means Vite never emits the chunks — the code is genuinely
absent from the bundle, not merely unreachable.

Backend: release 1 does not mount the `workspaces` / `turns` / `annotations` /
`rubric` / `references` / `citation_audit` / `export` / `files` routers, and
runs with `DRAFTING_ENABLED=false`. Router mounting is gated on the same setting
that drives the edition, so a release-1 deployment cannot serve a
proposal-assist endpoint.

### The new shell

`SimpleShell` replaces `AppShell` for release 1: one plain top bar (product
name, Calls / Profiles / Admin, user menu), content below. No activity bar, no
status bar, no resizable panes, no bottom panel. Same Mantine theme tokens, so
it still looks like the same product.

`PreAuthLayout` is reused unchanged for login/register — it is already
auth-free and shell-free.

### The triage screen

Mockups: https://claude.ai/code/artifact/cba2b38b-a1e7-4179-bca6-a734195e50fc

Release 1 serves three people at three moments, and they do not want the same
thing: the corridor passer-by (three seconds, signage), the researcher on Monday
morning (five minutes, a reading app), and the coordinator (as long as it takes,
a dense admin tool). Treating all three as one app with panes is the mistake the
cockpit SPA makes; each gets its own visual register here.

The researcher's screen is the product. It is a **single ~680 px column** — an
expanding feed, not a mail-client split and not a one-card-at-a-time stack. The
focused call is expanded with its evidence; the rest are collapsed to title,
funder and days left. A decided card leaves the list.

Three properties follow from the job rather than from taste:

- **The list must end.** A feed that never empties trains people to stop opening
  it. Reaching zero is the reward, and the inbox-zero state is a designed screen,
  not an afterthought.
- **The evidence is visible, not behind a click.** If people decide from the
  title alone, the ranking added nothing a keyword search would not have given
  them. The quoted sentence *is* the decision input, so it gets prime space.
- **One primary action, one keypress.** "Powerful" here means fast, not dense:
  `j`/`k` to move, `s` to save, `h` to hide — inbox empty in under a minute.
  More filters would make the tool complicated, not powerful.

On a 1440 px screen the column is **not** stretched; reading degrades past ~75
characters. The extra width holds a 240 px left rail for orientation only:
Inbox / Saved / Hidden with counts, and the profile being matched against.

### What a call card shows

The card answers four questions in the order a researcher asks them: *what is
this*, *what does it fund*, *why is it in front of me*, *can I apply*. Field by
field, with where each comes from:

| Shown | Source | Always present? |
| --- | --- | --- |
| Title | `Call.title` | yes |
| Funder | `Call.source_name` | yes |
| Programme | `Call.programme` | no — omit the separator when absent |
| Deadline + days left | `Call.deadline_local_date` | usually |
| Budget (a **range** when min ≠ max) | `Call.budget_min/max/currency` | often absent |
| **What the call funds** | `Call.summary` | depends on the source adapter |
| The matched sentence | `call_ranking_criteria.quoted_evidence` | when ranked |
| Criterion chips | `call_ranking_criteria.rating` | when ranked |
| Keywords | `Call.keywords` | often empty |
| "N things to check" | count of `CallRanking.applicant_notes` | **escalated calls only** |

Two of these are already on `CallOut` and cost no backend work: `summary` and
`keywords` are served today and the current dashboard renders neither.

**The card must read correctly with the optional fields missing**, because for
most calls they will be. `CallRanking.summary` and `applicant_notes` are written
only by the remote path (`enable_analysis=True`); the local Gemma path leaves
both NULL, and the remote path is capped at `RANKING_REMOTE_DAILY_CAP` (100/day).
So the richest cards are a minority — though a usefully biased minority, since
escalation is triggered by a high local score, which is exactly what sits at the
top of the inbox.

Where `Call.summary` is absent, say so and link out rather than collapsing the
section: an empty space reads as a bug, a sentence reads as a fact about the
source.

### Applicant notes on the detail page

`CallRanking.applicant_notes` is a list of `{category, text}` over a closed
vocabulary of nine categories, each with a display heading already defined in
`web/filters.py:note_category_heading` — Deadline, Eligibility, Budget &
funding, Team & consortium, IP & licensing, Data & open access, Evaluation
criteria, Process & timing, Other. The detail API already serves them as
`CallDetailNoteOut{category, heading, text}`.

These are the most useful thing on the page and the most dangerous, because they
are the **only block not sourced from the call text**. The criteria quote the
call; the summary is the funder's; the notes are the model's inference. So they
are grouped under their own heading, visibly attributed to the model, and
carry an instruction to verify against the call text. Never interleave them with
quoted evidence.

### Responsive strategy

Mostly plain responsive CSS — the single column *is* the phone layout, which is
half of why it was chosen. Three deliberate exceptions where the DOM genuinely
differs:

1. **The wall board** — not a breakpoint at all. Its own route, own layout,
   shares only the API.
2. **The decide gesture** — `s`/`h`/`j`/`k` on desktop; swipe right to save,
   left to hide on a phone. Same cards, same data, different interaction. The
   rail becomes a bottom tab bar, where thumbs are.
3. **Admin** — a seven-column table is bad on a phone at any breakpoint. Admin
   screens are **desktop-only** and say so politely on small screens. Nobody
   approves a wall display from a bus.

Everything else is media queries, with container queries where a component
should react to its own box rather than the viewport. `useMediaQuery` is
reserved for exceptions 2 and 3.

### Visual language

The existing app is **dark-only** (`frontend/src/theme/theme.css`, a VS Code
palette, `color-scheme: dark`). Release 1 keeps every token — teal `#0f766e` /
`#14b8a6`, the deadline red and amber, the exact system font stack, 4 px radii —
but puts the *application* screens on an off-white ground. It is a reading app,
read in a bright office, by people who are not developers; dark-on-dark is
precisely the register release 1 exists to leave behind. **The wall board stays
dark**, where dark is right.

Colour is reserved for one thing: **deadline urgency**. The bands already exist
(`frontend/src/routes/Deadline.tsx`: ≤14 days urgent, ≤30 days watch). Everything
else is greyscale plus the teal accent for actions. Type carries hierarchy, not
boxes and borders.

One typographic decision does real work: **quoted evidence is set in a serif**
(Georgia), the only serif in the product, used for nothing else. It makes the
quote read as *the funder said this* rather than *the model thinks this*. That
distinction carries the credibility of the whole feature, so it is a design
requirement, not decoration.

## 6. The corridor board

### Constraints (hard)

From the signage literature, treated as requirements:

- Readable text height = viewing distance (m) × 0.84 cm, then × 1.5 for comfort.
- Contrast ratio ≥ 7:1 for all text.
- Sans-serif, at most two fonts, no italics.
- A QR code must be ≥ 1/10 of its **scanning** distance, and ≥ 150 px on screen
  (the published screen floor; 240 px is the general digital recommendation, which
  is aimed at print and small embeds). Note that scanning distance is not viewing
  distance: people read the board from ~2-4 m but step to ~0.7-1 m to scan.

### A grid of eight, paging

Every call carries its own QR code. That decision sets the whole layout, because
**a QR changes who the board is for**: a spotlight board serves someone walking
past at 4 m, but a board where everything is scannable serves someone who
*stopped*. Someone close enough to scan does not need 80 px type.

So the body is a 2 x 4 grid of eight cards on a 1920 x 1080 screen. Per card:

- QR **150 px** (≈ 9.4 cm on a 55" screen) — comfortably above the 1/10 rule for
  an arm's-length scan.
- Title **34 px** — readable standing ~1.7 m back.
- Funder, days-to-deadline, budget.

One element stays big enough to read at 4 m, and it is the header: the board
title plus **the nearest deadline across all pages**, in the urgent colour.
That is the hook that makes someone stop. Without it the board is invisible to
anyone not already standing at it.

Pages cycle every `page_seconds` (default 30). The page count is **dynamic** —
every call at or above `min_score` that an admin has approved is included,
eight per page.

**Paging does not add coverage for free; it dilutes exposure.** At three pages
any one call is on screen a third of the time. At six, a passer-by has a
one-in-six chance of seeing a given call and a full cycle runs three minutes,
which is longer than anyone stands in a corridor. `max_pages` (default 3) exists
for this reason: past it, raise `min_score` rather than adding pages. Without
the cap the threshold quietly becomes a lottery.

A footer shows `Sida N av M` and a progress bar, so someone who stopped knows
more is coming and roughly how long to wait.

Sizing is expressed in `vh`, not pixels, so the same page works on a portrait
screen — most corridor displays are hung portrait, and the deployment screen is
not known yet. A portrait board is one column of four rather than two of four;
`items_per_page` covers it.

### What is on the board

Per call: title, funder/source, days-to-deadline, budget, and a QR code.

**No summary line.** An earlier revision of this spec called for a one-line
snippet of `Call.summary`; eight cards on a 1080 px board leave no room for it
without shrinking the QR or the title below their legibility floors. The QR *is*
the "tell me more" — a passer-by who wants the detail scans it and reads the
funder's own page on a device built for reading.

Note the deliberate difference from the logged-in view: the board never shows
the criterion chips, `quoted_evidence`, `rationale`, or `applicant_notes`.
Everything in that group is a judgement *about this institution's fit*, and none
of it belongs in a corridor. Title, funder, deadline and budget are facts the
funder already published.

The QR points at `Call.url` — the **funder's own page**, never at this app. A QR
that dead-ends on a login screen is worse than no QR.

Deadline urgency, not match score, is the loud number. "Closes in 9 days" means
something to a passer-by; "0.83" does not.

### What is deliberately not public

- No score numbers
- No LLM-generated rationale text
- No profile text or profile identity beyond the board's own title
- No call-detail page, no filters, no navigation
- Calls whose deadline has passed
- Unpublished boards → 404, indistinguishable from a nonexistent slug

### Public endpoint

`GET /api/public/boards/{slug}` — no authentication, rate-limited, cached.

Returns only: board title, `generated_at`, and for each approved item the call
title, source name, deadline, match reasons, and `Call.url`. It reuses
`build_dashboard_view()` (`src/call_manager/web/dashboard.py`), which already
takes a bare `profile_id` and requires no user, to order the admin's candidate
list.

The public page is a route under `PreAuthLayout` at `/b/:slug` — that layout is
already auth-free, so no new auth architecture is introduced.

## 7. Pivot-RP features in scope

**Share a call with a colleague.** From the call detail page, send a link by
email. Reuses the existing email transport. Small, and it is how a tool like
this spreads inside a hospital.

**Notify when a saved call changes.** Sequenced **last** in release 1, because
there is no change detection today: `Call` has no content hash and no
`updated_at` (`src/call_manager/db/models.py:191-203`), and ingestion does no
comparison. Requires a content hash computed at upsert time, a record of what
changed, and a notification path for users who saved that call. It depends on
`call_decisions` existing first. Touching ingestion is the highest-risk change
in release 1, so it lands alone and last.

Deadline reminders are **not** in release 1.

## 8. Reused unchanged

- Auth: `PreAuthLayout`, `Login`, `Register`, session handling
- `build_dashboard_view()` — the ranking query
- The profiles API and pages (re-skinned into `SimpleShell`; contents unchanged)
- The admin users pages
- The Mantine theme tokens, the `openapi-fetch` client, generated types
- The weekly email digest, plus a decisions-aware filter

## 9. Testing

Three gates that make the design's claims true rather than asserted:

1. **Bundle gate.** Assert the `VITE_APP_EDITION=match` build output contains no
   cockpit/editor/collab chunk. Without this, "excluded" is a claim.
2. **Route manifest drift guard covers both routers.** `router.test.tsx`
   currently pins one router against `routes.json` `spa_routes`. With two
   routers and one manifest, the simple router can silently drift. The guard
   must assert the release-1 route set explicitly, and that it is a subset of
   the manifest.
3. **Public endpoint gates.** Unpublished board → 404. No auth required for a
   published one. Response body contains no score float and no rationale text.
   An unapproved call never appears. A call past its deadline never appears.

Plus, per the repo convention, mutation-test each gate: break the thing the gate
claims to protect and confirm the gate goes red.

## 10. Open questions

- ~~Does the ranking output carry per-field match attribution?~~ **Resolved:
  yes.** `call_ranking_criteria` carries per-criterion rating, rationale and
  quoted evidence. §4.2 is upgraded accordingly.
- How many of the six criteria are actually populated in the live data today?
  The table exists; whether every ranker path fills all six is unverified.
  Measure before designing the chip row's empty state.
- Portrait or landscape corridor screen? The design works for both, but the
  first real screen should be measured, not assumed.
- Is the digest's current per-profile targeting sufficient, or does release 1
  need the discipline-specific variants that peer universities run?

### Not yet designed

Three screens are in scope but unmocked, and they are the ones most likely to
come out wrong:

- **First run.** What a user sees before they have a profile. The profile drives
  all matching, so this path is load-bearing and entirely undrawn.
- **Search.** The triage screen shows an affordance for it; nothing behind it is
  designed. This is also the only place a researcher could ask *"why isn't call X
  here?"* — which is exactly the blind spot §F2 of the matching review describes,
  currently with no UI surface at all.
- **The board approval queue.** The only genuinely novel admin screen: the
  ranker proposes, an admin ticks, in bulk. No precedent anywhere in the app.

### Two untested assumptions

The design rests on a bet that has not been checked with a real user:

1. **That the quoted sentence is what makes a researcher decide.** If they skim
   titles and ignore the quote, the ranking adds nothing over a keyword list and
   the layout is wrong. Twenty minutes with one researcher and the mockups
   settles it.
2. **That people will actually hide things.** If hiding feels risky — "what if I
   miss something?" — nothing gets hidden, the inbox never empties, and the
   design collapses into a feed. Watch for it; the fix (make Hidden visibly and
   obviously recoverable) is cheap, but only if the problem is noticed.

## 11. Sequencing

1. `SimpleShell` + `router.simple.tsx` + the build flag + the bundle gate
2. Re-skin the existing calls / profiles / account / admin-users pages into it
3. `call_decisions` + the triage inbox + digest integration
4. Match reasons replacing the score badge
5. `boards` + `board_items` + the bulk admin approval queue (§4.3 — the gate is
   only real if approving 24 candidates is fast)
6. The public board endpoint + the paging `/b/:slug` display page
7. Share a call with a colleague
8. Change detection and notification (alone, last)
