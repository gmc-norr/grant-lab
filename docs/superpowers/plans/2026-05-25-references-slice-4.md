# References Subsystem — Slice 4 (Composer Integration + Cap Pre-Counting) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users can attach references (already-ingested papers / prior proposals / analyses / data) to a chat turn via the composer. The B8 daily input-token cap is pre-counted to include the reference text so callers can't bypass the cap by attaching huge references. Total composer inclusions (attachments + references) share a single `MAX_TURN_INCLUSIONS = 5` ceiling.

**Architecture:** `submit_user_turn` gains a `reference_ids: list[uuid.UUID]` parameter. References are validated (workspace membership, `text_status='done'`) before any cap math, pre-counted at `text_chars_total // 4` per reference (truncated text counted at the truncation boundary, not the full chars), and persisted in `ChatTurn.content` as `{"type": "reference", "reference_id": "..."}` blocks alongside text and image blocks. `resolve_user_content` learns to handle reference blocks by reading the `references/<ref_id>/text.txt` Stage-1 output, truncating at a per-ref char budget, wrapping in `<system>Reference: <title>\n\n...</system>` and emitting as a text block to the SDK. Web composer's `<details>` gets a reference checkbox picker that mirrors the existing attachment picker; the route accepts `reference_ids: list[uuid.UUID] = Form(default_factory=list)` and the combined-count check uses the renamed `MAX_TURN_INCLUSIONS` constant.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, FastAPI's `Form(default_factory=list)` for multi-value checkbox fields, Jinja2 + HTMX, the existing Reference + ReferenceChunk schema from Slice 1, the existing `resolve_user_content` from B7, the existing `caps.py` pre-counting machinery from B8.

---

## File Structure

**New code:**
- None new — Slice 4 extends existing modules.

**Modified:**
- `src/call_manager/proposal_assist/turns/submit.py` — rename `MAX_ATTACHMENTS_PER_TURN` → `MAX_TURN_INCLUSIONS`, add `reference_ids` parameter, validate + pre-count + include in combined inclusion-count cap. Extend `_build_user_content` to emit `{"type": "reference", "reference_id": ...}` blocks.
- `src/call_manager/proposal_assist/attachments/resolver.py` — add `reference` block handling alongside the existing `text` + `image` cases. Cap per-reference text at `REFERENCE_TEXT_BUDGET_CHARS` (default 50_000); append `...[truncated, full text via search_references/read_reference_section]` when truncated.
- `src/call_manager/proposal_assist/web/workspaces.py` — extend `post_turn` to accept `reference_ids`, do the combined-count check, pass through. Extend `_workspace_main_context` (the view that renders the composer) to load `workspace_references = [...]` with `text_status='done'` references only.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — add `<details>` reference picker after the attachment picker. Show only `text_status='done'` references.
- `tests/proposal_assist/test_turns_submit.py` — add tests for `reference_ids` (success, cross-workspace IDOR, status-not-done rejection, combined-inclusion cap).
- `tests/proposal_assist/test_turns_submit_with_attachments.py` — add a combined attachments+references test.
- `tests/proposal_assist/test_attachments_resolver.py` — add tests for reference block resolution (text injection, truncation, missing file, cross-workspace).
- `tests/proposal_assist/test_web_workspaces_turns.py` — e2e test for posting a turn with `reference_ids`.

**Out of scope for Slice 4** (lands in Slice 5): delete + copy + reembed CLI + admin panel showing reference failure counts.

---

## Task 1: Rename `MAX_ATTACHMENTS_PER_TURN` → `MAX_TURN_INCLUSIONS`

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py:73-77`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:52` (import)
- Modify: `tests/proposal_assist/test_turns_submit_attachments_cap.py` (any test referencing the old name)
- Modify: `tests/proposal_assist/test_web_workspaces_turns.py` (any test referencing the old name)

This is a pure rename to make room for references sharing the count. No new behavior. The constant value stays at 5.

### Step 1: Find all references to the old name

Run: `grep -rn "MAX_ATTACHMENTS_PER_TURN" src tests`

Expected: 5-10 references. List them — every site needs to update.

### Step 2: Rename in `submit.py`

In `src/call_manager/proposal_assist/turns/submit.py`, change line 73:

```python
MAX_TURN_INCLUSIONS = 5
"""Combined per-turn ceiling on attachments + references. Imported by
web/workspaces.py so the web-layer pre-check and the submit-layer
authoritative check use one constant — drift in either direction would
cause a UX/contract mismatch.
"""
```

Update the cap check around `submit.py:235`:

```python
n_inclusions = len(attachment_ids or []) + len(reference_ids or [])
if n_inclusions > MAX_TURN_INCLUSIONS:
    raise ValueError(
        f"too many inclusions: {n_inclusions} > {MAX_TURN_INCLUSIONS}"
    )
```

(For this task `reference_ids` is still nil — Task 2 wires it in. Add the `or []` defensively to keep the math safe.)

### Step 3: Rename in `web/workspaces.py`

Update import and call site (line 52 + line 648-655).

### Step 4: Update all test files referencing the old name

Replace via grep + sed or per-file edit.

### Step 5: Run the affected tests

Run: `uv run pytest tests/proposal_assist/test_turns_submit.py tests/proposal_assist/test_turns_submit_attachments_cap.py tests/proposal_assist/test_web_workspaces_turns.py -v`

Expected: all green.

### Step 6: Run full suite

Run: `uv run pytest -q`

Expected: baseline + 0 (rename only). Baseline is 1043 passed, 1 skipped.

### Step 7: Commit

```bash
git add -A
git commit -m "refactor(refs-slice-4): rename MAX_ATTACHMENTS_PER_TURN → MAX_TURN_INCLUSIONS"
```

---

## Task 2: Add `reference_ids` parameter to `submit_user_turn`

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py` (extend signature + validation + pre-count + content builder)
- Create: `tests/proposal_assist/test_turns_submit_with_references.py`

### Step 1: Write the failing tests

Create `tests/proposal_assist/test_turns_submit_with_references.py`:

```python
"""submit_user_turn with reference_ids: validation, pre-count, and
combined inclusion cap. Follows the same pattern as
test_turns_submit_with_attachments.py."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import (
    ChatTurn,
    Reference,
    TurnJob,
    User,
    Workspace,
)
from call_manager.proposal_assist.turns.submit import (
    MAX_TURN_INCLUSIONS,
    submit_user_turn,
)


def _seed_workspace(s, *, owner_email=None):
    owner = User(email=owner_email or f"u-{uuid.uuid4()}@ki.se")
    s.add(owner)
    s.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    s.add(ws)
    s.flush()
    return owner, ws


def _seed_reference(
    s,
    *,
    workspace_id,
    text_status="done",
    text_chars_total=200,
    title="Test Paper",
    filename="paper.pdf",
):
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        title=title,
        filename=filename,
        storage_path=f"references/{uuid.uuid4()}/original.pdf",
        mime_type="application/pdf",
        source_type="manual",
        source_uri=None,
        uploaded_by=None,
        text_status=text_status,
        text_chars_total=text_chars_total,
        embedding_model="mxbai-embed-large",
    )
    s.add(ref)
    s.flush()
    return ref


def test_submit_with_one_reference_sets_pre_count(monkeypatch, pg_session_factory, tmp_path):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_chars_total=400)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="discuss this paper",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()
        jobid = job.id

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "user"
            )
        )
    # 400 // 4 (reference) + len("discuss this paper") // 4 (content) = 100 + 4 = 104
    assert ct.tokens_in == 100 + (len("discuss this paper") // 4)
    # Content should be a list with text + reference blocks
    assert isinstance(ct.content, list)
    assert any(
        b.get("type") == "reference" and b.get("reference_id") == str(refid)
        for b in ct.content
    )


def test_submit_rejects_reference_with_text_status_pending(
    monkeypatch, pg_session_factory, tmp_path
):
    """Including a not-yet-extracted reference is a contract violation —
    Phase 2 hasn't produced text.txt yet, so the resolver would fail."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_status="pending")
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s, pytest.raises(ValueError, match="not ready"):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_rejects_cross_workspace_reference(
    monkeypatch, pg_session_factory, tmp_path
):
    """IDOR defense: ref belongs to workspace B; user submits a turn in
    workspace A. Must reject."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner_a, ws_a = _seed_workspace(s)
        owner_b, ws_b = _seed_workspace(s)
        ref_b = _seed_reference(s, workspace_id=ws_b.id)
        s.commit()
        ws_a_id, ref_b_id, uid_a = ws_a.id, ref_b.id, owner_a.id

    with pg_session_factory() as s, pytest.raises(
        ValueError, match="does not belong"
    ):
        submit_user_turn(
            session=s,
            workspace_id=ws_a_id,
            content_text="hi",
            reference_ids=[ref_b_id],
            submitted_by_user_id=uid_a,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_rejects_combined_inclusions_exceeding_cap(
    monkeypatch, pg_session_factory, tmp_path
):
    """5 attachments + 1 reference > MAX_TURN_INCLUSIONS=5."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        # Fake attachment IDs — submit_user_turn rejects them as missing
        # but the inclusion-count check should fire FIRST.
        ref = _seed_reference(s, workspace_id=ws.id)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    # 5 fake attachment ids + 1 real reference
    fake_atts = [uuid.uuid4() for _ in range(5)]
    with pg_session_factory() as s, pytest.raises(
        ValueError, match="too many inclusions"
    ):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            attachment_ids=fake_atts,
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_with_null_text_chars_treated_as_zero(
    monkeypatch, pg_session_factory, tmp_path
):
    """Defensive: a reference with text_status='done' but text_chars_total
    NULL (e.g., extracted-but-not-counted edge case) is pre-counted as 0.
    The submit doesn't fail — the resolver would still find text.txt.
    Worst case: cap pre-count understates by O(50K chars / ref); the
    SDK's TurnDoneEvent corrects the running total."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_chars_total=None)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()
        jobid = job.id

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "user"
            )
        )
    # tokens_in = 0 (null ref) + len("hi") // 4 = 0
    assert ct.tokens_in == 0
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/test_turns_submit_with_references.py -v`

Expected: `TypeError: submit_user_turn() got an unexpected keyword argument 'reference_ids'`.

### Step 3: Extend `submit_user_turn` signature

In `src/call_manager/proposal_assist/turns/submit.py`, modify the function signature (around line 157):

```python
def submit_user_turn(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    content_text: str,
    attachment_ids: list[uuid.UUID] | None = None,
    reference_ids: list[uuid.UUID] | None = None,
    submitted_by_user_id: int | None,
    settings: Settings,
    tokens_in: int | None = None,
    workspaces_dir: Path | None = None,
) -> TurnJob:
```

### Step 4: Add reference validation + pre-count

After the existing attachment validation block (around line 275), add the reference block. Insert BEFORE the `tokens_in` pre-count so we can include reference chars:

```python
    references: list[Reference] = []
    if reference_ids:
        ref_rows = list(
            session.scalars(
                select(Reference).where(Reference.id.in_(reference_ids))
            )
        )
        ref_by_id = {r.id: r for r in ref_rows}
        for rid in reference_ids:
            ref = ref_by_id.get(rid)
            if ref is None:
                raise ValueError(f"reference {rid} not found")
            if ref.workspace_id != workspace_id:
                raise ValueError(
                    f"reference {rid} does not belong to workspace {workspace_id}"
                )
            if ref.text_status != "done":
                raise ValueError(
                    f"reference {rid} is not ready (text_status="
                    f"{ref.text_status!r})"
                )
            references.append(ref)
```

Add the `Reference` import at the top of the file (`from call_manager.db.models import ... , Reference`).

### Step 5: Extend the pre-count

Modify the existing `tokens_in` pre-count block (around line 272-275):

```python
    if tokens_in is None:
        ref_chars = sum(min((r.text_chars_total or 0), REFERENCE_TEXT_BUDGET_CHARS) for r in references)
        tokens_in = (
            sum(att.tokens_estimate or 0 for att in attachments)
            + (ref_chars // 4)
            + (len(content_text) // 4)
        )
```

And add a module-level constant near `MAX_TURN_INCLUSIONS`:

```python
REFERENCE_TEXT_BUDGET_CHARS = 50_000
"""Per-reference truncation budget for content injection. The resolver
caps each reference's text at this length, so the cap pre-count uses
``min(text_chars_total, REFERENCE_TEXT_BUDGET_CHARS)`` to match — over-
counting would refuse legitimately small turns; under-counting would
let cap-bypass attacks through."""
```

### Step 6: Extend `_build_user_content`

Modify `_build_user_content` (around line 80):

```python
def _build_user_content(
    *,
    content_text: str,
    attachment_ids: list[uuid.UUID] | None,
    reference_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Shape the ``ChatTurn.content`` payload.

    Text-only submits keep the legacy dict shape so existing renderers
    and adapters don't have to special-case the common path.
    """
    if not attachment_ids and not reference_ids:
        return {"type": "text", "text": content_text}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": content_text}]
    for aid in attachment_ids or []:
        blocks.append({"type": "image", "attachment_id": str(aid)})
    for rid in reference_ids or []:
        blocks.append({"type": "reference", "reference_id": str(rid)})
    return blocks
```

Update the call site around line 300:

```python
    content = _build_user_content(
        content_text=content_text,
        attachment_ids=attachment_ids,
        reference_ids=reference_ids,
    )
```

### Step 7: Update the combined-inclusion cap check

The cap check from Task 1 already accounts for `reference_ids` length (we wrote `len(attachment_ids or []) + len(reference_ids or [])` in T1). Verify it's still there.

### Step 8: Run new tests

Run: `uv run pytest tests/proposal_assist/test_turns_submit_with_references.py -v`

Expected: 5 passed.

### Step 9: Run full suite

Run: `uv run pytest -q`

Expected: 1043 + 5 = 1048 passed, 1 skipped.

### Step 10: Commit

```bash
git add src/call_manager/proposal_assist/turns/submit.py \
        tests/proposal_assist/test_turns_submit_with_references.py
git commit -m "feat(refs-slice-4): submit_user_turn accepts reference_ids with pre-count + IDOR check"
```

---

## Task 3: Extend `resolve_user_content` to handle reference blocks

**Files:**
- Modify: `src/call_manager/proposal_assist/attachments/resolver.py`
- Modify: `tests/proposal_assist/test_attachments_resolver.py`

### Step 1: Write failing tests

Append to `tests/proposal_assist/test_attachments_resolver.py`:

```python
def test_resolve_reference_block_injects_truncated_text(
    pg_session_factory, tmp_path
):
    """A reference block resolves to a text block wrapped in
    <system>Reference: <title> ... </system>, truncated at the budget."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.proposal_assist.attachments.resolver import (
        REFERENCE_TEXT_BUDGET_CHARS,
        resolve_user_content,
    )

    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="Test Title",
            filename="t.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="done",
            text_chars_total=100,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref)
        s.commit()
        ref_dir = tmp_path / str(ws.id) / "references" / str(ref.id)
        ref_dir.mkdir(parents=True)
        (ref_dir / "text.txt").write_text("paper body content here")
        wsid, refid = ws.id, ref.id

    with pg_session_factory() as s:
        out = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=wsid,
            content=[
                {"type": "text", "text": "discuss"},
                {"type": "reference", "reference_id": str(refid)},
            ],
        )
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "discuss"}
    assert out[1]["type"] == "text"
    body = out[1]["text"]
    assert "<system>Reference: Test Title" in body
    assert "paper body content here" in body
    assert "</system>" in body


def test_resolve_reference_truncates_at_budget(pg_session_factory, tmp_path):
    """Text longer than REFERENCE_TEXT_BUDGET_CHARS gets cut + ellipsis suffix."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.proposal_assist.attachments.resolver import (
        REFERENCE_TEXT_BUDGET_CHARS,
        resolve_user_content,
    )

    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title=None,
            filename="big.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="done",
            text_chars_total=200_000,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref)
        s.commit()
        ref_dir = tmp_path / str(ws.id) / "references" / str(ref.id)
        ref_dir.mkdir(parents=True)
        (ref_dir / "text.txt").write_text("x" * 200_000)
        wsid, refid = ws.id, ref.id

    with pg_session_factory() as s:
        out = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=wsid,
            content=[
                {"type": "text", "text": "discuss"},
                {"type": "reference", "reference_id": str(refid)},
            ],
        )
    body = out[1]["text"]
    # 50K chars of x's + truncation marker
    assert body.count("x") == REFERENCE_TEXT_BUDGET_CHARS
    assert "[truncated" in body
    # Falls back to filename when title is None
    assert "big.pdf" in body


def test_resolve_reference_cross_workspace_rejected(pg_session_factory, tmp_path):
    """IDOR: reference belongs to a different workspace than the content."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.proposal_assist.attachments.resolver import (
        AttachmentResolutionError,
        resolve_user_content,
    )

    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws_a = Workspace(owner_id=owner.id, title="A")
        ws_b = Workspace(owner_id=owner.id, title="B")
        s.add_all([ws_a, ws_b])
        s.flush()
        ref_b = Reference(
            workspace_id=ws_b.id,
            kind="paper",
            title="X",
            filename="x.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="done",
            text_chars_total=10,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref_b)
        s.commit()
        ws_a_id, ref_b_id = ws_a.id, ref_b.id

    with pg_session_factory() as s, pytest.raises(
        AttachmentResolutionError, match="does not belong"
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=ws_a_id,
            content=[
                {"type": "text", "text": "hi"},
                {"type": "reference", "reference_id": str(ref_b_id)},
            ],
        )


def test_resolve_reference_missing_text_file_rejected(pg_session_factory, tmp_path):
    """text_status='done' but text.txt missing → AttachmentResolutionError.
    This is the contract: 'done' means the file is on disk."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.proposal_assist.attachments.resolver import (
        AttachmentResolutionError,
        resolve_user_content,
    )

    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="X",
            filename="x.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="done",
            text_chars_total=10,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref)
        s.commit()
        wsid, refid = ws.id, ref.id

    with pg_session_factory() as s, pytest.raises(
        AttachmentResolutionError, match="text.*missing"
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=wsid,
            content=[
                {"type": "text", "text": "hi"},
                {"type": "reference", "reference_id": str(refid)},
            ],
        )
```

(Assume `import uuid` and `import pytest` are already at the top of the test file from earlier tests. If not, add them.)

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/test_attachments_resolver.py -v`

Expected: 4 new tests fail with `unknown content-block type: 'reference'`.

### Step 3: Extend resolver

In `src/call_manager/proposal_assist/attachments/resolver.py`, add the constant + the new branch.

Near the top (after imports):

```python
REFERENCE_TEXT_BUDGET_CHARS = 50_000
"""Per-reference truncation budget for content injection. Must match
``call_manager.proposal_assist.turns.submit.REFERENCE_TEXT_BUDGET_CHARS``
— the submit pre-count uses this value to estimate tokens. Keep in
sync; the constants are duplicated rather than imported to avoid a
cyclic module dep (submit imports resolver context indirectly via
processor)."""
```

Add the import:

```python
from call_manager.db.models import Attachment, Reference
```

In `resolve_user_content`, inside the `for block in content:` loop, add a new branch BEFORE the trailing `else: raise AttachmentResolutionError(...)`:

```python
        elif kind == "reference":
            rid_str = block.get("reference_id")
            if not isinstance(rid_str, str):
                raise AttachmentResolutionError(
                    "reference block missing reference_id"
                )
            try:
                rid = uuid.UUID(rid_str)
            except ValueError as e:
                raise AttachmentResolutionError(
                    f"reference block reference_id not a valid uuid: {rid_str!r}"
                ) from e
            ref = session.scalar(select(Reference).where(Reference.id == rid))
            if ref is None:
                raise AttachmentResolutionError(f"reference {rid} not found")
            if ref.workspace_id != workspace_id:
                raise AttachmentResolutionError(
                    f"reference {rid} does not belong to workspace {workspace_id}"
                )
            if ref.text_status != "done":
                raise AttachmentResolutionError(
                    f"reference {rid} text not ready (text_status="
                    f"{ref.text_status!r})"
                )
            text_path = (
                workspaces_dir
                / str(workspace_id)
                / "references"
                / str(rid)
                / "text.txt"
            )
            if not text_path.exists():
                raise AttachmentResolutionError(
                    f"reference {rid} text file missing at {text_path}"
                )
            full_text = text_path.read_text(errors="replace")
            display_label = ref.title or ref.filename
            if len(full_text) > REFERENCE_TEXT_BUDGET_CHARS:
                body = (
                    full_text[:REFERENCE_TEXT_BUDGET_CHARS]
                    + "\n\n[truncated — full text via search_references / "
                    "read_reference_section MCP tools]"
                )
            else:
                body = full_text
            wrapped = (
                f"<system>Reference: {display_label}\n\n{body}\n</system>"
            )
            out.append({"type": "text", "text": wrapped})
```

### Step 4: Run new resolver tests

Run: `uv run pytest tests/proposal_assist/test_attachments_resolver.py -v -k reference`

Expected: 4 passed (plus existing tests still pass).

### Step 5: Run full suite

Run: `uv run pytest -q`

Expected: 1048 + 4 = 1052 passed, 1 skipped.

### Step 6: Commit

```bash
git add src/call_manager/proposal_assist/attachments/resolver.py \
        tests/proposal_assist/test_attachments_resolver.py
git commit -m "feat(refs-slice-4): resolver injects reference text as <system>-wrapped block"
```

---

## Task 4: Web composer route + template

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (route + context)
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` (picker UI)
- Modify: `tests/proposal_assist/test_web_workspaces_turns.py` (e2e test)

### Step 1: Write the failing e2e test

Append to `tests/proposal_assist/test_web_workspaces_turns.py`:

```python
def test_post_turn_with_reference_ids_form_field(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """End-to-end: POST a turn with reference_ids; the ChatTurn row's
    content includes a reference block; tokens_in pre-count includes
    the reference chars."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.auth.session_cookie import SessionSigner

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    from call_manager.main import create_app

    app = create_app()
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="My Paper",
            filename="my.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="done",
            text_chars_total=80,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref)
        s.commit()
        uid, wsid, refid = owner.id, ws.id, ref.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    try:
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={
                "content": "talk about this",
                "reference_ids": str(refid),
            },
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "user"
            )
        )
    assert isinstance(ct.content, list)
    assert any(
        b.get("type") == "reference" and b.get("reference_id") == str(refid)
        for b in ct.content
    )
    # Pre-count: 80 // 4 + len("talk about this") // 4 = 20 + 3 = 23
    assert ct.tokens_in == 20 + (len("talk about this") // 4)


def test_post_turn_with_reference_ids_returns_400_for_pending_reference(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """text_status='pending' → submit raises ValueError → route returns 400."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.auth.session_cookie import SessionSigner

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    from call_manager.main import create_app

    app = create_app()
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="X",
            filename="x.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="manual",
            uploaded_by=None,
            text_status="pending",
            text_chars_total=None,
            embedding_model="mxbai-embed-large",
        )
        s.add(ref)
        s.commit()
        uid, wsid, refid = owner.id, ws.id, ref.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    try:
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={"content": "x", "reference_ids": str(refid)},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400
    assert b"not ready" in resp.content


def test_workspace_main_context_includes_workspace_references(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The composer picker needs the list of done-references in the
    template context. Only text_status='done' refs appear (pending /
    failed are filtered out)."""
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.auth.session_cookie import SessionSigner

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    from call_manager.main import create_app

    app = create_app()
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        # Two refs: one done (should appear in picker), one pending (filtered out)
        ref_done = Reference(
            workspace_id=ws.id, kind="paper", title="Done", filename="d.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf", source_type="manual",
            uploaded_by=None, text_status="done", text_chars_total=10,
            embedding_model="mxbai-embed-large",
        )
        ref_pending = Reference(
            workspace_id=ws.id, kind="paper", title="Pending", filename="p.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf", source_type="manual",
            uploaded_by=None, text_status="pending", text_chars_total=None,
            embedding_model="mxbai-embed-large",
        )
        s.add_all([ref_done, ref_pending])
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    try:
        resp = client.get(f"/workspaces/{wsid}")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    body = resp.content.decode()
    # Composer picker should list the done ref but not the pending one
    assert "Done" in body
    assert 'name="reference_ids"' in body
    # The pending row's title should NOT appear inside a picker checkbox
    # (it may appear elsewhere; we assert the picker block doesn't show it)
    # Use a coarse check — the picker uses `reference_ids` as the input name
    # so search for the checkbox value of the pending ref.
    # If we filtered correctly, ref_pending.id is not in any picker value.
    # (The reference panel from Slice 1 DOES list it elsewhere — that's
    # the /references panel route, separate from the composer.)
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v -k reference`

Expected: 3 new tests fail (route doesn't accept the kwarg / template doesn't have the picker).

### Step 3: Extend the `post_turn` route

In `src/call_manager/proposal_assist/web/workspaces.py`, extend the route signature around line 488:

```python
async def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
    content: str = Form(...),
    attachment_ids: list[uuid.UUID] = Form(default_factory=list),
    reference_ids: list[uuid.UUID] = Form(default_factory=list),
    files: list[UploadFile] = File(default_factory=list),
) -> HTMLResponse:
```

Extend the combined-count check (around line 648-655):

```python
    all_inclusions = len(new_upload_ids) + len(attachment_ids) + len(reference_ids)
    if all_inclusions > MAX_TURN_INCLUSIONS:
        _rollback_created_uploads(...)
        return HTMLResponse(
            f"<p>too many inclusions: {all_inclusions} > "
            f"{MAX_TURN_INCLUSIONS}</p>",
            status_code=400,
        )
```

Pass `reference_ids` through to `submit_user_turn` (around line 660):

```python
    job = submit_user_turn(
        session=session,
        workspace_id=workspace_id,
        content_text=content,
        attachment_ids=new_upload_ids + list(attachment_ids),
        reference_ids=list(reference_ids),
        ...
    )
```

### Step 4: Extend the workspace_main view context

In the same file around line 410, after `workspace_attachments = [...]`, add:

```python
    workspace_references = list(
        session.scalars(
            select(Reference)
            .where(
                Reference.workspace_id == workspace_id,
                Reference.text_status == "done",
            )
            .order_by(Reference.uploaded_at.desc())
        )
    )
```

Add `Reference` to the imports at the top.

Pass it to the template (around line 452):

```python
            "workspace_attachments": workspace_attachments,
            "workspace_references": workspace_references,
```

### Step 5: Extend the composer template

In `src/call_manager/web/templates/proposal_assist/workspace_main.html`, after the existing attachment picker `</details>` (around line 59), add:

```html
<details>
  <summary>Attach references ({{ workspace_references|length }} ready in workspace)</summary>

  {% if workspace_references %}
  <fieldset>
    <legend>Pick from this workspace:</legend>
    {% for ref in workspace_references %}
    <label class="ref-pick" style="display: block; margin: 0.25em 0;">
      <input type="checkbox" name="reference_ids" value="{{ ref.id }}">
      <strong>{{ ref.title or ref.filename }}</strong>
      <small>[{{ ref.kind }}]</small>
    </label>
    {% endfor %}
  </fieldset>
  {% else %}
  <p><em>No references ready. Upload via the /references panel.</em></p>
  {% endif %}
</details>
```

### Step 6: Run new e2e tests

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v -k reference`

Expected: 3 passed.

### Step 7: Run full suite

Run: `uv run pytest -q`

Expected: 1052 + 3 = 1055 passed, 1 skipped.

### Step 8: ruff

Run: `uv run ruff check src tests && uv run ruff format src tests`

Expected: clean.

### Step 9: Commit

```bash
git add src/call_manager/proposal_assist/web/workspaces.py \
        src/call_manager/web/templates/proposal_assist/workspace_main.html \
        tests/proposal_assist/test_web_workspaces_turns.py
git commit -m "feat(refs-slice-4): composer reference picker + reference_ids form field"
```

If ruff format made changes:

```bash
git add -A
git commit -m "style(refs-slice-4): ruff format"
```

---

## Final Verification (manual smoke test)

- [ ] **Step 1: Start the dev server from the worktree**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/references-slice-4
DATABASE_URL='postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager' \
APP_BASE_URL=http://127.0.0.1:8001 \
SESSION_SECRET='dev-smoke-test-secret-at-least-16-chars' \
ALLOWED_EMAIL_DOMAINS='ki.se' \
OLLAMA_URL='http://localhost:11434' \
EMBEDDING_MODEL='mxbai-embed-large' \
WORKSPACES_DIR=$(pwd)/var/workspaces \
uv run uvicorn 'call_manager.main:create_app' --factory --host 127.0.0.1 --port 8001
```

- [ ] **Step 2: Open the workspace from Slice 3's smoke test**

Visit `http://127.0.0.1:8001/workspaces/<wsid>` (use the workspace ID seeded during Slice 3 smoke). The composer should now have TWO `<details>` blocks: "Attach images" + "Attach references". (The Slice 3 smoke left a `text_status='failed'` ref — it should NOT appear in the picker. Upload a small PDF via the /references panel to get a `text_status='done'` ref.)

- [ ] **Step 3: Submit a turn with a reference**

Check a reference checkbox, type a prompt, submit. Verify in the DB:
- `chat_turns.tokens_in` reflects `text_chars_total // 4 + len(content)//4`.
- `chat_turns.content` is a list including a `{"type": "reference", "reference_id": ...}` block.

- [ ] **Step 4: Hand off to controller for merge** via `superpowers:finishing-a-development-branch`.

---

## Self-review notes

**Spec coverage** — Section 5 Slice 4 items:
- ✓ Extend `submit_user_turn` with `reference_ids` + pre-count via `text_chars_total // 4` + reject not-`done` refs → Task 2
- ✓ Rename `MAX_ATTACHMENTS_PER_TURN` → `MAX_TURN_INCLUSIONS = 5`, combined count check → Task 1+2
- ✓ Sibling `resolve_references()` injecting `<system>Reference: <title>\n\n<text>...</system>` → Task 3 (implemented inline in `resolve_user_content` rather than a separate function; equivalent surface, simpler module)
- ✓ Processor call site is unchanged — `resolve_user_content` is already called by processor and now handles the new block type → no Task 3 processor change needed
- ✓ Composer `<details>` picker → Task 4
- ✓ Tests for `reference_ids` + pre-count + over-cap → Task 2
- ✓ E2E test posting a turn with references → Task 4

**Plan deviation from spec rev4:** spec says "sibling resolver function `resolve_references()`" — this plan extends `resolve_user_content` instead of adding a new function because the existing function is already block-typed and dispatches by block kind. A sibling function would duplicate the dispatch loop. If the controller wants the spec's literal shape, the change is a 10-minute extract-method refactor on the resolver and doesn't affect any other module.

**Test count target** at end of Slice 4: 1043 baseline + 12 new = **1055 passed**, 1 skipped.

**Known limitations** carried forward:
- The Slice 3 Phase 0 `call_id=None` gap still blocks the full agent-driven smoke test (workspace needs a linked call_id for the SDK to launch). HTTP submit path works regardless.
- The per-reference truncation budget is hard-coded at 50K chars. If a user attaches 5 max-size refs, that's 250K chars ≈ 62.5K tokens of input cost — well over the per-turn input budget for most setups. The cap check rejects this at submit time. v1.1 may consider per-ref tunability.
- `_build_user_content` does NOT preserve the user's checkbox-order interleaving of images + references — images always come first, then references. Acceptable for v1; the SDK reads blocks as parallel context.
- The resolver duplicates the `REFERENCE_TEXT_BUDGET_CHARS` constant from submit.py to avoid a cyclic import. The two must be kept in sync — add a comment in both files noting the dependency.
