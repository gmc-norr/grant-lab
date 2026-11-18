# Grant Lab

Grant Lab is an experimental tool for finding grants and other funding opportunities,
for research and beyond. It collects calls from sources such as Vinnova and the EU Funding &
Tenders Portal, ranks each call against applicant profiles (an organisation, a group or one
person), and presents the results in a web dashboard and weekly email digest.

The proposal-writing workspace that used to ship alongside the dashboard is not part of
this branch. It is preserved, unmaintained, on the `workspace` branch and the
`workspace-v1` tag (`docs/superpowers/specs/2026-11-03-workspace-removal-design.md`). Its
database tables and its files on disk are kept.

## Main capabilities

- Scheduled ingestion from pluggable funding-call sources.
- Profile-specific relevance ranking with deterministic eligibility gates, local embeddings,
  and a structured LLM rubric.
- Searchable calls dashboard with ranking explanations and per-user subscriptions.
- Administration for users, source health, queues, and ranking evaluation.
- Weekly email digests and optional Google OIDC login.

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy, Alembic, and APScheduler.
- **Database:** PostgreSQL 16 with `pgvector`.
- **Frontend:** React, TypeScript, Vite, Mantine, and TanStack Query.
- **Local models:** Ollama for Gemma ranking and BGE-M3 embeddings.
- **Hosted model:** Anthropic, optionally enabled for rank escalation.

The application is single-instance: the scheduler runs inside the FastAPI process, and the
login rate limiters are per-process state.
A PostgreSQL advisory lock enforces this at startup — a second process (a second Uvicorn
worker, a second replica) finds the lock held and **refuses to start**. Set
`SCHEDULER_STANDBY_OK=true` only for a deliberate HTTP-only standby that runs no
scheduler; `GET /healthz` reports `scheduler_lock` so a monitor can tell which process
holds it. A process that loses the lock while running stops its scheduler and answers
`/healthz` with **503** (a standby answers 200) — restart it.

## Local development

### Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Docker Compose
- Node.js 22
- Corepack and pnpm 10.16.0

Ollama is not required to open the application, but ranking, embeddings, and profile
bootstrap need the configured local models.

### 1. Install dependencies

From the repository root:

```bash
uv sync
cd frontend
corepack enable
pnpm install --frozen-lockfile
cd ..
```

### 2. Start PostgreSQL

```bash
docker compose up -d db
```

This starts PostgreSQL with `pgvector` on `127.0.0.1:5432`. If that port is already
occupied, choose another host port:

```bash
HOST_DB_PORT=5433 docker compose up -d db
```

Use the same port in `DATABASE_URL` below.

### 3. Configure the application

Create `.env` in the repository root. `SESSION_SECRET` signs every login
cookie, so generate your own and paste it after the `=` (the app will not start
with it empty):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```dotenv
DATABASE_URL=postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager
SESSION_SECRET=
ALLOWED_EMAIL_DOMAINS=example.org
INITIAL_ADMIN_EMAIL=admin@example.org
INITIAL_ADMIN_PASSWORD=change-this-password
APP_BASE_URL=http://localhost:5173
SPA_MODE=dev

# Optional local model configuration
OLLAMA_URL=http://localhost:11434
GEMMA_MODEL=gemma4:e4b-it-q4_K_M
EMBEDDING_MODEL=bge-m3

# Optional: hosted re-ranking of the strongest matches.
# ANTHROPIC_API_KEY=
```

Replace the example domain and admin address together. The admin email must belong to an
`ALLOWED_EMAIL_DOMAINS` entry, `SESSION_SECRET` must contain at least 16 characters, and
`INITIAL_ADMIN_PASSWORD` must contain at least 12 characters.

The role and database in `DATABASE_URL` are `call_manager`, not `grant_lab`. That is what
`docker-compose.yml` creates: the Python package was renamed on 2026-09-05 and the
database deliberately was not, because renaming it means an edit to the live `.env` and a
Postgres rename. Until 2026-10-28 this file said `grant_lab:grant_lab@…/grant_lab`, so
step 4 below failed for anyone following it (review 2026-10-26, M13).

The initial admin is created on first startup. The bootstrap is idempotent and does not
overwrite an existing user's password during later restarts.

### 4. Apply database migrations

```bash
uv run alembic upgrade head
```

### 5. Start the backend

In the repository root:

```bash
uv run uvicorn grant_lab.main:create_app --factory --reload --port 8000
```

### 6. Start the frontend

In a second terminal:

```bash
cd frontend
pnpm run dev
```

Open [http://localhost:5173](http://localhost:5173) and sign in with the initial admin
credentials from `.env`. Vite is the browser-facing development server and proxies API and
authentication traffic to FastAPI on port 8000.

`APP_BASE_URL` must remain `http://localhost:5173` in this setup. Using the backend's port
there causes Origin/CSRF checks to reject requests sent through Vite.

## Optional model setup

For local ranking and embeddings, install Ollama on the machine that provides the GPU and
make the configured models available there. With Ollama running locally:

```bash
ollama pull gemma4:e4b-it-q4_K_M
ollama pull bge-m3
```

Set `OLLAMA_URL` to that server's URL when it runs on another host. After the models are
available, missing embeddings can be generated with:

```bash
uv run python -m grant_lab.embeddings.cli backfill
```

### Ollama service settings

These belong to the Ollama **service** on the GPU host, not to this project's `.env`.
Ranking depends on them, and nothing in the application can set them:

```dotenv
OLLAMA_FLASH_ATTENTION=false
OLLAMA_NUM_PARALLEL=1
OLLAMA_CONTEXT_LENGTH=8192
```

Review 2026-10-26 (H2) found Gemma 4's ratings non-deterministic on Ollama 0.35.1 without
the first two, which makes two runs of the same pair disagree, and the default 4096-token
context too small for the longest call texts (the EU full texts that fail to rank are
10.5-36 KB).

On a systemd install these go in `/etc/systemd/system/ollama.service.d/override.conf` as
`Environment=` lines, followed by `sudo systemctl daemon-reload && sudo systemctl restart
ollama`.

**Re-rank after any Ollama upgrade.** A version change moves roughly half the ratings, so
scores produced before it are not comparable with scores produced after it. Treat an
Ollama upgrade like a model swap: re-rank, do not mix.

**Re-ranking is a THIRD step, and it has to be the admin button.** Use
**"Re-rank everything (open calls × active profiles)"** on `/admin`. The nightly
reconcile will not pick these calls up on its own: a call that failed to rank because
the context was too small is recorded as a PARSE failure, and `enqueue_stale_pairs`
suppresses a parse failure on UNCHANGED INPUTS for 7 days
(`DEAD_LETTER_SUPPRESS_DAYS`). None of the three variables above is part of any
fingerprint - they belong to the Ollama service, not to this app - so raising
`OLLAMA_CONTEXT_LENGTH` changes no input, and the affected calls stay suppressed for
up to a week. The admin button is the documented override: it runs
`ranking.backfill.backfill_profile`, which applies neither the freshness check nor the
dead-letter suppression.

Do not commit `.env`, API keys, proposal data, or anything under `var/`.

## Tests and checks

Backend:

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check .
uv run mypy                    # type check src/grant_lab (settings in pyproject.toml)
```

Use fewer pytest workers on smaller machines; each worker starts its own testcontainers
PostgreSQL instance.

Coverage, as CI measures it (lines and branches per package):

```bash
uv run pytest -n 16 --dist loadfile --cov --cov-report=json:coverage.json
uv run python scripts/check_coverage_floors.py --section backend --report coverage.json
cd frontend && pnpm run test:coverage && cd ..
uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json
```

CI fails when a package drops below its floor in `coverage-floors.json`. When coverage
goes up, raise the floors on purpose: run the checker with `--update` and one `--report`
per run (the lowest value wins). Include CI's own report, the `coverage-backend` or
`coverage-frontend` artifact of the pull request's run, because CI can measure a little
lower than your machine. Then commit the changed file. For example, with a local run and
CI's report saved as `ci-coverage.json`:

```bash
uv run python scripts/check_coverage_floors.py --section backend --update --report coverage.json --report ci-coverage.json
```

Frontend:

```bash
cd frontend
pnpm run lint
pnpm run test
pnpm run build
pnpm run e2e:match           # production bundle in Chromium, API mocked
cd ..
bash scripts/e2e_run.sh      # real backend: testcontainers Postgres + uvicorn + Vite
```

## Production launch

### Build and package

`vite build` writes to `frontend/dist`, and **nothing copies it into the directory
FastAPI serves**. Run the packaging script — it is the only supported writer of
`src/grant_lab/web/spa/`:

```bash
pnpm -C frontend install --frozen-lockfile
pnpm -C frontend run build
pnpm -C frontend run package
```

Do **not** hand-copy a bundle into `src/grant_lab/web/spa/`;
`tests/architecture/test_package_spa.py::test_no_other_writer_of_the_served_tree`
fails if a second writer appears.

Building in Docker:

```bash
docker build -f frontend/Dockerfile -t grant-lab-spa .
```

Extract the result to `frontend/dist` and run `./scripts/package_spa.sh` — do not
`docker cp` straight into the served directory.

### Configure

Set `SPA_MODE=prod`, use an `https://` `APP_BASE_URL`, configure a strong production
`SESSION_SECRET`, apply migrations, and launch exactly one Uvicorn process behind a
TLS-terminating reverse proxy:

```bash
uv run alembic upgrade head
uv run uvicorn grant_lab.main:create_app --factory --host 127.0.0.1 --port 8000 \
  --timeout-graceful-shutdown 30
```

No `ANTHROPIC_API_KEY` is required; leaving it unset keeps the hosted tier off and
ranking runs entirely on the local Ollama. The old edition and drafting-switch settings
and the other workspace settings are no longer read; lines for them left in an existing
`.env` are ignored.

`--timeout-graceful-shutdown` bounds how long Uvicorn waits for in-flight requests. The
lifespan teardown then waits for scheduler ticks, so give the service manager a stop
timeout above that: `TimeoutStopSec=120` in a systemd unit.

Keep the database on persistent storage. Configure the reverse proxy to cap request
bodies and set the trusted-proxy settings to match the real proxy topology.

### Backup and retention

Three things hold state, and all three must be in the backup:

- **PostgreSQL** — every table, including the tables of the removed workspace, which keep
  their data. `pg_dump` of the database is sufficient.
- **The archived workspace files** — `var/workspaces` by default, or wherever
  `WORKSPACES_DIR` pointed. The app no longer reads or writes them, but they are the only
  copy of the proposal git histories and uploaded references, and the `workspace` branch
  needs them to be restored.
- **`.env`** — the configuration, including the session-signing secret. Rotating
  `SESSION_SECRET` signs every user out.

Nothing is pruned automatically. The database grows with calls and rankings.

## Repository layout

```text
alembic/                 Database migrations
docs/                    Architecture, specifications, and investigations
frontend/                React SPA, unit tests, and Playwright tests
scripts/                 Code generation and operational scripts
src/grant_lab/        FastAPI application and domain modules
tests/                   Backend and architecture tests
var/                     Local data, including the archived workspace files; ignored and confidential
```

The generated structural reference is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).