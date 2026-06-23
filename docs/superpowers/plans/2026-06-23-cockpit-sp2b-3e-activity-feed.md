# Cockpit SP2b-3e — Activity feed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a member-facing, curated, reverse-chronological **Activity feed** as the third bottom-panel tab of the cockpit, reading the existing `WorkspaceEvent` table.

**Architecture:** A pure, DB-free render/collapse core (`proposal_assist/activity/feed.py`) holds all domain logic (which kinds matter, how each renders, edit-collapsing, deep-link targets); a thin member-gated polled read endpoint (`web/activity.py`) does the windowed query + actor-name resolution and serializes the core's rows; the frontend adds an `ActivityPanel` as a new `bottom_tab`. No migration, no background job, no scheduler tick — a pure read over an existing table, the SP2b-3b shape.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (no Alembic change) / pytest (+ testcontainers Postgres); React 18 + TanStack Query + openapi-fetch / Vitest.

**Spec:** `docs/superpowers/specs/2026-06-23-cockpit-sp2b-3e-activity-feed-design.md` (read it; this plan implements it verbatim).

## Global Constraints

- **Worktree:** all work happens in `.worktrees/sp2b-3e-activity-feed` on branch `feat/sp2b-3e-activity-feed`. Subagents MUST run git as `git -C <worktree>` and verify `git rev-parse --abbrev-ref HEAD == feat/sp2b-3e-activity-feed` before committing; run pytest/pnpm from the worktree root.
- **Zero schema change:** no Alembic migration, no model column add. Alembic head stays `0055`. No new table, no background job, no scheduler tick.
- **Do NOT touch** `evaluator/`, `coherence/`, `review/`, `uncited_claims/`, or `drafting/` packages. Copy patterns from them; never edit them.
- **Targets** are constrained to the existing `TopTab` (`draft|spec|scorecard|review`) and `BottomTab` (`inbox|problems|activity`) values. No new navigation plumbing. Reference events carry **no** deep-link.
- **Backend test gate:** run only the affected test files (full `uv run pytest` is testcontainer-flaky); capture exit codes; NEVER pipe pytest through `tail`/`head`. Re-run any connection-refused failure in isolation before reporting it as a failure.
- **Frontend gate:** `pnpm exec tsc --noEmit` (there is no TS linter — tsc is the gate) **and** `pnpm vitest run`. Run `pnpm install` after pulling if `node_modules` is absent. Node 22 + pnpm 10.16 must be on PATH.
- **OpenAPI client regen:** `EMBEDDING_MODEL=bge-m3 pnpm gen:api` (run from `frontend/`). The default `.env` embedding model logs a stdout WARNING that corrupts `schema.gen.ts`; the explicit bge-m3 avoids it. Regen runs against the local app, offline.
- **Commits:** `git -c commit.gpgsign=false commit`. Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

## File Structure

**Backend (new package `src/call_manager/proposal_assist/activity/`):**
- `activity/__init__.py` — exports `build_activity_feed`, `actor_user_id`, `ActivityRow`, `ActivityTarget`.
- `activity/feed.py` — pure core: dataclasses, exclusion set, render/target map, edit-collapse, `build_activity_feed`. No DB, no I/O.
- `web/activity.py` — endpoint + Pydantic response models; windowed query + actor-name batch lookup.
- `src/call_manager/main.py` (modify) — import + `include_router(activity_router)`.

**Backend tests:**
- `tests/proposal_assist/activity/__init__.py` (empty; test subdirs are packages).
- `tests/proposal_assist/activity/test_feed.py` — pure-core unit tests (no DB).
- `tests/proposal_assist/activity/test_web.py` — endpoint tests (testcontainer Postgres).

**Frontend:**
- `frontend/src/cockpit/nav.ts` (modify) — extend `BottomTab` union.
- `frontend/src/api/schema.gen.ts` (regenerate) — picks up `ActivityFeedOut`/`ActivityRowOut`/`ActivityTargetOut`.
- `frontend/src/api/activity.ts` (new) — `useActivity` hook + generated types.
- `frontend/src/cockpit/ActivityPanel.tsx` (new) — the panel + local `toNav` + `formatRelativeTime`.
- `frontend/src/cockpit/ActivityPanel.test.tsx` (new).
- `frontend/src/cockpit/BottomPanel.tsx` (modify) — third tab button + body branch.
- `frontend/src/cockpit/BottomPanel.test.tsx` (modify) — assert the third tab.

**Task map:** Task 1 = pure core (no DB). Task 2 = endpoint + main.py wiring. Task 3 = frontend types + hook + nav + ActivityPanel. Task 4 = BottomPanel wiring.

---

### Task 1: Pure activity-feed core

**Files:**
- Create: `src/call_manager/proposal_assist/activity/__init__.py`
- Create: `src/call_manager/proposal_assist/activity/feed.py`
- Create: `tests/proposal_assist/activity/__init__.py`
- Test: `tests/proposal_assist/activity/test_feed.py`

**Interfaces:**
- Consumes: `call_manager.db.models.WorkspaceEvent` (read-only — `.id`, `.kind`, `.payload`, `.actor_id`, `.created_at`).
- Produces:
  - `ActivityTarget(top_tab: str|None=None, bottom_tab: str|None=None)` frozen dataclass with `.as_dict() -> {"top_tab","bottom_tab"}`.
  - `ActivityRow(id, category, actor, summary, count, created_at, target)` frozen dataclass with `.as_dict()`.
  - `actor_user_id(event: WorkspaceEvent) -> int | None` — `actor_id`, else `payload["edited_by_user_id"]` for `rubric_edited`, else `None`.
  - `build_activity_feed(events: list[WorkspaceEvent], actor_names: dict[int, str]) -> list[ActivityRow]` — `events` newest-first; `actor_names` maps user id → email.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/activity/__init__.py` (empty file) and `tests/proposal_assist/activity/test_feed.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from call_manager.db.models import WorkspaceEvent
from call_manager.proposal_assist.activity.feed import (
    actor_user_id,
    build_activity_feed,
)

_BASE = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)


def _ev(kind, *, actor_id=None, payload=None, secs_ago=0, ev_id=None):
    """A WorkspaceEvent built in-memory (no session). created_at is set
    explicitly so the helper does not depend on DB defaults. Newer events
    have a smaller secs_ago, since callers pass events newest-first."""
    return WorkspaceEvent(
        id=ev_id or uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        payload=payload or {},
        actor_id=actor_id,
        created_at=_BASE - timedelta(seconds=secs_ago),
    )


def test_excluded_and_unknown_kinds_produce_no_rows():
    events = [
        _ev("git_drift_reconciled"),
        _ev("disclosure_acknowledged", actor_id=1),
        _ev("cloud_turn_sent", actor_id=1),
        _ev("some_future_kind", actor_id=1),
    ]
    assert build_activity_feed(events, {1: "a@ki.se"}) == []


def test_collapse_same_actor_same_heading_edits():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=0),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=10),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=20),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert len(rows) == 1
    r = rows[0].as_dict()
    assert r["category"] == "edit"
    assert r["summary"] == "edited Methods ×3"
    assert r["count"] == 3
    assert r["actor"] == "a@ki.se"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None}


def test_collapse_mixed_headings_makes_n_edits():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=1, payload={"heading": "Aims", "artifact": "spec"}),
        _ev("user_edit", actor_id=1, payload={"heading": "Budget", "artifact": "draft"}),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert len(rows) == 1
    assert rows[0].summary == "made 3 edits"
    # mixed artifacts default to draft
    assert rows[0].target.top_tab == "draft"


def test_spec_init_edit_without_heading():
    events = [_ev("user_edit", actor_id=1, payload={"artifact": "spec"})]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert rows[0].summary == "edited the spec"
    assert rows[0].count == 1
    assert rows[0].target.top_tab == "spec"


def test_blank_heading_run_uses_artifact_fallback():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=1, payload={"artifact": "draft"}),  # blank heading
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert rows[0].summary == "edited the draft ×2"
    assert rows[0].target.top_tab == "draft"


def test_run_broken_by_interleaving_preserves_chronology():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=0),
        _ev("cloud_turn_completed", actor_id=1, secs_ago=10),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=20),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert [r.category for r in rows] == ["edit", "turn", "edit"]
    assert all(r.count == 1 for r in rows)


def test_actor_boundary_splits_run():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=2, payload={"heading": "Methods", "artifact": "draft"}),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se", 2: "b@ki.se"})
    assert [r.actor for r in rows] == ["a@ki.se", "b@ki.se"]
    assert len(rows) == 2


def test_render_and_target_table():
    cases = {
        "created": ("created the workspace", None),
        "user_citation_insert": ("added a citation", {"top_tab": "draft", "bottom_tab": None}),
        "cloud_turn_completed": ("ran the assistant", None),
        "cloud_turn_failed": ("had an assistant turn fail", None),
        "cloud_turn_cancelled": ("cancelled an assistant turn", None),
        "review_run_triggered": ("started a panel review", {"top_tab": "review", "bottom_tab": None}),
        "uncited_claims_run_triggered": ("started an uncited-claims check", {"top_tab": None, "bottom_tab": "problems"}),
        "uncited_claim_dismissed": ("dismissed an uncited claim", {"top_tab": None, "bottom_tab": "problems"}),
        "user_claim_dismissed": ("dismissed a flagged claim", None),
        "reference.added": ("added a reference", None),
        "reference.metadata_approved": ("approved reference metadata", None),
        "reference_metadata_waived": ("waived reference metadata", None),
        "reference_metadata_waiver_cleared": ("cleared a reference metadata waiver", None),
        "reference.kind_changed": ("changed a reference type", None),
        "reference.reprocessed": ("reprocessed a reference", None),
        "reference.tombstoned": ("removed a reference", None),
        "reference.deleted": ("deleted a reference", None),
        "rubric_initialized": ("set up the funder criteria", {"top_tab": "spec", "bottom_tab": None}),
    }
    for kind, (summary, target) in cases.items():
        rows = build_activity_feed([_ev(kind, actor_id=1)], {1: "a@ki.se"})
        assert len(rows) == 1, kind
        d = rows[0].as_dict()
        assert d["summary"] == summary, kind
        assert d["target"] == target, kind
        assert d["count"] == 1, kind


def test_actor_resolution_variants():
    # rubric_edited has no actor_id; user is in payload.edited_by_user_id
    rubric = _ev("rubric_edited", payload={"edited_by_user_id": 7})
    assert actor_user_id(rubric) == 7
    assert build_activity_feed([rubric], {7: "ed@ki.se"})[0].actor == "ed@ki.se"
    # rubric_initialized carries no user id anywhere -> System (None)
    init = _ev("rubric_initialized")
    assert actor_user_id(init) is None
    assert build_activity_feed([init], {})[0].actor is None
    # actor_id present but absent from names map -> None (defensive)
    assert build_activity_feed([_ev("created", actor_id=99)], {})[0].actor is None
    # null actor_id -> None
    assert build_activity_feed([_ev("created")], {})[0].actor is None


def test_collapsed_row_id_and_time_come_from_most_recent():
    newest = uuid.uuid4()
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "M", "artifact": "draft"}, secs_ago=0, ev_id=newest),
        _ev("user_edit", actor_id=1, payload={"heading": "M", "artifact": "draft"}, secs_ago=30),
    ]
    row = build_activity_feed(events, {1: "a@ki.se"})[0]
    assert row.id == str(newest)
    assert row.created_at == (_BASE).isoformat()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/activity/test_feed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.activity'`.

- [ ] **Step 3: Create the package `__init__.py`**

Create `src/call_manager/proposal_assist/activity/__init__.py`:

```python
"""SP2b-3e Activity feed: curated, read-only render of recent WorkspaceEvents."""

from call_manager.proposal_assist.activity.feed import (
    ActivityRow,
    ActivityTarget,
    actor_user_id,
    build_activity_feed,
)

__all__ = ["ActivityRow", "ActivityTarget", "actor_user_id", "build_activity_feed"]
```

- [ ] **Step 4: Implement the pure core**

Create `src/call_manager/proposal_assist/activity/feed.py`:

```python
"""Pure, DB-free core for the Activity feed (SP2b-3e).

`build_activity_feed` turns a newest-first list of WorkspaceEvent rows into
curated ActivityRows: it drops system/gate/superseded kinds, collapses
consecutive same-actor edit runs, and renders each surviving group with a
human-readable summary and an optional cheap deep-link target. All domain
knowledge lives here; the web layer only queries + resolves actor names.
"""

from __future__ import annotations

from dataclasses import dataclass

from call_manager.db.models import WorkspaceEvent

# Kinds that never produce a row: pure system reconciliation, the one-time LLM
# disclosure gate ack, and the pending half of a turn (superseded by its
# terminal cloud_turn_{completed,failed,cancelled} event, which carries the
# same actor_id).
_DROP: frozenset[str] = frozenset(
    {"git_drift_reconciled", "disclosure_acknowledged", "cloud_turn_sent"}
)


@dataclass(frozen=True)
class ActivityTarget:
    top_tab: str | None = None
    bottom_tab: str | None = None

    def as_dict(self) -> dict:
        return {"top_tab": self.top_tab, "bottom_tab": self.bottom_tab}


@dataclass(frozen=True)
class ActivityRow:
    id: str
    category: str
    actor: str | None
    summary: str
    count: int
    created_at: str
    target: ActivityTarget | None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "actor": self.actor,
            "summary": self.summary,
            "count": self.count,
            "created_at": self.created_at,
            "target": self.target.as_dict() if self.target else None,
        }


# kind -> (category, summary, target). user_edit is handled separately (collapse).
# Targets reuse only existing TopTab/BottomTab values; reference.* get no target.
_DRAFT = ActivityTarget(top_tab="draft")
_SPEC = ActivityTarget(top_tab="spec")
_REVIEW = ActivityTarget(top_tab="review")
_PROBLEMS = ActivityTarget(bottom_tab="problems")

_RENDER: dict[str, tuple[str, str, ActivityTarget | None]] = {
    "created": ("created", "created the workspace", None),
    "user_citation_insert": ("citation", "added a citation", _DRAFT),
    "cloud_turn_completed": ("turn", "ran the assistant", None),
    "cloud_turn_failed": ("turn", "had an assistant turn fail", None),
    "cloud_turn_cancelled": ("turn", "cancelled an assistant turn", None),
    "review_run_triggered": ("review", "started a panel review", _REVIEW),
    "uncited_claims_run_triggered": ("uncited", "started an uncited-claims check", _PROBLEMS),
    "uncited_claim_dismissed": ("dismissal", "dismissed an uncited claim", _PROBLEMS),
    "user_claim_dismissed": ("dismissal", "dismissed a flagged claim", None),
    "reference.added": ("reference", "added a reference", None),
    "reference.metadata_approved": ("reference", "approved reference metadata", None),
    "reference_metadata_waived": ("reference", "waived reference metadata", None),
    "reference_metadata_waiver_cleared": ("reference", "cleared a reference metadata waiver", None),
    "reference.kind_changed": ("reference", "changed a reference type", None),
    "reference.reprocessed": ("reference", "reprocessed a reference", None),
    "reference.tombstoned": ("reference", "removed a reference", None),
    "reference.deleted": ("reference", "deleted a reference", None),
    "rubric_initialized": ("rubric", "set up the funder criteria", _SPEC),
    "rubric_edited": ("rubric", "edited the funder criteria", _SPEC),
}


def actor_user_id(event: WorkspaceEvent) -> int | None:
    """The user id to attribute an event to: actor_id when set, else a per-kind
    payload fallback. Only rubric_edited needs the fallback (it leaves actor_id
    null and stores the editor in payload['edited_by_user_id'])."""
    if event.actor_id is not None:
        return event.actor_id
    if event.kind == "rubric_edited":
        uid = (event.payload or {}).get("edited_by_user_id")
        if isinstance(uid, int):
            return uid
    return None


def _resolve_actor(event: WorkspaceEvent, actor_names: dict[int, str]) -> str | None:
    uid = actor_user_id(event)
    return actor_names.get(uid) if uid is not None else None


def _artifact_tab(artifact: object) -> str:
    return "spec" if "spec" in str(artifact or "").lower() else "draft"


def _edit_row(run: list[WorkspaceEvent], actor_names: dict[int, str]) -> ActivityRow:
    """Render a maximal consecutive same-actor user_edit run (newest-first) as
    one row. Summary: a single distinct non-blank heading -> 'edited {heading}';
    else single artifact -> 'edited the {spec|draft}'; else 'made N edits'. '×N'
    is appended to the first two forms when N > 1."""
    count = len(run)
    most_recent = run[0]
    headings = [str((ev.payload or {}).get("heading") or "").strip() for ev in run]
    artifacts = {_artifact_tab((ev.payload or {}).get("artifact")) for ev in run}
    distinct = {h for h in headings if h}
    has_blank = any(not h for h in headings)

    if len(distinct) == 1 and not has_blank:
        base = f"edited {next(iter(distinct))}"
    elif len(artifacts) == 1:
        base = f"edited the {next(iter(artifacts))}"
    else:
        base = f"made {count} edits"

    summary = base if (count == 1 or base.startswith("made ")) else f"{base} ×{count}"
    top_tab = next(iter(artifacts)) if len(artifacts) == 1 else "draft"
    return ActivityRow(
        id=str(most_recent.id),
        category="edit",
        actor=_resolve_actor(most_recent, actor_names),
        summary=summary,
        count=count,
        created_at=most_recent.created_at.isoformat(),
        target=ActivityTarget(top_tab=top_tab),
    )


def build_activity_feed(
    events: list[WorkspaceEvent], actor_names: dict[int, str]
) -> list[ActivityRow]:
    """Curate a newest-first WorkspaceEvent list into ActivityRows. Drops
    excluded/unmapped kinds, collapses consecutive same-actor edit runs, and
    renders every other kind 1:1 via the _RENDER table. Order is preserved."""
    rows: list[ActivityRow] = []
    i = 0
    n = len(events)
    while i < n:
        e = events[i]
        if e.kind in _DROP:
            i += 1
            continue
        if e.kind == "user_edit":
            actor = actor_user_id(e)
            j = i
            run: list[WorkspaceEvent] = []
            while (
                j < n
                and events[j].kind == "user_edit"
                and actor_user_id(events[j]) == actor
            ):
                run.append(events[j])
                j += 1
            rows.append(_edit_row(run, actor_names))
            i = j
            continue
        spec = _RENDER.get(e.kind)
        if spec is None:
            i += 1  # unknown/unmapped kind -> drop (fail-safe)
            continue
        category, summary, target = spec
        rows.append(
            ActivityRow(
                id=str(e.id),
                category=category,
                actor=_resolve_actor(e, actor_names),
                summary=summary,
                count=1,
                created_at=e.created_at.isoformat(),
                target=target,
            )
        )
        i += 1
    return rows
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/activity/test_feed.py -v`
Expected: PASS — 10 passed.

- [ ] **Step 6: Commit**

```bash
git -C .worktrees/sp2b-3e-activity-feed add \
  src/call_manager/proposal_assist/activity/__init__.py \
  src/call_manager/proposal_assist/activity/feed.py \
  tests/proposal_assist/activity/__init__.py \
  tests/proposal_assist/activity/test_feed.py
git -C .worktrees/sp2b-3e-activity-feed -c commit.gpgsign=false commit -m "feat(activity): pure activity-feed core (curation, collapse, render)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Activity feed endpoint

**Files:**
- Create: `src/call_manager/proposal_assist/web/activity.py`
- Modify: `src/call_manager/main.py` (import near line 46–65; `include_router` after line 557)
- Test: `tests/proposal_assist/activity/test_web.py`

**Interfaces:**
- Consumes: `build_activity_feed`, `actor_user_id` from `call_manager.proposal_assist.activity`; `require_member` from `web._perms`; `_session_dep` from `web.workspaces`; `WorkspaceEvent`, `User` from `db.models`.
- Produces: `GET /api/workspaces/{workspace_id}/activity` → `ActivityFeedOut = {"items": list[ActivityRowOut]}`; router object `router` (exported as `activity_router` in main).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/activity/test_web.py`:

```python
import uuid
from datetime import UTC, datetime

from call_manager.db.models import User, WorkspaceEvent
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        return ws.id


def test_activity_empty_shape(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_activity_happy_path_orders_and_summarizes(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    # Explicit, distinct created_at values: events added in one transaction share
    # func.now(), so we cannot rely on insertion order — set the timestamps so
    # newest-first is deterministic regardless of the random UUID tie-breaker.
    t0 = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 23, 12, 5, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="created", actor_id=current_user.id, created_at=t0))
        s.add(
            WorkspaceEvent(
                workspace_id=ws_id,
                kind="reference.added",
                actor_id=current_user.id,
                payload={"reference_id": "x"},
                created_at=t1,
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    summaries = [it["summary"] for it in body["items"]]
    # newest-first: reference.added (t1) precedes created (t0)
    assert summaries == ["added a reference", "created the workspace"]
    assert all(it["actor"] == current_user.email for it in body["items"])


def test_activity_resolves_rubric_edited_via_payload(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        editor = User(status="active", email="editor@ki.se")
        s.add(editor)
        s.flush()
        s.add(
            WorkspaceEvent(
                workspace_id=ws_id,
                kind="rubric_edited",
                actor_id=None,
                payload={"edited_by_user_id": editor.id},
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    assert body["items"][0]["summary"] == "edited the funder criteria"
    assert body["items"][0]["actor"] == "editor@ki.se"


def test_activity_tiebreak_is_deterministic(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    same = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    lo = uuid.UUID(int=1)
    hi = uuid.UUID(int=2)
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(id=lo, workspace_id=ws_id, kind="created", actor_id=current_user.id, created_at=same))
        s.add(WorkspaceEvent(id=hi, workspace_id=ws_id, kind="reference.added", actor_id=current_user.id, created_at=same))
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    # equal created_at -> id DESC, so the larger uuid (hi) comes first
    assert body["items"][0]["id"] == str(hi)


def test_activity_unknown_workspace_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{uuid.uuid4()}/activity")
    assert r.status_code == 404


def test_activity_unauth_rejected(client_unauth, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/activity")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/activity/test_web.py -v`
Expected: FAIL — `404` on every route (the router is not mounted yet) / `ModuleNotFoundError` on `web.activity`.

- [ ] **Step 3: Implement the endpoint**

Create `src/call_manager/proposal_assist/web/activity.py`:

```python
"""Member-gated read-only Activity feed (SP2b-3e).

GET returns a curated, reverse-chronological log of recent WorkspaceEvents —
collapsed, actor-resolved, with cheap deep-links. No mutation, no LLM, no new
table. Mirrors the Problems panel's read-only/polled/member-gated shape.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceEvent
from call_manager.proposal_assist.activity.feed import (
    actor_user_id,
    build_activity_feed,
)
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["activity"])

_ACTIVITY_WINDOW = 100


class ActivityTargetOut(BaseModel):
    top_tab: Literal["draft", "spec", "scorecard", "review"] | None
    bottom_tab: Literal["inbox", "problems", "activity"] | None


class ActivityRowOut(BaseModel):
    id: str
    category: Literal[
        "created", "edit", "citation", "turn", "review",
        "uncited", "dismissal", "reference", "rubric",
    ]
    actor: str | None
    summary: str
    count: int
    created_at: str
    target: ActivityTargetOut | None


class ActivityFeedOut(BaseModel):
    items: list[ActivityRowOut]


@router.get("/{workspace_id}/activity", response_model=ActivityFeedOut)
def get_activity(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    events = list(
        session.scalars(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id)
            .order_by(WorkspaceEvent.created_at.desc(), WorkspaceEvent.id.desc())
            .limit(_ACTIVITY_WINDOW)
        )
    )
    actor_ids = {uid for uid in (actor_user_id(e) for e in events) if uid is not None}
    actor_names: dict[int, str] = {}
    if actor_ids:
        actor_names = {
            uid: email
            for uid, email in session.execute(
                select(User.id, User.email).where(User.id.in_(actor_ids))
            )
        }
    rows = build_activity_feed(events, actor_names)
    return {"items": [r.as_dict() for r in rows]}
```

- [ ] **Step 4: Mount the router in `main.py`**

In `src/call_manager/main.py`, add the import alongside the other proposal_assist web routers (near the `problems`/`uncited_claims` imports around lines 46–65):

```python
from call_manager.proposal_assist.web.activity import (
    router as activity_router,
)
```

And register it immediately after the `uncited_claims_router` include (after line 557):

```python
    app.include_router(uncited_claims_router)
    app.include_router(activity_router)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/activity/test_web.py -v`
Expected: PASS — 6 passed. (If any test reports `connection refused`, re-run that file alone before treating it as a real failure — the testcontainer Postgres is flaky under load.)

- [ ] **Step 6: Commit**

```bash
git -C .worktrees/sp2b-3e-activity-feed add \
  src/call_manager/proposal_assist/web/activity.py \
  src/call_manager/main.py \
  tests/proposal_assist/activity/test_web.py
git -C .worktrees/sp2b-3e-activity-feed -c commit.gpgsign=false commit -m "feat(activity): member-gated GET /activity endpoint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Frontend types, hook, nav, and ActivityPanel

**Files:**
- Modify: `frontend/src/cockpit/nav.ts`
- Regenerate: `frontend/src/api/schema.gen.ts`
- Create: `frontend/src/api/activity.ts`
- Create: `frontend/src/cockpit/ActivityPanel.tsx`
- Test: `frontend/src/cockpit/ActivityPanel.test.tsx`

**Interfaces:**
- Consumes: generated `components["schemas"]["ActivityFeedOut"|"ActivityRowOut"]` (from Task 2's endpoint); `api`, `unwrap` from `api/client`; `NavTarget`, `OnNavigate`, `TopTab`, `BottomTab` from `cockpit/nav`.
- Produces: `useActivity(id) -> UseQueryResult<ActivityFeed>`, types `ActivityRow`/`ActivityFeed`; component `ActivityPanel({ id, onNavigate })`.

- [ ] **Step 1: Extend the BottomTab union**

In `frontend/src/cockpit/nav.ts`, change line 2:

```typescript
export type BottomTab = "inbox" | "problems" | "activity";
```

(Leave `TopTab`, `NavTarget`, `OnNavigate` unchanged.)

- [ ] **Step 2: Regenerate the OpenAPI client**

From `frontend/`:

Run: `EMBEDDING_MODEL=bge-m3 pnpm gen:api`
Expected: `src/api/schema.gen.ts` now contains `ActivityFeedOut`, `ActivityRowOut`, and `ActivityTargetOut` under `components["schemas"]`, plus the `/api/workspaces/{workspace_id}/activity` path. Verify:

Run: `grep -c "ActivityRowOut" src/api/schema.gen.ts`
Expected: ≥ 1.

- [ ] **Step 3: Write the failing ActivityPanel test**

Create `frontend/src/cockpit/ActivityPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActivityPanel } from "./ActivityPanel";

const h = vi.hoisted((): { activity: { data: unknown; isError?: boolean; isLoading?: boolean } } => ({
  activity: { data: undefined },
}));

vi.mock("../api/activity", () => ({ useActivity: () => h.activity }));

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  h.activity = { data: undefined };
});

describe("ActivityPanel", () => {
  it("shows an empty state when there is no activity", () => {
    h.activity = { data: { items: [] } };
    wrap(<ActivityPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/no activity yet/i)).toBeInTheDocument();
  });

  it("renders rows with actor, summary, relative time and routes Go-to", () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    h.activity = {
      data: {
        items: [
          {
            id: "e1",
            category: "review",
            actor: "alice@ki.se",
            summary: "started a panel review",
            count: 1,
            created_at: twoHoursAgo,
            target: { top_tab: "review", bottom_tab: null },
          },
          {
            id: "e2",
            category: "created",
            actor: null,
            summary: "created the workspace",
            count: 1,
            created_at: twoHoursAgo,
            target: null,
          },
        ],
      },
    };
    const onNavigate = vi.fn();
    wrap(<ActivityPanel id="ws1" onNavigate={onNavigate} />);
    expect(screen.getByText("alice@ki.se")).toBeInTheDocument();
    expect(screen.getByText("started a panel review")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument(); // null actor
    expect(screen.getAllByText(/2h ago/).length).toBeGreaterThan(0);
    // Only the row with a target has a Go-to button.
    const gotos = screen.getAllByRole("button", { name: /go to/i });
    expect(gotos).toHaveLength(1);
    fireEvent.click(gotos[0]);
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "review", bottom_tab: undefined });
  });

  it("surfaces a fetch error inline", () => {
    h.activity = { data: undefined, isError: true };
    wrap(<ActivityPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/could not load activity/i);
  });
});
```

- [ ] **Step 4: Run the test to verify it fails**

From `frontend/`:
Run: `pnpm vitest run src/cockpit/ActivityPanel.test.tsx`
Expected: FAIL — cannot resolve `./ActivityPanel`.

- [ ] **Step 5: Write the data hook**

Create `frontend/src/api/activity.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type ActivityRow = components["schemas"]["ActivityRowOut"];
export type ActivityFeed = components["schemas"]["ActivityFeedOut"];

/** GET /api/workspaces/{id}/activity — curated reverse-chronological feed.
 *  Polled every 20s (lower urgency than Problems' 15s); recomputed per GET. */
export function useActivity(id: string) {
  return useQuery({
    queryKey: ["activity", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/activity", {
          params: { path: { workspace_id: id } },
        }),
      ) as ActivityFeed,
    refetchInterval: 20000,
  });
}
```

- [ ] **Step 6: Write the panel**

Create `frontend/src/cockpit/ActivityPanel.tsx`:

```tsx
import { type ActivityRow, useActivity } from "../api/activity";
import type { BottomTab, NavTarget, OnNavigate, TopTab } from "./nav";

// The generated target Literals match TopTab/BottomTab exactly, so this is a
// null -> undefined normalization (same shape as ProblemsPanel's toNav).
function toNav(target: ActivityRow["target"]): NavTarget {
  return {
    top_tab: (target?.top_tab ?? undefined) as TopTab | undefined,
    bottom_tab: (target?.bottom_tab ?? undefined) as BottomTab | undefined,
  };
}

function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function ActivityPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useActivity(id);

  if (q.isError) {
    return (
      <div className="activity-panel">
        <p role="status" className="activity-error">
          Could not load activity.
        </p>
      </div>
    );
  }
  if (q.isLoading) {
    return (
      <div className="activity-panel">
        <p className="activity-loading">Loading…</p>
      </div>
    );
  }
  const rows: ActivityRow[] = q.data?.items ?? [];
  if (rows.length === 0) {
    return (
      <div className="activity-panel">
        <p className="activity-empty">No activity yet.</p>
      </div>
    );
  }
  return (
    <div className="activity-panel">
      <ul className="activity-list">
        {rows.map((r) => (
          <li key={r.id} className={`activity-row category-${r.category}`}>
            <span className="activity-actor">{r.actor ?? "System"}</span>{" "}
            <span className="activity-summary">{r.summary}</span>
            <span className="activity-time"> · {formatRelativeTime(r.created_at)}</span>
            {r.target && (
              <button
                type="button"
                className="activity-act"
                onClick={() => onNavigate(toNav(r.target))}
              >
                Go to
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 7: Run the test + type-check to verify they pass**

From `frontend/`:
Run: `pnpm vitest run src/cockpit/ActivityPanel.test.tsx`
Expected: PASS — 3 passed.
Run: `pnpm exec tsc --noEmit`
Expected: no output (exit 0).

- [ ] **Step 8: Commit**

```bash
git -C .worktrees/sp2b-3e-activity-feed add \
  frontend/src/cockpit/nav.ts \
  frontend/src/api/schema.gen.ts \
  frontend/src/api/activity.ts \
  frontend/src/cockpit/ActivityPanel.tsx \
  frontend/src/cockpit/ActivityPanel.test.tsx
git -C .worktrees/sp2b-3e-activity-feed -c commit.gpgsign=false commit -m "feat(activity): ActivityPanel + useActivity hook + nav/types

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire the Activity tab into BottomPanel

**Files:**
- Modify: `frontend/src/cockpit/BottomPanel.tsx`
- Test: `frontend/src/cockpit/BottomPanel.test.tsx`

**Interfaces:**
- Consumes: `ActivityPanel` from `./ActivityPanel`; `BottomTab` (now includes `"activity"`).
- Produces: a third `role="tab"` ("Activity", no count badge) and a third body branch. `Cockpit.tsx` needs no change — its `onNavigate`/`setBottomTab` are already generic over `BottomTab`.

- [ ] **Step 1: Update the failing test**

Edit `frontend/src/cockpit/BottomPanel.test.tsx`. Add the ActivityPanel mock next to the others:

```tsx
vi.mock("./ActivityPanel", () => ({ ActivityPanel: () => <div>activity-body</div> }));
```

Add a test inside the `describe("BottomPanel", ...)` block:

```tsx
  it("exposes the Activity tab and shows its body when active", () => {
    const onTab = vi.fn();
    const { rerender } = render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    const tab = screen.getByRole("tab", { name: /^activity$/i });
    expect(tab).toBeInTheDocument();
    fireEvent.click(tab);
    expect(onTab).toHaveBeenCalledWith("activity");
    rerender(
      <BottomPanel
        id="ws1"
        activeTab="activity"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.getByText("activity-body")).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run the test to verify it fails**

From `frontend/`:
Run: `pnpm vitest run src/cockpit/BottomPanel.test.tsx`
Expected: FAIL — no tab named "Activity" / `activity-body` not found.

- [ ] **Step 3: Add the tab + body branch**

Edit `frontend/src/cockpit/BottomPanel.tsx`. Add the import:

```tsx
import { ActivityPanel } from "./ActivityPanel";
```

Add the third tab button immediately after the Problems tab button (after the `Problems {problemsCount}` button closes):

```tsx
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "activity"}
            onClick={() => onTab("activity")}
          >
            Activity
          </button>
```

Replace the body ternary (the `{activeTab === "inbox" ? (...) : (...)}` block) with three explicit conditionals:

```tsx
      {!collapsed && (
        <div className="bottom-panel-body">
          {activeTab === "inbox" && <InboxPanel id={id} count={inboxCount} />}
          {activeTab === "problems" && <ProblemsPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "activity" && <ActivityPanel id={id} onNavigate={onNavigate} />}
        </div>
      )}
```

- [ ] **Step 4: Run the test + type-check + full frontend suite**

From `frontend/`:
Run: `pnpm vitest run src/cockpit/BottomPanel.test.tsx`
Expected: PASS — all BottomPanel tests pass (the existing two + the new one).
Run: `pnpm exec tsc --noEmit`
Expected: no output (exit 0).
Run: `pnpm vitest run`
Expected: PASS — full frontend suite green (no regressions in Cockpit/Problems/Inbox tests).

- [ ] **Step 5: Commit**

```bash
git -C .worktrees/sp2b-3e-activity-feed add \
  frontend/src/cockpit/BottomPanel.tsx \
  frontend/src/cockpit/BottomPanel.test.tsx
git -C .worktrees/sp2b-3e-activity-feed -c commit.gpgsign=false commit -m "feat(activity): wire Activity tab into the bottom panel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 4)

Run from the worktree root:
- `uv run pytest tests/proposal_assist/activity/ -v` → all activity backend tests pass (re-run any connection-refused file in isolation).
- From `frontend/`: `pnpm exec tsc --noEmit` (exit 0) and `pnpm vitest run` (full suite green).

No Alembic migration was added (confirm `alembic` heads still report `0055`). No file under `evaluator/`, `coherence/`, `review/`, `uncited_claims/`, or `drafting/` was modified.
