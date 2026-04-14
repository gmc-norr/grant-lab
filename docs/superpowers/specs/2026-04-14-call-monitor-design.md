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

Six core tables.

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
  created_by, created_at, updated_at

call_rankings
  call_id, profile_id, score (int 0-100), rationale (text),
  model (text), ranked_at (timestamp)
  PRIMARY KEY(call_id, profile_id)

users
  id, email, name, is_admin (bool), created_at, last_login_at

subscriptions
  user_id, profile_id, digest_enabled (bool)
  PRIMARY KEY(user_id, profile_id)
```

Plus two small auxiliary tables:

```
user_call_dismissals
  user_id, call_id, dismissed_at
  PRIMARY KEY(user_id, call_id)

digest_runs
  id, user_id, sent_at, included_call_ids (int[])
```

**Notes:**

- `calls.raw` retains the original payload so we can re-extract or re-rank without re-fetching.
- `call_rankings` is recomputed on profile change (delete + re-rank) or new call (insert per profile).
- All profile kinds share one schema — distinguished only by `kind`.
- `subscriptions` controls digest delivery, independent of viewability — for v1, all logged-in users can view all profiles.

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
4. Updates `source.last_fetched_at` only on success

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

~50 new calls/week × 15 profiles = ~750 ranking calls/week. With prompt caching on profile blocks, well under $5/month. Backfill on profile changes is similarly cheap.

### Storage

One row per `(call_id, profile_id)` in `call_rankings`, including `model` and `ranked_at` so stale rankings can be identified after model or prompt changes.

## Profile management & bootstrap

### Bootstrap flow (the killer feature)

1. **Create profile** — user enters name, kind, and seed data: a list of group members (names + ORCID/affiliation) and/or pasted past grants (titles + abstracts).
2. **Bootstrap fetch** — backend pulls evidence:
   - PubMed via E-utilities for the listed authors (last 5 years), no API key required.
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

### Pages

- `/` — dashboard. Sidebar lists profiles. Main pane shows ranked call feed for the selected profile.
- `/calls/<id>` — call detail. Full metadata + per-profile scores ("Why this matched") + link to original source.
- `/profiles` — list of all profiles.
- `/profiles/new` — bootstrap wizard.
- `/profiles/<id>` — view + edit (free text, structured fields, member list).
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

1. Gather subscribed profiles where `digest_enabled = true`.
2. For each profile: top 5 calls with `score >= threshold` (default 60), where the call is new since the user's last digest and the deadline is still in the future.
3. Skip the email entirely if zero qualifying calls (no "nothing to report" noise).
4. Render one email grouped by profile, send via Resend (or SMTP relay if hospital prefers).

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
- Secrets via env vars: `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAIN`.
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

### Near term (months 1–3 after v1)

- **More sources.** Add adapters for ESF (`esf.se`), Tillväxtverket (regional fund), EUfonder.se aggregator, and key program-specific pages (e.g., `digital-strategy.ec.europa.eu` for AI/health calls). Each is one new file in `sources/`.
- **Slack/Teams delivery.** A new `delivery/` module alongside `digest/`; per-subscription channel target. Hospital likely already runs Teams.
- **Per-call user actions.** "Interested" / "Not interested" / "Discussing" — beyond the simple Hide. Surface aggregate "X people interested" on call detail.
- **Better filters.** Programme taxonomy, geographic eligibility, consortium requirements.

### Medium term (months 3–9)

- **Application tracking.** Promote a call to a "tracked application": owner, status (intent / drafting / submitted / awarded / rejected), deadline reminders, file uploads. This is a meaningful new module but well-bounded.
- **Profile ownership and roles.** Profile owner, editors, viewers. Currently everyone can edit everything; this gets uncomfortable as the org grows.
- **Bootstrap improvements.** Auto-refresh profile from new publications quarterly. Detect when a profile is going stale.
- **Trend dashboard.** Org-level view: funding-call volume by programme over time, our hit rate per programme, gaps where we should be applying more.
- **Cross-profile clustering.** Show which calls match multiple profiles (collaboration opportunities).

### Longer term (9+ months)

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

- **Hospital email allowlist** — confirm exact domain(s) before launch.
- **SMTP vs Resend** — confirm hospital is OK with outbound email via Resend; if not, default to SMTP relay through hospital infra.
- **Past-grants data** — pasted text is the v1 answer; revisit if/when the hospital exposes a structured grants registry.
