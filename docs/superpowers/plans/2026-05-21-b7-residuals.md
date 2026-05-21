# B7 Residuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Close two blockers + three important items surfaced by the post-B7 thorough review, before opening B8.

**Architecture:** Surgical fixes within existing files. The token-cap fix uses Option A from the review: NULL the user-turn's pre-counted `tokens_in` after the assistant row is stamped with the SDK-reported truth. No schema change. The XSS fix wraps four error-response interpolations in `html.escape` to match the existing correct pattern at workspaces.py:671. The constant dedup lifts `MAX_ATTACHMENTS_PER_TURN` into `turns/submit.py` as a public symbol and imports it in the web layer. The empty-text validation moves from the resolver (which currently emits a misleading "attachment_resolution_failed" error) into `submit_user_turn` itself.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Postgres 16, pytest + testcontainers.

---

## File Structure

**Modify:**
- `src/call_manager/proposal_assist/web/workspaces.py` — escape four error interpolations (lines 567, 575, 585, 591/592); import the dedup'd constant.
- `src/call_manager/proposal_assist/turns/submit.py` — rename `_MAX_ATTACHMENTS_PER_TURN` → `MAX_ATTACHMENTS_PER_TURN` (public); add empty-text+attachments validation at the top of `submit_user_turn`.
- `src/call_manager/proposal_assist/turns/processor.py` — `_finalize_success` accepts `user_turn_id` and NULLs the user-turn row's `tokens_in` after the assistant row is stamped.
- `src/call_manager/proposal_assist/web/files.py` — one-line docstring note: `HTTPException.detail` from this module must never be rendered as raw HTML (currently safe — JSON-only — but the constraint should be explicit).

**Test (existing files):**
- `tests/proposal_assist/test_turns_processor.py` — add `test_finalize_success_clears_user_turn_pre_count`.
- `tests/proposal_assist/test_web_workspaces_turns.py` — add `test_post_turn_error_html_is_escaped`.
- `tests/proposal_assist/test_web_workspaces_files.py` — add `test_upload_same_image_twice_creates_two_rows`.
- `tests/proposal_assist/test_turns_submit_with_attachments.py` — add `test_submit_mixed_valid_and_cross_workspace_ids` and `test_submit_empty_text_with_attachments_raises_at_submit_time`.

**Out of scope** (genuine future work, not residuals):
- Soft-deleted workspace queued-turn UX (cancel queued turns at soft-delete time) — note in B8 plan.
- Render-shape dedupe between `_render_chat_turn_html` and `post_turn` echo path (different SSE vs HTML newline handling) — backlog.
- GIF first-frame downconvert / animated-GIF token estimate — backlog.
- `Attachment.kind` selector at upload time (B8 preliminary_data) — backlog.

---

## Task 1: Escape error-response interpolations in post_turn (XSS blocker)

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:567, 575, 585, 591-592`
- Test: `tests/proposal_assist/test_web_workspaces_turns.py`

### Step 1: Write the failing XSS regression test

Add to `tests/proposal_assist/test_web_workspaces_turns.py`:

```python
def test_post_turn_error_html_is_escaped(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Reflected XSS regression: a malicious Content-Type or upload
    failure detail must NOT land as live HTML in the swap target.
    Uses an attacker payload that, if unescaped, would execute as
    a <script> in the chat-history swap target."""
    import io

    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test ws")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        attack = "image/png<script>alert(1)</script>"
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={"content": "hi"},
            files={"files": ("x.png", io.BytesIO(b"x"), attack)},
        )
        assert resp.status_code == 400
        # The reflection MUST be escaped — neither the raw < bracket nor
        # an executable <script> tag may appear in the response body.
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text or "&#x3c;script&#x3e;" in resp.text
    finally:
        client.__exit__(None, None, None)
```

### Step 2: Run test to confirm it fails

`uv run pytest tests/proposal_assist/test_web_workspaces_turns.py::test_post_turn_error_html_is_escaped -v`
Expected: FAIL — the response body contains the raw `<script>` payload.

### Step 3: Escape the four interpolations

In `src/call_manager/proposal_assist/web/workspaces.py`, find the existing `import html as _html` (already imported per workspaces.py:671 usage) and wrap each interpolation:

```python
# Line 566-569 — wrap content_type
return HTMLResponse(
    f"<p>Unsupported content_type: {_html.escape(repr(content_type))}</p>",
    status_code=400,
)
```

```python
# Line 575 — wrap e.detail
return HTMLResponse(
    f"<p>{_html.escape(str(e.detail))}</p>",
    status_code=e.status_code,
)
```

```python
# Line 585 — wrap e.detail
return HTMLResponse(
    f"<p>{_html.escape(str(e.detail))}</p>",
    status_code=e.status_code,
)
```

```python
# Line 590-594 — wrap both content_type and detected_mime
return HTMLResponse(
    f"<p>content_type {_html.escape(repr(content_type))} does not match "
    f"the actual image format {_html.escape(repr(detected_mime))}</p>",
    status_code=400,
)
```

Note: `_html.escape(repr(x))` escapes the printable repr (which includes quotes). This is intentional — the quotes give the user a clear visual delimiter and `escape` ensures `<`/`>`/`&` from inside the quoted string can't break out.

The TokenCapExceeded path (line 660-666) interpolates server-side values (e.reason, e.used_in, etc.) — those are not user-controlled, so they're safe. Leave them alone. The empty-upload message (`"<p>empty upload</p>"` at line 579) has no interpolation — leave it. The storage-write-failure message (line 614) is static — leave it. The over-cap message (line 642) interpolates server-side counts — safe.

### Step 4: Run test to confirm it passes

`uv run pytest tests/proposal_assist/test_web_workspaces_turns.py::test_post_turn_error_html_is_escaped -v`

### Step 5: Run full proposal_assist suite for no regressions

`uv run pytest tests/proposal_assist -q`

### Step 6: Commit

```bash
git add tests/proposal_assist/test_web_workspaces_turns.py \
        src/call_manager/proposal_assist/web/workspaces.py
git commit -m "fix(b7-residuals): escape error-response HTML in post_turn

Four error branches interpolated client-controlled values
(content_type, HTTPException.detail from upload validation) into
raw HTML f-strings. A malicious Content-Type header could land
live <script> in the HTMX swap target. Pattern at line 671
already escapes ValueError correctly — apply the same to the
four older error paths."
```

---

## Task 2: Fix token-cap double-counting (vision blocker)

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py` (`_finalize_success` accepts `user_turn_id` and NULLs its `tokens_in`)
- Test: `tests/proposal_assist/test_turns_processor.py`

### Step 1: Write the failing regression test

Add to `tests/proposal_assist/test_turns_processor.py`:

```python
@pytest.mark.asyncio
async def test_finalize_success_clears_user_turn_pre_count(
    pg_session_factory,
    _settings_env,
):
    """B7 submit pre-counts vision tokens on the USER chat_turns row.
    The processor then writes the SDK's TRUE token count (which already
    includes input tokens) on the ASSISTANT chat_turns row. Without
    clearing the user-turn pre-count, the cap query double-counts the
    user's input tokens.

    After _finalize_success runs, the user-turn row must have
    tokens_in = NULL so the cap sum reflects only the SDK-reported
    truth on the assistant row."""
    from call_manager.db.models import ChatTurn, TurnJob

    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=137, tokens_out=42, model="fake", session_id="sX"),
        ],
    )

    # Simulate B7's submit-time pre-count by writing a non-NULL
    # tokens_in on the user-turn row BEFORE the drain.
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == job.user_turn_id))
        user_turn.tokens_in = 99
        s.commit()
        user_turn_id = user_turn.id

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        # User-turn's pre-count must be cleared.
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        assert user_turn.tokens_in is None, (
            "user-turn pre-count must be NULL after finalize; "
            "otherwise the cap query double-counts input tokens"
        )
        # Assistant row carries the SDK truth.
        assistant_turns = list(
            s.scalars(
                select(ChatTurn)
                .where(ChatTurn.workspace_id == wsid)
                .where(ChatTurn.role == "assistant")
            )
        )
        assert len(assistant_turns) == 1
        assert assistant_turns[0].tokens_in == 137
        assert assistant_turns[0].tokens_out == 42
```

### Step 2: Run test to confirm it fails

`uv run pytest tests/proposal_assist/test_turns_processor.py::test_finalize_success_clears_user_turn_pre_count -v`
Expected: FAIL with `assert user_turn.tokens_in is None` (actual 99).

### Step 3: Thread `user_turn_id` into `_finalize_success` and NULL the user row

In `src/call_manager/proposal_assist/turns/processor.py`:

3a. Update the `_finalize_success` signature to accept `user_turn_id`:

```python
def _finalize_success(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    submitted_by_user_id: int | None,
    user_turn_id: uuid.UUID,
    last_assistant_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int,
    model: str | None,
    session_id: str | None,
) -> None:
```

3b. Inside the function, after the assistant row stamping block (around line 303), clear the user-turn's pre-count:

```python
        # B7 cap-accounting fix: submit_user_turn pre-stamps the USER
        # chat_turns row with an estimate (sum of attachment vision tokens
        # + len(text)//4). The SDK's TurnDoneEvent.tokens_in already
        # includes those user-input tokens. Leaving the pre-count in place
        # would double-count input on the next daily-cap query. Clear it
        # here so the cap sum reflects only the SDK-reported truth on
        # the assistant row.
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None:
            user_turn.tokens_in = None
```

3c. Update the call site in `process_turn_once` to pass `user_turn_id`:

```python
    if sdk_error is None and other_error is None:
        _finalize_success(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            submitted_by_user_id=submitted_by_user_id,
            user_turn_id=user_turn_id,  # ← new
            last_assistant_id=last_assistant_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            session_id=session_id,
        )
```

(`user_turn_id` is already in scope at line 142 from the RETURNING clause.)

### Step 4: Run test to confirm it passes

`uv run pytest tests/proposal_assist/test_turns_processor.py::test_finalize_success_clears_user_turn_pre_count -v`

### Step 5: Run full proposal_assist suite

`uv run pytest tests/proposal_assist -q`

### Step 6: Commit

```bash
git add tests/proposal_assist/test_turns_processor.py \
        src/call_manager/proposal_assist/turns/processor.py
git commit -m "fix(b7-residuals): clear user-turn pre-count on successful finalize

B7's submit pre-stamps the user chat_turns row with a vision-token
estimate; the SDK's TurnDoneEvent.tokens_in already includes those
user input tokens. Leaving the pre-count in place double-counts
against the daily cap on the next submit. Cap sum should reflect
only SDK-reported truth on the assistant row."
```

---

## Task 3: Dedup MAX_ATTACHMENTS_PER_TURN constant + empty-text validation

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py` — rename `_MAX_ATTACHMENTS_PER_TURN` → `MAX_ATTACHMENTS_PER_TURN` (public); add early validation for empty text + attachments.
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — drop local `_MAX_TURN_ATTACHMENTS = 5`, import from submit module.
- Test: `tests/proposal_assist/test_turns_submit_with_attachments.py` — add `test_submit_empty_text_with_attachments_raises_at_submit_time`.

### Step 1: Write the failing test

Add to `tests/proposal_assist/test_turns_submit_with_attachments.py`:

```python
def test_submit_empty_text_with_attachments_raises_at_submit_time(
    pg_session_factory, _settings_env, tmp_path
):
    """Empty content_text + at least one attachment must surface as
    a clear ValueError at submit time, not as a misleading
    'attachment_resolution_failed: text block missing text' from the
    processor's resolver step."""
    import io
    from PIL import Image

    from call_manager.config import Settings
    from call_manager.db.models import Attachment
    from call_manager.proposal_assist.turns.submit import submit_user_turn
    from tests.proposal_assist.conftest import make_user, make_workspace

    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, owner_id = ws.id, owner.id
        # Seed an attachment with a real on-disk file.
        att_id = uuid.uuid4()
        att_dir = tmp_path / str(wsid) / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (4, 4), color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        (att_dir / f"{att_id}.png").write_bytes(buf.getvalue())
        s.add(
            Attachment(
                id=att_id,
                workspace_id=wsid,
                kind="figure",
                filename="x.png",
                storage_path=f"{att_id}.png",
                mime_type="image/png",
                uploaded_by=owner_id,
                width_px=4,
                height_px=4,
                tokens_estimate=1,
            )
        )
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(ValueError, match="turn must have text or attachments"):
            # Note: empty text + ZERO attachments should also raise.
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="",
                attachment_ids=None,
                submitted_by_user_id=owner_id,
                settings=settings,
                workspaces_dir=tmp_path,
            )

    # Empty text + WITH attachments: also rejected, but with a
    # clearer error than the resolver's downstream "missing text".
    with pg_session_factory() as s:
        with pytest.raises(ValueError, match="content_text required"):
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="",
                attachment_ids=[att_id],
                submitted_by_user_id=owner_id,
                settings=settings,
                workspaces_dir=tmp_path,
            )
```

(`pytest` import + `uuid` may already be present; the test file from T3 will tell you.)

### Step 2: Run test to confirm it fails

`uv run pytest tests/proposal_assist/test_turns_submit_with_attachments.py::test_submit_empty_text_with_attachments_raises_at_submit_time -v`
Expected: FAIL (probably the second `with pytest.raises` block — the empty-text path probably reaches the resolver and raises a different error).

### Step 3: Rename constant + add empty-text validation in submit.py

3a. In `src/call_manager/proposal_assist/turns/submit.py:71`, rename the constant from `_MAX_ATTACHMENTS_PER_TURN` to `MAX_ATTACHMENTS_PER_TURN` (drop the leading underscore — it's now public API). Update all usages within the file (line 220, 222).

3b. At the top of `submit_user_turn`'s body (before the row lock at line ~84), add:

```python
    # Validate inputs early so misuse surfaces with a clear error
    # rather than a downstream "attachment_resolution_failed: text
    # block missing text" from the resolver.
    if not content_text and not attachment_ids:
        raise ValueError("turn must have text or attachments")
    if not content_text and attachment_ids:
        raise ValueError("content_text required when attachments are present")
```

(Keep both checks distinct — they convey different operator intents.)

### Step 4: Import the constant in workspaces.py + drop the local copy

In `src/call_manager/proposal_assist/web/workspaces.py`:
- Remove the line `_MAX_TURN_ATTACHMENTS = 5` (line 484).
- Add `MAX_ATTACHMENTS_PER_TURN` to the existing `from call_manager.proposal_assist.turns.submit import ...` (line 49ish).
- Change the only usage at line 638 from `_MAX_TURN_ATTACHMENTS` to `MAX_ATTACHMENTS_PER_TURN`. Update the error message at line 642 to match.

### Step 5: Update the existing T3 over-cap test if it asserts the old constant name

Run `grep -rn "_MAX_TURN_ATTACHMENTS\|_MAX_ATTACHMENTS_PER_TURN" src/ tests/`. If anything else references the underscored names, update them to the public name.

### Step 6: Run tests

```
uv run pytest tests/proposal_assist/test_turns_submit_with_attachments.py tests/proposal_assist/test_web_workspaces_turns.py -v
uv run pytest tests/proposal_assist -q
```

### Step 7: Commit

```bash
git add tests/proposal_assist/test_turns_submit_with_attachments.py \
        src/call_manager/proposal_assist/turns/submit.py \
        src/call_manager/proposal_assist/web/workspaces.py
git commit -m "fix(b7-residuals): dedup MAX_ATTACHMENTS_PER_TURN + early empty-text validation

Two related cleanups from the post-B7 review:
- One constant, two locations (workspaces.py and submit.py). Drift
  risk. Lift to a public symbol on submit.py and import it.
- Empty content_text with attachments used to fail at the resolver
  with 'attachment_resolution_failed: text block missing text' —
  misleading. Validate at submit_user_turn entry with a clear
  ValueError instead."
```

---

## Task 4: Add the remaining three regression tests + doc note

**Files:**
- Test: `tests/proposal_assist/test_web_workspaces_files.py` — `test_upload_same_image_twice_creates_two_rows`.
- Test: `tests/proposal_assist/test_turns_submit_with_attachments.py` — `test_submit_mixed_valid_and_cross_workspace_ids`.
- Modify: `src/call_manager/proposal_assist/web/files.py` — docstring note that `HTTPException.detail` from this module must never be rendered as raw HTML.

### Step 1: Test — same image uploaded twice creates two rows

Add to `tests/proposal_assist/test_web_workspaces_files.py`:

```python
def test_upload_same_image_twice_creates_two_rows(
    monkeypatch, pg_url, pg_session_factory, tmp_path,
    _isolated_workspaces_dir,  # use the autouse fixture from this file
):
    """Each upload of the same bytes creates a distinct Attachment
    row + a distinct file on disk. The composer can reference both
    in a single turn."""
    # ... (mirror the pattern from test_upload_writes_dimensions —
    # POST the same PNG twice, assert 2 distinct Attachment.id rows
    # AND 2 distinct files on disk under workspaces_dir/<wsid>/attachments/).
```

(Mirror the existing test setup pattern in `test_web_workspaces_files.py`. Use the same small inline PNG bytes.)

### Step 2: Test — mixed valid + cross-workspace IDs raises

Add to `tests/proposal_assist/test_turns_submit_with_attachments.py`:

```python
def test_submit_mixed_valid_and_cross_workspace_ids_raises(
    pg_session_factory, _settings_env, tmp_path
):
    """Mixing valid attachment_ids from THIS workspace with one
    that belongs to ANOTHER workspace must raise — not silently
    drop the cross-workspace one and proceed with the valid ones.
    Defense against partial-success via crafted form payloads."""
    # ... seed two workspaces with attachments; submit on ws_a with
    # [valid_a1, valid_a2, cross_ws_b]; assert ValueError.
```

(Mirror the existing `test_submit_with_cross_workspace_attachment_raises` test setup.)

### Step 3: Doc note in files.py

In `src/call_manager/proposal_assist/web/files.py` at the top of the upload handler (or module docstring), add:

```python
# B7 residuals review: HTTPException.detail from this module's
# validators (e.g. "unrecognised image data", "unsupported image
# format") contains attacker-influenced data (Pillow's exception
# message from arbitrary upload bytes). These are JSON-only
# responses today, so the HTMLResponse XSS vector at workspaces.py
# does NOT apply here. If a future change makes any of these paths
# return HTMLResponse, the detail MUST be escaped (see post_turn's
# error paths for the pattern).
```

### Step 4: Run the new tests + full suite

```
uv run pytest tests/proposal_assist -q
```

### Step 5: Commit

```bash
git add tests/proposal_assist/test_web_workspaces_files.py \
        tests/proposal_assist/test_turns_submit_with_attachments.py \
        src/call_manager/proposal_assist/web/files.py
git commit -m "test(b7-residuals): same-image-twice + mixed cross-ws ids + files.py doc note"
```

---

## Final Verification

- [ ] **Step 1: Full test suite + ruff**

```bash
uv run pytest -q
uv run ruff format src tests
uv run ruff check src tests
```

Expected: 928 baseline + 5 new tests = 933 passed / 1 skipped. Ruff clean.

- [ ] **Step 2: Commit any ruff format changes**

```bash
git add -A
git commit -m "style(b7-residuals): ruff format" || true
```

- [ ] **Step 3: Hand off to controller for merge**

Use `superpowers:finishing-a-development-branch` per the established pattern.
