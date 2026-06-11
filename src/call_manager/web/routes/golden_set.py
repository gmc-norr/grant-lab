from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User
from call_manager.ranking.golden_set import (
    add_triple,
    candidates_cosine_disagreement,
    candidates_dismissed_high_score,
    delete_triple,
    list_triples,
)

router = APIRouter()


@router.get("/admin/golden-set", response_class=HTMLResponse)
def golden_set_page(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
) -> HTMLResponse:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        triples = list_triples(session)
        candidates = candidates_dismissed_high_score(session) + candidates_cosine_disagreement(
            session
        )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="golden_set.html",
        context={"user": user, "triples": triples, "candidates": candidates},
    )


@router.post("/admin/golden-set/add")
def golden_set_add(
    request: Request,
    call_id: int = Form(...),
    profile_id: int = Form(...),
    expected_score: int = Form(...),
    note: str = Form(""),
    user: User = Depends(require_admin),  # noqa: B008
):
    if not 0 <= expected_score <= 100:
        raise HTTPException(status_code=400, detail="expected_score must be 0-100")
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        add_triple(
            session,
            call_id=call_id,
            profile_id=profile_id,
            expected_score=expected_score,
            note=(note.strip() or None),
            created_by=user.id,
        )
        session.commit()
    return RedirectResponse(url="/admin/golden-set", status_code=303)


@router.post("/admin/golden-set/delete")
def golden_set_delete(
    request: Request,
    triple_id: int = Form(...),
    user: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        delete_triple(session, triple_id=triple_id)
        session.commit()
    return RedirectResponse(url="/admin/golden-set", status_code=303)
