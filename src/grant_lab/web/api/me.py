from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from grant_lab.auth.dependencies import get_current_user
from grant_lab.auth.passwords import WeakPassword, hash_password, verify_password
from grant_lab.db.models import Profile, Subscription, User
from grant_lab.proposal_assist.collab.permissions import (
    WS_RECONNECTABLE_CLOSE,
    signal_user_revocation,
)
from grant_lab.web.dashboard import set_default_profile
from grant_lab.web.profile_access import readable_profile_ids

router = APIRouter()


class UserOut(BaseModel):
    id: int
    email: str
    name: str | None
    status: str
    default_profile_id: int | None


class MeOut(BaseModel):
    user: UserOut
    capabilities: dict[str, bool]


@router.get("/me", response_model=MeOut)
def get_me(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    return MeOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            status=user.status,
            default_profile_id=user.default_profile_id,
        ),
        capabilities={
            "admin": user.is_admin,
            # Whether THIS user can read deleted-workspace content: admin AND the
            # deployment allows it. One boolean because that is the question the
            # UI asks — it decides whether to offer the door, and a door that
            # 403s is worse than no door. The route re-checks the setting; this
            # is presentation, never the guard.
            "retention_inspection": (
                user.is_admin and request.app.state.settings.retention_inspection_enabled
            ),
        },
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
        # The picker's read-model is a SEPARATE query from GET /api/profiles —
        # Settings.tsx calls this endpoint, not that one — so it needs the read
        # rule in its own right. Without it every active profile's id AND NAME
        # goes to every user, and subscribing to one would mail them its ranked
        # calls every Monday.
        profiles = list(
            session.execute(
                select(Profile)
                .where(
                    Profile.is_active.is_(True),
                    Profile.id.in_(readable_profile_ids(user)),
                )
                .order_by(Profile.name)
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
    name: str = Field(max_length=200)


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
    # 1024-char cap mirrors LoginIn.password: argon2 cost is proportional
    # to password length; reject oversized inputs before hashing.
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


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
        ok, _ = verify_password(db_user.password_hash, body.current_password)
        if db_user.password_hash is None or not ok:
            raise HTTPException(status_code=400, detail="current password is incorrect")
        try:
            db_user.password_hash = hash_password(body.new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db_user.session_epoch = (db_user.session_epoch or 0) + 1  # revoke other sessions
        new_epoch = db_user.session_epoch
        session.commit()
    # Tear down the user's OTHER live collab sockets. 1012 (reconnectable), NOT
    # 1008: the acting browser re-mints its cookie just below and should reconnect
    # cleanly under the new epoch (logout/disable stay terminal 1008).
    signal_user_revocation(request, user.id, close_code=WS_RECONNECTABLE_CLOSE)
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


class SubscriptionsIn(BaseModel):
    profile_ids: list[int]


class SubscriptionsOut(BaseModel):
    subscribed: list[int]


@router.put("/me/subscriptions", response_model=SubscriptionsOut)
def update_subscriptions(
    request: Request,
    body: SubscriptionsIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    desired = set(body.profile_ids)
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        # ONE predicate, bound once and applied to BOTH sides of the diff below.
        # Unreadable is treated exactly like inactive: invisible to the diff in
        # both directions. Filtering only `valid_ids` would be worse than not
        # filtering at all — an unreadable-but-ACTIVE profile would still enter
        # `existing_active_ids`, land in `to_remove`, and one innocent settings
        # save after a revocation would DELETE the subscription and its
        # digest_enabled preference, which re-sharing could then never restore.
        readable = readable_profile_ids(user)
        valid_ids = (
            set(
                session.execute(
                    select(Profile.id).where(
                        Profile.id.in_(desired),
                        Profile.is_active.is_(True),
                        Profile.id.in_(readable),
                    )
                ).scalars()
            )
            if desired
            else set()
        )
        # DIFF scoped to ACTIVE, READABLE-profile subscriptions only — subs to
        # temporarily-inactive or no-longer-shared profiles are left untouched so
        # their digest_enabled preference is preserved. The client can never echo
        # either kind of id back (the picker shows neither), so scoping the diff
        # this way is the only safe one.
        #
        # The consequence, chosen deliberately (plan Task 5 Step 2): a subscription
        # to a profile the user can no longer read is INVISIBLE to the picker, the
        # user cannot remove it themselves, and it RESUMES the day the owner
        # re-shares the profile — that is the price of preserving digest_enabled
        # across a revocation, and nothing is delivered while the revocation
        # stands (the digest applies the same read rule).
        existing_active_ids = set(
            session.execute(
                select(Subscription.profile_id)
                .join(Profile, Profile.id == Subscription.profile_id)
                .where(
                    Subscription.user_id == user.id,
                    Profile.is_active.is_(True),
                    Profile.id.in_(readable),
                )
            ).scalars()
        )
        # DIFF, not delete-all-then-reinsert — reinserting would reset digest_enabled.
        to_remove = existing_active_ids - valid_ids
        to_add = valid_ids - existing_active_ids
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
    return SubscriptionsOut(subscribed=sorted(valid_ids))
