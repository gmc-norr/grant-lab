# Proposal-Assist Unblock-Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a newly created proposal-assist workspace advanceable end-to-end: gate the composer on Phase-0 setup state, show an auto-polling status card that links to the (currently orphaned) rubric-review gate, humanize the phase chip, and reject turns posted during setup.

**Architecture:** `current_phase == 'setup'` (the server default until the rubric-review POST advances it to `'spec'`) decides composer-vs-card; the unique-per-workspace `WorkspaceSetupJob.status` decides *which* card. The card is a self-replacing htmx poll element (`hx-get` its own partial every 3s; terminal states render without the trigger, so polling stops by itself). A `409` guard in `post_turn` closes the first-message race; its JSON detail is already user-visible via the global `htmx:responseError` handler in app.js.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + htmx (server-rendered), SQLAlchemy 2.x, pytest + testcontainers Postgres, ruff.

**Spec:** [docs/superpowers/specs/2026-06-12-proposal-assist-unblock-lifecycle-design.md](../specs/2026-06-12-proposal-assist-unblock-lifecycle-design.md)

**Codex review (incorporated):** the test migration (Task 1) is broader than first scoped — `make_workspace` is called from ~50 files, there are 5 direct constructors in `test_web_workspaces_turns.py`, and three tests *rely* on the `setup` default and must be pinned. Viewers must keep polling the `waiting` card (P2). The phase chip is a unit-testable filter, asserted by exact markup (P3a). Failed-setup raw error text is owner-only (P3b).

---

## File structure

| File | Change | Responsibility |
|------|--------|----------------|
| `tests/proposal_assist/conftest.py` | `make_workspace` gains `current_phase="spec"` default | Existing tests simulate post-setup workspaces (required — the gate changes what a default workspace means). |
| `tests/proposal_assist/test_kill_switch.py`, `test_global_cap.py`, `test_web_workspaces_turns.py` | add `current_phase="spec"` at 9 direct `Workspace(...)` turn/composer sites | The constructors that POST `/turns` or assert the composer. |
| `tests/proposal_assist/test_phase0_setup.py`, `test_phase0_e2e.py`, `test_models_workspace.py` | pin `setup` where the test asserts it | The three setup-relying tests the default change would otherwise break. |
| `src/call_manager/proposal_assist/web/workspaces.py` | `post_turn` guard; new `GET /{id}/setup-status` route; `get_workspace_main` context | All route logic. |
| `src/call_manager/web/templates/proposal_assist/partials/setup_status.html` | create | The status card (one branch per job status; self-polls; viewer-aware). |
| `src/call_manager/web/filters.py` + `src/call_manager/main.py` | add + register a `phase_label` Jinja filter | Humanized, unit-testable phase chip. |
| `src/call_manager/web/templates/proposal_assist/workspace_main.html` | composer-vs-card branch; `phase_label` chip | Template gating. |
| `tests/proposal_assist/test_web_workspace_setup_gate.py` | create | Guard, endpoint-per-status, gating, viewer behaviour. |
| `tests/test_web_filters.py` (or the existing filters test) | add `phase_label` unit tests | Filter incl. unknown-phase fallback. |

No migration. No CSS change (the card reuses the existing `.callout`/`.callout-warning` rules and Pico's `aria-busy` spinner).

---

### Task 1: Existing-test migration — declare workspace phase explicitly

The 409 guard (Task 2) and composer gating (Task 5) make `current_phase` load-bearing. Today most tests build workspaces at the `setup` server-default but treat them as ready. This task makes the phase explicit: `make_workspace` defaults to `spec` (post-setup — what those tests mean), the few setup-relying tests are pinned to `setup`, and the direct turn/composer constructors get `spec`. The suite must stay green at the end (no feature code exists yet).

**Files:** `tests/proposal_assist/conftest.py`, `test_kill_switch.py`, `test_global_cap.py`, `test_web_workspaces_turns.py`, `test_phase0_setup.py`, `test_phase0_e2e.py`, `test_models_workspace.py`

- [ ] **Step 1: `make_workspace` gains a `current_phase="spec"` default**

In `tests/proposal_assist/conftest.py`, the helper signature (~line 193) currently ends:

```python
    ack_disclosure: bool = False,
) -> "Workspace":  # noqa: F821
```

Add the keyword:

```python
    ack_disclosure: bool = False,
    current_phase: str = "spec",
) -> "Workspace":  # noqa: F821
```

and the constructor (~line 214) currently reads:

```python
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
    )
```

Add the field:

```python
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
        current_phase=current_phase,
    )
```

Append to the docstring (after the `ack_disclosure` paragraph):

```python
    `current_phase` defaults to "spec" (post-setup): most tests simulate a
    ready workspace where the composer is live and POST /turns is allowed.
    Pass current_phase="setup" to exercise the Phase-0 gate (status card
    instead of composer; turns rejected with 409). Tests that assert the
    setup default must NOT rely on this helper's default — they pin "setup"
    explicitly or build the row directly.
```

- [ ] **Step 2: Pin `setup` in the three setup-relying tests**

These assert `current_phase == "setup"`; the new `spec` default would break them.

In `tests/proposal_assist/test_phase0_setup.py:306`, change:

```python
        ws = make_workspace(s, call_id=call.id)
```

to:

```python
        ws = make_workspace(s, call_id=call.id, current_phase="setup")
```

In `tests/proposal_assist/test_phase0_e2e.py` (~line 97), change:

```python
        ws = make_workspace(
            s,
            owner=owner,
            call_id=call.id,
            profile_id=profile.id,
        )
```

to:

```python
        ws = make_workspace(
            s,
            owner=owner,
            call_id=call.id,
            profile_id=profile.id,
            current_phase="setup",
        )
```

In `tests/proposal_assist/test_models_workspace.py`, the defaults test (`test_workspace_phased_columns_have_safe_defaults`, ~line 119-125) must test the genuine model default, so build the row directly instead of via the now-opinionated helper. `make_user` and `Workspace` are already imported in this file. Change:

```python
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        s.refresh(ws)
        assert ws.current_phase == "setup"
```

to:

```python
    with pg_session_factory() as s:
        owner = make_user(s)
        s.flush()
        ws = Workspace(title="Defaults check", owner_id=owner.id)
        s.add(ws)
        s.commit()
        s.refresh(ws)
        assert ws.current_phase == "setup"
```

(The remaining assertions in that test — `call_body_snapshot == ""`, etc. — are unchanged.)

- [ ] **Step 3: Add `current_phase="spec"` to the 9 direct turn/composer constructors**

These build a workspace directly (DB-default `setup`) and then POST `/turns` or assert the composer — they must be post-setup.

`tests/proposal_assist/test_kill_switch.py:36` and `:65` — both:

```python
        ws = Workspace(owner_id=owner.id, title="Test")
```
→
```python
        ws = Workspace(owner_id=owner.id, title="Test", current_phase="spec")
```

`tests/proposal_assist/test_global_cap.py:119`:

```python
        ws = Workspace(owner_id=owner.id, title="Test")
```
→
```python
        ws = Workspace(owner_id=owner.id, title="Test", current_phase="spec")
```

`tests/proposal_assist/test_global_cap.py:171`:

```python
        ws = Workspace(owner_id=owner.id, title="T")
```
→
```python
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
```

`tests/proposal_assist/test_web_workspaces_turns.py` — five sites. Line 592:

```python
        ws = Workspace(owner_id=owner.id, title="Test ws")
```
→
```python
        ws = Workspace(owner_id=owner.id, title="Test ws", current_phase="spec")
```

Lines 638, 717, 782, 840 are each:

```python
        ws = Workspace(owner_id=owner.id, title="T")
```
→
```python
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
```

(Tests in `test_*_submit*.py` call `submit_user_turn()` directly, below the route, so the route guard never sees them — do NOT touch those. `test_rubric_review_endpoint.py` manages `current_phase` itself — leave it.)

- [ ] **Step 4: Run the full proposal-assist suite — confirm the migration is green**

Run: `uv run pytest tests/proposal_assist/ -q`
Expected: same pass count as before the change (no feature code reads `current_phase` on these paths yet). If a turn-posting or composer-asserting test fails with the workspace at `setup`, it's a missed constructor — add `current_phase="spec"` there. If a phase-0/default test fails expecting `setup`, pin it `setup`. (The four files that reference `current_phase` are `test_phase0_e2e.py`, `test_phase0_setup.py`, `test_models_workspace.py`, `test_rubric_review_endpoint.py` — a quick `grep -rn current_phase tests/proposal_assist/` re-confirms the universe.)

- [ ] **Step 5: Commit**

```bash
git add tests/proposal_assist/conftest.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_phase0_setup.py tests/proposal_assist/test_phase0_e2e.py tests/proposal_assist/test_models_workspace.py
git commit -m "test(proposal-assist): make workspace phase explicit (default test workspaces to post-setup)"
```

---

### Task 2: `post_turn` setup guard (409)

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`post_turn`, guard right after `_require_owner` at ~line 697)
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
Expected: `test_post_turn_during_setup_returns_409` FAILS (turn accepted); `test_post_turn_after_setup_proceeds` passes.

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

Confirm no regression in the turn-posting suites (post-setup via Task 1):
Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py -q`
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
    assert "hx-trigger" not in r.text  # owner has an action: polling stops


def test_setup_status_failed_owner_sees_error_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="failed", last_error="boom: no rubric"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "boom: no rubric" in r.text  # owner sees diagnostics
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


def test_setup_status_viewer_waiting_keeps_polling_without_link(
    client_authed_user, pg_session_factory, current_user
):
    # P2: a viewer has no rubric action, so the waiting card must KEEP polling
    # to catch the owner's confirm (otherwise it's permanently stale).
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
    assert "/rubric-review" not in r.text       # owner-only action
    assert "hx-trigger" in r.text               # still polling (P2)
    assert "Waiting for the owner" in r.text


def test_setup_status_failed_viewer_hidden_error(
    client_authed_user, pg_session_factory, current_user
):
    # P3b: raw worker exception text is owner-only; viewers get generic copy.
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner-f@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(
        pg_session_factory, owner, job_status="failed", last_error="Traceback: secret path /srv/x"
    )
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "secret path" not in r.text  # raw diagnostics hidden from viewers


def test_setup_status_non_member_403_and_missing_404(
    client_authed_owner, pg_session_factory, current_user
):
    import uuid as _uuid

    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        other = make_user(s, email="gate-other@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, other, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 403
    r = client_authed_owner.get(f"/workspaces/{_uuid.uuid4()}/setup-status")
    assert r.status_code == 404
```

(`_make_setup_workspace(pg_session_factory, owner, ...)` reads only `owner.id`, which stays available on the expunged fixture rows — the same cross-session pattern existing tests use; `make_session_factory` sets `expire_on_commit=False`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v -k setup_status`
Expected: FAIL — all `setup_status` tests 404 (route not defined).

- [ ] **Step 3: Create the partial**

Create `src/call_manager/web/templates/proposal_assist/partials/setup_status.html`:

```jinja
{# Phase-0 setup status card. Rendered instead of the composer while
   workspace.current_phase == 'setup', and returned alone by
   GET /workspaces/{id}/setup-status (the htmx poll target).

   Self-polling: the root div carries hx-get/hx-trigger/hx-swap while the
   card is non-terminal *for this viewer*, and replaces itself every 3s.
   - owner: polls during queued/running; stops at waiting (owner has the
     rubric link to act on), failed, done.
   - viewer: polls during queued/running AND waiting (no action — must keep
     watching for the owner's confirm); stops at failed, done.

   Context: workspace, setup_status (str — job status, 'queued' when no job
   row exists yet), setup_last_error (str|None), is_owner (bool). #}
{% set _polling = setup_status in ("queued", "running")
   or (setup_status == "waiting_for_rubric_review" and not is_owner) %}
<div id="setup-status"
     class="callout{% if setup_status == 'failed' %} callout-warning{% endif %}"
     {% if _polling %}hx-get="/workspaces/{{ workspace.id }}/setup-status" hx-trigger="every 3s" hx-swap="outerHTML"{% endif %}>
  {% if setup_status == "waiting_for_rubric_review" and is_owner %}
    <p><strong>Your workspace is ready.</strong></p>
    <a role="button" href="/workspaces/{{ workspace.id }}/rubric-review">Review the rubric to start drafting</a>
  {% elif setup_status == "waiting_for_rubric_review" %}
    <p aria-busy="true">Waiting for the owner to review the rubric…</p>
  {% elif setup_status == "failed" %}
    <p><strong>Setup failed.</strong></p>
    {% if is_owner %}
      {% if setup_last_error %}<p class="muted">{{ setup_last_error }}</p>{% endif %}
      <p>Delete this workspace and create it again, or contact an administrator.</p>
    {% else %}
      <p class="muted">Contact the workspace owner or an administrator.</p>
    {% endif %}
  {% elif setup_status == "done" %}
    <p><strong>Setup is complete.</strong> <a href="/workspaces/{{ workspace.id }}">Reload</a></p>
  {% else %}
    {# queued / running / no-job row → still working #}
    <p aria-busy="true">Setting up your workspace… (usually under a minute)</p>
  {% endif %}
</div>
```

- [ ] **Step 4: Add the route**

In `src/call_manager/proposal_assist/web/workspaces.py`:

1. Add `WorkspaceSetupJob` to the existing `from call_manager.db.models import (...)` block (alphabetical position).
2. Add this route directly AFTER `get_workspace_main` (before `post_disclosure`):

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
    terminal states (for the viewing role) render without the hx-trigger,
    so the final swap stops the polling. Access mirrors the main view.
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
Expected: all pass (2 guard + 8 setup-status).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/setup_status.html src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspace_setup_gate.py
git commit -m "feat(proposal-assist): setup-status card partial + self-terminating poll endpoint"
```

---

### Task 4: `phase_label` Jinja filter (humanized phase chip)

**Files:**
- Modify: `src/call_manager/web/filters.py` (add `phase_label`)
- Modify: `src/call_manager/main.py` (import + register)
- Test: `tests/test_web_filters.py` (create if absent, else append)

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/test_web_filters.py` (create the file with this content if it doesn't exist):

```python
from call_manager.web.filters import phase_label


def test_phase_label_known_phases():
    assert phase_label("setup") == "Setting up"
    assert phase_label("spec") == "Drafting"
    assert phase_label("plan") == "Planning"
    assert phase_label("draft") == "Revising"
    assert phase_label("review") == "Review"


def test_phase_label_unknown_falls_back_to_raw():
    # Defensive: a future phase value (uninsertable today via the DB CHECK)
    # renders as-is rather than blanking the chip.
    assert phase_label("negotiation") == "negotiation"


def test_phase_label_none():
    assert phase_label(None) == "—"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_filters.py -v -k phase_label`
Expected: FAIL — `ImportError: cannot import name 'phase_label'`.

- [ ] **Step 3: Add the filter**

In `src/call_manager/web/filters.py`, add (after `model_label`, matching the existing label-map-with-passthrough pattern):

```python
def phase_label(value: str | None) -> str:
    """Human-readable label for `Workspace.current_phase`. The stored values
    are the lifecycle enum ('setup','spec','plan','draft','review'); the UI
    chip should read as words, not a raw token.

    Unknown values pass through unchanged so a future phase isn't swallowed
    into a blank chip — it just looks slightly raw until added here.
    """
    if not value:
        return "—"
    return {
        "setup": "Setting up",
        "spec": "Drafting",
        "plan": "Planning",
        "draft": "Revising",
        "review": "Review",
    }.get(value, value)
```

- [ ] **Step 4: Register the filter**

In `src/call_manager/main.py`, add `phase_label` to the filters import block (~line 41-47):

```python
from call_manager.web.filters import (
    criterion_label,
    display_date,
    display_dt,
    model_label,
    note_category_heading,
    phase_label,
)
```

and register it alongside the others (~line 366-370):

```python
    templates.env.filters["phase_label"] = phase_label
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_filters.py -v -k phase_label`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/filters.py src/call_manager/main.py tests/test_web_filters.py
git commit -m "feat(web): phase_label filter for the workspace phase chip"
```

---

### Task 5: Main-view composer gating + phase chip

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`get_workspace_main`: setup context)
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` (gating branch; `phase_label` chip)
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
    # Exact chip markup (the card ALSO says "Setting up", so assert the chip).
    assert '<span class="phase-chip">Setting up</span>' in r.text


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
    assert '<span class="phase-chip">Drafting</span>' in r.text


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

In `src/call_manager/proposal_assist/web/workspaces.py`, inside `get_workspace_main`, just before the `is_owner = ws.owner_id == user.id` line (~line 604), add:

```python
    # Phase-0 gate: while in setup, the left pane renders the setup-status
    # card (partials/setup_status.html) instead of the composer. Query the
    # job only when it's actually needed.
    if ws.current_phase == "setup":
        setup_job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        setup_status = setup_job.status if setup_job else "queued"
        setup_last_error = setup_job.last_error if setup_job else None
    else:
        setup_status, setup_last_error = None, None
```

and add the two keys to the `TemplateResponse` context dict (alongside `"is_owner"`, ~line 612):

```python
            "setup_status": setup_status,
            "setup_last_error": setup_last_error,
```

(`WorkspaceSetupJob` is already imported by Task 3.)

- [ ] **Step 4: Gate the composer and humanize the chip in `workspace_main.html`**

In `src/call_manager/web/templates/proposal_assist/workspace_main.html`:

1. Replace the chip line (line 11):

```jinja
      <span class="phase-chip">{{ workspace.current_phase }}</span>
```

with:

```jinja
      <span class="phase-chip">{{ workspace.current_phase | phase_label }}</span>
```

2. The composer region currently branches `{% if is_owner %}…composer…{% else %}…read-only…{% endif %}` (lines 34-91). Add an outer setup branch — leave the entire `<form id="composer">…</form>` block and the read-only `{% else %}` exactly as they are; only the opening `{% if %}` and the inserted include change:

```jinja
    {% if workspace.current_phase == 'setup' %}
    {% include "proposal_assist/partials/setup_status.html" %}
    {% elif is_owner %}
    <form id="composer" class="composer" method="post" enctype="multipart/form-data"
          ... (unchanged) ...
    </form>
    {% else %}
    <p class="muted"><em>(read-only — only the workspace owner can send turns)</em></p>
    {% endif %}
```

(The include inherits `workspace`, `setup_status`, `setup_last_error`, `is_owner` from the page context.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_workspace_setup_gate.py -v`
Expected: all pass (16 total).

Confirm the main-view consumers still pass (spec-phase via Task 1):
Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_web_workspaces_viewer_enforcement.py tests/proposal_assist/test_web_workspaces_disclosure_gate.py tests/proposal_assist/test_web_workspaces_main.py tests/proposal_assist/test_b7_e2e.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/proposal_assist/workspace_main.html tests/proposal_assist/test_web_workspace_setup_gate.py
git commit -m "feat(proposal-assist): gate composer on Phase-0 setup; humanized phase chip"
```

---

### Task 6: Whole-feature verification

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/filters.py src/call_manager/main.py tests/proposal_assist/test_web_workspace_setup_gate.py tests/test_web_filters.py tests/proposal_assist/conftest.py`
Expected: `All checks passed!`

Run: `uv run ruff format --check src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/filters.py src/call_manager/main.py tests/proposal_assist/test_web_workspace_setup_gate.py tests/test_web_filters.py`
Expected: already formatted (else run `uv run ruff format <paths>` and amend the relevant commit).

- [ ] **Step 2: Full proposal-assist + filters sweep**

Run: `uv run pytest tests/proposal_assist/ tests/test_web_filters.py -q`
Expected: all pass — including the rubric-review endpoint tests, every turn-posting suite, and the phase-0 worker tests (pinned to `setup` in Task 1).

- [ ] **Step 3: Spec checklist spot-check**

Every spec testing bullet maps to a test: gating (Task 5), card-per-status incl. `done` + missing-job fallback (Task 3), poll-endpoint auth 403/404 (Task 3), owner-vs-viewer incl. P2 viewer-polling and P3b viewer-error-hiding (Task 3), turn guard 409 (Task 2), phase chip exact markup + the `phase_label` unknown fallback (Tasks 4-5). Report any deviation.

---

## Self-review notes (for the implementer)

- **Order matters:** Task 1 (migration) MUST land before Task 2/5, and Task 4 (filter) before Task 5 (the chip uses it).
- **`hx-trigger` is the polling marker:** tests assert its presence/absence; it appears only inside the `{% if _polling %}` guard. Owner-`waiting` and all `failed`/`done` cards omit it; viewer-`waiting` keeps it (P2).
- **Viewer error hiding (P3b):** only the owner branch renders `setup_last_error`; the raw worker exception never reaches a viewer.
- **No new CSS:** `.callout` / `.callout-warning` (app.css §12) + Pico `aria-busy`; `role="button"` gets Pico button styling.
- **`make_workspace` doesn't create `WorkspaceSetupJob` rows** (the creation route does); `_make_setup_workspace` inserts them per test.
- **Known suite flakiness (NOT a regression):** in-process APScheduler threads can race testcontainer teardown (`Event loop is closed` noise, occasionally `test_editing_e2e`); rerun the failed test before suspecting these changes.
