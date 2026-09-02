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
| `max_items` | int, default 8 | |
| `rotate_seconds` | int, default 12 | spotlight dwell time |
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

**The board never publishes an unapproved call.** The ranker *proposes* a
candidate list in the admin UI; an admin ticks what goes up. Rationale: a
corridor screen is the institution's voice, and a single bad rank on a hospital
wall is a public incident, not a bug report. The cost is one table and one
checkbox column.

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

## 6. The corridor board

### Constraints (hard)

From the signage literature, treated as requirements:

- Readable text height = viewing distance (m) × 0.84 cm, then × 1.5 for comfort.
- Contrast ratio ≥ 7:1 for all text.
- Sans-serif, at most two fonts, no italics.
- A QR code must be ≥ 1/10 of its scanning distance, and ≥ 200 px on screen.

### Two zones, two distances

A corridor screen has two audiences and the design serves both:

- **Spotlight zone** — for someone walking past at ~4 m. Text ≥ 80 px on a
  55" 1080p screen (4 × 0.84 × 1.5 = 5 cm). Shows one call: title (max 3 lines),
  its match reasons, funder, days-to-deadline, and its QR code.
- **List zone** — for someone who stops at ~2 m. Text ≥ 40 px
  (2 × 0.84 × 1.5 = 2.5 cm). Shows the remaining ranked calls: rank, short
  title, days left.

The spotlight advances down the list every `rotate_seconds`. At any instant the
whole ranked list is visible; over one cycle each call gets full-size treatment.

Sizing is expressed in `vh`, not pixels, so the same page works on a portrait
screen — most corridor displays are hung portrait, and the deployment screen is
not known yet.

### What is on the board

Per call: title, funder/source, days-to-deadline, a one-line snippet of the
call's **own summary**, and a QR code.

Note the deliberate difference from the logged-in view: the board shows the
funder's own words, never the criterion chips or `quoted_evidence`-plus-
`rationale` expansion. Criterion ratings are judgements *about this
institution's fit*; a summary snippet is public information the funder already
published. Both answer "why is this here?", but only one is safe on a wall.

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

## 11. Sequencing

1. `SimpleShell` + `router.simple.tsx` + the build flag + the bundle gate
2. Re-skin the existing calls / profiles / account / admin-users pages into it
3. `call_decisions` + the triage inbox + digest integration
4. Match reasons replacing the score badge
5. `boards` + `board_items` + admin board management
6. The public board endpoint + the `/b/:slug` display page
7. Share a call with a colleague
8. Change detection and notification (alone, last)
