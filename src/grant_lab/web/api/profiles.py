from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import (
    Profile,
    ProfileBootstrapJob,
    ProfileKind,
    ProfileLevel,
    ProfileViewer,
    Subscription,
    User,
)
from grant_lab.embeddings.similarity import build_profile_text
from grant_lab.profiles import post_edit
from grant_lab.profiles.create import create_profile_with_bootstrap
from grant_lab.profiles.fields import FieldValidationError, parse_structured
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.profiles.forms import (
    ALLOWED_CURRENCIES,
    BUDGET_MAX,
    BUDGET_MIN,
    TRL_MAX,
    TRL_MIN,
    bounded_int_or_400,
    parse_members_strict,
    split_lines,
)
from grant_lab.web.profile_access import can_read_profile, readable_profile_ids

router = APIRouter()


class ProfileListItemOut(BaseModel):
    id: int
    name: str
    kind: str
    level: str | None
    areas_count: int
    budget_currency: str | None
    description: str
    is_owner: bool = Field(description="The viewer created this profile.")
    can_edit: bool = Field(
        description=(
            "The viewer may edit it: its owner or an admin, the same rule as "
            "GET /profiles/{id}."
        )
    )


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
    # Whether TRANSFERRING this profile away from its current owner would cost
    # them their own read access, per transfer_read_effect (Task 1). D17: the
    # transfer UI must be able to say this BEFORE the act, so it lives here,
    # not only on the PUT /owner response. False for every reader who is not
    # the owner -- transfer_read_effect returns False in that case, since the
    # transfer takes nothing from someone who is not being transferred FROM.
    outgoing_lost_read: bool
    # The owner's UI has to render its own current state; the only alternative
    # would be guessing, or a second endpoint that does not exist. A Literal, not
    # a bare `str`, so the generated TS type is the union and a frontend that
    # compares against a third spelling fails to compile instead of silently
    # rendering the wrong branch.
    visibility: Literal["private", "org"]
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
    # FOR UPDATE, not a bare get. Without the lock two writers can both read
    # owner A, both pass the authorization check below, and the second then
    # computes its effects for an owner that no longer holds the profile.
    #
    # The lock lives here rather than in the transfer handler on purpose:
    # locking one caller would leave the other six racing it. The cost is that
    # SEVEN call sites inherit it, reaching SIXTEEN routes -- and FIVE of those
    # are reads, which now take an exclusive row lock they do not need:
    #
    # Named, not numbered: line citations here were invalidated twice by other
    # commits on this same branch. A name survives an edit above it.
    #
    #   GET  /profiles/{id}/viewers     list_profile_viewers        <- READ
    #   PUT  /profiles/{id}/viewers     replace_profile_viewers
    #   POST /profiles/{id}/viewers     add_profile_viewer
    #   PUT  /profiles/{id}/visibility  set_profile_visibility
    #   PUT  /profiles/{id}/owner       transfer_profile_owner
    #   POST /boards                    boards.py::create_board
    #   (via boards.py::_board_for_edit) 10 board routes, of which
    #        GET /boards/{id}, GET .../queue, GET .../items and
    #        GET .../auto-preview are READS
    #
    # That is deliberate, not an oversight: a board's edit permission IS its
    # profile's edit permission, so the read paths share the gate and cannot
    # be split from it without splitting the gate. Each route releases the lock
    # at the end of its `with sf() as session:` block.
    #
    # ONE of those routes holds it for a long time, and it is not free:
    # approve_items reaches here through _board_for_edit and then runs the QR
    # encode loop inside its own session, so this profile row is locked for that
    # whole batch -- bounded at ~8.7 s for one approve, and longer under
    # concurrency because qr.py's `_encode_lock` is process-wide. The reversal
    # is named and measured at that call site in web/api/boards.py; do not read
    # this helper as "nothing slow runs under the lock". It did once.
    #
    # NOT every profile mutation passes here. `update_profile` inlines this
    # same three-check ladder rather than calling this helper -- pinned that way
    # by tests/architecture/test_profile_access_single_source.py -- and since
    # 2026-10-31 takes the same FOR UPDATE itself (beta review F6: an edit used
    # to commit after a transfer had removed its author's access). Pinned by
    # test_an_edit_in_flight_holds_the_row_so_a_transfer_waits_for_it.
    #
    # If profile-scoped read latency ever matters, the fix is a separate
    # lock-free read gate, not removing this.
    p = session.execute(
        select(Profile).where(Profile.id == profile_id).with_for_update()
    ).scalar_one_or_none()
    if p is None or p.is_active is False:
        raise HTTPException(status_code=404, detail="profile not found")
    if not can_read_profile(session, user, p.id):
        raise HTTPException(status_code=404, detail="profile not found")
    if p.created_by != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not authorized")
    return p


def transfer_read_effect(session, *, profile: Profile, outgoing: User) -> bool:
    """True when `outgoing` will LOSE read access if `profile` leaves them.

    Computed, never asserted. `readable_profile_ids` short-circuits on
    `if user.is_admin` (profile_access.py:61) and returns every profile, so an
    admin previous owner loses nothing; `visibility='org'` and an explicit
    ProfileViewer row each keep access too. The decisions note records that an
    earlier revision stated the revocation unconditionally and was wrong, so the
    transfer UI must render THIS, not a fixed sentence.

    Asks the single-source predicate as if the transfer had already happened, by
    excluding only the owner arm -- which is the one arm the transfer removes.
    """
    if profile.created_by != outgoing.id:
        return False  # not the owner; the transfer takes nothing from them
    return not _readable_ignoring_ownership(session, outgoing, profile.id)


def _readable_ignoring_ownership(session, user: User, profile_id: int) -> bool:
    """`can_read_profile` with the owner arm neutralised -- and, as a side
    effect of HOW that is done, the profile_viewers arm too.

    `readable_profile_ids` has an admin arm, an owner arm, a `visibility ==
    'org'` arm and a profile_viewers arm (profile_access.py:61-73). Probing
    `can_read_profile` with a synthetic `id=-1` user correctly drops the owner
    arm -- `created_by == -1` can never match a real row -- but the SAME probe
    also drops the profile_viewers arm, because that arm is keyed on the same
    `user.id` (`ProfileViewer.user_id == -1` can never match either). The
    probe cannot neutralise one without the other; both happen to be indexed
    by identity.

    That is why the viewer row is re-asked for below, for the REAL user id:
    it puts back the one arm the probe took out along with ownership. This is
    a lookup of one row, not a second copy of the read rule -- it asks the
    exact question `readable_profile_ids` itself asks for this user
    (`ProfileViewer` matching this `profile_id` and this `user.id`), with no
    `or_`, no visibility comparison and no admin branch of its own, so there
    is nothing here for a rule to drift out of sync with.

    Implemented by asking the real predicate about a user whose id cannot match
    `created_by` -- rather than by rewriting the arms here, which
    tests/architecture/test_profile_access_single_source.py forbids and which is
    how a second read rule gets born.
    """
    probe = SimpleNamespace(id=-1, is_admin=user.is_admin)
    if can_read_profile(session, probe, profile_id):
        return True
    viewer = session.scalar(
        select(ProfileViewer.user_id).where(
            ProfileViewer.profile_id == profile_id,
            ProfileViewer.user_id == user.id,
        )
    )
    return viewer is not None


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
                is_owner=p.created_by == user.id,
                can_edit=p.created_by == user.id or user.is_admin,
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
        outgoing_lost_read = transfer_read_effect(session, profile=p, outgoing=user)
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
    return ProfileDetailOut(
        **core, **flat, can_edit=can_edit, outgoing_lost_read=outgoing_lost_read, **boot
    )


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
            select(Profile.id).where(
                Profile.name == body.name,
                Profile.is_active.is_(True),
                # Scoped to the caller, matching uq_profiles_active_owner_name.
                # The pre-check and the index must agree: a mismatch is how
                # this defect happened -- the handler and the schema each
                # enforced something, and neither enforced what :284 says.
                Profile.created_by == user.id,
            )
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
        # FOR UPDATE, for the reason _profile_for_edit gives: authorize and write
        # under the same row lock a transfer takes, so the two commit in one
        # order. An unlocked read let an edit authorized as owner commit AFTER a
        # transfer had removed that owner's access (beta review 2026-10-31 F6).
        # Held to the commit below; the embedding/backfill runs after it.
        p = session.execute(
            select(Profile).where(Profile.id == profile_id).with_for_update()
        ).scalar_one_or_none()
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
        old_rank_fp = rank_fingerprint(p)
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
        new_rank_fp = rank_fingerprint(p)
        rank_inputs_changed = old_rank_fp != new_rank_fp
        session.commit()

    # Schedule on the RANK fingerprint, not on `body.description`. The embed
    # inputs are a subset of the rank inputs, so this schedules a superset of
    # the EMBED scheduling: `refresh_embedding` can never fire without
    # `rank_inputs_changed`.
    #
    # It is NOT a superset of the old `body.description` guard, and deliberately
    # so: an identical PUT with a non-empty description used to schedule 90 days
    # of backfill and now schedules nothing (test_an_identical_put_schedules_nothing).
    # What it gains is the other direction -- an edit that changes only
    # `structured`, or that blanks the description, used to schedule nothing at
    # all and left the vector describing content the profile no longer has.
    #
    # This comparison is in-memory (`new`) against DB-loaded (`old`):
    # expire_on_commit=False (db/session.py:58) means committing does NOT
    # reload the row, so moving these lines cannot make the representations
    # match. It is safe only because the canonicalizer in
    # profiles/fingerprint.py normalizes numbers and omits empties.
    if rank_inputs_changed:
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
        target = session.scalar(select(User).where(User.email == email)) if email else None
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
    # Same union as the In model: the answer to a write is the same vocabulary as
    # the write, and the card derives its rendered state from this field.
    visibility: Literal["private", "org"]


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


class ProfileTransferIn(BaseModel):
    email: str
    keep_read_access: bool = False


class ProfileTransferOut(BaseModel):
    """`outgoing_lost_read` here is the REALIZED outcome of this transfer,
    computed after any `keep_read_access` row already exists -- unlike the
    same-named field on `GET /api/profiles/{id}` (`ProfileDetailOut`), which is
    the HYPOTHETICAL "would leaving cost the owner read" computed BEFORE the
    act, so a transfer with `keep_read_access=True` can return `False` here for
    a profile where the pre-transfer `GET` returned `True`.
    """

    owner: ProfileViewerOut
    outgoing_lost_read: bool


@router.put("/profiles/{profile_id}/owner", response_model=ProfileTransferOut)
def transfer_profile_owner(
    request: Request,
    profile_id: int,
    body: ProfileTransferIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Move a profile to another user. Owner-or-admin, via `_profile_for_edit`'s
    own check; `update_profile` inlines the same ladder rather than calling it.

    The admin arm is not a convenience: the case that motivated this is an owner
    who can no longer log in, so only an admin can act.

    Ownership is load-bearing in SEVEN places -- `readable_profile_ids`'s owner
    arm (web/profile_access.py), `_profile_for_edit`, `get_profile`,
    `update_profile`, `replace_profile_viewers`, `add_profile_viewer`, and
    `get_call_detail`'s profile resolution (web/api/calls.py) -- which is why
    this is one transaction and not four endpoints. Cited by name: the numeric
    version of this list was already approximate at the base commit (one entry
    pointed at a decorator, another at a parameter) and this branch shifted
    every one of them.

    Recipient eligibility is `status == 'active'` and nothing narrower: an
    unshared private profile has no viewer rows at all, so a
    viewers-only rule would make transfer impossible for exactly the case it
    exists for. The refusal is ONE generic 400 for unknown, inactive and
    ineligible alike -- mirroring add_profile_viewer's own generic-400 rationale
    (:667-669), not replace_profile_viewers' silent-drop shape (:600-601), so
    this is not a directory of who has an account.
    """
    sf = request.app.state.session_factory
    # The converter wraps the WHOLE `with` block, not just session.commit(),
    # and it stays INSIDE this function on purpose.
    #
    # Why a converter at all: migration 0076 put `created_by` INTO the
    # active-name unique key, so step 2's ownership change can now breach a
    # constraint it never could before -- the recipient may already own an
    # active profile with this name. Unhandled, the request 500s, and
    # 500-vs-200 is the private-profile-name oracle 0076 closed at creation,
    # re-served through this endpoint: POST a guessed name, PUT it at the
    # victim, read the status. The failing case rolls back, so the probe is
    # free and repeatable. The refusal is therefore the SAME generic 400 an
    # unknown, inactive or ineligible recipient gets -- a distinct message
    # would assert the oracle back into existence.
    #
    # BLUNTED, NOT CLOSED, and the difference matters. 400-vs-200 is still an
    # observable: create a profile named X, PUT it at the victim, and the status
    # tells you whether the victim owns an active profile named X. What this
    # removes is that the probe used to be FREE and REPEATABLE -- the 500 rolled
    # back, so it cost nothing. Now each probe costs the attacker the profile it
    # transferred away on a miss. The residual is inherent to putting created_by
    # in the unique key: any refusal at all is observable. The only true close is
    # to not refuse -- e.g. disambiguate the name on transfer -- which is a
    # product decision, not a bug fix.
    # tests/test_api_profile_name_privacy.py's gate 11 pins it.
    #
    # Why the whole block and not session.commit(): the breach surfaces at the
    # next AUTOFLUSH -- the subscription and viewer lookups below -- several
    # statements before any commit. Measured: `IntegrityError ... raised as a
    # result of Query-invoked autoflush ... UPDATE profiles SET created_by=`.
    # A try/except around commit() alone does NOT catch it.
    #
    # Why not extracted into a helper: tests/architecture/
    # test_profile_access_single_source.py pins THIS function by name as a
    # surface that must call the read predicate, and exempts it by name from
    # the ProfileViewer ban. Moving the body to a helper reddens both halves --
    # measured, 2 failed. The gate is right; the extraction was wrong.
    try:
        with sf() as session:
            p = _profile_for_edit(session, profile_id=profile_id, user=user)
            email = (body.email or "").strip().lower()
            target = session.scalar(select(User).where(User.email == email)) if email else None
            if target is None or target.status != "active":
                raise HTTPException(status_code=400, detail="could not transfer profile")

            # Never None: Profile.created_by is nullable=False with
            # ondelete="RESTRICT" (db/models/profiles.py:108-110, set at table
            # creation in migration 0003, never retrofitted onto existing rows),
            # so a referenced user row cannot be deleted out from under it.
            outgoing = session.get(User, p.created_by)

            # 1. D10's stale-row hazard: the incoming owner may already hold a
            # legitimate viewer row (add_profile_viewer only refuses one for
            # whoever is owner AT THAT MOMENT). Delete it so the ProfileViewer
            # invariant "the owner never has a row" (db/models/profiles.py:156-157)
            # stays true after the transfer.
            session.execute(
                delete(ProfileViewer).where(
                    ProfileViewer.profile_id == p.id,
                    ProfileViewer.user_id == target.id,
                )
            )

            # 2. Move ownership.
            p.created_by = target.id

            # 3. Give the new owner a subscription, mirroring
            # profiles/create.py:79 -- otherwise D9 notifies nobody. NEVER touch
            # the outgoing owner's row: digest/assemble.py:86-88 is explicit that
            # deleting it destroys their digest_enabled preference beyond a
            # re-share's ability to restore it, and assemble.py:97 already filters
            # the digest through readable_profile_ids, so their section simply
            # stops appearing.
            already_subscribed = session.scalar(
                select(Subscription.user_id).where(
                    Subscription.user_id == target.id,
                    Subscription.profile_id == p.id,
                )
            )
            if already_subscribed is None:
                session.add(Subscription(user_id=target.id, profile_id=p.id, digest_enabled=True))

            # 4. Open-6: keep the outgoing owner's read access if asked. Computed
            # AFTER step 2, so can_read_profile below asks the REAL post-transfer
            # predicate rather than a hypothetical. Guarded on outgoing.id !=
            # target.id: a self-transfer (recipient email == the current owner's
            # own address) would otherwise add a ProfileViewer row for someone who
            # is STILL the owner after step 2, breaking the invariant this whole
            # step exists to restore in step 1.
            if body.keep_read_access and outgoing.id != target.id:
                already_viewer = session.scalar(
                    select(ProfileViewer.user_id).where(
                        ProfileViewer.profile_id == p.id,
                        ProfileViewer.user_id == outgoing.id,
                    )
                )
                # Unlike the three `outgoing is not None` guards this function
                # dropped, this one STAYS: the owner-never-has-a-row invariant is
                # docstring-only (db/models/profiles.py:156-157), enforced by the
                # three writers agreeing and not by the database, so a row here is
                # unreachable-by-agreement rather than unreachable-by-constraint.
                if already_viewer is None:
                    session.add(ProfileViewer(profile_id=p.id, user_id=outgoing.id))

            # Open-7: a default_profile_id the outgoing owner can no longer read
            # must not keep pointing the dashboard at a profile they cannot open.
            outgoing_lost_read = not can_read_profile(session, outgoing, p.id)
            if outgoing_lost_read and outgoing.default_profile_id == p.id:
                outgoing.default_profile_id = None

            session.commit()
            owner_out = ProfileViewerOut(user_id=target.id, name=target.name, email=target.email)
    except IntegrityError as e:
        raise HTTPException(status_code=400, detail="could not transfer profile") from e

    return ProfileTransferOut(owner=owner_out, outgoing_lost_read=outgoing_lost_read)
