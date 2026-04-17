from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Profile, ProfileKind, User
from call_monitor.profiles.bootstrap import bootstrap_profile
from call_monitor.profiles.pubmed import PubMedClient
from call_monitor.profiles.seed import Member, SeedInput
from call_monitor.ranking.ollama_client import OllamaClient

router = APIRouter()


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
    members = _parse_members(orcids, names_affiliations)
    if kind not in {k.value for k in ProfileKind}:
        kind = "group"
    seed = SeedInput(name=name, kind=kind, members=members, pasted_grants=pasted_grants)

    settings = request.app.state.settings
    pubmed = PubMedClient()
    ollama = OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model)
    draft = bootstrap_profile(seed=seed, pubmed=pubmed, ollama=ollama)

    session_factory = request.app.state.session_factory
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

    return RedirectResponse(url=f"/profiles/{new_id}/edit", status_code=303)


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
