from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User

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


@router.post("/me/default-profile", status_code=200)
def set_default(request: Request, body: DefaultProfileIn, user: User = Depends(get_current_user)):  # noqa: B008
    from call_manager.web.dashboard import set_default_profile

    sf = request.app.state.session_factory
    with sf() as session:
        set_default_profile(session, user_id=user.id, profile_id=body.profile_id)
        session.commit()
    return {"ok": True}
