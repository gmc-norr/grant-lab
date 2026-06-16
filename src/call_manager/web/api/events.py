from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/events/ping")
async def ping(
    request: Request,
    max: int = Query(default=0, ge=0),
    since: int | None = Query(default=None),
    cursor: int | None = Query(default=None),
) -> StreamingResponse:
    """Dev-only SSE validation endpoint: replayable named JSON ``ping`` events
    with ids, so the client hook exercises typed events + ids + catch-up via
    BOTH fresh-connect ?since/?cursor and in-session Last-Event-ID."""
    # Fresh-connect catch-up: ?since (or ?cursor) sets the resume point.
    seed = since if since is not None else cursor
    start = seed + 1 if seed is not None else 0
    # In-session reconnect: Last-Event-ID wins when present (mirrors workspaces.py).
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        start = int(last) + 1

    async def gen() -> AsyncIterator[bytes]:
        n, sent = start, 0
        while max == 0 or sent < max:
            payload = json.dumps({"n": n})
            yield f"id: {n}\nevent: ping\ndata: {payload}\n\n".encode()
            n += 1
            sent += 1
            if max == 0:  # unbounded live stream: pace it, never busy-loop
                await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
