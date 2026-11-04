import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

router = APIRouter()


class HealthOut(BaseModel):
    status: str
    # True only in the ONE process that holds the single-instance advisory
    # lock and whose lock connection is still alive (review 2026-09-02
    # §2.6(b)). False is a 503 unless this is a deliberate standby
    # (SCHEDULER_STANDBY_OK) — see healthz.
    scheduler_lock: bool


@router.get("/healthz", response_model=HealthOut)
def healthz(request: Request) -> HealthOut | JSONResponse:
    """Liveness + readiness in one endpoint.

    Single-instance deploy: there's no useful liveness-vs-readiness split.
    Monitoring should treat any non-200 as "do not route traffic / page
    operator." We ping the database with `SELECT 1` so an unreachable DB
    surfaces as 503 instead of being silently invisible.

    A process that does not hold the single-instance lock is a 503 too, unless
    it was booted as a deliberate standby (SCHEDULER_STANDBY_OK=true). Since
    93a5ea26 the watchdog STOPS the scheduler when the lock is lost, so a 200
    there meant HTTP up, zero scheduled work, and every status-code monitor
    green (review 2026-10-06 M12). The same holds for a process whose lock
    probe failed at boot: no scheduler ever started, and only a restart fixes
    it. The body still says why.
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
    held = lock is not None and lock.is_held()
    if not held and not request.app.state.settings.scheduler_standby_ok:
        return JSONResponse(
            status_code=503,
            content=HealthOut(status="scheduler_lock_not_held", scheduler_lock=False).model_dump(),
        )
    return HealthOut(status="ok", scheduler_lock=held)
