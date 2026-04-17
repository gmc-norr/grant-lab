from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Profile, User

router = APIRouter()


@router.get("/profiles", response_class=HTMLResponse)
def profiles_index(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="profiles_index.html",
        context={"profiles": profiles, "user": user},
    )
