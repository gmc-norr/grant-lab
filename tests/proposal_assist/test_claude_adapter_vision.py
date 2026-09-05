"""Claude adapter forwards already-resolved image content blocks to the
SDK via the AsyncIterable[dict] path. Attachment resolution is the
processor's job — the adapter just handles transport shape."""

import base64
import json

import pytest

from grant_lab.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)
from grant_lab.proposal_assist.drafting.contract import AgentHandle


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
async def test_adapter_forwards_already_resolved_image_blocks(adapter, handle_with_fake_client):
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
async def test_adapter_text_only_keeps_string_path(adapter, handle_with_fake_client):
    """A plain str user_message uses the cheap SDK string path —
    no JSON-iterable construction."""
    handle, transport = handle_with_fake_client
    async for _ in adapter.send_turn(handle, "hello"):
        break

    assert len(transport.writes) == 1
    payload = json.loads(transport.writes[0])
    assert payload["message"]["content"] == "hello"
