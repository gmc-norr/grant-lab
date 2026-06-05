import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
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
    return {"status": "ok"}
