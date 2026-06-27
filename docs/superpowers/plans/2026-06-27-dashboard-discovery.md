# Dashboard Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the calls dashboard searchable and filterable — free-text search (`q`) and a badge-aligned score floor (`min_score`) on `GET /api/calls`, plus a "+N more in your dashboard →" breadcrumb in the weekly digest — so a researcher can reach the calls relevant to them, not just the top 5.

**Architecture:** `q` is an index-backed substring `ILIKE` (existing pg_trgm GIN indexes; no migration) appended to the read-model's shared `base_filters` so it narrows the list *and* the summary badges. `min_score` is a ranked-branch-only predicate (`CallRanking.aggregate_score >= N`) shared by the page query and its pagination count; it drops unranked (NULL-score) calls by construction. The digest computes a per-section dashboard-aligned `more_count` and renders a deep link to `?profile=&min_score=60`. The React SPA adds a debounced search box and a score-floor `Select`, both URL-bound through the existing `["calls", params]` query.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / PostgreSQL 16 (pg_trgm already enabled); React 18 + Vite, Mantine v9, `@mantine/hooks`, TanStack Query, `openapi-fetch`.

**Spec:** `docs/superpowers/specs/2026-06-27-dashboard-discovery-design.md` (read it; it carries the rationale and the resolved design forks).

## Global Constraints

Every task's requirements implicitly include these (copied verbatim from the spec):

- **Search semantics:** substring (`%term%`), **AND across whitespace-split terms**, **OR across four columns** `title`, `summary`, `full_text`, and `array_to_string(keywords,' ')`. Escape LIKE wildcards (`\` `%` `_`) so input matches literally; match with `.ilike(pat, escape="\\")`. Only `title`/`summary`/`full_text` are trigram-indexed; the keywords leg is an accepted unindexed scan.
- **Score floor:** applies **only** in the ranked branch (a profile is resolved). `CallRanking.aggregate_score >= min_score`; on the LEFT JOIN this drops unranked (NULL) calls — intended. It narrows the **page list and its `pagination.total`/`has_next`**, but **NOT** the three summary badges (`open_count`/`high_score`/`soon_deadline`), which keep describing the full (q-filtered) feed. Ignored when no profile is resolved.
- **Validation:** `q: str | None = Query(default=None, max_length=200)`; `min_score: int | None = Query(default=None, ge=0, le=100)`.
- **Frontend:** URL-as-source-of-truth; `updateParam` deletes `page` on every change. Sanitize both params in `dashboardParamsFrom` (trim `q` → `undefined` when empty; `min_score` accepted only when `Number.isFinite` and `0 ≤ n ≤ 100`, else `undefined`). Debounce search 300 ms via `useDebouncedValue` from `@mantine/hooks` (do NOT hand-roll). Score `Select` disabled when `data.effective.profile_id == null`. Every `Select` passes `comboboxProps={{ withinPortal: false }}`. Colors from `--app-*` tokens; **no bare element selectors** in `.module.css`. `tsc` is the type gate (no TS ESLint).
- **Digest:** `more_count` = `max(0, top_total − len(shown_top))`, top bucket only, render the breadcrumb only when `> 0`, deep-link `?profile={id}&min_score=60` (60 == `DIGEST_TOP_THRESHOLD`). It is a deliberately-generous dashboard-aligned signal, **not** an exact hidden-from-email count.
- **No DB migration** (no schema change). `schema.gen.ts` is regenerated **once, in Task 3** (after both backend params exist) via `pnpm gen:api` — Tasks 1–2 intentionally leave it stale; no frontend code consumes the params until Task 3.
- **Worktree/env note:** a fresh worktree has no `frontend/node_modules` (gitignored). Frontend tasks (3, 4) run `pnpm install` in the worktree's `frontend/` first; the OpenAPI regen also needs the Python venv. `pnpm gen:api` pins `EMBEDDING_MODEL=bge-m3` internally — do not override it.
- **Final gates (whole branch):** `ruff check .`, `pytest -v` (grouped dir runs if the whole suite flakes on testcontainers — see project memory), and in `frontend/`: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm run build`, and OpenAPI drift (`bash scripts/gen_openapi_types.sh --check` or re-run `pnpm gen:api` and `git diff --exit-code frontend/src/api/schema.gen.ts`).

---

### Task 1: Backend free-text search (`q`)

**Files:**
- Modify: `src/call_manager/web/dashboard.py` (add `_ilike_escape`/`_search_predicate` helpers; `q` param on `build_dashboard_view`; append to `base_filters`)
- Modify: `src/call_manager/web/api/calls.py:374-399` (add `q` Query param; pass through)
- Test: `tests/test_api_calls_feed_behavior.py` (add a search corpus seed + search tests)

**Interfaces:**
- Consumes: `build_dashboard_view(session, *, user_id, source, sort, profile_id, limit, page, show_closed)` (current signature, `dashboard.py:205-215`); the `base_filters` list (`dashboard.py:288-290`); imports `and_, func, or_, select` (already present, `dashboard.py:24`).
- Produces: `build_dashboard_view(..., q: str | None = None)` — a new trailing keyword-only param consumed by Task 2 (which adds `min_score` after it) and by `get_calls`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api_calls_feed_behavior.py` (after the existing `_seed_scored`/source tests; reuse the module's `_app`, `_login`, `_titles`, `select`, `Source`, `Call`, `datetime`, `UTC`, `timedelta` already imported at the top):

```python
# --- free-text search (q) ---------------------------------------------------


def _seed_search_corpus(pg_session_factory):
    """Five open Vinnova calls, each carrying a distinct search term in exactly
    one column, plus two wildcard-escape traps."""
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all(
            [
                Call(source_id=vn.id, external_id="S1", url="s1", title="Oncology research",
                     published_at=datetime(2026, 4, 1, tzinfo=UTC), deadline=now + timedelta(days=60)),
                Call(source_id=vn.id, external_id="S2", url="s2", title="Generic grant",
                     summary="Funds cancer immunotherapy work",
                     published_at=datetime(2026, 4, 2, tzinfo=UTC), deadline=now + timedelta(days=60)),
                Call(source_id=vn.id, external_id="S3", url="s3", title="Infra call",
                     full_text="Detailed body mentions proteomics methods",
                     published_at=datetime(2026, 4, 3, tzinfo=UTC), deadline=now + timedelta(days=60)),
                Call(source_id=vn.id, external_id="S4", url="s4", title="Keyword call",
                     keywords=["genomics", "sequencing"],
                     published_at=datetime(2026, 4, 4, tzinfo=UTC), deadline=now + timedelta(days=60)),
                Call(source_id=vn.id, external_id="S5", url="s5", title="Discount 50% bonus grant_id",
                     published_at=datetime(2026, 4, 5, tzinfo=UTC), deadline=now + timedelta(days=60)),
                Call(source_id=vn.id, external_id="S6", url="s6", title="Number 5012 grantXid plan",
                     published_at=datetime(2026, 4, 6, tzinfo=UTC), deadline=now + timedelta(days=60)),
            ]
        )
        s.commit()


def test_search_matches_title_substring(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        # substring, case-insensitive: "onco" matches "Oncology research".
        body = c.get("/api/calls", params={"q": "onco"}).json()
    assert _titles(body) == ["Oncology research"]


def test_search_matches_each_column(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        assert _titles(c.get("/api/calls", params={"q": "immunotherapy"}).json()) == ["Generic grant"]
        assert _titles(c.get("/api/calls", params={"q": "proteomics"}).json()) == ["Infra call"]
        assert _titles(c.get("/api/calls", params={"q": "genomics"}).json()) == ["Keyword call"]


def test_search_multi_term_is_and(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        # both terms live in S2's summary -> match
        assert _titles(c.get("/api/calls", params={"q": "cancer immunotherapy"}).json()) == ["Generic grant"]
        # no single call carries both terms -> empty
        assert _titles(c.get("/api/calls", params={"q": "oncology immunotherapy"}).json()) == []


def test_search_escapes_like_wildcards(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        # "50%" must match the literal "50%" (S5), NOT "50" anywhere (S6 "5012").
        assert _titles(c.get("/api/calls", params={"q": "50%"}).json()) == ["Discount 50% bonus grant_id"]
        # "_" must be literal too: "grant_id" matches S5, not S6's "grantXid".
        assert _titles(c.get("/api/calls", params={"q": "grant_id"}).json()) == ["Discount 50% bonus grant_id"]


def test_blank_search_is_noop(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        assert len(c.get("/api/calls", params={"q": "   "}).json()["items"]) == 6


def test_search_too_long_is_422(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, *_ = _login(app, pg_session_factory)
    with c:
        assert c.get("/api/calls", params={"q": "x" * 201}).status_code == 422


def test_search_narrows_summary_badges(monkeypatch, pg_url, pg_session_factory):
    """q narrows the summary badges too (it lives in base_filters)."""
    app = _app(monkeypatch, pg_url)
    _seed_search_corpus(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls", params={"q": "oncology"}).json()
    assert body["summary"]["open_count"] == 1  # only "Oncology research" matches
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_api_calls_feed_behavior.py -v -k "search"`
Expected: FAIL — the seven `test_search_*` cases return all rows / 200 (no `q` filter yet); `test_search_too_long_is_422` returns 200 not 422.

- [ ] **Step 3: Add the search helpers and param in `dashboard.py`**

Add module-level helpers (place them just above `build_dashboard_view`, after the dataclasses / constants):

```python
def _ilike_escape(term: str) -> str:
    """Escape LIKE wildcards so a user search term matches literally. ``\\`` is
    the escape char passed to ``.ilike(..., escape="\\\\")``."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_predicate(q: str):
    """Substring free-text match: AND across whitespace-split terms, OR across
    title/summary/full_text/keywords. Index-backed (pg_trgm) on the first three;
    the keywords leg is an accepted unindexed expression scan."""
    keywords_str = func.array_to_string(Call.keywords, " ")
    term_clauses = []
    for term in q.split():
        pat = f"%{_ilike_escape(term)}%"
        term_clauses.append(
            or_(
                Call.title.ilike(pat, escape="\\"),
                Call.summary.ilike(pat, escape="\\"),
                Call.full_text.ilike(pat, escape="\\"),
                keywords_str.ilike(pat, escape="\\"),
            )
        )
    return and_(*term_clauses)
```

Add `q` to the signature (`dashboard.py:205-215`) as a trailing keyword-only param:

```python
    show_closed: bool,
    q: str | None = None,
) -> DashboardView:
```

Append the predicate to `base_filters` (right after the `source_id` filter, `dashboard.py:288-290`):

```python
    base_filters = [Call.id.notin_(dismissed_subq)]
    if source_id is not None:
        base_filters.append(Call.source_id == source_id)
    if q and q.strip():
        base_filters.append(_search_predicate(q.strip()))
```

- [ ] **Step 4: Add the `q` param to the endpoint (`calls.py`)**

In `get_calls` (`calls.py:374-399`), add the query param and thread it through:

```python
    show_closed: bool = Query(default=False),
    q: str | None = Query(default=None, max_length=200),
    user: User = Depends(get_current_user),  # noqa: B008
):
```

```python
        view = build_dashboard_view(
            session,
            user_id=user.id,
            source=source,
            sort=sort,
            profile_id=profile_id,
            limit=limit,
            page=page,
            show_closed=show_closed,
            q=q,
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_api_calls_feed_behavior.py -v -k "search"`
Expected: PASS (7 tests). Then run the whole file to confirm no parity regression: `pytest tests/test_api_calls_feed_behavior.py -v` — all green.

- [ ] **Step 6: Lint and commit**

Run: `ruff check src/call_manager/web/dashboard.py src/call_manager/web/api/calls.py tests/test_api_calls_feed_behavior.py`

```bash
git add src/call_manager/web/dashboard.py src/call_manager/web/api/calls.py tests/test_api_calls_feed_behavior.py
git commit -m "feat(calls): free-text search (q) on the calls feed"
```

---

### Task 2: Backend score floor (`min_score`)

**Files:**
- Modify: `src/call_manager/web/dashboard.py` (add `min_score` param; ranked-branch floor; score-aware `list_total`)
- Modify: `src/call_manager/web/api/calls.py` (add `min_score` Query param; pass through)
- Test: `tests/test_api_calls_feed_behavior.py` (min_score tests; reuse `_seed_scored`)

**Interfaces:**
- Consumes: `build_dashboard_view(..., q=...)` from Task 1; the ranked `stmt` (`dashboard.py:254-271`, selects `(Call, CallRanking)` via outerjoin); `list_filters` / `list_total` (`dashboard.py:292-306`); `profile_obj`/`profile_id` (resolved at `dashboard.py:243-249`); `_seed_scored(pg_session_factory, profile_id)` (seeds High=85, Low=15, Unranked=None; `test_api_calls_feed_behavior.py:~318`).
- Produces: `build_dashboard_view(..., q=None, min_score=None)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api_calls_feed_behavior.py`:

```python
# --- score floor (min_score) ------------------------------------------------


def test_min_score_floor_keeps_only_scored_at_or_above(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)  # High=85, Low=15, Unranked=None
    with c:
        body = c.get(f"/api/calls?profile={pid}&min_score=60").json()
    assert _titles(body) == ["High score"]          # 85 >= 60; 15 and unranked dropped
    assert body["pagination"]["total"] == 1


def test_min_score_drops_unranked_keeps_lower_scored(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)
    with c:
        body = c.get(f"/api/calls?profile={pid}&min_score=10").json()
    # 85 and 15 both >= 10; the unranked (NULL score) call is dropped under a floor.
    assert _titles(body) == ["High score", "Low score"]
    assert body["pagination"]["total"] == 2


def test_min_score_ignored_without_profile(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)
    with c:
        body = c.get("/api/calls?min_score=60").json()  # no ?profile= -> floor ignored
    assert set(_titles(body)) == {"High score", "Low score", "Unranked"}


def test_min_score_out_of_range_is_422(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, *_ = _login(app, pg_session_factory)
    with c:
        assert c.get("/api/calls?min_score=200").status_code == 422
        assert c.get("/api/calls?min_score=-1").status_code == 422


def test_min_score_does_not_narrow_summary_badges(monkeypatch, pg_url, pg_session_factory):
    """DELIBERATE (spec): the score floor narrows the LIST, but the summary badges
    keep describing the full profile feed. If this assertion ever flips, it is a
    design change, not a bug fix."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)  # all three calls are OPEN (no deadline)
    with c:
        body = c.get(f"/api/calls?profile={pid}&min_score=80").json()
    assert _titles(body) == ["High score"]            # list narrowed by the floor
    assert body["summary"]["open_count"] == 3         # badge still counts all open calls
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_api_calls_feed_behavior.py -v -k "min_score"`
Expected: FAIL — no `min_score` param yet (filter not applied; 422 cases return 200).

- [ ] **Step 3: Add `min_score` to the read-model**

Add the param to the signature (after `q`):

```python
    q: str | None = None,
    min_score: int | None = None,
) -> DashboardView:
```

Replace the page-query execution block (`dashboard.py:292-295`) — keep `list_filters` as-is, add a ranked-only `list_where`:

```python
    # The list (and its pagination total) hide closed calls by default.
    list_filters = list(base_filters)
    if not show_closed:
        list_filters.append(open_expr)
    # Score floor: ranked branch only (unranked calls have no score). The ranked
    # `stmt` already has CallRanking in its FROM, so the predicate resolves there;
    # because `>= min_score` is false for the LEFT-JOIN NULLs, unranked calls drop
    # out — the intended "matched calls only" behaviour.
    score_floor_active = (
        profile_id is not None and profile_obj is not None and min_score is not None
    )
    list_where = list(list_filters)
    if score_floor_active:
        list_where.append(CallRanking.aggregate_score >= min_score)
    rows = session.execute(stmt.where(*list_where)).unique().all()
```

Replace the `list_total` line (`dashboard.py:306`) with a floor-aware count:

```python
    # Pagination total tracks the visible (toggled) set — including the score
    # floor, so total/has_next stay correct. A floored count must JOIN CallRanking
    # (a plain Call count can't see aggregate_score).
    if score_floor_active:
        list_total = (
            session.scalar(
                select(func.count())
                .select_from(Call)
                .outerjoin(
                    CallRanking,
                    (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile_id),
                )
                .where(*list_where)
            )
            or 0
        )
    else:
        list_total = session.scalar(select(func.count()).select_from(Call).where(*list_filters)) or 0
```

Leave `open_count`, `soon_deadline`, and `high_score` (`dashboard.py:308-339`) untouched — they use `base_filters` and must NOT see the floor.

- [ ] **Step 4: Add the `min_score` param to the endpoint (`calls.py`)**

```python
    q: str | None = Query(default=None, max_length=200),
    min_score: int | None = Query(default=None, ge=0, le=100),
    user: User = Depends(get_current_user),  # noqa: B008
):
```

```python
            show_closed=show_closed,
            q=q,
            min_score=min_score,
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_api_calls_feed_behavior.py -v -k "min_score"` → PASS (5 tests).
Then the whole file: `pytest tests/test_api_calls_feed_behavior.py -v` → all green (parity intact).

- [ ] **Step 6: Lint and commit**

Run: `ruff check src/call_manager/web/dashboard.py src/call_manager/web/api/calls.py tests/test_api_calls_feed_behavior.py`

```bash
git add src/call_manager/web/dashboard.py src/call_manager/web/api/calls.py tests/test_api_calls_feed_behavior.py
git commit -m "feat(calls): badge-aligned score floor (min_score), drops unranked"
```

---

### Task 3: Frontend params plumbing + OpenAPI regen

**Files:**
- Regenerate: `frontend/src/api/schema.gen.ts` (via `pnpm gen:api` — picks up `q`/`min_score` from Tasks 1–2)
- Modify: `frontend/src/api/dashboard.ts` (`DashboardParams` + sanitized `dashboardParamsFrom`)
- Test: `frontend/src/api/dashboard.test.ts` (new — unit tests for `dashboardParamsFrom`)

**Interfaces:**
- Consumes: `DashboardParams` (`dashboard.ts:11-17`); `dashboardParamsFrom(sp)` (`dashboard.ts:19-27`); the regenerated `get_calls` query type now carrying optional `q`/`min_score`.
- Produces: `DashboardParams` with `q?: string; min_score?: number` (entering the `["calls", params]` key); `dashboardParamsFrom` emitting canonical, sanitized values that Task 4's controls read back.

- [ ] **Step 1: Install deps and regenerate the OpenAPI types**

In the worktree: `cd frontend && pnpm install` (node_modules is gitignored, absent in a fresh worktree).
Then: `pnpm gen:api` (runs `bash ../scripts/gen_openapi_types.sh`, which pins `EMBEDDING_MODEL=bge-m3` internally — do not set it yourself).
Expected: `git diff frontend/src/api/schema.gen.ts` shows the `get_calls_api_calls_get` query gaining optional `q` and `min_score`. No other endpoints change.

- [ ] **Step 2: Write the failing unit tests**

Create `frontend/src/api/dashboard.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { dashboardParamsFrom } from "./dashboard";

const P = (qs: string) => dashboardParamsFrom(new URLSearchParams(qs));

describe("dashboardParamsFrom q/min_score sanitization", () => {
  it("trims q and drops empty/whitespace", () => {
    expect(P("q=%20onco%20").q).toBe("onco");
    expect(P("q=%20%20").q).toBeUndefined();
    expect(P("").q).toBeUndefined();
  });

  it("parses a valid in-range min_score (including 0)", () => {
    expect(P("min_score=60").min_score).toBe(60);
    expect(P("min_score=0").min_score).toBe(0);
    expect(P("min_score=100").min_score).toBe(100);
  });

  it("rejects malformed or out-of-range min_score -> undefined", () => {
    expect(P("min_score=abc").min_score).toBeUndefined();
    expect(P("min_score=-5").min_score).toBeUndefined();
    expect(P("min_score=150").min_score).toBeUndefined();
    expect(P("").min_score).toBeUndefined();
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/api/dashboard.test.ts`
Expected: FAIL — `q` isn't trimmed (`"%20onco%20"` → `" onco "`); `min_score` is unknown on the returned object (and `tsc` in the test would flag it once used).

- [ ] **Step 4: Extend `DashboardParams` and `dashboardParamsFrom`**

In `frontend/src/api/dashboard.ts`, add the two fields to `DashboardParams`:

```ts
export type DashboardParams = {
  source?: string;
  sort?: "newest" | "deadline";
  profile?: string;
  page?: number;
  show_closed?: boolean;
  q?: string;
  min_score?: number;
};
```

Replace `dashboardParamsFrom` with the sanitizing version:

```ts
export function dashboardParamsFrom(sp: URLSearchParams): DashboardParams {
  // Sanitize q/min_score here so the query key and controls only ever see
  // canonical values (a malformed deep link can't poison client state).
  const rawQ = (sp.get("q") ?? "").trim();
  const rawScore = sp.get("min_score");
  const parsedScore = rawScore != null ? Number(rawScore) : NaN;
  return {
    source: sp.get("source") ?? undefined,
    sort: (sp.get("sort") as "newest" | "deadline" | null) ?? undefined,
    profile: sp.get("profile") ?? undefined,
    page: sp.get("page") ? Number(sp.get("page")) : 1,
    show_closed: sp.get("show_closed") === "1",
    q: rawQ || undefined,
    min_score:
      Number.isFinite(parsedScore) && parsedScore >= 0 && parsedScore <= 100
        ? parsedScore
        : undefined,
  };
}
```

- [ ] **Step 5: Run the tests and the type gate**

Run: `cd frontend && pnpm vitest run src/api/dashboard.test.ts` → PASS.
Run: `pnpm exec tsc --noEmit` → no errors (the regenerated query type accepts `q`/`min_score`; `DashboardParams` stays a structural subset).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/schema.gen.ts frontend/src/api/dashboard.ts frontend/src/api/dashboard.test.ts
git commit -m "feat(spa): plumb q/min_score params (+regen OpenAPI types)"
```

---

### Task 4: Frontend controls — search box + score floor

**Files:**
- Modify: `frontend/src/routes/CallsFilters.tsx` (debounced `TextInput` + score-floor `Select`)
- Test: `frontend/src/routes/CallsFilters.test.tsx` (extend with control tests)

**Interfaces:**
- Consumes: `dashboardParamsFrom`/`useDashboard` (`CallsFilters.tsx:4`, params now carry `q`/`min_score`); `updateParam(key, value)` (`CallsFilters.tsx:20-28`, deletes `page`); `data.effective.profile_id` (for disabled state); `useDebouncedValue` (`@mantine/hooks`); the test harness `dashData(over)` / `LocationProbe` (`CallsFilters.test.tsx:10-39`).
- Produces: two URL-bound sidebar controls. No new exports.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/routes/CallsFilters.test.tsx`:

```ts
it("typing in Search writes a debounced ?q= and resets the page", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/?page=3" },
  );
  await user.type(await screen.findByLabelText("Search"), "onco");
  await waitFor(() => {
    const s = screen.getByTestId("search").textContent ?? "";
    expect(s).toContain("q=onco");
    expect(s).not.toContain("page=3");
  });
});

it("seeds the Search box from the URL ?q=", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/?q=onco" });
  expect(await screen.findByLabelText("Search")).toHaveValue("onco");
});

it("the score floor is disabled without a profile and enabled with one", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: null }) as never);
  const { unmount } = renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Min score", { selector: "input" })) as HTMLInputElement).toBeDisabled();
  unmount();
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Min score", { selector: "input" })) as HTMLInputElement).not.toBeDisabled();
});

it("selecting a score band writes ?min_score=", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Min score", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "60+ (Good)", hidden: true }));
  await waitFor(() => expect(screen.getByTestId("search").textContent).toContain("min_score=60"));
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/routes/CallsFilters.test.tsx`
Expected: FAIL — no "Search" or "Min score" controls in the DOM.

- [ ] **Step 3: Add the controls to `CallsFilters.tsx`**

Update the imports:

```ts
import { Select, Switch, TextInput } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
```

Inside `CallsFilters`, after `const loading = data === undefined;` and the `updateParam` definition, add the debounced-search state:

```ts
  const [searchInput, setSearchInput] = useState(params.q ?? "");
  const [debouncedSearch] = useDebouncedValue(searchInput, 300);
  // Commit only when the debounced value differs from the (already-trimmed) URL
  // value — this guard prevents an echo loop with the re-sync effect below.
  // params.q is in the deps so the closure never reads a stale URL value; the
  // guard makes the resulting self-triggered re-run a no-op.
  useEffect(() => {
    const next = debouncedSearch.trim();
    if (next !== (params.q ?? "")) updateParam("q", next);
  }, [debouncedSearch, params.q]);
  // Re-sync the box when the URL q changes externally (back/forward, deep link).
  useEffect(() => {
    setSearchInput(params.q ?? "");
  }, [params.q]);
```

Render the search box at the top of the filters block (just after `<h2 className={styles.heading}>Filters</h2>`):

```tsx
      <TextInput
        label="Search"
        placeholder="Search calls…"
        value={searchInput}
        onChange={(e) => setSearchInput(e.currentTarget.value)}
      />
```

Add the score-floor `Select` after the Sort select (before the Show-closed `Switch`):

```tsx
      <Select
        label="Min score"
        description={data?.effective.profile_id == null ? "Select a profile to filter by score" : undefined}
        data={[
          { value: "", label: "Any" },
          { value: "60", label: "60+ (Good)" },
          { value: "80", label: "80+ (Strong)" },
        ]}
        value={params.min_score != null ? String(params.min_score) : ""}
        onChange={(v) => updateParam("min_score", v ?? "")}
        disabled={loading || data?.effective.profile_id == null}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />
```

Note: this file has no `.module.css` rule changes — the controls use Mantine defaults and the existing `styles.filters` wrapper. Do not add bare element selectors.

- [ ] **Step 4: Run the tests and the type gate**

Run: `cd frontend && pnpm vitest run src/routes/CallsFilters.test.tsx` → PASS (existing + 4 new).
Run: `pnpm exec tsc --noEmit` → no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/CallsFilters.tsx frontend/src/routes/CallsFilters.test.tsx
git commit -m "feat(spa): debounced search box + score-floor select in CallsFilters"
```

---

### Task 5: Digest "+N more in your dashboard →" breadcrumb

**Files:**
- Modify: `src/call_manager/digest/assemble.py` (`more_count` on `DigestProfileSection`; per-profile `top_total`)
- Modify: `src/call_manager/digest/templates/digest.html.jinja`, `src/call_manager/digest/templates/digest.txt.jinja`
- Test: `tests/test_digest_assemble.py` (more_count math), `tests/test_digest_render.py` (breadcrumb render)

**Interfaces:**
- Consumes: `assemble_for_user(session, *, user_id, now)` (`assemble.py:72`); the `top_q` filter conditions (`assemble.py:222-237`), `excl_clause` (`assemble.py:157`), `now`; the section-build loop (`assemble.py:308-336`); `DigestProfileSection` (`assemble.py:57-61`); `render_html`/`render_text(payload, dashboard_url=...)`. Test harnesses: `_seed_user_with_profile`/`_seed_call`/`assemble_for_user` (`test_digest_assemble.py`); `_payload()`/`render_html` (`test_digest_render.py`).
- Produces: `DigestProfileSection.more_count: int` (default 0) consumed by both templates.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_digest_assemble.py` (reuse its `_seed_user_with_profile`, `_seed_call`, `assemble_for_user`, `now` fixture, and `pg_session_factory`):

```python
def test_more_count_counts_top_surplus_beyond_shown(pg_session_factory, now):
    """7 strong (>=60) matches -> 5 shown in the section, more_count == 2."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        for i in range(7):
            _seed_call(
                s, title=f"Strong {i}", score=80, profile_id=pid,
                deadline=now + timedelta(days=30 + i),
            )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    section = next(sec for sec in payload.sections if sec.profile_id == pid)
    shown_top = [e for e in section.entries if e.bucket == "top"]
    assert len(shown_top) == 5
    assert section.more_count == 2


def test_more_count_zero_when_top_fits(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        for i in range(3):
            _seed_call(
                s, title=f"Strong {i}", score=80, profile_id=pid,
                deadline=now + timedelta(days=30 + i),
            )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    section = next(sec for sec in payload.sections if sec.profile_id == pid)
    assert section.more_count == 0
```

Add to `tests/test_digest_render.py`:

```python
def _payload_with_more(more_count: int):
    p = _payload()
    p.sections[0].more_count = more_count
    return p


def test_html_renders_more_breadcrumb_when_positive():
    html = render_html(_payload_with_more(12), dashboard_url="http://app/")
    assert "12 more strong matches in your dashboard" in html
    assert "http://app/?profile=1&amp;min_score=60" in html


def test_html_omits_more_breadcrumb_when_zero():
    html = render_html(_payload_with_more(0), dashboard_url="http://app/")
    assert "more strong matches" not in html


def test_text_renders_more_breadcrumb_when_positive():
    text = render_text(_payload_with_more(12), dashboard_url="http://app/")
    assert "12 more strong matches in your dashboard" in text
    assert "http://app/?profile=1&min_score=60" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_digest_assemble.py -v -k "more_count" && pytest tests/test_digest_render.py -v -k "more"`
Expected: FAIL — `DigestProfileSection` has no `more_count` (TypeError on the render helper's attribute set / assemble doesn't set it); templates render no breadcrumb.

- [ ] **Step 3: Add `more_count` to the dataclass**

In `assemble.py`, extend `DigestProfileSection` (`assemble.py:57-61`):

```python
@dataclass
class DigestProfileSection:
    profile_id: int
    profile_name: str
    entries: list[DigestCallEntry]
    more_count: int = 0
```

- [ ] **Step 4: Compute `top_total` per profile and set `more_count`**

Initialize a per-profile accumulator next to `per_call`/`call_objs` (`assemble.py:161-162`):

```python
    per_call: dict[int, list[tuple[Profile, str, CallRanking | None]]] = defaultdict(list)
    call_objs: dict[int, Call] = {}
    top_total_by_profile: dict[int, int] = {}
```

Refactor the "top" candidate query to share its WHERE conditions and add the count (replace `assemble.py:222-240`):

```python
        # 3b. top candidates: aggregate_score >= threshold
        top_conditions = [
            CallRanking.profile_id == profile.id,
            CallRanking.aggregate_score >= DIGEST_TOP_THRESHOLD,
            excl_clause,
            or_(Call.deadline.is_(None), Call.deadline > now),
        ]
        top_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(*top_conditions)
            .order_by(
                CallRanking.aggregate_score.desc(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(50)  # over-fetch; dedup may drop some
        )
        for c, rk in session.execute(top_q).all():
            per_call[c.id].append((profile, "top", rk))
            call_objs[c.id] = c
        # Total strong matches for this profile == the dashboard's ?min_score=60
        # set; used for an honest "+N more in your dashboard" breadcrumb.
        top_total_by_profile[profile.id] = (
            session.scalar(
                select(func.count())
                .select_from(Call)
                .join(CallRanking, CallRanking.call_id == Call.id)
                .where(*top_conditions)
            )
            or 0
        )
```

Set `more_count` when building the section (replace the `sections.append(...)` block at `assemble.py:330-336`):

```python
        more_count = max(0, top_total_by_profile.get(pid, 0) - len(buckets["top"]))
        sections.append(
            DigestProfileSection(
                profile_id=pid,
                profile_name=profiles_by_id[pid].name,
                entries=ordered,
                more_count=more_count,
            )
        )
```

(`buckets["top"]` is already capped to `DIGEST_BUCKET_SIZE` at `assemble.py:320`, so `len(buckets["top"])` is the shown-top count.)

- [ ] **Step 5: Render the breadcrumb in both templates**

In `digest.html.jinja`, insert after the borderline `{% endif %}` (line 32) and before the section `{% endfor %}` (line 33):

```jinja
  {% if section.more_count > 0 %}
    <p style="font-size: 13px; margin: 8px 0;"><a href="{{ dashboard_url }}?profile={{ section.profile_id }}&amp;min_score=60" style="color: #27a; text-decoration: none;">+{{ section.more_count }} more strong matches in your dashboard &rarr;</a></p>
  {% endif %}
```

In `digest.txt.jinja`, insert after the borderline `{% endif %}` (line 19) and before the section `{% endfor %}` (line 20):

```jinja
{% if section.more_count > 0 %}+{{ section.more_count }} more strong matches in your dashboard: {{ dashboard_url }}?profile={{ section.profile_id }}&min_score=60
{% endif %}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_digest_assemble.py -v -k "more_count"` → PASS.
Run: `pytest tests/test_digest_render.py -v` → PASS (new breadcrumb tests + existing render tests unchanged, since `more_count` defaults to 0).

- [ ] **Step 7: Lint and commit**

Run: `ruff check src/call_manager/digest/assemble.py tests/test_digest_assemble.py tests/test_digest_render.py`

```bash
git add src/call_manager/digest/assemble.py src/call_manager/digest/templates/digest.html.jinja src/call_manager/digest/templates/digest.txt.jinja tests/test_digest_assemble.py tests/test_digest_render.py
git commit -m "feat(digest): +N more in your dashboard breadcrumb (top-bucket surplus)"
```

---

## Final verification (whole branch, before finishing)

- [ ] Backend: `ruff check .` then `pytest tests/test_api_calls_feed_behavior.py tests/test_digest_assemble.py tests/test_digest_render.py -v` (full suite via grouped dir runs if testcontainers flakes under load).
- [ ] Frontend (`cd frontend`): `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm run build`.
- [ ] OpenAPI drift: re-run `pnpm gen:api` and `git diff --exit-code frontend/src/api/schema.gen.ts` (must be clean).
- [ ] Manual smoke (optional): with a profile selected, `?q=` narrows list + badges; `&min_score=60` drops unranked and narrows only the list; a digest render shows the breadcrumb deep-linking to `?profile=&min_score=60`.

## Self-review notes (author)

- **Spec coverage:** search (T1), score floor (T2), frontend params+sanitization (T3), controls (T4), digest breadcrumb (T5) — all spec components mapped. Edge cases (422s, blank q, floor-without-profile, badge non-narrowing, more_count `<=5→0`) each have a test.
- **Type/name consistency:** `build_dashboard_view(..., q=None, min_score=None)` added in T1/T2 and consumed by `get_calls`; `DashboardParams.q/min_score` (T3) feed `["calls", params]`; `DigestProfileSection.more_count` (T5) consumed by both templates. The frontend control labels ("Search", "Min score") and option label ("60+ (Good)") match the test selectors.
- **Ordering rationale:** schema.gen.ts is regenerated once (T3) after both backend params land; T1/T2 are Python-only and leave it stale by design (no consumer until T3). T5 (digest) is independent and last.
