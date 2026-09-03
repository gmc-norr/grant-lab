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
- `/profiles`, `/profiles/new`, `/profiles/:id`, `/profiles/:id/edit` —
  including **LLM-assisted profile bootstrap**, which is easy to miss in a
  "view + edit" scope line and is arguably the highest-leverage feature in the
  release. `profiles/bootstrap.py`, `bootstrap_worker.py`, `bootstrap_prompt.py`
  and `profiles/pubmed.py` build a profile from a seed plus the group's own
  publications, with `bootstrap_status` already surfaced on the profiles API.

  It matters more than its size suggests: **matching quality is bounded by
  profile quality**, so this is the machinery that decides whether the product
  is good. It runs on local Ollama (`bootstrap_worker.py:106`), so it ships in
  release 1 with no `ANTHROPIC_API_KEY`.
- `/me` — account settings, **including digest subscriptions**. `subscriptions`
  (`user_id`, `profile_id`, `digest_enabled`), `PUT /me/subscriptions`
  (`web/api/me.py:205`) and the `Settings.tsx` UI all exist already; the digest
  is user-configurable today and release 1 keeps that.
- `/admin` — the admin dashboard. Easy to overlook because it is an index route,
  but it carries **source health**: per-source call counts
  (`web/api/admin.py:167`). Without it nobody notices when a source stops
  returning calls and the inbox just quietly gets emptier. In scope.
- `/admin/users`, `/admin/users/pending`
- `/boards`, `/boards/new`, `/boards/:id` — wall-display management. **Not under
  `/admin`**: since any user who can edit a profile may build a board (§4.3), an
  `/admin` path would be a lie about who the page is for, and the route guard
  would have to contradict the URL. Only the Publish control inside the page is
  admin-gated.

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

Only §4.3 is genuinely new. §4.1 migrates a table that already carries rows and
§4.2 renders columns that are already populated and already served — a reader
who skips this note will plan far more work than the release needs.

### 4.1 Triage decisions

**This extends an existing mechanism; it is not greenfield.** `user_call_dismissals`
already exists and is wired end to end: `web/api/calls.py:193` writes a dismissal,
`web/dashboard.py:326` filters the list on it, and `digest/assemble.py` respects
it. Half of what this section asks for is already shipped and carrying real rows.

What it lacks is the other half of the pair. Today a user can only push a call
away; there is no way to keep one, so the list has a delete key and no inbox.
And the existing key is `(user_id, call_id)` — a dismissal is global, so a
researcher matching against two profiles who dismisses a call for one silently
dismisses it for both. That is wrong once a user has more than one profile.

Replace it with `call_decisions`:

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

Unique index on `(user_id, profile_id, call_id)` — at most one decision per call
per profile.

**Migration, not a fresh table.** Every `user_call_dismissals` row becomes a
`hidden` decision. Existing rows carry no profile, so they fan out.

**This is a deliberate semantic change, and it is not lossless.** An earlier
revision of this spec called fanning out "the only lossless reading"; it is not.
Today's dismissal is global — `web/dashboard.py:325-332` filters on
`(user_id, call_id)` and nothing else — so it also suppresses the call on any
profile the user creates *later*. No per-profile backfill can reproduce that: a
profile added next month starts clean and the old hide reappears. That is the
intended trade, because per-profile decisions are the entire point of the new
table, but it must be written down rather than discovered by the first user it
surprises.

**The backfill set, named exactly:** every profile the user is subscribed to
(`subscriptions.profile_id` for that `user_id`) plus their
`users.default_profile_id`, restricted to `profiles.is_active = true`. Not
"every active profile in the database" — `Profile.is_active`
(`src/call_manager/db/models.py:294`) is a global flag, not a per-user one, so
that reading would write a hide onto other groups' profiles. Fanning out to the
user's own set is the conservative direction: it preserves their intent
everywhere it currently applies, where picking a single profile would silently
un-hide calls people had already dealt with.

Drop `user_call_dismissals` in the same migration; leaving both is how two
sources of truth start.

**Undecided is the absence of a row.** The inbox query is a `NOT EXISTS`, and a
newly ingested call arrives undecided with no backfill.

The dashboard defaults to undecided — the inbox. Two more views: Saved, and
Hidden (so a mistake is recoverable, which also keeps hiding from feeling
risky — see the untested assumptions in §10).

**Hidden is a re-skin, not a new feature.** `POST /calls/{id}/hide`,
`DELETE /calls/{id}/hide` (un-hide) and `GET /calls/hidden`
(`web/api/calls.py:181-231`) all exist and work today. Release 1 re-points them
at `call_decisions` and gives them a visible home in the UI; the recoverability
the §10 assumption depends on is already implemented server-side. **Saved** is
the genuinely new half of the pair.

The digest respects decisions: a hidden call never appears in a later digest for
that profile. This is what stops the digest becoming noise by week five. The
existing digest already does this for dismissals, so the migration must keep
that behaviour working rather than reintroduce it.

**What a saved call leads to: nothing, deliberately.** Release 1 has no
proposal-assist, so Save means "I am interested in this" and the next step is
the funder's own page. The two existing "Start drafting" entry points
(`routes/CallRow.tsx:55` and `routes/CallDetailBody.tsx:48`) link to
`/cockpit/new`, which §3 excludes — **they must be removed, not disabled**, or
release 1 ships two buttons that 404. Release 2 adds drafting on top of a Saved
list that already exists.

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

**Most of this is already built — on the detail page.**
`frontend/src/routes/CallDetailBody.tsx:145-171` already renders one card per
criterion carrying its label, its rating, its `rationale` and its
`quoted_evidence` as a blockquote, fed by `CallDetailCriterionOut`
(`web/api/calls.py:147-155`). So §4.2 is new work **only for the list/feed
view**, where `routes/CallRow.tsx:6,53` still renders `MatchBadge`.

The detail page does need one *removal*, not an addition: `CallDetailBody.tsx:57-67`
renders `{ranking.aggregate_score}` under the heading "Aggregate score" — exactly
the raw number the paragraph above forbids.

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
| `created_by` | FK → users | the author |
| `published_by` | FK → users, nullable | admin who made it public; NULL while unpublished |
| `published_at` | timestamptz, nullable | |
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

### Who may do what

The risky act is not creating a board — it is making one **world-readable**. So
the two are separated:

| Action | Who |
| --- | --- |
| Create a board, edit its settings, approve/reorder items | any user who **can edit the board's profile** |
| Flip `is_published` (and un-publish) | **admin only** |
| Delete a board | its creator, or an admin |

"Can edit the profile" is not a new concept: `web/api/profiles.py:151` already
defines it as `p.created_by == user.id or user.is_admin`. Boards reuse that rule
exactly, so no second permission model appears.

This maps the permission to where the risk actually sits. A researcher builds the
board for their group's own screen — harmless, because an unpublished board is
private and its slug resolves to a 404. An admin then glances at it and
publishes. A hospital already understands that shape: it is the same as any
comms sign-off.

`created_by` on `boards` is the author; `approved_by` on `board_items` records
who confirmed each call. Publication is recorded separately (`published_by`,
`published_at`) because "I picked these calls" and "I put them on a public wall"
are different claims and a later question about the wall will be about the
second one.

## 5. Making "out of release 1" true

Two routers:

- `frontend/src/router.tsx` — the existing full router (release 2)
- `frontend/src/router.simple.tsx` — release 1

`frontend/src/main.tsx` picks one at build time from a Vite env flag
(`VITE_APP_EDITION` = `match` | `full`, default `full`).

**The current router is only partly lazy, so exclusion must not lean on
laziness.** An earlier revision claimed "the cockpit and admin-drafting routes
are already `lazy(() => import(...))`". Only some are. `Cockpit` and the admin
screens are lazy (`frontend/src/router.tsx:31-43`), but `CockpitChat` and
`CockpitSidebar` are **static** imports at `router.tsx:5-6`, and
`WorkspaceList`, `WorkspaceCreate` and `WorkspacesFilters` are static as well.

What actually makes the exclusion real is simpler and stronger than laziness:
`router.simple.tsx` must not import cockpit, workspace, editor or collab modules
**at all**, lazily or otherwise, and must share no module with `router.tsx` that
does. Then tree-shaking has no path to reach that code and Vite never emits it.
Whether the *full* router lazy-loads a route is irrelevant to what the *simple*
build contains.

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

**`summary` and `keywords` must be added to the list schema — this is not
free.** An earlier revision of this spec claimed the two were "already on
`CallOut` and cost no backend work". They are not on it. `CallOut`
(`web/api/calls.py:33-50`), the model `GET /api/calls` returns, carries only
`id`, `title`, `deadline`, `deadline_local_date`, `published_at`, `source_name`,
`source_slug` and `aggregate_score`. `summary`, `keywords`, `programme` and the
budget fields live on `CallDetailCallOut` (`calls.py:109-125`) — the **detail**
model, which is why the detail page renders them today and the list cannot.

The expanded card therefore needs real backend work: add the fields to `CallOut`
and to the row construction in `get_calls` (`calls.py:404-416`). It is a small
change, but it is a change, and §11 carries it as one.

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

- QR **170 px** (≈ 10.6 cm on a 55" screen). The 1/10 rule at the 1 m maximum
  scanning distance stated above needs **10 cm**, and the 150 px screen floor is
  only 9.4 cm — it satisfies the pixel floor and *fails* the distance rule. Where
  the two disagree the distance rule governs, because it is the one that decides
  whether a phone actually locks on.
- Title **34 px** — readable standing ~1.7 m back.
- Funder, days-to-deadline, budget.

One element stays big enough to read at 4 m, and it is the header: the board
title plus **the nearest deadline across all pages**, in the urgent colour.
That is the hook that makes someone stop. Without it the board is invisible to
anyone not already standing at it.

Pages cycle every `page_seconds` (default 30). The page count is **dynamic** —
approved calls are laid out eight per page, up to `max_pages`.

**Capacity is `items_per_page` × `max_pages`, 24 by default, and approval is
capped at it.** Two rules in an earlier revision could not both hold: "every
approved call is included" and "`max_pages` default 3". Approve a 25th call and
one of them breaks. The cap therefore belongs at the **approval** step, where a
human is present — the admin queue refuses the 25th approval and names what to
drop, or the admin raises `min_score` or `max_pages` deliberately. What must not
happen is the display silently truncating a list an admin approved: they would
have no way to see the loss.

**Paging does not add coverage for free; it dilutes exposure.** At three pages
any one call is on screen a third of the time. At six, a passer-by has a
one-in-six chance of seeing a given call and a full cycle runs three minutes,
which is longer than anyone stands in a corridor. `max_pages` (default 3) exists
for this reason: past it, raise `min_score` rather than adding pages. Without
the cap the threshold quietly becomes a lottery.

A footer shows `Sida N av M` and a progress bar, so someone who stopped knows
more is coming and roughly how long to wait.

Sizing is expressed in `vh`, not pixels, because most corridor displays are hung
portrait and the deployment screen is not known yet.

**But `vh` alone does not make one layout fit both orientations, and the default
of 8 does not fit a portrait column.** Eight cards in a single column need eight
170 px QRs — 1360 px of QR before a single title — which overflows a 1080 x 1920
portrait screen once titles and meta are added. So orientation changes the
*setting*, not just the CSS:

| Orientation | Grid | `items_per_page` |
| --- | --- | --- |
| Landscape 1920 x 1080 | 2 x 4 | 8 (default) |
| Portrait 1080 x 1920 | 1 x 4 | **4** |

The board renderer picks the default from the viewport aspect ratio and the
legibility floors (170 px QR, 34 px title) rather than trusting a single number
to work everywhere; `items_per_page` remains an override for a screen that
measures differently.

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

Returns exactly the card fields and nothing else: board title, `generated_at`,
and per approved item the call title, source name, `deadline_local_date`,
`budget_min` / `budget_max` / `currency`, and `Call.url`.

**No match reasons.** An earlier revision listed them in this payload, which
contradicted the rule two paragraphs above that the board never shows criterion
data. Budget, which the card requires, was missing from that same list. Both are
corrected here.

**This needs its own query — `build_dashboard_view()` cannot be reused.** An
earlier revision claimed it "already takes a bare `profile_id` and requires no
user". That is false. Its signature is
`build_dashboard_view(session, *, user_id: int, ...)`
(`src/call_manager/web/dashboard.py:230-242`): `user_id` is a **required**
keyword argument, and lines 325-332 use it to subtract that user's dismissals
from every result. A public request has no user to supply, and borrowing an
admin's id would silently apply one person's hides to a corridor wall.

So release 1 adds a small **userless** query: `board_items` joined to `calls`,
ordered by `board_items.position`, filtered to `Call.deadline` in the future. It
touches no decision table at all — which is the correct semantics anyway, since
a board is the institution's published list, not any individual's inbox. The
same query without the `board_items` join and with a `min_score` filter over
`call_rankings` feeds the admin approval queue.

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
- `build_dashboard_view()` — the ranking query, for the **logged-in** views
  only. The corridor board gets its own userless query instead (§6), because
  this one requires a `user_id` and subtracts that user's decisions
- The profiles API and pages (re-skinned into `SimpleShell`; contents unchanged)
- The admin users pages
- The Mantine theme tokens, the `openapi-fetch` client, generated types
- The weekly email digest, plus a decisions-aware filter — it already filters on
  `user_call_dismissals`, so the §4.1 migration must preserve that behaviour
  rather than rebuild it
- `subscriptions` + `PUT /me/subscriptions` + the `Settings.tsx` UI — **choosing
  which profiles you get a digest for** already works and is untouched. Note the
  limit, which release 1 inherits rather than closes: `PUT /me/subscriptions`
  (`web/api/me.py:205-254`) takes only `profile_ids` and diffs set membership —
  it inserts and deletes rows and never reads or writes
  `Subscription.digest_enabled`. So subscribe/unsubscribe is shipped; **pausing
  the digest for one profile without unsubscribing from it is not exposed**,
  even though the column exists (`models.py:325-327`)
- The profile bootstrap worker, prompt and PubMed lookup — local-only, unchanged

## 9. Testing

Five gates that make the design's claims true rather than asserted:

1. **Bundle gate.** Assert the `VITE_APP_EDITION=match` build output contains no
   cockpit/editor/collab code. Without this, "excluded" is a claim. Assert on
   **emitted content**, not on chunk filenames: Vite is free to fold an excluded
   module into a differently-named chunk, and a gate that greps for
   `cockpit-*.js` would then pass with the code shipped. Grep the emitted
   JavaScript for stable sentinels from the excluded modules instead.
2. **Route manifest drift guard covers both routers.** `router.test.tsx`
   currently pins one router against `routes.json` `spa_routes`. With two
   routers and one manifest, the simple router can silently drift.

   **A subset assertion is not enough, and on its own is hollow.**
   `frontend/routes.json` line 2 already lists `/cockpit` inside `spa_routes`,
   so a `router.simple.tsx` that still mounted every cockpit path would remain a
   perfectly valid subset. Subset catches an *unknown* path; it cannot catch an
   *excluded* one. The guard must therefore pin `router.simple.tsx` to an
   **exact allowlist** and assert the excluded prefixes are absent by name:
   `/cockpit*`, `/admin/drafting`, `/admin/golden-set`,
   `/admin/deleted-workspaces`.
3. **Public endpoint gates.** Unpublished board → 404. No auth required for a
   published one. An unapproved call never appears. A call past its deadline
   never appears. And the response body carries **no score field of any type and
   no score value** — phrasing this as "no score float" would not hold, because
   `CallRanking.aggregate_score` is a `SmallInteger`
   (`src/call_manager/db/models.py:357`) and an exposed integer score would sail
   through such a gate. Assert on the key set instead: no `aggregate_score`, no
   `embed_similarity` (the actual float, `models.py:368`), no `rationale`, no
   `quoted_evidence`, at any nesting depth.

4. **No release-1 screen links into the excluded half.** Assert that no component
   reachable from `router.simple.tsx` emits an href under `/cockpit`. The bundle
   gate (1) proves the cockpit *code* is absent; it says nothing about a link
   pointing at a route the server will 404. Two such links exist today
   (`CallRow.tsx:55`, `CallDetailBody.tsx:48`), so this gate starts red and
   removing them is what turns it green.

5. **Every route outside a pinned public allowlist carries an auth dependency.**
   Walk every route on the app and assert it.

   **Phrase it as an allowlist, not as "`/api/public/*` is the only
   unauthenticated prefix" — that version is false and the gate would start
   permanently red.** Authentication cannot precede login: `POST /auth/login`
   and `POST /auth/register` (`web/api/auth.py:74,135`) and `GET /healthz`
   (`web/routes/health.py:20`) are unauthenticated today and must stay so. So
   the gate pins a short, explicit list — `/api/public/*`, the session and OIDC
   endpoints, `/healthz` — and asserts every *other* route carries
   `Depends(get_current_user)`. The allowlist itself is the reviewed artefact:
   adding a line to it is a visible diff, which is the whole point.

   This matters because auth here is enforced **per route**
   (`Depends(get_current_user)`), not by middleware — there is no deny-by-default
   path check, so an endpoint becomes public by *omitting a line*. That is silent
   in both directions: the board needs the omission, and any other route that
   loses it ships wide open with nothing to notice. This gate is the only thing
   that would catch the second case.

Plus, per the repo convention, mutation-test each gate: break the thing the gate
claims to protect and confirm the gate goes red.

## 10. Open questions

- ~~Does the ranking output carry per-field match attribution?~~ **Resolved:
  yes.** `call_ranking_criteria` carries per-criterion rating, rationale and
  quoted evidence. §4.2 is upgraded accordingly.
- How many of the six criteria are actually populated in the live data today?
  The table exists; whether every ranker path fills all six is unverified.
  Measure before designing the chip row's empty state.
- Portrait or landscape corridor screen? §6 now specifies both — 2 x 4 with
  `items_per_page=8` landscape, 1 x 4 with `items_per_page=4` portrait — but the
  first real screen should still be measured, not assumed. The open part is
  which one gets deployed, not whether the layout exists.
- Is the digest's current per-profile targeting sufficient, or does release 1
  need the discipline-specific variants that peer universities run?

### Not yet designed

Three screens are in scope but unmocked, and they are the ones most likely to
come out wrong:

- **First run.** What a user sees before they have a profile. The profile drives
  all matching, so this path is load-bearing and entirely undrawn.
- **Search — the UI only. The backend already exists.** An earlier revision said
  "nothing behind it is designed"; in fact `build_dashboard_view()` already
  accepts `q` and implements escaped multi-field matching
  (`web/dashboard.py:211-227,240,335-336`), and `GET /api/calls` already accepts
  and forwards it (`web/api/calls.py:382,401-402`). The remaining work is the
  affordance and its result states, not a query. This is also the only place a
  researcher could ask *"why isn't call X here?"* — the blind spot §F2 of the
  matching review describes, which today has no UI surface even though the query
  behind it is shipped.
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
3. `call_decisions` — migrate `user_call_dismissals` and drop it (§4.1), remove
   the two `/cockpit/new` links, then the triage inbox and digest integration
4. Match reasons on the **list** view, replacing `MatchBadge`, plus the
   `summary`/`keywords` additions to `CallOut` the expanded card needs (§5). The
   detail page already renders chips, rationale and quoted evidence (§4.2) — the
   only work there is deleting its raw "Aggregate score" block
5. `boards` + `board_items` + the bulk admin approval queue (§4.3 — the gate is
   only real if approving 24 candidates is fast)
6. The public board endpoint + the paging `/b/:slug` display page
7. Share a call with a colleague
8. Change detection and notification (alone, last)
