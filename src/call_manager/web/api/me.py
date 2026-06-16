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
def get_me(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    return MeOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            status=user.status,
            default_profile_id=user.default_profile_id,
        ),
        capabilities={"admin": user.is_admin},
    )
