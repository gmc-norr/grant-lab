# B7 — Attachments + Vision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add image upload + vision input to the proposal-assist drafting agent. Users can drag-drop images into the chat composer or browse a persistent files panel, and the agent receives images as native content blocks in its next turn.

**Architecture:** Filesystem storage under `var/workspaces/<id>/attachments/` (matches B5's git-repo pattern). The existing `Attachment` model (migration 0021) holds metadata; no new table. ChatTurn.content already accepts `list[dict]` — images become `{"type": "image", "attachment_id": "..."}` blocks alongside `{"type": "text", "text": "..."}`. The processor passes the list through; the claude_adapter resolves each `attachment_id` to bytes-on-disk, base64-encodes, and sends via the SDK's `AsyncIterable[dict]` query path with the standard Anthropic content-block shape. Vision tokens are pre-counted (Anthropic's published formula) and passed via `submit_user_turn(tokens_in=...)` — the hook B6-residuals-2 added.

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

### Step 4: Write the failing adapter-vision test

Create `tests/proposal_assist/test_claude_adapter_vision.py`:

```python
"""Claude adapter forwards image content blocks to the SDK via the
AsyncIterable[dict] path. We mock the transport to inspect the payload
shape without hitting Anthropic."""

import base64
import json

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


@pytest.mark.asyncio
async def test_adapter_forwards_image_content_blocks_to_sdk(
    monkeypatch, tmp_path
):
    """Given a user_message with a text block + an image block referencing
    an attachment on disk, the adapter must:
    - read the file bytes
    - base64-encode them
    - emit a single user-message dict with content = [text_block, image_block]
      in Anthropic's standard shape
    """
    # Build a 1x1 PNG on disk
    png_bytes = bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
        "0000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
    )
    img_path = tmp_path / "tiny.png"
    img_path.write_bytes(png_bytes)

    adapter = ClaudeAgentSDKAdapter(
        api_key="sk-fake",
        model="claude-opus-4-7",
        cwd=str(tmp_path),
        attachments_dir=str(tmp_path),  # storage helper resolves from here
    )

    transport = _RecordingTransport()
    # Substitute the adapter's connected client transport with our recorder.
    # The adapter's send_turn builds the AsyncIterable[dict] and calls
    # client.query(prompt=iter). We bypass real SDK connect by stubbing
    # the client and exposing _transport.
    handle = AgentHandle(workspace_id="00000000-0000-0000-0000-000000000001")
    handle.client = _FakeClient(transport)  # see helper below

    content_blocks = [
        {"type": "text", "text": "What is in this figure?"},
        {"type": "image", "attachment_id": "11111111-1111-1111-1111-111111111111"},
    ]
    # Map attachment_id -> on-disk path. The adapter resolves via a callback
    # to avoid coupling to the DB. (See task body for the resolver shape.)
    handle.attachment_resolver = lambda aid: img_path

    async for _ in adapter.send_turn(handle, content_blocks):
        # We don't care about events here — the FakeClient yields none.
        # We only want to inspect what was written to the transport.
        break

    assert len(transport.writes) == 1
    payload = json.loads(transport.writes[0])
    assert payload["type"] == "user"
    assert payload["message"]["role"] == "user"
    content = payload["message"]["content"]
    assert content[0] == {"type": "text", "text": "What is in this figure?"}
    img_block = content[1]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert base64.b64decode(img_block["source"]["data"]) == png_bytes


@pytest.mark.asyncio
async def test_adapter_text_only_still_uses_string_path(monkeypatch, tmp_path):
    """Backwards compat: when send_turn is called with a plain string, the
    adapter still uses the cheap string path (no JSON serialization burden)."""
    adapter = ClaudeAgentSDKAdapter(
        api_key="sk-fake",
        model="claude-opus-4-7",
        cwd=str(tmp_path),
        attachments_dir=str(tmp_path),
    )
    transport = _RecordingTransport()
    handle = AgentHandle(workspace_id="00000000-0000-0000-0000-000000000001")
    handle.client = _FakeClient(transport)

    async for _ in adapter.send_turn(handle, "hello"):
        break

    assert len(transport.writes) == 1
    payload = json.loads(transport.writes[0])
    assert payload["message"]["content"] == "hello"


class _FakeClient:
    def __init__(self, transport):
        self._transport = transport
        self._query = object()  # truthy

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

    async def receive_messages(self):
        return
        yield  # pragma: no cover — make this an async generator
```

The `_FakeClient` mirrors the SDK's actual `query()` behavior (verified in `/.venv/lib/python3.12/site-packages/claude_agent_sdk/client.py:283-310`). The test verifies the adapter constructs the right shape *before* it hits the SDK.

Run: `uv run pytest tests/proposal_assist/test_claude_adapter_vision.py -v`
Expected: FAIL — `attachments_dir` not yet a kwarg, `send_turn` still text-only.

### Step 5: Extend the drafting contract

In `src/call_manager/proposal_assist/drafting/contract.py`, find the `send_turn` abstract method on `DraftingAgent` (or the `AgentHandle.send_turn` signature) and change `user_message: str` to `user_message: str | list[dict[str, Any]]`. Keep the docstring in sync — add a paragraph:

```python
"""
user_message:
    - str: plain text, no attachments. Adapter uses the SDK's
      cheap string-prompt path.
    - list[dict]: Anthropic content blocks. Each block is either
      {"type": "text", "text": ...} or
      {"type": "image", "attachment_id": "<uuid>"}.
      The adapter resolves attachment_id to bytes-on-disk via
      AgentHandle.attachment_resolver and emits a base64-encoded
      image block to the SDK.
"""
```

Also add to `AgentHandle` (dataclass / pydantic — check the existing shape):

```python
attachment_resolver: Callable[[uuid.UUID], Path] | None = None
```

(Type imports: `from collections.abc import Callable`, `from pathlib import Path`, `import uuid`.)

### Step 6: Extend the claude_adapter

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`:

1. Add `attachments_dir: str` to `__init__` (stored on `self._attachments_dir`).
2. Pass it down into `AgentHandle` when launching (read the existing launch code; the handle either picks it up via `self._attachments_dir` or the controller passes it through).
3. Change `send_turn`'s signature to `user_message: str | list[dict]`.
4. When `isinstance(user_message, str)`: keep the existing `await client.query(user_message)` path (cheap).
5. When `isinstance(user_message, list)`: build the content blocks, base64-encoding each image:

```python
async def _build_user_message_dict(
    self,
    handle: AgentHandle,
    blocks: list[dict],
) -> dict:
    out_content = []
    for block in blocks:
        if block["type"] == "text":
            out_content.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            attachment_id = uuid.UUID(block["attachment_id"])
            path = handle.attachment_resolver(attachment_id)
            data = path.read_bytes()
            media_type = _mime_for_suffix(path.suffix)
            out_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        else:
            # Defensive: unknown block kinds are silently dropped, with
            # a log line so a future block type doesn't silently fail.
            logger.warning("dropping unknown content-block type: %r", block.get("type"))
    return {
        "type": "user",
        "message": {"role": "user", "content": out_content},
        "parent_tool_use_id": None,
    }


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _mime_for_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix not in _MIME_BY_SUFFIX:
        raise ValueError(f"unsupported image suffix: {suffix}")
    return _MIME_BY_SUFFIX[suffix]
```

Then in `send_turn`:

```python
if isinstance(user_message, list):
    msg = await self._build_user_message_dict(handle, user_message)

    async def _gen():
        yield msg

    await handle.client.query(_gen())
else:
    await handle.client.query(user_message)
```

(Imports: `import base64`, `import logging`, `logger = logging.getLogger(__name__)`.)

### Step 7: Extend the fake_adapter

In `src/call_manager/proposal_assist/drafting/fake_adapter.py`, update `send_turn` to accept the same `str | list[dict]` shape. Record the last user_message on `self.last_user_message` so tests can assert what was passed in (for the integration test in T6).

### Step 8: Extend the processor

In `src/call_manager/proposal_assist/turns/processor.py`, replace `_content_to_text` with `_content_to_sdk_message`:

```python
def _content_to_sdk_message(content: Any) -> str | list[dict[str, Any]]:
    """Convert ChatTurn.content to the form the adapter wants.

    - str → str (cheap path).
    - dict with type=text → str.
    - list of blocks → list[dict] passing only {type: text|image} blocks
      through. Each image block carries its attachment_id verbatim.
    - anything else → empty string (treat as 'no content').
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return ""
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                out.append({"type": "text", "text": str(b["text"])})
            elif b.get("type") == "image" and b.get("attachment_id"):
                out.append(
                    {"type": "image", "attachment_id": str(b["attachment_id"])}
                )
        # If after filtering nothing remains, return empty string —
        # the adapter will then take the cheap path.
        if not out:
            return ""
        # If the list is one text block, collapse to str for the cheap path.
        if len(out) == 1 and out[0]["type"] == "text":
            return out[0]["text"]
        return out
    return ""
```

Update the call site in `process_turn_once` (the line that does `user_text = _content_to_text(user_turn.content)`):

```python
user_message = _content_to_sdk_message(user_turn.content)
# ... pass user_message through to controller.send_turn(workspace_id, user_message)
```

(The signature of `controller.send_turn` will need a follow-on type update — it already passes through to the adapter, so the str|list[dict] type widens cleanly.)

### Step 9: Update existing tests that call _content_to_text

Run `grep -rn "_content_to_text" tests/ src/` and update any references. There's at least one in processor's own tests (`tests/proposal_assist/test_turns_processor.py`); change the import + the symbol if used directly.

### Step 10: Run the vision tests

Run: `uv run pytest tests/proposal_assist/test_claude_adapter_vision.py tests/proposal_assist/attachments/test_tokens.py -v`
Expected: 7 passed.

### Step 11: Run the full proposal_assist suite

Run: `uv run pytest tests/proposal_assist -q`
Expected: all previously-passing tests still pass; new tests from this task pass.

### Step 12: Commit

```bash
git add src/call_manager/proposal_assist/attachments \
        src/call_manager/proposal_assist/drafting/contract.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        src/call_manager/proposal_assist/drafting/fake_adapter.py \
        src/call_manager/proposal_assist/turns/processor.py \
        tests/proposal_assist/attachments \
        tests/proposal_assist/test_claude_adapter_vision.py \
        tests/proposal_assist/test_turns_processor.py
git commit -m "feat(b7): adapter + processor handle image content blocks

Claude SDK adapter now accepts list[dict] content blocks for user
messages; resolves attachment_id to base64-encoded image bytes and
sends via the SDK's AsyncIterable[dict] query path. Processor
preserves content blocks through to the adapter. Pure tokens.py
module estimates Anthropic vision-token cost from image dimensions
(used by T3 to pre-populate ChatTurn.tokens_in).
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


def test_write_creates_directories_and_persists_bytes(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    storage.write(
        workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"hello"
    )
    p = tmp_path / str(wsid) / "attachments" / f"{aid}.png"
    assert p.exists()
    assert p.read_bytes() == b"hello"


def test_delete_workspace_removes_directory(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    storage.write(
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

    def write(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
        data: bytes,
    ) -> Path:
        p = self.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def delete_workspace(self, *, workspace_id: uuid.UUID) -> None:
        """Idempotent: remove the workspace's attachments/ dir if it exists."""
        target = self.root / str(workspace_id) / "attachments"
        if target.exists():
            shutil.rmtree(target)
```

Run: 5 passed.

### Step 3: Write the file-panel test

`tests/proposal_assist/test_web_workspaces_files.py` — cover:

- `GET /workspaces/<id>/files` renders the panel for an owner; returns 403 for a stranger.
- `POST /workspaces/<id>/files` accepts a multipart PNG, validates size + MIME, writes to disk, inserts an Attachment row.
- `POST /workspaces/<id>/files` rejects oversize (>5 MB) with 400.
- `POST /workspaces/<id>/files` rejects an unsupported MIME (e.g., text/plain) with 400.
- `GET /workspaces/<id>/files/<attachment_id>` streams the bytes back to an authorized requester.
- `GET /workspaces/<id>/files/<attachment_id>` returns 403 to a stranger.
- `POST /workspaces/<id>/files/<attachment_id>/delete` removes the row + the file from disk for an owner; 403 for stranger.

Use the same fixture pattern as `test_web_workspaces_cancel.py` (authed client via session signer). Reference small inline PNG bytes (the test from T1 has a 1×1 PNG hex string you can reuse).

### Step 4: Implement web/files.py

Routes:

```python
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
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
def list_files(workspace_id, request, user, session): ...

@router.post("/{workspace_id}/files")
async def upload_file(workspace_id, request, user, session, file: UploadFile):
    # 1. _require_owner
    # 2. Read up to _MAX_BYTES + 1; if more, reject with 400.
    # 3. Validate MIME via the upload's content_type (defense layer 1) and
    #    via PIL/Pillow detection (defense layer 2 — never trust the client).
    # 4. Generate attachment uuid; insert Attachment row (kind='figure',
    #    storage_path = relative path) with rowcount-safe commit.
    # 5. Write bytes via AttachmentStorage.
    # 6. Return the file_row.html partial for HTMX swap-in.

@router.get("/{workspace_id}/files/{attachment_id}")
def serve_file(workspace_id, attachment_id, request, user, session):
    # 1. _require_owner_or_viewer (B6 read-only viewers also see files).
    # 2. Look up Attachment, confirm workspace_id matches.
    # 3. Stream bytes back with the right Content-Type header.
    # 4. Add `Cache-Control: private, max-age=300` (per-user cache; private
    #    so a shared proxy can't leak).

@router.post("/{workspace_id}/files/{attachment_id}/delete")
def delete_file(workspace_id, attachment_id, request, user, session):
    # _require_owner (viewers cannot delete).
    # Delete file from disk first (storage.delete_attachment), then DELETE row.
    # Return 303 to /workspaces/<id>/files.
```

**Security notes for the implementer:**
- Validate the suffix from the MIME (`_SUFFIX_BY_MIME[content_type]`); NEVER trust the upload's filename.
- Read the upload in chunks up to `_MAX_BYTES + 1`. If you exceed, abort and reject — don't `read()` all at once, that's a memory DoS vector.
- The serve route must return the raw bytes with `Content-Disposition: inline; filename="<sanitized>"`. Run the filename through the same allowlist sanitizer used for markdown export.

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

Edit the autogen migration to add nullable `width_px: int`, `height_px: int`, `tokens_estimate: int` columns on `attachments`. NULLable for backwards compat — existing rows from B5 (none in prod yet but the migration should handle it gracefully). Add a backfill: for any existing row, leave NULL — the runtime treats NULL as "needs probing on demand" and updates the row when first read.

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
   - If `attachment_ids` is None, skip the attachment branch.
   - Otherwise, enforce `len(attachment_ids) <= 5`.
   - Look up all Attachment rows by id WHERE workspace_id = this workspace. If any id doesn't match, raise `ValueError("attachment X does not belong to workspace Y")`.
2. Compute `tokens_in = sum(att.tokens_estimate for att in attachments) + estimate_text_tokens(content_text)`.
   - For text tokens, use a simple `len(content_text) // 4` (matches the compaction heuristic in `turns/compaction.py`). Not exact but consistent with existing accounting.
3. Build content:
   ```python
   if attachment_ids:
       content = [{"type": "text", "text": content_text}]
       for aid in attachment_ids:
           content.append({"type": "image", "attachment_id": str(aid)})
   else:
       content = {"type": "text", "text": content_text}
   ```
4. Pass `tokens_in` to the existing ChatTurn insert (already supported per B6-residuals-2).

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
1. `_require_owner_or_viewer` (existing) — viewers can submit turns? Re-check the B6 design — likely yes, viewers can chat. Confirm from `permissions.py`. (NOTE for the implementer: if viewers are read-only and CANNOT submit, change to `_require_owner`. Find the existing policy in `workspaces.py:post_turn` and replicate.)
2. For each `UploadFile` in `files`: run the same upload flow as `files.py:upload_file` (validate MIME, size, Pillow-probe, create Attachment row, write to disk). Collect the new attachment IDs.
3. Combine the upload-IDs with the form's `attachment_ids` (cap at 5 total).
4. Call `submit_user_turn(content_text=content, attachment_ids=all_ids, ...)`.
5. On `ValueError` from submit: rollback session, return 400 HTML.
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

### Step 1: Extend the soft-delete test

Extend the existing test (or write a new one): seed an Attachment + write a file via AttachmentStorage; call soft_delete; assert the file is gone AND the workspace dir's `attachments/` subdir is gone. The B5 git repo (workspace dir's `repo/` subdir) should be untouched.

### Step 2: Wire storage cleanup into soft_delete

```python
@router.post("/{workspace_id}/delete")
def soft_delete(...):
    ws = _require_owner(...)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()
    # B7: scrub attachment bytes from disk (compliance — soft-delete
    # means "do not retain user uploads"). DB rows remain so the audit
    # trail is preserved; the bytes are gone.
    storage = AttachmentStorage(root=settings.workspaces_dir)
    storage.delete_workspace(workspace_id=workspace_id)
    return RedirectResponse(url="/workspaces", status_code=303)
```

(Read `settings.workspaces_dir` from `request.app.state.settings.workspaces_dir` — that's how other handlers read settings.)

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
