# Calls-Detail React Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the Jinja call-detail page (`GET /calls/{id}` + `POST /calls/{id}/hide`) to a React `/calls/:id` route driven by a typed `/api/calls/{id}` read-model, then delete the Jinja route/template/module.

**Architecture:** Strangler-fig, same as the cockpit slices — additive typed `/api` → React route under the existing AppShell → per-slice Jinja teardown. The detail read-model reuses the Jinja query verbatim; the three "why-string" helpers are relocated (byte-identical) to a neutral kept module first so nothing kept imports the doomed module. `/calls` becomes an SPA-owned **prefix** in the route manifest (the `/cockpit` precedent); the React `:id` lives only in the app's route tree.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (no schema change) · React 19 / Vite / TanStack Query / openapi-fetch · pytest (+ testcontainers Postgres) · vitest.

**Source spec:** `docs/superpowers/specs/2026-06-26-calls-detail-react-migration-design.md` (`ae43f9c`, Codex-revised).

## Global Constraints

- **No DB schema change. No new dependency.** Additive `/api` + one helper relocation, then teardown.
- **Model-name collisions:** the existing `web/api/calls.py` already defines `CallOut`/`RankingOut`, and the schema already contains a `CriterionOut`. ALL new detail models MUST be prefixed `CallDetail*` / `CallHide*` (`CallDetailOut`, `CallDetailCallOut`, `CallDetailRankingOut`, `CallDetailCriterionOut`, `CallDetailNoteOut`, `CallHideOut`). FastAPI auto-suffixes a collision to `CriterionOut1`, which silently breaks the TS types.
- **Errors:** raise `HTTPException(status_code=…, detail=…)`. The global handler (`main.py:333`) converts it to the `/api` envelope `{"error": {"code", "message"}}` for `/api` paths (`code_for(404)=="not_found"`, `400=="bad_request"`). NEVER hand-build an envelope here.
- **Int path params type as `number` in TS** (openapi-fetch). Hooks take `id: number`; the path is `"/api/calls/{call_id}"` with `params.path.call_id` a number.
- **Route manifest is prefix-not-`:param`.** The manifest entry is the prefix `/calls` (`frontend/routes.json` `spa_routes`), NEVER `/calls/:id`. Adding `/calls/:id` literally leaves `/calls/123` matching nothing → 404 after the Jinja route dies. The React `:id` route lives only in `router.tsx`.
- **Manifest + React route land together** (Task 6): `router.test.tsx` is a SET-EQUALITY drift guard — the `calls/:id` router route and the `/calls` manifest prefix must appear in the same commit or the guard fails.
- **Two regen scripts, distinct outputs:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` writes `frontend/src/api/schema.gen.ts` only (the `bge-m3` override avoids the mxbai `.env` default logging a WARNING to stdout that corrupts the codegen). `uv run python scripts/gen_routes.py` writes `frontend/src/routes.gen.ts` + `src/call_manager/web/spa_routes.py` only.
- **Ordering:** additive `/api` + React route + manifest `/calls` FIRST (Tasks 1–6); Jinja deletion + `backend_prefixes` `/calls` removal LAST (Task 7). While the Jinja route exists it WINS over the SPA catch-all (real route > fallback), so the manifest `/calls` is inert in prod mid-slice — no regression.
- **`/api` calls tests live in `tests/` ROOT** (calls are not workspace-scoped — the existing `tests/test_api_calls_feed_behavior.py` is at root), NOT under `tests/proposal_assist/`.
- **psycopg2 is NOT installed.** Test `DATABASE_URL`s use `postgresql+psycopg://` or the `pg_url` fixture; the gen-openapi script already uses a dummy `postgresql+psycopg://` URL.
- **Frontend has no TS linter — `tsc` (via `pnpm build`) is the gate.** Run `cd frontend && pnpm test` and `pnpm build`.
- **Subagent git discipline (detached SDD):** every dispatch must `cd <worktree>`, use `git -C <worktree>`, and `git rev-parse --abbrev-ref HEAD` to verify the branch before committing; use `git rm` for deletions. Subagents default to the MAIN-tree cwd.

---

### Task 1: Relocate the detail "why" helpers to a neutral kept module

Move `_parse_optional_profile_id`, `_criterion_detail`, `_money_range`, `_trl_range` out of the doomed `web/routes/calls.py` into a new kept module, byte-identical, and repoint the Jinja route's imports. Nothing changes behavior — the existing Jinja-detail tests prove it.

**Files:**
- Create: `src/call_manager/web/call_detail_view.py`
- Modify: `src/call_manager/web/routes/calls.py` (remove the 4 helper defs + now-unused `datetime` import; import them from the new module)
- Test: `tests/test_call_detail_view.py` (new unit test pinning a relocated helper)

**Interfaces:**
- Produces: `call_manager.web.call_detail_view._parse_optional_profile_id(raw: str | None) -> int | None`, `_criterion_detail(name: str, *, call: Call, profile: Profile, ranking: CallRanking) -> str | None`, `_money_range(lo: int | None, hi: int | None, currency: str) -> str`, `_trl_range(lo, hi) -> str`.

- [ ] **Step 1: Write the failing unit test**

Create `tests/test_call_detail_view.py`:

```python
from call_manager.web.call_detail_view import _money_range, _trl_range


def test_money_range_formats_thousands_with_currency():
    assert _money_range(10000, 500000, "EUR") == "10,000–500,000 EUR"


def test_money_range_unspecified():
    assert _money_range(None, None, "EUR") == "(unspecified)"


def test_trl_range_partial_dash():
    assert _trl_range(3, None) == "3–—"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_call_detail_view.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.web.call_detail_view'`.

- [ ] **Step 3: Create the new module (helpers moved verbatim)**

Create `src/call_manager/web/call_detail_view.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from call_manager.db.models import Call, CallRanking, Profile


def _parse_optional_profile_id(raw: str | None) -> int | None:
    """HTML <select> filters submit `?profile=` (empty string) when "all"
    is chosen. FastAPI's `int | None` declaration refuses to parse an
    empty string as int — so the param is accepted as `str | None` and
    we coerce here, treating empty/whitespace identically to omission.
    A non-empty non-integer still 400s, since that's an actual bad URL."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid profile id: {raw!r}") from e


def _criterion_detail(
    name: str, *, call: Call, profile: Profile, ranking: CallRanking
) -> str | None:
    """One-line "why" string for a deterministic gate criterion. Returns
    None for LLM criteria (topic_fit / eligibility / exclusions) — their
    rationale field already carries a free-text explanation.

    Generated at render time so profile/call edits made AFTER the last
    ranking are reflected immediately — the ratings are point-in-time
    snapshots, the displayed reasoning is "as the rubric looks today"."""
    p = profile.structured or {}
    c_struct = call.structured or {}
    if name == "timeline":
        if call.deadline is None:
            return "No deadline declared by source — treated as PASS."
        delta = call.deadline - datetime.now(UTC)
        days = delta.days
        if days < 0:
            return f"Deadline passed {-days} day(s) ago ({call.deadline.date()})."
        return f"Deadline {call.deadline.date()} — {days} day(s) away (minimum 14 day window)."
    if name == "budget":
        c_min, c_max = call.budget_min, call.budget_max
        p_min, p_max = p.get("budget_min"), p.get("budget_max")
        c_cur = (call.currency or "?").upper()
        p_cur = (profile.budget_currency or "?").upper()
        c_str = _money_range(c_min, c_max, c_cur)
        p_str = _money_range(p_min, p_max, p_cur)
        if c_min is None or c_max is None or p_min is None or p_max is None:
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        if c_cur != p_cur and "?" not in (c_cur, p_cur):
            return f"Call: {c_str} · Profile: {p_str} — currency mismatch."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "trl":
        c_min, c_max = c_struct.get("trl_min"), c_struct.get("trl_max")
        p_min, p_max = p.get("trl_min"), p.get("trl_max")
        c_str = _trl_range(c_min, c_max)
        p_str = _trl_range(p_min, p_max)
        if None in (c_min, c_max, p_min, p_max):
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "embedding":
        sim = ranking.embed_similarity
        if sim is None:
            return "No cosine — one or both vectors missing, gate UNKNOWN."
        return f"Cosine similarity {sim:.3f} (pass threshold 0.30)."
    return None


def _money_range(lo: int | None, hi: int | None, currency: str) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    lo_s = f"{lo:,}" if lo is not None else "—"
    hi_s = f"{hi:,}" if hi is not None else "—"
    return f"{lo_s}–{hi_s} {currency}"


def _trl_range(lo, hi) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    return f"{lo if lo is not None else '—'}–{hi if hi is not None else '—'}"
```

- [ ] **Step 4: Remove the moved code from the Jinja route module and import it**

In `src/call_manager/web/routes/calls.py`:
1. Delete the four function definitions (`_parse_optional_profile_id`, `_criterion_detail`, `_money_range`, `_trl_range`).
2. Delete the now-unused top import `from datetime import UTC, datetime` (the remaining route code does not use `datetime`; ruff will flag it as F401 otherwise).
3. Add, with the other `call_manager` imports:

```python
from call_manager.web.call_detail_view import (
    _criterion_detail,
    _money_range,
    _parse_optional_profile_id,
    _trl_range,
)
```

`_money_range` / `_trl_range` are imported because the module-level template context and the route still reference them transitively via `_criterion_detail`; keep all four imported so the route module is unchanged in behavior. (If ruff flags `_money_range`/`_trl_range` as unused in `calls.py`, drop those two from the import — `_criterion_detail` calls them from `call_detail_view.py`, not from `calls.py`. Let ruff decide.)

- [ ] **Step 5: Run the relocated-helper test + the existing Jinja-detail tests**

Run: `uv run pytest tests/test_call_detail_view.py tests/test_web_call_detail.py tests/test_web_calls_start_drafting.py -q`
Expected: PASS (relocation is behavior-preserving; the Jinja route now imports the helpers).

- [ ] **Step 6: ruff + boot check**

Run: `uv run ruff check src/call_manager/web/call_detail_view.py src/call_manager/web/routes/calls.py && uv run python -c "import call_manager.main"`
Expected: clean; boots.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/call_detail_view.py src/call_manager/web/routes/calls.py tests/test_call_detail_view.py
git commit -m "refactor(calls): relocate detail why-helpers to web/call_detail_view (kept module)"
```

---

### Task 2: `GET /api/calls/{call_id}` detail read-model

Add the typed detail endpoint to `web/api/calls.py`, mirroring the Jinja query exactly. Profile resolution keys off `is_active` ALONE (no ownership check — ownership gates only `best_profile_id`).

**Files:**
- Modify: `src/call_manager/web/api/calls.py`
- Test: `tests/test_api_call_detail.py` (new)

**Interfaces:**
- Consumes: `call_manager.web.call_detail_view._criterion_detail`, `_parse_optional_profile_id`; `call_manager.web.filters.criterion_label`, `model_label`, `note_category_heading`.
- Produces: route `GET /api/calls/{call_id}?profile=` → `CallDetailOut` with `call: CallDetailCallOut`, `ranking: CallDetailRankingOut | None`, `criteria: list[CallDetailCriterionOut]`, `best_profile_id: int | None`. (Pydantic field names are the contract for the TS hook in Task 4.)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_call_detail.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, email="u@ki.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call(pg_session_factory, *, title="CD"):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=title,
            url="https://x",
            title=title,
            summary="A summary.",
            published_at=datetime.now(UTC),
            keywords=["genomics", "oncology"],
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _seed_profile(pg_session_factory, *, owner_id, active=True, name="P"):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            is_active=active,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _seed_ranking(pg_session_factory, *, cid, pid, score=72, sim=0.42):
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=pid,
                aggregate_score=score,
                model="claude-sonnet-4-6",
                prompt_version="v1",
                embed_similarity=sim,
                rationale="Strong fit.",
            )
        )
        s.add(
            CallRankingCriterion(
                call_id=cid, profile_id=pid, criterion="embedding", rating="pass"
            )
        )
        s.commit()


def test_detail_no_profile_returns_call_without_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["call"]["id"] == cid
    assert body["call"]["keywords"] == ["genomics", "oncology"]
    assert body["call"]["summary"] == "A summary."
    assert body["ranking"] is None
    assert body["criteria"] == []
    assert body["best_profile_id"] is None


def test_detail_active_profile_returns_ranking_and_why(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ranking"]["aggregate_score"] == 72
    assert body["ranking"]["model_label"] == "Sonnet 4.6"
    emb = next(c for c in body["criteria"] if c["criterion"] == "embedding")
    assert emb["detail"] == "Cosine similarity 0.420 (pass threshold 0.30)."
    assert emb["is_reason_code"] is True
    assert body["best_profile_id"] == pid


def test_detail_active_not_owned_profile_still_shows_ranking(monkeypatch, pg_url, pg_session_factory):
    # P1b parity: ranking display keys off is_active ALONE, not ownership.
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    other_uid = _login(app, pg_session_factory, email="other@ki.se")[1]
    pid = _seed_profile(pg_session_factory, owner_id=other_uid, name="Other")
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    assert other_uid != uid
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"]["aggregate_score"] == 72
    # ...but best_profile_id (ownership-gated) stays None for the logged-in user.
    assert r.json()["best_profile_id"] is None


def test_detail_inactive_profile_no_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid, active=False)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_empty_profile_is_none(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_bad_profile_400(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=abc")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_detail_missing_call_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/calls/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_detail_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 401
```

Note: fix the deliberate typo in `test_detail_active_not_owned_profile_still_shows_ranking` — the `_setup` call must be `_setup(monkeypatch, pg_url)`. (Written here as a reminder to use `pg_url`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api_call_detail.py -q`
Expected: FAIL — `404`/`405` (no `GET /api/calls/{call_id}` route yet).

- [ ] **Step 3: Add imports + models + endpoint to `web/api/calls.py`**

Add to the imports block:

```python
from fastapi import HTTPException  # add to the existing fastapi import line
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from call_manager.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    User,  # already imported
)
from call_manager.web.call_detail_view import _criterion_detail, _parse_optional_profile_id
from call_manager.web.filters import criterion_label, model_label, note_category_heading
```

Add the response models (after `DashboardOut`):

```python
class CallDetailCallOut(BaseModel):
    id: int
    title: str
    source_name: str | None
    programme: str | None
    external_id: str | None
    published_at: datetime | None
    deadline: datetime | None
    budget_min: int | None
    budget_max: int | None
    currency: str | None
    url: str
    keywords: list[str]
    summary: str | None


class CallDetailNoteOut(BaseModel):
    category: str | None
    heading: str
    text: str


class CallDetailRankingOut(BaseModel):
    aggregate_score: int
    aggregate_reason_label: str | None
    model_label: str
    rationale: str
    embed_similarity: float | None
    escalated: bool
    escalated_from_score: int | None
    analysis_model_label: str
    summary: str | None
    applicant_notes: list[CallDetailNoteOut]


class CallDetailCriterionOut(BaseModel):
    criterion: str
    label: str
    rating: str
    is_topic_fit: bool
    detail: str | None
    rationale: str | None
    is_reason_code: bool
    quoted_evidence: str | None


class CallDetailOut(BaseModel):
    call: CallDetailCallOut
    ranking: CallDetailRankingOut | None
    criteria: list[CallDetailCriterionOut]
    best_profile_id: int | None
```

Add the endpoint:

```python
@router.get("/calls/{call_id}", response_model=CallDetailOut)
def get_call_detail(
    request: Request,
    call_id: int,
    profile: str | None = Query(default=None),
    user: User = Depends(get_current_user),  # noqa: B008
):
    profile_id = _parse_optional_profile_id(profile)  # empty -> None, bad -> 400
    sf = request.app.state.session_factory
    with sf() as session:
        call = session.execute(
            select(Call).options(joinedload(Call.source)).where(Call.id == call_id)
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")

        ranking = None
        criteria: list[CallRankingCriterion] = []
        profile_obj: Profile | None = None
        # Parity: resolve ?profile by is_active ALONE — NOT ownership (web/routes/calls.py:69-71).
        if profile_id is not None:
            p = session.get(Profile, profile_id)
            if p is not None and p.is_active:
                profile_obj = p
                ranking = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == call_id,
                        CallRanking.profile_id == profile_id,
                    )
                ).scalar_one_or_none()
                if ranking is not None:
                    criteria = list(
                        session.execute(
                            select(CallRankingCriterion).where(
                                CallRankingCriterion.call_id == call_id,
                                CallRankingCriterion.profile_id == profile_id,
                            )
                        ).scalars()
                    )

        # best-fit profile: highest-scoring profile OWNED by the current user.
        best_profile_id = session.scalar(
            select(CallRanking.profile_id)
            .join(Profile, Profile.id == CallRanking.profile_id)
            .where(CallRanking.call_id == call_id, Profile.created_by == user.id)
            .order_by(
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(1)
        )

        call_out = CallDetailCallOut(
            id=call.id,
            title=call.title,
            source_name=(call.source.name if call.source else None),
            programme=call.programme,
            external_id=call.external_id,
            published_at=call.published_at,
            deadline=call.deadline,
            budget_min=call.budget_min,
            budget_max=call.budget_max,
            currency=call.currency,
            url=call.url,
            keywords=list(call.keywords or []),
            summary=call.summary,
        )

        ranking_out = None
        if ranking is not None:
            notes = [
                CallDetailNoteOut(
                    category=n.get("category"),
                    heading=note_category_heading(n.get("category")),
                    text=n.get("text", ""),
                )
                for n in (ranking.applicant_notes or [])
            ]
            ranking_out = CallDetailRankingOut(
                aggregate_score=ranking.aggregate_score,
                aggregate_reason_label=(
                    criterion_label(ranking.aggregate_reason)
                    if ranking.aggregate_reason
                    else None
                ),
                model_label=model_label(ranking.model),
                rationale=ranking.rationale,
                embed_similarity=ranking.embed_similarity,
                escalated=ranking.escalated,
                escalated_from_score=ranking.escalated_from_score,
                analysis_model_label=model_label(ranking.analysis_model or ranking.model),
                summary=ranking.summary,
                applicant_notes=notes,
            )

        criteria_out = [
            CallDetailCriterionOut(
                criterion=c.criterion,
                label=criterion_label(c.criterion),
                rating=c.rating,
                is_topic_fit=(c.criterion == "topic_fit"),
                detail=(
                    _criterion_detail(c.criterion, call=call, profile=profile_obj, ranking=ranking)
                    if (profile_obj is not None and ranking is not None)
                    else None
                ),
                rationale=c.rationale,
                is_reason_code=(c.criterion in ("budget", "timeline", "trl", "embedding")),
                quoted_evidence=c.quoted_evidence,
            )
            for c in criteria
        ]

    return CallDetailOut(
        call=call_out, ranking=ranking_out, criteria=criteria_out, best_profile_id=best_profile_id
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_call_detail.py -q`
Expected: PASS (all 8).

- [ ] **Step 5: ruff**

Run: `uv run ruff check src/call_manager/web/api/calls.py`
Expected: clean. (If `_money_range`/`_trl_range` are reported unused anywhere, they were never imported here — only `_criterion_detail` + `_parse_optional_profile_id` are.)

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/calls.py tests/test_api_call_detail.py
git commit -m "feat(api): GET /api/calls/{id} detail read-model (ranking + why-strings + best_profile)"
```

---

### Task 3: `POST /api/calls/{call_id}/hide` + regenerate OpenAPI types

Add the idempotent hide action, then regenerate `schema.gen.ts` so the new `/api/calls/{call_id}` GET + hide POST (and `CallDetailOut`) are available to the TS client.

**Files:**
- Modify: `src/call_manager/web/api/calls.py`
- Modify (generated): `frontend/src/api/schema.gen.ts`
- Test: `tests/test_api_calls_hide.py` (new)

**Interfaces:**
- Produces: route `POST /api/calls/{call_id}/hide` → `CallHideOut {hidden: bool}`; schema paths `"/api/calls/{call_id}"` (GET) and `"/api/calls/{call_id}/hide"` (POST) in `schema.gen.ts`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_calls_hide.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User, UserCallDismissal
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id="H", url="https://x", title="H",
                 published_at=datetime.now(UTC))
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_hide_returns_hidden_true_and_creates_dismissal(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post(f"/api/calls/{cid}/hide")
    assert r.status_code == 200
    assert r.json() == {"hidden": True}
    with pg_session_factory() as s:
        row = s.execute(
            select(UserCallDismissal).where(
                UserCallDismissal.user_id == uid, UserCallDismissal.call_id == cid
            )
        ).scalar_one()
    assert row.dismissed_at is not None


def test_hide_is_idempotent(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide")
        r2 = client.post(f"/api/calls/{cid}/hide")
    assert r2.status_code == 200
    with pg_session_factory() as s:
        rows = s.execute(
            select(UserCallDismissal).where(
                UserCallDismissal.user_id == uid, UserCallDismissal.call_id == cid
            )
        ).scalars().all()
    assert len(rows) == 1


def test_hide_404_for_unknown_call(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/calls/999999/hide")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_hide_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(f"/api/calls/{cid}/hide")
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api_calls_hide.py -q`
Expected: FAIL — `404`/`405` (no hide route yet).

- [ ] **Step 3: Add the hide endpoint to `web/api/calls.py`**

Add imports:

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

from call_manager.db.models import UserCallDismissal  # add to the existing models import
```

Add the model + endpoint:

```python
class CallHideOut(BaseModel):
    hidden: bool


@router.post("/calls/{call_id}/hide", response_model=CallHideOut)
def hide_call(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        # ON CONFLICT DO NOTHING — idempotent if the user hides twice.
        session.execute(
            pg_insert(UserCallDismissal)
            .values(user_id=user.id, call_id=call_id)
            .on_conflict_do_nothing(index_elements=["user_id", "call_id"])
        )
        session.commit()
    return CallHideOut(hidden=True)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_api_calls_hide.py -q && uv run ruff check src/call_manager/web/api/calls.py`
Expected: PASS (4); ruff clean.

- [ ] **Step 5: Regenerate the OpenAPI TS types**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: `wrote frontend/src/api/schema.gen.ts`.

- [ ] **Step 6: Verify the schema gained the new paths + models and the frontend still compiles**

Run:
```bash
grep -c '"/api/calls/{call_id}"' frontend/src/api/schema.gen.ts
grep -c 'CallDetailOut' frontend/src/api/schema.gen.ts
cd frontend && pnpm build
```
Expected: both greps ≥ 1; `pnpm build` (tsc + vite) clean. (The Jinja `/calls/{call_id}` HTML path is ALSO still present — it drops in Task 7.)

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/api/calls.py tests/test_api_calls_hide.py frontend/src/api/schema.gen.ts
git commit -m "feat(api): POST /api/calls/{id}/hide (idempotent) + regen OpenAPI types"
```

---

### Task 4: Frontend hooks — `useCallDetail` + `useHideCall`

**Files:**
- Create: `frontend/src/api/calls.ts`
- Test: `frontend/src/api/calls.test.tsx`

**Interfaces:**
- Consumes: `schema.gen.ts` paths `"/api/calls/{call_id}"` (GET) + `"/api/calls/{call_id}/hide"` (POST); `api`, `unwrap` from `./client`.
- Produces: `useCallDetail(id: number, profile?: number | null)` (query key `["call", id, profile ?? null]`); `useHideCall()` (mutation `(id: number) => …`, invalidates `["calls"]` on success — navigation is the caller's job); type `CallDetail = components["schemas"]["CallDetailOut"]`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/calls.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCallDetail, useHideCall } from "./calls";

afterEach(() => vi.restoreAllMocks());

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const DETAIL = {
  call: {
    id: 5, title: "T", source_name: "Vinnova", programme: null, external_id: "X",
    published_at: null, deadline: null, budget_min: null, budget_max: null,
    currency: null, url: "https://x", keywords: [], summary: null,
  },
  ranking: null,
  criteria: [],
  best_profile_id: null,
};

it("useCallDetail GETs the detail path with the profile query and unwraps", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: DETAIL, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useCallDetail(5, 7), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(DETAIL);
  expect(get).toHaveBeenCalledWith("/api/calls/{call_id}", {
    params: { path: { call_id: 5 }, query: { profile: "7" } },
  });
});

it("useCallDetail omits the profile query when none is given", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: DETAIL, error: undefined } as never);
  const qc = new QueryClient();
  renderHook(() => useCallDetail(5, null), { wrapper: wrapper(qc) });
  await waitFor(() =>
    expect(get).toHaveBeenCalledWith("/api/calls/{call_id}", {
      params: { path: { call_id: 5 }, query: {} },
    }),
  );
});

it("useHideCall POSTs the hide path and invalidates ['calls']", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useHideCall(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync(5);
  expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calls"] });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/api/calls.test.tsx`
Expected: FAIL — cannot resolve `./calls`.

- [ ] **Step 3: Create the hooks**

Create `frontend/src/api/calls.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type CallDetail = components["schemas"]["CallDetailOut"];

// Detail read-model for /calls/:id. profile is the dashboard's effective profile,
// carried via ?profile= so the ranking shown matches the list. Keyed by both so
// switching profile refetches.
export function useCallDetail(id: number, profile?: number | null) {
  return useQuery({
    queryKey: ["call", id, profile ?? null],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/calls/{call_id}", {
          params: {
            path: { call_id: id },
            query: profile != null ? { profile: String(profile) } : {},
          },
        }),
      ),
  });
}

// Idempotent hide. Invalidates the dashboard list cache; the CALLER navigates
// back to "/" (keeps this hook free of Router context, so it unit-tests with
// only a QueryClientProvider).
export function useHideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) =>
      unwrap(await api.POST("/api/calls/{call_id}/hide", { params: { path: { call_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}
```

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/api/calls.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (3); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/calls.ts frontend/src/api/calls.test.tsx
git commit -m "feat(spa): useCallDetail + useHideCall hooks against /api/calls/{id}"
```

---

### Task 5: `CallDetail` route component

Render the detail sections at Jinja parity (score hero, analyst notes, details grid, keywords, summary, criterion breakdown, hide). Loading + not-found states.

**Files:**
- Create: `frontend/src/routes/CallDetail.tsx`
- Test: `frontend/src/routes/CallDetail.test.tsx`

**Interfaces:**
- Consumes: `useCallDetail`, `useHideCall` (Task 4); `Deadline` from `./Deadline`; `useParams`/`useSearchParams`/`useNavigate`/`Link` from `react-router-dom`.
- Produces: `export function CallDetail()` — registered at `calls/:id` in Task 6.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/CallDetail.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallDetail } from "./CallDetail";

afterEach(() => vi.restoreAllMocks());

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

function renderDetail(url = "/calls/5?profile=7") {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/calls/:id" element={<><CallDetail /><LocationProbe /></>} />
            <Route path="/" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

const RANKED = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    applicant_notes: [{ category: "budget", heading: "Budget & funding", text: "Note." }],
  },
  criteria: [
    {
      criterion: "embedding", label: "Topic similarity", rating: "pass", is_topic_fit: false,
      detail: "Cosine similarity 0.420 (pass threshold 0.30).", rationale: null,
      is_reason_code: true, quoted_evidence: null,
    },
  ],
  best_profile_id: 7,
};

it("renders the ranking hero, analyst notes, and criterion why-string", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toBeInTheDocument();
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
  expect(screen.getByText("Cosine similarity 0.420 (pass threshold 0.30).")).toBeInTheDocument();
});

it("a not-found error shows the not-found alert", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "not_found", message: "call not found" } },
  } as never);
  renderDetail();
  expect(await screen.findByRole("alert")).toHaveTextContent(/call not found/i);
});

it("Hide posts and navigates back to /", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  renderDetail();
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 } },
  }));
  await waitFor(() => expect(screen.getByTestId("loc").textContent).toBe("/"));
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/CallDetail.test.tsx`
Expected: FAIL — cannot resolve `./CallDetail`.

- [ ] **Step 3: Create the component**

Create `frontend/src/routes/CallDetail.tsx`:

```tsx
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall } from "../api/calls";
import { Deadline } from "./Deadline";

function budgetText(c: { budget_min: number | null; budget_max: number | null; currency: string | null }): string {
  if (c.budget_min == null && c.budget_max == null) return "—";
  const fmt = (n: number) => n.toLocaleString("en-US");
  const lo = c.budget_min != null ? fmt(c.budget_min) : "—";
  const hi = c.budget_max != null ? fmt(c.budget_max) : "—";
  return `${lo} – ${hi} ${c.currency ?? ""}`.trim();
}

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  const hide = useHideCall();
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <p role="alert">{notFound ? "Call not found." : "Couldn’t load this call."}</p>;
  }

  const { call, ranking, criteria, best_profile_id } = data;
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (best_profile_id != null) startParams.set("profile_id", String(best_profile_id));

  return (
    <main>
      <Link to="/" className="backlink">← All calls</Link>

      <header className="page-header">
        <hgroup>
          <h1>{call.title}</h1>
          <p>
            {call.source_name}
            {call.programme && <> · {call.programme}</>}
            {call.external_id && <> · <code>{call.external_id}</code></>}
          </p>
        </hgroup>
        <div className="actions">
          <Link to={`/cockpit/new?${startParams.toString()}`} role="button">
            Start drafting workspace
          </Link>
        </div>
      </header>

      {ranking && (
        <section aria-label="Ranking">
          <article className="score-hero">
            <div
              className={`figure ${
                ranking.aggregate_score >= 70 ? "strong" : ranking.aggregate_score > 0 ? "mid" : ""
              }`}
            >
              {ranking.aggregate_score}
            </div>
            <div className="body">
              <strong>Aggregate score</strong>
              {ranking.aggregate_reason_label && (
                <span className="gate-tag">{ranking.aggregate_reason_label}</span>
              )}
              <span className="gate-tag" title="LLM that produced this ranking">
                Analyzed by {ranking.model_label}
              </span>
              {ranking.rationale && !ranking.aggregate_reason_label && (
                <div className="rationale">{ranking.rationale}</div>
              )}
              {ranking.embed_similarity != null && (
                <div className="muted similarity">
                  Cosine similarity: {ranking.embed_similarity.toFixed(3)}
                </div>
              )}
              {ranking.escalated && ranking.escalated_from_score != null && (
                <div className="muted escalation">
                  Escalated from {ranking.escalated_from_score} (analysis by{" "}
                  {ranking.analysis_model_label}).
                </div>
              )}
            </div>
          </article>
        </section>
      )}

      {ranking && (ranking.summary || ranking.applicant_notes.length > 0) && (
        <section className="analysis-section" aria-label="Deep analysis">
          <h2>
            Analyst notes <small className="muted" style={{ fontWeight: 400 }}>— {ranking.model_label}</small>
          </h2>
          {ranking.summary && <p className="summary-paragraph">{ranking.summary}</p>}
          {ranking.applicant_notes.length > 0 && (
            <div className="analysis-notes">
              {ranking.applicant_notes.map((n, i) => (
                <article key={i} className={`analysis-note cat-${n.category ?? "other"}`}>
                  <h3>{n.heading}</h3>
                  <p>{n.text}</p>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      <section aria-label="Call details">
        <h2>Details</h2>
        <div className="detail-grid">
          <div>
            <span className="k">Published</span>
            <span className="v">{call.published_at ? call.published_at.slice(0, 10) : "—"}</span>
          </div>
          <div>
            <span className="k">Deadline</span>
            <span className="v">{call.deadline ? <Deadline iso={call.deadline} /> : "—"}</span>
          </div>
          <div>
            <span className="k">Budget</span>
            <span className="v">{budgetText(call)}</span>
          </div>
          <div>
            <span className="k">External link</span>
            <span className="v">
              <a href={call.url} target="_blank" rel="noopener">Open at source →</a>
            </span>
          </div>
        </div>
        {call.keywords.length > 0 && (
          <p className="keyword-list">
            <small className="muted">Keywords:</small>{" "}
            {call.keywords.map((kw) => (
              <span key={kw} className="chip">{kw}</span>
            ))}
          </p>
        )}
      </section>

      {call.summary && (
        <section>
          <h2>Summary</h2>
          <p>{call.summary}</p>
        </section>
      )}

      {criteria.length > 0 && (
        <section>
          <h2>Criterion breakdown</h2>
          <div className="criterion-list">
            {criteria.map((c) => (
              <div key={c.criterion} className="criterion-card">
                <div className="head">
                  <span>{c.label}</span>
                  <span>
                    <strong>{c.is_topic_fit ? `${c.rating}/5` : c.rating}</strong>
                  </span>
                </div>
                {c.detail && <div className="rationale">{c.detail}</div>}
                {c.rationale && (!c.detail || !c.detail.includes(c.rationale)) && (
                  <div className="rationale muted" style={{ fontSize: "0.9em" }}>
                    {c.is_reason_code ? (
                      <>Reason code: <code>{c.rationale}</code></>
                    ) : (
                      c.rationale
                    )}
                  </div>
                )}
                {c.quoted_evidence && <blockquote>“{c.quoted_evidence}”</blockquote>}
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="hide-call">
        <button
          type="button"
          className="secondary outline compact"
          onClick={async () => {
            await hide.mutateAsync(call.id);
            navigate("/");
          }}
        >
          Hide this call
        </button>
        <small className="muted">Removes it from your feed permanently. Other users are unaffected.</small>
      </section>
    </main>
  );
}
```

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/CallDetail.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (3); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/CallDetail.tsx frontend/src/routes/CallDetail.test.tsx
git commit -m "feat(spa): CallDetail route component (ranking hero, notes, criteria, hide)"
```

---

### Task 6: Wire the route + manifest prefix + dashboard link

Register `calls/:id` in the router, add the `/calls` SPA-ownership **prefix** to the manifest (regenerate), switch the `CallRow` title to an SPA `<Link>`, and update the route-ownership guard tests. The router route and the manifest prefix MUST land in this single commit (the `router.test.tsx` set-equality guard).

**Files:**
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/routes.json`
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Modify: `frontend/src/routes/CallRow.tsx`, `frontend/src/routes/CallRow.test.tsx`
- Modify: `tests/test_gen_routes.py`, `frontend/src/dev-proxy.test.ts`

**Interfaces:**
- Consumes: `CallDetail` (Task 5).
- Produces: SPA route `/calls/:id`; manifest `SPA_ROUTES` now includes `/calls`.

- [ ] **Step 1: Update the manifest and regenerate**

In `frontend/routes.json`, change the `spa_routes` line:

```json
  "spa_routes": ["/", "/cockpit", "/calls"],
```

Leave `backend_prefixes` UNCHANGED — `/calls` stays there because the Jinja `/calls` route still exists until Task 7. Then regenerate:

Run: `uv run python scripts/gen_routes.py`
Expected: prints `wrote spa_routes.py and routes.gen.ts: {'spa': ['/cockpit', '/calls', '/'], ...}` (sorted by length). `frontend/src/routes.gen.ts` `SPA_ROUTES` and `src/call_manager/web/spa_routes.py` `SPA_ROUTES` both now include `/calls`.

- [ ] **Step 2: Register the React route**

In `frontend/src/router.tsx`, add the import and the route child (after `cockpit/:id`, before the `"*"` catch-all):

```tsx
import { CallDetail } from "./routes/CallDetail";
```

```tsx
      { path: "calls/:id", element: <CallDetail /> },
```

(No `handle` — the detail page renders in the main outlet with no sidebar, like `cockpit/new`.)

- [ ] **Step 3: Switch the CallRow title to a Link and update its test**

In `frontend/src/routes/CallRow.tsx`, replace the title `<a>` (and fix the stale comment):

```tsx
        <Link
          className={styles.title}
          to={profileId != null ? `/calls/${call.id}?profile=${profileId}` : `/calls/${call.id}`}
        >
          {call.title}
        </Link>
```

Update the comment on line 8 from "to the un-ported Jinja detail page" to "to the React detail route". (`Link` is already imported.)

In `frontend/src/routes/CallRow.test.tsx`, update the first test and add a profile-threading assertion:

```tsx
it("links the title to the React detail route, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute("href", "/calls/3");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("threads the resolved profile into the detail link", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute(
    "href",
    "/calls/3?profile=7",
  );
});
```

- [ ] **Step 4: Update the route-ownership guard tests (the `/calls` ownership flip)**

In `tests/test_gen_routes.py`, in `test_is_spa_route_is_exact_not_prefix`, flip the `/calls/123` assertion to True (it is now an owned prefix; the root special-case is still pinned by `test_root_owns_only_itself`, which is unaffected):

```python
    assert is_spa_route("/") is True
    assert is_spa_route("/made-up-page") is False
    assert is_spa_route("/api/me") is False
    assert is_spa_route("/calls/123") is True  # /calls is now an SPA-owned prefix (calls-detail)
    assert is_spa_route("/login") is False
```

In `frontend/src/dev-proxy.test.ts`:
- In the "does NOT own backend or unknown paths" test, REMOVE the `expect(isSpaRoute("/calls/1")).toBe(false);` line.
- In the "owns the root / exactly" test, add `expect(isSpaRoute("/calls/1")).toBe(true);` and rename it to "owns the root and the /calls prefix".
- In the `shouldServeViaVite` test, change `expect(shouldServeViaVite("/calls/1")).toBe(false)` to `expect(shouldServeViaVite("/calls/1")).toBe(true); // owned detail route` and remove the "un-ported detail page -> uvicorn" comment.
- LEAVE the explicit-`["/"]`-manifest branch test (`isSpaRoute("/calls/1", ["/"])` / the `flipped` array) UNCHANGED — it pins the root special-case independent of the shipped manifest.

- [ ] **Step 5: Run the affected frontend + backend route tests**

Run:
```bash
cd frontend && pnpm exec vitest run src/router.test.tsx src/dev-proxy.test.ts src/routes/CallRow.test.tsx && pnpm exec tsc --noEmit
cd .. && uv run pytest tests/test_gen_routes.py -q
```
Expected: all PASS. `router.test.tsx` drift guard passes because the leaf `/calls/:id` collapses to the owning `/calls` prefix and `{"/", "/cockpit", "/calls"} == SPA_ROUTES`.

- [ ] **Step 6: Full frontend build + manifest sync check**

Run:
```bash
uv run python scripts/gen_routes.py --check
cd frontend && pnpm build
```
Expected: "route artifacts in sync with routes.json"; build clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py \
        frontend/src/router.tsx frontend/src/routes/CallRow.tsx frontend/src/routes/CallRow.test.tsx \
        tests/test_gen_routes.py frontend/src/dev-proxy.test.ts
git commit -m "feat(spa): own /calls in the route manifest + wire /calls/:id route + CallRow Link"
```

---

### Task 7: Jinja teardown — delete the detail route/template/module + repoint

Now that `/calls/:id` is fully live in React, delete the Jinja detail surface, remove `/calls` from `backend_prefixes`, regenerate both artifact sets, delete the three superseded Jinja tests, and repoint the auth tests that used `/calls` as a sample protected route.

**Files:**
- Modify: `src/call_manager/main.py` (drop the `calls` router import + `include_router`)
- Delete: `src/call_manager/web/routes/calls.py`, `src/call_manager/web/templates/call_detail.html`
- Modify: `frontend/routes.json` (remove `/calls` from `backend_prefixes`)
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`, `frontend/src/api/schema.gen.ts`
- Modify: `tests/test_gen_routes.py` (`is_backend_route` flip), `tests/test_web_auth.py` (repoint)
- Delete: `tests/test_web_call_detail.py`, `tests/test_web_calls_hide.py`, `tests/test_web_calls_start_drafting.py`

**Interfaces:**
- Produces: no bare `/calls/{id}` Jinja route; `BACKEND_PREFIXES` no longer contains `/calls`; `schema.gen.ts` no longer contains the Jinja `/calls/{call_id}` HTML path.

- [ ] **Step 1: Write/adjust the failing guard first (TDD on deletion)**

In `tests/test_gen_routes.py`, flip the backend-route assertion (it currently expects True):

```python
def test_is_backend_route_for_proxy_carveout():
    from call_manager.web.spa_routes import is_backend_route

    assert is_backend_route("/api/me") is True
    assert is_backend_route("/calls/123") is False  # /calls retired from backend_prefixes (calls-detail)
    assert is_backend_route("/app") is False
```

Run: `uv run pytest tests/test_gen_routes.py::test_is_backend_route_for_proxy_carveout -q`
Expected: FAIL (manifest still lists `/calls` in `backend_prefixes`).

- [ ] **Step 2: Remove `/calls` from `backend_prefixes` and regenerate routes**

In `frontend/routes.json`, drop `"/calls"` from `backend_prefixes`:

```json
  "backend_prefixes": ["/api", "/healthz", "/login", "/logout", "/register",
    "/auth", "/me", "/admin", "/profiles", "/workspaces", "/static"],
```

Run: `uv run python scripts/gen_routes.py`
Then: `uv run pytest tests/test_gen_routes.py -q`
Expected: regenerates `routes.gen.ts` + `spa_routes.py` (BACKEND_PREFIXES drops `/calls`; SPA_ROUTES unchanged); `test_gen_routes.py` PASS.

- [ ] **Step 3: Unmount + delete the Jinja module and template**

In `src/call_manager/main.py`:
- Remove `calls` from the `from call_manager.web.routes import (...)` import tuple.
- Remove the `app.include_router(calls.router)` line.
- Verify no other `calls.` reference remains: `grep -n "\bcalls\." src/call_manager/main.py` → no route references (the `web.routes.calls` module is gone).

Delete the files:

```bash
git rm src/call_manager/web/routes/calls.py src/call_manager/web/templates/call_detail.html
```

Run: `uv run python -c "import call_manager.main"`
Expected: boots (no import error).

- [ ] **Step 4: Add a route-absence guard + delete the three superseded Jinja tests**

Append to `tests/test_gen_routes.py` (or add a small new test file `tests/test_calls_jinja_surface_absent.py` using `pg_url`) a route-table assertion that no non-`/api` route under `/calls/` remains:

```python
def test_no_jinja_calls_route_remains(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/calls/{call_id}" not in paths
    assert "/calls/{call_id}/hide" not in paths
    # the /api detail + hide ARE present
    assert "/api/calls/{call_id}" in paths
    assert "/api/calls/{call_id}/hide" in paths
```

Delete the superseded Jinja tests (coverage moved to `tests/test_api_call_detail.py` + `tests/test_api_calls_hide.py` in Tasks 2–3; the Start-drafting link is covered by `CallDetail.test.tsx`):

```bash
git rm tests/test_web_call_detail.py tests/test_web_calls_hide.py tests/test_web_calls_start_drafting.py
```

- [ ] **Step 5: Repoint the auth tests off `/calls`**

In `tests/test_web_auth.py`, the three tests use `/calls/5` + `/calls/5/hide` ONLY as sample protected routes. Repoint to kept protected Jinja routes — `GET /me` (me.py:12, `get_current_user`, HTMLResponse) and `POST /me/name` (me.py:44, protected):

- `test_html_401_get_threads_next`: change the request to `GET /me?x=1` and the expected location to `"/login?next=%2Fme%3Fx%3D1"` (path+query is still threaded). Run the test and confirm the exact encoded location; adjust the expected string to match `safe_next` + `urlencode` if it normalizes the query.
- `test_html_401_post_does_not_thread_next`: change the request to `POST /me/name`; the expected location stays `"/login"` (a POST never threads `next`).
- `test_json_401_unaffected_by_next`: change the request to `GET /me` (no `Accept: text/html`); still asserts a JSON 401.

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS (adjust the one `next=` expected string if needed, then green).

- [ ] **Step 6: Regenerate the OpenAPI types (drop the Jinja HTML path)**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then verify the Jinja HTML path is gone but the `/api` paths remain:

```bash
grep -c '"/calls/{call_id}"' frontend/src/api/schema.gen.ts   # expect 0
grep -c '"/api/calls/{call_id}"' frontend/src/api/schema.gen.ts  # expect >=1
cd frontend && pnpm build
```
Expected: 0 / ≥1 / build clean.

- [ ] **Step 7: Run the slice's full backend + frontend suites**

Run:
```bash
uv run pytest tests/test_api_call_detail.py tests/test_api_calls_hide.py tests/test_call_detail_view.py \
              tests/test_gen_routes.py tests/test_web_auth.py tests/test_api_calls_feed_behavior.py -q
uv run ruff check src/call_manager/web/
cd frontend && pnpm test && pnpm build
```
Expected: all green; ruff clean; build clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(teardown): delete Jinja call-detail route/template/module; retire /calls backend prefix; repoint auth tests"
```

---

## Verification (whole-slice gate, before merge)

- `uv run python -c "import call_manager.main"` boots.
- Backend: `uv run pytest tests/test_api_call_detail.py tests/test_api_calls_hide.py tests/test_call_detail_view.py tests/test_gen_routes.py tests/test_web_auth.py tests/test_api_calls_feed_behavior.py -q` green; `uv run ruff check src/` clean. (Whole-suite collection `uv run pytest --collect-only -q` shows no orphaned imports from the deleted module/tests.)
- Frontend: `cd frontend && pnpm test` green; `pnpm build` (tsc + vite) clean.
- Manifest sync: `uv run python scripts/gen_routes.py --check` → in sync; `frontend/src/routes.gen.ts` `SPA_ROUTES` gained `/calls`, `BACKEND_PREFIXES` dropped `/calls`; `spa_routes.py` matches.
- Schema: `schema.gen.ts` has `/api/calls/{call_id}` (GET) + `/api/calls/{call_id}/hide` (POST) + `CallDetailOut`; the Jinja `/calls/{call_id}` HTML path is gone.
- Route-table: no non-`/api` route under `/calls/`.

## Self-Review

**Spec coverage:** §3.1 backend read-model → Task 2 (incl. P1b active-not-owned parity test). §3.1 hide → Task 3. Helper relocation → Task 1. §3.2 hooks → Task 4; `CallDetail` → Task 5; `CallRow` Link + manifest prefix → Task 6. §4 teardown + six test surfaces → Tasks 6 (gen_routes is_spa_route, dev-proxy, CallRow.test) + 7 (call_detail/hide/start_drafting deletes, auth repoint, gen_routes is_backend_route). §5 routing order + manifest-flip-with-deletion → Tasks 6/7 split (manifest add while Jinja exists = inert; backend_prefixes removal + deletion together in 7). §6 gates → per-task + whole-slice. No spec requirement is unmapped.

**Placeholder scan:** every code step carries complete code; commands have expected output. The one deliberately-flagged spot is the auth `next=` expected string (Task 7 Step 5) — the plan instructs running the test to confirm the exact encoding, which is the correct TDD posture for an env-derived value, not a placeholder.

**Type consistency:** Pydantic field names in Task 2 (`aggregate_reason_label`, `model_label`, `analysis_model_label`, `is_topic_fit`, `is_reason_code`, `best_profile_id`) match the TS usage in Task 5. Hook signatures `useCallDetail(id: number, profile?: number | null)` / `useHideCall()` (Task 4) match their call sites (Task 5). Model names are uniformly `CallDetail*`/`CallHide*` (collision-proof). Path template `"/api/calls/{call_id}"` is identical across schema, hooks, tests.
