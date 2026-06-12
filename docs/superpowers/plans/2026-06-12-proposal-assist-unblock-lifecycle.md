# Proposal-Assist Unblock-Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a newly created proposal-assist workspace advanceable end-to-end: gate the composer on Phase-0 setup state, show an auto-polling status card that links to the (currently orphaned) rubric-review gate, humanize the phase chip, and reject turns posted during setup.

**Architecture:** `current_phase == 'setup'` (the server default until the rubric-review POST advances it to `'spec'`) decides composer-vs-card; the unique-per-workspace `WorkspaceSetupJob.status` decides *which* card. The card is a self-replacing htmx poll element (`hx-get` its own partial every 3s; terminal states render without the trigger, so polling stops by itself). A `409` guard in `post_turn` closes the first-message race; its JSON detail is already user-visible via the global `htmx:responseError` handler in app.js.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx (server-rendered), SQLAlchemy 2.x, pytest + testcontainers Postgres, ruff.

**Spec:** [docs/superpowers/specs/2026-06-12-proposal-assist-unblock-lifecycle-design.md](../specs/2026-06-12-proposal-assist-unblock-lifecycle-design.md)

---

## File structure

| File | Change | Responsibility |
|------|--------|----------------|
| `tests/proposal_assist/conftest.py` | `make_workspace` gains `current_phase="spec"` default | Existing tests simulate post-setup workspaces (required migration — the guard changes what a default workspace means). |
| `tests/proposal_assist/test_kill_switch.py`, `tests/proposal_assist/test_global_cap.py` | add `current_phase="spec"` at 4 direct `Workspace(...)` sites | Same migration for the only direct-constructor turn-posting tests. |
| `src/call_manager/proposal_assist/web/workspaces.py` | `post_turn` guard; new `GET /{id}/setup-status` route; `get_workspace_main` context | All route logic. |
| `src/call_manager/web/templates/proposal_assist/partials/setup_status.html` | create | The status card (one branch per job status; self-polls in non-terminal states). |
| `src/call_manager/web/templates/proposal_assist/workspace_main.html` | composer-vs-card branch; phase-chip label map | Template gating. |
| `tests/proposal_assist/test_web_workspace_setup_gate.py` | create | All new tests (guard, endpoint per status, gating, chip). |

No migration. No CSS change (the card reuses the existing `.callout`/`.callout-warning` rules and Pico's `aria-busy` spinner).

---

### Task 1: Existing-test migration — default test workspaces to post-setup

The 409 guard (Task 2) and composer gating (Task 4) change what a default-phase (`'setup'`) workspace means. Existing turn-posting and main-view tests must create post-setup workspaces. This task is a behavioral no-op until those land — the suite must stay green.

**Files:**
- Modify: `tests/proposal_assist/conftest.py` (the `make_workspace` helper, around line 193)
- Modify: `tests/proposal_assist/test_kill_switch.py:36,65`
- Modify: `tests/proposal_assist/test_global_cap.py:119,171`

- [ ] **Step 1: Add the `current_phase` parameter to `make_workspace`**

In `tests/proposal_assist/conftest.py`, the helper currently reads:

```python
def make_workspace(
    session,
    *,
    owner=None,
    title: str = "Test workspace",
    call_id: int | None = None,
    profile_id: int | None = None,
    ack_disclosure: bool = False,
) -> "Workspace":  # noqa: F821
```

and constructs:

```python
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
    )
```

Change the signature to add a keyword with default `"spec"`, and pass it through:

```python
def make_workspace(
    session,
    *,
    owner=None,
    title: str = "Test workspace",
    call_id: int | None = None,
    profile_id: int | None = None,
    ack_disclosure: bool = False,
    current_phase: str = "spec",
) -> "Workspace":  # noqa: F821
```

```python
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
        current_phase=current_phase,
    )
```

Also extend the helper's docstring (after the `ack_disclosure` paragraph) with:

```python
    `current_phase` defaults to "spec" (post-setup): most tests simulate a
    fully set-up workspace where the composer is live and POST /turns is
    allowed. Pass current_phase="setup" to exercise the Phase-0 gate
    (status card instead of composer; turns rejected with 409).
```

- [ ] **Step 2: Add `current_phase="spec"` at the 4 direct constructor sites**

In `tests/proposal_assist/test_kill_switch.py` (two sites, lines ~36 and ~65), change:

```python
        ws = Workspace(owner_id=owner.id, title="Test")
```

to:

```python
        ws = Workspace(owner_id=owner.id, title="Test", current_phase="spec")
```

In `tests/proposal_assist/test_global_cap.py` line ~119, change:

```python
        ws = Workspace(owner_id=owner.id, title="Test")
```

to:

```python
        ws = Workspace(owner_id=owner.id, title="Test", current_phase="spec")
```

and line ~171, change:

```python
        ws = Workspace(owner_id=owner.id, title="T")
```

to:

```python
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
```

(These four are the only direct-constructor sites that POST `/workspaces/{id}/turns` through the route. `test_rubric_review_endpoint.py` keeps its own `setup`-phase workspaces deliberately — it tests the setup→spec transition. `test_turns_submit_with_*` call `submit_user_turn()` directly, below the route, so the route guard never sees them. Do NOT touch those files.)

- [ ] **Step 3: Run the proposal-assist suite to confirm the migration is a no-op**

Run: `uv run pytest tests/proposal_assist/ -q`
Expected: everything that passed before still passes (no behavior reads `current_phase` on these paths yet).

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/conftest.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py
git commit -m "test(proposal-assist): default test workspaces to post-setup phase (spec)"
```

---

### Task 2: `post_turn` setup guard (409)

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`post_turn`, guard right after `_require_owner` at ~line 693)
- Test: `tests/proposal_assist/test_web_workspace_setup_gate.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_web_workspace_setup_gate.py`:

```python
"""Phase-0 setup gate (Theme 7 slice A).

While current_phase == 'setup': POST /turns is rejected (409), the main view
shows the setup-status card instead of the composer, and the card self-polls
GET /workspaces/{id}/setup-status until the WorkspaceSetupJob is terminal.
"""

from __future__ import annotations


def test_post_turn_during_setup_returns_409(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(
            s, owner=current_user, ack_disclosure=True, current_phase="setup"
        )
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "too early"},
    )
    assert response.status_code == 409
    assert "still setting up" in response.json()["detail"]
    # Nothing was enqueued.
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is None


def test_post_turn_after_setup_proceeds(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # spec phase
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "hello"},
    )
    assert response.status_code in (200, 201, 303)
```

- [ ] **Step 2: Run tests to verify the guard test fails**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v`
Expected: `test_post_turn_during_setup_returns_409` FAILS (turn accepted, no 409); `test_post_turn_after_setup_proceeds` already passes.

- [ ] **Step 3: Add the guard**

In `src/call_manager/proposal_assist/web/workspaces.py`, inside `post_turn`, immediately after the `_require_owner(...)` call (ends ~line 697) and BEFORE the `settings = request.app.state.settings` line, insert:

```python
    # Phase gate: during Phase-0 setup the workspace git repo doesn't exist
    # yet, so a turn would run the agent against nothing and fail silently.
    # The composer is suppressed during setup (workspace_main.html); this
    # guards stale tabs and hand-crafted POSTs. The JSON detail is shown
    # inline by app.js's global htmx:responseError handler.
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
```

`HTTPException` is already imported in this file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v`
Expected: 2 passed.

Also confirm no regression in the turn-posting suites (now post-setup via Task 1):
Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py tests/proposal_assist/test_web_workspaces_disclosure_gate.py tests/proposal_assist/test_web_workspaces_viewer_enforcement.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspace_setup_gate.py
git commit -m "feat(proposal-assist): reject turns during Phase 0 setup (409)"
```

---

### Task 3: Setup-status card partial + poll endpoint

**Files:**
- Create: `src/call_manager/web/templates/proposal_assist/partials/setup_status.html`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (import `WorkspaceSetupJob`; new route after `get_workspace_main`)
- Test: `tests/proposal_assist/test_web_workspace_setup_gate.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_web_workspace_setup_gate.py`:

```python
def _make_setup_workspace(pg_session_factory, owner, *, job_status=None, last_error=None):
    """A setup-phase workspace, optionally with a WorkspaceSetupJob row.

    make_workspace doesn't enqueue a setup job (the creation route does), so
    tests control the job row explicitly. job_status=None ⇒ no row (the
    endpoint falls back to the polling 'queued' card).
    """
    from call_manager.db.models import WorkspaceSetupJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.flush()
        if job_status is not None:
            s.add(
                WorkspaceSetupJob(
                    workspace_id=ws.id,
                    status=job_status,
                    last_error=last_error,
                )
            )
        s.commit()
        return ws.id


def test_setup_status_running_polls_and_has_no_link(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert "hx-trigger" in r.text  # still polling
    assert "aria-busy" in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_missing_job_falls_back_to_polling_card(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status=None)
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "hx-trigger" in r.text
    assert "Setting up" in r.text


def test_setup_status_waiting_links_rubric_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert "hx-trigger" not in r.text  # terminal: polling stops


def test_setup_status_failed_shows_error_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="failed", last_error="boom: no rubric"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "boom: no rubric" in r.text
    assert "hx-trigger" not in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_done_links_back_to_workspace(
    client_authed_owner, pg_session_factory, current_user
):
    # Cross-tab race: rubric confirmed elsewhere while this card still polls.
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="done")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup is complete" in r.text
    assert f'href="/workspaces/{wsid}"' in r.text
    assert "hx-trigger" not in r.text


def test_setup_status_viewer_sees_status_without_rubric_link(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(
        pg_session_factory, owner, job_status="waiting_for_rubric_review"
    )
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "/rubric-review" not in r.text  # owner-only action
    assert "Waiting for the owner" in r.text


def test_setup_status_non_member_403_and_missing_404(
    client_authed_owner, pg_session_factory, current_user
):
    import uuid as _uuid

    from tests.proposal_assist.conftest import make_user

    # Someone else's workspace, current_user not a viewer → 403.
    with pg_session_factory() as s:
        other = make_user(s, email="gate-other@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, other, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 403
    # Unknown workspace id → 404.
    r = client_authed_owner.get(f"/workspaces/{_uuid.uuid4()}/setup-status")
    assert r.status_code == 404
```

Note on `_make_setup_workspace(pg_session_factory, owner, ...)`: the `owner` argument is a `User` row from a previous session; `make_workspace` only reads `owner.id`, which stays loaded on the expunged/returned fixtures used here (this mirrors how existing tests pass `current_user` across sessions).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v -k setup_status`
Expected: FAIL — all `setup_status` tests get 404 (route not defined).

- [ ] **Step 3: Create the partial**

Create `src/call_manager/web/templates/proposal_assist/partials/setup_status.html`:

```jinja
{# Phase-0 setup status card. Rendered instead of the composer while
   workspace.current_phase == 'setup', and returned alone by
   GET /workspaces/{id}/setup-status (the htmx poll target).

   Self-polling: while the job is non-terminal the root div carries
   hx-get/hx-trigger/hx-swap and replaces itself every 3s; terminal states
   render WITHOUT the trigger, so the final swap stops the polling.

   Context: workspace, setup_status (str — job status, 'queued' when no job
   row exists yet), setup_last_error (str|None), is_owner (bool). #}
{% set _polling = setup_status in ("queued", "running") %}
<div id="setup-status"
     class="callout{% if setup_status == 'failed' %} callout-warning{% endif %}"
     {% if _polling %}hx-get="/workspaces/{{ workspace.id }}/setup-status"
     hx-trigger="every 3s" hx-swap="outerHTML"{% endif %}>
  {% if _polling %}
    <p aria-busy="true">Setting up your workspace… (usually under a minute)</p>
  {% elif setup_status == "waiting_for_rubric_review" %}
    <div>
      <p><strong>Your workspace is ready.</strong></p>
      {% if is_owner %}
      <a role="button" href="/workspaces/{{ workspace.id }}/rubric-review">Review the rubric to start drafting</a>
      {% else %}
      <p class="muted">Waiting for the owner to review the rubric.</p>
      {% endif %}
    </div>
  {% elif setup_status == "failed" %}
    <div>
      <p><strong>Setup failed.</strong></p>
      {% if setup_last_error %}<p class="muted">{{ setup_last_error }}</p>{% endif %}
      <p>Delete this workspace and create it again, or contact an administrator.</p>
    </div>
  {% else %}
    {# 'done' — only reachable by a stale polling card after the rubric was
       confirmed in another tab (that transaction also advances the phase, so
       a fresh page load renders the composer instead of this card). #}
    <p><strong>Setup is complete.</strong>
      <a href="/workspaces/{{ workspace.id }}">Reload to start drafting</a></p>
  {% endif %}
</div>
```

- [ ] **Step 4: Add the route**

In `src/call_manager/proposal_assist/web/workspaces.py`:

1. Add `WorkspaceSetupJob` to the existing `from call_manager.db.models import (...)` block (alphabetical position within that import).
2. Add this route directly AFTER the `get_workspace_main` function (before `post_disclosure`):

```python
@router.get("/{workspace_id}/setup-status", response_class=HTMLResponse)
def get_setup_status(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Render ONLY the setup-status card partial (htmx poll target).

    The card on the main view polls this every 3s while Phase 0 runs;
    terminal job states render without the hx-trigger, so the final swap
    stops the polling. Access mirrors the main view (owner or viewer).
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    job = session.scalar(
        select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/setup_status.html",
        {
            "workspace": ws,
            "setup_status": job.status if job else "queued",
            "setup_last_error": job.last_error if job else None,
            "is_owner": ws.owner_id == user.id,
        },
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v`
Expected: all pass (2 guard + 7 setup-status).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/setup_status.html src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspace_setup_gate.py
git commit -m "feat(proposal-assist): setup-status card partial + self-terminating poll endpoint"
```

---

### Task 4: Main-view composer gating + phase chip

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`get_workspace_main`: setup context)
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` (gating branch; chip)
- Test: `tests/proposal_assist/test_web_workspace_setup_gate.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_web_workspace_setup_gate.py`:

```python
def test_main_view_during_setup_shows_card_not_composer(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert 'id="composer"' not in r.text
    # Humanized chip: the raw enum value must not render as the phase label.
    assert "Setting up" in r.text


def test_main_view_waiting_shows_rubric_link(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert 'id="composer"' not in r.text


def test_main_view_after_setup_shows_composer_not_card(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # spec phase
        s.commit()
        wsid = ws.id
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="composer"' in r.text
    assert 'id="setup-status"' not in r.text
    # Chip shows the spec-phase label, not the raw enum.
    assert "Drafting" in r.text


def test_main_view_viewer_during_setup_sees_card_without_rubric_link(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner2@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(
        pg_session_factory, owner, job_status="waiting_for_rubric_review"
    )
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert "/rubric-review" not in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v -k main_view`
Expected: FAIL — composer renders during setup; no card; chip shows raw `setup`.

- [ ] **Step 3: Add the setup context to `get_workspace_main`**

In `src/call_manager/proposal_assist/web/workspaces.py`, inside `get_workspace_main`, just before the `is_owner = ws.owner_id == user.id` line, add:

```python
    # Phase-0 gate: while the workspace is in setup, the left pane renders
    # the setup-status card (partials/setup_status.html) instead of the
    # composer. Only query the job row when it's actually needed.
    if ws.current_phase == "setup":
        setup_job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        setup_status = setup_job.status if setup_job else "queued"
        setup_last_error = setup_job.last_error if setup_job else None
    else:
        setup_status, setup_last_error = None, None
```

and add the two keys to the `TemplateResponse` context dict (alongside `"is_owner"`):

```python
            "setup_status": setup_status,
            "setup_last_error": setup_last_error,
```

- [ ] **Step 4: Gate the composer and humanize the chip in `workspace_main.html`**

In `src/call_manager/web/templates/proposal_assist/workspace_main.html`:

1. Replace the chip line:

```jinja
      <span class="phase-chip">{{ workspace.current_phase }}</span>
```

with:

```jinja
      {% set _phase_labels = {'setup': 'Setting up', 'spec': 'Drafting', 'plan': 'Planning', 'draft': 'Revising', 'review': 'Review'} %}
      <span class="phase-chip">{{ _phase_labels.get(workspace.current_phase, workspace.current_phase) }}</span>
```

2. Wrap the composer region. The current structure is:

```jinja
    {% if is_owner %}
    <form id="composer" ...>
      ...
    </form>
    {% else %}
    <p class="muted"><em>(read-only — only the workspace owner can send turns)</em></p>
    {% endif %}
```

Change the OUTER branching to (the `<form id="composer">…</form>` block itself is unchanged — keep every line of it as-is):

```jinja
    {% if workspace.current_phase == 'setup' %}
    {% include "proposal_assist/partials/setup_status.html" %}
    {% elif is_owner %}
    <form id="composer" ...>
      ...
    </form>
    {% else %}
    <p class="muted"><em>(read-only — only the workspace owner can send turns)</em></p>
    {% endif %}
```

(The include sees `workspace`, `setup_status`, `setup_last_error`, `is_owner` from the page context — Jinja includes share the enclosing context by default.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v`
Expected: all pass (13 total).

Also confirm the main-view consumers still pass (they now create spec-phase workspaces via Task 1):
Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_web_workspaces_viewer_enforcement.py tests/proposal_assist/test_web_workspaces_disclosure_gate.py tests/proposal_assist/test_b7_e2e.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/proposal_assist/workspace_main.html tests/proposal_assist/test_web_workspace_setup_gate.py
git commit -m "feat(proposal-assist): gate composer on Phase-0 setup; humanize phase chip"
```

---

### Task 5: Whole-feature verification

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspace_setup_gate.py tests/proposal_assist/conftest.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py`
Expected: `All checks passed!`

Run: `uv run ruff format --check src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspace_setup_gate.py tests/proposal_assist/conftest.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py`
Expected: already formatted (run `uv run ruff format <paths>` and amend the relevant commit if not).

- [ ] **Step 2: Full proposal-assist sweep**

Run: `uv run pytest tests/proposal_assist/ -q`
Expected: all pass — including the rubric-review endpoint tests (own setup-phase workspaces, untouched) and every turn-posting suite (post-setup via Task 1).

- [ ] **Step 3: Spot-check the spec's test checklist**

Every spec testing bullet maps to a test: gating (Task 4 tests), card-per-status incl. `done` + missing-job fallback (Task 3), poll endpoint auth (Task 3 `403/404`), owner-vs-viewer (Tasks 3+4), turn guard 409/proceeds (Task 2), phase chip labels (Task 4: "Setting up"/"Drafting"). One deliberate deviation: the spec's "unknown phase falls back to raw string" is enforced by `.get(..., fallback)` in the template but not tested — the DB CHECK constraint makes an unknown phase uninsertable, so a route-level test can't construct it. Note this in the final report.

---

## Self-review notes (for the implementer)

- **Order matters:** Task 1 (test migration) MUST land before Task 2 — without it, the guard turns ~7 existing turn-posting test files red.
- **`hx-trigger` as the polling marker:** the tests assert presence/absence of the literal `hx-trigger` attribute — it appears in the partial only inside the `{% if _polling %}` guard, so it's a faithful structural proxy for "this card still polls."
- **No new CSS:** the card reuses `.callout` / `.callout-warning` (app.css section 12) and Pico's `aria-busy` spinner; `role="button"` on the rubric link gets Pico's button styling.
- **`make_workspace` doesn't create `WorkspaceSetupJob` rows** (the creation route does); the `_make_setup_workspace` helper inserts them explicitly per test.
- **Known suite flakiness (NOT a regression):** in-process APScheduler threads can race testcontainer teardown (`Event loop is closed` noise, occasionally `test_editing_e2e`); rerun the failed test before suspecting these changes.
