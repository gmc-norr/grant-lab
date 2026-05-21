# B7 — Attachments + Vision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Revisions

**rev2 (2026-05-21, after Codex review of rev1):**
1. **Adapter contract fix:** Task 1 originally assumed `ClaudeAgentSDKAdapter(api_key=..., cwd=..., attachments_dir=...)` and grew public fields on `AgentHandle`. Actual constructor is `model + workspaces_dir`; `AgentHandle` is explicitly opaque (`state: dict[str, Any]`). Rev2 keeps the constructor as-is, leaves `AgentHandle` opaque, and moves attachment resolution to a new `attachments/resolver.py` module called by the processor. Adapter only handles SDK transport shape.
2. **Upload/delete safety:** Rev1 inserted DB row before file write (left a row if write failed) and deleted file before row (left a file if row delete failed). Rev2 uses temp-file + atomic `os.replace` for writes, row-first delete, explicit rollback paths, and tests the failure cases.
3. **NULL `tokens_estimate` handling:** Rev1 contradicted itself ("probe on demand" vs "skip as zero"). Rev2 commits to re-probe-and-persist on submit; missing file → 400. Never defaults to zero (which would let attackers bypass the cap via the T2-T3 migration window).
4. **No silent drops:** Rev1's `_content_to_text` quietly dropped unknown blocks and missing attachments. Rev2's resolver raises `AttachmentResolutionError`; processor catches and emits `cloud_turn_failed` audit. Model never answers a question about an image it didn't see.
5. **Explicit owner-only permissions:** Rev1 said "maybe owner_or_viewer" for turn submission. Rev2 makes it `_require_owner` per the existing "shared-read, single-write" spec; uploads/deletes also owner-only; only `GET /files/<id>` and the panel list allow viewers.

**Goal:** Add image upload + vision input to the proposal-assist drafting agent. Users can drag-drop images into the chat composer or browse a persistent files panel, and the agent receives images as native content blocks in its next turn.

**Architecture:** Filesystem storage under `var/workspaces/<id>/attachments/` (matches B5's git-repo pattern). The existing `Attachment` model (migration 0021) holds metadata; no new table. ChatTurn.content stores `{"type": "image", "attachment_id": "..."}` blocks alongside `{"type": "text", "text": "..."}` — a lightweight reference, not the raw bytes.

**Layering (Codex-revised):** A new `attachments/resolver.py` (called by the processor) handles all attachment lookup → disk read → API-shape conversion. The Claude adapter stays ignorant of the DB and storage layout — its `send_turn` accepts either a plain `str` (cheap path) OR a `list[dict]` of already-API-shape content blocks (`{"type":"image","source":{"type":"base64","media_type":"...","data":"..."}}`) and just forwards to the SDK via the `AsyncIterable[dict]` query path. `AgentHandle` stays opaque; no new public fields. Vision tokens are pre-counted (Anthropic's published formula) at upload time and stored on the Attachment row, then summed at submit time and passed via `submit_user_turn(tokens_in=...)` — the hook B6-residuals-2 added.

**Failure mode (Codex-revised):** Missing attachment row, cross-workspace attachment_id, missing file on disk, or unknown content-block type during processor resolution → fail the turn loudly. The processor catches the resolution error, calls `_finalize_failed(error='attachment_resolution_failed: ...')`, and emits a `cloud_turn_failed` audit event. The user sees an error; the model never sees a turn with a missing image silently dropped (which would let it answer the wrong question).

**v1 Scope:**
- Image formats: PNG, JPEG, WebP, GIF.
- Per image: 5 MB max. Per turn: 5 images max (uploads + references combined).
- Both UX paths: drag-drop on chat composer (new uploads) + persistent files panel (browse/select existing).
- Soft-delete on the workspace removes attachment files from disk.

**Out of scope (defer to B8/B9):**
- PDFs (Claude's PDF support exists but charges per page; needs separate token-counting + UX).
- Text-file attachments (.txt/.md/.docx).
- Configurable size/count via env (hardcoded for now; revisit if users hit it).
- Anthropic Files API (we keep storage local).
- Cross-workspace attachment sharing.
- Vision-token-specific dashboard signal — flagged by review as an Important item, deferred to B8 admin panel.

**Tech Stack:** Python 3.12, FastAPI (multipart via `python-multipart`, already a transitive dep), HTMX 1.x, Pillow (NEW dep — image-dimension probe for token counting), Postgres 16, claude-agent-sdk 0.2.82.

---

## File Structure

**Create:**
- `src/call_manager/proposal_assist/attachments/__init__.py`
- `src/call_manager/proposal_assist/attachments/storage.py` — filesystem write/read/delete helpers, path derivation.
- `src/call_manager/proposal_assist/attachments/tokens.py` — Anthropic's vision-token estimation per image, per their docs.
- `src/call_manager/proposal_assist/web/files.py` — files panel routes (list / upload / delete / serve-bytes).
- `src/call_manager/web/templates/proposal_assist/workspace_files.html` — files panel page.
- `src/call_manager/web/templates/proposal_assist/partials/file_row.html` — single attachment row (re-used by list + upload response).
- `src/call_manager/web/templates/proposal_assist/partials/chat_image.html` — inline image thumbnail in chat history.
- `tests/proposal_assist/attachments/__init__.py`
- `tests/proposal_assist/attachments/test_storage.py`
- `tests/proposal_assist/attachments/test_tokens.py`
- `tests/proposal_assist/test_web_workspaces_files.py` — file panel routes (upload / list / delete / serve).
- `tests/proposal_assist/test_turns_submit_with_attachments.py` — submit with files + cap accounting.
- `tests/proposal_assist/test_claude_adapter_vision.py` — adapter sends image content blocks correctly.

**Modify:**
- `src/call_manager/proposal_assist/turns/submit.py` — accept optional `attachment_ids: list[uuid.UUID]`, validate ownership, build content blocks.
- `src/call_manager/proposal_assist/turns/processor.py` — `_content_to_text` no longer drops non-text blocks; renamed to `_content_to_sdk_blocks` returning `list[dict]`.
- `src/call_manager/proposal_assist/drafting/contract.py` — `send_turn` signature: `user_message: str | list[dict]`.
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — `send_turn` builds AsyncIterable[dict] for the SDK when content blocks present; reads attachment bytes via storage helper.
- `src/call_manager/proposal_assist/drafting/fake_adapter.py` — record content blocks for assertions; keep test surface stable.
- `src/call_manager/proposal_assist/web/workspaces.py` — POST /turns accepts multipart + `attachment_ids[]`; soft_delete hook calls storage helper.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — chat composer: drag-drop zone + multipart form + attachment select.
- `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` — render image blocks inline.
- `src/call_manager/main.py` — include `files` router.
- `pyproject.toml` — add `Pillow` to dependencies.
- `.env.example` — document `B7_MAX_IMAGE_BYTES` / `B7_MAX_IMAGES_PER_TURN` if we end up env-gating (per current decision: NO, hardcoded — but flag if scope grows).

**Out of scope** (per recon's gaps, but tracked for follow-up): Anthropic Files API integration, PDF attachments, text-file attachments.

---

## Task 1: Token counting + adapter content-block plumbing

**Why first:** The riskiest layer is the SDK integration. Prove it works end-to-end with a fake transport before any UX exists. Token counting is a pure function with no deps.

**Files:**
- Create: `src/call_manager/proposal_assist/attachments/__init__.py`
- Create: `src/call_manager/proposal_assist/attachments/tokens.py`
- Create: `tests/proposal_assist/attachments/__init__.py`
- Create: `tests/proposal_assist/attachments/test_tokens.py`
- Create: `tests/proposal_assist/test_claude_adapter_vision.py`
- Modify: `src/call_manager/proposal_assist/drafting/contract.py`
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py`
- Modify: `src/call_manager/proposal_assist/drafting/fake_adapter.py`
- Modify: `src/call_manager/proposal_assist/turns/processor.py`
- Modify: `pyproject.toml`

### Step 1: Add Pillow dependency

Run: `uv add Pillow`

Verify `pyproject.toml` shows the new entry and `uv.lock` is updated. Commit those two files alone before any code change — keeps the dependency bump isolated.

```bash
git add pyproject.toml uv.lock
git commit -m "deps(b7): add Pillow for image-dimension probing"
```

### Step 2: Write the failing token-count test

Create `tests/proposal_assist/attachments/__init__.py` (empty) and `tests/proposal_assist/attachments/test_tokens.py`:

```python
"""Vision-token estimation per Anthropic's published formula:
tokens = (width * height) / 750, with the longest side capped at 1568 px
(images are downscaled before the count). Source:
https://docs.anthropic.com/en/docs/build-with-claude/vision#calculate-image-costs
"""
import io

import pytest
from PIL import Image

from call_manager.proposal_assist.attachments.tokens import (
    estimate_image_tokens,
    estimate_image_tokens_from_bytes,
)


def _make_image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    img = Image.new("RGB", (width, height), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_estimate_small_image():
    # 200 x 300 = 60_000 px / 750 = 80 tokens
    assert estimate_image_tokens(200, 300) == 80


def test_estimate_rounds_up():
    # 100 x 100 = 10_000 px / 750 = 13.33 -> 14
    assert estimate_image_tokens(100, 100) == 14


def test_estimate_caps_long_side_at_1568():
    # 3000 x 2000 -> scaled to 1568 x 1045 -> 1_638_960 / 750 = ~2186 tokens
    tokens = estimate_image_tokens(3000, 2000)
    # Expect ~2186; allow ±5 for integer rounding of the scale.
    assert 2180 <= tokens <= 2200


def test_estimate_from_bytes_png():
    data = _make_image_bytes(640, 480, fmt="PNG")
    tokens = estimate_image_tokens_from_bytes(data)
    # 640 * 480 = 307_200 / 750 = 410 (rounded up)
    assert tokens == 410


def test_estimate_from_bytes_rejects_non_image():
    with pytest.raises(ValueError):
        estimate_image_tokens_from_bytes(b"not an image")
```

Run: `uv run pytest tests/proposal_assist/attachments/test_tokens.py -v`
Expected: import error / FAIL (the module doesn't exist yet).

### Step 3: Implement the token-count module

Create `src/call_manager/proposal_assist/attachments/__init__.py` (empty).

Create `src/call_manager/proposal_assist/attachments/tokens.py`:

```python
"""Vision-token estimation for Claude image inputs.

Anthropic's published formula: tokens ≈ (width * height) / 750, with the
longest side downscaled to 1568 px before the count. We use this to pre-
populate ChatTurn.tokens_in at submit time so the daily-token cap reflects
queued vision cost (closes the residual gap from B6-residuals-2 where
text-only submits left the cap reactive).

Source: https://docs.anthropic.com/en/docs/build-with-claude/vision
"""

from __future__ import annotations

import io
import math

from PIL import Image, UnidentifiedImageError

_MAX_LONG_SIDE = 1568
_TOKENS_PER_PIXEL_DENOMINATOR = 750


def estimate_image_tokens(width: int, height: int) -> int:
    """Estimate input tokens for an image of the given pixel dimensions.

    If the longest side exceeds 1568 px, scale proportionally first
    (Anthropic downscales internally before counting tokens)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid dimensions: {width}x{height}")
    long_side = max(width, height)
    if long_side > _MAX_LONG_SIDE:
        scale = _MAX_LONG_SIDE / long_side
        width = int(width * scale)
        height = int(height * scale)
    return math.ceil((width * height) / _TOKENS_PER_PIXEL_DENOMINATOR)


def estimate_image_tokens_from_bytes(data: bytes) -> int:
    """Probe image dimensions and return the token estimate.

    Raises ValueError if the bytes are not a recognized image format."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except UnidentifiedImageError as e:
        raise ValueError(f"unrecognized image data: {e}") from e
    return estimate_image_tokens(width, height)
```

Run: `uv run pytest tests/proposal_assist/attachments/test_tokens.py -v`
Expected: 5 passed.

### Step 4: Widen the drafting contract

The current adapter contract (`src/call_manager/proposal_assist/drafting/contract.py:127-131`) has `send_turn(self, handle, user_message: str) -> AsyncIterator[StreamEvent]`. We widen `user_message` to `str | list[dict[str, Any]]` and document what the list shape means.

**Important (Codex finding):** `AgentHandle` stays opaque. NO new public fields. NO `attachment_resolver` callback. The adapter knows nothing about the DB or attachment storage — it only handles SDK transport shape. Attachment resolution (DB lookup → disk read → API-shape conversion) happens one layer up in the processor via the new `attachments/resolver.py` module (see Step 7 below).

Edit `contract.py` — change just the `send_turn` line in the Protocol:

```python
async def send_turn(
    self,
    handle: AgentHandle,
    user_message: str | list[dict[str, Any]],
) -> AsyncIterator[StreamEvent]: ...
```

And add a paragraph to the Protocol's class docstring:

```
user_message is either a plain string (cheap text-only path) or a
list of Anthropic content-block dicts (text and/or image with a
base64 source). The dicts are ALREADY in Anthropic API shape —
adapters MUST NOT do attachment-id resolution. That lives in the
attachments/resolver module called by the processor.
```

No `AgentHandle` change. No `imports of Callable` or `Path` here.

### Step 5: Write the failing adapter-vision test

Create `tests/proposal_assist/test_claude_adapter_vision.py`. The adapter takes already-resolved content blocks and just forwards them via the SDK's `AsyncIterable[dict]` query path. We stub the SDK client to record what gets written:

```python
"""Claude adapter forwards already-resolved image content blocks to the
SDK via the AsyncIterable[dict] path. Attachment resolution is the
processor's job — the adapter just handles transport shape."""

import base64
import json
from pathlib import Path

import pytest

from call_manager.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)
from call_manager.proposal_assist.drafting.contract import AgentHandle


class _RecordingTransport:
    """Capture every line the adapter writes to the SDK transport."""

    def __init__(self):
        self.writes: list[str] = []

    async def write(self, line: str) -> None:
        self.writes.append(line)


class _FakeClient:
    """Mirrors ClaudeSDKClient.query() behavior at client.py:283-310 in
    SDK 0.2.82. Routes string prompts through the str path, iterables
    through the iterable path — same as the real SDK."""

    def __init__(self, transport):
        self._transport = transport
        self._query = object()  # truthy so the adapter's connect-check passes

    async def query(self, prompt, session_id="default"):
        if isinstance(prompt, str):
            payload = {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
                "session_id": session_id,
            }
            await self._transport.write(json.dumps(payload))
        else:
            async for msg in prompt:
                if "session_id" not in msg:
                    msg["session_id"] = session_id
                await self._transport.write(json.dumps(msg))

    async def receive_response(self):
        return
        yield  # pragma: no cover — make this an async generator


@pytest.fixture
def adapter(tmp_path):
    return ClaudeAgentSDKAdapter(
        model="claude-opus-4-7",
        workspaces_dir=str(tmp_path),
    )


@pytest.fixture
def handle_with_fake_client(tmp_path):
    transport = _RecordingTransport()
    handle = AgentHandle(
        workspace_id=__import__("uuid").UUID("00000000-0000-0000-0000-000000000001"),
        state={"client": _FakeClient(transport)},
    )
    return handle, transport


@pytest.mark.asyncio
async def test_adapter_forwards_already_resolved_image_blocks(
    adapter, handle_with_fake_client
):
    """When send_turn receives a list[dict] of already-API-shape blocks
    (with base64 sources), it forwards them verbatim via the SDK's
    iterable path — no resolution, no DB lookup, no disk read."""
    handle, transport = handle_with_fake_client
    png_bytes = bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
        "0000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
    )
    content_blocks = [
        {"type": "text", "text": "What is in this figure?"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png_bytes).decode("ascii"),
            },
        },
    ]

    async for _ in adapter.send_turn(handle, content_blocks):
        break

    assert len(transport.writes) == 1
    payload = json.loads(transport.writes[0])
    assert payload["type"] == "user"
    assert payload["message"]["role"] == "user"
    assert payload["message"]["content"] == content_blocks
    assert payload["parent_tool_use_id"] is None


@pytest.mark.asyncio
async def test_adapter_text_only_keeps_string_path(
    adapter, handle_with_fake_client
):
    """A plain str user_message uses the cheap SDK string path —
    no JSON-iterable construction."""
    handle, transport = handle_with_fake_client
    async for _ in adapter.send_turn(handle, "hello"):
        break

    assert len(transport.writes) == 1
    payload = json.loads(transport.writes[0])
    assert payload["message"]["content"] == "hello"
```

Run: `uv run pytest tests/proposal_assist/test_claude_adapter_vision.py -v`
Expected: FAIL — `send_turn` still rejects non-str argument.

### Step 6: Extend the claude_adapter

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`, change just the `send_turn` body (don't touch the constructor — it already takes `model + workspaces_dir`):

```python
async def send_turn(
    self,
    handle: AgentHandle,
    user_message: str | list[dict[str, Any]],
) -> AsyncIterator[StreamEvent]:
    """Stream events for one turn.

    user_message is either:
    - str: plain text — passed directly to SDK's cheap str path.
    - list[dict]: already-API-shape content blocks (text and/or
      image-with-base64-source). Forwarded via the SDK's
      AsyncIterable[dict] query path. Attachment-id resolution is
      the processor's job (attachments/resolver.py); adapter does
      not touch the DB or filesystem.
    """
    client: ClaudeSDKClient = handle.state["client"]
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = self._model

    if isinstance(user_message, list):
        async def _gen():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_message},
                "parent_tool_use_id": None,
            }

        await client.query(_gen())
    else:
        await client.query(user_message)

    # ... rest of method (receive_response loop) unchanged
```

Update the function's type hint at the signature line. The `async for message in client.receive_response()` body below stays as-is — that handles the response stream and is identical for both paths.

### Step 7: Build the resolver module (replaces the old "_content_to_text" rewrite)

Create `src/call_manager/proposal_assist/attachments/resolver.py`. This is the module Codex's review asked us to add — one layer above the adapter, below the processor:

```python
"""Resolve ChatTurn.content into a form the SDK adapter can send.

ChatTurn.content stores image blocks as {"type": "image", "attachment_id": "..."} —
lightweight references. Before a turn runs, the processor calls
resolve_user_content to swap these references for full API-shape image
blocks with base64-encoded bytes, after validating that each attachment
exists, belongs to this workspace, and the file is present on disk.

This module owns ALL attachment-related DB and filesystem access for the
turn-run path. The Claude adapter stays ignorant of both.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Attachment

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class AttachmentResolutionError(RuntimeError):
    """Raised when an attachment_id cannot be resolved — missing row,
    wrong workspace, missing file on disk, or unknown content-block
    type. The processor catches this, marks the turn failed, and
    emits cloud_turn_failed. Never silently dropped."""


def resolve_user_content(
    *,
    session: Session,
    workspaces_dir: Path,
    workspace_id: uuid.UUID,
    content: Any,
) -> str | list[dict[str, Any]]:
    """Convert stored ChatTurn.content to a form the adapter can send.

    - str → str (cheap path).
    - {"type": "text", "text": ...} → str (cheap path).
    - list of blocks → list[dict] with image blocks fully resolved to
      base64 sources. If after resolution only text blocks remain and
      there is exactly one, collapse to str.
    - Anything else → raise AttachmentResolutionError.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        raise AttachmentResolutionError(
            f"unexpected single-block content type: {content.get('type')!r}"
        )
    if not isinstance(content, list):
        raise AttachmentResolutionError(
            f"unexpected content type: {type(content).__name__}"
        )

    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or "type" not in block:
            raise AttachmentResolutionError(f"malformed content block: {block!r}")
        kind = block["type"]
        if kind == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                raise AttachmentResolutionError("text block missing text")
            out.append({"type": "text", "text": text})
        elif kind == "image":
            aid_str = block.get("attachment_id")
            if not isinstance(aid_str, str):
                raise AttachmentResolutionError("image block missing attachment_id")
            try:
                aid = uuid.UUID(aid_str)
            except ValueError as e:
                raise AttachmentResolutionError(
                    f"image block attachment_id not a valid uuid: {aid_str!r}"
                ) from e
            att = session.scalar(select(Attachment).where(Attachment.id == aid))
            if att is None:
                raise AttachmentResolutionError(f"attachment {aid} not found")
            if att.workspace_id != workspace_id:
                raise AttachmentResolutionError(
                    f"attachment {aid} belongs to workspace {att.workspace_id}, "
                    f"not {workspace_id}"
                )
            file_path = workspaces_dir / str(workspace_id) / "attachments" / Path(att.storage_path).name
            if not file_path.exists():
                raise AttachmentResolutionError(
                    f"attachment {aid} file missing on disk at {file_path}"
                )
            suffix = file_path.suffix.lower()
            if suffix not in _MIME_BY_SUFFIX:
                raise AttachmentResolutionError(
                    f"attachment {aid} has unsupported suffix {suffix!r}"
                )
            data = file_path.read_bytes()
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _MIME_BY_SUFFIX[suffix],
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        else:
            raise AttachmentResolutionError(f"unknown content-block type: {kind!r}")

    if not out:
        raise AttachmentResolutionError("content list resolved to zero blocks")
    if len(out) == 1 and out[0]["type"] == "text":
        return out[0]["text"]
    return out
```

### Step 8: Write resolver tests

Create `tests/proposal_assist/attachments/test_resolver.py` covering:

- str input → str output (passthrough).
- single text dict → str.
- list with text + image (with valid Attachment row + file on disk) → list with API-shape image block (base64 data round-trips).
- list with image referencing a non-existent attachment → raises `AttachmentResolutionError`.
- list with image referencing an attachment in a DIFFERENT workspace → raises.
- list with image whose Attachment row exists but file is missing on disk → raises.
- list with unknown block type → raises.
- list that collapses to a single text block → returns str (cheap path).

Use real Postgres via `pg_session_factory`; write a small PNG to `tmp_path/<wsid>/attachments/<aid>.png` and seed an Attachment row with `storage_path` pointing at it. The path-derivation in resolver uses `Path(att.storage_path).name` so the row's storage_path can be relative or absolute — the test confirms relative paths work.

### Step 9: Wire the resolver into the processor

In `src/call_manager/proposal_assist/turns/processor.py`, replace the existing `_content_to_text` usage:

```python
from call_manager.proposal_assist.attachments.resolver import (
    AttachmentResolutionError,
    resolve_user_content,
)

# In process_turn_once, replace:
#     user_text = _content_to_text(user_turn.content)
# with:
try:
    with session_factory() as s:
        user_message = resolve_user_content(
            session=s,
            workspaces_dir=Path(settings.workspaces_dir),
            workspace_id=workspace_id,
            content=user_turn.content,
        )
except AttachmentResolutionError as e:
    logger.warning(
        "process_turn_once: attachment resolution failed for turn_job %s: %s",
        turn_job_id, e,
    )
    _finalize_failed(
        session_factory,
        turn_job_id=turn_job_id,
        workspace_id=workspace_id,
        submitted_by_user_id=submitted_by_user_id,
        error=f"attachment_resolution_failed: {e}",
        tokens_in=0,
        tokens_out=0,
    )
    return
```

The processor needs a `settings` argument (currently it relies on a controller-passed one — check the actual call site in `scheduler.py` or `controller.send_turn`; the `settings.workspaces_dir` access pattern matches other proposal_assist code). If the existing function signature doesn't carry `settings`, add it as a kwarg with a default `settings: Settings | None = None` and raise if None when content is non-str (since text-only path doesn't need workspaces_dir).

Delete the old `_content_to_text` function — it's no longer used.

### Step 10: Update fake_adapter for content blocks

In `src/call_manager/proposal_assist/drafting/fake_adapter.py`, widen `send_turn`'s `user_message` type to `str | list[dict]`. Record `last_user_message` for test assertion (used by Task 6's e2e test).

### Step 11: Sweep existing tests for _content_to_text references

Run `grep -rn "_content_to_text" tests/ src/`. Update any test that imported or called the old function. Most will just need to change the import to `resolve_user_content`, but a few might assert behavior that no longer applies — those tests should now exercise the resolver directly.

### Step 12: Run the tests

```bash
uv run pytest tests/proposal_assist/attachments tests/proposal_assist/test_claude_adapter_vision.py tests/proposal_assist/test_turns_processor.py -v
```
Expected: all pass.

### Step 13: Run the full proposal_assist suite

Run: `uv run pytest tests/proposal_assist -q`
Expected: no regressions.

### Step 14: Commit

```bash
git add src/call_manager/proposal_assist/attachments \
        src/call_manager/proposal_assist/drafting/contract.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        src/call_manager/proposal_assist/drafting/fake_adapter.py \
        src/call_manager/proposal_assist/turns/processor.py \
        tests/proposal_assist/attachments \
        tests/proposal_assist/test_claude_adapter_vision.py \
        tests/proposal_assist/test_turns_processor.py
git commit -m "feat(b7): resolver + adapter + processor handle image blocks

Per Codex review, attachment resolution lives in a new
attachments/resolver.py module called by the processor —
adapter stays ignorant of the DB and filesystem. send_turn's
user_message widens to str | list[dict] of already-API-shape
blocks. Failures (missing attachment, wrong workspace, missing
file, unknown block type) raise AttachmentResolutionError; the
processor catches and finalizes the turn as failed with a
cloud_turn_failed audit event. No silent drops.

Also: pure tokens.py module estimates Anthropic vision-token cost
from image dimensions (used by T3 to pre-populate ChatTurn.tokens_in).
"
```

---

## Task 2: Storage helper + files panel + serve route

**Files:**
- Create: `src/call_manager/proposal_assist/attachments/storage.py`
- Create: `src/call_manager/proposal_assist/web/files.py`
- Create: `src/call_manager/web/templates/proposal_assist/workspace_files.html`
- Create: `src/call_manager/web/templates/proposal_assist/partials/file_row.html`
- Create: `tests/proposal_assist/attachments/test_storage.py`
- Create: `tests/proposal_assist/test_web_workspaces_files.py`
- Modify: `src/call_manager/main.py` (include router)
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (link to files panel from main view)

### Step 1: Write the storage test

`tests/proposal_assist/attachments/test_storage.py`:

```python
"""Storage helper: write attachment bytes under
var/workspaces/<workspace_uuid>/attachments/<attachment_uuid>.<ext>
and resolve the path back from an attachment_id."""

import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.attachments.storage import (
    AttachmentStorage,
    attachment_path,
)


def test_attachment_path_shape(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    p = storage.path_for(workspace_id=wsid, attachment_id=aid, suffix=".png")
    assert p == tmp_path / str(wsid) / "attachments" / f"{aid}.png"


def test_write_atomic_creates_directories_and_persists_bytes(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    storage.write_atomic(
        workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"hello"
    )
    p = tmp_path / str(wsid) / "attachments" / f"{aid}.png"
    assert p.exists()
    assert p.read_bytes() == b"hello"
    # No leftover .partial file in the directory.
    assert not any(child.suffix == ".partial" for child in p.parent.iterdir())


def test_write_atomic_cleans_partial_on_failure(tmp_path, monkeypatch):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()

    # Force os.replace to fail to simulate a mid-write failure.
    import os

    original_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        storage.write_atomic(
            workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"x"
        )
    # Neither the .partial nor the final file remain.
    target_dir = tmp_path / str(wsid) / "attachments"
    if target_dir.exists():
        assert list(target_dir.iterdir()) == []
    monkeypatch.setattr(os, "replace", original_replace)


def test_delete_one_is_idempotent(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    storage.write_atomic(
        workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"x"
    )
    storage.delete_one(workspace_id=wsid, attachment_id=aid, suffix=".png")
    assert not (tmp_path / str(wsid) / "attachments" / f"{aid}.png").exists()
    # Calling again must not raise.
    storage.delete_one(workspace_id=wsid, attachment_id=aid, suffix=".png")


def test_delete_workspace_removes_directory(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    storage.write_atomic(
        workspace_id=wsid, attachment_id=uuid.uuid4(), suffix=".png", data=b"x"
    )
    assert (tmp_path / str(wsid)).exists()
    storage.delete_workspace(workspace_id=wsid)
    # Only the attachments subdir is removed; the workspace dir may
    # contain git repo (B5). Verify only attachments is gone.
    assert not (tmp_path / str(wsid) / "attachments").exists()


def test_delete_workspace_is_idempotent(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    # Never wrote anything for this workspace.
    storage.delete_workspace(workspace_id=wsid)  # MUST NOT raise


def test_path_for_rejects_traversal_in_suffix(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    with pytest.raises(ValueError):
        storage.path_for(
            workspace_id=uuid.uuid4(),
            attachment_id=uuid.uuid4(),
            suffix="../../etc/passwd",
        )
```

Run: FAIL.

### Step 2: Implement storage.py

```python
"""Filesystem layout for workspace attachments.

Each workspace's attachments live under
<workspaces_dir>/<workspace_uuid>/attachments/<attachment_uuid>.<ext>.

The parent workspace dir already exists from B5 (git repos live under
<workspaces_dir>/<workspace_uuid>/repo/). We only manage the
`attachments/` subdir."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

_ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def attachment_path(
    *,
    root: Path,
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    suffix: str,
) -> Path:
    suffix = suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError(f"disallowed suffix: {suffix!r}")
    return root / str(workspace_id) / "attachments" / f"{attachment_id}{suffix}"


class AttachmentStorage:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
    ) -> Path:
        return attachment_path(
            root=self.root,
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )

    def write_atomic(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
        data: bytes,
    ) -> Path:
        """Write bytes to a `.partial` temp file alongside the target,
        fsync, then `os.replace` to the final path. POSIX guarantees
        the rename is atomic on the same filesystem, so concurrent
        readers see either no file or the complete file — never a
        partial write.

        Caller's responsibility: if the surrounding DB transaction
        rolls back AFTER this returns, call delete_one() to clean up
        the orphan file. The reverse (DB commits, write fails) cannot
        happen because we write the file BEFORE the DB row is
        inserted in the upload handler."""
        import os

        final = self.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(final.suffix + ".partial")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        return final

    def delete_one(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
    ) -> None:
        """Idempotent: remove a single attachment file if it exists.
        Caller (delete handler / upload-rollback) commits the DB
        change FIRST, then calls this. If unlink fails after commit
        the file is orphaned — log + sweep later, the DB row is
        already gone so no broken-link from app code."""
        p = self.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
        if p.exists():
            p.unlink()

    def delete_workspace(self, *, workspace_id: uuid.UUID) -> None:
        """Idempotent: remove the workspace's attachments/ dir if it exists."""
        target = self.root / str(workspace_id) / "attachments"
        if target.exists():
            shutil.rmtree(target)
```

Run: 5 passed.

### Step 3: Write the file-panel test

`tests/proposal_assist/test_web_workspaces_files.py` — cover:

- `GET /workspaces/<id>/files` renders the panel for an **owner OR viewer** (viewers see existing images); returns 403 for a stranger.
- `POST /workspaces/<id>/files` accepts a multipart PNG, validates size + MIME, writes to disk, inserts an Attachment row. **Owner only — viewers get 403.**
- `POST /workspaces/<id>/files` rejects oversize (>5 MB) with 400 — and asserts NEITHER a row nor a file remains on disk after the rejection.
- `POST /workspaces/<id>/files` rejects an unsupported MIME (e.g., text/plain) with 400 — same NO-leak assertion.
- `POST /workspaces/<id>/files` simulates a DB commit failure after the file is on disk (monkeypatch `session.commit` to raise) — asserts the file is NOT left orphaned on disk.
- `GET /workspaces/<id>/files/<attachment_id>` streams the bytes to an **owner OR viewer**; 403 for stranger.
- `GET /workspaces/<id>/files/<attachment_id>` returns 404 if the attachment row belongs to a different workspace (no IDOR via URL).
- `POST /workspaces/<id>/files/<attachment_id>/delete` removes the row first, then the file — assert the row is gone AND the file is gone. **Owner only — viewers get 403.**
- `POST /workspaces/<id>/files/<attachment_id>/delete` is idempotent (calling twice does not raise).

Use the same fixture pattern as `test_web_workspaces_cancel.py` (authed client via session signer). Reference small inline PNG bytes (the test from T1 has a 1×1 PNG hex string you can reuse).

### Step 4: Implement web/files.py

Permission policy (Codex finding made explicit):
- `GET /files` (list) + `GET /files/<id>` (serve bytes): `_require_owner_or_viewer` — viewers need to see chat images they receive.
- `POST /files` (upload), `POST /files/<id>/delete`: `_require_owner` — "shared-read, single-write" per the existing workspace permission spec.

Routes:

```python
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter(prefix="/workspaces", tags=["files"])

_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@router.get("/{workspace_id}/files", response_class=HTMLResponse)
def list_files(workspace_id, request, user, session):
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    # ... render panel with all Attachment rows for this workspace ...


@router.post("/{workspace_id}/files")
async def upload_file(
    workspace_id, request, user, session, file: UploadFile = File(...)
):
    """Order (Codex-revised, atomic):
      1. _require_owner.
      2. Validate content_type is in _ALLOWED_MIMES (defense layer 1).
      3. Stream-read up to _MAX_BYTES + 1; if exceeded, reject with 400
         and short-circuit BEFORE any disk write.
      4. PIL/Pillow probe — confirms MIME server-side (defense layer 2,
         "never trust the client") AND extracts (width, height).
      5. Compute tokens_estimate = estimate_image_tokens(width, height).
      6. Generate attachment uuid + derive suffix from MIME (NOT filename).
      7. storage.write_atomic(...) — fsync + os.replace.
      8. INSERT Attachment row (kind='figure', filename=sanitized client name,
         storage_path=str(final), mime_type, width_px, height_px,
         tokens_estimate, uploaded_by=user.id).
      9. session.commit().

    Rollback path:
      - If step 3-7 raises: file isn't on disk (write_atomic cleans its
        own .partial). No DB row. Return 400 with the validation error.
      - If step 8-9 raises: storage.delete_one(...) to remove the orphan
        file we just wrote. Re-raise / return 500.

    Returns the file_row.html partial for HTMX swap-in."""

@router.get("/{workspace_id}/files/{attachment_id}")
def serve_file(workspace_id, attachment_id, request, user, session):
    """1. _require_owner_or_viewer (viewers see chat images).
       2. SELECT Attachment WHERE id=attachment_id; 404 if not found OR
          workspace_id mismatch (prevents IDOR via URL guessing).
       3. Resolve path via storage.path_for(...); 404 if file missing on disk.
       4. Stream bytes with Content-Type from att.mime_type.
       5. Cache-Control: private, max-age=300 (per-user, blocks proxy leak).
       6. Content-Disposition: inline; filename="<sanitized client name>"
          (sanitizer from b6 markdown export — reject .. + non-alnum)."""


@router.post("/{workspace_id}/files/{attachment_id}/delete")
def delete_file(workspace_id, attachment_id, request, user, session):
    """Order (Codex-revised):
       1. _require_owner.
       2. SELECT Attachment WHERE id=attachment_id AND workspace_id=ws.id
          (idempotent: 200 + empty response if already gone).
       3. session.delete(att); session.commit() — row removed FIRST so the
          source of truth is gone before any FS state.
       4. storage.delete_one(...) — idempotent unlink. If unlink fails
          (e.g., file already gone, or FS error), log a warning. The
          orphan file is harmless because no DB row points at it; a
          future sweep job (B8) can collect orphans.
       5. Return 303 to /workspaces/<id>/files."""
```

**Security notes for the implementer:**
- Validate the suffix from the MIME (`_SUFFIX_BY_MIME[content_type]`); NEVER derive it from the upload's filename.
- Read the upload in chunks up to `_MAX_BYTES + 1`. If you exceed, abort and reject — don't `read()` all at once, that's a memory DoS vector.
- The serve route's `Content-Disposition` filename must go through the same allowlist sanitizer used for markdown export.
- Reject path-traversal in any filename use (`..` runs, slashes) defense-in-depth.

### Step 5: Wire files router in main.py

Add `from call_manager.proposal_assist.web.files import router as files_router`
and `app.include_router(files_router)`. Match the position of the existing `workspaces_router` include.

### Step 6: Add a "Files" link on the workspace main view

`src/call_manager/web/templates/proposal_assist/workspace_main.html`: add a link in the side nav (next to "Settings"):

```html
<a href="/workspaces/{{ workspace.id }}/files">Files</a>
```

### Step 7: Build the files panel template

`workspace_files.html` extends the workspace base template and renders:

```html
{% extends "proposal_assist/_workspace_base.html" %}
{% block content %}
<h1>Files for {{ workspace.title }}</h1>
<form method="post" enctype="multipart/form-data"
      hx-post="/workspaces/{{ workspace.id }}/files"
      hx-target="#file-list"
      hx-swap="afterbegin"
      hx-on::after-request="this.reset()">
  <input type="file" name="file" accept="image/png,image/jpeg,image/webp,image/gif" required>
  <button type="submit">Upload</button>
</form>
<div id="file-list">
  {% for att in attachments %}
    {% include "proposal_assist/partials/file_row.html" %}
  {% endfor %}
</div>
{% endblock %}
```

And `partials/file_row.html`:

```html
<div class="file-row" data-attachment-id="{{ att.id }}">
  <img src="/workspaces/{{ att.workspace_id }}/files/{{ att.id }}"
       alt="{{ att.filename|e }}"
       style="max-width: 120px; max-height: 120px;">
  <span class="filename">{{ att.filename|e }}</span>
  <span class="uploaded">{{ att.uploaded_at | display_dt }}</span>
  <form method="post"
        hx-post="/workspaces/{{ att.workspace_id }}/files/{{ att.id }}/delete"
        hx-target="closest .file-row"
        hx-swap="outerHTML">
    <button type="submit">Delete</button>
  </form>
</div>
```

### Step 8: Run the panel tests

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_files.py -v`
Expected: all pass.

### Step 9: Run the full suite

Run: `uv run pytest -q`
Expected: no regressions.

### Step 10: Commit

```bash
git add src/call_manager/proposal_assist/attachments/storage.py \
        src/call_manager/proposal_assist/web/files.py \
        src/call_manager/web/templates/proposal_assist \
        src/call_manager/main.py \
        tests/proposal_assist/attachments/test_storage.py \
        tests/proposal_assist/test_web_workspaces_files.py
git commit -m "feat(b7): file upload + persistent files panel

POST /workspaces/<id>/files accepts multipart PNG/JPEG/WebP/GIF up to
5 MB, validates MIME via content_type AND Pillow probe, stores under
var/workspaces/<id>/attachments/, inserts Attachment row. GET serves
bytes with cache-private headers (auth-checked owner-or-viewer).
Delete removes file + row, owner-only.
"
```

---

## Task 3: Chat composer with multipart submission

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py` — accept `attachment_ids: list[uuid.UUID]`, validate ownership, build content blocks.
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — `POST /workspaces/<id>/turns` accepts multipart with `attachment_ids[]` form field + `files[]` upload field.
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` — multipart form + drag-drop affordance + attachment picker.
- Test: `tests/proposal_assist/test_turns_submit_with_attachments.py`

### Step 1: Write the failing submit-with-attachments test

Cover:
- `submit_user_turn` with `attachment_ids=[uuid]` builds content as `[{type:text,text:...}, {type:image,attachment_id:str(uuid)}]`.
- Token estimate (from the image dimensions stored on the Attachment via Pillow probe at upload — see below for the column add) is summed and passed as `tokens_in`.
- `attachment_id` referencing an attachment from a DIFFERENT workspace raises `ValueError` (and the route returns 400).
- Submitting more than 5 attachments raises `ValueError` (and the route returns 400).

(Use the `make_user`/`make_workspace`/Attachment direct-insert pattern from existing tests.)

### Step 2: Add `width_px`, `height_px`, `tokens_estimate` columns to Attachment

To pre-count tokens at submit time without re-reading bytes for each turn, we need width/height stored once at upload time. Generate an Alembic migration:

```bash
uv run alembic revision -m "b7_attachment_dimensions" --autogenerate
```

Edit the autogen migration to add nullable `width_px: int`, `height_px: int`, `tokens_estimate: int` columns on `attachments`. NULLable for **two narrow reasons:**
1. Migration ordering: T2 panel inserts rows before T3 backfills the upload handler. Rows created in that window have NULL dimensions until probed on submit.
2. Old rows from a prior B5 demo (none in prod) — handle them gracefully.

**Codex-finding fix:** NULL does NOT default to zero in the cap sum. On submit (T3 step 4), encountering an Attachment with NULL `tokens_estimate` triggers a re-probe: read the bytes from disk via `AttachmentStorage`, call `estimate_image_tokens_from_bytes`, UPDATE the row with the result. Persist before counting. If the file is missing on disk (orphan), raise `ValueError(f"attachment {aid} dimensions unknown and file missing")` — submit returns 400. **Never silently treat NULL as zero — that would let attackers bypass the cap by uploading via the T2 window.**

Run `uv run alembic upgrade head` against the test container; verify the columns appear.

### Step 3: Extend the upload handler to populate dimensions

In `web/files.py:upload_file`, after the Pillow MIME probe, also extract `img.size` (= width, height tuple). Compute `tokens_estimate = estimate_image_tokens(width, height)`. Pass all three into the new `Attachment(..., width_px=..., height_px=..., tokens_estimate=...)` constructor.

Update `test_web_workspaces_files.py::test_upload_writes_dimensions` to assert the row carries the three new values.

### Step 4: Extend `submit_user_turn`

In `src/call_manager/proposal_assist/turns/submit.py`, change the signature:

```python
def submit_user_turn(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    content_text: str,
    attachment_ids: list[uuid.UUID] | None = None,
    submitted_by_user_id: int | None,
    settings: Settings,
) -> TurnJob:
```

(Note: the old `content` kwarg goes away in favor of `content_text + attachment_ids`. This is a breaking change to the function signature — update every caller in src/ AND tests/. Run `grep -rn "submit_user_turn(" src/ tests/` first to enumerate.)

Behavior:

1. After the FOR UPDATE lock (existing) and before the cap check:
   - If `attachment_ids` is None or empty, skip the attachment branch.
   - Otherwise, enforce `len(attachment_ids) <= 5`.
   - Look up all Attachment rows by id WHERE workspace_id = this workspace. If any id doesn't match, raise `ValueError("attachment X does not belong to workspace Y")` (handler returns 400).
2. **NULL tokens_estimate re-probe (Codex-finding fix):** for each looked-up Attachment with `tokens_estimate IS NULL`:
   - Resolve disk path via `storage.path_for(workspace_id=..., attachment_id=att.id, suffix=Path(att.storage_path).suffix)`.
   - If the file is missing, raise `ValueError(f"attachment {att.id} dimensions unknown and file missing — re-upload required")`.
   - Otherwise: `data = path.read_bytes(); est = estimate_image_tokens_from_bytes(data)`. UPDATE `att.tokens_estimate`, `att.width_px`, `att.height_px` in the same session. The FOR UPDATE lock on workspaces ensures no concurrent submit can race this.
3. Compute `tokens_in = sum(att.tokens_estimate for att in attachments) + estimate_text_tokens(content_text)`.
   - For text tokens, use `len(content_text) // 4` (matches the compaction heuristic in `turns/compaction.py`). Not exact but consistent with existing accounting.
4. Build content:
   ```python
   if attachment_ids:
       content = [{"type": "text", "text": content_text}]
       for aid in attachment_ids:
           content.append({"type": "image", "attachment_id": str(aid)})
   else:
       content = {"type": "text", "text": content_text}
   ```
5. Pass `tokens_in` to the existing ChatTurn insert (already supported per B6-residuals-2).

### Step 5: Extend the web POST /turns handler

In `src/call_manager/proposal_assist/web/workspaces.py:post_turn`:

```python
@router.post("/{workspace_id}/turns")
async def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    content: str = Form(...),
    attachment_ids: list[uuid.UUID] = Form(default_factory=list),
    files: list[UploadFile] = File(default_factory=list),
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
) -> HTMLResponse:
    ...
```

Steps inside the handler:
1. `_require_owner` — **owner-only, NOT viewer.** Codex-confirmed via `workspaces.py:441` docstring ("Returns 403 for non-owners (shared-read, single-write spec)"). Preserve this exactly.
2. For each `UploadFile` in `files`: run the same upload flow as `files.py:upload_file` (validate MIME, size, Pillow-probe, write atomically via storage, create Attachment row). Collect the new attachment IDs. **Use the same atomic ordering as T2 — write file first via `write_atomic`, insert row, commit; on commit failure delete the file via `storage.delete_one`.** If any upload validation fails (oversize, bad MIME), rollback any prior uploads from THIS request (delete their rows + files) and return 400.
3. Combine the upload-IDs with the form's `attachment_ids` (cap at 5 total — return 400 if over).
4. Call `submit_user_turn(content_text=content, attachment_ids=all_ids, ...)`.
5. On `ValueError` from submit (cross-workspace attachment, NULL tokens with missing file, over-cap): rollback session AND delete any files just-uploaded for this request, return 400 HTML.
6. On success: render the chat partial (existing behavior).

### Step 6: Update the chat composer template

`workspace_main.html`:

```html
<form id="composer" method="post" enctype="multipart/form-data"
      action="/workspaces/{{ workspace.id }}/turns"
      hx-post="/workspaces/{{ workspace.id }}/turns"
      hx-target="#chat-history"
      hx-swap="beforeend"
      hx-encoding="multipart/form-data"
      hx-on::after-request="this.reset()">

  <textarea name="content" rows="3" required></textarea>

  <details>
    <summary>Attach images ({{ workspace_attachments|length }} in workspace)</summary>

    <label>
      Upload new (max 5 MB each, 5 total per turn):
      <input type="file" name="files" multiple
             accept="image/png,image/jpeg,image/webp,image/gif">
    </label>

    {% if workspace_attachments %}
    <fieldset>
      <legend>Or pick from this workspace:</legend>
      {% for att in workspace_attachments %}
      <label class="att-pick">
        <input type="checkbox" name="attachment_ids" value="{{ att.id }}">
        <img src="/workspaces/{{ att.workspace_id }}/files/{{ att.id }}"
             alt="{{ att.filename|e }}"
             style="max-width: 80px; max-height: 80px;">
        <span>{{ att.filename|e }}</span>
      </label>
      {% endfor %}
    </fieldset>
    {% endif %}
  </details>

  <button type="submit">Send</button>
</form>
```

The drag-drop affordance can be added via a tiny inline JS handler that pushes dropped files into the file input. Keep this minimal — a 10-line vanilla script. Document in the template that more sophisticated drag-drop UX is a follow-up.

### Step 7: Pass workspace_attachments to the main view

`web/workspaces.py:workspace_main`: query `Attachment.workspace_id == workspace_id`, pass into the template context.

### Step 8: Run the tests

Run: `uv run pytest tests/proposal_assist/test_turns_submit_with_attachments.py tests/proposal_assist/test_web_workspaces_files.py -v`
Expected: all pass.

### Step 9: Full suite

Run: `uv run pytest -q`
Expected: no regressions; vision tests from T1 still pass.

### Step 10: Commit

```bash
git add ...
git commit -m "feat(b7): chat composer accepts file uploads + attachment refs

POST /workspaces/<id>/turns now accepts multipart with files[] (new
uploads, processed identically to /files endpoint) and
attachment_ids[] (existing workspace files referenced by id, max 5
combined). submit_user_turn builds content blocks and pre-counts
vision tokens via tokens_estimate stored on each Attachment row
(populated at upload time from Pillow's image probe).
"
```

---

## Task 4: Chat history renders image thumbnails

**Files:**
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html`
- Test: `tests/proposal_assist/test_web_workspaces_turns.py` (or extend existing)

### Step 1: Write the rendering test

Assert that when a ChatTurn.content contains an `{"type": "image", "attachment_id": ...}` block, the rendered HTML includes an `<img src="/workspaces/<id>/files/<aid>">` element. Cover both formats — content as list-of-dicts AND a single text dict.

### Step 2: Update the partial

`partials/chat_turn.html` — iterate over content blocks if list, render text and image blocks separately:

```jinja
{% set blocks = chat_turn.content if chat_turn.content is iterable and chat_turn.content is not string else [chat_turn.content] %}
{% for b in blocks %}
  {% if b.type == "text" %}
    <p>{{ b.text|e }}</p>
  {% elif b.type == "image" %}
    <img src="/workspaces/{{ chat_turn.workspace_id }}/files/{{ b.attachment_id }}"
         alt="attached image"
         class="chat-image"
         style="max-width: 300px; max-height: 300px;">
  {% endif %}
{% endfor %}
```

(Watch out for the case where `content` is a single dict — the iterable check should detect it and wrap in a list, but Jinja's `is iterable` test is True for dicts too. Use `mapping` test instead, or normalize in the route handler before render.)

### Step 3: Run tests

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v`

### Step 4: Commit

```bash
git commit -m "feat(b7): chat history renders attached images inline"
```

---

## Task 5: Soft-delete cleans up attachment files

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:soft_delete`
- Test: `tests/proposal_assist/test_web_workspaces_settings.py` (extend the existing soft-delete test)

### Step 1: Extend the soft-delete tests

Extend (or add to) `test_web_workspaces_settings.py` with **three** tests:

1. **Happy path:** seed an Attachment row + write a file via `AttachmentStorage.write_atomic`; call soft_delete; assert the file is gone AND the workspace dir's `attachments/` subdir is gone. The B5 git repo (workspace dir's `repo/` subdir) must be untouched.
2. **Idempotent FS failure:** seed an Attachment + file; monkeypatch `shutil.rmtree` to raise OSError; call soft_delete; assert the DB still records `soft_deleted_at` (commit succeeded) and that the handler logged a warning (use `caplog` for `proposal_assist.web.workspaces` at WARNING). Then unpatch `rmtree`, call soft_delete again — must not raise (idempotent), and the file is now gone.
3. **No-attachments case:** soft-delete a workspace with no attachments; assert no error (cleanup is a no-op).

### Step 2: Wire storage cleanup into soft_delete (DB-first ordering)

```python
@router.post("/{workspace_id}/delete")
def soft_delete(...):
    """Order (Codex-finding fix): DB commit FIRST (so the user-visible
    "deleted" state is durable), THEN best-effort FS cleanup. If the
    FS cleanup fails, the workspace is still soft-deleted; the orphan
    files are harmless (no app code reads them) and a re-invocation
    of this handler OR a future B8 sweep job will collect them."""
    ws = _require_owner(...)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()
    # B7: scrub attachment bytes from disk. DB rows remain (compliance
    # — audit trail preserved); only the bytes are removed.
    storage = AttachmentStorage(root=settings.workspaces_dir)
    try:
        storage.delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete: attachment cleanup failed for workspace %s; "
            "files will be collected by future sweep",
            workspace_id, exc_info=True,
        )
    return RedirectResponse(url="/workspaces", status_code=303)
```

(Read `settings.workspaces_dir` from `request.app.state.settings.workspaces_dir` — that's how other handlers read settings. Add `logger = logging.getLogger(__name__)` if not already present at module top.)

### Step 3: Run tests

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_settings.py -v`

### Step 4: Commit

```bash
git commit -m "feat(b7): soft_delete scrubs attachment bytes from disk

DB rows are preserved for the audit trail; the bytes are removed
to comply with the 'workspace deleted ⇒ user uploads forgotten'
expectation."
```

---

## Task 6: End-to-end test — upload → submit → drain → adapter

**Files:**
- Test: `tests/proposal_assist/test_b7_e2e.py` (new)

### Step 1: Write the E2E test

A single asyncio test that:
1. Authenticates as the workspace owner.
2. POST a PNG to `/workspaces/<id>/files`. Assert 200, Attachment row exists, file is on disk.
3. POST a text+attachment turn to `/workspaces/<id>/turns` referencing the uploaded attachment.
4. Confirm a `cloud_turn_sent` WorkspaceEvent landed with `actor_id == owner.id` and `payload.turn_job_id`.
5. Run `drain_turn_queue_tick` against the fake adapter (`FakeAgentAdapter` with `script_next_turn([AssistantTextEvent(text="ok"), TurnDoneEvent(...)])`).
6. Assert the fake adapter's `last_user_message` is a list of dicts containing both text and image blocks (the image block carries the right attachment_id).
7. Assert a `cloud_turn_completed` event landed.

### Step 2: Run, commit

```bash
uv run pytest tests/proposal_assist/test_b7_e2e.py -v
git add tests/proposal_assist/test_b7_e2e.py
git commit -m "test(b7): e2e upload → submit → drain → fake adapter sees image"
```

---

## Final Verification

- [ ] **Step 1: Full test suite + ruff**

```bash
uv run pytest -q
uv run ruff format src tests
uv run ruff check src tests
git add -A
git commit -m "style(b7): ruff format" || true  # only if format changed anything
```

- [ ] **Step 2: Manual smoke test (UI)**

Start dev server and walk through:
1. Create a workspace (existing flow).
2. Navigate to /workspaces/<id>/files. Upload a small PNG. Verify it appears in the list.
3. Navigate to /workspaces/<id>. Click "Send" with a text message + select the uploaded image from the picker. Verify the chat history shows both the text and the image inline.
4. Open the chat for a second viewer (per B6 viewer-share). Confirm they see the image but cannot upload or delete.
5. Soft-delete the workspace. Verify the file is gone from disk under var/workspaces/<id>/attachments/.

Document the smoke test results in the merge commit message.

- [ ] **Step 3: Handoff to controller for merge**

The controller will use `superpowers:finishing-a-development-branch`.

---

## Risk register

1. **SDK content-block shape may not match Anthropic API exactly.** Mitigation: T1's adapter test uses the real SDK's `query()` parsing logic via a recording transport. If T1 fails, the implementer escalates.
2. **Pillow probe on malformed image bytes could DOS the worker.** Mitigation: read up to MAX_BYTES+1 in chunks; Pillow's `Image.open` lazy-loads metadata only — no full decode until `.size` is accessed, which is dimension-only. Safe.
3. **Filesystem path traversal via filename.** Mitigation: filenames from client are ONLY used in the Content-Disposition header (sanitized). The on-disk path is derived from server-side UUIDs only.
4. **Vision-token estimate drifts from actual billing.** Mitigation: Anthropic's formula is published. If estimates drift, monitor via the `tokens_in` field on ChatTurn (the processor will overwrite with the true count after the turn completes). Document the discrepancy as expected.
5. **Viewer permissions on the /files endpoints.** Make sure GET allows viewers (they need to see chat images), but POST/DELETE are owner-only. Test both paths.

---

## Self-review

**Coverage of design choices:**
- Images-only ✓ (T1 token module, T2 MIME allowlist).
- Both UX paths ✓ (T2 panel + T3 chat composer multipart).
- Filesystem storage ✓ (T2 storage helper, T5 cleanup).
- 5 MB / 5 images limits ✓ (T2 `_MAX_BYTES`, T3 `len(attachment_ids) <= 5`).

**Cross-task type consistency:**
- ChatTurn.content shape: T1 introduces `list[dict]` flow through processor + adapter. T3 writes that shape via submit_user_turn. T4 reads that shape in templates. Consistent.
- Attachment.tokens_estimate: T2 step 3 adds the columns; T3 step 4 reads them. T2 step 1 test references the column — make sure the migration in T3 step 2 lands the schema before T2 step 1 runs.

**Ordering verified:**
- T2 (panel) creates Attachment rows with the existing schema (no dimensions columns yet).
- T3 (composer) adds `width_px / height_px / tokens_estimate` migration and backfills the upload handler to populate them.
- A workspace upgraded mid-deploy would have NULL dimensions on rows created during T2's window before T3's migration ran. T3 step 2's migration must declare these columns NULLable AND the cap-sum logic in T3 step 4 must skip NULL values (treat as zero-token contribution, since we can't re-probe without re-reading bytes). Implementer: write this safety into the cap calc and add a test.
