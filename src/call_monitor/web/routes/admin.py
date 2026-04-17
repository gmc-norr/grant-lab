from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from call_monitor.auth.dependencies import require_admin
from call_monitor.db.models import Call, Source, User

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        rows = session.execute(
            select(
                Source,
                func.count(Call.id).label("call_count"),
            )
            .outerjoin(Call, Call.source_id == Source.id)
            .group_by(Source.id)
            .order_by(Source.name)
        ).all()
        # Materialize before session closes
        sources = [
            {
                "name": src.name,
                "slug": src.slug,
                "last_fetched_at": src.last_fetched_at,
                "call_count": cc,
            }
            for src, cc in rows
        ]

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"sources": sources, "user": user},
    )
