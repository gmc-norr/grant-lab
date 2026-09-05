# Plan C (corridor boards) — the decisions the spec does not make

**Date:** 2026-09-05 (rev 3) · **Status:** decided with the human; implementation plan not yet written
**Spec:** [`specs/2026-09-03-call-match-release-1-design.md`](../specs/2026-09-03-call-match-release-1-design.md) §4.3, §6

## What this document is

Plan C builds the corridor board: `boards`, `board_items`, the admin approval queue,
and the public `/b/:slug` screen. The spec fixes the tables, the permissions, the grid,
the QR rules and what is deliberately not public.

This note records the decisions the spec does **not** make, plus **two deliberate
overrides of the spec** (D2 and D7). Rev 1 was reviewed by two outside models; rev 2
corrected the four errors they both found; rev 3 adds D7 and D8, and records three
questions that turned out to be **already answered by the code** rather than open.

## Revision history — and what rev 1 got wrong

Rev 1's four errors all had one cause: its author read §6's opening and its closing and
skipped the middle, which is exactly where the approval cap and the portrait layout
live. The code claims in rev 1 were all checked; the spec it was building on was not.

| rev 1 said | measured |
| --- | --- |
| "the owner must ask an admin to un-approve a call" | §4.3 gives **approve/reorder to any profile editor**. Only `is_published` is admin-only. |
| "capacity is still open; probably the first 24 by position" | §6 settles it at **approval** time, and "the display silently truncating a list an admin approved" is **explicitly forbidden** — rev 1 proposed the banned behaviour. |
| "three calls means three cards and five empty slots" | That is the **landscape** layout. §6: portrait is **1×4, `items_per_page` 4**. |
| D2 "fills a gap the spec does not address" | §6 **does** specify the queue as the userless query. D2 is an **override**. |

## Three questions that were not open — the code already answers them

Recorded because each was carried as an open question into rev 2, and each cost nothing
but a grep to close.

- **Deadline timezone.** `Call.deadline` is `DateTime(timezone=True)`, and
  `web/filters.py:37 deadline_local_date()` converts it to a **Europe/Stockholm**
  calendar date. Its docstring names itself "the SINGLE shared localization helper used
  by both the digest and the JSON API … prevents them from ever diverging." **The board
  reuses it.** A second conversion on the board would be the divergence that docstring
  exists to prevent.
- **"Closes in N days".** `frontend/src/routes/Deadline.tsx` and `deadlineLocalView`
  already derive it from that local date, and `TriageCard` already renders it. The board
  reuses them.
- **Rate-limit numbers.** `main.py` already builds
  `SlidingWindowLimiter(max_requests=30, window_seconds=15*60, name="ip")`. The public
  board reuses the same primitive at **60 per 15 minutes per IP**: D6 caches ~5 minutes,
  so one legitimate screen spends ~3 requests per window — 20× headroom for several
  screens behind one NAT, while still bounding a scraper.

## The question Plan B handed over

Plan B (**merged `740a2d53`**) closed with:

> **F-10.** `_base_call_filters` / `_decision_predicate` require a `user_id` (the
> docstring forbids growing separate copies), and §4.3's board is profile-scoped with
> no user in scope. Plan C must answer *"whose hides suppress a call on a shared board?"*

It has two halves and they get different answers. **That asymmetry is the single most
likely thing to be "fixed" by a later reader**, so each decision below ends with what it
does *not* say.

### D1. The public board applies NO decisions. (The spec's rule, restated.)

§6: the board is fed by a **userless** query — `board_items` joined to `calls`, ordered
by `board_items.position`, filtered to `Call.deadline` in the future. It touches no
decision table at all, "which is the correct semantics anyway, since a board is the
institution's published list, not any individual's inbox."

A deliberate, documented fork of the query, not accidental duplication: a public request
has no user, and borrowing an admin's id "would silently apply one person's hides to a
corridor wall."

**D1 does not say** that owner hides apply on `/b/:slug`. They never do.

### D2. The admin approval queue applies the PROFILE OWNER's hides. **Overrides the spec.**

Decided 2026-09-05, over "show everything" and "show hidden items flagged". §6 says the
same userless query feeds the queue; D2 changes the queue only. Plan C must carry this
as a recorded deviation, with §6 quoted beside it.

**Why the owner specifically.** A hide is keyed `(user_id, profile_id, call_id)`; Plan
B's model comment is explicit that "the key is per user, so a colleague's decision is
their own row." There is no "the group hid it" state, so any rule that respects triage
must name one person's rows.

**Key off the profile, not the board.** The predicate takes
`user_id = profiles.created_by WHERE profiles.id = boards.profile_id` — **not**
`boards.created_by`. Those differ: §4.3 lets any profile *editor* create a board, and
"can edit" is `created_by == user.id or user.is_admin`, so an **admin can build a board
on a profile they do not own**.

**The consequence that decided it.** §4.3 names the failure mode: "an approval UI that
costs a minute per call will be abandoned, and an abandoned gate is worse than no gate."
Respecting *everybody's* triage would be worse than either option: profiles default to
`'org'` (`alembic/versions/0070_profile_visibility.py`), so any logged-in user could keep
a call off a public wall.

**Honest about the limit.** D2 shortens the queue *only if that owner actually triages*.
It is not by itself a defence against the abandoned gate. The spec's bulk affordances
("approve everything at or above N", a visible waiting count) remain required.

**This resolves F-10's engineering half.** The queue is no longer userless, so it
**reuses** the existing predicate rather than forking it. Only D1's public query is
userless.

**D2 does not say** that owner hides apply to already-approved cards (see D3), nor that
the queue keys off `boards.created_by`, nor that any other user's hides count.

### D3. A hide never removes a call that is already on the board.

Approval is a separate, deliberate, recorded act (`board_items.approved_by`,
`approved_at`). A hide is the owner tidying their own inbox. Letting the second silently
undo the first overloads one gesture with two very different meanings.

**Un-approving is not admin-only.** §4.3 gives "approve/reorder items" to any user who
can edit the profile. The owner *can* take a card down — deliberately, on the board,
rather than by hiding it in their inbox. Only flipping `is_published` is admin-only.

**D3 does not say** that a hide auto-removes a card, or that deadline expiry deletes
`board_items` rows (it filters them from the query).

### D4. Making a profile private does nothing to a live board.

This interaction did not exist when the spec was written on 2026-09-03; profiles became
personal a day later in `ed1153a3`.

`visibility='private'` governs who may read the profile through the app's read surfaces.
The board's public payload names no profile — §6's "deliberately not public" list
includes "No profile text or profile identity beyond the board's own title". Publication
is a separate admin act with its own record.

Auto-unpublishing would let a non-admin's personal privacy tweak blank a hospital
corridor screen and undo an admin's decision — inverting the authority split the spec is
built on.

**Two carried cautions.** The board's *title* is free text an author chose and may name
the group even when the profile is private; Plan C should say so at the point the title
is set. And D4 governs an **already published** board only — whether a board whose
profile is *already* private may be published is not decided here.

### D5. An underfull board shows what it has. No filler, no reflow.

Empty slots are relative to the board's **stored** `items_per_page` (see D7): landscape
2×4 = 8, portrait 1×4 = 4. Three approved calls means three cards and one empty slot on
a portrait board, five on a landscape one. Zero means the board title and a quiet line.

**Word the empty state for what it is.** Zero *approved* items does not mean zero open
calls — the threshold set and the approved set are deliberately different. So the line is
"No calls are currently posted", not "No open calls right now", which would be a false
claim made in public.

Rejected "reflow to fill the space" because §6 pins card, type and QR sizes to **measured
legibility floors** (170 px QR, 34 px title). Sizing cards to queue depth makes those
floors a function of how many calls happen to be approved.

Rejected "don't publish below eight" because the screen would go dark exactly when the
admin is behind, and to a passer-by dark is indistinguishable from broken.

**D5 does not say** that the gap is an adequate operational alert. It is visible only to
passers-by, who cannot act on it. See the open question on queue monitoring.

### D6. The public page caches ~5 minutes. Last-good-copy is for TRANSPORT failures only, and is bounded.

Substantially revised in rev 2, because rev 1 broke unpublishing and both reviewers
caught it independently. §6: "Unpublished boards → 404, indistinguishable from a
nonexistent slug." Rev 1 said "if the fetch fails, keep rendering what it last had" —
and **a 404 is a failed fetch**, so an admin could unpublish a board and the screen would
keep showing it indefinitely. The emergency stop would not stop anything.

- **Successful fetch:** cache ~5 minutes. New approvals appear within about that.
- **Transport failure or 5xx:** keep rendering the last good copy — a stale board beats a
  browser error on a hospital wall. This is a requirement on the `/b/:slug` page, not
  just a cache header; an unattended screen on hospital wifi *will* lose the network.
- **404 or 410:** **blank the board.** This is the unpublish path and it must take
  effect. Never the old cards.
- **Bounded staleness:** the last-good copy has a maximum age. Past it the screen stops
  showing cards. An unattended screen must not present a month-old board as current.
- **Deadlines expire locally.** Even while serving a stale copy, the page stops rendering
  any card whose deadline has passed. Otherwise "no past deadlines" — a §6 "deliberately
  not public" rule — fails exactly when nobody is watching.

**D6 does not say** that a `Cache-Control` header is sufficient, nor that the
last-good-copy shares the 5-minute bound. Those are two different numbers and Plan C must
name both.

### D7. Orientation is STORED on the board. **Overrides the spec.**

Decided 2026-09-05, over "cap everything at 12" and "let the display truncate and say so".

**The contradiction this resolves.** §6 states three things that cannot all hold:

1. "Capacity is `items_per_page` × `max_pages`, 24 by default, and approval is capped at it."
2. Portrait uses `items_per_page` **4** → capacity **12**.
3. "The board renderer picks the default from the viewport aspect ratio."

If the renderer decides orientation, then at approval time nobody knows the capacity. A
board approved at 24 that lands on a portrait screen must drop 12 — and §6 explicitly
forbids "the display silently truncating a list an admin approved: they would have no way
to see the loss."

**The decision.** The board author picks **portrait or landscape** when creating the
board. `items_per_page` follows from it (4 or 8) and remains an override for a screen
that measures differently. The approval cap is computed from the **stored** value, so it
is always truthful. The renderer **obeys** the stored setting instead of inferring one.

This keeps §6's real intent — no silent truncation — and gives up only the convenience of
a board that reformats itself for whatever screen it lands on. That convenience was never
safe: it made a human's approval decision depend on a viewport nobody had measured.

**D7 does not say** that `items_per_page` disappears (it stays, as the override), nor
that a board may be moved between orientations without re-checking its approved count —
Plan C must decide what happens when an author flips orientation on a board that is
already over the new capacity, and that is the one thing D7 adds to Plan C's task list.

### D8. The board shows LIVE call data, always.

Decided 2026-09-05, over "snapshot the approved fields" and "flag material changes for
re-approval".

`board_items` stores a `call_id`; the public query joins live `calls`. So a funder editing
a title, budget or deadline reaches the wall with no renewed approval. That is accepted:
**approval approves the call, not a snapshot.**

**Why not a snapshot.** It would keep showing a deadline the funder has since corrected.
On a hospital wall a wrong date is worse than an unreviewed title, and corrections are the
common case while adversarial retitling is hypothetical.

**Why not re-approval flagging.** It adds a recurring chore, and §4.3 warns that an
abandoned gate is worse than no gate because the board silently stops updating.

**Bounded by what exists.** `Call` has **no status or withdrawn column** — measured, not
assumed. A withdrawn call cannot be represented anywhere in the app today, so "the board
drops withdrawn calls" is not implementable in release 1 and must not be claimed. The
only automatic removal is the existing deadline filter.

**D8 corrects D3's wording.** D3 said the board changes only when a human changes it or a
deadline passes. With D8 that is false as written: it also changes when the funder's data
changes. Plan C should carry D8's sentence, not D3's.

## Still open — for the human, before Plan C's Task 1

1. **Nobody is told the queue needs attention.** D5's gap is visible only on the corridor
   screen, to passers-by who cannot act on it. Decide what notifies a profile editor:
   live cards falling below `items_per_page`, a deadline dropping an item, or a
   waiting-count threshold. Not schema-blocking; it is a surface.
2. **Ownership succession.** D2 keys off `profiles.created_by`. Decide what happens to a
   board when that person leaves — reassign the profile, or the queue loses its reference.

## What Plan C must still measure for itself

Every `file.py:NN`, every count, and every gate. Nothing in this note carries a line
number that has not been checked against `master` at the time of writing, and Plan C's
own citations must be re-measured when it is authored — Plan B was written against a base
**1496 tests higher** than its brief claimed, and profile-visibility merging underneath it
left a migration-number collision and a test path under a directory that does not exist.

**One drift already found: do not copy the spec's line numbers.** §4.3 cites
`web/api/profiles.py:151` for the "can edit the profile" rule. The rule is unchanged, but
it now sits at **line 196** — and the file is `src/grant_lab/`, not `src/call_manager/`,
after the 2026-09-05 package rename (`623e6f33`). Every path in the spec and in documents
written before that date needs the same substitution; they are deliberately left as
written, because they are records of what the code was called at the time.
