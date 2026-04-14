# Call Monitor — Design Spec

**Date:** 2026-04-14
**Status:** Draft for implementation
**Audience:** Engineers and stakeholders at the hospital

## Purpose

An internal hospital tool that aggregates funding calls (grants, EU calls, etc.) from multiple sources, ranks them with an LLM against organization- and group-level profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest.

The wedge over existing tools (e.g., Ymner) is **smarter ranking against multiple bespoke profiles** — not keyword filtering, but LLM-evaluated relevance against a profile that captures a research group's actual priorities, past funded work, and constraints.

## Scope (v1)

**In scope:**

- Two ingestion sources: EU Funding & Tenders Portal, Vinnova
- Multi-profile ranking (organization-level + research-group-level + user-level)
- Profile bootstrap from publications (PubMed) and pasted past grants
- Web dashboard for browsing ranked calls
- Weekly email digest per profile subscription
- Magic-link email auth, restricted to a hospital email domain

**Explicitly out of scope for v1** (see Roadmap):

- Application tracking, deadlines management, application-prep helpers
- Per-profile access control / private profiles
- Slack / Teams delivery
- Mobile-optimized UI
- Public/external user access

## Users & scale

- 5–15 profiles (a few group-level + organization-level + a handful of user-level)
- 20–50 hospital users
- Single university hospital, internal use only
- No commercialization

## Architecture

A single FastAPI app with internally bounded modules and a single Postgres database. Background jobs run inside the same app via APScheduler.

### Modules

Each module is independently testable and depends only on the database (not on other modules).

- `sources/` — one adapter per source. Each exposes `fetch_new_calls(since: datetime) -> Iterator[NormalizedCall]`. Adding a source = adding a file.
- `ingestion/` — orchestrator that runs source adapters on schedule, deduplicates, writes to `calls`, enqueues ranking jobs.
- `ranking/` — given `(call, profile)`, returns `(score 0–100, rationale)`. Calls Anthropic Claude Haiku 4.5 with prompt caching on profile blocks.
- `profiles/` — CRUD + bootstrap flow (PubMed + LLM → draft → user edit).
- `digest/` — weekly job that assembles and sends per-recipient digest emails.
- `web/` — FastAPI routes + Jinja2 templates + HTMX. Magic-link auth.
- `db/` — SQLAlchemy models, Alembic migrations, session management.

### Data flow

1. `ingestion` runs daily (one job per source, staggered) → upserts new rows into `calls`.
2. For each new call × each active profile → `ranking` writes a row to `call_rankings`.
3. Users browse via `web` (filtered/sorted by rank for their selected profile).
4. Weekly, `digest` queries top-ranked recent calls per subscribed profile per user → sends an email.

## Data model

Seven core tables.

```
sources
  id, name, slug, last_fetched_at, config (jsonb)

calls
  id, source_id, external_id, url, title, summary,
  full_text, deadline, budget_min, budget_max, currency,
  programme, keywords (text[]), published_at, fetched_at,
  raw (jsonb)
  UNIQUE(source_id, external_id)

profiles
  id, name, kind ('org'|'group'|'user'),
  description (text),
  structured (jsonb)   -- {areas[], budget_min, budget_max, trl_min, trl_max, hard_exclusions[]}
  is_active (bool, default true)
  created_by (FK users(id)), created_at, updated_at

call_rankings
  call_id, profile_id, score (int 0-100), rationale (text),
  model (text), prompt_version (text), ranked_at (timestamptz)
  PRIMARY KEY(call_id, profile_id)

users
  id, email, name, is_admin (bool), created_at, last_login_at

magic_link_tokens
  id, user_id (FK users(id)), token_hash (text),
  created_at (timestamptz), expires_at (timestamptz),
  consumed_at (timestamptz, null)
  INDEX on token_hash

subscriptions
  user_id, profile_id, digest_enabled (bool)
  PRIMARY KEY(user_id, profile_id)
```

Plus three small auxiliary tables:

```
user_call_dismissals
  user_id, call_id, dismissed_at (timestamptz)
  PRIMARY KEY(user_id, call_id)

digest_runs
  id, user_id (FK users(id)), sent_at (timestamptz),
  included_call_ids (int[])

ranking_jobs
  id, call_id (FK calls(id)), profile_id (FK profiles(id)),
  status ('pending'|'running'|'failed'|'done'),
  attempts (int, default 0), last_error (text, null),
  created_at (timestamptz), updated_at (timestamptz)
  INDEX on (status, updated_at)
```

**Notes:**

- `calls.raw` retains the original payload so we can re-extract or re-rank without re-fetching.
- `call_rankings` is recomputed on profile change (delete + re-rank) or new call (insert per profile). `prompt_version` (a short string bumped when the prompt template changes) makes stale rows identifiable without relying solely on `ranked_at`.
- All profile kinds share one schema — distinguished only by `kind`.
- **Profile deletion is soft.** `profiles.is_active` defaults `true`; setting it `false` removes the profile from feeds, ranking backfills, and digests, but preserves `call_rankings` and `subscriptions` history for audit. Hard delete is never exposed in the UI; available via a direct SQL command if a profile needs to be fully removed. FKs cascade on hard delete for test cleanliness.
- `subscriptions` controls digest delivery, independent of viewability — for v1, all logged-in users can view all `is_active` profiles. Subscriptions to a soft-deleted profile are ignored at digest time but rows are kept (re-activating the profile restores them).
- **All timestamp columns are `TIMESTAMPTZ`.** Adapters must normalize source timestamps to UTC before yielding a `NormalizedCall`. The web layer converts to `Europe/Stockholm` for display; the DB stores UTC. Deadlines especially matter: an EU call's "23:59 Brussels time" must be stored as the correct UTC instant, not as a naive timestamp.
- **Indexing.** GIN index on `calls.keywords` (text[]) for keyword filtering; B-tree on `calls.deadline` and `calls.published_at` for feed sorts; B-tree on `(call_rankings.profile_id, call_rankings.score DESC)` for ranked feeds; unique index on `magic_link_tokens.token_hash`.
- **`digest_runs.included_call_ids` as int[]** is fine for v1 audit needs. If we later want richer per-call audit (e.g., the score at the time of send, or filtering "show me everything that included call X"), migrate to a `digest_run_calls` join table.

## Ingestion

### Adapter contract

```python
class SourceAdapter(Protocol):
    slug: str
    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]: ...
```

`NormalizedCall` is a dataclass matching the `calls` table fields. Adapters know nothing about the database — they translate source-specific shapes into this common shape and yield them.

### v1 adapters

- **`eu_portal.py`** — uses the EU Funding & Tenders Portal search API (returns JSON). Pulls calls with status `OPEN` or `FORTHCOMING`. No scraping.
- **`vinnova.py`** — uses Vinnova's open data API at `data.vinnova.se`. Falls back to scraping `vinnova.se/sok-finansiering/` if the API proves insufficient. Adapter hides the choice from the rest of the system.

### Scheduling

APScheduler runs each adapter daily at staggered times (e.g., 03:00, 03:15 Europe/Stockholm). The ingestion runner:

1. Calls `adapter.fetch_new_calls(since=source.last_fetched_at)`
2. Upserts each `NormalizedCall` by `(source_id, external_id)` — idempotent
3. For genuinely new rows, enqueues a ranking job (in-process queue at this scale)
4. Updates `source.last_fetched_at` only if the run completed end-to-end without error. **Partial failures** (e.g., 40 of 50 records inserted, then a network error) do *not* advance the timestamp — the next run re-fetches from the same `since`, and the idempotent upsert handles already-seen records. This trades a small amount of duplicate work for guaranteed at-least-once ingestion.

**Multi-instance future.** APScheduler runs in-process and is safe with one instance. If we ever run multiple replicas (probably not at this scale), we need either a single dedicated scheduler instance or a database-backed lock (e.g., `pg_advisory_lock` keyed by source slug) to prevent duplicate concurrent ingest jobs. Out of scope for v1; flagged here so we don't paint ourselves into a corner.

### Failure isolation

Each adapter run is wrapped — one source's outage does not stop another. Failures log + increment a counter; three consecutive failures send an admin alert email.

## Ranking

### Job

Given `(call, profile)`, return `(score: int 0–100, rationale: str)`.

### Prompt shape

A single Claude call per pair. The profile block is marked with `cache_control` so subsequent rankings against the same profile hit the prompt cache.

```
SYSTEM (cached per profile):
  You are a research-funding analyst for [hospital name].
  Score how relevant a funding call is to this profile.

  PROFILE:
    Name: <profile.name>
    Kind: <org|group|user>
    Description: <profile.description>
    Structured constraints:
      - Areas: <areas>
      - Budget range: <min>-<max>
      - TRL: <range>
      - Hard exclusions: <list>

  Output JSON: {"score": 0-100, "rationale": "<2-3 sentences>"}
  Hard exclusions → score must be ≤ 10.

USER (per call):
  CALL:
    Title: <title>
    Programme: <programme>
    Deadline: <deadline>
    Budget: <budget_min>-<budget_max>
    Summary: <summary or first 2000 chars of full_text>
```

### Model

`claude-haiku-4-5` for v1. Promote to Sonnet only if quality is insufficient on the golden set.

### Triggers

- New call ingested → score against every active profile.
- Profile created or `description`/`structured` changed → backfill score against every call from the last 90 days.
- Admin "re-rank everything" button (used after prompt changes).

### Cost expectation

~50 new calls/week × 15 profiles = ~750 ranking calls/week steady-state. With prompt caching on profile blocks, well under $5/month at steady state.

**Burst cost.** A profile bootstrap or significant edit triggers a backfill against ~90 days of calls (estimate: ~600 calls × 1 profile = 600 ranking calls in a few minutes). At Haiku rates with cached profiles, this is still cents per backfill. The "re-rank everything" admin button is the largest burst: ~600 calls × 15 profiles = ~9,000 ranking calls — under $1, and rate-limited by the in-process queue so it can't overwhelm the API or the DB.

### Job queue & retry semantics

Ranking jobs are the unit `(call_id, profile_id)`. They are enqueued in an in-process queue (Python `asyncio.Queue` or a lightweight `rq`-style table in Postgres — a `ranking_jobs` table with `attempts` counter is the pragmatic choice at this scale and survives restarts).

Each job:

1. Reads the call and profile.
2. Calls Claude, parses JSON, upserts `call_rankings` on `(call_id, profile_id)` PK — idempotent.
3. On LLM error (5xx, timeout), retries with exponential backoff (e.g., 30s, 2m, 10m), up to 5 attempts.
4. On parse error (malformed JSON), retries once with a tightened prompt; if still bad, dead-letter the job.
5. After 5 failed attempts, mark the job failed in `ranking_jobs.status`, surface the count in the admin panel, do **not** block other jobs.

Per-pair retry (not per-batch) matters: if scoring a new call against 15 profiles fails on profile #7, profiles 8–15 still complete. The dead-letter job can be retried manually from the admin panel.

### Storage

One row per `(call_id, profile_id)` in `call_rankings`, including `model`, `prompt_version`, and `ranked_at` so stale rankings can be identified after model or prompt changes. A separate `ranking_jobs` table tracks in-flight and failed jobs.

## Profile management & bootstrap

### Bootstrap flow (the killer feature)

1. **Create profile** — user enters name, kind, and seed data: a list of group members (ORCID preferred; name + affiliation as fallback) and/or pasted past grants (titles + abstracts).
2. **Bootstrap fetch** — backend pulls evidence:
   - **PubMed via E-utilities** for the listed authors (last 5 years), no API key required.
     - **ORCID-first matching:** if an ORCID is provided, use the `AUID` field to fetch unambiguous publications.
     - **Name-based fallback:** when only name + affiliation is provided, query PubMed with `Author[au] AND Affiliation[ad]` to reduce false positives. Affiliation matching is fuzzy and noisy by nature; the user reviews the draft, so some imprecision is acceptable.
     - **Empty results:** if no publications are found for a member, log it and continue with the rest. The bootstrap proceeds even if all PubMed lookups fail — the LLM draft will then rest only on pasted grants.
   - Past grants are used as-pasted (no reliable structured source for Swedish/EU per-researcher grants).
3. **Draft generation** — Claude reads (publications + grants) → produces a 1–2 paragraph free-text description plus suggested structured fields (areas, TRL, budget range).
4. **Edit screen** — user reviews, refines free text, adjusts structured fields, saves.
5. **Re-rank trigger** — backfill scoring against last 90 days of calls.

### Edit later

Same edit screen as the final bootstrap step. No bootstrap re-run unless explicitly requested.

### Structured fields (kept small)

```
areas: text[]              # free-form tags
budget_min, budget_max     # int or null
trl_min, trl_max           # 1-9 or null
hard_exclusions: text[]    # e.g., "animal studies", "defence"
```

### Permissions (v1)

Any logged-in user can create or edit any profile. Internal tool, low sensitivity. Add ownership/roles if it becomes a real issue.

### Module surface

`profiles/` exposes:

- `create_profile(...)`, `update_profile(...)`
- `bootstrap_profile(seed_data) -> DraftProfile` — the only function that touches PubMed and the LLM.

## Web dashboard

### Stack

FastAPI + Jinja2 templates + HTMX. No SPA, no build step. Approximately 10 routes total.

### Auth

Email magic links. User enters email → one-time link sent → click sets a 30-day session cookie. No passwords, no SSO. Restricted to a hospital email domain allowlist (configured via env var).

**Token lifecycle:**

- On login request, generate 32 random bytes; URL-encode as the user-visible token; store only `sha256(token)` in `magic_link_tokens.token_hash` (never the raw token). Treat it like a password.
- `expires_at = now() + 15 minutes`. Links older than that are rejected.
- **Single-use:** on click, atomically `UPDATE ... SET consumed_at = now() WHERE consumed_at IS NULL AND expires_at > now() RETURNING user_id`. A row that returns zero rows means already-consumed or expired — reject.
- Session cookie is independent: a 30-day signed, HTTP-only, Secure, SameSite=Lax cookie holding the user id. Replay of the consumed magic link gains nothing.
- **User creation:** if the submitted email is in the allowlist and no user exists, create one on magic-link request (so first-click also creates them). `users.name` starts null; user can set it on `/me`.

**Rate limiting:**

- Per email: max 5 magic-link requests per 15 minutes (stored in a small in-memory LRU or a Postgres table `login_attempts`). Prevents inbox-spamming abuse from anyone who knows a colleague's address.
- Per IP: max 30 requests per 15 minutes across all emails.
- Exceeding either returns the same UI message as a successful request ("check your email") without sending — do not leak rate-limit state to the caller.

**Domain allowlist:**

`ALLOWED_EMAIL_DOMAINS` accepts a comma-separated list, matched against the substring after the final `@`. Subdomains and aliases must be listed explicitly — e.g., `regionstockholm.se,karolinska.se,ki.se`. The match is exact on the domain (no wildcard); if the hospital uses many subdomains, list them. This is intentional — implicit subdomain matching has bitten teams before (e.g., `intern.regionstockholm.se` accidentally being treated like the main domain).

**Known residual risk:** anyone with a hospital-domain email alias that forwards to an external address can gain access. Acceptable for this threat model (internal tool, low-sensitivity data), explicitly noted so future changes don't assume otherwise.

**First-admin bootstrap:**

On startup, a bootstrap routine reads `INITIAL_ADMIN_EMAIL` (comma-separated, optional). For each address: if the user exists, set `is_admin = true`; otherwise create the user with `is_admin = true`. Idempotent, safe to run every boot. Without this env var, the `/admin` panel is unreachable on a fresh deploy.

### Pages

- `/` — dashboard. Sidebar lists profiles. Main pane shows ranked call feed for the selected profile.
- `/calls/<id>` — call detail. Full metadata + per-profile scores ("Why this matched") + link to original source.
- `/profiles` — list of all profiles.
- `/profiles/new` — bootstrap wizard.
- `/profiles/<id>` — view-only summary (description, structured fields, recent ranked calls).
- `/profiles/<id>/edit` — separate edit page (form-based, not inline). One submit updates the profile and triggers re-ranking. Inline editing is rejected for v1 — it adds JS complexity and HTMX form-swap edge cases that aren't worth it for an infrequently-edited resource.
- `/profiles/<id>/calls` — call feed pinned to that profile.
- `/me` — subscription preferences (which profiles email you).
- `/admin` — source health, ranking queue depth, "re-rank everything" button. Gated by `users.is_admin`.

### Feed interactions

- Filters: deadline (next 30/60/90 days), programme, score threshold.
- Sort: score, deadline, newest.
- Per-user "Hide" button — dismisses a call from that user's feeds via `user_call_dismissals`. Doesn't affect others.

### Out of v1 (see Roadmap)

Comments, application tracking, file uploads, in-app notifications, mobile-optimized layout, public sharing.

## Email digest

### Cadence

Weekly, Monday 07:00 Europe/Stockholm.

### Per-recipient logic

For each user with at least one subscription:

1. Gather subscribed profiles where `digest_enabled = true` and the profile is `is_active = true`.
2. For each profile, select up to 5 calls matching *all* of:
   - `call_rankings.score >= threshold` (default 60)
   - `call_id NOT IN` the union of `digest_runs.included_call_ids` for this user (i.e., this user has never been sent this call in any prior digest)
   - `calls.deadline IS NULL OR calls.deadline > now()` (rolling/continuous calls with `NULL` deadline are included, not dropped)
3. Deduplicate across the user's profile sections: if the same call qualifies for multiple subscribed profiles, show it once, under the highest-scoring profile; note other matching profiles inline ("also matches: <list>").
4. Skip the email entirely if zero qualifying calls after dedup (no "nothing to report" noise).
5. Render one email grouped by profile, send via Resend (or SMTP relay if hospital prefers).

**"New since last digest" is defined strictly as set-difference against this user's `digest_runs.included_call_ids` history.** A call that was below threshold last week but crossed the threshold this week (e.g., after a prompt change or re-rank) *will* be included — which is the desired behavior. A call included in a previous digest but that the user didn't act on is *not* re-sent; if the user wants to see it again, they use the dashboard.

### Email content

- **Subject:** `<N> new funding calls for <profile names>`
- **Body:** per-profile section: title, programme, deadline (with "in N days" badge), score + one-line rationale, link to dashboard call detail.
- **Footer:** "Manage subscriptions" link to `/me`.

### Tracking

`digest_runs` stores `(user_id, sent_at, included_call_ids[])` so calls aren't re-sent to the same user, and so "why didn't I get X?" is debuggable.

### Failure handling

Per-recipient send wrapped — one bounce doesn't stop the batch. Bounces logged. Three consecutive bounces auto-disables that user's digests with admin notification.

### Templates

MJML → HTML at build time, plus a plain-text alternative. Stored in `digest/templates/`.

## Tech stack

- **Language:** Python 3.12
- **Web:** FastAPI + Jinja2 + HTMX
- **DB:** PostgreSQL 16 (managed)
- **ORM/migrations:** SQLAlchemy 2.x + Alembic
- **Scheduler:** APScheduler (in-process)
- **LLM:** `anthropic` SDK, Claude Haiku 4.5, prompt caching on profile blocks
- **Email:** Resend or SMTP relay
- **HTTP:** `httpx` with retries
- **Package management:** `uv`
- **Lint/format:** `ruff`
- **Testing:** `pytest`

## Deployment

- Single container, deployed to Fly.io or a small Hetzner VM. One process for web + scheduler.
- Managed Postgres separately; daily automated backups.
- Secrets and config via env vars: `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS` (comma-separated), `INITIAL_ADMIN_EMAIL` (comma-separated, optional after first deploy).
- CI: GitHub Actions runs lint + tests on every push; the golden-set ranking eval runs on a weekly cron.

## Testing

- **Unit tests** for each module: adapter normalization, ranking with fake LLM, digest assembly with seeded DB, profile bootstrap with fake PubMed + fake LLM.
- **Integration tests:** full ingest → rank → digest pipeline against ephemeral Postgres (testcontainers).
- **Golden ranking set:** ~20 hand-labeled `(call, profile, expected_band)` triples, run against real Claude weekly to catch regressions.
- **No browser tests** for v1 — server-rendered pages are testable via FastAPI's `TestClient`. Playwright not warranted at this scale.

## Observability

- Structured JSON logs to stdout — captured by host.
- `/healthz` endpoint.
- Admin panel: per-source last-fetch time + error count, ranking queue depth, last digest run summary.
- Error alerts via email to an admin address. Sentry optional.

## Repo layout

```
call-monitor/
├── pyproject.toml
├── alembic/
├── src/call_monitor/
│   ├── sources/        # one file per source
│   ├── ingestion/
│   ├── ranking/
│   ├── profiles/
│   ├── digest/
│   ├── web/            # routes + templates
│   ├── db/             # models, session
│   └── main.py         # FastAPI app + scheduler wiring
├── tests/
└── docs/superpowers/specs/
```

## Roadmap (post-v1)

The features below are deliberately deferred. They are listed in rough priority order; the actual order will follow user feedback after v1 ships.

### Near term

- **More sources.** Add adapters for ESF (`esf.se`), Tillväxtverket (regional fund), EUfonder.se aggregator, and key program-specific pages (e.g., `digital-strategy.ec.europa.eu` for AI/health calls). Each is one new file in `sources/`.
- **Slack/Teams delivery.** A new `delivery/` module alongside `digest/`; per-subscription channel target. Hospital likely already runs Teams.
- **Per-call user actions.** "Interested" / "Not interested" / "Discussing" — beyond the simple Hide. Surface aggregate "X people interested" on call detail.
- **Better filters.** Programme taxonomy, geographic eligibility, consortium requirements.

### Medium term

- **Application tracking.** Promote a call to a "tracked application": owner, status (intent / drafting / submitted / awarded / rejected), deadline reminders, file uploads. This is a meaningful new module but well-bounded.
- **Profile ownership and roles.** Profile owner, editors, viewers. Currently everyone can edit everything; this gets uncomfortable as the org grows.
- **Bootstrap improvements.** Auto-refresh profile from new publications quarterly. Detect when a profile is going stale.
- **Trend dashboard.** Org-level view: funding-call volume by programme over time, our hit rate per programme, gaps where we should be applying more.
- **Cross-profile clustering.** Show which calls match multiple profiles (collaboration opportunities).

### Longer term

- **Application-prep helpers.** Pre-fill standard sections (organization description, past projects) when starting a new application. Optionally an LLM-drafted first pass of the relevance section based on the matched profile.
- **Researcher directory integration.** If/when the hospital exposes one, replace pasted member lists with real lookups.
- **Public read-only mode.** Some hospitals share their funding watchlist with consortium partners.
- **Multi-tenancy.** If other hospitals or universities want to use the same instance under their own profiles. This is a significant refactor and should only be considered if there's real demand.
- **SSO.** Replace magic-link auth with hospital SAML/OIDC if access friction becomes a problem.

### Explicitly considered and rejected (for now)

- **A custom recommendation model trained on past hits.** Too little data, LLM ranking is good enough.
- **Browser extension for one-click "save this call" from any source.** Adapters are a better investment.
- **AI agent that drafts entire applications.** Out of scope; quality bar for funding applications is too high for unattended generation.

## Open questions

- **Hospital email allowlist** — confirm exact domain(s), including any subdomains, before launch.
- **SMTP vs Resend** — confirm hospital is OK with outbound email via Resend; if not, default to SMTP relay through hospital infra.
- **Past-grants data** — pasted text is the v1 answer; revisit if/when the hospital exposes a structured grants registry.
- **Initial `prompt_version`** — start at `"v1"`. Bump when the system or per-profile prompt template changes in a way that warrants re-ranking; the admin "re-rank everything" button performs the backfill.
