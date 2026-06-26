from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    Profile,
    ProfileBootstrapJob,
    ProfileKind,
    ProfileLevel,
    User,
)
from call_manager.profiles.create import create_profile_with_bootstrap
from call_manager.profiles.fields import FieldValidationError, parse_structured
from call_manager.profiles.forms import parse_members_strict

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


class ProfileCreateIn(BaseModel):
    name: str
    kind: str = "organizational_unit"
    level: str = ""
    orcids: str = ""
    names_affiliations: str = ""
    pasted_grants: str = ""
    mission_text: str = ""
    areas: str = ""
    career_stage: str = ""
    applicant_types: str = ""
    role: str = ""
    years_since_phd: str = ""
    org_type: str = ""
    capabilities: str = ""
    patient_populations: str = ""
    scale: str = ""
    funding_instruments: str = ""
    funder_exclusions: str = ""
    country: str = ""
    host_institution: str = ""


class ProfileCreateOut(BaseModel):
    id: int


@router.post("/profiles", response_model=ProfileCreateOut)
def create_profile(
    request: Request, body: ProfileCreateIn, user: User = Depends(get_current_user)  # noqa: B008
):
    if len(body.name) > 200:
        raise HTTPException(status_code=400, detail="name too long (max 200 chars)")
    if len(body.pasted_grants) > 100_000:
        raise HTTPException(status_code=413, detail="pasted grants too large (max 100 KB)")
    if len(body.mission_text) > 10_240:
        raise HTTPException(status_code=413, detail="mission text too large (max 10 KB)")
    valid_kinds = {k.value for k in ProfileKind}
    kind = body.kind if body.kind in valid_kinds else ProfileKind.ORGANIZATIONAL_UNIT.value
    level_val: str | None = None
    if kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
        level_val = body.level.strip() or None
        if level_val is not None and level_val not in {x.value for x in ProfileLevel}:
            raise HTTPException(status_code=400, detail=f"unknown level: {body.level!r}")
    try:
        user_structured = parse_structured(
            kind=kind,
            raw={
                "areas": body.areas, "career_stage": body.career_stage,
                "applicant_types": body.applicant_types, "role": body.role,
                "years_since_phd": body.years_since_phd, "org_type": body.org_type,
                "capabilities": body.capabilities, "patient_populations": body.patient_populations,
                "scale": body.scale, "funding_instruments": body.funding_instruments,
                "funder_exclusions": body.funder_exclusions, "country": body.country,
                "host_institution": body.host_institution,
            },
        )
    except FieldValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e

    sf = request.app.state.session_factory
    with sf() as session:
        existing = session.execute(
            select(Profile.id).where(Profile.name == body.name, Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="profile name already exists")
    members = parse_members_strict(body.orcids, body.names_affiliations)
    seed_payload = {
        "name": body.name, "kind": kind, "level": level_val,
        "members": [
            {"orcid": m.orcid, "name": m.name, "affiliation": m.affiliation}
            for m in members
        ],
        "pasted_grants": body.pasted_grants, "mission_text": body.mission_text,
    }
    try:
        with sf() as session:
            profile = create_profile_with_bootstrap(
                session=session, owner_id=user.id, name=body.name, kind=kind,
                level=level_val, structured={**user_structured}, seed_payload=seed_payload,
            )
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e
    return ProfileCreateOut(id=new_id)
