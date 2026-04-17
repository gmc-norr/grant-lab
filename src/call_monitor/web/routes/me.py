from fastapi import APIRouter, Depends, Form, Request
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


@router.post("/me", response_class=HTMLResponse)
async def update_me(
    request: Request,
    name: str = Form(""),
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
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="user not found")
        db_user.name = name.strip() or None

        # Sync subscriptions only when the subscriptions form was
        # actually submitted. The name-only form omits the
        # `_subscriptions_submitted` discriminator, so it must not
        # touch the subscriptions table — otherwise every name save
        # would silently unsubscribe the user from every profile.
        if "_subscriptions_submitted" in form:
            # Only allow subscriptions to ACTIVE profiles that actually exist.
            # Silently drop stale / fabricated IDs rather than 500ing on the FK.
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

        # Re-render with fresh state
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
        context={
            "user": db_user,
            "profiles": profiles,
            "subscribed_ids": subscribed_ids,
        },
    )
