# call-manager

Internal hospital tool that aggregates funding calls (grants, EU calls, etc.) from multiple sources, ranks them with an LLM against organization- and group-level profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest.

See [docs/superpowers/specs/2026-04-14-call-manager-design.md](docs/superpowers/specs/2026-04-14-call-manager-design.md) for the v1 design.

## Conventions

- **Worktree directory:** `.worktrees/` (project-local, gitignored). Use this for any feature or plan work requiring isolation.
- **Plans:** saved to `docs/superpowers/plans/YYYY-MM-DD-<name>.md`.
- **Specs:** saved to `docs/superpowers/specs/YYYY-MM-DD-<name>.md`.

## Stack (v1)

- Python 3.12
- FastAPI + Jinja2 + HTMX (server-rendered, no SPA)
- PostgreSQL 16 + SQLAlchemy 2.x + Alembic
- APScheduler (in-process, single-instance hard constraint)
- `uv` for package management, `ruff` for lint/format, `pytest` for tests

## LLM

**Gemma 4 E4B (Q8) served locally via Ollama** on an on-prem NVIDIA RTX 2080 Ti. Accessed through an OpenAI-compatible HTTP client. Claude Haiku 4.5 is kept in the spec as a documented escalation fallback — not the current choice.

The ranking module's prompt-in/JSON-out interface is deliberately model-agnostic so swapping to a hosted API (or a larger local model) is a config change, not a rewrite.
