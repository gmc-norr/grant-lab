from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Profile, ProfileKind, ProfileLevel, User
from call_manager.embeddings.refresh import refresh_profile_embedding
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.fields import (
    FieldValidationError,
    parse_structured,
    sanitize_structured,
)
from call_manager.profiles.pubmed import PubMedClient
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.backfill import backfill_profile
from call_manager.ranking.ollama_client import OllamaClient

router = APIRouter()

_logger = logging.getLogger(__name__)


@router.get("/profiles", response_class=HTMLResponse)
def profiles_index(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profiles = (
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            )
            .scalars()
            .all()
        )
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="profiles_index.html",
        context={"profiles": profiles, "user": user},
    )


@router.get("/profiles/new", response_class=HTMLResponse)
def profile_new_form(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="profile_new.html",
        context={"user": user},
    )


@router.post("/profiles/new")
def profile_new_submit(
    request: Request,
    background: BackgroundTasks,
    name: str = Form(...),
    kind: str = Form("organizational_unit"),
    level: str = Form(""),
    orcids: str = Form(""),
    names_affiliations: str = Form(""),
    pasted_grants: str = Form(""),
    mission_text: str = Form(""),
    # new structured inputs (validated via parse_structured)
    areas: str = Form(""),
    career_stage: str = Form(""),
    applicant_types: str = Form(""),
    role: str = Form(""),
    years_since_phd: str = Form(""),
    org_type: str = Form(""),
    capabilities: str = Form(""),
    patient_populations: str = Form(""),
    scale: str = Form(""),
    funding_instruments: str = Form(""),
    funder_exclusions: str = Form(""),
    country: str = Form(""),
    host_institution: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="name too long (max 200 chars)")
    if len(pasted_grants) > 100_000:
        raise HTTPException(status_code=413, detail="pasted grants too large (max 100 KB)")
    if kind not in {k.value for k in ProfileKind}:
        kind = ProfileKind.ORGANIZATIONAL_UNIT.value
    level_val: str | None = None
    if kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
        level_val = level.strip() or None
        if level_val is not None and level_val not in {x.value for x in ProfileLevel}:
            raise HTTPException(status_code=400, detail=f"unknown level: {level!r}")

    # Validate the new structured inputs up-front (before the LLM round-trip).
    try:
        user_structured = parse_structured(
            kind=kind,
            raw={
                "areas": areas,
                "career_stage": career_stage,
                "applicant_types": applicant_types,
                "role": role,
                "years_since_phd": years_since_phd,
                "org_type": org_type,
                "capabilities": capabilities,
                "patient_populations": patient_populations,
                "scale": scale,
                "funding_instruments": funding_instruments,
                "funder_exclusions": funder_exclusions,
                "country": country,
                "host_institution": host_institution,
            },
        )
    except FieldValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e

    session_factory = request.app.state.session_factory
    # Early bail-out for an existing active name — saves an expensive LLM
    # round-trip for the obvious duplicate case. The DB-level partial unique
    # index (uq_profiles_active_name) remains the source of truth and
    # catches the concurrent-submit race below.
    with session_factory() as session:
        existing = session.execute(
            select(Profile.id).where(Profile.name == name, Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="profile name already exists")
    members = _parse_members(orcids, names_affiliations)
    seed = SeedInput(
        name=name,
        kind=kind,
        level=level_val,
        members=members,
        pasted_grants=pasted_grants,
        mission_text=mission_text,
    )

    settings = request.app.state.settings
    pubmed = PubMedClient()
    ollama = OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model)
    draft = bootstrap_profile(seed=seed, pubmed=pubmed, ollama=ollama)

    # Merge: bootstrap draft is the base, but its LLM-suggested namespaces are
    # SANITIZED before they are ever persisted (parse_structured guards user
    # input; sanitize_structured guards machine input). User-entered values then
    # win over the sanitized suggestions.
    draft_clean = sanitize_structured(kind=kind, structured=draft.structured)
    merged = dict(draft.structured)
    merged["areas"] = user_structured["areas"] or draft.structured.get("areas", [])
    for ns in ("eligibility", "org", "funding", "geo"):
        base_ns = draft_clean.get(ns, {})
        user_ns = {k: v for k, v in user_structured.get(ns, {}).items() if v not in (None, [], "")}
        if base_ns or user_ns:
            merged[ns] = {**base_ns, **user_ns}

    try:
        with session_factory() as session:
            profile = Profile(
                name=name,
                kind=kind,
                level=level_val,
                description=draft.description,
                structured=merged,
                created_by=user.id,
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        # Lost the race with a concurrent submit of the same name. The
        # partial unique index rejected us; surface the same 409 the
        # pre-check would have.
        raise HTTPException(status_code=409, detail="profile name already exists") from e

    # Embed the new profile in a background task so the user gets a snappy
    # redirect instead of a 30s+ wait on Ollama. Best-effort: on failure
    # we log and the daily refresh-missing tick re-tries. This used to run
    # synchronously; an Ollama cold-start can take close to the reverse-proxy
    # timeout and was leaving the user on a hung submit.
    embed_client = request.app.state.embed_client
    background.add_task(
        _refresh_embedding_in_background,
        session_factory=session_factory,
        embed_client=embed_client,
        profile_id=new_id,
    )

    return RedirectResponse(url=f"/profiles/{new_id}/edit", status_code=303)


@router.get("/profiles/{profile_id}", response_class=HTMLResponse)
def profile_detail(
    request: Request,
    profile_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None:
            raise HTTPException(status_code=404, detail="profile not found")
        session.expunge(p)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="profile_detail.html",
        context={"profile": p, "user": user},
    )


@router.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
def profile_edit_form(
    request: Request,
    profile_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None:
            raise HTTPException(status_code=404, detail="profile not found")
        session.expunge(p)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="profile_edit.html",
        context={"profile": p, "user": user},
    )


@router.post("/profiles/{profile_id}/edit")
def profile_edit_submit(
    request: Request,
    profile_id: int,
    background: BackgroundTasks,
    description: str = Form(""),
    areas: str = Form(""),
    budget_min: str = Form(""),
    budget_max: str = Form(""),
    budget_currency: str = Form(""),  # NEW
    trl_min: str = Form(""),
    trl_max: str = Form(""),
    must_include: str = Form(""),
    hard_exclusions: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(description) > 50_000:
        raise HTTPException(status_code=413, detail="description too long (max 50 KB)")
    # Validate rubric inputs before opening a DB session so a rejection
    # doesn't leave the handler holding an unused connection. All bounds
    # return 400 (not 422) to match the legacy behavior on oversized
    # name/description.
    parsed_budget_min = _bounded_int(
        budget_min, field="budget_min", low=_BUDGET_MIN, high=_BUDGET_MAX
    )
    parsed_budget_max = _bounded_int(
        budget_max, field="budget_max", low=_BUDGET_MIN, high=_BUDGET_MAX
    )
    parsed_trl_min = _bounded_int(trl_min, field="trl_min", low=_TRL_MIN, high=_TRL_MAX)
    parsed_trl_max = _bounded_int(trl_max, field="trl_max", low=_TRL_MIN, high=_TRL_MAX)
    normalized_currency = (budget_currency or "").strip().upper()
    if normalized_currency not in _ALLOWED_CURRENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown currency: {budget_currency!r}",
        )
    session_factory = request.app.state.session_factory
    description_changed = False
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None:
            raise HTTPException(status_code=404, detail="profile not found")
        # Owner-or-admin policy. The profile `created_by` column records
        # the creator; admins can edit anyone's. Returning 403 (not 404)
        # is fine here — this is an internal hospital tool where profile
        # existence isn't a secret, and 403 is less confusing to the
        # legitimate user who mis-types a URL.
        if p.created_by != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")
        # Capture pre-mutation value for the change-detection below. Compare
        # on stripped form so whitespace-only edits don't trigger a needless
        # re-embed (build_profile_text strips anyway).
        old_description = p.description or ""
        p.description = description
        # Capture the change-detection flag at the moment of mutation so
        # intent isn't load-bearing on commit ordering — a future contributor
        # adding work between this assignment and the commit (or moving the
        # commit earlier) won't accidentally invalidate the comparison.
        description_changed = old_description.strip() != description.strip()
        p.structured = {
            "areas": _split_lines(areas),
            "budget_min": parsed_budget_min,
            "budget_max": parsed_budget_max,
            "trl_min": parsed_trl_min,
            "trl_max": parsed_trl_max,
            "must_include": _split_lines(must_include),
            "hard_exclusions": _split_lines(hard_exclusions),
        }
        p.budget_currency = normalized_currency or None
        session.commit()

    # Re-embed + backfill run in a background task so the user gets a snappy
    # redirect. Both used to block the request: refresh_profile_embedding
    # can hit a 120s Ollama timeout, and backfill_profile scans 90 days of
    # calls and enqueues one job per (call, profile). A single task drives
    # both so order is preserved — Codex H1 requires the embedding refresh
    # to complete BEFORE backfill enqueues ranking jobs (otherwise the
    # jobs see the stale pre-edit vector). Skip embedding refresh when the
    # rubric changed but the description didn't.
    embed_client = request.app.state.embed_client
    background.add_task(
        _refresh_and_backfill_in_background,
        session_factory=session_factory,
        embed_client=embed_client,
        profile_id=profile_id,
        refresh_embedding=description_changed,
    )

    return RedirectResponse(url=f"/profiles/{profile_id}", status_code=303)


def _refresh_embedding_in_background(*, session_factory, embed_client, profile_id: int) -> None:
    """Run refresh_profile_embedding outside the request lifecycle so a slow
    Ollama doesn't stall the user. Swallow all errors — the daily
    refresh-missing tick will retry, and the embedding gate treats a
    missing vector as UNKNOWN (safe fallback to LLM)."""
    try:
        with session_factory() as s:
            refresh_profile_embedding(s, embed_client, profile_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.embedding_refresh_failed",
            extra={
                "event": "profile_embedding_refresh_failed",
                "profile_id": profile_id,
                "error": str(e),
            },
        )


def _refresh_and_backfill_in_background(
    *, session_factory, embed_client, profile_id: int, refresh_embedding: bool
) -> None:
    """Background driver for the profile-edit path. Refresh first (if the
    description changed), then enqueue backfill. Order matters: the ranking
    jobs must see the post-edit vector, not the pre-edit one."""
    if refresh_embedding:
        _refresh_embedding_in_background(
            session_factory=session_factory,
            embed_client=embed_client,
            profile_id=profile_id,
        )
    try:
        backfill_profile(session_factory=session_factory, profile_id=profile_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.backfill_failed",
            extra={
                "event": "profile_backfill_failed",
                "profile_id": profile_id,
                "error": str(e),
            },
        )


_MAX_LIST_ENTRIES = 100
_ALLOWED_CURRENCIES = {"", "SEK", "EUR", "USD", "NOK", "DKK"}
# Sanity bounds on rubric inputs. Budget is in major units (SEK/EUR/etc.);
# 10**12 covers any realistic hospital grant with room to spare while
# rejecting obvious garbage. TRL is the 1–9 Technology Readiness Level
# scale.
_BUDGET_MIN = 0
_BUDGET_MAX = 10**12
_TRL_MIN = 1
_TRL_MAX = 9


def _split_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip()][:_MAX_LIST_ENTRIES]


def _bounded_int(raw: str, *, field: str, low: int, high: int) -> int | None:
    """Parse an optional integer, reject if out of [low, high].

    Empty input → None. Non-numeric input → None (UX choice: a user who
    types "n/a" in an optional numeric box shouldn't see a 400).
    Out-of-range numeric input → HTTPException, because that's the shape
    of an attack / garbage we don't want silently persisted."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if v < low or v > high:
        raise HTTPException(
            status_code=400,
            detail=f"{field} out of range ({low}..{high}): {v}",
        )
    return v


def _parse_members(orcids: str, names_affiliations: str) -> list[Member]:
    members: list[Member] = []
    for raw in orcids.splitlines():
        orcid = raw.strip()
        if orcid:
            with contextlib.suppress(ValueError):
                members.append(Member(orcid=orcid))
    for raw in names_affiliations.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name_part, aff_part = line.split("|", 1)
        name_part = name_part.strip()
        aff_part = aff_part.strip()
        if name_part and aff_part:
            with contextlib.suppress(ValueError):
                members.append(Member(name=name_part, affiliation=aff_part))
    return members
