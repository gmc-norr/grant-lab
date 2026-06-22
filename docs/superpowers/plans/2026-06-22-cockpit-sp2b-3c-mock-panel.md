# SP2b-3c — On-demand Mock-Panel Review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an on-demand "Run full mock-panel review now" action — a ~6-call hosted-Opus fan-out (fit-check + 4 personas + consolidation) that writes `fit-check.md` + `rubric-review.md` into the workspace repo and surfaces them in a new "Panel review" cockpit tab.

**Architecture:** Reuse the dormant `ReviewRun`/`ReviewRunItem` tables (zero migration). A new `proposal_assist/review/` package is a *bespoke* background job (the 6-call fan-out is unlike the single-call coherence/evaluator passes) but mirrors their queue/scheduler/gate hardening. The only shared-code extraction is `_gate_cloud_llm` → `web/_cloud_gate.py`. Personas run as one-shot SDK adapters whose role lives in the **instruction** (the adapter has no `system_prompt` param), with a forced `submit_review` tool as their only capability.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, APScheduler, Claude Agent SDK adapter (`claude-opus-4-7`), React 18 + Vite + TanStack Query + `openapi-fetch`, pytest (+ testcontainers Postgres), vitest.

Spec: `docs/superpowers/specs/2026-06-22-cockpit-sp2b-3c-mock-panel-design.md`.

## Global Constraints

- **Default branch is `master`.** Work ONLY in this worktree (`.worktrees/sp2b-3c-mock-panel`, branch `feat/sp2b-3c-mock-panel`). Every git command runs with `git -C <worktree>` (or after `cd` into it); verify `git branch --show-current` == `feat/sp2b-3c-mock-panel` before any commit. Never edit/commit in the main checkout.
- **No new migration.** Alembic head stays `0054`. The `ReviewRun`/`ReviewRunItem` tables (migrations 0026/0033) and `Workspace.review_runs_per_day_override`/`review_dollar_cap_override` (0023) already exist.
- **Do NOT touch** `proposal_assist/drafting/claude_adapter.py` or `drafting/contract.py`. The personas use the adapter as-is.
- **Tests:** run only the affected files, append `; echo $?`, never pipe pytest through `tail`. Full `uv run pytest` is testcontainer-flaky (intermittent connection-refused) — re-run a connection-refused failure in isolation before treating it as real. Backend test command form: `uv run pytest tests/proposal_assist/review/test_X.py -v ; echo $?`.
- **Frontend gate is `pnpm exec tsc --noEmit`** (there is NO TS linter). Run `pnpm install` after pulling. Regenerate the OpenAPI client with `EMBEDDING_MODEL=bge-m3 pnpm gen:api` (the default `.env` model logs a stdout WARNING that corrupts `schema.gen.ts`).
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Commit with `git -c commit.gpgsign=false commit`.
- **Model id** is `settings.proposal_assist_llm_model` (currently `claude-opus-4-7`). Pricing (config): $5.00/MTok input, $25.00/MTok output.
- **`ReviewRun` column facts:** `triggered_at` (server-default `now()`, used for both the daily cap window and the reaper age cutoff — there is **no `started_at`** on the run), `draft_sha` is **NOT NULL** (set at enqueue), `fit_check_sha`/`rubric_review_sha`/`estimated_dollars` nullable. `status` CHECK = `queued|running|done|failed|cancelled|partial`. `uq_review_runs_active` = partial-unique on `workspace_id` where `status IN ('queued','running')`. `ReviewRunItem.kind` CHECK = `fit_check|persona_domain_skeptic|persona_methodology_critic|persona_translation_skeptic|persona_devils_advocate|consolidation`; `status` CHECK = `queued|running|done|failed|skipped|cancelled`; `UNIQUE(review_run_id, kind)`; has `started_at`/`completed_at`/`raw_response_json` (JSONB)/`error_message`/`estimated_dollars` (`Numeric(6,4)`). `ReviewRun.estimated_dollars` is `Numeric(6,3)`.

---

## File Structure

**New (backend):**
- `proposal_assist/web/_cloud_gate.py` — shared cloud-LLM trigger gate.
- `proposal_assist/citations/gitio.py` — *edited* (add `blob_sha`, `read_blob`).
- `proposal_assist/review/__init__.py`, `context.py`, `personas.py`, `review_personas.yaml`, `agent.py`, `jobs.py`, `pass_runner.py`.
- `proposal_assist/tools/review_submit.py` — the `submit_review` tool impl.

**New (frontend):**
- `frontend/src/api/review.ts`, `frontend/src/cockpit/ReviewPanel.tsx`.

**Edited:** `config.py`, `scheduler.py`, `main.py`, `tools/registry.py`, `web/coherence.py`, `web/evaluator.py`, `web/workspaces.py` (soft-delete), `frontend/src/routes/Cockpit.tsx`, `frontend/src/api/schema.gen.ts` (regenerated).

**Tests:** `tests/proposal_assist/review/` (mirrors `tests/proposal_assist/evaluator/`) + a frontend spec.

---

## Task 1: Foundation — shared gate, git helpers, config

**Files:**
- Create: `src/call_manager/proposal_assist/web/_cloud_gate.py`
- Modify: `src/call_manager/proposal_assist/web/coherence.py:36-65`, `src/call_manager/proposal_assist/web/evaluator.py:39-66`
- Modify: `src/call_manager/proposal_assist/citations/gitio.py` (append two functions)
- Modify: `src/call_manager/config.py` (add import + a `review_*` block)
- Test: `tests/proposal_assist/review/__init__.py` (new, empty), `tests/proposal_assist/review/test_cloud_gate.py`, `tests/proposal_assist/review/test_gitio_blob.py`, `tests/proposal_assist/review/test_config.py`

**Interfaces:**
- Produces: `web._cloud_gate.gate_cloud_llm(*, ws, request, session, user, disclosure_action: str) -> None`; `citations.gitio.blob_sha(repo_dir, ref, path) -> str`; `citations.gitio.read_blob(repo_dir, sha) -> str`; `Settings.review_*` fields (see below).

- [ ] **Step 1: Write the failing gate test**

`tests/proposal_assist/review/test_cloud_gate.py`:
```python
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm


def _req(drafting_enabled=True):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(drafting_enabled=drafting_enabled))))


def test_gate_403_message_uses_disclosure_action(monkeypatch):
    ws = SimpleNamespace(current_phase="active", id="W")
    user = SimpleNamespace(id=7)

    class _S:  # session whose scalar() returns None (no prior ack)
        def scalar(self, *a, **k):
            return None

    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=ws, request=_req(), session=_S(), user=user,
                       disclosure_action="running a mock-panel review")
    assert ei.value.status_code == 403
    assert "running a mock-panel review" in ei.value.detail


def test_gate_409_setup_and_503_drafting():
    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=SimpleNamespace(current_phase="setup", id="W"),
                       request=_req(), session=object(), user=user, disclosure_action="x")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=SimpleNamespace(current_phase="active", id="W"),
                       request=_req(drafting_enabled=False), session=object(), user=user,
                       disclosure_action="x")
    assert ei.value.status_code == 503
```
Also create the empty `tests/proposal_assist/review/__init__.py`.

- [ ] **Step 2: Run it; expect import failure**

Run: `uv run pytest tests/proposal_assist/review/test_cloud_gate.py -v ; echo $?`
Expected: FAIL (ModuleNotFoundError: `_cloud_gate`).

- [ ] **Step 3: Create the shared gate**

`src/call_manager/proposal_assist/web/_cloud_gate.py`:
```python
"""Shared cloud-LLM trigger gate for the manual background-job endpoints
(coherence, evaluator, review). Extracted from the previously-duplicated
per-endpoint copies; the ONLY per-caller difference is the disclosure_action
phrase woven into the 403 message. Same order + status codes as the interactive
turns route (web/workspaces.py): setup -> 409, drafting disabled -> 503, missing
per-actor disclosure ack -> 403."""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace, WorkspaceEvent


def gate_cloud_llm(
    *, ws: Workspace, request: Request, session: Session, user, disclosure_action: str
) -> None:
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
    if not request.app.state.settings.drafting_enabled:
        raise HTTPException(
            status_code=503,
            detail="Drafting is currently disabled by an administrator. Please try again later.",
        )
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    if prior_ack is None:
        raise HTTPException(
            status_code=403,
            detail=f"Please acknowledge the data-disclosure notice before {disclosure_action}.",
        )
```

- [ ] **Step 4: Rewire the two siblings to a thin wrapper (preserve their tests + exact 403 phrases)**

In `src/call_manager/proposal_assist/web/coherence.py`, replace the whole `_gate_cloud_llm` function body (lines 36-65) with a thin delegating wrapper (keeps the same name, so any direct-import test still resolves):
```python
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm


def _gate_cloud_llm(*, ws, request, session, user) -> None:
    gate_cloud_llm(
        ws=ws, request=request, session=session, user=user,
        disclosure_action="starting a coherence pass",
    )
```
(Remove the now-unused `HTTPException`/`select`/`WorkspaceEvent` imports from coherence.py only if nothing else uses them — check first; leave them if other functions reference them.)

In `src/call_manager/proposal_assist/web/evaluator.py`, do the same (lines 39-66):
```python
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm


def _gate_cloud_llm(*, ws, request, session, user) -> None:
    gate_cloud_llm(
        ws=ws, request=request, session=session, user=user,
        disclosure_action="running an evaluation",
    )
```

- [ ] **Step 5: Run the gate test + the existing sibling endpoint tests**

Run: `uv run pytest tests/proposal_assist/review/test_cloud_gate.py tests/proposal_assist/coherence/ tests/proposal_assist/evaluator/test_web_evaluator.py -v ; echo $?`
Expected: PASS (the siblings' 403 messages are byte-identical, so their gate assertions still hold). If a connection-refused error appears, re-run the failing file alone.

- [ ] **Step 6: Write the git-helper test**

`tests/proposal_assist/review/test_gitio_blob.py`:
```python
import subprocess
from pathlib import Path

from call_manager.proposal_assist.citations.gitio import (
    blob_sha,
    read_blob,
    write_and_commit_files,
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *args], cwd=repo, check=True)
    write_and_commit_files(repo, {"fit-check.md": "# fit\n"}, "seed")
    return repo


def test_blob_sha_and_read_blob_roundtrip(tmp_path):
    repo = _init_repo(tmp_path)
    sha = blob_sha(repo, "HEAD", "fit-check.md")
    assert len(sha) == 40
    assert read_blob(repo, sha) == "# fit\n"


def test_blob_sha_missing_path_raises(tmp_path):
    repo = _init_repo(tmp_path)
    import pytest

    from call_manager.proposal_assist.citations.gitio import GitCommitError

    with pytest.raises(GitCommitError):
        blob_sha(repo, "HEAD", "nope.md")
```

- [ ] **Step 7: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_gitio_blob.py -v ; echo $?`
Expected: FAIL (cannot import `blob_sha`).

- [ ] **Step 8: Add the git helpers**

Append to `src/call_manager/proposal_assist/citations/gitio.py` (after `current_head`):
```python
def blob_sha(repo_dir: Path, ref: str, path: str) -> str:
    """Return the git blob SHA of *path* at *ref* (e.g. 'HEAD'). Used to pin the
    exact bytes of a committed review file so the read endpoint can fetch them by
    SHA. GIT_CEILING_DIRECTORIES pins discovery exactly like current_head()."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"{ref}:{path}"],
            cwd=Path(repo_dir),
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError as exc:
        raise GitCommitError(f"rev-parse {ref}:{path} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(f"rev-parse {ref}:{path} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def read_blob(repo_dir: Path, sha: str) -> str:
    """Return the content of a git blob by SHA (`git cat-file -p`), decoded with
    errors="replace" so a corrupt or binary blob can never raise UnicodeDecodeError
    up into the endpoint (review files are text, but be defensive — Codex Low).
    GIT_CEILING_DIRECTORIES pinned like current_head(). Caller validates the SHA
    shape before passing it."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-p", sha],
            cwd=Path(repo_dir),
            capture_output=True,  # bytes (NO text=True) so WE control decoding
            env=env,
        )
    except OSError as exc:
        raise GitCommitError(f"cat-file {sha} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(
            f"cat-file {sha} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace")
```

- [ ] **Step 9: Run the git-helper test; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_gitio_blob.py -v ; echo $?`
Expected: PASS.

- [ ] **Step 10: Add the config block**

In `src/call_manager/config.py`: ensure `from decimal import Decimal` is imported at the top (it is NOT currently — add it). Then add, right after the `evaluator_daily_token_cap` field (line ~288), inside the `Settings` class:
```python
    # SP2b-3c: on-demand mock-panel review (the 6-call fan-out background job).
    review_tick_seconds: int = Field(
        default=20,
        alias="REVIEW_TICK_SECONDS",
        description="How often the review drain tick claims+runs one queued mock-panel run.",
    )
    review_persona_timeout_seconds: int = Field(
        default=180,
        alias="REVIEW_PERSONA_TIMEOUT_SECONDS",
        description="Per sub-call asyncio.wait_for bound (launch+drive) on each persona / "
        "fit-check / consolidation SDK turn.",
    )
    review_job_timeout_seconds: int = Field(
        default=900,
        alias="REVIEW_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL: a running review run older than this (by triggered_at) is "
        "failed (crash recovery), unwedging the active-unique slot. The whole 6-call fan-out "
        "must fit well under this.",
    )
    review_reaper_tick_seconds: int = Field(
        default=60,
        alias="REVIEW_REAPER_TICK_SECONDS",
        description="How often the review reaper sweep runs (decoupled from the TTL above).",
    )
    review_terminate_timeout_seconds: int = Field(
        default=30,
        alias="REVIEW_TERMINATE_TIMEOUT_SECONDS",
        description="Per sub-call asyncio.wait_for bound on each one-shot adapter's terminate().",
    )
    review_persona_concurrency: int = Field(
        default=3,
        alias="REVIEW_PERSONA_CONCURRENCY",
        description="Max simultaneous wave-1 sub-calls (fit-check + 4 personas) within one run.",
    )
    review_runs_per_day_default: int = Field(
        default=5,
        alias="REVIEW_RUNS_PER_DAY_DEFAULT",
        description="Global per-workspace daily cap on mock-panel runs (Europe/Stockholm day); "
        "overridable per workspace via Workspace.review_runs_per_day_override.",
    )
    review_dollar_cap_default: Decimal = Field(
        default=Decimal("5.000"),
        alias="REVIEW_DOLLAR_CAP_DEFAULT",
        description="Global per-workspace daily $ cap on mock-panel runs; overridable per "
        "workspace via Workspace.review_dollar_cap_override.",
    )
    review_price_input_per_mtok: Decimal = Field(
        default=Decimal("5.00"),
        alias="REVIEW_PRICE_INPUT_PER_MTOK",
        description="claude-opus-4-7 input price per 1M tokens (cache-read is bundled into "
        "tokens_in by the adapter and priced at this rate).",
    )
    review_price_output_per_mtok: Decimal = Field(
        default=Decimal("25.00"),
        alias="REVIEW_PRICE_OUTPUT_PER_MTOK",
        description="claude-opus-4-7 output price per 1M tokens.",
    )
```

- [ ] **Step 11: Write + run the config test**

`tests/proposal_assist/review/test_config.py`:
```python
from decimal import Decimal

from call_manager.config import Settings


def test_review_defaults():
    s = Settings()
    assert s.review_persona_concurrency == 3
    assert s.review_runs_per_day_default == 5
    assert s.review_dollar_cap_default == Decimal("5.000")
    assert s.review_price_input_per_mtok == Decimal("5.00")
    assert s.review_price_output_per_mtok == Decimal("25.00")
    assert s.review_job_timeout_seconds == 900
```
Run: `uv run pytest tests/proposal_assist/review/test_config.py -v ; echo $?`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel
git branch --show-current   # must print feat/sp2b-3c-mock-panel
git add src/call_manager/proposal_assist/web/_cloud_gate.py \
        src/call_manager/proposal_assist/web/coherence.py \
        src/call_manager/proposal_assist/web/evaluator.py \
        src/call_manager/proposal_assist/citations/gitio.py \
        src/call_manager/config.py \
        tests/proposal_assist/review/
git -c commit.gpgsign=false commit -m "feat(review): SP2b-3c foundation — shared cloud gate, git blob helpers, config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Personas + context + submit tool + adapter

**Files:**
- Create: `src/call_manager/proposal_assist/review/__init__.py` (empty), `review/personas.py`, `review/review_personas.yaml`, `review/context.py`, `review/agent.py`, `src/call_manager/proposal_assist/tools/review_submit.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (add `ToolContext.review_run_item_id`; register `submit_review`)
- Test: `tests/proposal_assist/review/test_personas.py`, `test_context.py`, `test_submit_review.py`

**Interfaces:**
- Consumes: `Settings.review_*` (Task 1); `ReviewRun`/`ReviewRunItem` models.
- Produces:
  - `review.personas`: `REVIEW_KINDS`, `PERSONA_KINDS`, `WAVE1_KINDS` tuples; `persona_prompt(kind: str) -> str`.
  - `review.context.assemble_review_context(*, session, workspace_id, workspaces_dir) -> ReviewContext` where `ReviewContext = {draft_text, spec_text, rubric: list[dict], funder_notes: str, draft_sha: str}`; `persona_instruction(kind: str, ctx: ReviewContext) -> str`; `consolidation_instruction(ctx, persona_md: dict[str,str], fit_check_md: str|None) -> str`.
  - `tools.review_submit.submit_review_impl(ctx, *, markdown: str) -> dict`.
  - `review.agent.build_review_adapter(*, settings, session_factory, workspace_id, review_run_id, item_id, kind) -> ClaudeAgentSDKAdapter`; `review_allowed_tools() -> list[str]`.
  - `ToolContext.review_run_item_id: uuid.UUID | None`.

- [ ] **Step 1: Create the persona prompts file**

`src/call_manager/proposal_assist/review/review_personas.yaml` — six keys, each a system-role paragraph. Every prompt ends with the "only submit_review" line. (Author real prose; the text below is the required content, not a placeholder.)
```yaml
fit_check: |
  You are the eligibility-and-fit screener on a mock grant-review panel for a Swedish
  university-hospital research group (typical funders: Vinnova, the Swedish Research Council,
  EU programmes). Judge ONLY whether the DRAFT proposal actually fits THIS call: eligibility,
  scope match, mandatory criteria, and any deal-breakers. Output a short markdown verdict —
  a heading line stating GO / MARGINAL / NO-FIT, then the concrete mismatches or confirmations
  (bullet points), grounded in the rubric and funder notes. Be specific; do not restate the
  proposal. Call submit_review EXACTLY ONCE with your markdown; you have no other tools;
  everything you need is below.
persona_domain_skeptic: |
  You are a subject-matter expert in the proposal's research domain on a mock grant-review
  panel. Critique the science: is it novel, significant, and credible? Where is it thin,
  overclaimed, behind the field, or missing key prior work? Output prioritized markdown
  bullets (most serious first), each naming the specific weakness and where in the DRAFT it
  appears. Call submit_review EXACTLY ONCE with your markdown; you have no other tools;
  everything you need is below.
persona_methodology_critic: |
  You are a methods-and-statistics reviewer on a mock grant-review panel. Critique the aims,
  study design, power/sample size, controls, feasibility, and timeline. Flag what a tough
  methods panelist would: under-powered analyses, confounds, vague endpoints, unrealistic
  milestones. Output prioritized markdown bullets, each naming the flaw and the relevant DRAFT
  section. Call submit_review EXACTLY ONCE with your markdown; you have no other tools;
  everything you need is below.
persona_translation_skeptic: |
  You are the research-to-clinic / societal-impact reviewer on a mock grant-review panel for a
  hospital research group (the Vinnova–healthcare angle). Critique the path from result to
  clinical or societal benefit: is the translation real and credible, or hand-waved? Is the
  implementation, adoption, and stakeholder story concrete? Is the impact proportionate to the
  ask? Output prioritized markdown bullets. Call submit_review EXACTLY ONCE with your markdown;
  you have no other tools; everything you need is below.
persona_devils_advocate: |
  You are the panelist looking for reasons to reject on a mock grant-review panel. Find the
  weakest link, internal contradictions, unstated assumptions, and the single strongest
  argument a hostile reviewer would make against funding. Be adversarial but fair and specific.
  Output prioritized markdown bullets. Call submit_review EXACTLY ONCE with your markdown; you
  have no other tools; everything you need is below.
consolidation: |
  You are the CHAIR of a mock grant-review panel. You are given the panelists' individual
  critiques and the eligibility/fit verdict. Synthesize them into ONE prioritized, de-duplicated
  review of the DRAFT against the funder rubric: a short overall assessment, then the top issues
  ordered by how much they threaten funding, each with the criterion it touches and a concrete
  suggested fix. Do not invent new criticisms beyond the panelists' input and the rubric. Output
  markdown. Call submit_review EXACTLY ONCE with your markdown; you have no other tools;
  everything you need is below.
```

- [ ] **Step 2: Write the personas test**

`tests/proposal_assist/review/test_personas.py`:
```python
from call_manager.proposal_assist.review.personas import (
    PERSONA_KINDS,
    REVIEW_KINDS,
    WAVE1_KINDS,
    persona_prompt,
)


def test_kind_sets():
    assert set(REVIEW_KINDS) == {
        "fit_check", "persona_domain_skeptic", "persona_methodology_critic",
        "persona_translation_skeptic", "persona_devils_advocate", "consolidation",
    }
    assert "fit_check" not in PERSONA_KINDS and len(PERSONA_KINDS) == 4
    assert WAVE1_KINDS == ("fit_check", *PERSONA_KINDS)


def test_every_kind_has_a_prompt_with_submit_review():
    for kind in REVIEW_KINDS:
        p = persona_prompt(kind)
        assert p and "submit_review" in p
```

- [ ] **Step 3: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_personas.py -v ; echo $?`
Expected: FAIL (no `review.personas`).

- [ ] **Step 4: Create `review/__init__.py` (empty) and `review/personas.py`**

`src/call_manager/proposal_assist/review/personas.py`:
```python
"""Loads review_personas.yaml — the six mock-panel role prompts keyed by
ReviewRunItem.kind. Validated at load against the CHECK-constraint kind set so a
drift between the yaml and the schema fails fast. The prompt is the LEADING
paragraph of each sub-call's INSTRUCTION (the SDK adapter has no system_prompt
parameter — see review/agent.py)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REVIEW_KINDS: tuple[str, ...] = (
    "fit_check",
    "persona_domain_skeptic",
    "persona_methodology_critic",
    "persona_translation_skeptic",
    "persona_devils_advocate",
    "consolidation",
)
PERSONA_KINDS: tuple[str, ...] = (
    "persona_domain_skeptic",
    "persona_methodology_critic",
    "persona_translation_skeptic",
    "persona_devils_advocate",
)
WAVE1_KINDS: tuple[str, ...] = ("fit_check", *PERSONA_KINDS)

_YAML_PATH = Path(__file__).with_name("review_personas.yaml")


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    data = yaml.safe_load(_YAML_PATH.read_text())
    if not isinstance(data, dict) or set(data) != set(REVIEW_KINDS):
        raise RuntimeError(
            f"review_personas.yaml keys {sorted(data or {})} != {sorted(REVIEW_KINDS)}"
        )
    return {k: str(v).strip() for k, v in data.items()}


def persona_prompt(kind: str) -> str:
    return _load()[kind]
```

- [ ] **Step 5: Run the personas test; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_personas.py -v ; echo $?`
Expected: PASS.

- [ ] **Step 6: Write the context test**

`tests/proposal_assist/review/test_context.py` — mirrors `tests/proposal_assist/evaluator/test_context.py`. It builds a workspace repo with `proposal.md`/`proposal-spec.md`, calls `assemble_review_context`, and asserts the instruction contains the persona role + rubric + draft, and `draft_sha` is the sha256 of the proposal bytes:
```python
import hashlib
import uuid

from call_manager.proposal_assist.review.context import (
    assemble_review_context,
    consolidation_instruction,
    persona_instruction,
)
from call_manager.proposal_assist.review.personas import persona_prompt


def test_context_and_instructions(review_workspace):
    # review_workspace fixture (added below) -> (session, workspace_id, workspaces_dir)
    session, ws_id, wdir, repo = review_workspace
    repo.joinpath("proposal.md").write_text("# Aims\nWe will cure X.\n")
    repo.joinpath("proposal-spec.md").write_text("# Intent\nCure X by 2030.\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "draft"], cwd=repo, check=True)

    ctx = assemble_review_context(session=session, workspace_id=ws_id, workspaces_dir=wdir)
    assert ctx.draft_sha == hashlib.sha256(b"# Aims\nWe will cure X.\n").hexdigest()
    instr = persona_instruction("persona_domain_skeptic", ctx)
    assert persona_prompt("persona_domain_skeptic")[:40] in instr
    assert "We will cure X." in instr
    cons = consolidation_instruction(ctx, {"persona_domain_skeptic": "- weak novelty"}, "GO")
    assert "weak novelty" in cons and "GO" in cons
```
Add the shared `review_workspace` fixture to `tests/proposal_assist/review/conftest.py` (mirror the evaluator's workspace fixture; it must yield `(session, workspace_id, workspaces_dir, repo_path)` with an initialized git repo at `<workspaces_dir>/<id>/repo` and a `Workspace` row with `current_phase="active"`, a non-empty `rubric_criteria`, and `rubric_funder_notes`). Reuse the evaluator test's fixture pattern verbatim, extended to return the repo path.

- [ ] **Step 7: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_context.py -v ; echo $?`
Expected: FAIL.

- [ ] **Step 8: Create `review/context.py`**

`src/call_manager/proposal_assist/review/context.py` (mirrors `evaluator/context.py` — reuse its `content_sha`/`_read_artifact`/`_render_criteria`/`_render_sections` helpers by importing them):
```python
"""Assemble the mock-panel sub-call instructions (persona role + criteria + indexed
spec + indexed draft) and capture the draft's content sha256 for staleness. Pure
(DB read + file reads). Reuses the evaluator's artifact/criteria renderers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.evaluator.context import (
    _read_artifact,
    _render_criteria,
    _render_sections,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.personas import persona_prompt


@dataclass
class ReviewContext:
    draft_text: str
    spec_text: str
    rubric: list[dict]
    funder_notes: str
    draft_sha: str  # sha256 of the EXACT proposal.md bytes assembled


def assemble_review_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> ReviewContext:
    ws = session.get(Workspace, workspace_id)
    rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_artifact(repo_dir, PROPOSAL_FILENAME)
    spec_text, _ = _read_artifact(repo_dir, SPEC_FILENAME)
    return ReviewContext(
        draft_text=draft_text,
        spec_text=spec_text,
        rubric=rubric,
        funder_notes=funder_notes,
        draft_sha=draft_sha,
    )


def _blocks(ctx: ReviewContext) -> str:
    draft_sections = split_sections(ctx.draft_text) if ctx.draft_text.strip() else []
    spec_sections = split_sections(ctx.spec_text) if ctx.spec_text.strip() else []
    draft_block = _render_sections(draft_sections) if draft_sections else "(draft is empty)"
    spec_block = _render_sections(spec_sections) if spec_sections else "(spec is empty)"
    return (
        f"\n# Funder criteria\n{_render_criteria(ctx.rubric)}\n"
        f"\n# Funder notes\n{ctx.funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec_block}\n"
        f"\n# DRAFT (proposal.md)\n{draft_block}\n"
    )


def persona_instruction(kind: str, ctx: ReviewContext) -> str:
    return f"{persona_prompt(kind)}\n{_blocks(ctx)}"


def consolidation_instruction(
    ctx: ReviewContext, persona_md: dict[str, str], fit_check_md: str | None
) -> str:
    parts = [persona_prompt("consolidation"), _blocks(ctx), "\n# Panelist critiques\n"]
    if fit_check_md:
        parts.append(f"\n## Eligibility / fit verdict\n{fit_check_md}\n")
    for kind, md in persona_md.items():
        parts.append(f"\n## {kind}\n{md}\n")
    return "".join(parts)
```
Note: confirm `_read_artifact`, `_render_criteria`, `_render_sections` are importable from `evaluator/context.py` (they are module-level there). If any is name-mangled or private in a way that linting forbids cross-module import, copy its body into `review/context.py` instead (do NOT change the evaluator file).

- [ ] **Step 9: Run the context test; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_context.py -v ; echo $?`
Expected: PASS. (If connection-refused, re-run alone.)

- [ ] **Step 10: Add `ToolContext.review_run_item_id` + write the submit-tool test**

In `src/call_manager/proposal_assist/tools/registry.py`, add to the `ToolContext` dataclass (after the `eval_*` fields, ~line 102), additive and defaulted:
```python
    # SP2b-3c: the mock-panel sub-call's target ReviewRunItem. submit_review writes
    # the produced markdown onto this row. origin_kind="review_session",
    # origin_job_id=<review_runs.id>.
    review_run_item_id: uuid.UUID | None = field(default=None)
```

`tests/proposal_assist/review/test_submit_review.py` (mirrors `test_submit_scorecard.py`):
```python
import uuid

import pytest

from call_manager.db.models import ReviewRun, ReviewRunItem
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.tools.review_submit import submit_review_impl


def _ctx(session_factory, ws_id, run_id, item_id):
    return ToolContext(
        workspace_id=ws_id, repo_dir=None, session_factory=session_factory,
        settings=None, origin_kind="review_session", origin_job_id=run_id,
        review_run_item_id=item_id,
    )


@pytest.mark.asyncio
async def test_submit_stores_markdown_once(review_run_with_item):
    session_factory, ws_id, run_id, item_id = review_run_with_item
    ctx = _ctx(session_factory, ws_id, run_id, item_id)
    out = await submit_review_impl(ctx, markdown="# Findings\n- x")
    assert out == {"stored": True}
    with session_factory() as s:
        item = s.get(ReviewRunItem, item_id)
        assert item.raw_response_json == {"markdown": "# Findings\n- x"}
    # exactly-once
    out2 = await submit_review_impl(ctx, markdown="again")
    assert "error" in out2


@pytest.mark.asyncio
async def test_submit_rejects_empty(review_run_with_item):
    session_factory, ws_id, run_id, item_id = review_run_with_item
    out = await submit_review_impl(_ctx(session_factory, ws_id, run_id, item_id), markdown="  ")
    assert "error" in out


@pytest.mark.asyncio
async def test_submit_rejects_foreign_workspace(review_run_with_item):
    # A ctx stamped with a DIFFERENT workspace_id than the run's must be rejected.
    session_factory, ws_id, run_id, item_id = review_run_with_item
    other_ws = uuid.uuid4()
    ctx = _ctx(session_factory, other_ws, run_id, item_id)
    out = await submit_review_impl(ctx, markdown="# x")
    assert "error" in out
```
Add a `review_run_with_item` fixture to `conftest.py` that creates a `ReviewRun(status="running", draft_sha="x"*64)` and one `ReviewRunItem(kind="fit_check", status="running")`, yielding `(session_factory, ws_id, run_id, item_id)`.

- [ ] **Step 11: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_submit_review.py -v ; echo $?`
Expected: FAIL.

- [ ] **Step 12: Create the submit tool**

`src/call_manager/proposal_assist/tools/review_submit.py` (mirrors `tools/scorecard.py`):
```python
"""submit_review tool impl: the ONLY tool a mock-panel sub-call (fit-check, a
persona, or consolidation) may call. Validates a non-empty markdown body and
writes it onto the running ReviewRunItem identified by ctx.review_run_item_id.
Mirrors the propose_*/submit_scorecard contract: returns {"error": ...} for every
expected failure, never raises. Exactly-once: a second call errors."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import ReviewRun, ReviewRunItem
from call_manager.proposal_assist.tools.registry import ToolContext


async def submit_review_impl(ctx: ToolContext, *, markdown: str) -> dict[str, Any]:
    body = markdown.strip() if isinstance(markdown, str) else ""
    if not body:
        return {"error": "markdown must be a non-empty string"}
    with ctx.session_factory() as session:
        item = session.get(ReviewRunItem, ctx.review_run_item_id, with_for_update=True)
        if (
            item is None
            or item.review_run_id != ctx.origin_job_id
            or item.status != "running"
        ):
            return {"error": "submit_review target is not a running item for this run"}
        # Verify workspace ownership of the parent run (mirrors submit_scorecard's
        # job.workspace_id == ctx.workspace_id guard) — a stale/mismatched ctx must
        # never write into another workspace's run.
        run = session.get(ReviewRun, item.review_run_id)
        if run is None or run.workspace_id != ctx.workspace_id or run.status != "running":
            return {"error": "submit_review target run is not active for this workspace"}
        if item.raw_response_json is not None:
            return {"error": "a review has already been submitted for this item"}
        item.raw_response_json = {"markdown": body}
        session.commit()
    return {"stored": True}
```

- [ ] **Step 13: Register the tool**

In `src/call_manager/proposal_assist/tools/registry.py`, register `submit_review` exactly as `submit_scorecard` is registered (find the `@server.tool(...)` / tool-list block for `submit_scorecard` and add a sibling that calls `submit_review_impl`). Import `submit_review_impl` at the top. The tool's MCP name must be `submit_review` (so the allowlist `["submit_review"]` matches; the SDK prefixes MCP tools — match the exact prefix convention `submit_scorecard` uses, e.g. `mcp__proposal_assist__submit_review`, by following the same registration call).

- [ ] **Step 14: Create the adapter builder**

`src/call_manager/proposal_assist/review/agent.py` (mirrors `evaluator/agent.py`):
```python
"""Build a mock-panel sub-call's OWN one-shot SDK adapter. Each sub-call (fit-check,
a persona, consolidation) gets a dedicated adapter + MCP server + ToolContext stamped
with review provenance (origin_kind='review_session', origin_job_id=<review_run.id>,
review_run_item_id=<item.id>). Its ONLY tool is submit_review — no live reads, so it
can only judge the pinned instruction. The persona role lives in the INSTRUCTION, not
a system prompt (the adapter has no such parameter)."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def review_allowed_tools() -> list[str]:
    return ["submit_review"]


def build_review_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    review_run_id: uuid.UUID,
    item_id: uuid.UUID,
    kind: str,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="review_session",
            origin_job_id=review_run_id,
            review_run_item_id=item_id,
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=review_allowed_tools(),
    )
```
Note: confirm the exact allowlist string the SDK expects. `evaluator/agent.py` returns `["submit_scorecard"]` (bare name) and the adapter forwards it. Match that exactly — return `["submit_review"]` (bare). The `kind` param is accepted for symmetry/logging but the role is delivered via the instruction by the caller (`pass_runner`), not here.

- [ ] **Step 15: Run the submit + adapter tests**

Run: `uv run pytest tests/proposal_assist/review/test_submit_review.py tests/proposal_assist/review/test_personas.py tests/proposal_assist/review/test_context.py -v ; echo $?`
Expected: PASS. Also smoke-import the adapter: `uv run python -c "from call_manager.proposal_assist.review.agent import build_review_adapter; print('ok')" ; echo $?`.

- [ ] **Step 16: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel add \
  src/call_manager/proposal_assist/review/ \
  src/call_manager/proposal_assist/tools/review_submit.py \
  src/call_manager/proposal_assist/tools/registry.py \
  tests/proposal_assist/review/
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel -c commit.gpgsign=false commit -m "feat(review): personas, context, submit_review tool, one-shot adapter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Queue helpers, caps, pricing (`review/jobs.py`)

**Files:**
- Create: `src/call_manager/proposal_assist/review/jobs.py`
- Test: `tests/proposal_assist/review/test_jobs.py`

**Interfaces:**
- Consumes: `ReviewRun`, `Workspace`, `Settings`, `_stockholm_day_start_utc` (from `proposal_assist/turns/caps.py`).
- Produces (all flush, never commit — caller owns the txn):
  - `enqueue_review_run(session, *, workspace_id, draft_sha) -> ReviewRun`
  - `active_review_run(session, *, workspace_id) -> ReviewRun | None`
  - `claim_next_review_run(session) -> ReviewRun | None`
  - `cancel_review_runs_for_workspace(session, *, workspace_id) -> int`
  - `reap_stuck_review_runs(session, *, timeout_seconds) -> int`
  - `latest_review_run(session, *, workspace_id) -> ReviewRun | None`
  - `review_runs_today(session, *, workspace_id) -> int`
  - `review_dollars_today(session, *, workspace_id) -> Decimal`
  - `review_run_cap(ws, settings) -> int`; `review_dollar_cap(ws, settings) -> Decimal`
  - `estimate_dollars(tokens_in, tokens_out, settings) -> Decimal`

- [ ] **Step 1: Write the jobs test**

`tests/proposal_assist/review/test_jobs.py` (mirrors `tests/proposal_assist/evaluator/test_jobs.py`):
```python
from decimal import Decimal

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, Workspace
from call_manager.proposal_assist.review.jobs import (
    active_review_run,
    cancel_review_runs_for_workspace,
    claim_next_review_run,
    enqueue_review_run,
    estimate_dollars,
    reap_stuck_review_runs,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
)


def test_enqueue_create_or_return_active(review_workspace):
    session, ws_id, *_ = review_workspace
    r1 = enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    r2 = enqueue_review_run(session, workspace_id=ws_id, draft_sha="b" * 64)
    assert r1.id == r2.id  # returns the existing active run
    assert active_review_run(session, workspace_id=ws_id).id == r1.id


def test_claim_flips_to_running_and_excludes_running_ws(review_workspace):
    session, ws_id, *_ = review_workspace
    enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    claimed = claim_next_review_run(session)
    assert claimed is not None and claimed.status == "running"
    assert claim_next_review_run(session) is None  # ws already has a running run


def test_cancel_and_reap(review_workspace):
    session, ws_id, *_ = review_workspace
    enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    assert cancel_review_runs_for_workspace(session, workspace_id=ws_id) == 1
    # a running run older than TTL is reaped
    r = ReviewRun(workspace_id=ws_id, status="running", draft_sha="a" * 64)
    session.add(r)
    session.flush()
    # force triggered_at into the past
    session.execute(
        ReviewRun.__table__.update().where(ReviewRun.id == r.id).values(
            triggered_at=__import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").UTC)
        )
    )
    assert reap_stuck_review_runs(session, timeout_seconds=10) == 1


def test_caps_and_pricing(review_workspace):
    session, ws_id, *_ = review_workspace
    ws = session.get(Workspace, ws_id)
    s = Settings()
    assert review_run_cap(ws, s) == 5
    assert review_dollar_cap(ws, s) == Decimal("5.000")
    ws.review_runs_per_day_override = 2
    ws.review_dollar_cap_override = Decimal("1.500")
    session.flush()
    assert review_run_cap(ws, s) == 2 and review_dollar_cap(ws, s) == Decimal("1.500")
    assert review_runs_today(session, workspace_id=ws_id) == 0
    assert review_dollars_today(session, workspace_id=ws_id) == Decimal("0")
    # 1M in + 1M out => $5 + $25 = $30.00
    assert estimate_dollars(1_000_000, 1_000_000, s) == Decimal("30.0000")
    assert estimate_dollars(0, 0, s) == Decimal("0.0000")


def test_runs_today_excludes_cancelled(review_workspace):
    session, ws_id, *_ = review_workspace
    r = enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    assert review_runs_today(session, workspace_id=ws_id) == 1
    r.status = "cancelled"
    session.flush()
    assert review_runs_today(session, workspace_id=ws_id) == 0
```

- [ ] **Step 2: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_jobs.py -v ; echo $?`
Expected: FAIL.

- [ ] **Step 3: Create `review/jobs.py`**

`src/call_manager/proposal_assist/review/jobs.py`:
```python
"""Pure-DB helpers for the review_runs queue + the daily caps + token→dollar pricing.
The caller owns the transaction (these flush, never commit), mirroring
evaluator/jobs.py. Bespoke: no started_at column on ReviewRun (the reaper uses
triggered_at; queue latency is bounded by the 20s drain tick)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, Workspace
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def active_review_run(session: Session, *, workspace_id: uuid.UUID) -> ReviewRun | None:
    return session.scalar(
        select(ReviewRun).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.status.in_(("queued", "running")),
        )
    )


def enqueue_review_run(
    session: Session, *, workspace_id: uuid.UUID, draft_sha: str
) -> ReviewRun:
    """Create a queued run, or return the existing active (queued|running) one.
    Race-safe against uq_review_runs_active via a SAVEPOINT (same pattern as
    enqueue_evaluator_run)."""
    existing = active_review_run(session, workspace_id=workspace_id)
    if existing is not None:
        return existing
    run = ReviewRun(workspace_id=workspace_id, status="queued", draft_sha=draft_sha)
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        if run in session:
            session.expunge(run)
        winner = active_review_run(session, workspace_id=workspace_id)
        if winner is None:
            raise RuntimeError(
                "review enqueue lost the active-unique race but found no active run"
            ) from None
        return winner
    return run


def claim_next_review_run(session: Session) -> ReviewRun | None:
    """Claim the oldest queued run whose workspace is LIVE and has no running run.
    FOR UPDATE SKIP LOCKED. Soft-deleted workspaces excluded."""
    running_ws = select(ReviewRun.workspace_id).where(ReviewRun.status == "running")
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    run = session.scalar(
        select(ReviewRun)
        .where(
            ReviewRun.status == "queued",
            ReviewRun.workspace_id.not_in(running_ws),
            ReviewRun.workspace_id.in_(live_ws),
        )
        .order_by(ReviewRun.triggered_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        return None
    run.status = "running"
    session.flush()
    return run


def cancel_review_runs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Cancel any active (queued|running) review run for a workspace (soft-delete
    handler). ONE atomic UPDATE ... RETURNING — never an ORM load-then-write."""
    ids = (
        session.execute(
            update(ReviewRun)
            .where(
                ReviewRun.workspace_id == workspace_id,
                ReviewRun.status.in_(("queued", "running")),
            )
            .values(status="cancelled", completed_at=datetime.now(UTC))
            .returning(ReviewRun.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    return len(ids)


def reap_stuck_review_runs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running run whose triggered_at is older than
    the TTL (ReviewRun has no started_at — queue latency is bounded by the drain
    tick, so triggered_at ~= run start). Pure DB. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(ReviewRun)
        .where(ReviewRun.status == "running", ReviewRun.triggered_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for run in rows:
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def latest_review_run(session: Session, *, workspace_id: uuid.UUID) -> ReviewRun | None:
    return session.scalar(
        select(ReviewRun)
        .where(ReviewRun.workspace_id == workspace_id)
        .order_by(ReviewRun.triggered_at.desc())
        .limit(1)
    )


def review_runs_today(session: Session, *, workspace_id: uuid.UUID) -> int:
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(func.count())
        .select_from(ReviewRun)
        .where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.triggered_at >= day_start,
            ReviewRun.status != "cancelled",
        )
    )
    return int(total or 0)


def review_dollars_today(session: Session, *, workspace_id: uuid.UUID) -> Decimal:
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(func.coalesce(func.sum(ReviewRun.estimated_dollars), 0)).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.triggered_at >= day_start,
            ReviewRun.status != "cancelled",
        )
    )
    return Decimal(total or 0)


def review_run_cap(ws: Workspace, settings: Settings) -> int:
    if ws is not None and ws.review_runs_per_day_override is not None:
        return ws.review_runs_per_day_override
    return settings.review_runs_per_day_default


def review_dollar_cap(ws: Workspace, settings: Settings) -> Decimal:
    if ws is not None and ws.review_dollar_cap_override is not None:
        return ws.review_dollar_cap_override
    return settings.review_dollar_cap_default


def estimate_dollars(tokens_in: int, tokens_out: int, settings: Settings) -> Decimal:
    """Conservative cost estimate. The adapter bundles base+cache_creation+cache_read
    into tokens_in (contract.py exposes no separate cache-read), so price the whole
    tokens_in at the input rate. Quantize to the item column scale (4 dp)."""
    cost = (
        Decimal(int(tokens_in or 0)) / Decimal(1_000_000) * settings.review_price_input_per_mtok
        + Decimal(int(tokens_out or 0)) / Decimal(1_000_000) * settings.review_price_output_per_mtok
    )
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
```

- [ ] **Step 4: Run the jobs test; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_jobs.py -v ; echo $?`
Expected: PASS. (Connection-refused → re-run alone.)

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel add \
  src/call_manager/proposal_assist/review/jobs.py tests/proposal_assist/review/test_jobs.py
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel -c commit.gpgsign=false commit -m "feat(review): queue helpers, daily caps, token->dollar pricing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: The fan-out runner + scheduler ticks + soft-delete wiring

**Files:**
- Create: `src/call_manager/proposal_assist/review/pass_runner.py`
- Modify: `src/call_manager/scheduler.py` (two ticks, after the evaluator reaper at line 619)
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (soft-delete, after line 1329)
- Test: `tests/proposal_assist/review/test_pass_runner.py`, `test_scheduler_wiring.py`, `test_soft_delete_cancels_review.py`

**Interfaces:**
- Consumes: Task 2 (`assemble_review_context`, `persona_instruction`, `consolidation_instruction`, `build_review_adapter`, `WAVE1_KINDS`, `PERSONA_KINDS`, `REVIEW_KINDS`), Task 3 (jobs + `estimate_dollars`), `gitio.write_and_commit_files`/`blob_sha`, `drafting.contract.LaunchContext`/`TurnDoneEvent`, `paths.workspace_repo_dir`.
- Produces: `run_review_pass(*, run_id, session_factory, settings, adapter_builder=build_review_adapter) -> None`; `drain_review_queue_tick(*, session_factory, settings, adapter_builder=build_review_adapter) -> None`.

- [ ] **Step 1: Write the pass_runner test (with a fake adapter)**

`tests/proposal_assist/review/test_pass_runner.py`. The fake adapter simulates the model: on `send_turn`, for a "success" kind it writes `{"markdown": ...}` to the item (as the real `submit_review` tool would) and yields a `TurnDoneEvent`; for a "fail" kind it raises; for "timeout" it sleeps past the per-call timeout. The pass marks each item done/failed accordingly.
```python
import asyncio
import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, ReviewRunItem
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.review.jobs import enqueue_review_run
from call_manager.proposal_assist.review.pass_runner import run_review_pass


class _Handle:
    def __init__(self, ws_id):
        self.workspace_id = ws_id


def make_fake_builder(session_factory, behavior: dict[str, str]):
    """behavior[kind] in {'ok','fail','timeout'}."""

    def builder(*, settings, session_factory as sf, workspace_id, review_run_id, item_id, kind):  # noqa
        class _Fake:
            async def launch(self, ctx):
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                b = behavior.get(kind, "ok")
                if b == "timeout":
                    await asyncio.sleep(settings.review_persona_timeout_seconds + 5)
                if b == "fail":
                    raise RuntimeError("boom")
                with session_factory() as s:
                    item = s.get(ReviewRunItem, item_id)
                    item.raw_response_json = {"markdown": f"# {kind}\nok"}
                    s.commit()
                yield TurnDoneEvent(tokens_in=1000, tokens_out=500, model="claude-opus-4-7")

            async def terminate(self, handle):
                return None

        return _Fake()

    return builder
```
(NOTE: `def builder(..., session_factory as sf, ...)` is illegal Python — the implementer must write the fake builder accepting the real keyword args `settings, session_factory, workspace_id, review_run_id, item_id, kind`; capture the outer `session_factory` via closure under a different local name. The corrected skeleton:)
```python
def make_fake_builder(outer_session_factory, behavior):
    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind):
        class _Fake:
            async def launch(self, ctx):
                return _Handle(workspace_id)
            async def send_turn(self, handle, instruction):
                b = behavior.get(kind, "ok")
                if b == "timeout":
                    await asyncio.sleep(settings.review_persona_timeout_seconds + 5)
                if b == "fail":
                    raise RuntimeError("boom")
                with outer_session_factory() as s:
                    s.get(ReviewRunItem, item_id).raw_response_json = {"markdown": f"# {kind}\nok"}
                    s.commit()
                yield TurnDoneEvent(tokens_in=1000, tokens_out=500, model="claude-opus-4-7")
            async def terminate(self, handle):
                return None
        return _Fake()
    return builder


def _claim_running(session_factory, ws_id, draft_sha):
    with session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.commit()
        return run.id


@pytest.mark.asyncio
async def test_happy_path_all_done(review_active_workspace):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=settings,
        adapter_builder=make_fake_builder(session_factory, {}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "done"
        assert run.fit_check_sha and run.rubric_review_sha
        assert run.estimated_dollars and run.input_tokens == 6000
        items = s.query(ReviewRunItem).filter_by(review_run_id=run_id).all()
        assert len(items) == 6 and all(i.status == "done" for i in items)


@pytest.mark.asyncio
async def test_one_persona_fails_is_partial(review_active_workspace):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=Settings(),
        adapter_builder=make_fake_builder(session_factory, {"persona_domain_skeptic": "fail"}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "partial"
        assert run.rubric_review_sha  # consolidation still ran


@pytest.mark.asyncio
async def test_fit_check_fail_with_consolidation_is_partial(review_active_workspace):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=Settings(),
        adapter_builder=make_fake_builder(session_factory, {"fit_check": "fail"}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "partial"
        assert run.fit_check_sha is None and run.rubric_review_sha


@pytest.mark.asyncio
async def test_all_personas_fail_is_failed(review_active_workspace):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    behavior = {k: "fail" for k in (
        "persona_domain_skeptic", "persona_methodology_critic",
        "persona_translation_skeptic", "persona_devils_advocate")}
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=Settings(),
        adapter_builder=make_fake_builder(session_factory, behavior),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "failed" and run.rubric_review_sha is None


@pytest.mark.asyncio
async def test_subcall_skips_when_run_no_longer_running(review_active_workspace):
    # Codex High: a queued-behind-the-semaphore sub-call must NOT launch (spend
    # tokens / send workspace content) once the run has been cancelled/reaped.
    from call_manager.proposal_assist.review import pass_runner as pr

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    with session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="queued"))
        run_id = run.id
        s.commit()
    with session_factory() as s:  # cancel between the read session and the sub-call
        s.get(ReviewRun, run_id).status = "cancelled"
        s.commit()

    launched = {"v": False}

    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind):
        class _Fake:
            async def launch(self, ctx):
                launched["v"] = True
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")

            async def terminate(self, handle):
                return None

        return _Fake()

    await pr._run_one_call(
        kind="fit_check", instruction="x", run_id=run_id, workspace_id=ws_id,
        session_factory=session_factory, settings=Settings(), adapter_builder=builder,
    )
    assert launched["v"] is False
    with session_factory() as s:
        item = s.query(ReviewRunItem).filter_by(review_run_id=run_id, kind="fit_check").one()
        assert item.status == "cancelled"


@pytest.mark.asyncio
async def test_artifact_commit_failure_fails_run(review_active_workspace, monkeypatch):
    # Codex Medium: a git failure in finalize must fail the run immediately, not
    # leave it 'running' until the reaper TTL.
    from call_manager.proposal_assist.citations.gitio import GitCommitError
    from call_manager.proposal_assist.review import pass_runner as pr

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)

    def boom(*a, **k):
        raise GitCommitError("disk full")

    monkeypatch.setattr(pr, "write_and_commit_files", boom)
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=Settings(),
        adapter_builder=make_fake_builder(session_factory, {}),
    )
    with session_factory() as s:
        assert s.get(ReviewRun, run_id).status == "failed"
```
Add a `review_active_workspace` fixture to `conftest.py` yielding `(session_factory, ws_id, wdir, repo_path, draft_sha)` — an active workspace with an initialized repo containing committed `proposal.md`/`proposal-spec.md`; `draft_sha` is the sha256 of the proposal bytes.

- [ ] **Step 2: Run it; expect ImportError**

Run: `uv run pytest tests/proposal_assist/review/test_pass_runner.py -v ; echo $?`
Expected: FAIL.

- [ ] **Step 3: Create `review/pass_runner.py`**

`src/call_manager/proposal_assist/review/pass_runner.py`:
```python
"""Run one mock-panel review pass (the 6-call fan-out) for a claimed (running)
review_runs row, and the async drain tick. Bespoke (unlike the single-call
coherence/evaluator passes): Wave 1 = fit-check + 4 personas concurrently
(semaphore-bounded), Wave 2 = consolidation over the survivors. Each sub-call is
its own one-shot adapter with ONLY submit_review; the produced markdown is read
back from the item's raw_response_json. Partial-failure aware. Carries the
evaluator hardening: launch inside wait_for, bounded terminate, FOR UPDATE
finalize guard."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, ReviewRunItem, Workspace
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    write_and_commit_files,
)
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.agent import build_review_adapter
from call_manager.proposal_assist.review.context import (
    assemble_review_context,
    consolidation_instruction,
    persona_instruction,
)
from call_manager.proposal_assist.review.jobs import (
    claim_next_review_run,
    estimate_dollars,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
)
from call_manager.proposal_assist.review.personas import (
    PERSONA_KINDS,
    REVIEW_KINDS,
    WAVE1_KINDS,
)

logger = logging.getLogger(__name__)

_FIT_CHECK_FILE = "fit-check.md"
_RUBRIC_REVIEW_FILE = "rubric-review.md"


def _now() -> datetime:
    return datetime.now(UTC)


async def _run_one_call(
    *, kind, instruction, run_id, workspace_id, session_factory, settings, adapter_builder
) -> None:
    """Run a single sub-call against its (already 'running') item. Records tokens +
    dollars and flips the item to done/failed. Never raises — failure is recorded."""
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        item = s.scalar(
            select(ReviewRunItem).where(
                ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
            )
        )
        item_id = item.id
        # Pre-launch live recheck (mirrors evaluator/pass_runner.py:92): a soft-delete
        # cancel or reaper may have flipped the run between step-1's read session and
        # now — and with the wave-1 semaphore, a queued sub-call can START long after
        # wave-1 began. Don't spend tokens or send workspace content for a run that is
        # no longer running. Full prevention would need task cancellation; this narrows
        # the window the way the evaluator does.
        if run is None or run.status != "running":
            item.status = "cancelled"
            item.error_message = "run no longer active"
            item.completed_at = _now()
            s.commit()
            return
        item.status = "running"
        item.started_at = _now()
        item.model = settings.proposal_assist_llm_model
        s.commit()

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        review_run_id=run_id,
        item_id=item_id,
        kind=kind,
    )
    handle = None
    done: TurnDoneEvent | None = None
    error: str | None = None
    try:

        async def _drive() -> TurnDoneEvent | None:
            nonlocal handle
            handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(_drive(), timeout=settings.review_persona_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — terminal for THIS sub-call
        logger.exception("review sub-call %s failed for run %s", kind, run_id)
        error = (
            "timed out"
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else "sub-call failed"
        )
    finally:
        if handle is not None:
            try:
                await asyncio.wait_for(
                    adapter.terminate(handle),
                    timeout=settings.review_terminate_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("review adapter terminate failed (%s, run %s)", kind, run_id)

    with session_factory() as s:
        item = s.get(ReviewRunItem, item_id, with_for_update=True)
        if item is None:
            return
        item.completed_at = _now()
        if done is not None:
            item.input_tokens = done.tokens_in or 0
            item.output_tokens = done.tokens_out or 0
            item.estimated_dollars = estimate_dollars(
                done.tokens_in or 0, done.tokens_out or 0, settings
            )
        if error is None and item.raw_response_json and item.raw_response_json.get("markdown"):
            item.status = "done"
        else:
            item.status = "failed"
            item.error_message = error or "no review submitted"
        s.commit()


def _markdown_of(session, run_id, kind) -> str | None:
    item = session.scalar(
        select(ReviewRunItem).where(
            ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
        )
    )
    if item and item.status == "done" and item.raw_response_json:
        return item.raw_response_json.get("markdown")
    return None


async def run_review_pass(
    *,
    run_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_review_adapter,
) -> None:
    # 1. Short read session: guard running, pre-create the 6 items FIRST, pin context,
    #    set draft_sha, re-check caps.
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        if run is None or run.status != "running":
            return
        workspace_id = run.workspace_id
        ws = s.get(Workspace, workspace_id)
        existing = {
            i.kind for i in s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            )
        }
        for kind in REVIEW_KINDS:
            if kind not in existing:
                s.add(ReviewRunItem(review_run_id=run_id, kind=kind, status="queued"))
        ctx = assemble_review_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )
        run.draft_sha = ctx.draft_sha
        # pass-start cap recheck (a long queue wait may have pushed the ws over budget)
        over = (
            review_runs_today(s, workspace_id=workspace_id) > review_run_cap(ws, settings)
            or review_dollars_today(s, workspace_id=workspace_id) >= review_dollar_cap(ws, settings)
        )
        if over:
            for it in s.scalars(select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)):
                it.status = "skipped"
                if it.kind == "consolidation":
                    it.error_message = "daily mock-panel budget reached"
            run.status = "failed"
            run.completed_at = _now()
            s.commit()
            return
        s.commit()

    # 2. Wave 1 — fit-check + 4 personas concurrently (semaphore-bounded).
    sem = asyncio.Semaphore(settings.review_persona_concurrency)

    async def _guarded(kind: str, instruction: str) -> None:
        async with sem:
            await _run_one_call(
                kind=kind, instruction=instruction, run_id=run_id, workspace_id=workspace_id,
                session_factory=session_factory, settings=settings, adapter_builder=adapter_builder,
            )

    wave1 = [
        _guarded(kind, persona_instruction(kind, ctx)) for kind in WAVE1_KINDS
    ]
    await asyncio.gather(*wave1)  # each _run_one_call swallows its own failure

    # 3. Wave 2 — consolidation over whatever personas succeeded.
    with session_factory() as s:
        persona_md = {
            k: _markdown_of(s, run_id, k) for k in PERSONA_KINDS
        }
        persona_md = {k: v for k, v in persona_md.items() if v}
        fit_md = _markdown_of(s, run_id, "fit_check")
    if persona_md:
        cons_instr = consolidation_instruction(ctx, persona_md, fit_md)
        await _run_one_call(
            kind="consolidation", instruction=cons_instr, run_id=run_id,
            workspace_id=workspace_id, session_factory=session_factory, settings=settings,
            adapter_builder=adapter_builder,
        )
    else:
        with session_factory() as s:
            it = s.scalar(
                select(ReviewRunItem).where(
                    ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == "consolidation"
                )
            )
            it.status = "skipped"
            it.error_message = "all personas failed"
            it.completed_at = _now()
            s.commit()

    # 4. Write artifacts + finalize (row-locked, still-running guard).
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    with session_factory() as s:
        run = s.get(ReviewRun, run_id, with_for_update=True)
        if run is None or run.status != "running":
            s.commit()
            return
        items = {
            i.kind: i for i in s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            )
        }
        fit_md = _markdown_of(s, run_id, "fit_check")
        cons_md = _markdown_of(s, run_id, "consolidation")
        files: dict[str, str] = {}
        if fit_md:
            files[_FIT_CHECK_FILE] = fit_md
        if cons_md:
            files[_RUBRIC_REVIEW_FILE] = cons_md
        commit_failed = False
        if files:
            try:
                write_and_commit_files(repo_dir, files, f"mock-panel review {run_id}")
                if _FIT_CHECK_FILE in files:
                    run.fit_check_sha = blob_sha(repo_dir, "HEAD", _FIT_CHECK_FILE)
                if _RUBRIC_REVIEW_FILE in files:
                    run.rubric_review_sha = blob_sha(repo_dir, "HEAD", _RUBRIC_REVIEW_FILE)
            except GitCommitError:
                # Don't let a git failure escape the finalize and wedge the active
                # slot until the reaper TTL — fail the run NOW, surface it on the item.
                logger.exception("review artifact commit failed for run %s", run_id)
                commit_failed = True
                cons = items.get("consolidation")
                if cons is not None:
                    cons.error_message = (
                        (cons.error_message or "") + " [artifact commit failed]"
                    ).strip()
        run.input_tokens = sum((i.input_tokens or 0) for i in items.values())
        run.output_tokens = sum((i.output_tokens or 0) for i in items.values())
        dollars = sum((i.estimated_dollars or 0) for i in items.values())
        run.estimated_dollars = (dollars or None) and __import__("decimal").Decimal(
            dollars
        ).quantize(__import__("decimal").Decimal("0.001"))
        all_done = all(items[k].status == "done" for k in REVIEW_KINDS if k in items)
        cons_done = items.get("consolidation") and items["consolidation"].status == "done"
        if commit_failed:
            run.status = "failed"
        elif all_done:
            run.status = "done"
        elif cons_done:
            run.status = "partial"
        else:
            run.status = "failed"
        run.completed_at = _now()
        s.commit()


async def drain_review_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_review_adapter,
) -> None:
    with session_factory() as s:
        run = claim_next_review_run(s)
        s.commit()
        run_id = run.id if run is not None else None
    if run_id is None:
        return
    await run_review_pass(
        run_id=run_id, session_factory=session_factory, settings=settings,
        adapter_builder=adapter_builder,
    )
```
(Implementer cleanup: replace the inline `__import__("decimal").Decimal(...)` with a top-level `from decimal import Decimal` and `Decimal(dollars).quantize(Decimal("0.001"))`; the inline form above is only to keep this block import-list-honest — do it cleanly.)

- [ ] **Step 4: Run the pass_runner test; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_pass_runner.py -v ; echo $?`
Expected: PASS (4 tests). The timeout test path is exercised by `test_one_persona_fails_is_partial` variants — add a `{"persona_methodology_critic": "timeout"}` case if you want explicit coverage (the per-call `wait_for` makes it fail that one item; with `review_persona_timeout_seconds` defaulting to 180, set it low in the test via `Settings(review_persona_timeout_seconds=1)`).

- [ ] **Step 5: Add the scheduler ticks**

In `src/call_manager/scheduler.py`, after the evaluator reaper `add_job` (line 619), add the review drain + reaper ticks, mirroring the evaluator block exactly:
```python
    # SP2b-3c: review drain tick (async — one fresh-stateless mock-panel fan-out).
    def _review_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.review.pass_runner import drain_review_queue_tick

        if not settings.drafting_enabled:
            return
        coro = drain_review_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("review tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.review_job_timeout_seconds + 60)
        except Exception:
            _logger.exception("review tick failed")

    sched.add_job(
        _review_tick,
        trigger=IntervalTrigger(seconds=settings.review_tick_seconds),
        id="review_drain",
        max_instances=1,
        coalesce=True,
    )

    def _review_reaper_tick() -> None:
        from call_manager.proposal_assist.review.jobs import reap_stuck_review_runs

        try:
            with session_factory() as s:
                reap_stuck_review_runs(s, timeout_seconds=settings.review_job_timeout_seconds)
                s.commit()
        except Exception:
            _logger.exception("review reaper tick failed")

    sched.add_job(
        _review_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.review_reaper_tick_seconds),
        id="review_reaper",
        max_instances=1,
        coalesce=True,
    )
```
The drain future timeout uses `review_job_timeout_seconds + 60` (the whole fan-out, not a single sub-call) — note this differs from the evaluator, which uses its per-pass timeout; the mock-panel's pass is multi-call so the job timeout is the right bound.

- [ ] **Step 6: Write the scheduler-wiring test**

`tests/proposal_assist/review/test_scheduler_wiring.py` — mirror `tests/proposal_assist/evaluator/test_scheduler_wiring.py`, asserting jobs `review_drain` and `review_reaper` are registered after `build_scheduler(...)`.

- [ ] **Step 7: Wire the soft-delete cancel**

In `src/call_manager/proposal_assist/web/workspaces.py`: import `cancel_review_runs_for_workspace` near the other cancel imports (line ~59), and after line 1329 (`cancel_evaluator_jobs_for_workspace(...)`) add:
```python
    # SP2b-3c: same rationale for the review queue — cancel any active mock-panel run.
    cancel_review_runs_for_workspace(session, workspace_id=workspace_id)
```

- [ ] **Step 8: Write the soft-delete test**

`tests/proposal_assist/review/test_soft_delete_cancels_review.py` — mirror `test_soft_delete_cancels_evaluator.py`: enqueue a review run, soft-delete the workspace via the same code path the evaluator test uses, assert the run's status is `cancelled`.

- [ ] **Step 9: Run all Task-4 tests**

Run: `uv run pytest tests/proposal_assist/review/test_pass_runner.py tests/proposal_assist/review/test_scheduler_wiring.py tests/proposal_assist/review/test_soft_delete_cancels_review.py -v ; echo $?`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel add \
  src/call_manager/proposal_assist/review/pass_runner.py \
  src/call_manager/scheduler.py \
  src/call_manager/proposal_assist/web/workspaces.py \
  tests/proposal_assist/review/
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel -c commit.gpgsign=false commit -m "feat(review): fan-out pass runner, scheduler ticks, soft-delete cancel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Endpoints (`web/review.py`)

**Files:**
- Create: `src/call_manager/proposal_assist/web/review.py`
- Modify: `src/call_manager/main.py` (import + `include_router`)
- Test: `tests/proposal_assist/review/test_web_review.py`

**Interfaces:**
- Consumes: Task 1 gate, Task 3 jobs, `gitio.read_blob`, `evaluator.context.content_sha`, `_perms.require_member`, `workspaces._session_dep`.
- Produces: router with `POST /{workspace_id}/review-runs`, `GET /{workspace_id}/review-runs/latest`, `GET /{workspace_id}/review-runs/{run_id}/files/{name}`.

- [ ] **Step 1: Write the endpoint tests**

`tests/proposal_assist/review/test_web_review.py` — mirror `test_web_evaluator.py` (TestClient + an authed member + a disclosure-acked active workspace). Cases:
```python
# pseudocode outline — fill with the evaluator test's client/fixture machinery
def test_trigger_creates_run_and_event(client, member, active_ws): ...   # 200, WorkspaceEvent written
def test_repeat_trigger_returns_active_not_429(client, member, active_ws):
    # even with review_runs_per_day_default monkeypatched to 1
    r1 = client.post(url); r2 = client.post(url)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
def test_429_over_run_cap_when_no_active(client, member, active_ws): ...  # complete a run, cap=1 -> 429, no new row
def test_gate_403_without_disclosure(client, member_no_ack, active_ws): ...
def test_latest_serializes_items_budget_staleness(client, member, active_ws): ...
def test_files_endpoint_by_sha_and_404s(client, member, active_ws): ...
```

- [ ] **Step 2: Run it; expect ImportError/404**

Run: `uv run pytest tests/proposal_assist/review/test_web_review.py -v ; echo $?`
Expected: FAIL.

- [ ] **Step 3: Create `web/review.py`**

`src/call_manager/proposal_assist/web/review.py`:
```python
"""Member-gated on-demand mock-panel endpoints. POST enqueues (active-first, then
cap-check); GET latest is polled by the SPA; GET files/{name} returns a review file
at the run's stored blob SHA."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ReviewRun, ReviewRunItem, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import GitCommitError, read_blob
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.jobs import (
    active_review_run,
    enqueue_review_run,
    latest_review_run,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
)
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["review"])

_FILES = {"fit-check.md": "fit_check_sha", "rubric-review.md": "rubric_review_sha"}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _serialize(run: ReviewRun, items: list[ReviewRunItem], *, stale: bool, budget: dict) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "triggered_at": run.triggered_at.isoformat() if run.triggered_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "draft_sha": run.draft_sha,
        "fit_check_sha": run.fit_check_sha,
        "rubric_review_sha": run.rubric_review_sha,
        "estimated_dollars": str(run.estimated_dollars) if run.estimated_dollars is not None else None,
        "items": [
            {
                "kind": i.kind,
                "status": i.status,
                "error_message": i.error_message,
                "estimated_dollars": str(i.estimated_dollars) if i.estimated_dollars is not None else None,
            }
            for i in sorted(items, key=lambda x: x.kind)
        ],
        "stale": stale,
        "budget": budget,
    }


def _budget(session, ws, settings, *, workspace_id) -> dict:
    runs_cap = review_run_cap(ws, settings)
    dollars_cap = review_dollar_cap(ws, settings)
    return {
        "runs_used": review_runs_today(session, workspace_id=workspace_id),
        "runs_cap": runs_cap,
        "dollars_used": str(review_dollars_today(session, workspace_id=workspace_id)),
        "dollars_cap": str(dollars_cap),
    }


@router.post("/{workspace_id}/review-runs")
def start_review_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    gate_cloud_llm(ws=ws, request=request, session=session, user=user,
                   disclosure_action="running a mock-panel review")
    settings = request.app.state.settings
    # 1. active-first (create-or-return-active; no cap-check on the existing run)
    active = active_review_run(session, workspace_id=workspace_id)
    if active is None:
        # 2. cap-check only when creating a NEW run
        if review_runs_today(session, workspace_id=workspace_id) >= review_run_cap(ws, settings) or (
            review_dollars_today(session, workspace_id=workspace_id) >= review_dollar_cap(ws, settings)
        ):
            raise HTTPException(
                status_code=429,
                detail="Daily mock-panel review budget reached for this workspace.",
            )
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        draft_sha = content_sha(repo_dir / PROPOSAL_FILENAME)
        run = enqueue_review_run(session, workspace_id=workspace_id, draft_sha=draft_sha)
        session.add(WorkspaceEvent(
            workspace_id=workspace_id, kind="review_run_triggered", actor_id=user.id,
        ))
        session.commit()
    else:
        run = active
    items = session.query(ReviewRunItem).filter_by(review_run_id=run.id).all()
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    stale = run.draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME)
    return _serialize(run, items, stale=stale, budget=_budget(session, ws, settings, workspace_id=workspace_id))


@router.get("/{workspace_id}/review-runs/latest")
def latest_review(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    run = latest_review_run(session, workspace_id=workspace_id)
    if run is None:
        return None
    settings = request.app.state.settings
    items = session.query(ReviewRunItem).filter_by(review_run_id=run.id).all()
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    stale = run.draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME)
    return _serialize(run, items, stale=stale, budget=_budget(session, ws, settings, workspace_id=workspace_id))


@router.get("/{workspace_id}/review-runs/{run_id}/files/{name}")
def review_file(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    sha_attr = _FILES.get(name)
    if sha_attr is None:
        raise HTTPException(status_code=404, detail="unknown review file")
    run = session.get(ReviewRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="review run not found")
    sha = getattr(run, sha_attr)
    if not sha or not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="file not produced for this run")
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    try:
        markdown = read_blob(repo_dir, sha)
    except GitCommitError:
        raise HTTPException(status_code=404, detail="file content unavailable") from None
    return {"name": name, "sha": sha, "markdown": markdown}
```
Confirm `content_sha` is importable from `evaluator/context.py` (it is, per the spec). Confirm `WorkspaceEvent` has `kind`/`actor_id`/`workspace_id` columns and no other required fields (the gate reads `kind="disclosure_acknowledged"`, `actor_id`; mirror how disclosure-ack events are written elsewhere — `grep -rn "disclosure_acknowledged" src/call_manager/proposal_assist/web/` to copy the exact `WorkspaceEvent(...)` construction, including any required `payload`/`created_at`).

- [ ] **Step 4: Mount the router**

In `src/call_manager/main.py`: add the import next to the evaluator import (line ~40):
```python
from call_manager.proposal_assist.web.review import router as review_router
```
and after `app.include_router(evaluator_router)` (line 541):
```python
    app.include_router(review_router)
```

- [ ] **Step 5: Run the endpoint tests; expect PASS**

Run: `uv run pytest tests/proposal_assist/review/test_web_review.py -v ; echo $?`
Expected: PASS. (Connection-refused → re-run alone.)

- [ ] **Step 6: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel add \
  src/call_manager/proposal_assist/web/review.py src/call_manager/main.py \
  tests/proposal_assist/review/test_web_review.py
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel -c commit.gpgsign=false commit -m "feat(review): trigger/latest/files endpoints (active-first cap order)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — "Panel review" tab

**Files:**
- Modify: regenerate `frontend/src/api/schema.gen.ts`
- Create: `frontend/src/api/review.ts`, `frontend/src/cockpit/ReviewPanel.tsx`
- Modify: `frontend/src/routes/Cockpit.tsx` (add the `top_tab` + nav entry + render)
- Test: `frontend/src/cockpit/ReviewPanel.test.tsx`

**Interfaces:**
- Consumes: the OpenAPI types for the three endpoints (after regeneration); the existing cockpit `top_tab` nav machinery (mirror how `ScorecardPanel`/the Scorecard tab is wired in `Cockpit.tsx`).

- [ ] **Step 1: Regenerate the API client**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel/frontend
pnpm install
EMBEDDING_MODEL=bge-m3 pnpm gen:api
git -C .. diff --stat frontend/src/api/schema.gen.ts   # should show the new /review-runs paths
```
Verify the file is not corrupted (no leading WARNING line). The backend server must be importable for `gen:api`; if it shells out to the app, run it with `EMBEDDING_MODEL=bge-m3` so the mxbai stdout WARNING never appears.

- [ ] **Step 2: Create `api/review.ts`**

`frontend/src/api/review.ts` — mirror `api/evaluator.ts` (the `openapi-fetch` client + typed wrappers). Expose:
```ts
// startReviewRun(workspaceId): POST /review-runs
// fetchLatestReviewRun(workspaceId): GET /review-runs/latest -> ReviewRunDTO | null
// fetchReviewFile(workspaceId, runId, name): GET .../files/{name} -> { name, sha, markdown }
// Types come from schema.gen.ts (components["schemas"][...] or the path response types).
```
Use the exact `openapi-fetch` call shape `api/evaluator.ts` uses (same `client`, same error handling).

- [ ] **Step 3: Create `ReviewPanel.tsx` + its test (TDD)**

Write `frontend/src/cockpit/ReviewPanel.test.tsx` first (vitest + React Testing Library, mirroring `ScorecardPanel`'s test): render with a mocked `useReviewRun` returning (a) gated/no-run state → button disabled with reason; (b) a `running` run → per-item progress rows; (c) a `partial` run → banner + both docs; (d) `stale` → badge. Then implement `frontend/src/cockpit/ReviewPanel.tsx`:
- a `useReviewRun(workspaceId)` hook: TanStack Query `useQuery` on `fetchLatestReviewRun`, `refetchInterval: 4000` while `status` is `queued|running`, else `false`; a `useMutation` on `startReviewRun` that invalidates the latest query.
- the panel: a "Run full mock-panel review" button (disabled + reason when `budget.runs_used >= budget.runs_cap`, or when a run is active, or when the workspace is in setup/drafting-disabled — surface the POST's 409/503/403/429 detail as the disabled reason); a budget line; per-item progress (the six kinds with status pills); a `partial`/`failed` banner; the two markdown docs fetched via `fetchReviewFile` and rendered with the existing markdown renderer used by other panels; a staleness badge when `stale`; cost + timestamps.

- [ ] **Step 4: Wire the tab into `Cockpit.tsx`**

Add `"review"` to the `top_tab` union/set, a nav button labeled "Panel review" next to "Scorecard", and render `<ReviewPanel workspaceId={...} />` when `top_tab === "review"`. Mirror exactly how the Scorecard tab is added (find `ScorecardPanel` usage in `Cockpit.tsx`).

- [ ] **Step 5: Type-check + test**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel/frontend
pnpm exec tsc --noEmit ; echo $?          # MUST be 0 — this is the gate
pnpm exec vitest run src/cockpit/ReviewPanel.test.tsx ; echo $?
```
Expected: tsc exit 0; vitest PASS.

- [ ] **Step 6: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel add frontend/
git -C /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel -c commit.gpgsign=false commit -m "feat(review): Panel-review cockpit tab + API client

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (run before declaring done)

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/sp2b-3c-mock-panel
git branch --show-current     # feat/sp2b-3c-mock-panel
uv run pytest tests/proposal_assist/review/ -v ; echo $?
# re-run any connection-refused file in isolation; capture exit codes
uv run pytest tests/proposal_assist/coherence/ tests/proposal_assist/evaluator/ -v ; echo $?  # siblings unbroken
cd frontend && pnpm exec tsc --noEmit ; echo $? && pnpm exec vitest run ; echo $?
cd .. && git log --oneline master..HEAD    # 6 task commits
```
Confirm: alembic head still `0054` (`uv run alembic heads`); no edits under the main checkout; `drafting/claude_adapter.py` + `drafting/contract.py` untouched (`git diff --stat master..HEAD` lists neither).

---

## Self-Review (completed by plan author)

- **Spec coverage:** §1 data model → Tasks 3/4 (reuse, no migration). §2 personas → Task 2. §3.1–3.6 → Tasks 2 (context/agent/submit) + 3 (jobs) + 4 (pass_runner). §4 gate → Task 1. §5 endpoints/scheduler/config/git-helpers → Tasks 5 (endpoints) + 4 (scheduler) + 1 (config/gitio). §6 artifacts → Task 4 (write rule). §7 frontend → Task 6. §8 tests → every task's test step + final verification. No spec section is unmapped. **Pricing note:** `estimate_dollars` prices `tokens_in + tokens_out` only — this is the conservative deviation the spec itself documents in §3.5 (the adapter bundles cache-read into `tokens_in`; the contract exposes no separate cache-read telemetry, and we deliberately do not change `drafting/contract.py`). The plan matches the *revised* spec, not the original cache-read sketch.
- **Placeholder scan:** every code step has complete code; the one intentionally-illegal fake-builder snippet is immediately followed by the corrected version with an explicit callout. The `__import__("decimal")` inline in pass_runner is flagged for cleanup to a top-level import.
- **Type consistency:** `ReviewContext` fields, `persona_instruction`/`consolidation_instruction` signatures, `estimate_dollars(tokens_in, tokens_out, settings)`, `build_review_adapter(...)` kwargs, and `_serialize`/budget keys are used identically across Tasks 2→4→5. `review_run_item_id` is defined in Task 2 and consumed by the submit tool + adapter.
- **Known executor cross-checks (called out inline, not gaps):** the exact MCP tool-registration call for `submit_review` (mirror `submit_scorecard`); importability of `evaluator/context.py` helpers (`_read_artifact`/`_render_*`/`content_sha`) — copy bodies if cross-import is disallowed; the exact `WorkspaceEvent(...)` constructor fields (grep an existing disclosure-ack write); the Scorecard-tab wiring in `Cockpit.tsx` to mirror.
