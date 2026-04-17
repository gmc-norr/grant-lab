from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import User

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    return request.app.state.templates.TemplateResponse(
        request=request, name="me.html", context={"user": user}
    )


@router.post("/me", response_class=HTMLResponse)
def update_me(
    request: Request,
    name: str = Form(""),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            # User row vanished between get_current_user's lookup and this
            # handler — treat as logged out rather than 500 on None.name.
            raise HTTPException(status_code=401, detail="not authenticated")
        db_user.name = name.strip() or None
        session.commit()
        session.refresh(db_user)
        # Re-render with updated user
        return request.app.state.templates.TemplateResponse(
            request=request, name="me.html", context={"user": db_user}
        )
