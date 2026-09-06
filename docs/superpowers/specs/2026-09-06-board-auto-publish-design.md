# Corridor boards that publish themselves

> **Rev 2, 2026-09-06.** An outside review (`gpt-6-astra`) returned five findings
> and all five were verified against the code. Rev 1's takedown story is
> **reversed**: archive-only left a bad call on a wall for up to twenty hours and
> reached every board on the profile, so this revision adds the per-board
> suppression it had rejected. Also fixed: `approved_by` nullability stopped at
> the column and would have broken `BoardItemOut`; the nightly job did not take
> the board row lock every other writer takes; the loop-length example
> contradicted this spec's own capacity rule; and the alert section asserted one
> cause where there are four.

## Why this exists

`2026-09-03-call-match-release-1-design.md` §"New table `board_items`" states the
rule this spec changes, and states it deliberately:

> **The board never publishes an unapproved call.** `min_score` *proposes* the
> candidate set; an admin ticks what goes up. Rationale: a corridor screen is the
> institution's voice, and a single bad rank on a hospital wall is a public
> incident, not a bug report.

That rationale is sound and is not withdrawn. What has changed is the estimate
sitting underneath it. The same section already names the failure mode:

> An approval UI that costs a minute per call will be abandoned, and an abandoned
> gate is worse than no gate because the board silently stops updating.

Release 1 bet that a bulk "approve everything at or above N" made the gate cheap
enough to survive. The operator's judgement, 2026-09-06, is that it will not: the
admin will not come back. So the choice is not *gate versus no gate*. It is
**a gate nobody operates versus a board that maintains itself**, and a board that
silently stops updating is the outcome the release-1 rationale was written to
prevent.

This spec therefore makes the gate **optional per board**, and leaves it on by
default. No existing board changes behaviour.

**This spec supersedes that rule only for boards with `auto_publish = true`.**
The release-1 spec is a record of what was decided then and is not edited.

## What an auto board shows

Exactly the candidate set that already exists — `board_queue`
(`src/grant_lab/boards/queue.py`), verified at `a8e31f98`:

- `CallRanking.aggregate_score >= board.min_score`
- `Call.deadline IS NOT NULL AND Call.deadline > now()`
- not archived — `_decision_predicate("archive", user_id=<profile owner>, profile_id=board.profile_id)`
- the ranking join is scoped to **this board's own profile**, so a call ranked
  against another group's profile cannot qualify on this one

Ordered by `aggregate_score` descending, then `Call.id` descending — the same
order `board_queue` already returns.

**The threshold is the floor; the page capacity is the backstop.** Everything
qualifying is published, across as many pages as it needs, up to
`items_per_page * max_pages`. Past that ceiling the highest-scoring ones are
published and the remainder are not. `max_pages` stops being a routine constraint
and becomes a runaway guard.

Bounds, verified: `items_per_page` is `BETWEEN 1 AND 64`
(`ck_boards_items_per_page`, migration 0074) and `max_pages > 0`
(`ck_boards_max_pages`). Neither is changed here. `max_pages` is currently not
exposed on `BoardSettingsIn`; **this spec exposes it**, because on an auto board
it is the only lever that bounds loop length, and an admin who cannot raise it
cannot fix a truncated board.

### The number the admin needs to see

Loop length is the real cost of a low threshold, and it is invisible from the
settings form.

Rev 1 got its own example wrong, in a way worth keeping as the reason this
preview needs four numbers and not one. It said "142 qualifying calls is an
18-page loop." That contradicts the capacity rule two paragraphs above it.
Measured defaults (`db/models/boards.py:93-101`): `items_per_page = 8`,
`max_pages = 3`, `page_seconds = 30`. So on a default board, 142 qualifying calls
give **24 displayed, 118 truncated, 3 pages, a 90-second loop** — not 18 pages.
Eighteen pages is the capacity 142 calls would *need*, and it only becomes the
loop if the admin first raises `max_pages` to 18, at which point the loop is nine
minutes and a passer-by sees one page of eighteen.

Both of those are decisions the admin should make on purpose. So the settings
screen must show, live, for the current threshold and capacity, all four:

- how many calls **qualify**
- how many are **displayed**
- how many are **truncated** by `items_per_page * max_pages`
- the resulting **pages and loop duration**

One number cannot express this, because the failure is silent in both directions:
a threshold set too low truncates invisibly, and a `max_pages` raised to fix that
makes any given call visible 6% of the time.

## Taking a call down

**One action: "Remove from this board."** It deletes the `board_items` row and
records a suppression, in one transaction. The call is off the wall immediately
and the nightly job does not put it back.

New table `board_suppressions`:

| column | type | notes |
| --- | --- | --- |
| `board_id` | FK -> boards, ON DELETE CASCADE | |
| `call_id` | FK -> calls, ON DELETE CASCADE | |
| `suppressed_by` | FK -> users, ON DELETE RESTRICT | who took it down |
| `suppressed_at` | timestamptz NOT NULL | |

Primary key `(board_id, call_id)`. The nightly job excludes suppressed calls for
that board. No expiry: a call taken off a wall stays off until a human puts it
back.

**Approving a call clears its suppression.** `approve_items` deletes the row for
that `(board_id, call_id)` in the same transaction. An explicit human approval is
a newer and stronger signal than an old removal, and without this a call removed
once could never be re-approved on a manual board.

**DELETE records a suppression on manual boards too.** Uniform behaviour beats a
rule that depends on a mode, and it means flipping a board to auto does not
resurrect everything its owner ever removed.

### Why not archive, which is what an earlier revision proposed

Rev 1 reused the existing archive decision instead, on the grounds that "this call
should not be on our wall" and "this call is not for us" are the same judgement.
Two things make that wrong, and the second is decisive:

1. **Scope.** `boards.profile_id` carries a plain index, not a unique constraint
   (`db/models/boards.py:74`), so several boards can share one profile. The
   archive predicate is scoped `(owner, profile)` (`boards/queue.py:50-52`), so
   archiving pulls the call off **every** wall on that profile, not the one with
   the problem.
2. **Latency.** Archiving changes the candidate set, and the candidate set is only
   consulted at 05:00. A bad call noticed at 09:00 would stay on a hospital wall
   for **twenty hours**. A takedown that is not immediate is not a takedown, and
   the whole safety argument for making approval optional rested on it.

A suppression list is an **exception action, not a second approval workflow**. It
is touched when something is wrong, which is rare; it is not the recurring chore
this spec exists to remove. Rev 1 rejected it by confusing the two.

Archiving remains available and still works — it removes the call from the
candidate set for every board on that profile *and* from the owner's dashboard,
which is the right tool when the call genuinely is not for the group.

## Who writes the rows

A new nightly job, `boards:auto-publish`.

**Timing.** Verified schedule at `a8e31f98`: embeddings refresh 03:30, ranking
reconcile 04:00, and the ranking queue itself drains on an interval
(`RANKING_TICK_SECONDS`, default 60 s). There is therefore **no instant at which
"ranking is finished"** — the drain is asynchronous and unbounded. The job runs at
**05:00**, is idempotent, and is correct if run mid-drain: it publishes what the
scores say at that moment, and the next night corrects it. No completion signal is
invented.

**What it does, per board with `auto_publish = true`:** compute the set above,
then make `board_items` match it — insert what is missing, delete what no longer
qualifies, and rewrite `position` to the score order.

**It takes `_lock_board_row` and re-reads under it.** This job is a third writer
alongside `update_board` (`web/api/boards.py:344`) and `approve_items` (`:635`),
both of which lock the board row *before* making any decision from a read,
precisely because a decision made from an earlier read is invisible to a
concurrent commit. Without it the job can read `auto_publish = true`, compute a
replacement set, and write it after a settings request has switched automation
off — silently breaking this spec's own promise that turning it off freezes the
board.

So: encode outside the lock, then take the lock, then **re-read `auto_publish`,
`min_score`, `items_per_page` and `max_pages` from the locked row** and discard
any precomputed selection that those values invalidate. If `auto_publish` is now
false, the job does nothing to that board.

**The QR is encoded here.** `qr_for` runs in this job, off any web request. This
is where the encode always belonged; `5e8329dd` had already moved it out from
under the approval route's row locks, and a background job removes the last
request-path cost.

**`approved_by` must become nullable — in the column AND the response model.**
Verified at `a8e31f98`: the column is
`nullable=False, ForeignKey("users.id", ondelete="RESTRICT")`, and
`BoardItemOut.approved_by` is typed `int` (`web/api/boards.py:457`) with
`_item_out` passing `item.approved_by` straight into it (`:475`). Changing only
the column makes every auto board's item list fail pydantic validation on the way
out. Both change together, to `int | None`.

Blast radius, measured: `approved_by` appears in the frontend only in
`src/api/schema.gen.ts:2821` (typed `number`) and in `BoardDetail.test.tsx`
fixtures. **It is not rendered anywhere in the UI**, so the plan needs an OpenAPI
regeneration and a fixture update, not a component change.

A card nobody approved has no approver, and inventing a system user to satisfy the
column would make the audit trail lie. `NULL` means **published automatically**,
and stays that way for the life of the row — including after a board returns to
manual mode. `approved_at` stays `NOT NULL`; the row was created at a time
regardless.

## What does not change

- `approve_items`, `reorder_items` and the approval UI, for boards with
  `auto_publish = false` — the default, and every board that exists today.
- The public route. It reads `board_items` and cannot tell how a row got there,
  which is the point.
- Publishing (`is_published`) stays a separate, deliberate act. **Auto-publish
  fills a board; it never makes one world-readable.** A board that is not
  published stays private no matter what this job writes.
- The weekly board alert (`boards/alerts.py`, on the Monday digest tick). Its
  trigger, verified: `boards_needing_attention` fires for a **published,
  unmuted** board whose renderable count is below `items_per_page` — i.e. it
  cannot fill even one page. No new alert is added.

  Rev 1 claimed that on an auto board this means "the threshold is too high."
  That names one cause out of four. A thin board can equally be a candidate set
  emptied by **deadlines passing**, by **archiving**, or by **suppression** —
  and after this spec, suppression is a cause an operator creates by hand. The
  plan must word the auto-board message so it does not assert a cause it has not
  established.

## Flipping the switch

Turning `auto_publish` on **replaces the board's hand-picked items** on the next
run, because the derived set is authoritative. The settings screen must say this
before the flip is saved, naming how many current items would be dropped.

Turning it off freezes whatever is currently up and hands control back to
`approve_items`. Auto-published rows keep `approved_by = NULL`; they are not
retroactively attributed to whoever flipped the switch.

## Deliberately not in scope

- **Pinning.** No "always keep this card up" on an auto board. It reintroduces
  curation, which is the thing being removed, and nobody has asked for it.
- **Un-suppressing from a list.** Suppression is cleared by approving the call,
  which is a path that already exists. A management screen for the suppression
  table is not in release 1; if an operator suppresses something by mistake, they
  approve it back.
- **A choice of selection model.** There is one rule — threshold floor, capacity
  backstop — because a setting the admin must understand is a cost paid by the
  same person who has no time to approve calls.
- **A digest of what went up.** Considered and dropped. It is a second thing to
  read, and the board itself is the report.

## Open, for the implementation plan to settle

1. **Churn.** Scores move nightly, so a card can appear and vanish between days.
   Bounded by how often rankings actually change, which is **not measured**.
   The plan should measure it on real data before deciding whether any damping is
   needed. `PREDICTED (not run)`: most nights change little.
2. **Position stability.** Rewriting `position` every night to score order means
   cards move. Whether the wall should preserve a stable order and only append is
   a product question the measurement in (1) informs.
3. **What the remove button offers.** Rev 2 makes DELETE a real takedown, so the
   stale rev-1 question ("warn that it undoes itself") is gone. The open one is
   whether the same control should also offer archiving — "off this board" versus
   "not for us at all" — or whether presenting two choices at the moment someone
   is reacting to a bad card on a wall is one choice too many. Not designed here.
4. **Suppression and `ON DELETE RESTRICT`.** `suppressed_by` mirrors
   `board_items.approved_by`'s `RESTRICT`, which means a suppression row blocks
   deleting the user who made it. Whether that is right, or whether suppression
   should outlive its author with a nullable column the way `approved_by` now
   does, is a question the plan should answer rather than inherit.
