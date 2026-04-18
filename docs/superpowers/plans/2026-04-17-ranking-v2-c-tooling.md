# Ranking v2 — Plan C: Compare View + Setwise + Rollout Instrumentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Depends on:** Plans A (`2026-04-17-ranking-v2-a-schema.md`) and B (`2026-04-17-ranking-v2-b-scoring.md`) must be merged first. Plan A ships the dual-scheme schema and scheme-aware queue. Plan B ships `rank_pair_v2` and the worker dispatch.

**Goal:** Ship the operational tooling that lets the admin run the spec's 4-week rollout safely. Adds (a) admin per-scheme observability (queue depth, active scheme per profile, weekly golden-set Kendall's tau), (b) `/admin/ranking-compare` side-by-side view for a chosen profile, (c) `setwise.py` daily borderline pass with APScheduler wiring, and (d) auto-backfill when an admin flips a profile's `ranking_scheme`.

**Architecture:** Three small surface-area additions: a new `ranking/setwise.py` module + APScheduler job, a `web/routes/admin.py` extension with a sibling `ranking_compare.html` template, and a hook in the profile-edit POST that triggers `backfill_profile` when `ranking_scheme` changed. Golden-set evaluation lives in a new `ranking/eval.py` and runs as a separate weekly APScheduler job. No schema changes (Plan A's `setwise_offset` column is already there).

**Tech Stack:** Python 3.12 · APScheduler · FastAPI + Jinja2 · SQLAlchemy 2.x · `pytest` + `testcontainers[postgres]` · stdlib `statistics` (Kendall's tau computed inline; no scipy)

---

## Design decisions (fix-now; push back if you disagree)

- **Setwise group size = 4, fixed.** Spec line 188 specifies `{1: +5, 2: +2, 3: -2, 4: -5}` offsets. Don't make it configurable in v2 — additional knobs invite drift.
- **Setwise runs daily at 04:00 Stockholm**, after ingestion (03:00) and before the workday. One run per profile per day. APScheduler job ID `setwise:daily`.
- **Setwise prompt asks for a JSON object wrapping the permutation** — `{"order": [<id>, <id>, <id>, <id>]}` — not a bare array. Reason: Ollama's `response_format={"type": "json_object"}` enforces an OBJECT shape; a bare array would either fail validation or push the model toward an object. We use the object wrapper and parse `data["order"]`. Without `response_format`, Gemma sometimes prepends prose explanations.
- **Setwise offsets are reset to zero for the entire profile at the start of each daily pass.** Without this, a call that drops out of the top-12 borderline band keeps its old offset forever, the displayed score (`aggregate_score + setwise_offset`) drifts from the aggregate, and skipped/remainder groups carry stale data. The pass-start UPDATE is one statement scoped to the profile; cheap.
- **Stable group membership under score ties.** Scores are integer; ties in the [45, 60) band are common. The candidate query orders by `(aggregate_score DESC, call_id ASC)` — `call_id` as a stable secondary key prevents Postgres from arbitrarily reordering tied rows between runs, which would otherwise cause daily setwise offsets to flap on the same calls.
- **Compare view is admin-only.** `/admin/ranking-compare` requires `user.is_admin`. Picks a profile via query string (`?profile=<id>`) — no profile picker UI in this plan; admin pastes the URL.
- **Profile-flip backfill is synchronous.** When admin POSTs `/profiles/<id>/edit` and `ranking_scheme` changed, the route calls `backfill_profile(...)` inline. Idempotent at the DB level (Plan A's partial unique index includes scheme), so over-trigger is safe. Same pattern as Plan 4's edit-triggers-backfill.
- **Kendall's tau is computed inline** with stdlib only — n is small (~20-50 golden-set pairs), no need for scipy. Helper lives in `ranking/eval.py`.
- **Golden-set eval is read-only.** Reads existing CallRankings, compares against expected bands, emits a structured-log summary. Plan A added the schema; Plan B writes the rows; this plan reports on them. No new DB writes.
- **`/admin` panel** gains three small sections: per-scheme queue depth, per-profile active scheme + last-rank-time, and the most-recent golden-set Kendall's tau per scheme. No drag-and-drop, no charts — just dt/dd lists matching the existing admin style.

---

## File structure created or modified by this plan

```
call-monitor/
├── src/call_monitor/
│   ├── ranking/
│   │   ├── setwise.py                              # NEW: borderline setwise pass
│   │   └── eval.py                                 # NEW: golden-set Kendall's tau
│   ├── scheduler.py                                # extend: register setwise:daily + golden_eval:weekly
│   ├── web/
│   │   ├── routes/
│   │   │   ├── admin.py                            # extend: per-scheme metrics +
│   │   │   │                                          ranking-compare endpoint
│   │   │   └── profiles.py                         # extend: flip → backfill_profile
│   │   └── templates/
│   │       ├── admin.html                          # extend: 3 new dl sections
│   │       └── ranking_compare.html                # NEW
└── tests/
    ├── test_ranking_setwise.py                     # NEW
    ├── test_ranking_eval.py                        # NEW
    ├── test_scheduler_v2_jobs.py                   # NEW (smoke: 2 new jobs registered)
    ├── test_web_admin_compare.py                   # NEW
    ├── test_web_admin_v2_metrics.py                # NEW (3 new dl sections)
    └── test_web_profiles.py                        # extend: flip triggers backfill
```

---

## Task 1: Per-scheme `/admin` metrics

**Files:**
- Modify: `src/call_monitor/web/routes/admin.py`
- Modify: `src/call_monitor/web/templates/admin.html`
- Create: `tests/test_web_admin_v2_metrics.py`

The existing `/admin` (per Plans 3 + 5) shows source health, ranking queue depth, and digest panel. Add three new dl blocks: per-scheme queue depth, per-profile scheme + last-rank-time, and last-known golden-set tau (placeholder until Task 6 lands).

- [ ] **Step 1: Test `tests/test_web_admin_v2_metrics.py`**

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    return create_app()


def _admin_client(app, pg_session_factory):
    with TestClient(app):  # trigger lifespan to bootstrap admin
        pass
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_shows_per_scheme_queue_depth(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="P")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="X", url="https://x", title="X",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v1",
                         status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v2",
                         status=RankingJobStatus.PENDING))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # Per-scheme queue depth section
    import re
    m = re.search(r"v1[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1
    m = re.search(r"v2[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1


def test_admin_shows_per_profile_scheme_and_last_rank_time(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="HasRanking")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="LR", url="https://x", title="LR",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=80, rationale="r", model="m",
            prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "HasRanking" in r.text
    assert "v2" in r.text  # active scheme rendered next to profile name
```

- [ ] **Step 2: Update `src/call_monitor/web/routes/admin.py`**

Read the existing handler. Extend the helper functions (matching the existing `_digest_panel_context` style) and update the route to merge them into the template context.

```python
from sqlalchemy import select, func
from call_monitor.db.models import CallRanking, Profile, RankingJob, RankingJobStatus


def _v2_queue_depth(session) -> dict[str, int]:
    rows = session.execute(
        select(RankingJob.scheme, func.count(RankingJob.id))
        .where(RankingJob.status.in_([RankingJobStatus.PENDING, RankingJobStatus.RUNNING]))
        .group_by(RankingJob.scheme)
    ).all()
    return {scheme: n for scheme, n in rows}


def _per_profile_scheme(session) -> list[dict]:
    """One row per active profile: id, name, ranking_scheme, last ranked_at across all rows."""
    rows = session.execute(
        select(
            Profile.id, Profile.name, Profile.ranking_scheme,
            func.max(CallRanking.ranked_at).label("last_ranked"),
        )
        .outerjoin(CallRanking, (CallRanking.profile_id == Profile.id) & (CallRanking.scheme == Profile.ranking_scheme))
        .where(Profile.is_active.is_(True))
        .group_by(Profile.id, Profile.name, Profile.ranking_scheme)
        .order_by(Profile.name)
    ).all()
    return [
        {"id": pid, "name": name, "scheme": scheme, "last_ranked": last}
        for pid, name, scheme, last in rows
    ]
```

In the existing admin route, after the existing `context.update(_digest_panel_context(session))`, add:

```python
        context["v2_queue_depth"] = _v2_queue_depth(session)
        context["per_profile_scheme"] = _per_profile_scheme(session)
```

- [ ] **Step 3: Extend `src/call_monitor/web/templates/admin.html`**

Append before `{% endblock %}`:

```html
<h2>Ranking — per-scheme queue depth</h2>
<dl>
  <dt>v1</dt><dd>{{ v2_queue_depth.get('v1', 0) }}</dd>
  <dt>v2</dt><dd>{{ v2_queue_depth.get('v2', 0) }}</dd>
</dl>

<h2>Profiles — active scheme</h2>
<table>
  <thead><tr><th>Profile</th><th>Scheme</th><th>Last ranked</th></tr></thead>
  <tbody>
  {% for p in per_profile_scheme %}
    <tr>
      <td><a href="/profiles/{{ p.id }}">{{ p.name }}</a></td>
      <td>{{ p.scheme }}</td>
      <td>{% if p.last_ranked %}{{ p.last_ranked | display_dt }}{% else %}—{% endif %}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_web_admin_v2_metrics.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/admin.py src/call_monitor/web/templates/admin.html tests/test_web_admin_v2_metrics.py
git commit -m "feat: /admin shows per-scheme queue depth + per-profile scheme"
```

---

## Task 2: `/admin/ranking-compare` side-by-side view

**Files:**
- Modify: `src/call_monitor/web/routes/admin.py`
- Create: `src/call_monitor/web/templates/ranking_compare.html`
- Create: `tests/test_web_admin_compare.py`

`GET /admin/ranking-compare?profile=<id>` queries CallRanking for the chosen profile filtered by both schemes, and renders them in a table where each row is a Call and the columns are v1 score / v2 score / |Δ| / v1 rationale / v2 summary.

- [ ] **Step 1: Test `tests/test_web_admin_compare.py`**

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Profile, Source, User
from call_monitor.main import create_app


def _setup_admin(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    app = create_app()
    with TestClient(app): pass  # trigger lifespan → bootstrap admin
    return app


def _admin_cookie(app, pg_session_factory):
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_compare_view_renders_dual_scheme_rows(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Compare profile")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="C", url="https://x", title="CompareCall",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 rationale",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=85, rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get(f"/admin/ranking-compare?profile={pid}")
    assert r.status_code == 200
    assert "CompareCall" in r.text
    assert "70" in r.text
    assert "85" in r.text
    assert "v1 rationale" in r.text
    assert "Topic 4/5" in r.text
    # Δ column
    assert "15" in r.text  # |85-70|


def test_compare_view_requires_admin(monkeypatch, pg_url, pg_session_factory):
    """Non-admin user gets 403 (or 401)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with pg_session_factory() as s:
        u = User(email="notadmin@ki.se"); s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/admin/ranking-compare?profile=1")
    assert r.status_code in (401, 403)


def test_compare_view_missing_profile_param_returns_400(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get("/admin/ranking-compare")
    assert r.status_code == 400
```

- [ ] **Step 2: Update `src/call_monitor/web/routes/admin.py`**

Append a route:

```python
from fastapi import HTTPException
from sqlalchemy.orm import aliased


@router.get("/admin/ranking-compare", response_class=HTMLResponse)
def ranking_compare(
    request: Request,
    profile: int | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    if profile is None:
        raise HTTPException(status_code=400, detail="profile query param required")
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        p = session.get(Profile, profile)
        if p is None:
            raise HTTPException(status_code=404, detail="profile not found")
        v1 = aliased(CallRanking)
        v2 = aliased(CallRanking)
        rows = session.execute(
            select(
                Call.id, Call.title, Call.url,
                v1.aggregate_score, v1.rationale,
                v2.aggregate_score, v2.rationale,
            )
            .select_from(Call)
            .join(v1, (v1.call_id == Call.id) & (v1.profile_id == profile) & (v1.scheme == "v1"),
                  isouter=True)
            .join(v2, (v2.call_id == Call.id) & (v2.profile_id == profile) & (v2.scheme == "v2"),
                  isouter=True)
            .where((v1.aggregate_score.is_not(None)) | (v2.aggregate_score.is_not(None)))
            .order_by(func.coalesce(v2.aggregate_score, v1.aggregate_score).desc())
            .limit(200)
        ).all()
        items = [
            {
                "call_id": cid,
                "title": title,
                "url": url,
                "v1_score": v1s,
                "v1_rationale": v1r,
                "v2_score": v2s,
                "v2_rationale": v2r,
                "delta": (abs(v1s - v2s) if v1s is not None and v2s is not None else None),
            }
            for cid, title, url, v1s, v1r, v2s, v2r in rows
        ]
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="ranking_compare.html",
        context={"profile_id": profile, "profile_name": p.name, "items": items, "user": user},
    )
```

- [ ] **Step 3: Create `src/call_monitor/web/templates/ranking_compare.html`**

```html
{% extends "base.html" %}
{% block title %}Ranking compare: {{ profile_name }}{% endblock %}
{% block content %}
<p><a href="/admin">← Admin</a></p>
<h1>Ranking compare: {{ profile_name }}</h1>
<p>Side-by-side v1 vs v2 ranking. Sorted by v2 score desc (falls back to v1 if v2 missing).</p>

<table>
  <thead>
    <tr>
      <th>Call</th>
      <th style="text-align:right">v1 score</th>
      <th style="text-align:right">v2 score</th>
      <th style="text-align:right">|Δ|</th>
      <th>v1 rationale</th>
      <th>v2 summary</th>
    </tr>
  </thead>
  <tbody>
    {% for it in items %}
      <tr>
        <td><a href="/calls/{{ it.call_id }}?profile={{ profile_id }}">{{ it.title }}</a></td>
        <td style="text-align:right">{% if it.v1_score is not none %}{{ it.v1_score }}{% else %}—{% endif %}</td>
        <td style="text-align:right">{% if it.v2_score is not none %}{{ it.v2_score }}{% else %}—{% endif %}</td>
        <td style="text-align:right">{% if it.delta is not none %}{{ it.delta }}{% else %}—{% endif %}</td>
        <td><small>{{ it.v1_rationale or '—' }}</small></td>
        <td><small>{{ it.v2_rationale or '—' }}</small></td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_web_admin_compare.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/admin.py src/call_monitor/web/templates/ranking_compare.html tests/test_web_admin_compare.py
git commit -m "feat: /admin/ranking-compare side-by-side v1 vs v2"
```

---

## Task 3: `setwise.py` — borderline-band setwise pass

**Files:**
- Create: `src/call_monitor/ranking/setwise.py`
- Create: `tests/test_ranking_setwise.py`

`run_setwise_for_profile(*, session_factory, client, profile_id, now)` gathers calls in the v2 borderline band [45, 60) for the given profile (capped at 12), partitions into groups of 4, prompts the LLM to rank each group, applies offsets `{1: +5, 2: +2, 3: -2, 4: -5}` to `setwise_offset` on each row.

- [ ] **Step 1: Test `tests/test_ranking_setwise.py`**

```python
import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.setwise import (
    SETWISE_OFFSETS,
    SETWISE_GROUP_SIZE,
    SETWISE_BORDERLINE_LOWER,
    SETWISE_BORDERLINE_UPPER,
    run_setwise_for_profile,
)


def _seed_profile_with_borderline_calls(s, *, scores: list[int]) -> tuple[int, list[int]]:
    u = User(email=f"sw-{len(scores)}@ki.se"); s.add(u); s.flush()
    p = Profile(
        name=f"SW-{len(scores)}", kind=ProfileKind.GROUP, description="Description",
        structured={"areas": [], "hard_exclusions": []},
        created_by=u.id, ranking_scheme="v2",
    )
    s.add(p); s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    cids: list[int] = []
    for i, score in enumerate(scores):
        c = Call(
            source_id=src.id, external_id=f"sw-{i}", url=f"https://x/{i}",
            title=f"Call {i}", summary="x",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=p.id, scheme="v2",
            aggregate_score=score, rationale="r",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        cids.append(c.id)
    return p.id, cids


def _setwise_client_returning(perm: list[int]):
    """Fake Ollama returning a permutation wrapped as {"order": [...]} —
    matches the actual contract enforced by response_format=json_object."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({"order": perm})},
        }],
    }
    def handler(req):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def _setwise_client_returning_bare_array(perm: list[int]):
    """Adversarial: model returns a bare array instead of {"order": [...]}.
    Should be rejected (parser only accepts object form)."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(perm)},
        }],
    }
    def handler(req):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_setwise_skipped_when_fewer_than_4_borderline(pg_session_factory):
    """With < 4 borderline calls, no setwise pass runs; offsets stay 0."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[50, 55, 58])
        s.commit()

    n_groups = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(cids),  # never called
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n_groups == 0
    with pg_session_factory() as s:
        offsets = [r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()]
    assert offsets == [0, 0, 0]


def test_setwise_applies_offsets_to_one_group_of_four(pg_session_factory):
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        s.commit()

    # LLM ranks: cids[0] best, cids[2] second, cids[3] third, cids[1] worst.
    perm = [cids[0], cids[2], cids[3], cids[1]]
    n_groups = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(perm),
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n_groups == 1

    expected = {
        cids[0]: SETWISE_OFFSETS[1],   # +5
        cids[2]: SETWISE_OFFSETS[2],   # +2
        cids[3]: SETWISE_OFFSETS[3],   # -2
        cids[1]: SETWISE_OFFSETS[4],   # -5
    }
    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        actual = {r.call_id: r.setwise_offset for r in rows}
    assert actual == expected


def test_setwise_drops_remainder_group_smaller_than_4(pg_session_factory):
    """5 borderline calls → 1 group of 4 + 1 remainder of 1; remainder is dropped."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 56, 54, 52, 50])
        s.commit()
    perm = [cids[0], cids[1], cids[2], cids[3]]  # only the first 4
    run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(perm),
        profile_id=pid, now=datetime.now(UTC),
    )
    with pg_session_factory() as s:
        rows = {r.call_id: r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()}
    # Remainder (cids[4]) is untouched.
    assert rows[cids[4]] == 0
    # First group has the offsets.
    assert rows[cids[0]] == SETWISE_OFFSETS[1]


def test_setwise_caps_borderline_at_12(pg_session_factory):
    """Spec line 187: cap at 12. So max 3 groups."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=list(range(46, 60)))
        s.commit()
    # 14 borderline calls; capped to 12; 3 groups of 4.
    # Use a permutation handler that just returns the same 4 IDs each time
    # — we only verify the group count and that offsets were applied.
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{"message": {"role": "assistant", "content": "[1,2,3,4]"}}],
    }
    # We need a more realistic handler that returns a valid permutation
    # of the 4 IDs the worker actually shows. Easier: return any 4 known IDs.
    def handler(req):
        # Echo back the first 4 IDs in JSON; even if they're "wrong",
        # the handler should at least apply offsets to whatever it returns.
        import json as _json
        # The prompt body contains the IDs — extract them.
        data = _json.loads(req.read().decode("utf-8"))
        # The user message contains the IDs as a list.
        user_text = data["messages"][-1]["content"]
        import re
        ids_in_prompt = [int(x) for x in re.findall(r"\b(\d+)\b", user_text)][:4]
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"role": "assistant",
                                     "content": _json.dumps(ids_in_prompt)}}],
        })
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )

    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client, profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 3  # 12 calls / 4 per group


def test_setwise_skips_group_on_invalid_permutation(pg_session_factory):
    """If LLM returns a non-permutation (wrong length, unknown IDs, parse fail),
    skip the group with a log and move on."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 56, 54, 52])
        s.commit()
    # Return a permutation of length 3 — invalid.
    bad_client = _setwise_client_returning([cids[0], cids[1], cids[2]])
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=bad_client, profile_id=pid, now=datetime.now(UTC),
    )
    # Group was attempted but skipped → return value reflects attempted, not applied.
    # Implementation choice: count == 0 (no offsets applied).
    assert n == 0
    with pg_session_factory() as s:
        offsets = {r.call_id: r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()}
    assert all(o == 0 for o in offsets.values())


def test_setwise_rejects_bare_array_response(pg_session_factory):
    """Codex H1 regression: model returning a bare JSON array instead of
    {"order": [...]} is rejected. Without the rejection, all groups would
    silently apply offsets from the wrong shape."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        s.commit()
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning_bare_array(cids),
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 0
    with pg_session_factory() as s:
        offsets = [r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()]
    assert all(o == 0 for o in offsets)


def test_setwise_resets_stale_offsets_at_pass_start(pg_session_factory):
    """Codex H2 regression: a call with a stale setwise_offset (left over
    from a previous pass) gets cleared at the start of the next pass —
    even if it's no longer in the borderline band, even if its current
    group is skipped, even if there's no new group at all."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        # Manually set stale offsets on all four calls (as if a previous
        # pass had ranked them). Also push one of them out of the borderline
        # band by raising its score to 70.
        for i, cid in enumerate(cids):
            rk = s.get(CallRanking, (cid, pid, "v2"))
            rk.setwise_offset = SETWISE_OFFSETS[i + 1]
        # Bump cids[0] above the borderline upper bound — it shouldn't
        # participate in this pass and its stale offset must clear.
        s.get(CallRanking, (cids[0], pid, "v2")).aggregate_score = 70
        s.commit()

    # Only 3 borderline calls remain → no group of 4 → setwise won't run any group.
    # But the reset should still fire and clear ALL stale offsets for the profile.
    def handler(req):
        raise AssertionError("LLM should not be called — < 4 borderline calls")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client, profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 0
    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        offsets = {r.call_id: r.setwise_offset for r in rows}
    # Every offset is back to 0, including the call that left the band.
    assert all(o == 0 for o in offsets.values()), offsets
```

- [ ] **Step 2: Implement `src/call_monitor/ranking/setwise.py`**

```python
"""Setwise borderline pass — phase 2 of ranking v2.

Per spec (Zhuang et al., SIGIR 2024), pointwise scoring is least
consistent in the borderline band where ordering matters most. We
group borderline calls into sets of 4 and ask the LLM to rank within
each set. Apply fixed offsets {1: +5, 2: +2, 3: -2, 4: -5} to
`setwise_offset`; final displayed score is `aggregate_score + setwise_offset`
clamped to [0, 100] (handled at render time).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select

from call_monitor.db.models import Call, CallRanking, Profile
from call_monitor.ranking.ollama_client import LLMError, OllamaClient

_logger = logging.getLogger(__name__)

SETWISE_BORDERLINE_LOWER = 45
SETWISE_BORDERLINE_UPPER = 60  # exclusive
SETWISE_GROUP_SIZE = 4
SETWISE_BORDERLINE_CAP = 12
SETWISE_OFFSETS = {1: 5, 2: 2, 3: -2, 4: -5}


def run_setwise_for_profile(
    *,
    session_factory,
    client: OllamaClient,
    profile_id: int,
    now: datetime,
) -> int:
    """Run setwise on the v2 borderline band for one profile.

    Returns the number of groups for which offsets were successfully applied.
    Skips entire pass if the profile has < 4 borderline calls; skips
    individual groups if the LLM's permutation is invalid.

    **Reset semantics:** at the start of every pass, all setwise_offset
    values for this profile's v2 rows are zeroed. Then offsets are
    re-applied for whichever groups the LLM successfully ranked in this
    run. This prevents stale offsets from accumulating on calls that
    dropped out of the borderline band (or were in skipped groups, or
    were dropped as remainder).

    **Stable group membership under ties:** the candidate query orders by
    (aggregate_score DESC, call_id ASC). Without the stable secondary
    key, Postgres reorders tied rows between runs and the same call can
    flip groups daily, making offsets flap.
    """
    # Pass-start reset. One UPDATE scoped to this profile's v2 rows.
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or profile.ranking_scheme != "v2":
            return 0
        from sqlalchemy import update
        session.execute(
            update(CallRanking)
            .where(
                CallRanking.profile_id == profile_id,
                CallRanking.scheme == "v2",
                CallRanking.setwise_offset != 0,
            )
            .values(setwise_offset=0)
        )
        session.commit()

    # Fetch borderline candidates with stable ordering.
    with session_factory() as session:
        rows = session.execute(
            select(Call.id, Call.title, Call.summary)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile_id,
                CallRanking.scheme == "v2",
                CallRanking.aggregate_score >= SETWISE_BORDERLINE_LOWER,
                CallRanking.aggregate_score < SETWISE_BORDERLINE_UPPER,
            )
            # Stable secondary key — prevents Postgres from arbitrarily
            # reordering tied scores between daily runs (Codex M3).
            .order_by(CallRanking.aggregate_score.desc(), Call.id.asc())
            .limit(SETWISE_BORDERLINE_CAP)
        ).all()

    if len(rows) < SETWISE_GROUP_SIZE:
        return 0

    # Partition into groups of 4; drop remainder.
    groups: list[list[tuple[int, str, str | None]]] = []
    for i in range(0, len(rows) - len(rows) % SETWISE_GROUP_SIZE, SETWISE_GROUP_SIZE):
        groups.append(rows[i : i + SETWISE_GROUP_SIZE])

    applied = 0
    for group in groups:
        perm = _ask_for_permutation(client, profile, group)
        if perm is None:
            continue
        with session_factory() as session:
            for rank, cid in enumerate(perm, start=1):
                offset = SETWISE_OFFSETS.get(rank, 0)
                rk = session.get(CallRanking, (cid, profile_id, "v2"))
                if rk is not None:
                    rk.setwise_offset = offset
            session.commit()
        applied += 1
    return applied


def _ask_for_permutation(
    client: OllamaClient,
    profile: Profile,
    group: list[tuple[int, str, str | None]],
) -> list[int] | None:
    """Ask the LLM to rank the calls. Return the permutation as a list of
    call_ids in best→worst order, or None on parse failure / wrong length /
    unknown IDs.

    Wraps the response in `{"order": [...]}` because Ollama's
    `response_format={"type": "json_object"}` enforces an OBJECT shape.
    A bare-array response would either fail at the provider or push the
    model toward an object anyway. Without response_format, Gemma sometimes
    prepends prose explanations.
    """
    ids = [cid for cid, _, _ in group]
    items = "\n".join(
        f"  ID {cid}: {title} — {(summary or '')[:200]}"
        for cid, title, summary in group
    )
    example = json.dumps({"order": ids})
    messages = [
        {
            "role": "system",
            "content": (
                f"You are a research-funding analyst evaluating calls for the profile "
                f"'{profile.name}': {profile.description or ''}\n\n"
                f"Rank the {len(ids)} calls below from MOST to LEAST relevant. "
                f'Output ONLY a JSON object of the form {{"order": [<id>, ...]}} '
                f"with the call IDs in best-to-worst order. Example: {example}. "
                f"No prose, no other keys."
            ),
        },
        {"role": "user", "content": f"CALLS:\n{items}"},
    ]
    try:
        envelope = client.chat(messages=messages, response_format={"type": "json_object"})
        content = envelope["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (LLMError, ValueError, KeyError, IndexError) as e:
        _logger.warning(
            "ranking.setwise.parse_failed",
            extra={"event": "setwise_parse_failed", "error": str(e)},
        )
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "ranking.setwise.not_an_object",
            extra={"event": "setwise_not_an_object", "got": data},
        )
        return None
    perm = data.get("order")
    if not isinstance(perm, list) or len(perm) != len(ids) or set(perm) != set(ids):
        _logger.warning(
            "ranking.setwise.invalid_permutation",
            extra={
                "event": "setwise_invalid_permutation",
                "expected_ids": ids,
                "got": perm,
            },
        )
        return None
    return [int(x) for x in perm]
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ranking_setwise.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/setwise.py tests/test_ranking_setwise.py
git commit -m "feat: setwise borderline pass for ranking v2"
```

---

## Task 4: APScheduler — register `setwise:daily` + `golden_eval:weekly`

**Files:**
- Modify: `src/call_monitor/scheduler.py`
- Modify: `src/call_monitor/main.py` (no changes needed — scheduler picks up new jobs from build_scheduler)
- Create: `tests/test_scheduler_v2_jobs.py`

The setwise daily job iterates active v2 profiles and calls `run_setwise_for_profile` for each. The golden-eval weekly job runs Task 6's evaluator. Both go through `build_scheduler`.

- [ ] **Step 1: Test `tests/test_scheduler_v2_jobs.py`**

```python
from unittest.mock import MagicMock

from call_monitor.scheduler import build_scheduler


class _NullEmail:
    def send(self, **kw): raise NotImplementedError


def _build(session_factory, *, with_digest=False):
    kwargs = dict(
        session_factory=session_factory,
        eu_portal_base_url="http://eu",
        vinnova_base_url="https://data.vinnova.se",
        alerter=MagicMock(),
        ollama_url="http://ollama",
        gemma_model="gemma4:e4b-it-q4_K_M",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
        kwargs["digest_settings"] = MagicMock(digest_dry_run=False, admin_dashboard_url="http://app/")
    return build_scheduler(**kwargs)


def test_scheduler_registers_setwise_daily(pg_session_factory):
    sched = _build(pg_session_factory)
    assert sched.get_job("setwise:daily") is not None
    sched.shutdown(wait=False)


def test_scheduler_registers_golden_eval_weekly(pg_session_factory):
    sched = _build(pg_session_factory)
    assert sched.get_job("golden_eval:weekly") is not None
    sched.shutdown(wait=False)
```

- [ ] **Step 2: Update `src/call_monitor/scheduler.py`**

Add imports:

```python
from call_monitor.ranking.setwise import run_setwise_for_profile
from call_monitor.ranking.eval import run_golden_set_eval
```

Inside `build_scheduler`, after the existing `rank:drain` registration:

```python
    def _setwise_tick() -> None:
        from datetime import UTC, datetime as _dt
        from sqlalchemy import select
        from call_monitor.db.models import Profile

        with session_factory() as session:
            v2_profile_ids = session.execute(
                select(Profile.id).where(
                    Profile.is_active.is_(True),
                    Profile.ranking_scheme == "v2",
                )
            ).scalars().all()

        now = _dt.now(UTC)
        for pid in v2_profile_ids:
            try:
                run_setwise_for_profile(
                    session_factory=session_factory,
                    client=ollama,
                    profile_id=pid,
                    now=now,
                )
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "ranking.setwise.profile_failed",
                    extra={"event": "setwise_profile_failed", "profile_id": pid, "error": str(e)},
                )

    sched.add_job(
        _setwise_tick,
        trigger=CronTrigger(hour=4, minute=0),
        id="setwise:daily",
        name="Daily setwise borderline pass",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    def _golden_tick() -> None:
        try:
            run_golden_set_eval(session_factory=session_factory)
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "ranking.golden_eval.failed",
                extra={"event": "golden_eval_failed", "error": str(e)},
            )

    sched.add_job(
        _golden_tick,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="golden_eval:weekly",
        name="Weekly golden-set evaluation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
```

(Note: `run_golden_set_eval` is implemented in Task 6 — temporarily this import will fail. That's OK — we add the eval module in Task 6 and these tests pass after Task 6 lands.)

To unblock Task 4 from depending on Task 6, write Task 6 BEFORE Task 4 (re-order if executing serially) OR use a lazy local import:

```python
    def _golden_tick() -> None:
        from call_monitor.ranking.eval import run_golden_set_eval
        try:
            run_golden_set_eval(session_factory=session_factory)
        ...
```

We use the lazy import so this task can land before Task 6.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_scheduler_v2_jobs.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/scheduler.py tests/test_scheduler_v2_jobs.py
git commit -m "feat: scheduler registers setwise:daily + golden_eval:weekly"
```

---

## Task 5: Admin scheme radio + flip auto-triggers backfill

**Files:**
- Modify: `src/call_monitor/web/routes/profiles.py` (POST `/profiles/<id>/edit` accepts `ranking_scheme` admin-only; existing `backfill_profile` call carries through to the new scheme)
- Modify: `src/call_monitor/web/templates/profile_edit.html` (admin-only radio appears here for the first time — Plan A intentionally deferred it)
- Modify: `tests/test_web_profiles.py`

The radio button for `ranking_scheme` (`v1` ↔ `v2`) is added HERE in Plan C, not in Plan A. Plan A added the column with default `'v1'` but left no UI to change it because (a) v2 didn't exist (Plan A's worker raises `NotImplementedError`), and (b) flipping under Plan A would hide v1 rankings via the read-path filters with no v2 rows to replace them. Plan B implemented `rank_pair_v2`. NOW it's safe to expose the radio.

When admin flips a profile from `ranking_scheme='v1'` to `'v2'`, the existing per-save `backfill_profile` call (Plan 4) re-enqueues the last 90 days of calls under the new scheme — `backfill_profile` reads `profile.ranking_scheme` after commit, so the new scheme is what gets enqueued. No extra wiring needed; just expose the radio and accept the form field.

- [ ] **Step 1: Failing test — append to `tests/test_web_profiles.py`**

```python
def test_profile_edit_flipping_scheme_enqueues_backfill(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Admin flips ranking_scheme=v2 → backfill_profile runs synchronously,
    enqueueing v2 jobs for the last 90 days of calls."""
    from datetime import UTC, datetime
    from sqlalchemy import select
    from fastapi.testclient import TestClient
    from call_monitor.auth.session_cookie import SessionSigner
    from call_monitor.db.models import Call, Profile, RankingJob, Source, User
    from call_monitor.main import create_app

    pid, _ = profile_factory(name="FlipMe")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(
            source_id=src.id, external_id="FB", url="https://x", title="T",
            published_at=datetime.now(UTC),
        ))
        s.commit()

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    app = create_app()
    with TestClient(app): pass  # bootstrap admin
    with pg_session_factory() as s:
        admin = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=admin.id))

    with client:
        client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "", "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
                "ranking_scheme": "v2",
            },
            follow_redirects=False,
        )

    # A v2 RankingJob should now exist for the seeded call.
    with pg_session_factory() as s:
        jobs = s.execute(
            select(RankingJob).where(
                RankingJob.profile_id == pid, RankingJob.scheme == "v2"
            )
        ).scalars().all()
    assert len(jobs) == 1
```

- [ ] **Step 2: Update `src/call_monitor/web/routes/profiles.py` — add `ranking_scheme` form handling**

Add `ranking_scheme: str = Form("")` to `profile_edit_submit`'s parameter list.

In the body, after the existing `budget_currency` block from Plan A Task 6, add the admin-only scheme handling:

```python
        # Admin-only operational toggle. Silently ignore non-admin attempts —
        # the radio is only rendered for admins, but a researcher could still
        # craft a POST with this field. Drop the value rather than 400ing.
        if user.is_admin:
            requested = (ranking_scheme or "").strip().lower()
            if requested in {"v1", "v2"}:
                p.ranking_scheme = requested
```

Also confirm that the existing `backfill_profile(session_factory=session_factory, profile_id=profile_id)` call (per Plan 4 / Plan A Task 2's update) is still present. It runs unconditionally on every save and reads `profile.ranking_scheme` AFTER commit — so flipping the scheme automatically re-enqueues the last 90 days under the new scheme without extra wiring. Add a one-line comment to make this intentional behavior explicit:

```python
        # Backfill is unconditional on every save (idempotent at DB level via
        # the partial unique index on (call_id, profile_id, scheme)). When
        # the admin flips ranking_scheme above, this re-enqueues the last 90
        # days of calls under the NEW scheme — no extra wiring needed.
        backfill_profile(session_factory=session_factory, profile_id=profile_id)
```

- [ ] **Step 3: Update `src/call_monitor/web/templates/profile_edit.html` — add the admin radio**

At the top of the form (before the description textarea is fine), conditionally render the radio:

```html
{% if user.is_admin %}
  <p>
    <label><strong>Ranking scheme (admin only)</strong></label><br>
    <label><input type="radio" name="ranking_scheme" value="v1" {% if profile.ranking_scheme == 'v1' %}checked{% endif %}> v1 (monolithic LLM score)</label>
    <label style="margin-left: 1em"><input type="radio" name="ranking_scheme" value="v2" {% if profile.ranking_scheme == 'v2' %}checked{% endif %}> v2 (criterion-rubric)</label>
    <br><small>Flipping schemes triggers a 90-day backfill under the new scheme. Existing rows under the old scheme are retained for audit.</small>
  </p>
{% endif %}
```

The route's GET handler that renders the edit template already passes `user` in the context (verify by reading the existing handler). If not, add it.

- [ ] **Step 4: Add a non-admin-can't-flip regression test — append to `tests/test_web_profiles.py`**

```python
def test_profile_edit_non_admin_cannot_flip_ranking_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Non-admin POST with ranking_scheme=v2 is silently ignored — scheme stays v1."""
    pid, _ = profile_factory(name="Scheme protect")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)  # creates a non-admin user
    with client:
        client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "", "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
                "ranking_scheme": "v2",  # attempted
            },
            follow_redirects=False,
        )
    from sqlalchemy import select
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.ranking_scheme == "v1"  # unchanged — non-admin's flip was dropped
```

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_web_profiles.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/profiles.py src/call_monitor/web/templates/profile_edit.html tests/test_web_profiles.py
git commit -m "feat: admin scheme radio + flip auto-triggers backfill"
```

---

## Task 6: Golden-set eval — Kendall's tau per scheme

**Files:**
- Create: `src/call_monitor/ranking/eval.py`
- Create: `tests/test_ranking_eval.py`

The golden set is a small fixture file (`tests/fixtures/golden_set.json`) of `(call_id, profile_id, expected_score, expected_band)` triples curated by humans. The evaluator queries CallRanking under both schemes, computes Kendall's tau between (sample_score, expected_score) for each scheme, and logs the result. No DB writes.

For Plan C, we ship the evaluator + a placeholder fixture with synthetic data — the real golden set is curated by hospital admins as part of rollout week 1.

- [ ] **Step 1: Test `tests/test_ranking_eval.py`**

```python
import json
from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_monitor.ranking.eval import (
    GoldenSetReport,
    kendalls_tau,
    run_golden_set_eval,
)


def test_kendalls_tau_perfect_correlation():
    """Identical orderings → tau = 1."""
    assert kendalls_tau([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0


def test_kendalls_tau_perfect_anticorrelation():
    """Reversed orderings → tau = -1."""
    assert kendalls_tau([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_kendalls_tau_uncorrelated_zero_centered():
    """Random orderings → tau near 0."""
    tau = kendalls_tau([10, 20, 30, 40, 50], [30, 10, 50, 20, 40])
    assert -0.5 < tau < 0.5


def test_kendalls_tau_handles_ties():
    """Ties shouldn't crash."""
    tau = kendalls_tau([1, 1, 2], [1, 2, 2])
    assert -1 <= tau <= 1


def test_run_golden_set_eval_against_seeded_rankings(pg_session_factory, tmp_path):
    """Build a golden set as a JSON file, seed CallRanking under both schemes,
    run eval, verify the report shape."""
    with pg_session_factory() as s:
        u = User(email="ge@ki.se"); s.add(u); s.flush()
        p = Profile(
            name="GE", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p); s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        cids = []
        # 4 calls with expected scores [80, 60, 40, 20].
        # v1 ranks them [85, 65, 35, 25] (perfect order).
        # v2 ranks them [70, 80, 30, 40] (worse).
        v1_scores = [85, 65, 35, 25]
        v2_scores = [70, 80, 30, 40]
        for i, (e, v1, v2) in enumerate(zip([80, 60, 40, 20], v1_scores, v2_scores)):
            c = Call(
                source_id=src.id, external_id=f"GE-{i}", url=f"https://x/{i}",
                title=f"GE {i}", published_at=datetime.now(UTC),
            )
            s.add(c); s.flush()
            cids.append(c.id)
            for scheme, score in [("v1", v1), ("v2", v2)]:
                s.add(CallRanking(
                    call_id=c.id, profile_id=p.id, scheme=scheme,
                    aggregate_score=score, rationale="r",
                    model="m", prompt_version=scheme,
                    ranked_at=datetime.now(UTC),
                ))
        s.commit()
        pid = p.id

    # Build a fixture file at tmp_path/golden.json
    golden = [
        {"call_id": cid, "profile_id": pid, "expected_score": e}
        for cid, e in zip(cids, [80, 60, 40, 20])
    ]
    fixture_path = tmp_path / "golden.json"
    fixture_path.write_text(json.dumps(golden))

    report = run_golden_set_eval(
        session_factory=pg_session_factory,
        fixture_path=str(fixture_path),
    )
    assert isinstance(report, GoldenSetReport)
    assert report.tau_v1 == 1.0  # perfect correlation
    assert report.tau_v2 < 1.0   # less perfect
    assert report.n_fixture == 4
    assert report.n_v1 == 4      # all 4 triples had v1 rows
    assert report.n_v2 == 4      # all 4 triples had v2 rows


def test_run_golden_set_eval_per_scheme_n_reflects_actual_rows(
    pg_session_factory, tmp_path
):
    """Codex M2: when triples are missing CallRanking rows for a scheme,
    n_<scheme> reports the count of rows actually compared, NOT the
    fixture size. Otherwise /admin would show tau computed on a much
    smaller subset than the displayed Pairs count."""
    with pg_session_factory() as s:
        u = User(email="m2@ki.se"); s.add(u); s.flush()
        p = Profile(
            name="M2", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p); s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        cids = []
        # Three calls in fixture, but only the first two have v2 rows.
        for i in range(3):
            c = Call(
                source_id=src.id, external_id=f"M2-{i}", url=f"https://x/{i}",
                title=f"M2 {i}", published_at=datetime.now(UTC),
            )
            s.add(c); s.flush(); cids.append(c.id)
        for cid in cids[:2]:
            s.add(CallRanking(
                call_id=cid, profile_id=p.id, scheme="v2",
                aggregate_score=50, rationale="r",
                model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
            ))
        s.commit()
        pid = p.id

    golden = [
        {"call_id": cid, "profile_id": pid, "expected_score": s}
        for cid, s in zip(cids, [80, 60, 40])
    ]
    fixture_path = tmp_path / "golden.json"
    fixture_path.write_text(json.dumps(golden))

    report = run_golden_set_eval(
        session_factory=pg_session_factory, fixture_path=str(fixture_path),
    )
    assert report.n_fixture == 3
    assert report.n_v1 == 0      # no v1 rows seeded
    assert report.tau_v1 is None
    assert report.n_v2 == 2      # only 2 of 3 fixture triples had v2 rows
    # tau_v2 computed over those 2 rows (could be -1, 0, or 1 with n=2;
    # we just confirm it's not None and the count is honest).
    assert report.tau_v2 is not None
```

- [ ] **Step 2: Implement `src/call_monitor/ranking/eval.py`**

```python
"""Golden-set evaluation — Kendall's tau between CallRanking scores and
human-curated expected scores, computed per scheme.

Read-only. Logs a structured summary; the admin panel surfaces the most
recent result (Task 7 — already wired via the v2 metrics block).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from call_monitor.db.models import CallRanking

_logger = logging.getLogger(__name__)

# From src/call_monitor/ranking/eval.py: parents[0] is `ranking/`, [1] is
# `call_monitor/`, [2] is `src/`, [3] is the repo root. Fixture lives at
# <repo>/tests/fixtures/golden_set.json — so parents[3], not [2].
_DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden_set.json"


@dataclass(frozen=True)
class GoldenSetReport:
    """Per-scheme golden-set evaluation result.

    `n_fixture` is the size of the curated triple list. `n_v1` and `n_v2`
    are the per-scheme counts of triples that actually had a CallRanking
    row to compare against — usually less than n_fixture if some pairs
    haven't been ranked under both schemes yet. tau is computed on
    n_<scheme> rows, NOT n_fixture, so the displayed tau and the count
    must agree (Codex M2)."""
    n_fixture: int
    n_v1: int
    tau_v1: float | None
    n_v2: int
    tau_v2: float | None


def kendalls_tau(xs: list[float], ys: list[float]) -> float:
    """Kendall's tau-b — handles ties. O(n²); fine for n ≤ ~100."""
    n = len(xs)
    if n < 2:
        return 0.0
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                ties_x += 1; ties_y += 1
            elif dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    n_pairs = n * (n - 1) // 2
    denom_x = ((n_pairs - ties_x) * (n_pairs - ties_y)) ** 0.5
    if denom_x == 0:
        return 0.0
    return (concordant - discordant) / denom_x


def run_golden_set_eval(
    *,
    session_factory,
    fixture_path: str | None = None,
) -> GoldenSetReport:
    """Read the golden-set fixture, query CallRanking under both schemes,
    compute Kendall's tau against expected scores. Logs the report and
    returns it.

    Reports per-scheme `n_v1` / `n_v2` separately from the curated fixture
    size (`n_fixture`) so the admin panel doesn't display "tau computed
    over 50 pairs" when only 12 of those 50 had a CallRanking under v2."""
    path = Path(fixture_path) if fixture_path else _DEFAULT_FIXTURE
    if not path.exists():
        _logger.warning(
            "ranking.golden_eval.fixture_missing",
            extra={"event": "golden_fixture_missing", "path": str(path)},
        )
        return GoldenSetReport(
            n_fixture=0, n_v1=0, tau_v1=None, n_v2=0, tau_v2=None,
        )

    triples = json.loads(path.read_text())
    n_fixture = len(triples)

    def _scores_for(scheme: str) -> tuple[list[float], list[float]]:
        """Returns the (actual, expected) lists for triples that have a
        CallRanking row under this scheme. May be empty."""
        actual: list[float] = []
        expected: list[float] = []
        with session_factory() as session:
            for t in triples:
                row = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == t["call_id"],
                        CallRanking.profile_id == t["profile_id"],
                        CallRanking.scheme == scheme,
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                actual.append(float(row.aggregate_score))
                expected.append(float(t["expected_score"]))
        return actual, expected

    v1_actual, v1_expected = _scores_for("v1")
    v2_actual, v2_expected = _scores_for("v2")
    tau_v1 = kendalls_tau(v1_actual, v1_expected) if len(v1_actual) >= 2 else None
    tau_v2 = kendalls_tau(v2_actual, v2_expected) if len(v2_actual) >= 2 else None

    report = GoldenSetReport(
        n_fixture=n_fixture,
        n_v1=len(v1_actual), tau_v1=tau_v1,
        n_v2=len(v2_actual), tau_v2=tau_v2,
    )
    _logger.info(
        "ranking.golden_eval.report",
        extra={
            "event": "golden_eval_report",
            "n_fixture": n_fixture,
            "n_v1": len(v1_actual), "tau_v1": tau_v1,
            "n_v2": len(v2_actual), "tau_v2": tau_v2,
        },
    )
    return report
```

- [ ] **Step 3: Stub fixture file**

Create `tests/fixtures/golden_set.json`:

```json
[]
```

(Empty list. The hospital admin curates the real golden set as part of rollout week 1; the empty file just lets the evaluator fail-open with `n_pairs=0` instead of crashing.)

- [ ] **Step 4: Verify and commit**

```bash
mkdir -p tests/fixtures
echo "[]" > tests/fixtures/golden_set.json
uv run pytest tests/test_ranking_eval.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/eval.py tests/fixtures/golden_set.json tests/test_ranking_eval.py
git commit -m "feat: golden-set Kendall's tau evaluator (per scheme)"
```

---

## Task 7: Surface latest golden-tau in `/admin`

**Files:**
- Modify: `src/call_monitor/web/routes/admin.py` (read latest tau from a small persisted record)
- Modify: `src/call_monitor/web/templates/admin.html`

The simplest persistence for "most recent golden eval" is a single-row table or a flat JSON file written by the weekly job. To avoid a new schema, the eval job writes a JSON file at `data/golden_set_last.json` (gitignored), and the admin route reads it.

- [ ] **Step 1: Modify `src/call_monitor/ranking/eval.py`**

Add a constant + write the report to disk after computation:

```python
import os

_REPORT_PATH = Path(os.environ.get("CALL_MONITOR_DATA_DIR", "data")) / "golden_set_last.json"


def _persist_report(report: GoldenSetReport) -> None:
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(json.dumps({
        "n_fixture": report.n_fixture,
        "n_v1": report.n_v1,
        "tau_v1": report.tau_v1,
        "n_v2": report.n_v2,
        "tau_v2": report.tau_v2,
    }))


def load_last_report() -> GoldenSetReport | None:
    """Read the most recent persisted report, or None if never run."""
    if not _REPORT_PATH.exists():
        return None
    data = json.loads(_REPORT_PATH.read_text())
    return GoldenSetReport(
        n_fixture=data.get("n_fixture", 0),
        n_v1=data.get("n_v1", 0),
        tau_v1=data.get("tau_v1"),
        n_v2=data.get("n_v2", 0),
        tau_v2=data.get("tau_v2"),
    )
```

In `run_golden_set_eval`, after computing `report`, call `_persist_report(report)`.

Add `data/` to `.gitignore` if not already.

- [ ] **Step 2: Surface in admin**

In `src/call_monitor/web/routes/admin.py`, in the route handler:

```python
        from call_monitor.ranking.eval import load_last_report
        context["golden_set_report"] = load_last_report()
```

In `src/call_monitor/web/templates/admin.html`, append below the per-scheme queue depth section:

```html
<h2>Golden-set Kendall's tau (last run)</h2>
{% if golden_set_report %}
  <dl>
    <dt>Curated triples</dt><dd>{{ golden_set_report.n_fixture }}</dd>
    <dt>v1 τ (n={{ golden_set_report.n_v1 }})</dt>
    <dd>{% if golden_set_report.tau_v1 is not none %}{{ '%.3f' % golden_set_report.tau_v1 }}{% else %}—{% endif %}</dd>
    <dt>v2 τ (n={{ golden_set_report.n_v2 }})</dt>
    <dd>{% if golden_set_report.tau_v2 is not none %}{{ '%.3f' % golden_set_report.tau_v2 }}{% else %}—{% endif %}</dd>
  </dl>
{% else %}
  <p>(golden eval not yet run)</p>
{% endif %}
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ranking_eval.py tests/test_web_admin_v2_metrics.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/eval.py src/call_monitor/web/routes/admin.py src/call_monitor/web/templates/admin.html .gitignore
git commit -m "feat: /admin surfaces latest golden-set Kendall's tau"
```

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -v` → all green (~360+ tests after Plans A + B + C)
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected"
4. Manual smoke (requires Ollama with `gemma4:e4b-it-q4_K_M`):
   - Visit `/admin` → see per-scheme queue depth, per-profile scheme, golden-tau (empty until job fires)
   - Flip a profile to v2 via `/profiles/<id>/edit` → backfill auto-runs → drain processes
   - Visit `/admin/ranking-compare?profile=<id>` → see v1/v2 side-by-side
   - Hand-fire setwise:
     ```python
     from call_monitor.ranking.setwise import run_setwise_for_profile
     # ...
     ```
   - Hand-fire golden eval:
     ```python
     from call_monitor.ranking.eval import run_golden_set_eval
     run_golden_set_eval(session_factory=session_factory)
     ```

## After this plan ships

The full v2 spec (`docs/superpowers/specs/2026-04-17-ranking-v2-criterion-design.md`) is implemented. The 4-week phased rollout from the spec can proceed:

- **Week 1** (already done by the time A+B+C ship) — code + schema in place; all profiles still on v1.
- **Week 2** — admin uses `/admin/ranking-compare` to inspect side-by-side; weekly golden-tau report decides if v2 is ready.
- **Week 3** — admin flips 1-2 profiles to v2 via `/profiles/<id>/edit`; auto-backfill seeds them.
- **Week 4** — admin flips remainder; the daily setwise pass takes over.

## Out of scope (deferred to follow-up specs in v2 design doc)

- Embedding second-opinion (SPECTER2 + SciNCL)
- Embedding pre-gate (depends on second-opinion landing first)
- Pre-LLM text pipeline (PDF / section segmentation)
- Profile drift detection
- User feedback loop with per-criterion granularity
