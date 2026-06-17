# call-manager

Internal hospital tool that aggregates funding calls (grants, EU calls, Vinnova programmes, etc.) from multiple sources, ranks them with a local LLM against organization- and group-level research profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest. It also provides a proposal-assist workspace for drafting applications against the ranked calls.

The v1 design spec (`docs/superpowers/specs/2026-04-14-call-monitor-design.md`) was pruned from the working tree in the 2026-06-17 docs cleanup; recover it from git history if needed (`git log --all -- '*2026-04-14-call-monitor-design.md'`).

## Conventions

- **Worktree directory:** `.worktrees/` (project-local, gitignored). Use this for any feature or plan work requiring isolation.
- **Plans:** saved to `docs/superpowers/plans/YYYY-MM-DD-<name>.md`.
- **Specs:** saved to `docs/superpowers/specs/YYYY-MM-DD-<name>.md`.

## Stack

- Python 3.12 — `uv` for packages, `ruff` for lint/format, `pytest` (+ testcontainers Postgres) for tests.
- FastAPI backend. PostgreSQL 16 + SQLAlchemy 2.x + Alembic, with `pgvector` for embeddings.
- APScheduler, in-process — **single-instance hard constraint** (the scheduler must not run on more than one process).
- **Frontend is mid-migration to a React SPA.** The calls dashboard is a React 18 + Vite SPA (react-router, TanStack Query, `openapi-fetch` against types generated from the FastAPI OpenAPI schema; `pnpm`). The proposal-assist workspace, admin, auth, and profile pages are still server-rendered **Jinja2 + HTMX**.
- SPA serving is gated by `SPA_MODE`: `dev` (Vite dev server + proxy), `prod` (FastAPI serves the built bundle from `src/call_manager/web/spa/` — **prod MUST ship a built SPA**), `disabled`.

## LLM

Two tiers. The ranking/extraction interfaces are deliberately prompt-in/JSON-out and model-agnostic, so swapping a model is a config change, not a rewrite.

**Local — on-prem NVIDIA RTX 2080 Ti via Ollama (OpenAI-compatible HTTP):**
- Ranking: **Gemma 4 E4B (Q4_K_M)** — `GEMMA_MODEL` (`gemma4:e4b-it-q4_K_M`).
- Embeddings: **BGE-M3** (1024-dim) — `EMBEDDING_MODEL`; powers the cosine topic-similarity gate / embedding pre-filter. Dev fallback `mxbai-embed-large` (also 1024-dim).
- Citation audit also defaults to local Ollama (`CITATION_LLM_PROVIDER`).

**Hosted — Anthropic; opt-in, requires `ANTHROPIC_API_KEY` (disabled when unset):**
- Remote-rank escalation: pairs whose local aggregate score clears `RANKING_ESCALATION_MIN_SCORE` are re-ranked by **`claude-sonnet-4-6`** (`REMOTE_RANK_MODEL`), capped per day.
- Proposal-assist drafting: **Claude Agent SDK** adapter with **`claude-opus-4-7`** (`PROPOSAL_ASSIST_LLM_MODEL`).
