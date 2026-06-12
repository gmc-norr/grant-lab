# Admin Operator Levers — Design Spec

**Date:** 2026-06-12
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager

## Purpose

"Every recovery path the admin owns ends at SSH or psql" (Theme 6 of the 2026-06-11 product review). The `/admin` page is read-only except for one unguarded button. This slice gives the operator the routine recovery actions **from the UI**, each wrapping an already-safe backend, and puts a guard on the one dangerous button — removing the standing SSH/psql dependency for day-to-day operations.

### The gaps, with evidence (current code)

1. **No way to trigger a fetch.** Ingestion only runs on its daily cron ([scheduler.py:112-118](../../../src/call_manager/scheduler.py#L112-L118), one job per source: `id="ingest:{slug}"`). A stuck/empty source can't be re-fetched without shell access.
2. **No way to send the digest.** It only fires Monday 07:00 ([scheduler.py:276-287](../../../src/call_manager/scheduler.py#L276-L287), `id="digest:weekly"`). `run_digest` is week-idempotent via the `DigestRun` unique claim ([send.py:81-123](../../../src/call_manager/digest/send.py#L81-L123)), so a manual run is safe by construction — but there's no trigger.
3. **No way to re-enable a bounced user.** `User.digest_disabled_at` ([models.py:58](../../../src/call_manager/db/models.py#L58)) is set on 3+ bounces and **never cleared by any code**; the admin page shows only a *count* of disabled users ([admin.py:39-45](../../../src/call_manager/web/routes/admin.py#L39-L45)), not who, with no re-enable.
4. **The one action is the dangerous one.** `POST /admin/rerank` ([admin.py:238](../../../src/call_manager/web/routes/admin.py#L238)) enqueues `active profiles × 90 days of calls` with a single unguarded click — no count preview, no confirmation ([admin.html](../../../src/call_manager/web/templates/admin.html) "Re-rank everything").

## Goals

- An operator can **fetch a source**, **send the digest**, and **re-enable a bounced user** from `/admin`, with no shell.
- The dangerous **re-rank** button shows what it will do (a job-count estimate) and asks for confirmation.
- Each lever is **non-blocking** (no reverse-proxy timeout) and **safe to repeat** (idempotent backends).
- No regression to the read-only panels; no schema change.

## Execution model (the core decision)

`run_ingestion` (HTTP fetch + parse + embed) and `run_digest` (email sends) are too slow to run inside a request. Instead, a lever **nudges the existing scheduled job to run immediately** and returns:

```python
# src/call_manager/scheduler.py
def trigger_job_now(scheduler, job_id: str) -> bool:
    """Ask APScheduler to run the given job once, now. Returns False if the
    scheduler or the job is absent (e.g. digest unconfigured)."""
    if scheduler is None:
        return False
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=datetime.now(UTC))
    return True
```

The job runs once in APScheduler's background thread — reusing the exact tick with its already-wired clients (pubmed/ollama/embed/email) — then resumes its normal cron. The POST handler just calls `trigger_job_now` and redirects. The jobs already carry `max_instances=1` + `coalesce=True`, so a double-click can't double-run.

*Rejected alternative:* run `run_ingestion`/`run_digest` synchronously in the request — blocks the worker, risks a proxy timeout, and forces re-wiring every client the tick closures already capture.

## Components

### A. `trigger_job_now` helper (`scheduler.py`)

The function above — a pure, unit-testable wrapper over the APScheduler job API. Imported by the admin routes. (`datetime`/`UTC` are already imported in `scheduler.py`.)

### B. Lever routes (`web/routes/admin.py`)

All four are `POST`, gated by `require_admin` (existing dependency), and pass through `OriginCSRFMiddleware` like every other form. Each redirects (`303`) to `/admin?ok=<key>` or `/admin?err=<key>`.

1. **`POST /admin/sources/{slug}/fetch`** — validate the slug against `Source` (404 if unknown), then `trigger_job_now(scheduler, f"ingest:{slug}")` → `ok=fetch` / `err=fetch`.
2. **`POST /admin/digest/send`** — `trigger_job_now(scheduler, "digest:weekly")` → `ok=digest` / `err=digest_unconfigured` (the job is absent when no email is configured — [scheduler.py:247](../../../src/call_manager/scheduler.py#L247)).
3. **`POST /admin/digest/reenable`** — `user_id` form field; load the `User` (404 if missing); set `digest_disabled_at = None` (idempotent — a no-op if already enabled); commit → `ok=reenable`. Pure DB, no scheduler.
4. **`POST /admin/rerank`** — unchanged enqueue logic; redirect to `?ok=rerank` (was a bare redirect). The confirmation is template-side (component D).

`scheduler` is read from `request.app.state.scheduler`.

### C. Admin page additions (`admin.py` `admin_index` + `admin.html`)

- **Per-source Fetch button:** a small `POST /admin/sources/{slug}/fetch` form in each Source-health row.
- **Disabled-users list:** replace the bare "Disabled users" count with a list — query `User.id, User.email, User.digest_disabled_at WHERE digest_disabled_at IS NOT NULL ORDER BY digest_disabled_at DESC` — each row showing email + disabled-at + a "Re-enable" `POST /admin/digest/reenable` form (hidden `user_id`). Keep the count as a heading. When the list is empty, show "none".
- **Send-digest button:** a `POST /admin/digest/send` form in the Digest panel.

### D. Re-rank confirm + count preview (`admin.py` + `admin.html`)

- `admin_index` computes `rerank_estimate = active_profiles_count × calls_in_last_90_days_count` — an **upper bound** (the partial-unique dedup means the real enqueue is ≤ this). The 90-day window mirrors `backfill_profile`'s window.
- The Re-rank button renders the estimate ("Enqueues up to ~N ranking jobs") and carries `data-confirm="This can enqueue up to N ranking jobs and take a while. Continue?"`, reusing the existing `form[data-confirm]` handler in `app.js` (no new JS; CSP-safe).

### E. Feedback banner (`admin.html`)

The redirect carries `?ok=<key>` / `?err=<key>`. The template maps a **fixed set of keys to fixed messages** (never echoes the raw param — no XSS) and renders a dismissible banner: `.banner.banner-ok` (success, green — new style) or `.banner.banner-warn` (error, existing style). Unknown/absent key → no banner. Keys: `ok ∈ {fetch, digest, reenable, rerank}`, `err ∈ {fetch, digest_unconfigured, reenable}`.

## Data flow (example: fetch a stuck source)

Admin sees Vinnova "Last fetched: 3 days ago" → clicks **Fetch now** on that row → `POST /admin/sources/vinnova/fetch` → `trigger_job_now(scheduler, "ingest:vinnova")` sets the job's next run to now → `303 /admin?ok=fetch` → green banner "Fetch triggered for vinnova — refresh in a moment" → APScheduler runs the `ingest:vinnova` tick in the background → on refresh, "Last fetched" and "Calls in DB" update.

## Error handling / edge cases

- **Scheduler or job absent** (`trigger_job_now → False`): redirect with `err=` (e.g. `err=digest_unconfigured`) and a warning banner — never a 500, never a silent no-op.
- **Unknown source slug:** 404 (tampered request; every rendered button has a real slug).
- **Re-enable a missing user:** 404. Re-enabling an already-enabled user: harmless no-op, still `ok=reenable`.
- **Redundant send-digest** (week already sent): `run_digest` skips via the `DigestRun` claim; the banner still says "triggered" (the no-op is the safe, correct outcome).
- **Double-click any lever:** `max_instances=1` + `coalesce=True` collapse it to one run.
- **Non-admin:** `require_admin` → 403 (existing behavior).

## Non-goals (later Theme 6 slices, explicitly deferred)

Health visibility (Ollama up/down card; source `last_error`/streak/stale columns; scheduler last-run/next-run panel); **retry dead-letter jobs** and queue draining-vs-stuck (ranking-adjacent — coordinate with the in-flight ranking work); persistent jobstore / startup digest catch-up; drafting kill-switch toggle; a "fetch ALL sources at once" button (per-source covers the need). The `/healthz` endpoint is untouched.

## Testing

Following `tests/test_web_admin*.py` patterns (TestClient + `pg_session_factory`, an admin user via `require_admin`):

- **`trigger_job_now` unit tests:** `scheduler=None → False`; a fake scheduler whose `get_job` returns `None → False`; a fake job → `modify` called with a `next_run_time` and returns `True`.
- **Re-enable:** seed a `User` with `digest_disabled_at` set; `POST /admin/digest/reenable` (admin) → `303` to `/admin?ok=reenable`; DB shows `digest_disabled_at IS NULL`. Missing `user_id` → 404.
- **Fetch:** inject a fake scheduler on `app.state.scheduler` (after startup); `POST /admin/sources/{slug}/fetch` → `303 ?ok=fetch` and the `ingest:{slug}` job was nudged; unknown slug → 404.
- **Send digest:** fake scheduler with a `digest:weekly` job → `303 ?ok=digest`; without it → `303 ?err=digest_unconfigured`.
- **Admin-gated:** a non-admin user → 403 on every lever POST.
- **Admin page rendering:** the disabled-users list shows a seeded disabled user's email + a Re-enable form; each source row has a Fetch form; the Re-rank button shows the estimate and a `data-confirm`; `?ok=fetch` renders the success banner and `?err=digest_unconfigured` the warning banner (and the rendered text is the fixed message, not the raw param).

## Files

- Modify: `src/call_manager/scheduler.py` (add `trigger_job_now`).
- Modify: `src/call_manager/web/routes/admin.py` (4 lever routes; disabled-users list + rerank estimate in `admin_index`; redirect params).
- Modify: `src/call_manager/web/templates/admin.html` (per-source Fetch forms; disabled-users list + Re-enable forms; Send-digest form; rerank estimate + `data-confirm`; the `?ok`/`?err` banner).
- Modify: `src/call_manager/web/static/app.css` (add `.banner-ok`).
- Modify/Add tests: `tests/test_web_admin_*.py` (or a new `tests/test_web_admin_levers.py`) + a unit test for `trigger_job_now`.

No migration.
