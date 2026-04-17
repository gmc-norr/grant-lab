from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Profile, Subscription, User

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
        subscribed_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars().all()
        )
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="me.html",
        context={
            "user": user,
            "profiles": profiles,
            "subscribed_ids": subscribed_ids,
        },
    )


@router.post("/me/name", response_class=HTMLResponse)
def update_name(
    request: Request,
    name: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        db_user.name = name.strip() or None
        session.commit()
        session.refresh(db_user)
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
        subscribed_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars().all()
        )
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="me.html",
        context={"user": db_user, "profiles": profiles, "subscribed_ids": subscribed_ids},
    )


@router.post("/me/subscriptions", response_class=HTMLResponse)
async def update_subscriptions(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    form = await request.form()
    desired_subscribed_ids = {
        int(k.removeprefix("subscribe_"))
        for k in form
        if k.startswith("subscribe_") and k.removeprefix("subscribe_").isdigit()
    }
    with session_factory() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        valid_ids = set(
            session.execute(
                select(Profile.id).where(
                    Profile.id.in_(desired_subscribed_ids),
                    Profile.is_active.is_(True),
                )
            ).scalars().all()
        ) if desired_subscribed_ids else set()
        session.execute(delete(Subscription).where(Subscription.user_id == user.id))
        for pid in valid_ids:
            session.add(Subscription(user_id=user.id, profile_id=pid))
        session.commit()
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
        subscribed_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars().all()
        )
        session.refresh(db_user)
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="me.html",
        context={"user": db_user, "profiles": profiles, "subscribed_ids": subscribed_ids},
    )
