"""B7 Task 6: end-to-end integration test.

Chain: upload PNG → submit turn with attachment → drain → FakeAgentAdapter.

Proves that the full pipeline works correctly:
  1. POST /workspaces/<id>/files  → Attachment row with dims populated, file on disk.
  2. POST /workspaces/<id>/turns  → ChatTurn with list content, TurnJob queued,
     cloud_turn_sent WorkspaceEvent with actor_id + turn_job_id.
  3. drain_turn_queue_tick        → process_turn_once resolves the attachment_id
     reference into a base64 API-shape block before calling the adapter.
  4. FakeAgentAdapter.last_user_message is a list of dicts in already-API-shape:
     text block + image block with {"type":"image","source":{"type":"base64",...}}.
  5. cloud_turn_completed WorkspaceEvent landed.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from call_manager.db.models import Attachment, TurnJob, WorkspaceEvent
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
from tests.proposal_assist.conftest import make_workspace

# Minimal 1×1 RGBA PNG — same bytes as test_claude_adapter_vision.py.
# Using a known-good PNG so Pillow can probe it for dimensions.
_PNG_1X1_HEX = (
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
    "0000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)
_PNG_BYTES = bytes.fromhex(_PNG_1X1_HEX)


@pytest.fixture(autouse=True)
def _isolated_workspaces_dir(client_authed_owner, tmp_path):
    """Point the live app at a fresh tmp_path so file writes don't leak
    between tests (or escape into the repo's var/workspaces/)."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    yield tmp_path


@pytest.mark.asyncio
async def test_e2e_upload_submit_drain_adapter_sees_resolved_image(
    client_authed_owner,
    pg_session_factory,
    current_user,
    _settings_env,
    tmp_path,
):
    """Full chain: file upload → turn submit → drain tick → fake adapter
    sees the image as a base64 API-shape block (NOT the raw attachment_id
    reference shape stored in ChatTurn.content).

    Step 1: authenticate as workspace owner (client_authed_owner fixture).
    Step 2: POST a 1×1 PNG to /files — assert row + dims + file on disk.
    Step 3: POST a turn referencing the attachment via attachment_ids[].
    Step 4: assert cloud_turn_sent WorkspaceEvent with actor_id + turn_job_id.
    Step 5: drain with FakeAgentAdapter scripted with ok response.
    Step 6: assert adapter.last_user_message is the resolved API shape.
    Step 7: assert cloud_turn_completed WorkspaceEvent landed.
    """
    from call_manager.config import Settings

    settings = Settings()
    # Mirror tmp_path into the in-process settings so the drain path
    # (which calls resolve_user_content) reads from the same directory
    # the upload handler wrote to.
    workspaces_dir = tmp_path

    # ------------------------------------------------------------------ #
    # Step 1: create workspace                                             #
    # ------------------------------------------------------------------ #
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
        owner_id = current_user.id

    # ------------------------------------------------------------------ #
    # Step 2: upload PNG via /files                                        #
    # ------------------------------------------------------------------ #
    up_resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("figure.png", _PNG_BYTES, "image/png")},
    )
    assert up_resp.status_code == 200, up_resp.text

    with pg_session_factory() as s:
        att = s.scalar(select(Attachment).where(Attachment.workspace_id == wsid))
    assert att is not None, "Attachment row must exist after upload"
    aid = att.id
    assert att.tokens_estimate is not None, "tokens_estimate must be populated by upload handler"
    assert att.width_px is not None and att.height_px is not None, (
        "width_px / height_px must be populated by upload handler"
    )
    # 1×1 PNG → dimensions must be 1×1
    assert att.width_px == 1 and att.height_px == 1
    # File on disk at the canonical path.
    file_on_disk = tmp_path / str(wsid) / "attachments" / f"{aid}.png"
    assert file_on_disk.exists(), f"uploaded file must be on disk at {file_on_disk}"
    assert file_on_disk.read_bytes() == _PNG_BYTES

    # ------------------------------------------------------------------ #
    # Step 3: submit turn referencing the attachment                       #
    # ------------------------------------------------------------------ #
    turn_resp = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "What is shown in this figure?", "attachment_ids": str(aid)},
    )
    assert turn_resp.status_code == 200, turn_resp.text

    # ------------------------------------------------------------------ #
    # Step 4: cloud_turn_sent event with actor_id + turn_job_id            #
    # ------------------------------------------------------------------ #
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job is not None, "TurnJob must be queued after turn submit"
        assert job.status == "queued"
        job_id = job.id

        sent_event = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "cloud_turn_sent",
            )
        )
    assert sent_event is not None, "cloud_turn_sent WorkspaceEvent must exist"
    assert sent_event.actor_id == owner_id, (
        f"cloud_turn_sent.actor_id must equal owner.id ({owner_id}); "
        f"got {sent_event.actor_id}"
    )
    assert "turn_job_id" in sent_event.payload, (
        "cloud_turn_sent payload must contain turn_job_id"
    )
    assert sent_event.payload["turn_job_id"] == str(job_id)

    # ------------------------------------------------------------------ #
    # Step 5: run drain against FakeAgentAdapter                           #
    # ------------------------------------------------------------------ #
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="s1"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
        workspaces_dir=workspaces_dir,
    )

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        workspaces_dir=workspaces_dir,
    )
    assert dispatched == 1, f"drain must dispatch exactly 1 turn; got {dispatched}"

    # ------------------------------------------------------------------ #
    # Step 6: adapter saw the RESOLVED API-shape, not raw attachment_id   #
    # ------------------------------------------------------------------ #
    msg = adapter.last_user_message
    assert isinstance(msg, list), (
        f"adapter.last_user_message must be list[dict]; got {type(msg).__name__}"
    )
    assert len(msg) == 2, f"expected 2 blocks (text + image), got {len(msg)}: {msg!r}"

    text_block, image_block = msg

    # Text block.
    assert text_block == {"type": "text", "text": "What is shown in this figure?"}, (
        f"unexpected text block: {text_block!r}"
    )

    # Image block must be the fully-resolved API shape — NOT the
    # lightweight {"type":"image","attachment_id":"..."} that ChatTurn.content holds.
    assert image_block.get("type") == "image", (
        f"second block must have type='image'; got {image_block!r}"
    )
    source = image_block.get("source")
    assert isinstance(source, dict), f"image block must have 'source' dict; got {source!r}"
    assert source.get("type") == "base64", (
        f"source.type must be 'base64'; got {source.get('type')!r}"
    )
    assert source.get("media_type") == "image/png", (
        f"source.media_type must be 'image/png'; got {source.get('media_type')!r}"
    )
    # The data must be the base64 encoding of the exact bytes we uploaded.
    expected_b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
    assert source.get("data") == expected_b64, (
        "image block base64 data must round-trip to the original uploaded bytes"
    )
    # Confirm the raw attachment_id reference was NOT passed through.
    assert "attachment_id" not in image_block, (
        "adapter must NOT see the raw attachment_id reference — "
        "the resolver must have replaced it with the base64 source"
    )

    # ------------------------------------------------------------------ #
    # Step 7: cloud_turn_completed event landed                            #
    # ------------------------------------------------------------------ #
    with pg_session_factory() as s:
        completed_event = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "cloud_turn_completed",
            )
        )
    assert completed_event is not None, "cloud_turn_completed WorkspaceEvent must exist"
    assert completed_event.payload.get("turn_job_id") == str(job_id)

    # Sanity-check: turn_job transitioned to 'completed'.
    with pg_session_factory() as s:
        final_job = s.get(TurnJob, job_id)
    assert final_job.status == "completed"
