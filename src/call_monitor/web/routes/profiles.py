from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Profile, ProfileKind, User
from call_monitor.embeddings.refresh import refresh_profile_embedding
from call_monitor.profiles.bootstrap import bootstrap_profile
from call_monitor.profiles.pubmed import PubMedClient
from call_monitor.profiles.seed import Member, SeedInput
from call_monitor.ranking.backfill import backfill_profile
from call_monitor.ranking.ollama_client import OllamaClient

router = APIRouter()

_logger = logging.getLogger(__name__)


@router.get("/profiles", response_class=HTMLResponse)
def profiles_index(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
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
    name: str = Form(...),
    kind: str = Form("group"),
    orcids: str = Form(""),
    names_affiliations: str = Form(""),
    pasted_grants: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="name too long (max 200 chars)")
    if len(pasted_grants) > 100_000:
        raise HTTPException(status_code=413, detail="pasted grants too large (max 100 KB)")
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        existing = session.execute(
            select(Profile.id).where(
                Profile.name == name, Profile.is_active.is_(True)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="profile name already exists")
    members = _parse_members(orcids, names_affiliations)
    if kind not in {k.value for k in ProfileKind}:
        kind = "group"
    seed = SeedInput(name=name, kind=kind, members=members, pasted_grants=pasted_grants)

    settings = request.app.state.settings
    pubmed = PubMedClient()
    ollama = OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model)
    draft = bootstrap_profile(seed=seed, pubmed=pubmed, ollama=ollama)

    with session_factory() as session:
        profile = Profile(
            name=name,
            kind=ProfileKind(kind),
            description=draft.description,
            structured=draft.structured,
            created_by=user.id,
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        new_id = profile.id

    # Embed the new profile sync so the next ingest tick (which enqueues jobs
    # against every active profile) sees a populated embedding. Best-effort:
    # on failure we log and proceed; the daily refresh-missing tick re-tries.
    embed_client = request.app.state.embed_client
    try:
        with session_factory() as s:
            refresh_profile_embedding(s, embed_client, new_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.embedding_refresh_failed",
            extra={
                "event": "profile_embedding_refresh_failed",
                "profile_id": new_id,
                "error": str(e),
            },
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
    description: str = Form(""),
    areas: str = Form(""),
    budget_min: str = Form(""),
    budget_max: str = Form(""),
    budget_currency: str = Form(""),       # NEW
    trl_min: str = Form(""),
    trl_max: str = Form(""),
    must_include: str = Form(""),
    hard_exclusions: str = Form(""),
    ranking_scheme: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(description) > 50_000:
        raise HTTPException(status_code=413, detail="description too long (max 50 KB)")
    session_factory = request.app.state.session_factory
    description_changed = False
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None:
            raise HTTPException(status_code=404, detail="profile not found")
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
            "budget_min": _int_or_none(budget_min),
            "budget_max": _int_or_none(budget_max),
            "trl_min": _int_or_none(trl_min),
            "trl_max": _int_or_none(trl_max),
            "must_include": _split_lines(must_include),
            "hard_exclusions": _split_lines(hard_exclusions),
        }
        # Validate currency. Allow only the documented set (or empty → NULL).
        # Whitelist enforcement here even though the dropdown only offers
        # these values — an attacker crafting a different POST gets dropped,
        # not stored.
        normalized = (budget_currency or "").strip().upper()
        if normalized in {"", "SEK", "EUR", "USD", "NOK", "DKK"}:
            p.budget_currency = normalized or None
        # Admin-only operational toggle. Silently ignore non-admin attempts —
        # the radio is only rendered for admins, but a researcher could still
        # craft a POST with this field. Drop the value rather than 400ing.
        if user.is_admin:
            requested = (ranking_scheme or "").strip().lower()
            if requested in {"v1", "v2"}:
                p.ranking_scheme = requested
        session.commit()

    # Re-embed sync if the description actually changed. Codex H1: this MUST
    # happen BEFORE backfill_profile() so the freshly-enqueued ranking jobs
    # see the new embedding (and not the stale one from the pre-edit
    # description). Skip when only the rubric (areas/must_include/etc.)
    # changed — those don't feed into the embedding text.
    if description_changed:
        embed_client = request.app.state.embed_client
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
            # Don't abort — backfill still runs, embedding gate becomes UNKNOWN
            # and the LLM runs as the safe fallback. Daily refresh tick re-tries.

    # Backfill is unconditional on every save (idempotent at DB level via
    # the partial unique index on (call_id, profile_id, scheme)). When
    # the admin flips ranking_scheme above, this re-enqueues the last 90
    # days of calls under the NEW scheme — no extra wiring needed.
    backfill_profile(session_factory=session_factory, profile_id=profile_id)

    return RedirectResponse(url=f"/profiles/{profile_id}", status_code=303)


_MAX_LIST_ENTRIES = 100


def _split_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip()][:_MAX_LIST_ENTRIES]


def _int_or_none(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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
