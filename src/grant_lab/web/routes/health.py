import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

router = APIRouter()


class HealthOut(BaseModel):
    status: str
    # True only in the ONE process that holds the single-instance advisory
    # lock and whose lock connection is still alive. A standby
    # (SCHEDULER_STANDBY_OK) reports False; so does an instance whose lock
    # backend died under it (review 2026-09-02 §2.6(b)) — the status code stays
    # 200 because HTTP still works, the field is what a monitor alerts on.
    scheduler_lock: bool


@router.get("/healthz")
def healthz(request: Request) -> HealthOut:
    """Liveness + readiness in one endpoint.

    Single-instance deploy: there's no useful liveness-vs-readiness split.
    Monitoring should treat any non-200 as "do not route traffic / page
    operator." We ping the database with `SELECT 1` so an unreachable DB
    surfaces as 503 instead of being silently invisible.
    """
    engine = request.app.state.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface any DB error as 503
        # /healthz is unauthenticated; don't echo the driver exception (it can
        # carry the DSN/host). Log it server-side, return a static message.
        logging.getLogger(__name__).warning("healthz db check failed: %s", exc)
        raise HTTPException(status_code=503, detail="db unavailable") from exc
    lock = getattr(request.app.state, "scheduler_lock", None)
    return HealthOut(status="ok", scheduler_lock=lock is not None and lock.is_held())
