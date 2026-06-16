from __future__ import annotations

from fastapi import APIRouter

from call_manager.web.api import calls, me

router = APIRouter(prefix="/api")
router.include_router(me.router)
router.include_router(calls.router)
