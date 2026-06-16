from __future__ import annotations

from fastapi import APIRouter

from call_manager.web.api import me

router = APIRouter(prefix="/api")
router.include_router(me.router)
