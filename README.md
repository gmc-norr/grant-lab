# call-monitor

Internal hospital tool that aggregates funding calls (grants, EU calls, Vinnova programmes, etc.) from multiple sources, ranks them with a local LLM against organization- and group-level research profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest.

## What it does

- **Ingests** funding calls from external sources on a schedule (Vinnova two-stage API, EU Funding & Tenders Portal). Adapters are pluggable.
- **Ranks** each `(call × profile)` pair through a single criterion-rubric pipeline: rule-based gates (timeline / budget / TRL / embedding cosine) → LLM rubric call (topic-fit, eligibility, exclusions) → deterministic aggregation into a 0–100 `aggregate_score`.
- **Surfaces** results via a server-rendered FastAPI + HTMX dashboard with per-profile ranking, per-criterion explanation, and admin observability (queue depth, embedding coverage, last-run Kendall's τ).
- **Emails** a weekly digest per profile (top + borderline bands).

The ranking module is deliberately model-agnostic — swapping Gemma 4 for a hosted API or a larger local model is a config change.

## How it works

### Profiles — what you're matching against

A **profile** describes either a research **organization** (e.g. the hospital) or a **group** (e.g. a single PI's lab). It carries:

- A free-text **description** (a few sentences of research focus, used as system-prompt context AND as the input text to the embedding model)
- A **structured rubric** stored as JSONB: `areas` (named research areas), `must_include` (force-include keywords for borderline calls), `hard_exclusions` (deal-breakers — drug discovery, animal models, etc. — that the LLM is told to score ≤ 10), plus optional `budget_min` / `budget_max` / `trl_min` / `trl_max`
- A `budget_currency` (3-letter ISO code) used for budget-gate currency matching
- An `embedding` (BGE-M3, 1024-dim) refreshed by the embeddings backfill tick

### Ingestion → enqueue → drain → rank

1. **Source adapters** (Vinnova, EU Funding & Tenders Portal) run on cron jobs at 03:00 / 03:15 Europe/Stockholm. Each adapter normalizes the source's API into a common `Call` row with `title`, `summary`, `full_text`, `deadline`, `budget_min` / `max` / `currency`, `keywords`, plus a `structured` JSONB blob for source-specific extras (TRL ranges, programme codes, etc.). Vinnova uses a two-stage API: list endpoint → detail fetch per call.
2. **Embedding** happens inside ingestion: each new or updated `Call` is embedded immediately so the embedding pre-gate has data on the next ranking tick. A daily 03:30 backfill tick (`embeddings:refresh-missing`) catches anything that failed during ingest or was added without embedding (e.g. via tests/manual SQL).
3. New calls trigger a **per-active-profile fan-out**: for every active `Profile`, a `RankingJob` row is enqueued.
4. The **`rank:drain` worker tick** runs every `RANKING_TICK_SECONDS` (default 60s). It claims a small batch of `PENDING` jobs (`SELECT … FOR UPDATE SKIP LOCKED`), dispatches each through `rank_pair`, writes the result to `call_rankings` (PK: `call_id, profile_id`), and marks the job `DONE` or `FAILED`.

### Ranking pipeline — one path, four gates + an LLM rubric

`rank_pair` ([src/call_monitor/ranking/rank_pair.py](src/call_monitor/ranking/rank_pair.py)) decomposes the relevance question into **deterministic gates** + a **structured LLM rubric** + a **deterministic aggregation**:

**1. Rule-based gates** ([src/call_monitor/ranking/criteria.py](src/call_monitor/ranking/criteria.py)) — evaluated in pure Python before the LLM is touched:

| Gate | PASS condition | UNKNOWN condition | FAIL condition |
|---|---|---|---|
| **timeline** | `deadline >= now + 14 days` | `deadline is None` | expired or `< 14 days` away |
| **budget** | profile and call budget ranges overlap AND currencies match | any field missing, or currencies present but mismatch | ranges don't overlap |
| **TRL** | TRL ranges overlap | any TRL field missing | ranges don't overlap |
| **embedding** | cosine(profile, call) ≥ `EMBEDDING_PREGATE_THRESHOLD` (default 0.30) | either embedding missing OR embedded under different models | cosine below threshold |

A FAIL on any gate **short-circuits the LLM call** — the result is `aggregate_score = 0` with the gate name as `aggregate_reason`. UNKNOWN is *not* FAIL: source adapters frequently don't populate budget or TRL, and treating null as fail would reject valid calls.

The **`must_include` profile keyword override** is a partial escape hatch: if the call's title / summary / keywords match a must-include term, both the timeline and embedding FAILs are bypassed (those are topic gates and must-include is the user's "I want to see this regardless" signal). Budget and TRL FAILs still hard-zero the score even with override.

**2. LLM rubric call** ([src/call_monitor/ranking/prompt.py](src/call_monitor/ranking/prompt.py)) — only fires if all gates pass (or were UNKNOWN, or were bypassed by must-include). The system prompt asks the model to act as a research-funding analyst evaluating a call for the given profile. It returns a JSON object with three sub-rubrics:

```json
{
  "topic_fit":   {"rating": 0..5,         "rationale": "...", "quoted_evidence": "..."},
  "eligibility": {"rating": "pass|unclear|fail", "rationale": "...", "quoted_evidence": "..."},
  "exclusions":  {"rating": "pass|fail",  "rationale": "...", "quoted_evidence": "..."}
}
```

The call body is title + summary + 4 KB-truncated full-text. The model is instructed to set `quoted_evidence` to a verbatim span from the call (truncated to 200 chars) for any non-default judgment. The Ollama call uses `response_format={"type": "json_object"}` to enforce object shape.

**3. Quoted-evidence verification** — for each non-null `quoted_evidence`, the parser checks it appears as a (whitespace-tolerant) case-insensitive substring of `title + summary + full_text` (FULL full-text — no truncation, or quotes from past 4 KB would be falsely nulled). Verification failures null out the field; they never demote the score.

**4. Score aggregation** ([src/call_monitor/ranking/aggregate.py](src/call_monitor/ranking/aggregate.py)) — combines gates + LLM into a final 0–100 score:

| Condition | Score |
|---|---|
| Any gate FAIL (timeline / budget / TRL / embedding) | 0 |
| `eligibility == "fail"` | 0 |
| `exclusions == "fail"` | `min(10, topic_argmax × 2)` |
| `eligibility == "unclear"` | `min(50, topic_argmax × 20)` (capped at borderline) |
| Clean path | `topic_argmax × 20` |

The score is written to `call_rankings.aggregate_score` along with: rationale (a one-line per-criterion summary plus the per-criterion rationales as separate `call_ranking_criteria` rows), prompt version, model name, `embed_similarity` for downstream analysis, and the raw JSON envelope for audit.

### Read paths — what the user sees

- **Calls index** (`/calls?profile=<id>`): Postgres query `JOIN call_rankings ON call_id … WHERE profile_id = ?`, ordered by `aggregate_score DESC`.
- **Call detail** (`/calls/<id>?profile=<id>`): the full ranking, including the per-criterion table loaded from `call_ranking_criteria` — topic rating with rationale + verified quoted evidence, eligibility verdict, exclusions verdict, plus any gate FAIL reason.
- **Weekly digest**: per-profile email, sent Monday 07:00 Europe/Stockholm. Buckets: **must-include** (matched a must-include keyword, any score), **top** (`aggregate_score ≥ 60`), **borderline** (`45 ≤ aggregate_score < 60`). Recently-sent and user-dismissed calls are excluded (prior digests bounded to the last 26 weeks). Rendered HTML + plain-text via Jinja templates; sent through the configured SMTP/Resend client (or the `NoOpEmailClient` in dev).

### Operational tooling

- **`/admin`** shows source health (per-source last-fetched + call count), ranking-queue depth by status, **embedding coverage** (calls and profiles embedded vs stale, against the currently-configured embedding model), digest panel (last sent, runs this week, disabled users from bounce-handling), and the most recent golden-set Kendall's τ.
- **`/admin/rerank`** enqueues jobs for every active profile × every Call from the last 90 days. Idempotent (partial unique index dedups in-flight jobs).
- **Weekly golden-set evaluator** at Mon 06:00: reads `tests/fixtures/golden_set.json` (curated `(call_id, profile_id, expected_score)` triples), queries `call_rankings`, computes Kendall's τ-b inline (pure stdlib, ties-aware), persists the result to `data/golden_set_last.json` for `/admin` to read.
- **Embeddings backfill CLI** ([src/call_monitor/embeddings/cli.py](src/call_monitor/embeddings/cli.py)) — `python -m call_monitor.embeddings.cli backfill` walks every call + profile with NULL or model-mismatched embedding, calls Ollama in batches, exits 0 on full success / 1 if any row failed. Run this as part of every deploy that lands a migration touching the embedding columns (or that switches `EMBEDDING_MODEL`).

## Stack

- **Python 3.12** — `uv` for package management, `ruff` for lint/format
- **Web** — FastAPI + Jinja2 + HTMX (server-rendered, no SPA)
- **Database** — PostgreSQL 16 + `pgvector` extension + SQLAlchemy 2.x + Alembic (8 migrations)
- **Scheduler** — APScheduler in-process (single-instance hard constraint)
- **Auth** — itsdangerous-signed session cookies, domain-allowlisted email
- **LLM** — Gemma 4 E4B (Q4_K_M) served locally via Ollama on an on-prem RTX 2080 Ti, accessed through an OpenAI-compatible HTTP client
- **Embeddings** — BGE-M3 (1024-dim) via Ollama, written to `pgvector` columns
- **Tests** — `pytest` + `testcontainers[postgres]` (uses the `pgvector/pgvector:pg16` image), `httpx.MockTransport` for LLM stubbing

## Layout

```
src/call_monitor/
├── auth/             # session cookie + login
├── config.py         # settings & env wiring
├── db/               # SQLAlchemy models + session factory
├── digest/           # weekly digest assembly + email rendering
├── email/            # SMTP / Resend / no-op clients
├── embeddings/       # BGE-M3 client + similarity + scheduled refresh + backfill CLI
├── ingestion/        # source-agnostic orchestrator + alerts
├── sources/          # eu_portal, vinnova adapters
├── profiles/         # profile CRUD + structured rubric
├── ranking/          # gates, prompt, aggregate, rank_pair, queue, worker, eval
├── scheduler.py      # APScheduler wiring
├── web/              # FastAPI routes + Jinja templates
└── main.py           # app factory
alembic/versions/     # 8 schema migrations (incl. 0007 pgvector + 0008 v1/setwise drop)
docs/superpowers/
├── specs/            # design specs
└── plans/            # implementation plans (foundation through ranking-v2 E)
```

## Running

```bash
uv sync
uv run alembic upgrade head
uv run python -m call_monitor.embeddings.cli backfill   # see Deploy procedure
uv run uvicorn call_monitor.main:create_app --factory --reload
```

Required env: `DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS`, `INITIAL_ADMIN_EMAIL`, `OLLAMA_URL`, `GEMMA_MODEL` (default `gemma4:e4b-it-q4_K_M`), `EMBEDDING_MODEL` (default `bge-m3`).

## Deploy procedure

The supported deploy sequence is:

```bash
alembic upgrade head
python -m call_monitor.embeddings.cli backfill
# (only after the CLI exits 0:)
systemctl restart call-monitor   # or however your unit boots the app
```

**Why this order:**

1. **`alembic upgrade head` runs first** so the schema matches the new code. Migrations are intentionally schema-only — they never call out to Ollama. Embedding backfills used to be tempted into Alembic data migrations, but a migration that depends on an external HTTP service makes deploys fragile (Ollama down → schema deploy stuck mid-migration). The CLI is the explicit, separate, retryable step.
2. **`embeddings.cli backfill` runs second** to populate `calls.embedding` and `profiles.embedding` for any rows that are NULL or were embedded under an older `EMBEDDING_MODEL`. Without this, the embedding pre-gate degrades to UNKNOWN — ranking still works, but the cheap topic-similarity short-circuit doesn't fire and every (call × profile) pair pays for an LLM round-trip. The CLI loops until a batch makes no progress (everything left is failing), exits 0 on full success / 1 otherwise.
3. **Restart the app last**, after the CLI has reported success. The new code is now backed by an up-to-date schema and a fully-populated embedding column.

The `/admin` page surfaces an embedding-coverage badge — a quick post-deploy sanity check that the backfill succeeded.

## Tests

```bash
uv run pytest -q
```

430 tests across 71 files; full suite runs in ~20s against testcontainers Postgres (pgvector image).

## Project size (post Plan E simplification)

| Area | LOC |
|---|---|
| Production Python (`src/call_monitor`) | 6,000 |
| Tests | 9,901 |
| Templates (Jinja2 / HTML) | 400 |
| Alembic migrations (8 revisions) | 892 |
| **Test files** | **71** |
| **Tests passing** | **430** |
| **Commits on this branch** | **195** |
