# Corridor boards that publish themselves

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
settings form. Spec §6's landscape grid is 8 cards per page, so 142 qualifying
calls is an 18-page loop; at 30 s a page that is nine minutes, and a passer-by
sees one page.

The board settings screen must therefore show, live, for the current threshold:
**how many calls qualify, and how many pages that is.** Without it the admin is
tuning a number whose effect they cannot observe, which is the same abandoned-gate
failure in a different place.

## Taking a call down

**Archive the call.** `board_queue` already excludes archived calls, so an
archived call leaves the board on the next run and does not return.

This deliberately reuses one decision rather than inventing a second. The
consequence is stated plainly: archiving also removes the call from that profile
owner's own dashboard. That is one action with two effects, not two mechanisms —
and it is the correct shape, because "this call should not be on our wall" and
"this call is not for us" are the same judgement.

`DELETE /api/boards/{id}/items/{call_id}` continues to exist and continues to
remove a row. **On an auto board it is not a takedown** — the next run
re-publishes the call, because the call still qualifies. The UI must say so and
offer archiving instead. A delete that silently undoes itself overnight is worse
than no delete button.

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

**The QR is encoded here.** `qr_for` runs in this job, off any web request. This
is where the encode always belonged; `5e8329dd` had already moved it out from
under the approval route's row locks, and a background job removes the last
request-path cost.

**`approved_by` must become nullable.** Verified: it is
`nullable=False, ForeignKey("users.id", ondelete="RESTRICT")` today. A card nobody
approved has no approver, and inventing a system user to satisfy the column would
make the audit trail lie. `NULL` means **published automatically**, and is what
distinguishes an auto card from a human-approved one for the whole life of the
row. `approved_at` stays `NOT NULL` — the row was created at a time regardless.

## What does not change

- `approve_items`, `reorder_items` and the approval UI, for boards with
  `auto_publish = false` — the default, and every board that exists today.
- The public route. It reads `board_items` and cannot tell how a row got there,
  which is the point.
- Publishing (`is_published`) stays a separate, deliberate act. **Auto-publish
  fills a board; it never makes one world-readable.** A board that is not
  published stays private no matter what this job writes.
- The weekly board alert (`boards/alerts.py`, on the Monday digest tick). On an
  auto board "underfull" means the threshold is too high — the same message is
  still the right one. No new alert.

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
- **Per-board archive.** Rejected above: one decision, not two.
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
3. **Deleting an auto card.** The UI change described above ("this is not a
   takedown") is stated but not designed here.
