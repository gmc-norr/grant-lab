# Cockpit SP2b-3e — Activity feed (bottom-panel surface) — Design

**Status:** design approved 2026-06-23 · **Slice:** SP2b-3e · **Depends on:** SP2b-3b (Problems panel + bottom-panel region), SP2b-3d (uncited-claims events)

## 0. Summary

The Activity feed is the **third bottom-panel tab** of the cockpit, alongside Inbox and
Problems — the surface deferred from the SP2b-3b Problems panel (§8 of that spec) and the
last of its two follow-ons (the other, uncited-claims, shipped as SP2b-3d).

Its purpose is **collaboration awareness**: a member-facing, curated, reverse-chronological
log answering "what have my collaborators (and the assistant) done recently?". It reads the
existing `WorkspaceEvent` table — no new tables, no background job, no scheduler tick — and
renders a curated subset of event kinds into human-readable rows, collapsing bursts of
consecutive edits so a save-heavy session does not flood the feed.

It is the **architectural twin of the Problems panel**: a member-gated, polled, synchronous
read endpoint plus a new `bottom_tab`. All domain logic (which kinds matter, how each renders,
how edits collapse, where a row deep-links) lives server-side in a small pure module so it is
unit-testable without a database; the frontend only styles the actor, formats relative time,
and wires the "Go to" affordance.

**Zero schema change** (alembic head stays at `0055`). Nothing touches the `evaluator/`,
`coherence/`, `review/`, `uncited_claims/`, or drafting-adapter code.

## 1. Architecture & data flow

New backend package `src/call_manager/proposal_assist/activity/`, mirroring `problems/`:

- **`activity/feed.py`** — pure, DB-free core. `build_activity_feed(events, actor_names) ->
  list[ActivityRow]` takes already-fetched `WorkspaceEvent` rows (newest-first) plus an
  `{actor_id: email}` map, and applies, in order: the exclusion filter, consecutive-edit
  collapsing, and per-kind rendering (category + summary + target). All domain knowledge —
  the kind→verb table, the collapse rule, the target map — lives here as pure functions.
- **`web/activity.py`** — the endpoint. Performs the windowed `WorkspaceEvent` query, resolves
  actor names in one batched `User` lookup, calls `build_activity_feed`, and returns the
  `response_model`. Member-gated via `require_member`, identical in shape to `web/problems.py`.

**Data flow:** the SPA polls `GET /api/workspaces/{id}/activity` → the endpoint fetches the
most recent **100** raw events (a bounded, cheap indexed read over
`ix_workspace_events_ws_created`) → `build_activity_feed` drops system/gate kinds, collapses
consecutive same-actor edit runs, and renders each surviving group into one `ActivityRow` →
the endpoint returns `{ "items": [...] }` (≤100 rows, realistically far fewer after collapse).
No pagination and no "load older" in v1.

**No migration, no background job, no scheduler tick** — a pure read over the existing
`WorkspaceEvent` table, the same zero-schema-change profile SP2b-3b had.

**Mounting:** the router is included in `src/call_manager/main.py` next to the sibling
`problems`/`proposals` routers.

## 2. Curation & rendering rules

### 2.1 Dropped entirely (never produce a row)

- `git_drift_reconciled` — pure background/system reconciliation.
- `disclosure_acknowledged` — one-time LLM-disclosure gate ack; low signal.
- `cloud_turn_sent` — superseded by its terminal outcome event (prevents two rows per turn;
  the terminal `cloud_turn_completed`/`_failed`/`_cancelled` event carries the same
  `actor_id = submitted_by_user_id`).
- **Any kind not present in the §2.3 map** — unknown/future kinds are silently dropped
  (fail-safe: a newly introduced event type never crashes or pollutes the feed; it appears
  only once explicitly mapped here).

### 2.2 Collapse rule (edits only)

Walking the newest-first event stream, a **maximal run of consecutive `user_edit` events by
the same actor** becomes a single row:

- `count` = the number of events in the run.
- Summary (heading is `payload.get("heading")`, which may be missing or blank — the spec-init
  `user_edit` emits no heading, see `web/api/workspaces.py`):
  - exactly one distinct **non-blank** heading across the run, and no blank/missing ones →
    `edited {heading}`;
  - otherwise, if all events share one artifact → `edited the spec` / `edited the draft`
    (by artifact);
  - otherwise (blank heading on a mixed-artifact run) → `made N edits`.

  ` ×N` is appended to the first two forms when `count > 1` (the third already carries N).
- Target: derived from the run's `payload["artifact"]` — if all events share one artifact,
  `spec`-valued artifact → `top_tab="spec"`, otherwise `top_tab="draft"`; on a mixed run,
  default `top_tab="draft"`. (Artifact matching is case-insensitive: an artifact string
  containing `spec` → spec, else draft.)
- The row's `id` and `created_at` come from the run's **most-recent** event (the first one
  encountered in newest-first order).

"Consecutive" means uninterrupted in the time-ordered stream: if an event by another actor,
or any non-`user_edit` event, falls between two of Alice's edits, the run is broken and the
edits render as separate rows — chronology is always preserved. Only `user_edit` collapses;
every other kind renders one row per event (`count == 1`).

### 2.3 Render + target map

**Actor resolution.** Each event resolves to a single "actor user id" via `actor_user_id(event)`:
`event.actor_id` when set, else a per-kind `payload` fallback. The only kind needing the
fallback is **`rubric_edited`**, which leaves `actor_id` null and stores the editor in
`payload["edited_by_user_id"]` (`web/rubric_review.py`). `actor` is that id resolved to the
user's email, or `None` (frontend renders "System") when no id is available or the id is absent
from the resolved names map. **`rubric_initialized` always renders as "System"** — it carries
no user id in `actor_id` or payload (it is templated seeding from the call's extracted rubrics),
so this is intentional, not a gap. Targets are constrained to the existing `TopTab`
(`draft|spec|scorecard|review`) and `BottomTab` (`inbox|problems|activity`) values — no new
navigation plumbing is introduced.

| kind(s) | category | summary | target |
|---|---|---|---|
| `created` | `created` | created the workspace | — |
| `user_edit` (collapsed) | `edit` | edited {heading} ×N / made N edits | `top_tab` = draft or spec (per artifact) |
| `user_citation_insert` | `citation` | added a citation | `top_tab` = draft |
| `cloud_turn_completed` | `turn` | ran the assistant | — |
| `cloud_turn_failed` | `turn` | had an assistant turn fail | — |
| `cloud_turn_cancelled` | `turn` | cancelled an assistant turn | — |
| `review_run_triggered` | `review` | started a panel review | `top_tab` = review |
| `uncited_claims_run_triggered` | `uncited` | started an uncited-claims check | `bottom_tab` = problems |
| `uncited_claim_dismissed` | `dismissal` | dismissed an uncited claim | `bottom_tab` = problems |
| `user_claim_dismissed` | `dismissal` | dismissed a flagged claim | — |
| `reference.added` | `reference` | added a reference | — |
| `reference.metadata_approved` | `reference` | approved reference metadata | — |
| `reference_metadata_waived` | `reference` | waived reference metadata | — |
| `reference_metadata_waiver_cleared` | `reference` | cleared a reference metadata waiver | — |
| `reference.kind_changed` | `reference` | changed a reference type | — |
| `reference.reprocessed` | `reference` | reprocessed a reference | — |
| `reference.tombstoned` | `reference` | removed a reference | — |
| `reference.deleted` | `reference` | deleted a reference | — |
| `rubric_initialized` | `rubric` | set up the funder criteria | `top_tab` = spec |
| `rubric_edited` | `rubric` | edited the funder criteria | `top_tab` = spec |

**Reference events carry no deep-link in v1** — the References surface is a left-rail item,
not addressable by the current `NavTarget {top_tab, bottom_tab}`; adding it would require new
plumbing, which is out of scope (§5). The feed renders as a **flat reverse-chronological
list** (unlike Problems, which groups by category).

## 3. API contract

**`GET /api/workspaces/{workspace_id}/activity`** — member-gated (`require_member`),
`response_model=ActivityFeedOut`, no payload beyond the path parameter. Router prefix
`/api/workspaces`, `tags=["activity"]`.

**Endpoint body:**
1. `require_member(workspace_id=…, user=…, session=…)`.
2. Fetch the most recent 100 events:
   `select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == workspace_id)
   .order_by(WorkspaceEvent.created_at.desc(), WorkspaceEvent.id.desc()).limit(100)` (uses
   `ix_workspace_events_ws_created`). The `id desc` tie-breaker makes ordering — and therefore
   edit-collapse boundaries — deterministic when `created_at` ties (it is a DB default, and
   same-transaction inserts can share a timestamp). `id` is a random UUID, so the tie-break is
   deterministic, not semantically chronological — acceptable for stable rendering.
3. Resolve actor names in one batched query over the distinct actor user ids — i.e.
   `{actor_user_id(e) for e in events}` minus `None`, which folds in the `rubric_edited`
   `payload["edited_by_user_id"]` fallback as well as plain `actor_id`s:
   `select(User.id, User.email).where(User.id.in_(actor_ids))` → `{id: email}` (no N+1).
4. `rows = build_activity_feed(events, actor_names)`.
5. `return {"items": [row.as_dict() for row in rows]}`.

**Response models** (Pydantic; drive openapi-typescript codegen):

```python
class ActivityTargetOut(BaseModel):
    top_tab: Literal["draft", "spec", "scorecard", "review"] | None
    bottom_tab: Literal["inbox", "problems", "activity"] | None

class ActivityRowOut(BaseModel):
    id: str                  # stable React key = the row's most-recent underlying event id
    category: Literal["created", "edit", "citation", "turn", "review",
                      "uncited", "dismissal", "reference", "rubric"]
    actor: str | None        # email; None → frontend renders "System"
    summary: str             # backend-rendered verb + object ("edited Methods ×3")
    count: int               # >= 1 (collapsed edit runs > 1)
    created_at: str          # ISO-8601 of the row's most-recent event
    target: ActivityTargetOut | None

class ActivityFeedOut(BaseModel):
    items: list[ActivityRowOut]
```

The object envelope (`{items: [...]}`) — rather than a bare list — leaves room for feed-level
metadata (e.g. a future unread count) without breaking the client. `build_activity_feed`
returns small frozen `ActivityRow` dataclasses (pure, testable); the endpoint serializes them.

## 4. Frontend

- **`frontend/src/cockpit/nav.ts`** — extend one union:
  `BottomTab = "inbox" | "problems" | "activity"`. The two-axis nav and the expand-on-navigate
  logic in `Cockpit.tsx` are already generic over `bottom_tab`; no other nav change is needed.
- **`frontend/src/api/activity.ts`** — `useActivity(id)` mirroring `useProblems`:
  `queryKey: ["activity", id]`, `refetchInterval: 20000` (lower urgency than Problems' 15s),
  `unwrap(api.GET("/api/workspaces/{workspace_id}/activity", …))`. Types come from the
  generated `components["schemas"]["ActivityFeedOut"]` / `["ActivityRowOut"]` (regenerate with
  `EMBEDDING_MODEL=bge-m3 pnpm gen:api`). A local `toNav(target)` maps `ActivityTarget →
  NavTarget` (null → undefined), as in `ProblemsPanel`.
- **`frontend/src/cockpit/ActivityPanel.tsx`** — a flat reverse-chronological list (not
  grouped). Loading / error / empty states mirror `ProblemsPanel` ("Loading…", "Could not load
  activity.", "No activity yet."). Each row renders **{actor ?? "System"}** · summary · relative
  time, with the actor emphasized and `created_at` formatted relative ("2h ago", "just now")
  via a small `formatRelativeTime` helper. A "Go to" button appears only when `target` is
  non-null and calls `onNavigate(toNav(target))`. `count` is already baked into `summary`, so
  there is no separate count UI.
- **`frontend/src/cockpit/BottomPanel.tsx`** — add a third tab button (`role="tab"`,
  `aria-selected={activeTab === "activity"}`, label "Activity", **no count badge**) and a third
  body branch; the current two-way ternary becomes an explicit switch over the three tabs.

**No badge / unread count on the Activity tab in v1** — an "unread" count needs per-user
last-seen storage (a schema + write path), scope creep against the zero-migration profile. The
tab label is simply "Activity".

## 5. Out of scope / deferred (v1)

- **Unread/seen badge** on the Activity tab (needs per-user last-seen storage).
- **Pagination / "load older"** — v1 is the latest 100 events, collapsed.
- **Live SSE updates** — v1 polls at 20s.
- **Rich per-object deep-links** (a turn → that turn, a citation → that marker) and
  **References-rail navigation** — both need new `NavTarget` plumbing.
- **Reference titles** in summaries (would need a `Reference` join) — generic "a reference".
- **Filtering** by actor/kind/date.
- **Generalizing a shared event read-model** with the admin audit view (`web/routes/admin.py`)
  — the two audiences want opposite curation (admin = full fidelity, member = collapsed); keep
  Activity self-contained per the project's copy-not-generalize norm.
- **`review_run_triggered` duplicate-event race** (the SP2b-3d parity note): concurrent review
  POSTs could double-write that event, which the feed would render as two identical "started a
  panel review" rows. Fixing it touches the do-not-touch `review/` package — left out; revisit
  only if observed in practice.

## 6. Testing

**Pure `feed.py` unit tests (no DB) — the bulk of coverage:**
- **Exclusions:** `git_drift_reconciled`, `disclosure_acknowledged`, `cloud_turn_sent`, and any
  unmapped/unknown kind each produce zero rows.
- **Collapse:** three consecutive same-actor same-`heading` edits → one row `edited Methods ×3`
  with `count == 3`; mixed headings → `made 3 edits`; a turn or other-actor event between two
  edits → two separate edit rows (chronology preserved); an actor boundary splits a run.
- **Missing/blank heading:** a lone spec-init `user_edit` (payload `{artifact: "spec"}`, no
  `heading`) → `edited the spec` (not `edited None`); a run mixing a headed draft edit and a
  blank-heading draft edit → `edited the draft ×2`; a run mixing blank headings across draft
  and spec artifacts → `made N edits`.
- **Render/target table:** one assertion per category — e.g. `review_run_triggered` →
  `summary == "started a panel review"` and `target.top_tab == "review"`;
  `uncited_claim_dismissed` → `target.bottom_tab == "problems"`; `reference.added` →
  `target is None`; `user_edit` with a spec vs draft artifact → correct `top_tab`.
- **Actor:** `actor_id` resolves to email; null `actor_id` → `actor is None`; an id absent from
  the names map → `None` (defensive). **`rubric_edited`** (no `actor_id`) resolves via
  `payload["edited_by_user_id"]`; **`rubric_initialized`** → `actor is None` ("System"). Output
  is newest-first; a collapsed row's `id`/`created_at` come from its most-recent event.

**Endpoint tests (testcontainer Postgres):** non-member → 403; missing workspace → 404; happy
path seeds a few `WorkspaceEvent`s and asserts ordered `items` and expected summaries; the
actor join resolves email vs `None` and folds in the `rubric_edited`
`payload["edited_by_user_id"]` fallback; two events sharing one `created_at` come back in
`id desc` order deterministically; the `limit 100` window is honored. (Run these files in
isolation — full `uv run pytest` is testcontainer-flaky.)

**Frontend (vitest):** `ActivityPanel` renders actor/summary/relative-time, shows "Go to" only
when `target` is present and calls `onNavigate`; empty/loading/error states; `BottomPanel`
exposes the third tab and switches to it. Gate = `pnpm exec tsc --noEmit` + `pnpm vitest run`.
