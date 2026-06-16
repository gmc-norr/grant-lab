from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api")

from call_manager.web.api import me as _me  # noqa: E402

router.include_router(_me.router)
