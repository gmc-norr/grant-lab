from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Profile, ProfileBootstrapJob, User

router = APIRouter()


class ProfileListItemOut(BaseModel):
    id: int
    name: str
    kind: str
    level: str | None
    areas_count: int
    budget_currency: str | None
    description: str


class ProfilesListOut(BaseModel):
    profiles: list[ProfileListItemOut]


def _flatten_structured(p: Profile) -> dict:
    """Mirror of parse_structured: unwrap structured.{org,eligibility,funding,geo}
    into flat form fields. Read via .get — org exists only for org units,
    career_stage only for individuals. NOTE the one asymmetry: flat
    `funding_instruments` is stored as structured["funding"]["instruments"]."""
    s = p.structured or {}
    elig = s.get("eligibility") or {}
    org = s.get("org") or {}
    funding = s.get("funding") or {}
    geo = s.get("geo") or {}
    return {
        "areas": list(s.get("areas") or []),
        "budget_min": s.get("budget_min"),
        "budget_max": s.get("budget_max"),
        "trl_min": s.get("trl_min"),
        "trl_max": s.get("trl_max"),
        "must_include": list(s.get("must_include") or []),
        "hard_exclusions": list(s.get("hard_exclusions") or []),
        "org_type": org.get("org_type"),
        "capabilities": list(org.get("capabilities") or []),
        "patient_populations": list(org.get("patient_populations") or []),
        "scale": org.get("scale"),
        "career_stage": elig.get("career_stage"),
        "role": elig.get("role"),
        "years_since_phd": elig.get("years_since_phd"),
        "applicant_types": list(elig.get("applicant_types") or []),
        "funding_instruments": list(funding.get("instruments") or []),
        "funder_exclusions": list(funding.get("funder_exclusions") or []),
        "country": geo.get("country"),
        "host_institution": geo.get("host_institution"),
    }


class ProfileDetailOut(BaseModel):
    id: int
    name: str
    kind: str
    level: str | None
    description: str
    budget_currency: str | None
    # rubric
    areas: list[str]
    budget_min: int | None
    budget_max: int | None
    trl_min: int | None
    trl_max: int | None
    must_include: list[str]
    hard_exclusions: list[str]
    # namespaced -> flat
    org_type: str | None
    capabilities: list[str]
    patient_populations: list[str]
    scale: str | None
    career_stage: str | None
    role: str | None
    years_since_phd: int | None
    applicant_types: list[str]
    funding_instruments: list[str]
    funder_exclusions: list[str]
    country: str | None
    host_institution: str | None
    # state
    can_edit: bool
    bootstrap_status: str | None
    bootstrap_pubmed_status: str | None
    publications_fetched: int | None


@router.get("/profiles", response_model=ProfilesListOut)
def list_profiles(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        rows = list(
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            ).scalars()
        )
        out = [
            ProfileListItemOut(
                id=p.id,
                name=p.name,
                kind=p.kind,
                level=p.level,
                areas_count=len((p.structured or {}).get("areas") or []),
                budget_currency=p.budget_currency,
                description=p.description or "",
            )
            for p in rows
        ]
    return ProfilesListOut(profiles=out)


@router.get("/profiles/{profile_id}", response_model=ProfileDetailOut)
def get_profile(
    request: Request, profile_id: int, user: User = Depends(get_current_user)  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        can_edit = p.created_by == user.id or user.is_admin
        job = session.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == profile_id)
        ).scalar_one_or_none()
        flat = _flatten_structured(p)
        core = {
            "id": p.id, "name": p.name, "kind": p.kind, "level": p.level,
            "description": p.description or "", "budget_currency": p.budget_currency,
        }
        # Defense-in-depth: only the owner/admin (who polls on the edit page) sees job state.
        if can_edit and job is not None:
            boot = {
                "bootstrap_status": job.status,
                "bootstrap_pubmed_status": job.pubmed_status,
                "publications_fetched": job.publications_fetched,
            }
        else:
            boot = {
                "bootstrap_status": None,
                "bootstrap_pubmed_status": None,
                "publications_fetched": None,
            }
    return ProfileDetailOut(**core, **flat, can_edit=can_edit, **boot)
