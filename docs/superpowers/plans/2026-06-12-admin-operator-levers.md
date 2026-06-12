# Admin Operator Levers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `/admin` operator UI buttons to fetch a source, send the weekly digest, and re-enable a bounced user — each wrapping an already-safe backend — and put a confirm + count-preview on the one dangerous button (re-rank), removing the standing SSH/psql dependency for day-to-day recovery.

**Architecture:** Four `POST` routes under `require_admin`, each redirecting `303` to `/admin?ok=<key>`/`?err=<key>`. The fetch and digest levers don't run work in the request — they call a new `trigger_job_now(scheduler, job_id)` helper that sets an existing APScheduler job's `next_run_time=now`, so the job's already-wired tick runs once in the background thread and then resumes its cron. The re-enable lever is pure DB. The template maps a fixed set of redirect keys to fixed banner messages (never echoes the raw param).

**Tech Stack:** Python 3.12, FastAPI + Jinja2 (server-rendered), SQLAlchemy 2.x, APScheduler (in-process `BackgroundScheduler`), pytest + testcontainers Postgres, ruff.

**Spec:** [docs/superpowers/specs/2026-06-12-admin-operator-levers-design.md](../specs/2026-06-12-admin-operator-levers-design.md)

**Deviation from spec (deliberate):** the banner key set drops `err=reenable` — no route path emits it (a missing `user_id` is a 404 like an unknown slug; an already-enabled user is a no-op `ok=reenable`). The spec was updated to match.

---

## File structure

| File | Change | Responsibility |
|------|--------|----------------|
| `src/call_manager/scheduler.py` | add `trigger_job_now` | Pure wrapper over the APScheduler job API: nudge a job to run now. |
| `src/call_manager/web/routes/admin.py` | add 3 routes, edit `admin_rerank`, extend `admin_index` + `_digest_panel_context` | The four levers + the context (disabled-users list, rerank estimate). |
| `src/call_manager/web/templates/admin.html` | banner, per-source fetch forms, disabled-users list + re-enable forms, send-digest form, rerank confirm/estimate | All new admin markup. |
| `src/call_manager/web/static/app.css` | add `.banner` + `.banner-ok` + `.banner-warn` | Feedback banner styling (also styles the pre-existing, currently-unstyled calibration banner). |
| `tests/test_scheduler_trigger.py` | create | Unit tests for `trigger_job_now`. |
| `tests/test_web_admin_levers.py` | create | Route + rendering tests for all four levers. |

No migration. No JS change (reuses the existing `form[data-confirm]` handler).

---

### Task 1: `trigger_job_now` helper

**Files:**
- Modify: `src/call_manager/scheduler.py` (add a module-level function after `build_scheduler`)
- Test: `tests/test_scheduler_trigger.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler_trigger.py`:

```python
from datetime import UTC, datetime

from call_manager.scheduler import trigger_job_now


class _FakeJob:
    def __init__(self) -> None:
        self.next_run_time = "UNSET"

    def modify(self, **kwargs) -> None:
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    def __init__(self, jobs: dict) -> None:
        self._jobs = dict(jobs)

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)


def test_trigger_job_now_false_when_scheduler_none():
    assert trigger_job_now(None, "ingest:vinnova") is False


def test_trigger_job_now_false_when_job_absent():
    sched = _FakeScheduler({})
    assert trigger_job_now(sched, "digest:weekly") is False


def test_trigger_job_now_nudges_job_and_returns_true():
    job = _FakeJob()
    sched = _FakeScheduler({"ingest:vinnova": job})
    result = trigger_job_now(sched, "ingest:vinnova")
    assert result is True
    assert isinstance(job.next_run_time, datetime)
    # Nudged to a UTC-aware "now"
    assert job.next_run_time.tzinfo is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_trigger.py -v`
Expected: FAIL with `ImportError: cannot import name 'trigger_job_now'`.

- [ ] **Step 3: Write minimal implementation**

In `src/call_manager/scheduler.py`, add this function immediately **after** the `build_scheduler` function's closing `return sched` (module level, not nested). `_dt` and `UTC` are already imported at the top of the file ([scheduler.py:4-5](../../../src/call_manager/scheduler.py#L4-L5)).

```python
def trigger_job_now(scheduler, job_id: str) -> bool:
    """Ask APScheduler to run the given job once, immediately, then resume its
    normal schedule. Returns False if the scheduler or the job is absent (e.g.
    the digest job when no email backend is configured).

    The job runs in APScheduler's background thread, reusing the exact tick
    closure with its already-wired clients. The jobs carry max_instances=1 +
    coalesce=True, so a double-trigger collapses to one run.
    """
    if scheduler is None:
        return False
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=_dt.now(UTC))
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_trigger.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/scheduler.py tests/test_scheduler_trigger.py
git commit -m "feat(scheduler): trigger_job_now — nudge a scheduled job to run once now"
```

---

### Task 2: Re-enable lever route

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (imports + new route)
- Test: `tests/test_web_admin_levers.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_admin_levers.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_reenable_clears_digest_disabled_at(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        bounced = User(email="bounced@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(bounced)
        s.commit()
        s.refresh(bounced)
        target_id = bounced.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"
    with pg_session_factory() as s:
        refreshed = s.get(User, target_id)
        assert refreshed.digest_disabled_at is None


def test_reenable_already_enabled_is_noop_ok(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        enabled = User(email="fine@ki.se", digest_disabled_at=None)
        s.add(enabled)
        s.commit()
        s.refresh(enabled)
        target_id = enabled.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"


def test_reenable_unknown_user_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": "999999"},
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_reenable_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="nonadmin@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/digest/reenable", data={"user_id": "1"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py -v`
Expected: FAIL — `test_reenable_*` return 404/405 (route not defined) instead of 303. (`test_reenable_requires_admin` may already pass since an unknown POST under no matching route is 404, not 403 — that's fine; it will pass once the route exists.)

- [ ] **Step 3: Write minimal implementation**

In `src/call_manager/web/routes/admin.py`, extend the FastAPI imports (line 5) to add `Form` and `HTTPException`:

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request
```

Then add this route at the end of the file (after `admin_rerank`):

```python
@router.post("/admin/digest/reenable")
def admin_reenable_user(
    request: Request,
    user_id: int = Form(...),
    user: User = Depends(require_admin),  # noqa: B008
):
    """Clear digest_disabled_at for a bounced user so the weekly digest
    resumes for them. Idempotent: a no-op if already enabled."""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        target = session.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown user")
        target.digest_disabled_at = None
        session.commit()
    return RedirectResponse(url="/admin?ok=reenable", status_code=303)
```

Note: `user_id: int = Form(...)` needs no `# noqa: B008` — ruff's B008 allow-list covers FastAPI's `Form`/`Query`; only `Depends` needs the noqa.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/routes/admin.py tests/test_web_admin_levers.py
git commit -m "feat(admin): re-enable a bounced digest user (POST /admin/digest/reenable)"
```

---

### Task 3: Fetch + Send-digest lever routes

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (import `trigger_job_now`, `Source`; 2 new routes)
- Test: `tests/test_web_admin_levers.py` (add a fake scheduler + tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py` (the `Source` and `datetime` imports below the existing ones; `_FakeJob`/`_FakeScheduler` are new helpers):

```python
class _FakeJob:
    def __init__(self) -> None:
        self.next_run_time = "UNSET"

    def modify(self, **kwargs) -> None:
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    """Minimal stand-in for the APScheduler instance on app.state.scheduler.

    A no-op shutdown() so lifespan teardown is harmless even if a test forgets
    to restore — but tests DO restore (swap-then-restore) so the real scheduler
    is the one that actually shuts down."""

    def __init__(self, jobs: dict) -> None:
        self._jobs = dict(jobs)

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)

    def shutdown(self, wait: bool = True) -> None:
        return None


def test_fetch_source_nudges_ingest_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        job = _FakeJob()
        app.state.scheduler = _FakeScheduler({"ingest:vinnova": job})
        try:
            r = client.post(
                "/admin/sources/vinnova/fetch", follow_redirects=False
            )
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?ok=fetch"
            assert isinstance(job.next_run_time, datetime)
        finally:
            app.state.scheduler = real


def test_fetch_unknown_source_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/sources/does-not-exist/fetch", follow_redirects=False
        )
    assert r.status_code == 404


def test_fetch_err_when_job_missing(monkeypatch, pg_url, pg_session_factory):
    # Valid source row, but no ingest job in the (fake) scheduler — e.g. a
    # stale DB row whose source was removed from the registry.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})
        try:
            r = client.post(
                "/admin/sources/vinnova/fetch", follow_redirects=False
            )
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?err=fetch"
        finally:
            app.state.scheduler = real


def test_send_digest_nudges_weekly_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        job = _FakeJob()
        app.state.scheduler = _FakeScheduler({"digest:weekly": job})
        try:
            r = client.post("/admin/digest/send", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?ok=digest"
            assert isinstance(job.next_run_time, datetime)
        finally:
            app.state.scheduler = real


def test_send_digest_unconfigured_err(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})  # no digest:weekly job
        try:
            r = client.post("/admin/digest/send", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?err=digest_unconfigured"
        finally:
            app.state.scheduler = real


def test_fetch_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="nonadmin2@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/sources/vinnova/fetch")
    assert r.status_code == 403
```

No new top-level imports are needed in the test file: these tests POST to the slug string and let the route look up `Source` server-side; `datetime` and `User` are already imported from Task 2. (Do **not** add a `Source` import here — it would be an unused F401.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py -v -k "fetch or send_digest"`
Expected: FAIL — routes return 404/405 (not defined).

- [ ] **Step 3: Write minimal implementation**

In `src/call_manager/web/routes/admin.py`, add `trigger_job_now` to the imports (top-level import is safe — verified no cycle between `scheduler.py` and `web.routes.admin`):

```python
from call_manager.scheduler import trigger_job_now
```

`Source` is already imported in `admin.py` (line 18). Add these two routes after `admin_reenable_user`:

```python
def _scheduler(request: Request):
    """The app's APScheduler, or None if absent (e.g. before lifespan)."""
    return getattr(request.app.state, "scheduler", None)


@router.post("/admin/sources/{slug}/fetch")
def admin_fetch_source(
    slug: str,
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    """Trigger the per-source ingest job (id=ingest:{slug}) to run now."""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        src = session.execute(
            select(Source).where(Source.slug == slug)
        ).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="unknown source")
    ok = trigger_job_now(_scheduler(request), f"ingest:{slug}")
    key = "ok=fetch" if ok else "err=fetch"
    return RedirectResponse(url=f"/admin?{key}", status_code=303)


@router.post("/admin/digest/send")
def admin_send_digest(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    """Trigger the weekly digest job to run now. The job is absent when no
    email backend is configured; run_digest is week-idempotent, so a manual
    run during the same week safely de-dupes via the DigestRun claim."""
    ok = trigger_job_now(_scheduler(request), "digest:weekly")
    key = "ok=digest" if ok else "err=digest_unconfigured"
    return RedirectResponse(url=f"/admin?{key}", status_code=303)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py -v`
Expected: PASS (all lever tests).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/routes/admin.py tests/test_web_admin_levers.py
git commit -m "feat(admin): fetch-now (per source) + send-digest-now levers via trigger_job_now"
```

---

### Task 4: Re-rank redirect carries `ok=rerank`

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (`admin_rerank` redirect URL)
- Test: `tests/test_web_admin_levers.py` (add one assertion-focused test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py`:

```python
def test_rerank_redirects_with_ok_key(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post("/admin/rerank", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=rerank"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py::test_rerank_redirects_with_ok_key -v`
Expected: FAIL — `location` is `/admin` (no query), not `/admin?ok=rerank`.

- [ ] **Step 3: Write minimal implementation**

In `src/call_manager/web/routes/admin.py`, change the final line of `admin_rerank` from:

```python
    return RedirectResponse(url="/admin", status_code=303)
```

to:

```python
    return RedirectResponse(url="/admin?ok=rerank", status_code=303)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py::test_rerank_redirects_with_ok_key -v`
Expected: PASS.

Also re-run the pre-existing rerank tests to confirm no regression (they assert `status_code == 303`, which still holds):
Run: `uv run pytest tests/test_web_admin_ranking.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/routes/admin.py tests/test_web_admin_levers.py
git commit -m "feat(admin): rerank redirects with ok=rerank for banner feedback"
```

---

### Task 5: Feedback banner (template + CSS)

**Files:**
- Modify: `src/call_manager/web/templates/admin.html` (banner block near the top)
- Modify: `src/call_manager/web/static/app.css` (`.banner` + variants)
- Test: `tests/test_web_admin_levers.py` (rendering tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py`:

```python
def test_ok_fetch_renders_success_banner(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=fetch")
    assert r.status_code == 200
    assert "banner-ok" in r.text
    assert "Fetch triggered" in r.text


def test_err_digest_unconfigured_renders_warning_banner(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?err=digest_unconfigured")
    assert r.status_code == 200
    assert "banner-warn" in r.text
    assert "isn&#39;t configured" in r.text or "isn't configured" in r.text


def test_unknown_banner_key_renders_no_banner(
    monkeypatch, pg_url, pg_session_factory
):
    # A tampered/garbage key must NOT be echoed and must NOT render a banner.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=<script>alert(1)</script>")
    assert r.status_code == 200
    assert "banner-ok" not in r.text
    assert "alert(1)" not in r.text  # raw param never echoed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py -v -k banner`
Expected: FAIL — `banner-ok`/`banner-warn` text not present (no banner block yet).

- [ ] **Step 3: Write the template block**

In `src/call_manager/web/templates/admin.html`, insert the following **immediately after** the closing `</nav>` of the `subnav` (after line 12, before the `<article>` Source-health block). This maps fixed keys to fixed messages — the raw query param is never interpolated into the output.

```jinja
{% set _ok = request.query_params.get('ok') %}
{% set _err = request.query_params.get('err') %}
{% set _ok_msgs = {
  'fetch': 'Fetch triggered — the source will refresh shortly.',
  'digest': 'Weekly digest triggered — it will send shortly.',
  'reenable': 'User re-enabled for the weekly digest.',
  'rerank': 'Re-ranking enqueued for all active profiles.'
} %}
{% set _err_msgs = {
  'fetch': "Couldn't trigger the fetch — the scheduler or this source's job is unavailable.",
  'digest_unconfigured': "Digest isn't configured (no email backend), so it can't be sent."
} %}
{% if _ok and _ok in _ok_msgs %}
<div class="banner banner-ok" role="status">{{ _ok_msgs[_ok] }}</div>
{% elif _err and _err in _err_msgs %}
<div class="banner banner-warn" role="alert">{{ _err_msgs[_err] }}</div>
{% endif %}
```

- [ ] **Step 4: Add the CSS**

In `src/call_manager/web/static/app.css`, append to the end of **section 12 (Callouts, modal, utilities)** — locate it after the `.htmx-error-notice` rule (around line 1050). No `.banner*` rule exists today, so this is the only place the classes are defined:

```css
/* Operational feedback banners (admin levers). Also styles the pre-existing
   calibration warning, whose `banner banner-warn` markup was unstyled. */
.banner {
  border-radius: 0.6rem;
  padding: 0.7rem 1rem;
  font-size: 0.9rem;
  margin: 0 0 1rem;
  border: 1px solid var(--cm-border);
  background: var(--cm-surface-sunken);
}
.banner-ok {
  background: var(--cm-good-bg);
  border-color: var(--cm-good-border);
  color: var(--cm-good-text);
}
.banner-warn {
  background: var(--cm-warn-bg);
  border-color: var(--cm-warn-border);
  color: var(--cm-warn-text);
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py -v -k banner`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/admin.html src/call_manager/web/static/app.css tests/test_web_admin_levers.py
git commit -m "feat(admin): fixed-message feedback banner (ok/err keys) + banner CSS"
```

---

### Task 6: Per-source Fetch-now buttons

**Files:**
- Modify: `src/call_manager/web/templates/admin.html` (Source-health table)
- Test: `tests/test_web_admin_levers.py` (rendering test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py`:

```python
def test_source_rows_have_fetch_forms(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # vinnova is a migration-seeded source; its row carries a fetch form.
    assert 'action="/admin/sources/vinnova/fetch"' in r.text
    assert "Fetch now" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py::test_source_rows_have_fetch_forms -v`
Expected: FAIL — no fetch form in markup.

- [ ] **Step 3: Edit the Source-health table**

In `src/call_manager/web/templates/admin.html`, update the table head to add an Actions column and each row to add a fetch form. Replace the existing `<thead>`…`</thead>` and the row `<tr>`…`</tr>` inside `{% for s in sources %}`:

```jinja
    <thead>
      <tr>
        <th>Source</th>
        <th>Last fetched</th>
        <th>Calls in DB</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for s in sources %}
        <tr>
          <td>{{ s.name }} <code class="muted">{{ s.slug }}</code></td>
          <td>{% if s.last_fetched_at %}{{ s.last_fetched_at | display_dt }}{% else %}<span class="muted">never</span>{% endif %}</td>
          <td><strong>{{ s.call_count }}</strong></td>
          <td>
            <form method="post" action="/admin/sources/{{ s.slug }}/fetch">
              <button type="submit" class="secondary outline">Fetch now</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </tbody>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py::test_source_rows_have_fetch_forms -v`
Expected: PASS.

Confirm the pre-existing source-listing test still passes:
Run: `uv run pytest tests/test_web_admin.py::test_admin_lists_sources_with_status -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/templates/admin.html tests/test_web_admin_levers.py
git commit -m "feat(admin): per-source Fetch-now button in the source-health table"
```

---

### Task 7: Disabled-users list + re-enable forms + send-digest form

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (`_digest_panel_context`: add the disabled-users list)
- Modify: `src/call_manager/web/templates/admin.html` (digest panel)
- Test: `tests/test_web_admin_levers.py` (rendering test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py`:

```python
def test_digest_panel_lists_disabled_user_with_reenable_form(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="bounced2@ki.se", digest_disabled_at=datetime.now(UTC)))
        s.commit()
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # The disabled user's email is listed (not just a count)…
    assert "bounced2@ki.se" in r.text
    # …with a re-enable form targeting the lever route.
    assert 'action="/admin/digest/reenable"' in r.text
    assert "Re-enable" in r.text
    # And the send-digest lever is present.
    assert 'action="/admin/digest/send"' in r.text
    assert "Send digest now" in r.text


def test_digest_panel_shows_none_when_no_disabled_users(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "No disabled users." in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py -v -k "disabled or digest_panel"`
Expected: FAIL — list/forms not present.

- [ ] **Step 3: Add the disabled-users list to the context**

In `src/call_manager/web/routes/admin.py`, edit `_digest_panel_context` (lines 29-46). Keep the existing `disabled_users` count (the pre-existing test asserts it) and **add** a list. Replace the `return {…}` with the version that also returns the rows:

```python
    disabled_users = session.execute(
        select(func.count(User.id)).where(User.digest_disabled_at.is_not(None))
    ).scalar_one()
    disabled_rows = session.execute(
        select(User.id, User.email, User.digest_disabled_at)
        .where(User.digest_disabled_at.is_not(None))
        .order_by(User.digest_disabled_at.desc())
    ).all()
    return {
        "digest_last_sent_at": last_run.sent_at if last_run else None,
        "digest_runs_this_week": runs_this_week,
        "digest_disabled_users": disabled_users,
        "digest_disabled_user_rows": [
            {"id": r.id, "email": r.email, "digest_disabled_at": r.digest_disabled_at}
            for r in disabled_rows
        ],
    }
```

- [ ] **Step 4: Edit the digest panel template**

In `src/call_manager/web/templates/admin.html`, replace the Digest `<article>` (lines 72-80) with a version that keeps the existing `<dt>/<dd>` rows (so `test_admin_panel_shows_disabled_user_count` stays green), adds the disabled-users list with re-enable forms, and adds the send-digest form:

```jinja
<article>
  <header><strong>Digest (weekly)</strong></header>
  <dl>
    <dt>Last digest sent</dt>
    <dd>{% if digest_last_sent_at %}{{ digest_last_sent_at | display_dt }}{% else %}<span class="muted">No digests sent</span>{% endif %}</dd>
    <dt>Runs this week</dt><dd>{{ digest_runs_this_week }}</dd>
    <dt>Disabled users (3+ bounces)</dt><dd>{{ digest_disabled_users }}</dd>
  </dl>

  {% if digest_disabled_user_rows %}
  <ul class="disabled-users">
    {% for du in digest_disabled_user_rows %}
    <li>
      {{ du.email }}
      <span class="muted">disabled {{ du.digest_disabled_at | display_dt }}</span>
      <form method="post" action="/admin/digest/reenable">
        <input type="hidden" name="user_id" value="{{ du.id }}">
        <button type="submit" class="secondary outline">Re-enable</button>
      </form>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="muted">No disabled users.</p>
  {% endif %}

  <form method="post" action="/admin/digest/send">
    <button type="submit" class="secondary">Send digest now</button>
  </form>
</article>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py -v -k "disabled or digest_panel"`
Expected: PASS.

Confirm the pre-existing count test still passes:
Run: `uv run pytest tests/test_web_admin_digest_panel.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin.html tests/test_web_admin_levers.py
git commit -m "feat(admin): disabled-users list with Re-enable + Send-digest-now lever"
```

---

### Task 8: Re-rank confirm + count estimate

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (`admin_index`: compute `rerank_estimate`)
- Modify: `src/call_manager/web/templates/admin.html` (rerank form: `data-confirm` + estimate text)
- Test: `tests/test_web_admin_levers.py` (rendering test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_admin_levers.py`:

```python
def test_rerank_button_shows_estimate_and_confirm(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from datetime import timedelta

    from call_manager.db.models import Call, Source

    # One active profile (factory) × one recent call ⇒ estimate of 1.
    profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="E",
                url="https://x/e",
                title="T",
                published_at=datetime.now(UTC) - timedelta(days=5),
            )
        )
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # The destructive form carries a confirm prompt…
    assert "data-confirm=" in r.text
    # …and the page states the upper-bound job count (1 profile × 1 call).
    assert "up to ~1" in r.text or "up to 1" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_admin_levers.py::test_rerank_button_shows_estimate_and_confirm -v`
Expected: FAIL — no `data-confirm` / estimate text.

- [ ] **Step 3: Compute the estimate in `admin_index`**

In `src/call_manager/web/routes/admin.py`, add the import near the top (with the other `call_manager` imports):

```python
from call_manager.ranking.backfill import BACKFILL_DAYS, backfill_profile
```

(`backfill_profile` is already imported on line 24 — replace that line so both names come from the one import.)

Then, inside `admin_index`, within the `with session_factory() as session:` block (after the `queue_counts` computation, before building `context`), add:

```python
        active_profiles_count = session.execute(
            select(func.count(Profile.id)).where(Profile.is_active.is_(True))
        ).scalar_one()
        recent_cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
        recent_calls_count = session.execute(
            select(func.count(Call.id)).where(Call.published_at >= recent_cutoff)
        ).scalar_one()
        rerank_estimate = active_profiles_count * recent_calls_count
```

Add `rerank_estimate`, `active_profiles_count`, and `recent_calls_count` to the `context` dict (alongside `queue_counts`):

```python
            "rerank_estimate": rerank_estimate,
            "active_profiles_count": active_profiles_count,
            "recent_calls_count": recent_calls_count,
```

Add `timedelta` to the datetime import at the top of `admin.py` (line 3):

```python
from datetime import UTC, datetime, timedelta
```

(`Call` and `Profile` are already imported on lines 12 and 16.)

- [ ] **Step 4: Edit the rerank form template**

In `src/call_manager/web/templates/admin.html`, replace the rerank form (lines 44-46) with a version carrying `data-confirm` and an estimate line. The `data-confirm` reuses the existing `form[data-confirm]` handler in `app.js` (no new JS):

```jinja
  <form method="post" action="/admin/rerank"
        data-confirm="This enqueues up to ~{{ rerank_estimate }} ranking jobs and may take a while. Continue?">
    <button type="submit" class="secondary">Re-rank everything (last 90 days × active profiles)</button>
  </form>
  <p class="muted">Enqueues up to ~{{ rerank_estimate }} ranking jobs
    ({{ active_profiles_count }} active profiles × {{ recent_calls_count }} recent calls; upper bound — dedup and dead-letter suppression mean the real count is lower).</p>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_web_admin_levers.py::test_rerank_button_shows_estimate_and_confirm -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin.html tests/test_web_admin_levers.py
git commit -m "feat(admin): rerank confirm + upper-bound job-count estimate"
```

---

### Task 9: Whole-feature verification

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check src/call_manager/scheduler.py src/call_manager/web/routes/admin.py tests/test_scheduler_trigger.py tests/test_web_admin_levers.py`
Expected: `All checks passed!` (watch for F401 unused imports, B008 on non-Form params).

Run: `uv run ruff format --check src/call_manager/web/routes/admin.py src/call_manager/scheduler.py tests/test_web_admin_levers.py tests/test_scheduler_trigger.py`
Expected: no reformatting needed (run `uv run ruff format <paths>` and amend if it does).

- [ ] **Step 2: Full admin + scheduler test sweep**

Run: `uv run pytest tests/test_web_admin.py tests/test_web_admin_digest_panel.py tests/test_web_admin_ranking.py tests/test_web_admin_levers.py tests/test_scheduler_trigger.py -v`
Expected: all pass — including the pre-existing `test_admin_panel_shows_disabled_user_count` (count row preserved) and `test_admin_lists_sources_with_status` (source rows + dates still render).

- [ ] **Step 3: Manual smoke (optional, document result)**

If a dev DB + Ollama are available, follow the dev-run recipe and visit `/admin`: confirm each lever renders, the rerank confirm dialog fires, and `?ok=fetch` shows a green banner. Note the outcome in the PR/commit description. If not available, state that the smoke was skipped and the route/render tests are the coverage.

- [ ] **Step 4: Final commit (if formatting/lint produced changes)**

```bash
git add -A
git commit -m "chore(admin): lint/format pass for operator levers"
```

(Skip if nothing changed.)

---

## Self-review notes (for the implementer)

- **Spec coverage:** Task 1 = execution model (`trigger_job_now`); Task 2 = re-enable lever; Task 3 = fetch + send-digest levers; Task 4 = rerank redirect key; Task 5 = feedback banner (component E) + CSS; Task 6 = per-source fetch button (component C); Task 7 = disabled-users list + re-enable forms + send-digest button (component C); Task 8 = rerank confirm + estimate (component D). Error handling (404 unknown slug/user; `err=` on absent scheduler/job; double-click safety via `max_instances=1`/`coalesce`) is exercised in Tasks 2-3.
- **Known test flakiness (NOT a regression):** every test in `test_web_admin_levers.py` takes `pg_session_factory` in its signature, so the "first pg_url-only test crashes on a missing `references` table" failure mode does not apply. The fetch/send tests swap `app.state.scheduler` for a fake **and restore the real one in a `finally` before the `with client:` block exits**, so lifespan shutdown still calls `.shutdown()` on the real scheduler (the fake also has a no-op `shutdown` as belt-and-suspenders).
- **No new JS:** the rerank confirm reuses the existing `form[data-confirm]` handler ([app.js:29-35](../../../src/call_manager/web/static/app.js#L29-L35)).
- **CSRF:** all levers POST through `OriginCSRFMiddleware`, which allows a missing Origin (TestClient) and same-origin browser forms — same path the existing `/admin/rerank` form already uses.
