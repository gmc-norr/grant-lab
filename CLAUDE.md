# grant-lab

Internal hospital tool that aggregates funding calls (grants, EU calls, Vinnova programmes, etc.) from multiple sources, ranks them with a local LLM against organization- and group-level research profiles, and surfaces the most relevant calls via a web dashboard and weekly email digest. It also provides a proposal-assist workspace for drafting applications against the ranked calls.

The v1 design spec (`docs/superpowers/specs/2026-04-14-call-monitor-design.md`) was pruned from the working tree in the 2026-06-17 docs cleanup; recover it from git history if needed (`git log --all -- '*2026-04-14-call-monitor-design.md'`).

## Conventions

- **Commit titles name the change, not the activity.** `fix(collab): refuse a stale
  Y.Doc on reconnect` is findable; `Refactor and enhance codebase for clarity and
  performance` is not. The 2026-08-31 drift produced five such titles in a row, and one
  of them ("Refactor tests to use test_client_factory … and update dependencies")
  actually swapped the OIDC JWT library and rewrote claim validation — a
  security-relevant change a future reader would never find from its subject line
  (review 2026-09-02 §12). A title that would fit any commit in the repo is the tell.

- **The Python package is `grant_lab`** (renamed from `call_manager` on 2026-09-05).
  Documents under `docs/` written before that date still say `call_manager`, and are
  **deliberately left as written** — they are records of what the code was called at the
  time, and rewriting them would falsify their own history. Read `src/call_manager/x.py`
  in an old spec or plan as `src/grant_lab/x.py`. Two things keep the old name on
  purpose and must NOT be "made consistent": the **database role and database name**
  (`call_manager`, in `docker-compose.yml` and your `DATABASE_URL` — renaming them
  requires an edit to the live `.env` and a Postgres rename), and the
  `CALL_MANAGER_DATA_DIR` env var for the same reason. Also still deliberate: the
  session-cookie salt, the OAuth HKDF info string and `SCHEDULER_LOCK_NAME` — see the
  `chore(naming)` commit for why each would break something real.

- **Research artefacts live in `docs/research/`**, not the repo root. `README.pdf` is a
  rendered copy of the README and is gitignored — do not re-add it.

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
  Deliberately NOT in `addopts`, because `-n 16` is wrong for CI: **CI runs
  `uv run pytest -v -n 2 --dist loadfile`** (`.github/workflows/ci.yml`, the `backend`
  job). This line used to say parallel was *slower* on CI's 2-4 core runner. That was
  a prediction, never a measurement, and the 2026-10-26 review (A1) measured the
  opposite — but measured it on a **984-test subset** (web + collab) with pytest
  pinned to 2 cores and **Postgres left unpinned**, which A1 itself calls optimistic:
  308 s serial → 162 s at `-n 2`. **`-n 2` on a real GitHub runner is still
  unmeasured**, and A1 lists it under "Not checked". Do not read 308 s as the full
  suite's serial CI time; the suite is ~6x that subset.
  - **`-n 16` is tuned to the 72-thread dev box** — it means 16 pytest processes AND 16
    Postgres containers. Scale down on a smaller machine; `-n 4` is the measured floor
    **on this box** (still ~3m). CI deliberately runs below that floor, because a
    2-core runner cannot host 4 Postgres containers at all — the floor is a statement
    about this box, not a lower bound for every machine. Never use `-n auto`: `psutil`
    is not installed here, so xdist falls through to `sched_getaffinity` and resolves
    it to **72** workers.
  - **The `1 skipped` is the expectation. The pass total is not.** That skip is a
    real-SDK test gated on `ANTHROPIC_API_KEY`
    (`tests/proposal_assist/test_drafting_claude_adapter.py`); with the key exported you
    get `0 skipped` and one more pass, and it makes a live API call. So read `0 skipped`
    as "a live API call just ran", and read a different total as "a slice merged".
  - Recent measurements, for order of magnitude only: **4857** at `f0aaed0c`
    (2026-09-03), **3361** on 2026-08-16. This line said `3361` for two and a half
    weeks after it stopped being true, and a plan copied it into its own "Global
    Constraints" as a hard expectation — so the 2026-09-04 Call Match run opened
    against a base **1496 tests higher** than its brief claimed. **Measure the base
    yourself before you assert a total, carry your plan's DELTAS against what you
    measured, and never adjust an expected number to make the arithmetic close.**
- FastAPI backend. PostgreSQL 16 + SQLAlchemy 2.x + Alembic, with `pgvector` for embeddings.
- APScheduler, in-process — **single-instance hard constraint** (the scheduler must not run on more than one process).
- **Frontend is a React 19 + Vite SPA** (react-router, TanStack Query, `openapi-fetch` against types generated from the FastAPI OpenAPI schema; `pnpm`). It owns the calls dashboard, proposal-assist workspace, admin, auth, profile, and settings pages. Jinja remains only for email-digest rendering; there is no server-rendered web UI.
- SPA serving is gated by `SPA_MODE`: `dev` (Vite dev server + proxy), `prod` (FastAPI serves the built bundle from `src/grant_lab/web/spa/` — **prod MUST ship a built SPA**), `disabled`.

## LLM

Two tiers. The ranking/extraction interfaces are deliberately prompt-in/JSON-out and model-agnostic, so swapping a model is a config change, not a rewrite.

**Local — on-prem NVIDIA RTX 2080 Ti via Ollama (OpenAI-compatible HTTP):**
- Ranking: **Gemma 4 E4B (Q4_K_M)** — `GEMMA_MODEL` (`gemma4:e4b-it-q4_K_M`).
- Embeddings: **BGE-M3** (1024-dim) — `EMBEDDING_MODEL`; powers the cosine topic-similarity gate / embedding pre-filter. Dev fallback `mxbai-embed-large` (also 1024-dim).
- Citation audit also defaults to local Ollama (`CITATION_LLM_PROVIDER`).

**Hosted — Anthropic; opt-in, requires `ANTHROPIC_API_KEY` (disabled when unset):**
- Remote-rank escalation: pairs whose local aggregate score clears `RANKING_ESCALATION_MIN_SCORE` are re-ranked by **`claude-sonnet-5-5`** (`REMOTE_RANK_MODEL`), capped per day.
- Proposal-assist drafting: **Claude Agent SDK** adapter with **`claude-opus-5`** (`PROPOSAL_ASSIST_LLM_MODEL`).
