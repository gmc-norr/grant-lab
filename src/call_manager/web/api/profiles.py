from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    Profile,
    ProfileBootstrapJob,
    ProfileKind,
    ProfileLevel,
    ProfileViewer,
    User,
)
from call_manager.embeddings.similarity import build_profile_text
from call_manager.profiles import post_edit
from call_manager.profiles.create import create_profile_with_bootstrap
from call_manager.profiles.fields import FieldValidationError, parse_structured
from call_manager.profiles.forms import (
    ALLOWED_CURRENCIES,
    BUDGET_MAX,
    BUDGET_MIN,
    TRL_MAX,
    TRL_MIN,
    bounded_int_or_400,
    parse_members_strict,
    split_lines,
)
from call_manager.web.profile_access import can_read_profile, readable_profile_ids

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
    # 'org' | 'private'. The owner's UI has to render its own current state; the
    # only alternative would be guessing, or a second endpoint that does not exist.
    visibility: str
    bootstrap_status: str | None
    bootstrap_pubmed_status: str | None
    publications_fetched: int | None


def _profile_for_edit(session, *, profile_id: int, user: User) -> Profile:
    """The profile `user` may EDIT, or the right refusal — in this exact order.

    THREE checks, never two. Collapsing the last two into a single
    `if not can_edit: raise 403` is the mutation two reviewers named: the owner
    gate still holds, and every id in the database becomes answerable — 403 for
    one that exists, 404 for one that does not.

    Reading stays WIDER than editing. A colleague the profile is shared with
    reaches the 403; only someone who cannot read it at all sees the 404.
    """
    p = session.get(Profile, profile_id)
    if p is None or p.is_active is False:
        raise HTTPException(status_code=404, detail="profile not found")
    if not can_read_profile(session, user, p.id):
        raise HTTPException(status_code=404, detail="profile not found")
    if p.created_by != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not authorized")
    return p


@router.get("/profiles", response_model=ProfilesListOut)
def list_profiles(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        rows = list(
            session.execute(
                select(Profile)
                .where(
                    Profile.is_active.is_(True),
                    Profile.id.in_(readable_profile_ids(user)),
                )
                .order_by(Profile.name)
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
    request: Request,
    profile_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        # 404, not 403. workspaces.py:427-428 already made this call for the same
        # reason: "non-owner must not be able to distinguish 'no proposal-spec.md
        # yet' (404) from 'owner only' (403) for any workspace UUID they can
        # name." A 403 confirms the profile exists, and profile NAMES are the
        # thing worth not confirming: someone can walk /api/profiles/1..N and
        # ENUMERATE who is working on what. Identical detail string to the line
        # above, so the two answers are the same answer.
        if not can_read_profile(session, user, p.id):
            raise HTTPException(status_code=404, detail="profile not found")
        can_edit = p.created_by == user.id or user.is_admin
        job = session.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == profile_id)
        ).scalar_one_or_none()
        flat = _flatten_structured(p)
        core = {
            "id": p.id,
            "name": p.name,
            "kind": p.kind,
            "level": p.level,
            "description": p.description or "",
            "budget_currency": p.budget_currency,
            "visibility": p.visibility,
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
    request: Request,
    body: ProfileCreateIn,
    user: User = Depends(get_current_user),  # noqa: B008
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
                "areas": body.areas,
                "career_stage": body.career_stage,
                "applicant_types": body.applicant_types,
                "role": body.role,
                "years_since_phd": body.years_since_phd,
                "org_type": body.org_type,
                "capabilities": body.capabilities,
                "patient_populations": body.patient_populations,
                "scale": body.scale,
                "funding_instruments": body.funding_instruments,
                "funder_exclusions": body.funder_exclusions,
                "country": body.country,
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
        "name": body.name,
        "kind": kind,
        "level": level_val,
        "members": [
            {"orcid": m.orcid, "name": m.name, "affiliation": m.affiliation} for m in members
        ],
        "pasted_grants": body.pasted_grants,
        "mission_text": body.mission_text,
    }
    try:
        with sf() as session:
            profile = create_profile_with_bootstrap(
                session=session,
                owner_id=user.id,
                name=body.name,
                kind=kind,
                level=level_val,
                structured={**user_structured},
                seed_payload=seed_payload,
            )
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e
    return ProfileCreateOut(id=new_id)


class ProfileUpdateIn(BaseModel):
    description: str = ""
    areas: str = ""
    budget_min: str = ""
    budget_max: str = ""
    budget_currency: str = ""
    trl_min: str = ""
    trl_max: str = ""
    must_include: str = ""
    hard_exclusions: str = ""
    level: str = ""
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


class ProfileUpdateOut(BaseModel):
    id: int


@router.put("/profiles/{profile_id}", response_model=ProfileUpdateOut)
def update_profile(
    request: Request,
    profile_id: int,
    body: ProfileUpdateIn,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(body.description) > 50_000:
        raise HTTPException(status_code=413, detail="description too long (max 50 KB)")
    parsed_budget_min = bounded_int_or_400(
        body.budget_min, field="budget_min", low=BUDGET_MIN, high=BUDGET_MAX
    )
    parsed_budget_max = bounded_int_or_400(
        body.budget_max, field="budget_max", low=BUDGET_MIN, high=BUDGET_MAX
    )
    parsed_trl_min = bounded_int_or_400(body.trl_min, field="trl_min", low=TRL_MIN, high=TRL_MAX)
    parsed_trl_max = bounded_int_or_400(body.trl_max, field="trl_max", low=TRL_MIN, high=TRL_MAX)
    normalized_currency = (body.budget_currency or "").strip().upper()
    if normalized_currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=400, detail=f"unknown currency: {body.budget_currency!r}")

    sf = request.app.state.session_factory
    embed_text_changed = False
    with sf() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        # 404, not 403 — the same ladder _profile_for_edit runs, inlined here
        # because the architecture gate requires this pinned surface to CALL the
        # single-source predicate itself. Authorization is UNCHANGED (the line
        # below is still created_by-or-admin); what changes is the ORACLE. This
        # route used to 404 only for a missing or inactive id and 403 for every
        # active profile the caller did not own, so a stranger learned a private
        # profile exists by trying to edit it and could ENUMERATE names by
        # walking /api/profiles/1..N. Never widen the created_by line below to
        # the read rule: reading is wider than editing, and a colleague the
        # profile is shared with must reach the 403.
        if not can_read_profile(session, user, p.id):
            raise HTTPException(status_code=404, detail="profile not found")
        if p.created_by != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")
        active_job = session.execute(
            select(ProfileBootstrapJob.id).where(
                ProfileBootstrapJob.profile_id == profile_id,
                ProfileBootstrapJob.status.in_(("queued", "running")),
            )
        ).scalar_one_or_none()
        if active_job is not None:
            raise HTTPException(
                status_code=409, detail="profile is still drafting — try again shortly"
            )
        old_embed_text = build_profile_text(p)
        p.description = body.description
        if p.kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
            lv = (body.level or "").strip() or None
            if lv is not None and lv not in {x.value for x in ProfileLevel}:
                raise HTTPException(status_code=400, detail=f"unknown level: {body.level!r}")
            p.level = lv
        base = {
            "areas": split_lines(body.areas),
            "budget_min": parsed_budget_min,
            "budget_max": parsed_budget_max,
            "trl_min": parsed_trl_min,
            "trl_max": parsed_trl_max,
            "must_include": split_lines(body.must_include),
            "hard_exclusions": split_lines(body.hard_exclusions),
        }
        try:
            new_ns = parse_structured(
                kind=p.kind,
                raw={
                    "areas": body.areas,
                    "career_stage": body.career_stage,
                    "applicant_types": body.applicant_types,
                    "role": body.role,
                    "years_since_phd": body.years_since_phd,
                    "org_type": body.org_type,
                    "capabilities": body.capabilities,
                    "patient_populations": body.patient_populations,
                    "scale": body.scale,
                    "funding_instruments": body.funding_instruments,
                    "funder_exclusions": body.funder_exclusions,
                    "country": body.country,
                    "host_institution": body.host_institution,
                },
            )
        except FieldValidationError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        new_ns.pop("areas", None)  # base owns areas
        p.structured = {**base, **new_ns}
        p.budget_currency = normalized_currency or None
        new_embed_text = build_profile_text(p)
        embed_text_changed = old_embed_text.strip() != new_embed_text.strip()
        session.commit()

    if body.description.strip():
        background.add_task(
            post_edit.refresh_and_backfill_in_background,
            session_factory=sf,
            embed_client=request.app.state.embed_client,
            profile_id=profile_id,
            refresh_embedding=embed_text_changed,
        )
    return ProfileUpdateOut(id=profile_id)


# --------------------------------------------------------------------------
# Sharing. Four operations over two paths, all gated by _profile_for_edit:
# 404 for a profile the caller cannot READ, 403 for one they can read but not
# EDIT. A viewer who could re-share is not a viewer, and the owner is not the
# owner.
#
# These four are the ONLY functions outside db/models.py and web/profile_access.py
# allowed to touch ProfileViewer or the visibility column, and they are exempt in
# tests/architecture/test_profile_access_single_source.py BY NAME: they manage
# the rows, they do not decide access. Renaming one loses its exemption.
# --------------------------------------------------------------------------


class ProfileViewerOut(BaseModel):
    user_id: int
    # Name and email, not bare ids: `[3, 17]` is unusable in a UI, and the owner
    # has no other endpoint that could turn an id into a person (/api/admin/users
    # is admin-only).
    name: str | None
    email: str


class ProfileViewersOut(BaseModel):
    viewers: list[ProfileViewerOut]


def _viewers_out(rows) -> ProfileViewersOut:
    """Rows of (id, name, email) -> the response. Holds no query of its own: the
    SELECT over ProfileViewer must stay inside a by-name exempt function."""
    return ProfileViewersOut(
        viewers=[ProfileViewerOut(user_id=r.id, name=r.name, email=r.email) for r in rows]
    )


@router.get("/profiles/{profile_id}/viewers", response_model=ProfileViewersOut)
def list_profile_viewers(
    request: Request,
    profile_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        p = _profile_for_edit(session, profile_id=profile_id, user=user)
        rows = session.execute(
            select(User.id, User.name, User.email)
            .join(ProfileViewer, ProfileViewer.user_id == User.id)
            .where(ProfileViewer.profile_id == p.id)
            .order_by(User.email)
        ).all()
    return _viewers_out(rows)


class ProfileViewersIn(BaseModel):
    user_ids: list[int]


@router.put("/profiles/{profile_id}/viewers", response_model=ProfileViewersOut)
def replace_profile_viewers(
    request: Request,
    profile_id: int,
    body: ProfileViewersIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Reconcile the viewer list to `user_ids`, and answer with the server's truth.

    Ids that are not active users are dropped SILENTLY rather than 400'd — the
    same shape update_subscriptions uses, and for the same reason: a per-id error
    would be a user-existence oracle, and the client can reconcile from the
    response. The owner's own id is ignored; created_by already grants access and
    a row would let "remove this viewer" lock an owner out of their own profile.

    A DIFF, not delete-all-then-reinsert: added_at is a real timestamp and
    re-inserting an unchanged viewer would reset it.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        p = _profile_for_edit(session, profile_id=profile_id, user=user)
        desired = set(body.user_ids) - {p.created_by}
        valid_ids = (
            set(
                session.execute(
                    select(User.id).where(User.id.in_(desired), User.status == "active")
                ).scalars()
            )
            if desired
            else set()
        )
        existing = set(
            session.execute(
                select(ProfileViewer.user_id).where(ProfileViewer.profile_id == p.id)
            ).scalars()
        )
        to_remove = existing - valid_ids
        if to_remove:
            session.execute(
                delete(ProfileViewer).where(
                    ProfileViewer.profile_id == p.id,
                    ProfileViewer.user_id.in_(to_remove),
                )
            )
        for uid in sorted(valid_ids - existing):
            session.add(ProfileViewer(profile_id=p.id, user_id=uid))
        # Emptying the list does NOT touch visibility. A private profile with no
        # viewers is exactly that; flipping it back to 'org' here would re-share
        # it with the whole organization as a side effect of un-sharing it.
        session.commit()
        rows = session.execute(
            select(User.id, User.name, User.email)
            .join(ProfileViewer, ProfileViewer.user_id == User.id)
            .where(ProfileViewer.profile_id == profile_id)
            .order_by(User.email)
        ).all()
    return _viewers_out(rows)


class AddProfileViewerIn(BaseModel):
    email: str


@router.post("/profiles/{profile_id}/viewers", response_model=ProfileViewersOut)
def add_profile_viewer(
    request: Request,
    profile_id: int,
    body: AddProfileViewerIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Add one viewer BY EMAIL — the repo's existing sharing shape (settings.py
    add_member).

    PUT {user_ids} cannot be the only way in: a non-admin owner has no endpoint
    that lists users, so they could never learn a colleague's id.

    Unknown, non-active or otherwise unaddable address -> one GENERIC 400, the
    same answer every time, so this endpoint is not a directory of who has an
    account. Mirrors settings.py's "could not add collaborator".
    """
    sf = request.app.state.session_factory
    with sf() as session:
        p = _profile_for_edit(session, profile_id=profile_id, user=user)
        email = (body.email or "").strip().lower()
        target = (
            session.scalar(select(User).where(User.email == email)) if email else None
        )
        if target is None or target.status != "active":
            raise HTTPException(status_code=400, detail="could not add viewer")
        if target.id != p.created_by:  # the owner never has a row — no-op, 200
            already = session.scalar(
                select(ProfileViewer.user_id).where(
                    ProfileViewer.profile_id == p.id,
                    ProfileViewer.user_id == target.id,
                )
            )
            if already is None:
                session.add(ProfileViewer(profile_id=p.id, user_id=target.id))
                session.commit()
        rows = session.execute(
            select(User.id, User.name, User.email)
            .join(ProfileViewer, ProfileViewer.user_id == User.id)
            .where(ProfileViewer.profile_id == profile_id)
            .order_by(User.email)
        ).all()
    return _viewers_out(rows)


class ProfileVisibilityIn(BaseModel):
    # A Literal, so an unknown value is refused by the schema before it can reach
    # ck_profiles_visibility — the CHECK is the last line of defence, not the first.
    visibility: Literal["private", "org"]


class ProfileVisibilityOut(BaseModel):
    visibility: str


@router.put("/profiles/{profile_id}/visibility", response_model=ProfileVisibilityOut)
def set_profile_visibility(
    request: Request,
    profile_id: int,
    body: ProfileVisibilityIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        p = _profile_for_edit(session, profile_id=profile_id, user=user)
        # Independent of the viewer list: going private keeps whoever was already
        # shared with, and going 'org' leaves their rows in place so that flipping
        # back does not silently drop them.
        p.visibility = body.visibility
        session.commit()
    return ProfileVisibilityOut(visibility=body.visibility)
