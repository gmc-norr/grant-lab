# Feed Hygiene — Closed-Call Handling — Design Spec

**Date:** 2026-06-11
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager

## Purpose

The dashboard feed is the product's source of truth, but it presents **expired calls as live, urgent, and counted** — the single biggest trust-killer in the daily surface (Theme 3 of the 2026-06-11 product review). This slice makes the feed honest about what is still open, while keeping closed calls reachable for reference.

### The problem, with evidence (current code)

1. **The feed query has no deadline filter.** Both the ranked (profile-selected) and unranked branches list every non-dismissed call regardless of deadline ([calls.py:134-169](../../../src/call_manager/web/routes/calls.py#L134-L169)). In profile view the rows are sorted by `aggregate_score DESC` ([calls.py:142-147](../../../src/call_manager/web/routes/calls.py#L142-L147)), so a **closed call with a high score sits at the very top** — the worst place for a dead call.
2. **A past deadline is styled as urgent.** `deadline_label` sends any deadline `days <= 14` (which includes every *negative* value) into the red `deadline-soon` branch ([calls_index.html:22-23](../../../src/call_manager/web/templates/calls_index.html#L22-L23)). An expired call shows its past date in alarm-red.
3. **"Open calls" counts dead calls.** The badge renders `summary.total`, a count over the filter set with no deadline cut ([calls.py:179](../../../src/call_manager/web/routes/calls.py#L179), [calls_index.html:42-43](../../../src/call_manager/web/templates/calls_index.html#L42-L43)). The "Top matches" badge (`aggregate_score >= 70`) likewise counts closed calls ([calls.py:193-207](../../../src/call_manager/web/routes/calls.py#L193-L207)). (The "Deadline within 30 d" badge already excludes the past — [calls.py:186-188](../../../src/call_manager/web/routes/calls.py#L186-L188) — and is unchanged.)

## Goals

- The default feed shows only calls a researcher can still act on; closed calls are hidden but **one toggle away**, never silently deleted.
- A closed call can **never** outrank an open one — not in any sort, not even when closed calls are being shown.
- The summary badges count only actionable (open) calls, so the numbers a clinician trusts are honest.
- A past deadline reads as "Closed," not as an urgent red alarm.
- No schema change, no migration, no background reaper.

## Key definition

"Closed" is **derived at query time** from `deadline` — there is no `status` column and no reaper job (that is a separate Theme 3 item).

- **closed** ⟺ `deadline IS NOT NULL AND deadline <= now`
- **open** ⟺ `deadline IS NULL OR deadline > now`

A call with **no deadline** (e.g. Vinnova rolling calls) is therefore **open** and always visible — this is correct; those calls accept applications indefinitely.

The same comparison (`deadline <= now`, a full timestamp compare) is used in the query, the summary counts, and the template, so a call that closes partway through "today" is classified identically everywhere (no `.days`-truncation drift between the query and the rendered label).

## Components

### A. Feed query — filter + structural sort ([calls.py](../../../src/call_manager/web/routes/calls.py) `calls_index`)

1. **New query param:** `show_closed: bool = Query(default=False)`. An unchecked HTML checkbox submits nothing, so the natural default is false.
2. **Closed-last sort key (every branch).** Define `closed_expr = and_(Call.deadline.isnot(None), Call.deadline <= _now)` and prepend `closed_expr.asc()` as the **first** `order_by` term in *both* the ranked and unranked `select` statements. Booleans sort `False (0)` before `True (1)`, so open calls always precede closed ones. This is the structural guarantee from Goal 2: even with `show_closed` on, a high-scored closed call in profile view sorts below every open call. (The existing secondary keys — score/embed/published for ranked; `_order_for(sort)` for unranked — are retained after it.)
3. **Default filter.** The list filters are shared by the page query and the pagination total so rows and `has_next` agree. When `show_closed` is **false**, add the open predicate to that shared list-filter set:
   `or_(Call.deadline.is_(None), Call.deadline > _now)`.
   When `show_closed` is **true**, omit it (closed calls appear, but ordered last by the sort key above).
4. **Imports:** add `and_`, `or_` to the existing `from sqlalchemy import ...` line.

### B. Summary badges — count open only ([calls.py](../../../src/call_manager/web/routes/calls.py) summary block)

The summary badges describe *actionable* calls, so they exclude closed calls **regardless of the toggle**:

- **`open_count`** = `count(*)` over `base_filters AND or_(Call.deadline.is_(None), Call.deadline > _now)`. This is the "Open calls" badge — always the genuine open count, even while the user is peeking at closed rows.
- **`high_score`** (Top matches): add the same open predicate to its existing count so a *closed* high-scoring call is not advertised as a top match (it isn't actionable).
- **`soon_deadline`** (Deadline within 30 d): unchanged — already `deadline > _now AND <= +30d`.
- **Pagination `total`** (drives `has_next`): counted over the **list filters** (i.e. it tracks the toggle), so pagination matches the rows actually shown. When `show_closed` is false this equals `open_count`; when true it is the full set.

> **Decision — the "Open calls" badge tracks open-ness, not the visible row set.** When `show_closed` is on, the listed rows include closed calls but the badge still reads the open count. Rationale: a stable, honest "this many calls are still open" number is more useful than a number that silently swells when you peek at closed ones. (Rejected alternative: relabel the badge "All calls" while the toggle is on — more moving parts, less honest.)

### C. Deadline rendering ([calls_index.html](../../../src/call_manager/web/templates/calls_index.html) `deadline_label`)

Add a leading branch to the macro, using the **same** `deadline <= now` comparison as the query (not the `.days` value, to avoid same-day drift):

```jinja
{%- if deadline is none -%}
  —
{%- elif now and deadline <= now -%}
  <span class="deadline-closed">Closed · {{ deadline | display_date }}</span>
{%- else -%}
  … existing days<=14 / days<=30 / default branches …
{%- endif -%}
```

A new muted CSS class `.deadline-closed` (grey, de-emphasized — consistent with the calm-clinical design system, *not* the red `deadline-soon` / amber `deadline-watch` accents) is added to `app.css`.

### D. Toggle UI ([calls_index.html](../../../src/call_manager/web/templates/calls_index.html) filter bar)

- A checkbox added to the existing `<form method="get" action="/" class="filter-bar">`:
  `<label><input type="checkbox" name="show_closed" value="1" data-autosubmit {% if show_closed %}checked{% endif %}> Show closed calls</label>`.
  Always shown (closed calls matter in both unranked and profile views).
- **Auto-submit generalization.** The current handler binds only `select[data-autosubmit]` ([app.js:19](../../../src/call_manager/web/static/app.js#L19)). Generalize the selector to `[data-autosubmit]` so the checkbox auto-submits its form on `change` exactly like the selects. The handler already lives in `app.js` (no inline JS), so the strict `script-src 'self'` CSP is unaffected.
- **Pagination plumbing.** Add `{% if show_closed %}&show_closed=1{% endif %}` to the pagination `qs` set ([calls_index.html:126](../../../src/call_manager/web/templates/calls_index.html#L126)) so paging preserves the toggle. The checkbox itself preserves it across source/profile/sort changes (it is a field in the same GET form).

> **Decision — `show_closed` is a per-request URL param, not a persisted per-user preference.** "Let me peek at closed ones" is an exploratory action, not a standing default (unlike the sticky default profile). A URL param needs no column and no migration.

## Data flow

Researcher loads `/` → no `show_closed` → feed lists open calls only (closed filtered out), summary badges show open counts, any closed call's date would read "Closed" if shown. → Researcher ticks **Show closed calls** → form auto-submits `?show_closed=1` → feed now includes closed calls, but every one of them sorts **below** all open calls (in both the newest/deadline sort and the profile score sort); their dates render grey "Closed"; the "Open calls" / "Top matches" badges are unchanged (still open-only). → Unticks → back to the open-only default.

## Error handling / edge cases

- **No-deadline calls** are open by definition and always shown; the `closed_expr` sort key is `False` for them (they sort with the open group).
- **Same-day close:** query and template both use the timestamp compare `deadline <= now`, so a call closing earlier today is "closed" consistently in the list, the count, and the label.
- **Profile (ranked) view, `show_closed` on:** a closed call retains its score badge but sorts last; it cannot appear above an open call regardless of score.
- **Pagination:** `has_next`/`total` are computed over the list filters, so they match the toggled row set; the `show_closed` param rides the pagination links.
- **Unknown/garbage `show_closed` value:** FastAPI bool coercion accepts `1/0/true/false`; anything unparseable 422s like any other bad query param (acceptable — it is not a user-typed field, it is a checkbox).

## Non-goals (other Theme 3 slices, explicitly deferred)

Call lifecycle / `status` column / reaper; text search (`q=`); save/star (`user_call_saves`); undoable Hide + "Hidden calls" list + per-row Hide; restoring the Sort control in profile view; plain-language gate labels / hiding cosine ("engineer jargon"); share / copy-link; replacing the dev-instructions empty-state. Each is its own slice.

## Testing

Following the `tests/test_web_calls.py` pattern (TestClient + `pg_session_factory`, authenticated):

- **Default hides closed:** seed a past-deadline call, a future-deadline call, and a NULL-deadline call; `GET /` lists the future and NULL ones, not the past one.
- **Toggle reveals closed:** `GET /?show_closed=1` lists all three.
- **Closed sorts last (unranked):** with `show_closed=1` and `sort=deadline`, the past-deadline call appears **after** the open ones (guards against the `deadline ASC` pull-to-top).
- **Closed sorts last (ranked):** with a profile selected, a **high-scored closed** call appears below lower-scored open calls when `show_closed=1`, and is absent by default.
- **"Open calls" badge is open-only** regardless of `show_closed` (same count with the toggle on and off).
- **"Top matches" excludes a closed high-scorer.**
- **`deadline_label`:** a past deadline renders `deadline-closed`/"Closed", not `deadline-soon`; a NULL deadline still renders "—"; a 10-day-out deadline still renders `deadline-soon`.
- **Pagination preserves `show_closed`:** with closed calls present and a small `limit`, the Next link carries `show_closed=1`.

## Files

- Modify: `src/call_manager/web/routes/calls.py` (`show_closed` param; `closed_expr` sort key in both branches; conditional list filter; `open_count`; open predicate on `high_score`; `and_`/`or_` import; pass `show_closed` to the template).
- Modify: `src/call_manager/web/templates/calls_index.html` (`deadline_label` Closed branch; the toggle checkbox; pagination `qs`).
- Modify: `src/call_manager/web/static/app.js` (generalize `select[data-autosubmit]` → `[data-autosubmit]`).
- Modify: `src/call_manager/web/static/app.css` (add `.deadline-closed`).
- Modify: `tests/test_web_calls.py` (the cases above).

No migration.
