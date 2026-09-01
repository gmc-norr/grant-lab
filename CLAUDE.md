# call-manager

Internal hospital tool that aggregates funding calls (grants, EU calls, Vinnova programmes, etc.) from multiple sources, ranks them with a local LLM against organization- and group-level research profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest. It also provides a proposal-assist workspace for drafting applications against the ranked calls.

The v1 design spec (`docs/superpowers/specs/2026-04-14-call-monitor-design.md`) was pruned from the working tree in the 2026-06-17 docs cleanup; recover it from git history if needed (`git log --all -- '*2026-04-14-call-monitor-design.md'`).

## Conventions

- **Architecture map:** `docs/ARCHITECTURE.md` is **generated** from the source by
  `scripts/gen_architecture.py` (via `ast` — the app is never imported) and pinned by
  `tests/architecture/`. **Read it before asserting a structural fact**: it carries every
  partial-unique index with its exact predicate, every status vocabulary, every FK and
  ON DELETE, the fence primitives with their contracts, the armed-gate allowlist sizes,
  and every cap setting. Regenerate in the same commit that moves the code:
  `uv run python scripts/gen_architecture.py`. Do not hand-edit it.

- **Worktree directory:** `.worktrees/` (project-local, gitignored). Use this for any feature or plan work requiring isolation.
- **Plans:** saved to `docs/superpowers/plans/YYYY-MM-DD-<name>.md`.
  Executed plans are **pruned**: a plan is scaffolding, and once its work is merged the
  code is the result. The 2026-08-30 cleanup removed 60 executed plans and the 3
  superseded branch specs (~88k lines). Recover any of them from git history:
  `git log --all --diff-filter=D --name-only --format= -- 'docs/superpowers/**'` then
  `git show <commit>^:<path>`. **Specs are kept** — they carry the *why*, which the code
  does not.
- **Specs:** saved to `docs/superpowers/specs/YYYY-MM-DD-<name>.md`.
- **Prior investigations:** `docs/superpowers/INVESTIGATIONS.md` indexes all 25 spikes,
  multi-agent reviews and outside opinions in `notes/` and `reviews/` **by the question
  each answers**. **Check it before measuring anything** — two sessions have re-derived
  findings already recorded there. Completeness is pinned by `tests/architecture/`.

## Stack

- Python 3.12 — `uv` for packages, `ruff` for lint/format, `pytest` (+ testcontainers Postgres) for tests.
- **Fast local test runs:** `uv run pytest -n 16 --dist loadfile` (~1m vs ~11m serial).
  Each xdist worker starts its own testcontainers Postgres, so workers cannot see each
  other's data. `--dist loadfile` keeps every file's tests on one worker — do **not**
  switch to `--dist loadgroup`, which scatters all unmarked tests per-test.
  Deliberately NOT in `addopts`: CI runs on a 2-4 core runner where this is slower.
  - **`-n 16` is tuned to the 72-thread dev box** — it means 16 pytest processes AND 16
    Postgres containers. Scale down on a smaller machine; `-n 4` is the measured floor
    (still ~3m). Never use `-n auto`: `psutil` is not installed here, so xdist falls
    through to `sched_getaffinity` and resolves it to **72** workers.
  - Expected result is **`3361 passed, 1 skipped`** (2026-08-16). The **`1 skipped`** is
    the signal, not the total: that skip is a real-SDK test gated on `ANTHROPIC_API_KEY`
    (`tests/proposal_assist/test_drafting_claude_adapter.py`). With the key exported you
    get `0 skipped` and one more pass, and it makes a live API call. The pass total moves
    every slice — don't read a different total as a broken suite, read `0 skipped` as
    "a live API call just ran".
- FastAPI backend. PostgreSQL 16 + SQLAlchemy 2.x + Alembic, with `pgvector` for embeddings.
- APScheduler, in-process — **single-instance hard constraint** (the scheduler must not run on more than one process).
- **Frontend is a React 19 + Vite SPA** (react-router, TanStack Query, `openapi-fetch` against types generated from the FastAPI OpenAPI schema; `pnpm`). It owns the calls dashboard, proposal-assist workspace, admin, auth, profile, and settings pages. Jinja remains only for email-digest rendering; there is no server-rendered web UI.
- SPA serving is gated by `SPA_MODE`: `dev` (Vite dev server + proxy), `prod` (FastAPI serves the built bundle from `src/call_manager/web/spa/` — **prod MUST ship a built SPA**), `disabled`.

## LLM

Two tiers. The ranking/extraction interfaces are deliberately prompt-in/JSON-out and model-agnostic, so swapping a model is a config change, not a rewrite.

**Local — on-prem NVIDIA RTX 2080 Ti via Ollama (OpenAI-compatible HTTP):**
- Ranking: **Gemma 4 E4B (Q4_K_M)** — `GEMMA_MODEL` (`gemma4:e4b-it-q4_K_M`).
- Embeddings: **BGE-M3** (1024-dim) — `EMBEDDING_MODEL`; powers the cosine topic-similarity gate / embedding pre-filter. Dev fallback `mxbai-embed-large` (also 1024-dim).
- Citation audit also defaults to local Ollama (`CITATION_LLM_PROVIDER`).

**Hosted — Anthropic; opt-in, requires `ANTHROPIC_API_KEY` (disabled when unset):**
- Remote-rank escalation: pairs whose local aggregate score clears `RANKING_ESCALATION_MIN_SCORE` are re-ranked by **`claude-sonnet-4-6`** (`REMOTE_RANK_MODEL`), capped per day.
- Proposal-assist drafting: **Claude Agent SDK** adapter with **`claude-opus-5`** (`PROPOSAL_ASSIST_LLM_MODEL`).
