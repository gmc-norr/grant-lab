from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.auth.passwords import WeakPassword, hash_password, verify_password
from call_manager.db.models import Profile, Subscription, User
from call_manager.web.dashboard import set_default_profile

router = APIRouter()


class UserOut(BaseModel):
    id: int
    email: str
    status: str
    default_profile_id: int | None


class MeOut(BaseModel):
    user: UserOut
    capabilities: dict[str, bool]


@router.get("/me", response_model=MeOut)
def get_me(user: User = Depends(get_current_user)):  # noqa: B008
    return MeOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            status=user.status,
            default_profile_id=user.default_profile_id,
        ),
        capabilities={"admin": user.is_admin},
    )


class DefaultProfileIn(BaseModel):
    profile_id: int | None


class DefaultProfileOut(BaseModel):
    default_profile_id: int | None


@router.post("/me/default-profile", response_model=DefaultProfileOut)
def set_default(request: Request, body: DefaultProfileIn, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        set_default_profile(session, user_id=user.id, profile_id=body.profile_id)
        session.commit()
    # set_default_profile 404s on an invalid id before we get here, so the stored
    # value now equals body.profile_id (or None when clearing).
    return DefaultProfileOut(default_profile_id=body.profile_id)


class AccountProfileOut(BaseModel):
    id: int
    name: str
    subscribed: bool


class AccountSettingsOut(BaseModel):
    name: str | None
    email: str
    is_admin: bool
    google_oidc_enabled: bool
    google_linked: bool
    has_password: bool
    profiles: list[AccountProfileOut]


@router.get("/me/settings", response_model=AccountSettingsOut)
def get_settings(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        profiles = list(
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            ).scalars()
        )
        subscribed_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars()
        )
        profile_out = [
            AccountProfileOut(id=p.id, name=p.name, subscribed=(p.id in subscribed_ids))
            for p in profiles
        ]
    return AccountSettingsOut(
        name=user.name,
        email=user.email,
        is_admin=user.is_admin,
        google_oidc_enabled=request.app.state.settings.google_oidc_enabled,
        google_linked=user.oidc_sub is not None,
        has_password=user.password_hash is not None,
        profiles=profile_out,
    )


class UpdateNameIn(BaseModel):
    name: str


class UpdateNameOut(BaseModel):
    name: str | None


@router.post("/me/name", response_model=UpdateNameOut)
def update_name(
    request: Request,
    body: UpdateNameIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        db_user.name = body.name.strip() or None
        new_name = db_user.name
        session.commit()
    return UpdateNameOut(name=new_name)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordOut(BaseModel):
    ok: bool


@router.post("/me/password", response_model=ChangePasswordOut)
def change_password(
    request: Request,
    body: ChangePasswordIn,
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    signer = request.app.state.session_signer
    settings = request.app.state.settings
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        # An OAuth-only account (no hash) can't mint a first password from a session.
        if db_user.password_hash is None or not verify_password(
            db_user.password_hash, body.current_password
        ):
            raise HTTPException(status_code=400, detail="current password is incorrect")
        try:
            db_user.password_hash = hash_password(body.new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db_user.session_epoch = (db_user.session_epoch or 0) + 1  # revoke other sessions
        new_epoch = db_user.session_epoch
        session.commit()
    # Re-mint THIS browser's session under the new epoch (same cookie attrs as the Jinja route).
    response.set_cookie(
        "session",
        signer.make(user_id=user.id, session_epoch=new_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return ChangePasswordOut(ok=True)
