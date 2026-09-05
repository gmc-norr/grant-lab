from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from grant_lab.web.api.turns import SSE_HEADERS

router = APIRouter()


# include_in_schema=False: this is an internal dev-only validation harness, not a
# public API operation — keep it out of /openapi.json (matches the spa_serving
# catch-all / session-cookie precedent) so the generated TS client never sees it
# and the openapi drift gate is mode-agnostic. ge=0 on since/cursor so a resume
# seed can never go negative (which would emit negative event ids).
@router.get("/events/ping", include_in_schema=False)
async def ping(
    request: Request,
    max: int = Query(default=0, ge=0),
    since: int | None = Query(default=None, ge=0),
    cursor: int | None = Query(default=None, ge=0),
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
        headers=SSE_HEADERS,
    )
