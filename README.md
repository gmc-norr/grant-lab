# Grant Lab

Grant Lab is an internal hospital application for finding and developing
research-funding opportunities. It collects calls from sources such as Vinnova and the EU
Funding & Tenders Portal, ranks each call against organization and research-group profiles,
and presents the results in a web dashboard and weekly email digest.

The application also includes a collaborative proposal workspace. Research teams can draft
applications, manage references and citations, review proposal quality, and use an optional
hosted LLM assistant while retaining a versioned audit trail.

## Main capabilities

- Scheduled ingestion from pluggable funding-call sources.
- Profile-specific relevance ranking with deterministic eligibility gates, local embeddings,
  and a structured LLM rubric.
- Searchable calls dashboard with ranking explanations and per-user subscriptions.
- Collaborative proposal editing with branches, version history, references, citations,
  review passes, and Markdown export.
- Administration for users, source health, queues, retention, and ranking evaluation.
- Weekly email digests and optional Google OIDC login.

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy, Alembic, and APScheduler.
- **Database:** PostgreSQL 16 with `pgvector`.
- **Frontend:** React, TypeScript, Vite, Mantine, Tiptap, and Yjs.
- **Local models:** Ollama for Gemma ranking and BGE-M3 embeddings.
- **Hosted model:** Anthropic, optionally enabled for proposal drafting and rank escalation.

The application is single-instance: the scheduler runs inside the FastAPI process, and the
live-editing rooms, drafting subprocesses and login rate limiters are per-process state.
A PostgreSQL advisory lock enforces this at startup — a second process (a second Uvicorn
worker, a second replica) finds the lock held and **refuses to start**. Set
`SCHEDULER_STANDBY_OK=true` only for a deliberate HTTP-only standby that runs no
scheduler; `GET /healthz` reports `scheduler_lock` so a monitor can tell which process
holds it.

## Local development

### Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Docker Compose
- Node.js 20 or newer
- Corepack and pnpm 10.16.0

Ollama is not required to open the application, but ranking, embeddings, and profile
bootstrap need the configured local models. Proposal-assist drafting additionally requires
an Anthropic API key.

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

Create `.env` in the repository root:

```dotenv
DATABASE_URL=postgresql+psycopg://grant_lab:grant_lab@localhost:5432/grant_lab
SESSION_SECRET=local-development-secret-change-me
ALLOWED_EMAIL_DOMAINS=example.org
INITIAL_ADMIN_EMAIL=admin@example.org
INITIAL_ADMIN_PASSWORD=change-this-password
APP_BASE_URL=http://localhost:5173
SPA_MODE=dev

# Optional local model configuration
OLLAMA_URL=http://localhost:11434
GEMMA_MODEL=gemma4:e4b-it-q4_K_M
EMBEDDING_MODEL=bge-m3

# Leave drafting off until ANTHROPIC_API_KEY is configured.
DRAFTING_ENABLED=false
# ANTHROPIC_API_KEY=
```

Replace the example domain and admin address together. The admin email must belong to an
`ALLOWED_EMAIL_DOMAINS` entry, `SESSION_SECRET` must contain at least 16 characters, and
`INITIAL_ADMIN_PASSWORD` must contain at least 12 characters.

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
credentials from `.env`. Vite is the browser-facing development server and proxies API,
authentication, SSE, and WebSocket traffic to FastAPI on port 8000.

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

To enable the collaborative drafting assistant, set these values and restart FastAPI:

```dotenv
DRAFTING_ENABLED=true
ANTHROPIC_API_KEY=your-key
```

Do not commit `.env`, API keys, proposal data, or anything under `var/`.

## Tests and checks

Backend:

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check .
```

Use fewer pytest workers on smaller machines; each worker starts its own testcontainers
PostgreSQL instance.

Frontend:

```bash
cd frontend
pnpm run test
pnpm run build
```

## Production launch

Build the SPA before starting FastAPI in production mode:

```bash
docker build -f frontend/Dockerfile -t grant-lab-spa-build .
container=$(docker create grant-lab-spa-build)
rm -rf src/grant_lab/web/spa
mkdir -p src/grant_lab/web/spa
docker cp "$container:/build/dist/." src/grant_lab/web/spa/
docker rm "$container"
test -f src/grant_lab/web/spa/index.html
```

Set `SPA_MODE=prod`, use an `https://` `APP_BASE_URL`, configure a strong production
`SESSION_SECRET`, apply migrations, and launch exactly one Uvicorn process behind a
TLS-terminating reverse proxy:

```bash
uv run alembic upgrade head
uv run uvicorn grant_lab.main:create_app --factory --host 127.0.0.1 --port 8000 \
  --timeout-graceful-shutdown 30
```

`--timeout-graceful-shutdown` bounds how long Uvicorn waits for in-flight requests (an
SSE stream can otherwise hold shutdown open for as long as the turn runs). The lifespan
teardown then terminates drafting subprocesses first and waits for scheduler ticks last,
so give the service manager a stop timeout above that: `TimeoutStopSec=120` in a systemd
unit. A SIGKILL before the teardown finishes leaks the drafting subprocesses.

Keep the database and `WORKSPACES_DIR` on persistent storage. Configure the reverse proxy
to forward WebSockets, disable response buffering for SSE, cap request bodies, and set the
trusted-proxy settings to match the real proxy topology.

### Backup and retention

Three things hold state, and all three must be in the backup:

- **PostgreSQL** — every table, including the collaborative-editing update log
  (`yjs_updates`), which is the live document between checkpoints. `pg_dump` of the
  database is sufficient.
- **`WORKSPACES_DIR`** — one directory per workspace: `repo/` (the proposal markdown and
  its full git history, the only copy), `references/` (uploaded reference files and their
  extracted text) and attachments. Back it up as files; the git repositories inside it are
  plain repositories.
- **`.env`** — the configuration, including the session-signing secret. Rotating
  `SESSION_SECRET` signs every user out.

Nothing is pruned automatically. A deleted workspace is a soft delete: the database rows
and every byte under `WORKSPACES_DIR` stay, an admin can inspect the deleted workspace
when `RETENTION_INSPECTION_ENABLED` is true, and removing it for real is a deliberate
admin action, not a timer. Sizing guidance: the database grows with calls, rankings and
chat turns; `WORKSPACES_DIR` grows with uploaded references (capped at 50 MiB each) and
proposal history.

## Repository layout

```text
alembic/                 Database migrations
docs/                    Architecture, specifications, and investigations
frontend/                React SPA, unit tests, and Playwright tests
scripts/                 Code generation and operational scripts
src/grant_lab/        FastAPI application and domain modules
tests/                   Backend and architecture tests
var/                     Local workspace data; ignored and confidential
```

The generated structural reference is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).