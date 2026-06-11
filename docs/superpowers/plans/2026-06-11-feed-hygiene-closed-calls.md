# Feed Hygiene — Closed-Call Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dashboard feed from presenting expired calls as live, urgent, and counted — hide closed calls by default (one toggle away), make a closed call unable to outrank an open one, count only open calls in the summary badges, and render past deadlines as a calm "Closed" instead of red-urgent.

**Architecture:** "Closed" is derived at query time (`deadline IS NOT NULL AND deadline <= now`) — no schema change, no migration, no reaper. The feed query gains a `show_closed` flag (default false → filter closed out) and a *structural* closed-last sort key applied in every branch (so even when closed calls are shown they sort below open ones). Summary badges count open-only. The template renders a grey "Closed" label and a "Show closed calls" checkbox; a one-line `app.js` change lets the checkbox auto-submit like the existing select filters.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.x, pytest (+ testcontainers Postgres via `pg_url`/`pg_session_factory`/`profile_factory` fixtures). No new dependencies. No migration.

**Spec:** [docs/superpowers/specs/2026-06-11-feed-hygiene-closed-calls-design.md](../specs/2026-06-11-feed-hygiene-closed-calls-design.md)

---

## File Structure

**Modify:**
- `src/call_manager/web/routes/calls.py` — `calls_index`: `show_closed` param; `closed_expr`/`open_expr`; closed-last sort key in both query branches; conditional list filter; `open_count` + open-only `high_score`; pass `show_closed` to the template.
- `src/call_manager/web/templates/calls_index.html` — `summary.total` → `summary.open_count` (Task 2); `deadline_label` "Closed" branch, the toggle checkbox, pagination `qs` (Task 3).
- `src/call_manager/web/static/app.js` — generalize `select[data-autosubmit]` → `[data-autosubmit]`.
- `src/call_manager/web/static/app.css` — add `.deadline-closed`.
- `tests/test_web_calls.py` — make `_seed_calls` deadlines relative (Task 1); new closed-call tests (Tasks 2–3).
- `tests/test_web_calls_with_score.py` — new ranked closed-call tests (Task 2).

**The only consumer of the `summary` dict is `calls_index.html`** (built locally in `calls_index`), so the `total` → `open_count` rename is fully contained in these two files.

---

## Test infrastructure (read before starting)

`tests/test_web_calls.py` and `tests/test_web_calls_with_score.py` both define local `_setup(monkeypatch, pg_url)` (sets env, returns `create_app()`) and `_logged_in(app, pg_session_factory)` (creates a user, returns a `TestClient` with a signed session cookie). `test_web_calls_with_score.py` additionally uses the `profile_factory` fixture (returns `(profile_id, _)`) and seeds `CallRanking(call_id=, profile_id=, aggregate_score=, rationale=, model="gemma4:e4b-q8", prompt_version="v2.0")`. Reuse these exactly — do not invent new helpers where one exists.

**Run feed tests with:** `uv run pytest tests/test_web_calls.py tests/test_web_calls_with_score.py -q`

---

## Task 1: Make the test seed deadlines relative (decouple from wall-clock)

The shared `_seed_calls` helper uses hardcoded 2026 deadlines (`May 15` / `Jun 10` / `Jul 1`). Today (2026-06-11) two of those are already **past**, so the closed-call filter added in Task 2 would hide them and break three existing tests. Make the deadlines `now`-relative and clearly in the future, preserving the existing relative order (Vin soonest → EU two → EU one). Pure test-fixture change; behavior is unchanged today (all three stay open and visible), so the whole file stays green.

**Files:**
- Modify: `tests/test_web_calls.py`

- [ ] **Step 1: Update the imports and `_seed_calls`**

In `tests/test_web_calls.py`, change the datetime import line:

```python
from datetime import UTC, datetime, timedelta
```

Replace the entire `_seed_calls` function with:

```python
def _seed_calls(pg_session_factory):
    # Deadlines are relative to "now" so the suite is robust to wall-clock and
    # all three calls stay OPEN — the closed-call filter would otherwise hide
    # fixed past-2026 dates. Relative order preserved (and all > 30 days out, so
    # none land in the "deadline within 30 d" badge): Vin (soonest) < EU two < EU one.
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all(
            [
                # EU one: published April 1, latest deadline.
                Call(
                    source_id=eu.id,
                    external_id="E-1",
                    url="u1",
                    title="EU one",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=105),
                ),
                # Vin one: published April 5 (newest), soonest deadline.
                Call(
                    source_id=vn.id,
                    external_id="V-1",
                    url="u2",
                    title="Vin one",
                    published_at=datetime(2026, 4, 5, tzinfo=UTC),
                    deadline=now + timedelta(days=45),
                ),
                # EU two: published March 1 (oldest), middle deadline.
                Call(
                    source_id=eu.id,
                    external_id="E-2",
                    url="u3",
                    title="EU two",
                    published_at=datetime(2026, 3, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=75),
                ),
            ]
        )
        s.commit()
```

Also update the now-stale dates in the `test_root_sort_by_deadline` docstring so it doesn't lie — change its body line to:

```python
    """sort=deadline orders soonest-deadline first (irrespective of
    publication date). Vin one → EU two → EU one (relative future deadlines)."""
```

- [ ] **Step 2: Run the feed tests to confirm still green**

Run: `uv run pytest tests/test_web_calls.py -q`
Expected: PASS (all existing tests unchanged in outcome — the three calls remain open and visible; ordering, summary count of 3, and pagination are preserved).

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_calls.py
git commit -m "test(calls): make feed seed deadlines relative (future), robust to wall-clock"
```

---

## Task 2: Closed-call filter, closed-last sort, and open-only badges (route)

**Files:**
- Modify: `src/call_manager/web/routes/calls.py`
- Modify: `src/call_manager/web/templates/calls_index.html` (one line: `summary.total` → `summary.open_count`)
- Test: `tests/test_web_calls.py`, `tests/test_web_calls_with_score.py`

- [ ] **Step 1: Write the failing tests (unranked behavior) in `tests/test_web_calls.py`**

Add this helper after `_seed_calls`:

```python
def _add_closed_call(pg_session_factory, *, title="Closed call", external_id="C-1"):
    """Insert one call whose deadline is in the past (closed)."""
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add(
            Call(
                source_id=eu.id,
                external_id=external_id,
                url="uc",
                title=title,
                published_at=datetime(2026, 4, 2, tzinfo=UTC),
                deadline=now - timedelta(days=5),
            )
        )
        s.commit()
```

Add these tests:

```python
def test_default_feed_hides_closed_calls(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Closed call" not in r.text
    assert "Vin one" in r.text  # open calls still shown


def test_show_closed_reveals_closed_calls(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1")
    assert r.status_code == 200
    assert "Closed call" in r.text
    assert "Vin one" in r.text


def test_closed_calls_sort_last_unranked(monkeypatch, pg_url, pg_session_factory):
    """Even with sort=deadline (which would pull a past deadline to the top),
    a shown closed call must appear AFTER every open call."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1&sort=deadline")
    body = r.text
    assert body.index("Closed call") > body.index("Vin one")
    assert body.index("Closed call") > body.index("EU two")
    assert body.index("Closed call") > body.index("EU one")


def test_open_calls_badge_excludes_closed_regardless_of_toggle(
    monkeypatch, pg_url, pg_session_factory
):
    """The 'Open calls' badge counts only open calls (3), whether or not the
    closed call is being shown."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get("/")
        shown = client.get("/?show_closed=1")
    # Open count is 3 in both; the 4-row total is never shown as a badge.
    assert ">3</div>" in default.text
    assert ">3</div>" in shown.text
    assert ">4</div>" not in shown.text
```

- [ ] **Step 2: Write the failing tests (ranked behavior) in `tests/test_web_calls_with_score.py`**

Add `timedelta` to its datetime import:

```python
from datetime import UTC, datetime, timedelta
```

Add these tests:

```python
def test_profile_view_closed_call_never_outranks_open(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A high-scored CLOSED call is hidden by default; even when shown it
    sorts BELOW a lower-scored open call (structural closed-last sort)."""
    profile_id, _ = profile_factory()
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        closed = Call(
            source_id=src.id, external_id="CL", url="https://x/cl", title="Closed high",
            published_at=datetime(2026, 4, 1, tzinfo=UTC), deadline=now - timedelta(days=5),
        )
        openc = Call(
            source_id=src.id, external_id="OP", url="https://x/op", title="Open low",
            published_at=datetime(2026, 4, 2, tzinfo=UTC), deadline=now + timedelta(days=20),
        )
        s.add_all([closed, openc])
        s.flush()
        s.add(CallRanking(call_id=closed.id, profile_id=profile_id, aggregate_score=95,
                          rationale="x", model="gemma4:e4b-q8", prompt_version="v2.0"))
        s.add(CallRanking(call_id=openc.id, profile_id=profile_id, aggregate_score=20,
                          rationale="x", model="gemma4:e4b-q8", prompt_version="v2.0"))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get(f"/?profile={profile_id}")
        shown = client.get(f"/?profile={profile_id}&show_closed=1")
    assert "Closed high" not in default.text  # hidden despite score 95
    assert "Open low" in default.text
    assert "Closed high" in shown.text and "Open low" in shown.text
    assert shown.text.index("Open low") < shown.text.index("Closed high")


def test_top_matches_badge_excludes_closed_high_scorer(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A closed call scoring >=70 must not be counted as a 'Top match'."""
    profile_id, _ = profile_factory()
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        closed = Call(
            source_id=src.id, external_id="CH", url="https://x/ch", title="Closed top",
            published_at=datetime(2026, 4, 1, tzinfo=UTC), deadline=now - timedelta(days=3),
        )
        openc = Call(
            source_id=src.id, external_id="OH", url="https://x/oh", title="Open top",
            published_at=datetime(2026, 4, 2, tzinfo=UTC), deadline=now + timedelta(days=20),
        )
        s.add_all([closed, openc])
        s.flush()
        s.add(CallRanking(call_id=closed.id, profile_id=profile_id, aggregate_score=95,
                          rationale="x", model="gemma4:e4b-q8", prompt_version="v2.0"))
        s.add(CallRanking(call_id=openc.id, profile_id=profile_id, aggregate_score=75,
                          rationale="x", model="gemma4:e4b-q8", prompt_version="v2.0"))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/?profile={profile_id}")
    # Top matches = 1 (the open 75), not 2. Without the open-only filter the
    # badge would read 2, so no badge should render the value 2.
    assert ">2</div>" not in r.text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_calls.py tests/test_web_calls_with_score.py -q -k "closed or top_matches or show_closed"`
Expected: FAIL — `show_closed` is an unknown query param today (closed calls always show; closed sorts to the top under `sort=deadline`; badges count closed).

- [ ] **Step 4: Update the route `calls_index` in `src/call_manager/web/routes/calls.py`**

Change the SQLAlchemy import line (add `and_`, `or_`):

```python
from sqlalchemy import and_, func, or_, select
```

Add `show_closed` to the signature (after `page`):

```python
@router.get("/", response_class=HTMLResponse)
def calls_index(
    request: Request,
    source: str | None = Query(default=None),
    sort: _Sort = Query(default="newest"),  # noqa: B008
    profile: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    show_closed: bool = Query(default=False),
    user: User = Depends(get_current_user),  # noqa: B008
):
```

Just after the existing `_soon_cutoff` line, define the closed/open expressions:

```python
    _now = datetime.now(UTC)
    _soon_cutoff = _now + timedelta(days=30)
    # "Closed" is derived from the deadline — no status column. A call with no
    # deadline (rolling) is open. Same comparison used in filter, sort, badges.
    closed_expr = and_(Call.deadline.isnot(None), Call.deadline <= _now)
    open_expr = or_(Call.deadline.is_(None), Call.deadline > _now)
```

Prepend `closed_expr.asc()` to the `order_by` of **both** query branches. Ranked branch:

```python
                .order_by(
                    closed_expr.asc(),  # open (False=0) before closed (True=1)
                    CallRanking.aggregate_score.desc().nullslast(),
                    CallRanking.embed_similarity.desc().nullslast(),
                    Call.published_at.desc().nullslast(),
                    Call.id.desc(),
                )
```

Unranked branch:

```python
                .order_by(closed_expr.asc(), *_order_for(sort))
```

Replace the `stmt = stmt.where(*base_filters)` line (currently applied right after `base_filters` is built) with a list-filter that adds the open predicate unless closed calls are requested:

```python
        base_filters = [Call.id.notin_(dismissed_subq)]
        if source_id is not None:
            base_filters.append(Call.source_id == source_id)
        # The list (and its pagination total) hide closed calls by default.
        list_filters = list(base_filters)
        if not show_closed:
            list_filters.append(open_expr)
        stmt = stmt.where(*list_filters)
```

Replace the **entire contiguous summary-aggregates block** — the existing `total = ...`, `soon_deadline = ...`, and `high_score = ...` computations (currently the region from the `total =` line through the end of the `high_score` block) — with the following. It tracks the toggle for the pagination total, counts open-only for the "Open calls" badge, leaves `soon_deadline` logically unchanged, and excludes closed calls from "Top matches" (do not leave the old `total`/`soon_deadline`/`high_score` lines behind — this block supersedes all three):

```python
        # Pagination total tracks the visible (toggled) set.
        list_total = (
            session.scalar(select(func.count()).select_from(Call).where(*list_filters)) or 0
        )
        # "Open calls" badge is always the genuine open count, toggle or not.
        open_count = (
            session.scalar(
                select(func.count()).select_from(Call).where(*base_filters, open_expr)
            )
            or 0
        )
        soon_deadline = (
            session.scalar(
                select(func.count())
                .select_from(Call)
                .where(
                    *base_filters,
                    Call.deadline.isnot(None),
                    Call.deadline > _now,
                    Call.deadline <= _soon_cutoff,
                )
            )
            or 0
        )
        high_score = 0
        if profile is not None and profile_obj is not None:
            high_score = (
                session.scalar(
                    select(func.count())
                    .select_from(CallRanking)
                    .join(Call, Call.id == CallRanking.call_id)
                    .where(
                        *base_filters,
                        open_expr,  # a closed call is never a "top match"
                        CallRanking.profile_id == profile,
                        CallRanking.aggregate_score >= 70,
                    )
                )
                or 0
            )
```

Update the `summary`/`pagination` dicts and the template context:

```python
    summary = {"open_count": open_count, "high_score": high_score, "soon_deadline": soon_deadline}
    pagination = {
        "page": page,
        "limit": limit,
        "has_prev": page > 1,
        "has_next": offset + len(calls_with_rankings) < list_total,
    }
```

```python
        context={
            "calls_with_rankings": calls_with_rankings,
            "sources": sources,
            "profiles": profiles,
            "selected_source": source,
            "selected_sort": sort,
            "selected_profile_id": profile,
            "show_closed": show_closed,
            "summary": summary,
            "pagination": pagination,
            "now": _now,
            "user": user,
        },
```

- [ ] **Step 5: Update the one template line in `src/call_manager/web/templates/calls_index.html`**

Change the "Open calls" value (currently `{{ summary.total }}`):

```jinja
  <div class="summary-card">
    <div class="label">Open calls</div>
    <div class="value">{{ summary.open_count }}</div>
  </div>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_calls.py tests/test_web_calls_with_score.py -q`
Expected: PASS — the new closed-call tests pass and every pre-existing feed test (newest-first, source filter, summary count of 3, deadline sort, profile score order) still passes.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/call_manager/web/routes/calls.py
git add src/call_manager/web/routes/calls.py \
        src/call_manager/web/templates/calls_index.html \
        tests/test_web_calls.py tests/test_web_calls_with_score.py
git commit -m "feat(calls): hide closed calls by default; closed-last sort; open-only badges"
```

---

## Task 3: "Closed" deadline rendering, the toggle UI, and pagination plumbing

**Files:**
- Modify: `src/call_manager/web/templates/calls_index.html`
- Modify: `src/call_manager/web/static/app.js`
- Modify: `src/call_manager/web/static/app.css`
- Test: `tests/test_web_calls.py`

- [ ] **Step 1: Write the failing tests in `tests/test_web_calls.py`**

```python
def test_closed_deadline_renders_closed_label(monkeypatch, pg_url, pg_session_factory):
    """A shown closed call's deadline reads grey 'Closed', not red 'deadline-soon'."""
    app = _setup(monkeypatch, pg_url)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1")
    body = r.text
    assert "deadline-closed" in body
    assert "Closed" in body
    # The closed row must NOT be styled as the urgent (red) deadline.
    assert "deadline-soon" not in body


def test_show_closed_toggle_checkbox_present_and_reflects_state(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        off = client.get("/")
        on = client.get("/?show_closed=1")
    assert 'name="show_closed"' in off.text
    assert "checked" not in _show_closed_input(off.text)
    assert "checked" in _show_closed_input(on.text)


def test_pagination_preserves_show_closed(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed → 4 rows when shown
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1&limit=1")
    assert "show_closed=1" in r.text  # Next link carries the toggle
    assert "page=2" in r.text
```

Add this small helper near the top of the test file (after the imports) so the checkbox-state assertion is robust to attribute order:

```python
def _show_closed_input(html: str) -> str:
    """Return just the show_closed <input ...> tag, for state assertions."""
    start = html.index('name="show_closed"')
    open_lt = html.rfind("<", 0, start)
    close_gt = html.index(">", start)
    return html[open_lt : close_gt + 1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_calls.py -q -k "closed_label or toggle or preserves_show_closed"`
Expected: FAIL — no `deadline-closed` class yet (past deadline still renders `deadline-soon`); no `show_closed` checkbox; pagination `qs` omits the toggle.

- [ ] **Step 3: Add the "Closed" branch to the `deadline_label` macro in `calls_index.html`**

Replace the macro (lines ~17-30) with:

```jinja
{% macro deadline_label(deadline) -%}
  {%- if deadline is none -%}
    —
  {%- elif now and deadline <= now -%}
    <span class="deadline-closed">Closed · {{ deadline | display_date }}</span>
  {%- else -%}
    {%- set days = (deadline - now).days if now else None -%}
    {%- if days is not none and days <= 14 -%}
      <span class="deadline-soon">{{ deadline | display_date }}</span>
    {%- elif days is not none and days <= 30 -%}
      <span class="deadline-watch">{{ deadline | display_date }}</span>
    {%- else -%}
      {{ deadline | display_date }}
    {%- endif -%}
  {%- endif -%}
{%- endmacro %}
```

- [ ] **Step 4: Add the toggle checkbox to the filter bar in `calls_index.html`**

Inside `<form method="get" action="/" class="filter-bar">`, after the Sort `{% endif %}` block (line ~84), add:

```jinja
  <label class="checkbox-inline">
    <input type="checkbox" name="show_closed" value="1" data-autosubmit
           {% if show_closed %}checked{% endif %}>
    Show closed calls
  </label>
```

- [ ] **Step 5: Thread `show_closed` through the pagination links in `calls_index.html`**

Update the `qs` set (line ~126) to append the toggle when on:

```jinja
    {% set qs %}{% if selected_source %}&source={{ selected_source }}{% endif %}{% if selected_profile_id %}&profile={{ selected_profile_id }}{% endif %}{% if not selected_profile_id %}&sort={{ selected_sort }}{% endif %}{% if show_closed %}&show_closed=1{% endif %}&limit={{ pagination.limit }}{% endset %}
```

- [ ] **Step 6: Generalize the auto-submit selector in `src/call_manager/web/static/app.js`**

Change the querySelectorAll target (line ~19) so checkboxes auto-submit too. Replace:

```javascript
    // Selects that submit their enclosing form on change (filter bars).
    // Markup: <select data-autosubmit> ... </select>
    document.querySelectorAll("select[data-autosubmit]").forEach(function (el) {
```

with:

```javascript
    // Controls that submit their enclosing form on change (filter bars):
    // <select data-autosubmit> and <input type="checkbox" data-autosubmit>.
    document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
```

(The `change` event fires for both selects and checkboxes; the handler already lives in `app.js`, so the strict `script-src 'self'` CSP is unaffected.)

- [ ] **Step 7: Add the `.deadline-closed` style to `src/call_manager/web/static/app.css`**

After the `.deadline-watch` rule (line ~384), add:

```css
.deadline-closed { color: var(--cm-muted); white-space: nowrap; }
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_calls.py -q -k "closed_label or toggle or preserves_show_closed"`
Expected: PASS.

- [ ] **Step 9: Run the full feed suite and commit**

Run: `uv run pytest tests/test_web_calls.py tests/test_web_calls_with_score.py -q`
Expected: PASS (all feed tests green).

```bash
git add src/call_manager/web/templates/calls_index.html \
        src/call_manager/web/static/app.js \
        src/call_manager/web/static/app.css \
        tests/test_web_calls.py
git commit -m "feat(calls): grey Closed deadline label + Show-closed toggle (auto-submit)"
```

---

## Final verification

- [ ] **Run the feed + a broad sweep of related suites**

Run: `uv run pytest tests/test_web_calls.py tests/test_web_calls_with_score.py tests/test_web_call_detail.py -q`
Expected: PASS.

- [ ] **Lint the full changeset**

Run: `uv run ruff check src tests`
Expected: clean.

---

## Spec coverage map

| Spec requirement | Task |
|---|---|
| `show_closed` param, default false | Task 2 |
| Default filter hides closed (`deadline IS NULL OR > now`) | Task 2 |
| Closed-last sort key in both branches (closed never outranks open, even when shown) | Task 2 |
| "Open calls" badge counts open-only regardless of toggle (`open_count`) | Task 2 |
| "Top matches" excludes closed high-scorers | Task 2 |
| "Deadline within 30 d" badge unchanged | Task 2 (untouched) |
| Pagination total tracks the visible/toggled set | Task 2 |
| Grey "Closed" deadline label (not red `deadline-soon`), same `deadline <= now` rule | Task 3 |
| `.deadline-closed` CSS (muted) | Task 3 |
| "Show closed calls" checkbox in the filter bar, reflects state | Task 3 |
| Checkbox auto-submits like the selects (`[data-autosubmit]`) | Task 3 |
| Pagination preserves `show_closed` | Task 3 |
| No-deadline (rolling) calls treated as open / always visible | Task 2 (via `open_expr`) + test seed |
| No migration | (whole plan — derived at query time) |
| Test fixture robust to wall-clock | Task 1 |

All spec sections map to a task. No DB migration. Non-goals (search, save/star, undoable Hide, sort-in-profile, jargon, share, empty-state) are untouched.
