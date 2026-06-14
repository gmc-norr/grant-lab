from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Profile, Subscription, User

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profiles = (
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            )
            .scalars()
            .all()
        )
        subscribed_ids = set(
            session.execute(select(Subscription.profile_id).where(Subscription.user_id == user.id))
            .scalars()
            .all()
        )
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="me.html",
        context={
            "user": user,
            "profiles": profiles,
            "subscribed_ids": subscribed_ids,
            "google_oidc_enabled": request.app.state.settings.google_oidc_enabled,
            "google_linked": user.oidc_sub is not None,
        },
    )


@router.post("/me/name")
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
    # Post/Redirect/Get: the /me form is a plain full-page POST (no HTMX),
    # so redirect to a fresh GET. A browser refresh after submit re-issues
    # the GET, not the POST — no double-submit.
    return RedirectResponse(url="/me", status_code=303)


@router.post("/me/subscriptions")
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
        valid_ids = (
            set(
                session.execute(
                    select(Profile.id).where(
                        Profile.id.in_(desired_subscribed_ids),
                        Profile.is_active.is_(True),
                    )
                )
                .scalars()
                .all()
            )
            if desired_subscribed_ids
            else set()
        )
        # DIFF rather than delete-all-then-reinsert. Reinserting would reset
        # each surviving row's digest_enabled flag back to the default True,
        # silently re-enabling a digest the user had turned off. Touch only
        # the rows that actually changed.
        existing_ids = set(
            session.execute(select(Subscription.profile_id).where(Subscription.user_id == user.id))
            .scalars()
            .all()
        )
        to_remove = existing_ids - valid_ids
        to_add = valid_ids - existing_ids
        if to_remove:
            session.execute(
                delete(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.profile_id.in_(to_remove),
                )
            )
        for pid in to_add:
            session.add(Subscription(user_id=user.id, profile_id=pid))
        session.commit()
    # Post/Redirect/Get — see update_name above.
    return RedirectResponse(url="/me", status_code=303)
