# Proposal-Assist Live Turn Monitor (B2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the proposal-assist live loop — artifact refresh on turn-end, a working/Cancel affordance, and mid-turn-reload resilience — by evolving the per-turn SSE connector into a robust **per-job** connector with stable-id client-side de-duplication (the A2 model).

**Architecture:** Each in-flight turn gets one SSE connector watching exactly that `turn_job_id`. The stream stays workspace-global, so two live connectors can carry the same later rows; duplicates are suppressed client-side by a stable `id="chat-turn-{turn.id}"` via two hooks (`htmx:sseBeforeMessage` for streamed rows, `htmx:beforeSwap` for the POST echo). `post_turn` echoes the *persisted* user turn (real id) + the connector; `get_workspace_main` re-renders one connector per active job on reload. Connector cleanup (a real `sse-swap="done"` delete-child) drives an idempotent artifact refetch and a deferred, multi-connector-safe re-enable of section editing. Depends on shipped **B1** (`record_terminal_outcome`).

**Tech Stack:** Python 3.12 · FastAPI · Jinja2 + HTMX + vendored `htmx-sse.js` · SQLAlchemy 2.x / PostgreSQL · pytest + testcontainers · Node 18 `node --test` (first JS test in the repo).

**Spec:** [docs/superpowers/specs/2026-06-13-proposal-assist-turn-monitor-design.md](../specs/2026-06-13-proposal-assist-turn-monitor-design.md)

---

## File Structure

**New files**
- `src/call_manager/web/templates/proposal_assist/partials/turn_connector.html` — the per-job connector (sse-mount + message swap + `done` delete-child + working indicator + owner-gated Cancel). Rendered by both `post_turn` and `workspace_main.html`. Params: `workspace_id`, `job` (has `.id`/`.status`), `since` (int|None), `is_owner` (bool).
- `src/call_manager/web/templates/proposal_assist/partials/artifact_body.html` — sections-or-placeholder body, extracted from `artifact_pane.html`. Rendered by `artifact_pane.html` (page load) and the new `GET /{id}/artifact` route (refresh).
- `src/call_manager/web/static/dedup.js` — DOM-free pure helpers `extractChatTurnId(html)` + `stripPresentChatTurns(html, hasId)`; CJS `module.exports` for Node tests **and** `window.cmDedup` for the browser.
- `tests/js/dedup.test.js` — `node --test` coverage of the two pure helpers.
- `tests/js/app_wiring.test.js` — loads `app.js` under `node:vm` with DOM stubs and dispatches the real `htmx:sseBeforeMessage` / `htmx:beforeSwap` events to verify the wiring (event names, `preventDefault`, `serverResponse` rewrite).

**Modified files**
- `src/call_manager/proposal_assist/web/workspaces.py` — `_stream_turn_generator` (`since_position` + terminal-read-first reorder); `stream_turn` (`?since`); `post_turn` (persisted echo + connector partial); `get_workspace_main` (active-jobs query before history + connector context); `cancel_turn` (span-friendly fragment); new `GET /{id}/artifact`; new `_artifact_sections_and_rendered` helper.
- `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` — stable `id="chat-turn-{id}"`.
- `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html` — include `artifact_body.html`.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — `data-workspace-id` on `#chat-history`; per-active-job connector loop.
- `src/call_manager/web/templates/base.html` — load `dedup.js` before `app.js`.
- `src/call_manager/web/static/app.js` — dedup hooks; request-aware re-enable (composer-request counter + `htmx:afterRequest`, re-enabling only when no connector AND no pending submit remain — so a rejected submit doesn't strand editing); artifact refetch on cleanup; load-time disable when a connector is present.
- `src/call_manager/web/static/app.css` — `.turn-progress` / Cancel styling.
- `.github/workflows/ci.yml` — run `node --test tests/js/`.
- Existing tests updated where the connector markup contract changed.

---

## Task 1: Stable `id` on chat rows + contract test

**Files:**
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html:26`
- Test: `tests/proposal_assist/test_web_workspaces_sse.py`

- [ ] **Step 1: Write the failing contract test**

Add to `tests/proposal_assist/test_web_workspaces_sse.py`:

```python
def test_render_chat_turn_html_emits_stable_id():
    """A1/J: the .chat-turn div carries id='chat-turn-{turn.id}' so the
    client dedup can match the same row across the echo + SSE + history
    render paths. A turn with no id (never persisted) emits no id attr."""
    import uuid

    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.web.workspaces import _render_chat_turn_html

    tid = uuid.uuid4()
    turn = ChatTurn(
        id=tid,
        workspace_id=uuid.uuid4(),
        role="user",
        content={"type": "text", "text": "hi"},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    assert f'id="chat-turn-{tid}"' in snippet

    idless = ChatTurn(
        workspace_id=uuid.uuid4(),
        role="user",
        content={"type": "text", "text": "hi"},
        position=1,
    )
    assert "chat-turn-None" not in _render_chat_turn_html(idless)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py::test_render_chat_turn_html_emits_stable_id -v`
Expected: FAIL (`id="chat-turn-..."` not in snippet — the div has no id attribute yet).

- [ ] **Step 3: Add the id attribute**

In `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html`, change line 26 from:

```jinja
<div class="chat-turn" data-role="{{ turn.role }}">
```

to:

```jinja
<div class="chat-turn" data-role="{{ turn.role }}"{% if turn.id %} id="chat-turn-{{ turn.id }}"{% endif %}>
```

- [ ] **Step 4: Run the test + the existing escape tests**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py -v`
Expected: PASS (new test passes; the existing `test_render_chat_turn_html_*` escape tests still pass — they build turns with explicit `id=uuid4()`).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/chat_turn.html tests/proposal_assist/test_web_workspaces_sse.py
git commit -m "feat(proposal-assist): stable id on chat rows for client dedup"
```

---

## Task 2: Generator — `since_position` seed + terminal-read-first ordering

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:273-343` (`_stream_turn_generator`)
- Test: `tests/proposal_assist/test_web_workspaces_sse.py`

- [ ] **Step 1: Write the failing ordering + seed tests**

Add to `tests/proposal_assist/test_web_workspaces_sse.py`:

```python
@pytest.mark.asyncio
async def test_generator_does_not_lose_row_finalized_mid_poll(pg_session_factory, current_user):
    """Bug-2 REGRESSION (this fails on the old rows-first order, passes on the
    terminal-read-first order). We deterministically commit the finalize as a
    ONE-SHOT side effect of the poll's first ChatTurn row query — i.e. AFTER that
    poll's row snapshot. Old order: row query (empty) -> finalize commits ->
    status read sees terminal -> emits 'done' WITHOUT the outcome row -> lost.
    New order: status read (still running) -> row query (empty) -> finalize
    commits -> no 'done' this poll -> next poll delivers the row before 'done'."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.web.workspaces import _stream_turn_generator
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="running", queued_at=datetime.now(UTC), started_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    fired = {"done": False}

    def _finalize_once():
        if fired["done"]:
            return
        fired["done"] = True
        with pg_session_factory() as s2:
            s2.add(ChatTurn(
                workspace_id=wsid, role="system",
                content={"type": "turn_outcome", "outcome": "failed", "text": "FAILMARKER"},
            ))
            j = s2.get(TurnJob, jid)
            j.status = "failed"
            j.completed_at = datetime.now(UTC)
            s2.commit()

    class _Sess:
        """Delegates to a real session; the FIRST scalars() call (the poll's
        ChatTurn row query) returns its rows, THEN commits the finalize — so the
        finalize lands just after this poll's row snapshot."""

        def __init__(self, inner):
            self._inner = inner

        def scalars(self, *a, **k):
            result = self._inner.scalars(*a, **k)
            if not fired["done"]:
                rows = list(result)
                _finalize_once()
                return rows
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

    def _factory():
        return _Sess(pg_session_factory())

    events = []
    async for chunk in _stream_turn_generator(
        session_factory=_factory, workspace_id=wsid, turn_job_id=jid, poll_seconds=0.05,
    ):
        events.append(chunk)
        if len(events) > 40:
            pytest.fail("stream did not terminate")
    full = "".join(events)
    assert "FAILMARKER" in full  # FAILS on the old rows-first order (row lost behind done)
    assert full.index("FAILMARKER") < full.index("event: done")


@pytest.mark.asyncio
async def test_generator_since_position_skips_rows_at_or_below(pg_session_factory, current_user):
    """A reload connector passes since_position=P so it does not re-yield
    rows already on the page (position <= P)."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.web.workspaces import _stream_turn_generator
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="completed", queued_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        s.add(job)
        r1 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "FIRST"})
        r2 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "SECOND"})
        s.commit()
        wsid, jid, r1id, r2id = ws.id, job.id, r1.id, r2.id

    # Read server-assigned positions in a fresh session.
    with pg_session_factory() as s:
        p1 = s.scalar(select(ChatTurn.position).where(ChatTurn.id == r1id))
        p2 = s.scalar(select(ChatTurn.position).where(ChatTurn.id == r2id))
    assert p1 < p2

    events = []
    async for chunk in _stream_turn_generator(
        session_factory=pg_session_factory, workspace_id=wsid, turn_job_id=jid,
        since_position=p1, poll_seconds=0.05,
    ):
        events.append(chunk)
        if len(events) > 20:
            pytest.fail("stream did not terminate")
    full = "".join(events)
    assert "SECOND" in full
    assert "FIRST" not in full
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py::test_generator_does_not_lose_row_finalized_mid_poll tests/proposal_assist/test_web_workspaces_sse.py::test_generator_since_position_skips_rows_at_or_below -v`
Expected: BOTH FAIL — `test_generator_since_position_skips_rows_at_or_below` with `TypeError: ... unexpected keyword argument 'since_position'`; `test_generator_does_not_lose_row_finalized_mid_poll` with `assert "FAILMARKER" in full` failing (the current rows-first order emits `done` without the mid-poll outcome row). These two failures prove the test exercises the reorder, not just any timing.

- [ ] **Step 3: Add `since_position` + reorder the poll body**

In `src/call_manager/proposal_assist/web/workspaces.py`, change the generator signature (line 273-281) to add `since_position`:

```python
async def _stream_turn_generator(
    *,
    session_factory,
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    since_position: int | None = None,
    poll_seconds: float = 0.2,
    request: Request | None = None,
    templates: Jinja2Templates | None = None,
):
```

Replace the seed block (current lines 301-310) so a caller-supplied seed wins:

```python
    # Seed last_seen_position: an explicit since_position (reload / reconnect)
    # wins; otherwise seed at the user_turn's position so we don't re-echo it.
    with session_factory() as s:
        job = s.get(TurnJob, turn_job_id)
        if job is None or job.workspace_id != workspace_id:
            yield "event: done\ndata: missing\n\n"
            return
        if since_position is not None:
            last_seen_position = since_position
        else:
            last_seen_position = (
                s.scalar(select(ChatTurn.position).where(ChatTurn.id == job.user_turn_id)) or 0
            )
```

Replace the poll loop (current lines 312-343) with the **terminal-read-first** order:

```python
    while True:
        if request is not None and await request.is_disconnected():
            return
        with session_factory() as s:
            # (1) Read terminal status FIRST. Because B1's helper writes the
            # outcome row + status flip in one transaction, any poll that reads
            # "terminal" is guaranteed by the (later) row fetch below to also
            # see the outcome row — so no row is lost behind 'done' (bug 2).
            tj = s.get(TurnJob, turn_job_id)
            terminal = tj is None or tj.status in TERMINAL_STATUSES
            # (2) Fetch + yield all rows after the seed (workspace-global;
            # overlapping rows from other live connectors are deduped client-side).
            new_rows = list(
                s.scalars(
                    select(ChatTurn)
                    .where(
                        ChatTurn.workspace_id == workspace_id,
                        ChatTurn.position > last_seen_position,
                    )
                    .order_by(ChatTurn.position)
                )
            )
            ref_ids = _collect_reference_ids(new_rows)
            ref_map = _load_reference_map(s, workspace_id, ref_ids)
            for turn in new_rows:
                rendered = _render_chat_turn_html(
                    turn,
                    templates=templates,
                    chat_reference_map=ref_map,
                )
                yield f"data: {rendered}\n\n"
                last_seen_position = turn.position
            # (3) Only now, if this job was terminal, close the stream.
            if terminal:
                yield f"event: done\ndata: {tj.status if tj is not None else 'missing'}\n\n"
                return
        await asyncio.sleep(poll_seconds)
```

- [ ] **Step 4: Run the new + existing generator tests**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py -v`
Expected: PASS (both new tests; the existing `test_sse_emits_new_chat_turns_then_done` and `test_sse_poll_loop_handles_deleted_turn_job` still pass — the deleted-job case still yields `done` via `tj is None`).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_sse.py
git commit -m "feat(proposal-assist): generator since_position seed + terminal-read-first ordering"
```

---

## Task 3: `stream_turn` route accepts `?since`

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:1023-1055` (`stream_turn`)
- Test: `tests/proposal_assist/test_web_workspaces_sse.py`

- [ ] **Step 1: Write the failing route integration test**

Add to `tests/proposal_assist/test_web_workspaces_sse.py`. This proves `?since` is actually **forwarded** to the generator (not merely accepted): with a completed job (so the stream is finite and `.get` reads the whole body), rows at/below `P` are absent and rows above `P` are present. (Note: FastAPI **ignores** undeclared query params rather than 422-ing, so a signature check alone proves nothing.)

```python
def test_stream_route_since_skips_already_rendered_rows(
    client_authed_owner, pg_session_factory, current_user
):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="completed", queued_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        s.add(job)
        r1 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "FIRST"})
        r2 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "SECOND"})
        s.commit()
        wsid, jid, r1id = ws.id, job.id, r1.id

    with pg_session_factory() as s:
        p1 = s.scalar(select(ChatTurn.position).where(ChatTurn.id == r1id))

    # Completed job -> the generator does one poll then 'done', so .get() reads
    # the whole finite stream body.
    resp = client_authed_owner.get(f"/workspaces/{wsid}/turns/{jid}/stream?since={p1}")
    assert resp.status_code == 200
    body = resp.text
    assert "SECOND" in body
    assert "FIRST" not in body  # row at position p1 was seeded past
    assert "event: done" in body
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py::test_stream_route_since_skips_already_rendered_rows -v`
Expected: FAIL — without `?since` handling the route ignores the param and the generator seeds at the user-turn position, so `FIRST` (a row above that seed) **is** streamed → `assert "FIRST" not in body` fails.

- [ ] **Step 3: Add the `since` query param + forward it**

In `src/call_manager/proposal_assist/web/workspaces.py`, add `since` to the `stream_turn` signature (after `turn_job_id`, line 1026):

```python
@router.get("/{workspace_id}/turns/{turn_job_id}/stream")
async def stream_turn(
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    request: Request,
    since: int | None = None,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> StreamingResponse:
```

Forward it in the `StreamingResponse` call (lines 1046-1055):

```python
    return StreamingResponse(
        _stream_turn_generator(
            session_factory=request.app.state.session_factory,
            workspace_id=ws.id,
            turn_job_id=turn_job_id,
            since_position=since,
            request=request,  # B6-residuals: enables client-disconnect detection
            templates=request.app.state.templates,
        ),
        media_type="text/event-stream",
    )
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_sse.py::test_stream_route_since_skips_already_rendered_rows -v`
Expected: PASS (`FIRST` absent, `SECOND` + `event: done` present).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_sse.py
git commit -m "feat(proposal-assist): stream route accepts ?since seed param"
```

---

## Task 4: Connector partial + `post_turn` echoes the persisted turn

**Files:**
- Create: `src/call_manager/web/templates/proposal_assist/partials/turn_connector.html`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:974-1020` (`post_turn` echo + connector)
- Test: `tests/proposal_assist/test_web_workspaces_turns.py`

- [ ] **Step 1: Create the connector partial**

Create `src/call_manager/web/templates/proposal_assist/partials/turn_connector.html`:

```jinja
{# Per-job SSE connector (A2). Rendered by post_turn (since=None, is_owner=True)
   and by workspace_main.html's reload loop (since=max rendered position).
   `since is defined and since is not none` guards against an empty ?since
   that would 422 the route into a reconnect loop. Cancel is owner-only and
   queued-only (B1's helper rejects a non-queued cancel anyway). #}
<div id="sse-mount-{{ job.id }}" hx-ext="sse"
     sse-connect="/workspaces/{{ workspace_id }}/turns/{{ job.id }}/stream{% if since is defined and since is not none %}?since={{ since }}{% endif %}">
  <div sse-swap="message" hx-target="#chat-history" hx-swap="beforeend"></div>
  <div sse-swap="done" hx-target="#sse-mount-{{ job.id }}" hx-swap="delete"></div>
  <div class="turn-progress" aria-busy="true">
    Working…
    {% if is_owner and job.status == "queued" %}
      <button type="submit"
              hx-post="/workspaces/{{ workspace_id }}/turns/{{ job.id }}/cancel"
              hx-target="#cancel-status-{{ job.id }}" hx-swap="innerHTML">Cancel</button>
      <span id="cancel-status-{{ job.id }}"></span>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 2: Write the failing post_turn tests + update the contract test**

In `tests/proposal_assist/test_web_workspaces_turns.py`, **replace** the existing `test_post_turn_response_includes_user_turn_and_sse_connector` body's connector assertions (the `sse-close=` line) and add two tests:

```python
def test_post_turn_response_includes_user_turn_and_sse_connector(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H1: response body contains both the rendered user turn AND the SSE
    connector div (both appended to #chat-history). A2: the connector uses a
    real `done` delete-child, not the no-op sse-close attribute."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()
    assert "Hello agent" in body
    assert "chat-turn" in body
    assert "sse-connect=" in body
    assert "/turns/" in body and "/stream" in body
    assert 'hx-ext="sse"' in body
    # A2: real cleanup via a done delete-child; the no-op sse-close is gone.
    assert 'sse-swap="done"' in body
    assert 'hx-swap="delete"' in body
    assert "sse-close=" not in body


def test_post_turn_echo_carries_stable_chat_turn_id(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """A2: the echo renders the PERSISTED user turn (real id), so the client
    dedup can collapse it against the same row arriving via another live
    connector."""
    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        user_turn_id = job.user_turn_id
        # user_turn_id is a real persisted row (nullable FK, but set here).
        assert s.get(ChatTurn, user_turn_id) is not None

    assert f'id="chat-turn-{user_turn_id}"' in body


def test_post_turn_connector_shows_cancel_for_owner(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """The owner who submits a queued turn gets a Cancel control on the
    connector, targeting its own #cancel-status span (not #chat-history)."""
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    body = response.content.decode()
    with pg_session_factory() as s:
        jid = s.scalar(select(TurnJob.id).where(TurnJob.workspace_id == wsid))
    assert f'id="cancel-status-{jid}"' in body
    assert f"/turns/{jid}/cancel" in body
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py::test_post_turn_echo_carries_stable_chat_turn_id tests/proposal_assist/test_web_workspaces_turns.py::test_post_turn_response_includes_user_turn_and_sse_connector -v`
Expected: FAIL (echo lacks the persisted id — it renders a synthetic row; `sse-close=` still present; no `done` delete-child).

- [ ] **Step 4: Render the persisted turn + the connector partial**

In `src/call_manager/proposal_assist/web/workspaces.py`, **replace** the synthetic-echo block and connector f-string (current lines 974-1020 — from the `# B7 / T4: render the just-posted user turn …` comment through `return HTMLResponse(user_turn_html + sse_connector)`) with:

```python
    # A2: echo the PERSISTED user turn (real DB id) so the client dedup can
    # collapse it against the same row streamed by any other live connector.
    # user_turn_id is a nullable FK (ON DELETE SET NULL); submit_user_turn just
    # wrote it in the committed transaction, so it is present — but guard the
    # None case rather than render an id-less echo that would defeat the dedup.
    persisted = session.get(ChatTurn, job.user_turn_id)
    if persisted is None:
        raise HTTPException(status_code=500, detail="user turn vanished after enqueue")
    echo_ref_map = _load_reference_map(
        session,
        ws.id,
        _collect_reference_ids([persisted]),
    )
    templates = request.app.state.templates
    user_turn_html = templates.env.get_template(_CHAT_TURN_PARTIAL).render(
        turn=persisted, chat_reference_map=echo_ref_map
    )
    connector_html = templates.env.get_template(
        "proposal_assist/partials/turn_connector.html"
    ).render(workspace_id=workspace_id, job=job, since=None, is_owner=True)
    return HTMLResponse(user_turn_html + connector_html)
```

(The `_collect_reference_ids` import and `ChatTurn`, `HTTPException`, `_CHAT_TURN_PARTIAL`, `_load_reference_map` are all already in this module. The old block's `all_ids` / `reference_ids` echo-content rebuild is dropped — the persisted row already holds the authoritative content.)

- [ ] **Step 5: Run the post_turn tests**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v`
Expected: PASS (all three above; the other existing turns tests — escape, multipart, viewer-403 — still pass; the escape test now finds the escaped content inside the persisted-row render).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/turn_connector.html src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_turns.py
git commit -m "feat(proposal-assist): connector partial + persisted-turn echo (A2)"
```

---

## Task 5: Reconnect on render — `get_workspace_main` active-jobs query + connector loop

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:513-638` (`get_workspace_main`)
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html:28,33`
- Test: `tests/proposal_assist/test_web_workspaces_turns.py`

- [ ] **Step 1: Write the failing reload tests**

Add to `tests/proposal_assist/test_web_workspaces_turns.py`:

```python
def test_reload_renders_connector_for_active_running_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Mid-turn reload: get_workspace_main renders a connector for the active
    job, seeded at the last rendered chat position. A running job shows the
    working indicator but no Cancel."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="running", queued_at=datetime.now(UTC), started_at=datetime.now(UTC),
        )
        s.add(job)
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "partial"})
        s.commit()
        wsid, jid = ws.id, job.id
        max_pos = s.scalar(
            select(ChatTurn.position).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position.desc())
        )

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body
    # The connector seed reflects the FULL rendered history (necessary, not
    # sufficient, for the ordering guarantee — see the note below the test).
    assert f"?since={max_pos}" in body
    assert "Working" in body
    # Running job: no Cancel control.
    assert f"/turns/{jid}/cancel" not in body


def test_reload_connector_for_queued_job_shows_cancel(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="queued", queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body
    assert f"/turns/{jid}/cancel" in body


def test_reload_no_connector_when_no_active_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert "sse-mount-" not in body
    assert 'data-workspace-id' in body


def test_reload_connector_no_cancel_for_viewer(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A viewer may watch the connector but never gets a Cancel control."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="owner2@ki.se")
        ws = make_workspace(s, owner=owner, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=owner.id,
            status="queued", queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid, jid = ws.id, job.id

    body = client_authed_user.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body  # viewer sees the connector
    assert f"/turns/{jid}/cancel" not in body  # but no Cancel
```

> **Ordering note (active-jobs-before-history).** These tests verify the *feature* (connector rendered, seeded at the full history max, owner-gated Cancel) but not the literal "active-jobs query precedes the history query" order — the max position is fixed before the request, so either order satisfies the seed assertion. That ordering is a low-severity correctness guard against a job finalizing *between* the two reads; it is enforced by code placement (Step 3 inserts the active-jobs query immediately before the existing `chat_turns` query) and confirmed in code review. A deterministic interleaving test would require swapping the handler's `app.state.session_factory` for a proxy that commits a finalize between the two queries (cf. Task 2's `_Sess`); that is disproportionate here and is deliberately omitted, not silently skipped.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py::test_reload_renders_connector_for_active_running_job -v`
Expected: FAIL (`id="sse-mount-..."` not in body — `get_workspace_main` renders no connectors yet).

- [ ] **Step 3: Query active jobs before history + add context**

In `src/call_manager/proposal_assist/web/workspaces.py` `get_workspace_main`, immediately after the auth check (current line 517, before the `chat_turns = …` query at 518) insert the **active-jobs-first** query:

```python
    # A2 reconnect-on-render: query active jobs BEFORE the history fetch, so a
    # job that finalizes between the two reads still has its outcome row picked
    # up by the (later) history query — the page is never left with no connector
    # AND a missing final row.
    active_turn_jobs = list(
        session.scalars(
            select(TurnJob)
            .where(
                TurnJob.workspace_id == workspace_id,
                TurnJob.status.in_(("queued", "running")),
            )
            .order_by(TurnJob.queued_at, TurnJob.id)
        )
    )
```

After the `chat_turns` query (current line 524), compute the seed position:

```python
    chat_max_position = chat_turns[-1].position if chat_turns else 0
```

Add both to the `TemplateResponse` context dict (current lines 622-637), e.g. after `"chat_turns": chat_turns,`:

```python
            "active_turn_jobs": active_turn_jobs,
            "chat_max_position": chat_max_position,
```

(`TurnJob` is already imported in this module.)

- [ ] **Step 4: Render `data-workspace-id` + the connector loop in the template**

In `src/call_manager/web/templates/proposal_assist/workspace_main.html`, change line 28 to add `data-workspace-id`:

```jinja
    <div id="chat-history" class="chat-history" data-workspace-id="{{ workspace.id }}">
```

Then immediately after the `cloud_banner` include (line 33), insert the connector loop (outside the setup/composer conditional so viewers and owners both get connectors):

```jinja
    {% set workspace_id = workspace.id %}
    {% for job in active_turn_jobs or [] %}
      {% set since = chat_max_position %}
      {% include "proposal_assist/partials/turn_connector.html" %}
    {% endfor %}
```

- [ ] **Step 5: Run the reload tests + the full turns suite**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v`
Expected: PASS (the four reload tests + all existing turns tests).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/proposal_assist/workspace_main.html tests/proposal_assist/test_web_workspaces_turns.py
git commit -m "feat(proposal-assist): reconnect-on-render — per-active-job connectors on reload"
```

---

## Task 6: Artifact-body partial + refresh route

**Files:**
- Create: `src/call_manager/web/templates/proposal_assist/partials/artifact_body.html`
- Modify: `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html:17-23`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (new `_artifact_sections_and_rendered` helper + `get_workspace_main` reuse + new `GET /{id}/artifact` route — a distinct path from the existing `/{id}/artifact/raw|history|at|diff` routes, no collision)
- Test: `tests/proposal_assist/test_web_workspaces_artifact.py` (**existing** file with 7 `/artifact/raw|history|at|diff` tests — append, do not replace)

- [ ] **Step 1: Create the body partial**

Create `src/call_manager/web/templates/proposal_assist/partials/artifact_body.html`:

```jinja
{# Sections-or-placeholder body, shared between page-load (artifact_pane.html)
   and the GET /{id}/artifact refresh route. Returning this partial (NOT
   artifact_pane.html) on refresh avoids nesting a duplicate #artifact-rendered.
   The placeholder path keeps a failed first turn (no proposal.md yet) from
   refreshing the pane to blank. #}
{% if artifact_sections %}
  {% include "proposal_assist/partials/artifact_sections.html" %}
{% else %}
  {{ artifact_rendered | safe }}
{% endif %}
```

- [ ] **Step 2: Point `artifact_pane.html` at the partial**

In `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html`, replace the `<div id="artifact-rendered">…</div>` block (lines 17-23) with:

```jinja
<div id="artifact-rendered">
  {% include "proposal_assist/partials/artifact_body.html" %}
</div>
```

- [ ] **Step 3: Write the failing route tests**

**Append** these four tests to the **existing** `tests/proposal_assist/test_web_workspaces_artifact.py` (it already holds 7 tests for `/artifact/raw|history|at|diff` — leave them intact). Do **not** re-add a module docstring or `from __future__` import (the file already has them):

```python
def test_artifact_route_returns_body_without_nested_wrapper(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
    body = resp.content.decode()
    # No nested #artifact-rendered (the route returns only the body).
    assert 'id="artifact-rendered"' not in body


def test_artifact_route_empty_returns_placeholder_not_blank(
    client_authed_owner, pg_session_factory, current_user
):
    """A workspace with no proposal.md yet returns the placeholder, NOT a
    blank body (so a failed first turn doesn't blank the pane)."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
    assert "no proposal.md yet" in resp.content.decode()


def test_artifact_route_404_missing_workspace(client_authed_owner):
    import uuid

    resp = client_authed_owner.get(f"/workspaces/{uuid.uuid4()}/artifact")
    assert resp.status_code == 404


def test_artifact_route_allows_viewer(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="owner3@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    resp = client_authed_user.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
```

- [ ] **Step 4: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_artifact.py -v`
Expected: the **four new** `test_artifact_route_*` tests FAIL (no `GET /{id}/artifact` route yet — FastAPI 404/405), while the **7 existing** `/artifact/raw|history|at|diff` tests still PASS (this confirms the append didn't disturb them).

- [ ] **Step 5: Extract the artifact helper + add the route**

In `src/call_manager/proposal_assist/web/workspaces.py`, add a module-level helper (place it near `_render_proposal_markdown`, after line ~175):

```python
def _artifact_sections_and_rendered(settings, workspace_id: uuid.UUID):
    """Read proposal.md and return (raw, rendered_html, sections). Shared by
    get_workspace_main and the GET /{id}/artifact refresh route so both render
    identical sections-or-placeholder content."""
    repo_dir = Path(settings.workspaces_dir) / str(workspace_id) / "repo"
    proposal_path = repo_dir / "proposal.md"
    if proposal_path.exists():
        raw = proposal_path.read_text()
        rendered = _render_proposal_markdown(raw)
    else:
        raw, rendered = "", "<em>(no proposal.md yet)</em>"
    sections = [
        {
            "index": s.index,
            "heading": s.heading,
            "sha": s.sha,
            "rendered": _render_proposal_markdown(s.text),
        }
        for s in split_sections(raw)
        if s.text
    ]
    return raw, rendered, sections
```

Refactor `get_workspace_main` to use it — replace the existing proposal-read + sections block **including** the `settings = request.app.state.settings` line (current lines 579-603) with:

```python
    settings = request.app.state.settings
    raw, rendered, artifact_sections = _artifact_sections_and_rendered(settings, workspace_id)
```

Add the new route (place it right after `get_workspace_main` / `get_setup_status`, e.g. after line 638's function):

```python
@router.get("/{workspace_id}/artifact", response_class=HTMLResponse)
def get_artifact_body(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Return ONLY the artifact body (sections-or-placeholder) for the live
    refresh after a turn ends. Owner-or-viewer; 404 on a missing workspace."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    settings = request.app.state.settings
    _raw, rendered, artifact_sections = _artifact_sections_and_rendered(settings, ws.id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/partials/artifact_body.html",
        {
            "workspace": ws,
            "user": user,
            "is_owner": ws.owner_id == user.id,
            "artifact_sections": artifact_sections,
            "artifact_rendered": rendered,
            "annotations_enabled": True,
        },
    )
```

- [ ] **Step 6: Run the artifact tests + the existing main-view tests**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_artifact.py tests/proposal_assist/test_web_workspaces_turns.py -v`
Expected: PASS (new route tests; `get_workspace_main` still renders the artifact pane identically via the shared helper).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/artifact_body.html src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_artifact.py
git commit -m "feat(proposal-assist): artifact_body partial + GET /{id}/artifact refresh route"
```

---

## Task 7: Span-friendly cancel fragment

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:1433-1435` (`cancel_turn` return)
- Test: `tests/proposal_assist/test_web_workspaces_cancel.py`

- [ ] **Step 1: Write the failing fragment test**

Add to `tests/proposal_assist/test_web_workspaces_cancel.py`:

```python
def test_cancel_returns_span_friendly_fragment(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """The cancel response targets a <span> (#cancel-status-{job}), so it must
    be inline text, not a block <p>."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
            status="queued", queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    resp = client_authed_owner.post(f"/workspaces/{wsid}/turns/{jid}/cancel")
    assert resp.status_code == 200
    assert resp.text == "Cancelled."
    assert "<p>" not in resp.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_cancel.py::test_cancel_returns_span_friendly_fragment -v`
Expected: FAIL (`resp.text == "Cancelled."` — current return is `<p>cancelled</p>`).

- [ ] **Step 3: Return inline text**

In `src/call_manager/proposal_assist/web/workspaces.py`, change the `cancel_turn` return (lines 1433-1435):

```python
    if did:
        return HTMLResponse("Cancelled.")
    return HTMLResponse("Too late — the assistant already started.")
```

- [ ] **Step 4: Run the cancel suite**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_cancel.py -v`
Expected: PASS (new test; existing cancel tests still pass — they assert `b"cancelled" in response.content.lower()`, satisfied by `"Cancelled."`, and the no-op status checks are unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_cancel.py
git commit -m "feat(proposal-assist): span-friendly cancel status fragment"
```

---

## Task 8: Dedup pure helpers + Node test

**Files:**
- Create: `src/call_manager/web/static/dedup.js`
- Create: `tests/js/dedup.test.js`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the failing Node tests**

Create `tests/js/dedup.test.js`:

```javascript
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const { extractChatTurnId, stripPresentChatTurns } = require(
  "../../src/call_manager/web/static/dedup.js"
);

test("extractChatTurnId finds a chat-turn id (double quotes)", () => {
  assert.strictEqual(
    extractChatTurnId('<div class="chat-turn" id="chat-turn-abc-123"><p>hi</p></div>'),
    "chat-turn-abc-123"
  );
});

test("extractChatTurnId finds a chat-turn id (single quotes)", () => {
  assert.strictEqual(
    extractChatTurnId("<div class='chat-turn' id='chat-turn-xyz'></div>"),
    "chat-turn-xyz"
  );
});

test("extractChatTurnId returns null for a non-chat id (sse-mount)", () => {
  assert.strictEqual(
    extractChatTurnId('<div id="sse-mount-9"></div>'),
    null
  );
});

test("extractChatTurnId returns null for the done payload", () => {
  assert.strictEqual(extractChatTurnId("failed"), null);
  assert.strictEqual(extractChatTurnId("completed"), null);
});

test("stripPresentChatTurns drops a present chat-turn, keeps the connector", () => {
  const frag =
    '<div class="chat-turn" id="chat-turn-A"><p>echo</p><div class="ref-chip">x</div></div>' +
    '<div id="sse-mount-J"><div sse-swap="message"></div></div>';
  const present = new Set(["chat-turn-A"]);
  const out = stripPresentChatTurns(frag, (id) => present.has(id));
  assert.ok(!out.includes("chat-turn-A"), "present row removed");
  assert.ok(out.includes("sse-mount-J"), "connector preserved");
  assert.ok(out.includes("ref-chip") === false, "nested children of the dropped row gone");
});

test("stripPresentChatTurns keeps an absent chat-turn", () => {
  const frag = '<div class="chat-turn" id="chat-turn-B"><p>new</p></div>';
  const out = stripPresentChatTurns(frag, () => false);
  assert.strictEqual(out, frag);
});

test("stripPresentChatTurns keeps one, drops the other in a two-row fragment", () => {
  const frag =
    '<div class="chat-turn" id="chat-turn-A"><p>old</p></div>' +
    '<div class="chat-turn" id="chat-turn-B"><p>new</p></div>';
  const present = new Set(["chat-turn-A"]);
  const out = stripPresentChatTurns(frag, (id) => present.has(id));
  assert.ok(!out.includes("chat-turn-A"));
  assert.ok(out.includes("chat-turn-B"));
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `node --test tests/js/`
Expected: FAIL (`Cannot find module '.../dedup.js'`).

- [ ] **Step 3: Implement the pure helpers**

Create `src/call_manager/web/static/dedup.js`:

```javascript
// Pure, DOM-free de-duplication helpers for the A2 live-turn monitor.
// Loaded in the browser (sets window.cmDedup) AND importable by node --test
// (CommonJS module.exports). No window/document access here — the browser
// glue in app.js injects the `hasId` predicate.
(function () {
  "use strict";

  // Pull the chat-turn id out of a rendered row fragment. Returns null for a
  // fragment with no chat-turn id (e.g. the connector div, or the 'done'
  // payload string), so callers can pass anything through safely.
  function extractChatTurnId(html) {
    if (typeof html !== "string") return null;
    var m = /\bid=["']?(chat-turn-[^"'\s>]+)/.exec(html);
    return m ? m[1] : null;
  }

  // Walk top-level <div>…</div> blocks (depth-counted so nested divs like
  // .ref-chip don't confuse the boundary) and drop any whose chat-turn id
  // satisfies hasId(id). Non-div text and non-chat divs (the sse-mount
  // connector) pass through unchanged.
  function stripPresentChatTurns(html, hasId) {
    if (typeof html !== "string") return html;
    var out = "";
    var i = 0;
    var n = html.length;
    while (i < n) {
      var open = html.indexOf("<div", i);
      if (open === -1) {
        out += html.slice(i);
        break;
      }
      out += html.slice(i, open); // text before this div block
      // Depth-count to the matching </div>.
      var depth = 1;
      var j = open + 4;
      while (j < n && depth > 0) {
        var nextOpen = html.indexOf("<div", j);
        var nextClose = html.indexOf("</div>", j);
        if (nextClose === -1) {
          j = n;
          break;
        }
        if (nextOpen !== -1 && nextOpen < nextClose) {
          depth++;
          j = nextOpen + 4;
        } else {
          depth--;
          j = nextClose + 6;
        }
      }
      var blockEnd = depth === 0 ? j : n;
      var block = html.slice(open, blockEnd);
      var openTag = block.slice(0, block.indexOf(">") + 1);
      var id = extractChatTurnId(openTag);
      if (!(id && hasId(id))) {
        out += block; // keep it
      }
      i = blockEnd;
    }
    return out;
  }

  var api = { extractChatTurnId: extractChatTurnId, stripPresentChatTurns: stripPresentChatTurns };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.cmDedup = api;
  }
})();
```

- [ ] **Step 4: Run the Node tests**

Run: `node --test tests/js/`
Expected: PASS (7 tests pass).

- [ ] **Step 5: Wire `node --test` into CI**

In `.github/workflows/ci.yml`, add a step after the Pytest step (line 29). Node is preinstalled on `ubuntu-latest`:

```yaml
      - name: JS unit tests
        run: node --test tests/js/
```

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/static/dedup.js tests/js/dedup.test.js .github/workflows/ci.yml
git commit -m "feat(proposal-assist): DOM-free dedup helpers + node --test + CI step"
```

---

## Task 9: Browser wiring — dedup hooks, request-aware re-enable, artifact refetch

**Files:**
- Modify: `src/call_manager/web/templates/base.html:17`
- Modify: `src/call_manager/web/static/app.js:58-83`
- Create: `tests/js/app_wiring.test.js`

The dedup *parse* logic is covered by Task 8; this task adds a vm-based harness that loads `app.js` with DOM stubs and dispatches the real events, so a wrong event name, a missing `preventDefault()`, or a missing `serverResponse` rewrite can't slip through CI. The connector/cleanup timing is still confirmed by the **manual check** in Step 5.

- [ ] **Step 1: Load `dedup.js` before `app.js`**

In `src/call_manager/web/templates/base.html`, add the dedup script immediately before the `app.js` line (line 17):

```html
  <script src="/static/dedup.js"></script>
  <script src="/static/app.js" defer></script>
```

- [ ] **Step 2: Replace the turn-lock section with request-aware gating + dedup hooks**

In `src/call_manager/web/static/app.js`, **replace** the entire existing turn-lock section (lines 58-83 — from the `// Best-effort §1 turn-lock hint…` comment through the `reenable()` function) with:

```javascript
// Section-edit lock (best-effort; server 409s remain authoritative). Editing is
// disabled while a turn is in flight, and re-enabled only when BOTH (a) no SSE
// connector remains AND (b) no composer request is still awaiting its response.
// Tracking composer requests is what keeps a REJECTED submit (409/429/503/400 —
// which creates no connector) from leaving editing disabled forever, and stops
// an old connector's deferred cleanup from re-enabling while a new submit is
// mid-flight (the connector for it has not mounted yet).
var pendingComposerRequests = 0;

function reenable() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = false;
  });
}
function disableSectionEdits() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = true;
  });
}
function maybeReenable() {
  // Deferred to a macrotask so DOM mutations (connector add on swap, remove on
  // cleanup) have settled before we read them.
  setTimeout(function () {
    if (pendingComposerRequests === 0 && !document.querySelector('[id^="sse-mount-"]')) {
      reenable();
    }
  }, 0);
}

document.body.addEventListener("htmx:beforeRequest", function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === "composer") {
    pendingComposerRequests++;
    disableSectionEdits();
  }
});
document.body.addEventListener("htmx:afterRequest", function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === "composer") {
    pendingComposerRequests = Math.max(0, pendingComposerRequests - 1);
    // On success the connector is in the DOM by now (afterSwap precedes
    // afterRequest), so maybeReenable keeps editing disabled; on a rejected
    // submit there is no connector, so this is what re-enables it.
    maybeReenable();
  }
});

// A2: when an SSE connector ('sse-mount-*') is cleaned up (its job ended and the
// 'done' delete-child removed it), (1) refetch the artifact pane (idempotent —
// harmless after an unchanged/failed turn) and (2) attempt the combined re-enable.
document.body.addEventListener("htmx:beforeCleanupElement", function (evt) {
  var id = evt.detail.elt && evt.detail.elt.id;
  if (!id || id.indexOf("sse-mount-") !== 0) return;
  var host = document.getElementById("chat-history");
  var wsId = host && host.getAttribute("data-workspace-id");
  if (wsId && window.htmx) {
    window.htmx.ajax("GET", "/workspaces/" + wsId + "/artifact", {
      target: "#artifact-rendered",
      swap: "innerHTML",
    });
  }
  maybeReenable();
});

// A2 dedup — the SSE stream is workspace-global, so two live connectors can
// carry the same row. Suppress duplicates by stable id across BOTH swap paths
// (the SSE extension's own swap does not fire htmx:beforeSwap, so streamed rows
// need their own hook).
document.body.addEventListener("htmx:sseBeforeMessage", function (evt) {
  var data = evt.detail && evt.detail.data;
  var id = window.cmDedup.extractChatTurnId(data);
  if (id && document.getElementById(id)) {
    evt.preventDefault(); // already in the DOM — skip this streamed row
  }
});
document.body.addEventListener("htmx:beforeSwap", function (evt) {
  var target = evt.detail && evt.detail.target;
  if (!target || target.id !== "chat-history") return;
  var resp = evt.detail.serverResponse;
  if (typeof resp !== "string" || resp.indexOf("chat-turn-") === -1) return;
  evt.detail.serverResponse = window.cmDedup.stripPresentChatTurns(resp, function (id) {
    return !!document.getElementById(id);
  });
});

// A2 reconnect-on-render: a connector present on page load means a turn is live,
// so disable editing until it cleans up (symmetric with the re-enable above).
function disableSectionEditsIfConnectorPresent() {
  if (document.querySelector('[id^="sse-mount-"]')) {
    disableSectionEdits();
  }
}
if (document.readyState !== "loading") {
  disableSectionEditsIfConnectorPresent();
} else {
  document.addEventListener("DOMContentLoaded", disableSectionEditsIfConnectorPresent);
}
```

(This supersedes the old `htmx:beforeRequest` block and its outdated "load-time disabling is skipped intentionally" comment.)

- [ ] **Step 3: Write + run the wiring harness test**

Create `tests/js/app_wiring.test.js` — loads `app.js` under `node:vm` with minimal DOM/window stubs, dispatches the real events, and asserts the wiring effects:

```javascript
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "../../src/call_manager/web/static/app.js");
const dedup = require("../../src/call_manager/web/static/dedup.js");

function loadApp(present) {
  const presentSet = present || new Set();
  const handlers = {};
  const body = {
    addEventListener: function (name, fn) {
      (handlers[name] = handlers[name] || []).push(fn);
    },
  };
  const documentStub = {
    body: body,
    readyState: "complete",
    addEventListener: function () {},
    getElementById: function (id) {
      return presentSet.has(id) ? { id: id } : null;
    },
    querySelector: function () {
      return null;
    },
    querySelectorAll: function () {
      return [];
    },
  };
  const context = {
    document: documentStub,
    window: { cmDedup: dedup, htmx: { ajax: function () {} } },
    setTimeout: function (fn) {
      fn();
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), context);
  return handlers;
}

function fire(handlers, name, detail) {
  let prevented = false;
  const evt = { detail: detail, preventDefault: function () { prevented = true; } };
  (handlers[name] || []).forEach(function (fn) { fn(evt); });
  return { evt: evt, prevented: prevented };
}

test("sseBeforeMessage prevents default for an already-present row", function () {
  const handlers = loadApp(new Set(["chat-turn-A"]));
  const res = fire(handlers, "htmx:sseBeforeMessage", {
    data: '<div class="chat-turn" id="chat-turn-A"><p>x</p></div>',
  });
  assert.strictEqual(res.prevented, true);
});

test("sseBeforeMessage allows a new row", function () {
  const handlers = loadApp(new Set());
  const res = fire(handlers, "htmx:sseBeforeMessage", {
    data: '<div class="chat-turn" id="chat-turn-B"><p>x</p></div>',
  });
  assert.strictEqual(res.prevented, false);
});

test("beforeSwap strips a present row from the POST response, keeps the connector", function () {
  const handlers = loadApp(new Set(["chat-turn-A"]));
  const detail = {
    target: { id: "chat-history" },
    serverResponse:
      '<div class="chat-turn" id="chat-turn-A"><p>x</p></div><div id="sse-mount-J"></div>',
  };
  fire(handlers, "htmx:beforeSwap", detail);
  assert.ok(!detail.serverResponse.includes("chat-turn-A"));
  assert.ok(detail.serverResponse.includes("sse-mount-J"));
});

test("beforeSwap ignores swaps not targeting #chat-history", function () {
  const handlers = loadApp(new Set(["chat-turn-A"]));
  const detail = {
    target: { id: "artifact-rendered" },
    serverResponse: '<div class="chat-turn" id="chat-turn-A"></div>',
  };
  fire(handlers, "htmx:beforeSwap", detail);
  assert.ok(detail.serverResponse.includes("chat-turn-A"));
});
```

Run: `node --test tests/js/`
Expected: PASS (Task 8's `dedup.test.js` + these 4 wiring tests). If a handler used the wrong event name or dropped `preventDefault()`/the rewrite, these fail.

- [ ] **Step 4: Verify the full Python suite + lint still pass**

Run: `uv run pytest tests/proposal_assist -q && uv run ruff check .`
Expected: PASS (no Python changed here, but confirm nothing regressed; ruff does not lint JS).

- [ ] **Step 5: Manual browser check (record the result in the commit/PR)**

Start the dev server (per the project recipe) and, as the workspace owner:
1. Submit a turn → exactly one user row appears; "Working…" shows; the artifact pane repaints when the turn ends; section-edit buttons disable during, re-enable after.
2. Submit a turn, then immediately submit a second → two connectors; each row appears exactly once (no duplicates); editing re-enables only after **both** finish.
3. Reload mid-turn → the connector re-appears, streaming resumes, no duplicate rows, artifact repaints on completion.
4. Trigger a **rejected** submit (e.g. submit during Phase-0 setup → 409, or with drafting disabled → 503): the inline error shows AND section editing re-enables (it is not left stuck disabled).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/base.html src/call_manager/web/static/app.js tests/js/app_wiring.test.js
git commit -m "feat(proposal-assist): browser wiring — dedup hooks, request-aware re-enable, artifact refetch"
```

---

## Task 10: Working-indicator / Cancel styling

**Files:**
- Modify: `src/call_manager/web/static/app.css`

- [ ] **Step 1: Add `.turn-progress` styling**

Append to `src/call_manager/web/static/app.css` (use the existing `--cm-*` custom properties already in this file — match the `.turn-outcome` rule added in B1):

```css
/* B2 live turn monitor: in-flight indicator + inline Cancel. */
.turn-progress {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0.25rem 0;
  font-size: 0.85rem;
  color: var(--cm-neutral-text);
}
.turn-progress button {
  margin: 0;
  padding: 0.1rem 0.5rem;
  font-size: 0.8rem;
  width: auto;
}
.turn-progress [id^="cancel-status-"] {
  color: var(--cm-neutral-text);
}
```

- [ ] **Step 2: Verify it loads (manual)**

Reload a workspace with an active turn; the "Working…" row and Cancel button render with the calm-clinical styling (no layout break).

- [ ] **Step 3: Commit**

```bash
git add src/call_manager/web/static/app.css
git commit -m "style(proposal-assist): turn-progress / cancel affordance styling"
```

---

## Final verification

- [ ] Run the whole Python suite: `uv run pytest -q` → all pass.
- [ ] Run the JS suite: `node --test tests/js/` → all pass.
- [ ] Lint: `uv run ruff check .` → clean.
- [ ] Manual end-to-end (Task 9 Step 5) confirmed.
- [ ] Then finish the branch with **superpowers:finishing-a-development-branch** (local `--no-ff` merge to master — no git remote).

---

## Self-Review notes (author)

**Spec coverage:** F (generator `since_position` + reorder) → Task 2/3. G (connector partial) → Task 4. H (post_turn persisted echo) → Task 4. I (active-jobs-before-history + reload connectors) → Task 5. J (stable id + two dedup hooks) → Tasks 1, 8, 9. K (request-aware multi-connector re-enable — counter + `htmx:afterRequest`) → Task 9. L (artifact_body + route + refetch) → Tasks 6, 9. M (cancel fragment) → Task 7. Operational-assumption / cross-tab notes are documentation-only (no code). Stable-id contract test → Task 1; deterministic bug-2 regression → Task 2; `?since` forwarding integration test → Task 3; dedup helpers + wiring automated → Tasks 8, 9; reconnect-on-render + viewer-no-Cancel → Task 5; artifact route empty-state → Task 6.

**Codex plan-review (2026-06-13) corrections folded in:** (1) Task 2's ordering test is now a deterministic intra-poll race (`_Sess` proxy commits the finalize right after the poll's row snapshot) that **fails on the old rows-first order** — the earlier finalize-before-start test passed without the reorder. (2) Task 9 adds composer-request tracking + `htmx:afterRequest` so a **rejected** submit (no connector) re-enables editing instead of stranding it, and an old connector's cleanup can't re-enable mid-new-submit. (3) Task 9 adds a `node:vm` wiring harness so a wrong event name / missing `preventDefault` / missing `serverResponse` rewrite fails CI. (4) Task 3's `?since` test is a real stream integration test (rows ≤ P absent), and its incorrect "FastAPI 422s undeclared params" comment is removed. (5) Task 6 **appends** to the pre-existing `test_web_workspaces_artifact.py` (7 `/artifact/raw|history|at|diff` tests) rather than claiming to create it; the new `/{id}/artifact` path doesn't collide with the `/artifact/*` sub-routes. (6) Task 5's reload test comment no longer over-claims to prove query order; the literal active-jobs-before-history order is enforced by code placement + review (deterministic interleaving deliberately omitted as disproportionate for a Low guard).

**Type/name consistency:** the connector partial uses `job` (object, `.id`/`.status`), `workspace_id`, `since`, `is_owner` — provided identically by `post_turn` (`.render(workspace_id=…, job=job, since=None, is_owner=True)`) and the `workspace_main.html` loop (`{% set workspace_id %}`, loop var `job`, `{% set since = chat_max_position %}`, `is_owner` in context). `window.cmDedup.{extractChatTurnId,stripPresentChatTurns}` names match dedup.js exports and the Node test imports. `_artifact_sections_and_rendered` returns `(raw, rendered, sections)`, consumed consistently by `get_workspace_main` and `get_artifact_body`.

**Deviations from the spec, with rationale:** (1) the connector partial takes a `job` object rather than scalar `job_id`/`job_status` — both call sites already hold a `TurnJob`, so this is simpler and avoids unpacking. (2) Section-edit disabling on reload is client-side (a load-time check for a rendered connector) rather than a server-rendered `disabled` attribute — it keeps all edit-gating logic in one place (`app.js`) and is symmetric with the existing submit-disable / cleanup-re-enable cycle. (3) `stripPresentChatTurns` is implemented as a depth-counting top-level-div scanner (DOM-free) so it is Node-testable, honoring the spec's "pure function" intent.
