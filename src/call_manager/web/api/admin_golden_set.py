from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User
from call_manager.ranking.golden_set import (
    add_triple,
    candidates_cosine_disagreement,
    candidates_dismissed_high_score,
    candidates_escalation_disagreement,
    delete_triple,
    list_triples,
)

router = APIRouter()


class TripleOut(BaseModel):
    id: int
    call_id: int
    profile_id: int
    expected_score: int
    note: str | None
    call_title: str
    profile_name: str


class CandidateOut(BaseModel):
    call_id: int
    profile_id: int
    call_title: str
    profile_name: str
    aggregate_score: int
    embed_similarity: float | None
    reason: str


class AdminGoldenSetOut(BaseModel):
    triples: list[TripleOut]
    candidates: list[CandidateOut]


class AddTripleIn(BaseModel):
    call_id: int
    profile_id: int
    expected_score: int
    note: str | None = None


class TripleCreatedOut(BaseModel):
    id: int


@router.get("/admin/golden-set", response_model=AdminGoldenSetOut)
def golden_set_read(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        triples = list_triples(session)
        candidates = (
            candidates_dismissed_high_score(session)
            + candidates_cosine_disagreement(session)
            + candidates_escalation_disagreement(session)
        )
    return AdminGoldenSetOut(
        triples=[TripleOut(**asdict(t)) for t in triples],
        candidates=[CandidateOut(**asdict(c)) for c in candidates],
    )


@router.post("/admin/golden-set", response_model=TripleCreatedOut, status_code=201)
def golden_set_add(
    request: Request,
    body: AddTripleIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if not 0 <= body.expected_score <= 100:
        raise HTTPException(status_code=400, detail="expected_score must be 0-100")
    note = body.note.strip() or None if body.note else None
    with request.app.state.session_factory() as session:
        try:
            triple_id = add_triple(
                session,
                call_id=body.call_id,
                profile_id=body.profile_id,
                expected_score=body.expected_score,
                note=note,
                created_by=admin.id,
            )
            session.commit()
        except IntegrityError as e:
            session.rollback()
            # Two constraints can fire here: the call/profile FKs vs
            # uq_golden_set_triples_pair. (The 0-100 CheckConstraint is pre-guarded
            # above.) Branch on SQLSTATE so a duplicate isn't mislabeled "unknown".
            orig = getattr(e, "orig", None)
            # `sqlstate or pgcode` mirrors the codebase precedent
            # (proposal_assist/drafting/locking.py) so this stays correct if the
            # DBAPI driver ever exposes the code under pgcode instead.
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
            if sqlstate == "23505":  # unique_violation -> duplicate (call, profile)
                raise HTTPException(
                    status_code=409, detail="triple already exists for this call and profile"
                ) from None
            if sqlstate == "23503":  # foreign_key_violation -> unknown call/profile
                raise HTTPException(status_code=400, detail="unknown call or profile") from None
            raise  # unexpected constraint -> sanitized 500
    return TripleCreatedOut(id=triple_id)


@router.delete("/admin/golden-set/{triple_id}", status_code=204)
def golden_set_delete(
    request: Request,
    triple_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        delete_triple(session, triple_id=triple_id)
        session.commit()
    return Response(status_code=204)
