# Plan C (corridor boards) — the decisions the spec does not make

**Date:** 2026-09-05 (rev 6) · **Status:** decided with the human; implementation plan not yet written
**Spec:** [`specs/2026-09-03-call-match-release-1-design.md`](../specs/2026-09-03-call-match-release-1-design.md) §4.3, §6

## What this document is

Plan C builds the corridor board: `boards`, `board_items`, the admin approval queue,
and the public `/b/:slug` screen. The spec fixes the tables, the permissions, the grid,
the QR rules and what is deliberately not public.

This note records the decisions the spec does **not** make, plus **two deliberate
overrides of the spec** (D2 and D7) and **one capability the app does not have yet**
(D10, profile transfer). Rev 1 was reviewed by two outside models; rev 2 corrected the
four errors they both found; rev 3 added D7 and D8 and recorded three questions that
turned out to be **already answered by the code** rather than open; rev 4 closed the two
remaining open questions as D9 and D10, and corrected rev 3's own statement of the
second one; revs 5-6 are the outside review of D9 and D10, which found **nine** errors in
rev 4 — including one where rev 4 was wrong in the **opposite** direction from the code,
and one where the note printed a command that does not produce the number beside it.

**D10 widens Plan C's footprint beyond boards.** Transferring a profile touches the
permission model seven other surfaces read from, so Plan C must either carry it as its
own slice with its own gates or hand it to a separate plan. That is a sequencing call
for the plan author; it is not a reason to soften the decision.

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

### What rev 4 got wrong — D9 and D10, found by outside review

Three models reviewed rev 4 and all three returned findings; they **independently agreed
on three of these**. Every row was verified locally against `master` before this edit —
the models could not run anything, and two of their findings were themselves wrong.

| rev 4 said | measured |
| --- | --- |
| transfer means "the previous owner loses the ability to read it" | Only if they are **not an admin**. `profile_access.py:61` short-circuits `if user.is_admin` and returns every profile. Corroborated by both models. |
| a departed owner's queue grows "with nobody able to reach it" | Every active admin can still open and act on it (`profiles.py:142`, `:400`). The defect is **addressing**, not reachability. Corroborated by both models. |
| the open points are such that "none of it changes a table" | D9's own second-boolean option is an `ALTER TABLE subscriptions`. A self-contradiction inside one document. |
| `digest_enabled` is filtered by `digest/send.py:38` | It is filtered at `digest/assemble.py:95`. `send.py:38` filters the **user**, not the subscription. Corroborated by both models. |
| the old owner's stale `Subscription` row means they "keep getting the digest for a profile they may no longer read" | **Backwards.** `assemble.py:97` already filters the digest through `readable_profile_ids`, and `assemble.py:86-89` says deleting that row is forbidden because it destroys the `digest_enabled` preference. Rev 4 proposed the bug that comment exists to prevent. |
| D9's three triggers "all read the same two tables" | They do not. Two read `boards`/`board_items`/`calls`; the waiting-count trigger also touches `call_rankings` and `call_decisions`. |
| `grep -rn "created_by *=" src/grant_lab/web/api/` "returns three hits" | That command returns **5**: ` *=` also matches `created_by ==`. The three-hit count came from a run that filtered `==` out, and rev 4 printed the command without the filter — so anyone re-running it got a different number. The conclusion was right and the evidence was not reproducible. |
| "Still open" was "scoped *inside* those decisions" and complete | Two of its items were carried over from D7 and D4, and it omitted **every** open item D10 itself raises. Now one list, marked by where each was raised. |
| "the OWNER never has a row" attributed to `profile_access.py` | The phrase is in the `ProfileViewer` docstring (`db/models/profiles.py:136`). `profile_access.py` states the same fact in different words. Fact right, attribution wrong. |

Three further findings were **new**, not corrections — the stale `profile_viewers` row on
transfer (now in D10), the bounce-disabled owner (now in D9), and a defect in the **code**
rather than the note: `profile_access.py`'s docstring cited `profiles.py:342` for the
edit rule, which is now a Pydantic form field. Fixed in `c84bb4f5`, which names the
function so the next move survives. One model found it directly; it was also reached by
chasing a different finding, from the other end.

**Two findings were refuted by running the check the reviewers could not.** "Other
user-deletion paths may exist" — a repo-wide grep finds exactly one, `admin_users.py:210`,
so D10's claim stands. "Some `Profile` creation path may skip the subscription" — both
insertion paths subscribe the owner; the real, narrower point (no constraint enforces it)
is now recorded in D10.

**One finding was about the review package, not the note.** Four of D10's citations point
at files the package did not include (`web/api/calls.py`, `admin_golden_set.py`,
`workspaces.py`), so no reviewer could check them. They were verified locally instead. The
lesson is for the next package, not for this document: include the file behind every
`file.py:NN` the document cites, or expect that citation to come back unchecked.

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

### D9. The PROFILE OWNER is told the queue needs attention.

Decided 2026-09-05, closing rev 3's first open question. D5 established that an
underfull board is visible only to passers-by, who cannot act on it. The person told is
the same person D2 already keys the queue off: `profiles.created_by`.

**The channel already exists and is already populated for exactly this person.**
`profiles/create.py:79` inserts `Subscription(user_id=owner_id, profile_id=p.id,
digest_enabled=True)` inside the same atomic create as the profile row. Both paths that
insert a `Profile` do subscribe the owner — see D10's measurement — so `subscriptions`
is a per-(user, profile) row for every profile owner **today**, keyed the same way the
board is. Plan C adds no new addressing table and no new recipient concept.

It is a convention, not a constraint (D10 again): nothing enforces it, so if D9's alert
depends on the row existing, Plan C should assert it rather than trust it.

**Not the board's creator.** Consistent with D2: §4.3 lets any profile editor create a
board, so `boards.created_by` may be an admin who built it for somebody else. Notifying
them would tell the wrong person a queue they do not own is short.

**Two things Plan C must decide, not assumed here.**

1. **Whether it reuses `digest_enabled` or needs its own flag. This one DOES change a
   table.** The column is read at `digest/assemble.py:95`
   (`Subscription.digest_enabled.is_(True)`) — **not** in `send.py`, which was rev 4's
   citation and was wrong; `send.py:38` only filters the *user*. Reusing it means
   switching off the weekly email also switches off the corridor-board alert, which is a
   different thing with a different urgency. A second boolean on the same row is the
   cheaper honest option — and it is an `ALTER TABLE subscriptions` plus a migration, so
   Plan C must settle it **before** its schema task, not alongside.
2. **The trigger.** Rev 3 listed three candidates — live cards falling below
   `items_per_page`, a deadline dropping an item, a waiting-count threshold. Still open.
   Rev 4 claimed all three "read the same two tables"; that was wrong. The first two read
   `boards`, `board_items` and `calls`; the waiting-count trigger reads the D2 queue,
   which additionally touches `call_rankings` and `call_decisions`. They differ in cost
   and in staleness, and Plan C must enumerate each one's query rather than treat them as
   interchangeable. None of them **adds** a table.

**The gap D9 does not close — and it has TWO paths, not one.** `digest/send.py:38`
filters on both `User.status == 'active'` **and** `digest_disabled_at IS NULL`.

- **A disabled owner** is silently not notified. D10 is the answer to that one: the
  person has left, so ownership should move.
- **A bounce-disabled owner** is silently not notified *and is still here*.
  `send.py:183-184` sets `digest_disabled_at` once `digest_bounce_count` reaches
  `BOUNCE_DISABLE_THRESHOLD` (**3**, `send.py:18`), leaving `status == 'active'`. That
  owner can still log in and triage. **D10 does not fix this** — transfer is the wrong
  remedy for someone who has not left — and rev 4 wrongly folded it into D10.

Nothing anywhere reports that a notification had no recipient. Plan C must make the
silence visible rather than assume the mail went out; a delivery the system knows it
suppressed is the one case where an in-app surface is not optional.

**D9 does not say** that every profile *editor* is notified, nor that a `ProfileViewer`
is, nor that the alert appears anywhere other than the owner's inbox.

### D10. A profile can be TRANSFERRED to another user. New capability.

Decided 2026-09-05, closing rev 3's second open question, which asked what happens to a
board when the owner leaves.

**Rev 3 stated the failure mode wrongly, and the schema says so.** Rev 3 said the queue
"loses its reference". It cannot: `Profile.created_by` is
`ForeignKey("users.id", ondelete="RESTRICT")` and `nullable=False`
(`db/models/profiles.py:96-98`). Postgres **refuses** to delete a user who owns a profile. And
the only route that deletes a user at all, `admin_release_user`
(`web/api/admin_users.py:195`), raises 400 unless `u.status == "pending"` — a user who
has never been approved and therefore cannot have created anything. Leaving is
`admin_disable_user` (`admin_users.py:216`), which sets `status = "disabled"` and bumps
`session_epoch`.

So the real state of a departed owner is: **row present, FK intact, queue still resolves,
owner cannot log in, and — per D9 — is not emailed.**

Rev 4 then overreached and said the queue grows "with nobody able to reach it". **That is
false.** §4.3 gives approve/reorder to anyone who can edit the profile, which is
`created_by`-or-admin (`profiles.py:142`, `:400`), so every active admin can still open
the queue and act on it. The accurate statement is narrower and still bad enough: **the
one person the system tells is the one person who cannot act, and nobody else is told.**
Reachability is not the problem; addressing is. That is worse than a dangling reference,
because nothing is broken enough to notice.

**So Plan C has an escalation question D9 did not raise.** When the owner is disabled or
undeliverable, either the alert escalates to active admins, or transfer becomes a
precondition of disabling a user. Rev 4 assumed D10 made this moot; it does not, because
the bounce-disabled owner in D9 has not left.

**Measured: nothing in this codebase reassigns ownership today.** `created_by` is
written at creation and never updated. The command is
`grep -rn "created_by *=" src/grant_lab/web/api/*.py | grep -v "=="`, and **the filter is
not optional**: the pattern also matches `created_by ==`, so unfiltered it returns **5**,
two of them the edit gate itself. Rev 4 printed the unfiltered command beside the
filtered count, so a reader who ran it got a different number than the note claimed.
Filtered, it returns three hits, none of them a profile and none of them a reassignment: a
golden-set row's author (`admin_golden_set.py:94`), an `ArtifactVersion` built with the
literal string `"user"` (`workspaces.py:622`), and a response DTO copying a value it
just read (`workspaces.py:1337`). Transfer is genuinely new work,
not a widening of an existing endpoint.

**What transfer must move.** `created_by` is not a label; it is load-bearing in seven
places (measured, `grep -rn "Profile.created_by\|p.created_by" src/grant_lab/`):

| site | what it decides |
| --- | --- |
| `web/profile_access.py:69` | the **owner arm** of `readable_profile_ids` |
| `web/api/profiles.py:142` | one route's edit gate |
| `web/api/profiles.py:196` | the `can_edit` flag the SPA renders from |
| `web/api/profiles.py:400` | the update gate |
| `web/api/profiles.py:544` | viewer-list writes drop the owner's own id |
| `web/api/profiles.py:612` | adding the owner as a viewer is a deliberate no-op |
| `web/api/calls.py:641` | a call route scoped to profiles the user owns |

**The consequence that must not be silent: transfer moves READ access — for a non-admin.**
For a `visibility='private'` profile, `created_by` is normally the only arm granting the
old owner anything: they hold no `profile_viewers` row (`profiles.py:612` makes adding
one a no-op) and `visibility == 'org'` does not apply. So a **non-admin** previous owner
loses the ability to read the profile the moment it transfers, and the transfer UI must
say so before the act rather than after.

**An admin previous owner loses nothing.** `readable_profile_ids` short-circuits on
`if user.is_admin` (`profile_access.py:61`) and returns every profile. Rev 4 stated the
revocation unconditionally and was wrong. The warning the UI shows must be computed from
the actual post-transfer predicate, not from a fixed sentence.

**The stale-row hazard rev 4 missed.** `profiles.py:612` only refuses to add a viewer row
for the user who is the owner **at that moment**. An active non-owner may therefore hold
one legitimately — and if the profile is then transferred **to that person**, their
`profile_viewers` row survives, because nothing deletes it. D10's premise ("the owner
never has a row") is now false for that profile, and a *later* transfer away leaves them
with read access nobody granted deliberately. **Transfer must delete the incoming owner's
viewer row in the same transaction**, or the invariant stated in the `ProfileViewer`
docstring (`db/models/profiles.py:136`, "the OWNER never has a row") stops
being true.

**Who may transfer.** Owner-or-admin, the rule already at `profiles.py:400`. The admin
arm is not a convenience here — it is the entire point, because the case that motivated
D10 is an owner who can no longer log in.

**Follow-on rows — and one the codebase has ALREADY decided, against rev 4.**

- **The old owner's `Subscription` row: leave it. Do not "clean it up".** Rev 4 said they
  would otherwise "keep getting the digest for a profile they may no longer read". That is
  **false**: `digest/assemble.py:97` already filters the digest through
  `readable_profile_ids(user)`, so the section simply stops appearing. And the comment
  above it (`assemble.py:86-89`) is explicit that revocation must **not** touch
  `subscriptions`, because deleting the row destroys the user's `digest_enabled`
  preference and a later re-share can never restore it. Deleting on transfer would be
  exactly the bug that comment exists to prevent.
- **The new owner may have NO subscription row** — transfer must create one, mirroring
  `profiles/create.py:79`, or D9 notifies nobody. This is the follow-on that actually
  needs a write.
- **`users.default_profile_id`** (`db/models/identity.py:87`) may still point at the transferred
  profile for the old owner.

**The invariant is a convention, not a constraint.** Both paths that insert a `Profile`
do subscribe the owner — `profiles/create.py:79` atomically, and
`scripts/seed_dev_data.py:302` in a **separate transaction** after its own commit. No
CHECK, FK or unique index enforces it, so "every owner has a subscription" is true today
by inspection and could stop being true without anything going red. If D9's alert depends
on it, Plan C should assert it rather than trust it.

**D10 does not say** that disabling a user auto-transfers their profiles (it does not —
transfer is a deliberate act), nor that ownership may be transferred to an inactive user,
nor that the old owner retains read access. If they should, that is an explicit
`ProfileViewer` row written by the transfer, and Plan C must say so.

## Still open

Rev 3's two open questions are closed above as D9 and D10. Rev 4 said what remained was
"scoped inside those decisions" and that "none of it changes a table". **Both halves were
wrong**: two items below are carried over from D7 and D4, not from D9 or D10; and one is
a schema decision that must be taken before Plan C's schema task, not after it.

This is the complete list — every open point in this document, wherever it was raised.

**Must be decided BEFORE the schema task:**

1. **D9's flag** — reuse `digest_enabled` or add a second boolean to `subscriptions`.
   The second option is an `ALTER TABLE` plus a migration. Deciding it late means
   re-opening a table Plan C has already shipped.

**Decidable at any point — no table changes:**

2. **D9's trigger** — which of the three signals fires the alert. All three read existing
   tables, but not the same ones; see D9.
3. **D9's escalation** — who is told when the owner is disabled or bounce-disabled.
   Reads existing tables; the answer may be "active admins", which needs no new column.
4. **D7's flip** — what happens when an author changes orientation on a board already
   over the new capacity.
5. **D4's boundary** — whether a board whose profile is *already* private may be
   published at all. D4 governs an already-published board only.

**Raised by D10, and omitted from rev 4's list entirely:**

6. **Does the outgoing owner keep read access?** If yes, transfer must write them a
   `ProfileViewer` row; if no, the UI must warn a non-admin owner before the act. D10
   settles the mechanism, not the policy.
7. **`users.default_profile_id`** may still point at the transferred profile for the old
   owner. Decide whether transfer clears it.
8. **Who may receive a profile?** D10 says ownership must not move to an inactive user,
   but does not say whether a `ProfileViewer`, any active user, or only a named colleague
   is eligible.

Items 6-8 are writes inside the transfer transaction, not new tables.

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
