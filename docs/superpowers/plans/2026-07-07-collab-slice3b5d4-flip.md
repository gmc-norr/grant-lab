# Collab Slice 3b-5d·D4 — THE FLIP: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md` (read it first — §2 is the verified landscape, §3 the eight components, §10 the Codex amendments that shaped Tasks 4 and 7).

**Goal:** Turn real-time collab editing ON in production: a lazy enable-on-read seam, a kill-switch with a drain-then-truncate handover, severance/routing of every remaining git-first writer, room-canonical reads, the prod CSP WS allowance, and the Tiptap formatting floor — proven by the two-browser e2e in real `SPA_MODE=prod`.

**Architecture:** One new module (`collab/flip.py`) is the only prod caller of `enable_collab`, invoked from the two artifact read models. `COLLAB_KILL_SWITCH` is honored inside `is_collab_enabled` (module-level flag armed in `create_app`), with a startup handover pass (`collab/handover.py`) that checkpoints and then truncates every Yjs log so git becomes the sole canonical store while killed. Writers follow the established `_route()` fork: flag-on → kernel/room path, flag-off → byte-for-byte legacy.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x sync + testcontainers PG / pycrdt + the existing collab kernel / React 19 + Tiptap / Playwright two-browser e2e.

## Global Constraints

- **This slice ENDS flag-off parity as the merge bar** — post-merge, flag-ON is the prod path. Flag-off branches remain (kill-switch world) and every existing parity test must still pass **unchanged**; Tasks 5, 6, and 8 explicitly gate on that.
- **No migration, no schema change** (reuses `CollabArtifactState`; kill-switch is env-only). Backend + one frontend task + scripts.
- No new locks; no lock-ordering changes (routing branches read the flag outside locks; kernel paths carry their own established locking).
- Work in an isolated worktree: `.worktrees/collab-slice3b5d4`, branch `collab-slice3b5d4` off `master` (create via superpowers:using-git-worktrees at execution time). Subagents: always `git -C <abs-worktree-path>` and verify the branch before committing.
- **Explicit-path `git add` only** (never `-A`/`.`). Merges are **local `--no-ff` only**, not pushed. Do NOT invoke `/code-review` / `/ultrareview`.
- Commit trailer (last line of every commit body): `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Pytest: always `EMBEDDING_MODEL=bge-m3` and `-p no:cacheprovider`; **never whole-suite** (`connection refused` = testcontainers infra, re-run the file isolated; `UndefinedTable` = migration-ordering, report it).
- Python via `uv run` from the worktree root; frontend via `pnpm` from `frontend/`.
- OpenAPI regen (Task 9): from `frontend/`, `EMBEDDING_MODEL=bge-m3 pnpm run gen:api` (the env var is load-bearing — the mxbai default prints a WARNING that breaks openapi-typescript).
- The kill-switch module global MUST be reset between tests (autouse fixture in every test file that flips it) — a leaked `True` poisons the whole collab suite.

## File Structure

| File | Role |
|---|---|
| `src/call_manager/config.py` | Modify — `collab_kill_switch` setting (Task 1) |
| `src/call_manager/proposal_assist/collab/state.py` | Modify — kill-switch global + honor in `is_collab_enabled` + `reset_checkpoint` (Tasks 1, 4) |
| `src/call_manager/main.py` | Modify — arm kill-switch in `create_app`; lifespan handover call; CSP ws_origin derivation (Tasks 1, 4, 10) |
| `src/call_manager/proposal_assist/collab/flip.py` | Create — `has_pipe_tables` + `maybe_auto_enable` (Task 2) |
| `scripts/collab_table_audit.py` | Create — one-shot audit (Task 2) |
| `src/call_manager/web/api/workspaces.py` | Modify — read-model hooks + `get_spec` canonical (Task 3); save-route 409 guards (Task 5); `post_init_spec` routing (Task 6); `AddCitationOut.citekey` (Task 9) |
| `src/call_manager/proposal_assist/collab/store.py` | Modify — `find_artifacts_with_updates` + `truncate_log` (Task 4) |
| `src/call_manager/proposal_assist/collab/handover.py` | Create — `run_kill_switch_handover` (Task 4) |
| `src/call_manager/scheduler.py` | Modify — scan tick branches to handover under kill-switch (Task 4) |
| `src/call_manager/proposal_assist/collab/kernel.py` | Modify — `kernel_init_spec` (Task 6) |
| `src/call_manager/proposal_assist/citations/collab_cite.py` | Modify — `init_spec_via_kernel` + `insert_annotation_via_kernel` wrappers (Tasks 6, 8) |
| `src/call_manager/web/api/annotations.py` | Modify — canonical read (Task 7); insert routing (Task 8) |
| `src/call_manager/proposal_assist/analysis/insert.py` | Modify — extract `derive_annotation_cite` (Task 8) |
| `src/call_manager/web/middleware.py` | Modify — `build_csp(dev, ws_origin)` (Task 10) |
| `frontend/src/collab/schema.ts` | Modify — shortcuts + input rules (Task 11) |
| `scripts/collab_e2e_seed.py` + `scripts/collab_e2e_run.sh` + `frontend/e2e/collab.spec.ts` | Modify — prod-mode parameterization (Task 12) |

Tests: new files under `tests/proposal_assist/collab/` (`test_collab_kill_switch.py`, `test_collab_flip.py`, `test_collab_flip_readmodel.py`, `test_collab_kill_switch_handover.py`, `test_collab_save_severance.py`, `test_collab_init_spec_kernel.py`, `test_collab_annotations_canonical.py`, `test_collab_annotation_insert_kernel.py`) + modifications to `tests/test_security_headers.py` + `frontend/src/collab/schema.formatting.test.tsx`.

---

### Task 1: Kill-switch setting + `is_collab_enabled` choke point

**Files:**
- Modify: `src/call_manager/config.py` (after `collab_checkpoint_min_tail`, ~line 238)
- Modify: `src/call_manager/proposal_assist/collab/state.py`
- Modify: `src/call_manager/main.py::create_app` (right after `settings = Settings()`, ~line 272)
- Test: `tests/proposal_assist/collab/test_collab_kill_switch.py` (new)

**Interfaces:**
- Produces: `Settings.collab_kill_switch: bool` (env `COLLAB_KILL_SWITCH`, default False); `state.set_kill_switch(value: bool) -> None`; `state.kill_switch_engaged() -> bool`; `is_collab_enabled` returns False whenever the switch is armed (signature unchanged — every existing gate inherits the behavior with zero call-site edits). Later tasks rely on exactly these names.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_kill_switch.py`:

```python
"""3b-5d.D4: COLLAB_KILL_SWITCH masks the collab flag at the single choke point."""

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    """The switch is a module global — a leaked True poisons every collab test."""
    yield
    state.set_kill_switch(False)


def test_settings_parse_env_alias(monkeypatch):
    monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")
    assert Settings().collab_kill_switch is True
    monkeypatch.delenv("COLLAB_KILL_SWITCH")
    assert Settings().collab_kill_switch is False


def test_kill_switch_masks_enabled_flag(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="ks@ki.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        state.set_kill_switch(True)
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
        state.set_kill_switch(False)
        # enabled_at row untouched — the switch masks, never clears (sticky-forward)
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True


def test_enable_collab_still_writes_under_kill_switch_mask(pg_session_factory):
    """enable_collab itself is NOT gated (the handover/scan need DB truth);
    only the is_collab_enabled VIEW is masked."""
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="ks2@ki.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    state.set_kill_switch(False)
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_kill_switch.py -p no:cacheprovider -q`
Expected: collection error — `AttributeError`/`ImportError` on `state.set_kill_switch` (and `Settings` has no `collab_kill_switch` field once that's reached).

- [ ] **Step 3: Implement**

(a) `src/call_manager/config.py`, immediately after the `collab_checkpoint_min_tail` field (~line 238):

```python
    collab_kill_switch: bool = Field(
        default=False,
        alias="COLLAB_KILL_SWITCH",
        description=(
            "Emergency brake (3b-5d.D4): when true, is_collab_enabled reports "
            "False everywhere (WS refuses, writers take git-first branches, "
            "reads fall back to git, auto-enable pauses) and startup runs the "
            "drain-then-truncate handover pass, making git the sole canonical "
            "store. Engage by setting the env and RESTARTING — the restart is "
            "the socket drain. enabled_at rows are never cleared."
        ),
    )
```

(b) `src/call_manager/proposal_assist/collab/state.py` — add after the imports, and extend `is_collab_enabled`:

```python
# 3b-5d.D4 kill-switch: a process-wide override consulted by is_collab_enabled.
# Armed once at app creation from Settings.collab_kill_switch (create_app), and
# by tests via set_kill_switch. Module-level ON PURPOSE (not threaded through
# call sites): every gate in the tree already consults is_collab_enabled, and
# the switch is an emergency, restart-scoped toggle — not per-request state.
_KILL_SWITCH = False


def set_kill_switch(value: bool) -> None:
    global _KILL_SWITCH
    _KILL_SWITCH = bool(value)


def kill_switch_engaged() -> bool:
    return _KILL_SWITCH
```

```python
def is_collab_enabled(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> bool:
    if _KILL_SWITCH:
        return False
    row = _get(session, workspace_id, artifact)
    return bool(row and row.enabled_at is not None)
```

(c) `src/call_manager/main.py::create_app` — right after `settings = Settings()` (~line 272; add the import next to the other `call_manager` imports at the top of the file):

```python
from call_manager.proposal_assist.collab import state as collab_state
```

```python
    # 3b-5d.D4: arm the collab kill-switch BEFORE any request can consult the
    # flag. Module-level on purpose — see collab/state.py.
    collab_state.set_kill_switch(settings.collab_kill_switch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_kill_switch.py -p no:cacheprovider -q`
Expected: 4 passed. Then regression: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab -p no:cacheprovider -q` — all pass (the switch defaults False; nothing changes).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/config.py src/call_manager/proposal_assist/collab/state.py src/call_manager/main.py tests/proposal_assist/collab/test_collab_kill_switch.py
git add src/call_manager/config.py src/call_manager/proposal_assist/collab/state.py src/call_manager/main.py tests/proposal_assist/collab/test_collab_kill_switch.py
git commit -m "$(cat <<'EOF'
feat(collab): COLLAB_KILL_SWITCH masks is_collab_enabled at the choke point (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `collab/flip.py` (enable-on-touch seam) + table audit script

**Files:**
- Create: `src/call_manager/proposal_assist/collab/flip.py`
- Create: `scripts/collab_table_audit.py`
- Test: `tests/proposal_assist/collab/test_collab_flip.py` (new)

**Interfaces:**
- Consumes: `state.kill_switch_engaged()`, `state.is_collab_enabled`, `state.enable_collab` (Task 1 + existing).
- Produces: `has_pipe_tables(md: str) -> bool` and `maybe_auto_enable(session, *, workspace_id: uuid.UUID, artifact: str, head_md: str) -> bool` in `collab/flip.py` — Task 3 calls `maybe_auto_enable` from the read models; the audit script and the hold-back share `has_pipe_tables` (one definition).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_flip.py`:

```python
"""3b-5d.D4: enable-on-touch seam — sticky lazy enable with table hold-back."""

import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.flip import has_pipe_tables, maybe_auto_enable
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


# --- has_pipe_tables ------------------------------------------------------ #

def test_detects_plain_pipe_table():
    assert has_pipe_tables("## T\n| a | b |\n|---|---|\n| 1 | 2 |\n") is True

def test_detects_leading_pipe_free_row():
    assert has_pipe_tables("intro\na | b | c\n") is True

def test_fenced_code_is_excluded():
    md = "## T\n```\n| a | b |\n|---|---|\n```\nprose.\n"
    assert has_pipe_tables(md) is False

def test_plain_prose_and_single_pipe_are_clean():
    assert has_pipe_tables("## T\neither/or, no tables here.\n") is False
    assert has_pipe_tables("a single | pipe is not a table row\n") is False


# --- maybe_auto_enable ----------------------------------------------------- #

def _ws(s, email):
    return make_workspace(s, owner=make_user(s, email=email))


def test_enables_once_sticky_and_commits(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip1@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n") is True
    with pg_session_factory() as s:  # committed by the hook — visible to a NEW session
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        # sticky repeat: no error, still True
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n") is True


def test_hold_back_on_pipe_tables(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip2@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        md = "## T\n| a | b |\n|---|---|\n"
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md=md) is False
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_kill_switch_suppresses(pg_session_factory):
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        wid = _ws(s, "flip3@ki.se").id
        s.commit()
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n") is False
    state.set_kill_switch(False)
    with pg_session_factory() as s:  # nothing was persisted while suppressed
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_per_artifact_independence(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip4@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        assert maybe_auto_enable(s, workspace_id=wid, artifact="spec", head_md="## S\ny.\n") is True
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_concurrent_first_read_race_reports_enabled(pg_session_factory, monkeypatch):
    """The loser of the insert race gets IntegrityError on commit -> rollback ->
    report True (the other request enabled it)."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip5@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        real_commit = s.commit
        calls = {"n": 0}

        def _racy_commit():
            if calls["n"] == 0:
                calls["n"] += 1
                raise IntegrityError("dup", None, Exception("pk"))
            real_commit()

        monkeypatch.setattr(s, "commit", _racy_commit)
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip.py -p no:cacheprovider -q`
Expected: collection error — `ModuleNotFoundError: call_manager.proposal_assist.collab.flip`.

- [ ] **Step 3: Implement `collab/flip.py`**

```python
"""Enable-on-touch flip seam (3b-5d.D4): the ONLY prod caller of enable_collab.

maybe_auto_enable is invoked from the draft/spec read models — the first read
of an artifact (kill-switch off, no pipe tables at HEAD) enables collab for it,
stickily and per-artifact. New workspaces converge on their first cockpit load;
existing ones on their next open. Remove the hold-back once
scripts/collab_table_audit.py reports no affected artifacts."""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.proposal_assist.collab import state

logger = logging.getLogger(__name__)

# A pipe-table-shaped row: a line with >=2 pipes separating cell-ish content
# (covers header, |---| delimiter and body rows, with or without a leading
# pipe). Conservative ON PURPOSE: a false positive only DELAYS enablement
# (the artifact stays on the CM6 editor); a false negative flattens a table
# on the first room round-trip. Fenced code blocks are excluded by the scanner.
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")


def has_pipe_tables(md: str) -> bool:
    in_fence = False
    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(line):
            return True
    return False


def maybe_auto_enable(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, head_md: str
) -> bool:
    """Sticky lazy flip. Returns the artifact's EFFECTIVE enabled state.

    Enables iff: kill-switch off AND not already enabled AND head_md has no
    pipe tables (hold-back — the CommonMark bridge would flatten them).
    COMMITS the enable itself (the read models otherwise never write; the
    session factory is expire_on_commit=False so callers' loaded rows survive).
    The concurrent-first-read insert race resolves via IntegrityError → the
    other request won → report enabled."""
    if state.kill_switch_engaged():
        return False
    if state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return True
    if has_pipe_tables(head_md):
        logger.info(
            "collab.flip_held_back",
            extra={
                "event": "collab_flip_held_back",
                "workspace_id": str(workspace_id),
                "artifact": artifact,
            },
        )
        return False
    state.enable_collab(session, workspace_id=workspace_id, artifact=artifact)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    logger.info(
        "collab.flip_enabled",
        extra={
            "event": "collab_flip_enabled",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip.py -p no:cacheprovider -q`
Expected: 9 passed.

- [ ] **Step 5: Write the audit script**

Create `scripts/collab_table_audit.py` (mirror the engine/session imports from `main.py` — verify the exact module paths there before writing):

```python
#!/usr/bin/env python
"""One-shot pipe-table audit (3b-5d.D4 §3.8): list live workspaces whose HEAD
draft/spec contains pipe tables — these artifacts are HELD BACK from the
enable-on-read flip until cleaned up manually. Read-only.

Run from the repo root:  uv run python scripts/collab_table_audit.py
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import Workspace
from call_manager.main import make_engine, make_session_factory  # reuse main's factory imports
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.collab.flip import has_pipe_tables
from call_manager.proposal_assist.paths import workspace_repo_dir


def main() -> None:
    settings = Settings()
    sf = make_session_factory(make_engine(settings.database_url))
    hits = 0
    with sf() as s:
        rows = s.execute(
            select(Workspace.id, Workspace.title).where(Workspace.soft_deleted_at.is_(None))
        ).all()
    for ws_id, title in rows:
        repo = workspace_repo_dir(Path(settings.workspaces_dir), ws_id)
        for artifact, fname in (("draft", PROPOSAL_FILENAME), ("spec", SPEC_FILENAME)):
            try:
                md = (repo / fname).read_text(encoding="utf-8")
            except OSError:
                continue
            if has_pipe_tables(md):
                hits += 1
                print(f"{ws_id}\t{artifact}\t{title}")
    print(
        f"\n{hits} held-back artifact(s)."
        if hits
        else "clean — no pipe tables; the hold-back is removable."
    )


if __name__ == "__main__":
    main()
```

NOTE for the implementer: `make_engine` / `make_session_factory` — copy the import line `main.py` itself uses (they may live in `call_manager.db.engine` or similar); the script must import from the same place, not from `main` if `main` merely re-imports. Adjust the import to match reality, keep everything else verbatim. Sanity-run: `uv run python -c "import scripts.collab_table_audit"` is NOT possible (scripts/ is not a package) — instead verify with `uv run python scripts/collab_table_audit.py --help 2>&1 | head -1` against a running dev DB or accept an import-only smoke via `uv run python -c "import ast; ast.parse(open('scripts/collab_table_audit.py').read())"`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/flip.py scripts/collab_table_audit.py tests/proposal_assist/collab/test_collab_flip.py
git add src/call_manager/proposal_assist/collab/flip.py scripts/collab_table_audit.py tests/proposal_assist/collab/test_collab_flip.py
git commit -m "$(cat <<'EOF'
feat(collab): enable-on-touch flip seam + pipe-table hold-back and audit (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Read-model wiring — `get_draft`/`get_spec` auto-enable + `get_spec` room-canonical

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py::get_draft` (~lines 272-281) and `::get_spec` (~lines 305-328)
- Test: `tests/proposal_assist/collab/test_collab_flip_readmodel.py` (new)

**Interfaces:**
- Consumes: `maybe_auto_enable` (Task 2); existing `canonical_artifact_markdown(session, *, workspace_id, artifact, repo_dir) -> str`.
- Produces: the enable-on-read behavior every later task assumes — the FIRST authorized `GET /api/workspaces/{id}/draft` (or `/spec`) flips that artifact and returns `collab_enabled: true` in the same response; `get_spec` sections now derive from the canonical projection when enabled.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_flip_readmodel.py`. Fixture pattern mirrors `tests/proposal_assist/test_api_annotations_insert.py` (`client_authed_owner, current_user, pg_session_factory, tmp_path, _app` + `_app.state.settings.workspaces_dir = str(tmp_path)`); `_init_repo` comes from `tests.test_analysis_pipeline` (takes `(tmp_path, ws_id, doc)` and git-inits with the doc as `proposal.md`). For the spec artifact, write `proposal-spec.md` into the same repo and `git add/commit` it (subprocess, mirroring `tests/proposal_assist/collab/test_collab_checkpoint.py::_init_repo`).

```python
"""3b-5d.D4: the first artifact read flips collab on (enable-on-touch)."""

import subprocess

import pytest

from call_manager.db.models import CollabArtifactState
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"
TABLE_DOC = "# T\nintro\n## Body\n| a | b |\n|---|---|\n"


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _seed(s, tmp_path, owner, doc=DOC, *, with_spec=False):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, doc)
    if with_spec:
        (repo / "proposal-spec.md").write_text("## Approach\nspec body.\n")
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "spec"], cwd=repo, check=True)
    return ws, repo


def _flag_row(s, wid, artifact):
    return s.get(CollabArtifactState, (wid, artifact))


def test_first_draft_read_flips_and_reports_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is True  # SAME response reflects the flip
    with pg_session_factory() as s:
        row = _flag_row(s, wid, "draft")
        assert row is not None and row.enabled_at is not None
        assert _flag_row(s, wid, "spec") is None  # per-artifact: spec untouched


def test_spec_read_flips_spec_independently(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is True
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "spec").enabled_at is not None


def test_table_doc_is_held_back(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, doc=TABLE_DOC)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is False
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft") is None


def test_kill_switch_suppresses_flip(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is False
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft") is None


def test_repoless_workspace_reads_404_without_flipping(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # NO repo
        s.commit()
        wid = ws.id
    assert client_authed_owner.get(f"/api/workspaces/{wid}/draft").status_code == 404
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft") is None


def test_get_spec_reads_canonical_room_content(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Once enabled, spec sections derive from the LIVE room, not disk."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, with_spec=True)
        s.commit()
        wid = ws.id
    assert client_authed_owner.get(f"/api/workspaces/{wid}/spec").status_code == 200  # flips
    doc = pm_json_to_ydoc(markdown_to_pm("## Approach\n\nlive room rewrite.\n"))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="spec", actor_id=current_user.id,
            update=doc.get_update(),
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{wid}/spec").json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "live room rewrite." in joined          # canonical, not disk
    assert joined == serialize_doc_to_markdown(doc)  # exactly the room serialization
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip_readmodel.py -p no:cacheprovider -q`
Expected: FAIL — `collab_enabled` is False on first reads (no hook yet) and the canonical-spec test sees disk content.

- [ ] **Step 3: Wire the hook into both read models**

In `src/call_manager/web/api/workspaces.py` add the import (top of file, with the other collab imports): `from call_manager.proposal_assist.collab.flip import maybe_auto_enable`.

`get_draft` — replace lines ~272-280 (`collab_enabled = collab_state.is_collab_enabled(...)` through the `raw = proposal.read_text()` else-branch) with:

```python
        raw_disk = proposal.read_text()
        # 3b-5d.D4 enable-on-touch: the first read of a table-free artifact
        # flips collab on (sticky, per-artifact, kill-switch-suppressed) and
        # THIS response already reports collab_enabled=true.
        collab_enabled = maybe_auto_enable(
            session, workspace_id=workspace_id, artifact="draft", head_md=raw_disk
        )
        if collab_enabled:
            raw = canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
            )
        else:
            raw = raw_disk
```

`get_spec` — replace line ~318 (`sections = _sections_out(spec.read_text())`) with:

```python
        raw_disk = spec.read_text()
        collab_enabled = maybe_auto_enable(
            session, workspace_id=workspace_id, artifact="spec", head_md=raw_disk
        )
        raw = (
            canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact="spec", repo_dir=repo_dir
            )
            if collab_enabled
            else raw_disk
        )
        sections = _sections_out(raw)
```

…and DELETE the now-duplicate `collab_enabled = collab_state.is_collab_enabled(...)` block further down in `get_spec` (~lines 326-328). `canonical_artifact_markdown` is already imported for `get_draft` — verify, don't re-import. Everything else in both handlers stays byte-identical (`head` still reports git HEAD in both — the head field is the git anchor, canonical is the content projection; this matches `get_draft`'s existing enabled behavior).

- [ ] **Step 4: Run tests to verify they pass, plus regressions**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip_readmodel.py tests/proposal_assist/collab -p no:cacheprovider -q`
Then the read-model consumers: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_spec.py -p no:cacheprovider -q`
Expected: all pass. NOTE: any pre-existing workspace-API test that reads `/draft` or `/spec` and asserts `collab_enabled is False` will now get `True` — if one exists, STOP and report it to the controller (it is a deliberate behavior change; the controller decides the test's new expectation, do not silently edit).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_flip_readmodel.py
git add src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_flip_readmodel.py
git commit -m "$(cat <<'EOF'
feat(collab): enable-on-read in draft/spec read models + get_spec room-canonical (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Kill-switch handover — drain-then-truncate pass, lifespan + scan wiring

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/store.py` (add `find_artifacts_with_updates`, `truncate_log`)
- Modify: `src/call_manager/proposal_assist/collab/state.py` (add `reset_checkpoint`)
- Create: `src/call_manager/proposal_assist/collab/handover.py`
- Modify: `src/call_manager/main.py` (lifespan startup) and `src/call_manager/scheduler.py::_collab_checkpoint_job` (~lines 329-348)
- Test: `tests/proposal_assist/collab/test_collab_kill_switch_handover.py` (new)

**Interfaces:**
- Consumes: `checkpoint_artifact` (existing), Task 1's switch primitives.
- Produces: `store.find_artifacts_with_updates(session) -> list[tuple[uuid.UUID, str]]`; `store.truncate_log(session, *, workspace_id, artifact) -> int`; `state.reset_checkpoint(session, *, workspace_id, artifact) -> None`; `handover.run_kill_switch_handover(session_factory, *, workspaces_dir) -> int`.

**Why not `find_checkpoint_candidates(min_tail=0)`:** that query only groups rows ABOVE the watermark — a fully-checkpointed log has no such rows, yet its stale replay would still clobber kill-window git-first writes when the switch lifts (spec §3.2/§10). The handover must process every artifact with ANY log rows.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_kill_switch_handover.py` (helpers `_init_repo` / `_seed_room_edit` copied from `tests/proposal_assist/collab/test_collab_checkpoint_analysis.py` — module-local house pattern):

```python
"""3b-5d.D4 §3.2: the drain-then-truncate handover makes git the SOLE canonical
store while the kill-switch is engaged — closing BOTH clobber directions."""

import subprocess
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.collab import handover, state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_room_edit(pg_session_factory, wid, actor_id, md) -> Doc:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def _setup(pg_session_factory, tmp_path, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## Background\noriginal.\n")
    return wid, oid, repo


def _log_len(s, wid):
    return len(store.load_rows(s, workspace_id=wid, artifact="draft"))


def test_enter_direction_small_tail_drained_and_truncated(pg_session_factory, tmp_path):
    """A 1-row tail (far below the 200 scan floor) is checkpointed to git and
    the log is truncated — a subsequent git-first save can never be clobbered."""
    wid, oid, repo = _setup(pg_session_factory, tmp_path, "ho1@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nlive tail edit.\n")

    n = handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path))
    assert n == 1
    assert (repo / PROPOSAL_FILENAME).read_text() == serialize_doc_to_markdown(doc)
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 0
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0

    # kill-window git-first save lands; a second pass has nothing to clobber with
    write_and_commit_files(repo, {PROPOSAL_FILENAME: "## Background\nkill-window save.\n"}, "cm6")
    head = current_head(repo)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 0
    assert current_head(repo) == head  # untouched


def test_fully_checkpointed_log_is_still_truncated(pg_session_factory, tmp_path):
    """Exit-direction guard: even with NO uncheckpointed tail, the stale log must
    go — otherwise post-switch replay resurrects pre-kill content over
    kill-window git edits."""
    from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

    wid, oid, repo = _setup(pg_session_factory, tmp_path, "ho2@ki.se")
    _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\ncheckpointed edit.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert _log_len(s, wid) >= 1  # log present, tail == 0

    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 0

    # exit direction: kill-window git edit, then a fresh room open hydrates from CURRENT HEAD
    write_and_commit_files(repo, {PROPOSAL_FILENAME: "## Background\nkill-window truth.\n"}, "cm6")
    with pg_session_factory() as s:
        load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 1  # fresh hydration seed
    from call_manager.proposal_assist.collab.hydrate import build_doc
    assert "kill-window truth." in serialize_doc_to_markdown(build_doc([rows[0][2]]))


def test_failure_is_isolated_and_retryable(pg_session_factory, tmp_path, monkeypatch):
    """A truncate failure on one artifact rolls back atomically (checkpoint+truncate
    are one txn — the log survives, retry succeeds) and does not block others."""
    wid_a, oid_a, _ = _setup(pg_session_factory, tmp_path, "ho3a@ki.se")
    wid_b, oid_b, _ = _setup(pg_session_factory, tmp_path, "ho3b@ki.se")
    _seed_room_edit(pg_session_factory, wid_a, oid_a, "## Background\n\nA edit.\n")
    _seed_room_edit(pg_session_factory, wid_b, oid_b, "## Background\n\nB edit.\n")

    real_truncate = store.truncate_log

    def _fail_for_a(session, *, workspace_id, artifact):
        if workspace_id == wid_a:
            raise RuntimeError("boom")
        return real_truncate(session, workspace_id=workspace_id, artifact=artifact)

    monkeypatch.setattr(handover.store, "truncate_log", _fail_for_a)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1
    with pg_session_factory() as s:
        assert _log_len(s, wid_a) >= 1  # rolled back atomically — log intact
        assert _log_len(s, wid_b) == 0  # B unaffected

    monkeypatch.setattr(handover.store, "truncate_log", real_truncate)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1


def test_repoless_log_is_never_truncated(pg_session_factory, tmp_path):
    """No git repo = nowhere to hand the content over to — the log must survive
    (data preservation beats the pass completing)."""
    with pg_session_factory() as s:
        o = make_user(s, email="ho4@ki.se")
        ws = make_workspace(s, owner=o)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, o.id
    _seed_room_edit(pg_session_factory, wid, oid, "## X\n\norphan edit.\n")  # NO repo on disk

    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 0
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_kill_switch_handover.py -p no:cacheprovider -q`
Expected: collection error — no `collab.handover` module.

- [ ] **Step 3: Implement the store/state primitives**

`store.py` (add `delete` to the existing `from sqlalchemy import ...` import; append after `find_checkpoint_candidates`):

```python
def find_artifacts_with_updates(session: Session) -> list[tuple[uuid.UUID, str]]:
    """DISTINCT (workspace_id, artifact) owning ANY log rows, live workspaces
    only. The kill-switch handover needs every artifact with a log — including
    fully-checkpointed ones (a stale log would still clobber kill-window
    git-first writes on replay after the switch lifts), which is why this is
    NOT find_checkpoint_candidates(min_tail=0) (that only sees rows above the
    watermark)."""
    rows = session.execute(
        select(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .join(Workspace, Workspace.id == YjsUpdate.workspace_id)
        .where(Workspace.soft_deleted_at.is_(None))
        .distinct()
    ).all()
    return [(r[0], r[1]) for r in rows]


def truncate_log(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    """Delete ALL log rows for one artifact. Kill-switch handover ONLY — the
    caller must have checkpointed the full log to git IN THE SAME transaction.
    Returns the number of rows deleted."""
    res = session.execute(
        delete(YjsUpdate).where(
            YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact
        )
    )
    return int(res.rowcount or 0)
```

`state.py` (append):

```python
def reset_checkpoint(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Kill-switch handover ONLY: return the artifact to the 'never opened'
    state (the log is truncated separately, same transaction) so a
    post-kill-switch reconnect hydrates fresh from git HEAD. enabled_at is
    deliberately untouched (sticky-forward)."""
    row = _get(session, workspace_id, artifact)
    if row is not None:
        row.last_checkpoint_update_id = None
        row.last_checkpoint_commit_sha = None
```

- [ ] **Step 4: Implement `collab/handover.py`**

```python
"""Kill-switch handover (3b-5d.D4 §3.2): make git the SOLE canonical store.

Runs at app startup (lifespan) when COLLAB_KILL_SWITCH is engaged, and again
from the periodic scan tick while it stays engaged (retry for artifacts whose
earlier pass failed). Per artifact with ANY log rows: checkpoint the full log
to git (record_version=False), then truncate the log and reset the watermark —
drain and truncate in ONE transaction, so a truncate can never be visible
without its checkpoint. After the pass, nothing can overwrite git-first writes
(no log left to checkpoint from), and lifting the switch re-hydrates empty
stores from CURRENT git HEAD. Pure sync DB+git — never touches the asyncio
room MANAGER (no rooms exist while the switch refuses connects)."""

from __future__ import annotations

import logging

from call_manager.db.models import Workspace
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

logger = logging.getLogger(__name__)


def run_kill_switch_handover(session_factory, *, workspaces_dir) -> int:
    """Returns the number of artifacts fully handed over (drained + truncated)."""
    with session_factory() as s:
        candidates = store.find_artifacts_with_updates(s)
    done = 0
    for workspace_id, artifact in candidates:
        with session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            if ws is None or ws.soft_deleted_at is not None:
                continue
            try:
                res = checkpoint_artifact(
                    s,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    workspaces_dir=workspaces_dir,
                    record_version=False,
                )
                if res.commit_sha == "":
                    # No git repo — nowhere to hand the content over to. Keep
                    # the log (data preservation beats pass completion); the
                    # scan retries once a repo exists.
                    raise RuntimeError("no git repo to hand the collab log over to")
                store.truncate_log(s, workspace_id=workspace_id, artifact=artifact)
                state.reset_checkpoint(s, workspace_id=workspace_id, artifact=artifact)
                s.commit()  # checkpoint rows + truncate + reset: ONE transaction
                done += 1
            except Exception:  # noqa: BLE001 — isolate per artifact; scan retries
                s.rollback()
                logger.error(
                    "collab.kill_switch_handover_failed",
                    extra={
                        "event": "collab_kill_switch_handover_failed",
                        "workspace_id": str(workspace_id),
                        "artifact": artifact,
                    },
                    exc_info=True,
                )
    return done
```

- [ ] **Step 5: Run the handover tests**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_kill_switch_handover.py -p no:cacheprovider -q`
Expected: 4 passed.

- [ ] **Step 6: Wire lifespan + scheduler**

(a) `main.py` — locate `async def lifespan(...)` (the function passed to `FastAPI(lifespan=...)` at ~line 328; READ it first). At the START of its startup body — after `app.state.settings` / `app.state.session_factory` are available, BEFORE the scheduler starts — insert:

```python
    # 3b-5d.D4: kill-switch handover BEFORE serving — drain every collab log to
    # git and truncate it, so the git-first world is coherent from request #1.
    if app.state.settings.collab_kill_switch:
        from starlette.concurrency import run_in_threadpool

        from call_manager.proposal_assist.collab.handover import run_kill_switch_handover

        _n = await run_in_threadpool(
            lambda: run_kill_switch_handover(
                app.state.session_factory,
                workspaces_dir=app.state.settings.workspaces_dir,
            )
        )
        logger.warning("collab kill-switch engaged — handover drained %d artifact(s)", _n)
```

(If the lifespan body reads settings/session_factory from different names — e.g. closure variables instead of `app.state` — adapt the two references to what the function actually has in scope; everything else verbatim. Use the module's existing `logger`.)

(b) `scheduler.py` — replace the body of `_collab_checkpoint_job` (~lines 331-338):

```python
        def _collab_checkpoint_job() -> None:
            from call_manager.proposal_assist.collab.checkpoint import checkpoint_active_tails
            from call_manager.proposal_assist.collab.handover import run_kill_switch_handover

            if settings.collab_kill_switch:
                # Kill-switch engaged: retry the drain-then-truncate handover for
                # artifacts whose startup pass failed (no tail floor). Normal
                # tail-bounding is pointless while connects are refused.
                run_kill_switch_handover(
                    session_factory, workspaces_dir=settings.workspaces_dir
                )
                return
            checkpoint_active_tails(
                session_factory,
                workspaces_dir=settings.workspaces_dir,
                min_tail=settings.collab_checkpoint_min_tail,
            )
```

- [ ] **Step 7: Full collab regression + lint + commit**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab tests/test_analysis_queue.py tests/test_analysis_reconcile.py -p no:cacheprovider -q`
Expected: all pass (the handover only runs when explicitly invoked; default path untouched).

```bash
uv run ruff check src/call_manager/proposal_assist/collab/store.py src/call_manager/proposal_assist/collab/state.py src/call_manager/proposal_assist/collab/handover.py src/call_manager/main.py src/call_manager/scheduler.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py
git add src/call_manager/proposal_assist/collab/store.py src/call_manager/proposal_assist/collab/state.py src/call_manager/proposal_assist/collab/handover.py src/call_manager/main.py src/call_manager/scheduler.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py
git commit -m "$(cat <<'EOF'
feat(collab): kill-switch drain-then-truncate handover — git becomes sole canonical store (3b-5d.D4)

Closes both clobber directions from the spec §10 Codex High: enter (git-first
save vs later tail checkpoint) and exit (stale log replay vs kill-window edits).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: CM6 save-route severance (409 guards)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py::post_save_section` (insert after the `current_head` pre-read guard, ~line 808) and `::post_save_spec_section` (after its `current_head` guard, ~line 368)
- Test: `tests/proposal_assist/collab/test_collab_save_severance.py` (new)

**Interfaces:**
- Consumes: Task 1's kill-switch masking (a masked flag means NO 409 — git-first saves work again while killed) and Task 2/3's flip (a held-back artifact stays unguarded).
- Produces: both save routes return **409** `"this workspace now uses the live editor — reload the page"` when the artifact is effectively collab-enabled. `save_section` itself is untouched.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_save_severance.py`:

```python
"""3b-5d.D4 severance: CM6 saves 409 on a collab-enabled artifact (a stale tab
must not git-first-write around the live room); kill-switch restores git-first."""

import subprocess

import pytest

from call_manager.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"
SAVE_BODY_TEMPLATE = {"content": "## Body\nrewritten.\n"}


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _seed(s, tmp_path, owner, *, enable=(), with_spec=False):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, DOC)
    if with_spec:
        (repo / "proposal-spec.md").write_text("## Approach\nspec body.\n")
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "spec"], cwd=repo, check=True)
    for artifact in enable:
        state.enable_collab(s, workspace_id=ws.id, artifact=artifact)
    return ws, repo


def _save_payload(repo, fname="proposal.md", index=1, content="## Body\nrewritten.\n"):
    from call_manager.proposal_assist.citations.gitio import current_head
    from call_manager.proposal_assist.editing.sectionizer import split_sections

    doc = (repo / fname).read_text()
    sec = split_sections(doc)[index]
    return {
        "content": content,
        "base_commit_sha": current_head(repo),
        "base_section_sha": sec.sha,
    }


def test_draft_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 409
    assert "live editor" in r.json()["error"]["message"]
    assert (repo / "proposal.md").read_text() == DOC  # nothing written


def test_spec_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/spec/sections/1",
        json=_save_payload(repo, fname="proposal-spec.md", content="## Approach\nnew.\n"),
    )
    assert r.status_code == 409


def test_draft_save_works_when_only_spec_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Per-artifact: an enabled SPEC must not sever DRAFT saves."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200


def test_kill_switch_restores_git_first_save(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """With the switch on, the mask means NO 409 — the CM6 world is back."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    state.set_kill_switch(True)
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200
    assert "rewritten." in (repo / "proposal.md").read_text()
```

NOTE: the error envelope shape — existing API tests read `r.json()["error"]["message"]` for 4xx details (mirror whatever `test_api_annotations_insert.py`-adjacent tests assert; if the envelope key differs, match THAT, don't change the route).

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_save_severance.py -p no:cacheprovider -q`
Expected: the two 409 tests FAIL (routes return 200 and write); the other two pass vacuously (their value is regression).

- [ ] **Step 3: Implement the guards**

In `post_save_section`, immediately after the `current_head(repo_dir)` try/except guard (~line 808):

```python
        # 3b-5d.D4 severance: a collab-enabled artifact is Tiptap-only. A stale
        # CM6 tab (or direct API call) must not git-first-write around the live
        # room. 409 tells the client to reload (it will mount the collab editor).
        # Under the kill-switch the flag reads False -> git-first saves resume.
        if collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="draft"):
            raise HTTPException(
                status_code=409,
                detail="this workspace now uses the live editor — reload the page",
            )
```

In `post_save_spec_section`, immediately after its `current_head(repo_dir)` guard (~line 368): the same block with `artifact="spec"`.

- [ ] **Step 4: Run tests + the pre-existing save parity suites**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_save_severance.py -p no:cacheprovider -q` → 4 passed.
Then: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_editing_e2e.py tests/proposal_assist -p no:cacheprovider -q -k "save"` → all pre-existing save tests pass UNCHANGED (they never enable collab → the guard is invisible to them). Zero test-file edits outside the new file (`git status --porcelain -- tests/` shows only the new file).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_save_severance.py
git add src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_save_severance.py
git commit -m "$(cat <<'EOF'
feat(collab): sever CM6 save routes on collab-enabled artifacts — 409 reload (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `post_init_spec` — skeleton seeds through the kernel when enabled

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (add `kernel_init_spec` after `kernel_accept_prose`)
- Modify: `src/call_manager/proposal_assist/citations/collab_cite.py` (add `init_spec_via_kernel` — despite the module name it is the house home for direct async kernel wrappers; keep it there, matching `direct_cite_via_kernel`)
- Modify: `src/call_manager/web/api/workspaces.py::post_init_spec` (~lines 420-513, becomes async with a `_route()` fork)
- Test: `tests/proposal_assist/collab/test_collab_init_spec_kernel.py` (new)

**Interfaces:**
- Consumes: existing `apply_pm_json_to_fragment(doc, pm_json) -> bytes` (yjs_bridge.py:255 — whole-fragment replace returning the incremental update), `markdown_to_pm`, `serialize_doc_to_markdown`, `store.append_update(session, *, workspace_id, artifact, actor_id, update, commit=False)` (verify the exact kwarg — 2b-1 added `commit=False` semantics), `checkpoint_artifact`, `acquire_commit_mutex`, `MANAGER.operate`/`room.lock` (the `direct_cite_via_kernel` template at `collab_cite.py:35-82`), `SpecLockedError` (from `editing.save`), `RoomEvicted`.
- Produces: `kernel_init_spec(session, *, room_doc, workspace_id, workspaces_dir, actor_id) -> tuple[str, str, bytes | None]` (head_sha, room_markdown, broadcast_update_or_None) and async `init_spec_via_kernel(*, session_factory, workspaces_dir, workspace_id, actor_id) -> tuple[str, str]`.

**Deliberate provenance deviation (spec §3.3):** the kernel path's commit provenance comes from `checkpoint_artifact` (`ArtifactVersion` message `"collab checkpoint: spec"`, `WorkspaceEvent kind="collab_checkpoint"`) rather than the git path's `"user spec init"` / `kind="user_edit"` rows — do NOT duplicate provenance rows. Note this in the route docstring.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_init_spec_kernel.py` (helpers `_init_repo` with a spec file + `_reset_kill_switch` as in Task 5's file; seed the spec file as WHITESPACE-ONLY for the seeding cases):

```python
"""3b-5d.D4: 'Start spec from skeleton' goes through the KERNEL when the spec is
collab-enabled — an open empty room gets the skeleton live; git-side divergence
is impossible."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME, SPEC_SKELETON
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from tests.proposal_assist.conftest import make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _init_repo_with_blank_spec(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text("# T\nintro\n## Body\nx.\n")
    (repo / SPEC_FILENAME).write_text("\n")  # whitespace-only -> seedable
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed(s, tmp_path, owner, *, enable_spec=True):
    ws = make_workspace(s, owner=owner)
    _init_repo_with_blank_spec(Path(tmp_path) / str(ws.id) / "repo")
    if enable_spec:
        state.enable_collab(s, workspace_id=ws.id, artifact="spec")
    return ws


def test_enabled_init_seeds_skeleton_via_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    body = r.json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "## " in joined  # skeleton headings present
    with pg_session_factory() as s:
        # the ROOM is canonical and holds the skeleton (log non-empty)
        assert len(store.load_rows(s, workspace_id=wid, artifact="spec")) >= 1
        canon = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="spec",
            repo_dir=Path(tmp_path) / str(wid) / "repo",
        )
    assert canon == joined  # response sections == room serialization
    # git received the checkpoint commit too
    disk = (Path(tmp_path) / str(wid) / "repo" / SPEC_FILENAME).read_text()
    assert disk == canon


def test_enabled_init_is_noop_on_nonempty_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    assert client_authed_owner.post(f"/api/workspaces/{wid}/spec").status_code == 200
    with pg_session_factory() as s:
        n_rows = len(store.load_rows(s, workspace_id=wid, artifact="spec"))
    r2 = client_authed_owner.post(f"/api/workspaces/{wid}/spec")  # idempotent repeat
    assert r2.status_code == 200
    with pg_session_factory() as s:
        assert len(store.load_rows(s, workspace_id=wid, artifact="spec")) == n_rows


def test_enabled_init_409_on_locked_spec(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = current_user.id
        s.commit()
        wid = ws.id
    assert client_authed_owner.post(f"/api/workspaces/{wid}/spec").status_code == 409


def test_disabled_init_takes_git_path_unchanged(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user, enable_spec=False)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    disk = (Path(tmp_path) / str(wid) / "repo" / SPEC_FILENAME).read_text()
    assert disk == SPEC_SKELETON  # byte-for-byte legacy write
    with pg_session_factory() as s:
        assert store.load_rows(s, workspace_id=wid, artifact="spec") == []  # room untouched
```

- [ ] **Step 2: Run tests to verify they fail the right way**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_init_spec_kernel.py -p no:cacheprovider -q`
Expected: `test_enabled_init_seeds_skeleton_via_room` FAILS (room log empty — the git path wrote disk only); the disabled test PASSES (regression); the noop/locked tests may pass vacuously.

- [ ] **Step 3: Implement `kernel_init_spec` (kernel.py, after `kernel_accept_prose`)**

READ `kernel_accept_prose` (kernel.py:150+) first and mirror its session/mutex/spec-lock idioms exactly. The function (sync, runs inside the wrapper's `_body` threadpool):

```python
def kernel_init_spec(
    session: Session,
    *,
    room_doc,
    workspace_id: uuid.UUID,
    workspaces_dir,
    actor_id: int,
) -> tuple[str, str, bytes | None]:
    """Seed SPEC_SKELETON into an empty live spec room (3b-5d.D4 §3.3).

    Returns (head_sha, room_markdown, broadcast_update). broadcast_update is
    None on the idempotent no-op path (room already has content). Raises
    SpecLockedError under the mutex (mirrors post_init_spec's git path) and
    CommitMutexTimeout. Caller commits."""
    from call_manager.proposal_assist.artifacts import SPEC_SKELETON
    from call_manager.proposal_assist.collab.checkpoint import (
        checkpoint_artifact,
        serialize_doc_to_markdown,
    )
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_json_to_fragment
    from call_manager.proposal_assist.editing.save import SpecLockedError

    acquire_commit_mutex(session, workspace_id)
    ws = session.get(Workspace, workspace_id)
    session.refresh(ws, ["spec_locked_at"])
    if ws.spec_locked_at is not None:
        raise SpecLockedError(str(workspace_id))

    live_md = serialize_doc_to_markdown(room_doc)
    if live_md.strip() != "":
        # Idempotent no-op: the room already has content. Head = current git
        # HEAD (the response's git anchor, same as the git path's no-op branch).
        from call_manager.proposal_assist.citations.gitio import current_head
        from call_manager.proposal_assist.paths import workspace_repo_dir

        head = current_head(workspace_repo_dir(workspaces_dir, workspace_id))
        return head, live_md, None

    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(SPEC_SKELETON))
    store.append_update(
        session, workspace_id=workspace_id, artifact="spec",
        actor_id=actor_id, update=update, commit=False,
    )
    cp = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact="spec", workspaces_dir=workspaces_dir,
    )
    return cp.commit_sha, serialize_doc_to_markdown(room_doc), update
```

Adapt the import style to kernel.py's existing conventions (it already imports `acquire_commit_mutex`, `store`, `Workspace` etc. at module level — REUSE those; the function-local imports above are only for symbols the module does not already have; hoist any that ruff flags). If `store.append_update`'s no-commit kwarg is named differently, match reality — the invariant is: append staged, not committed.

- [ ] **Step 4: Implement `init_spec_via_kernel` (collab_cite.py, after `direct_cite_via_kernel`)**

```python
async def init_spec_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, actor_id: int
) -> tuple[str, str]:
    """'Start spec from skeleton' against the live spec room. Same
    operate/lock/threadpool/broadcast-outside-lock/evict contract as
    direct_cite_via_kernel. Returns (head_sha, room_markdown)."""
    from call_manager.proposal_assist.collab.kernel import kernel_init_spec
    from call_manager.proposal_assist.editing.save import SpecLockedError

    key = (workspace_id, "spec")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, workspace_id=workspace_id, artifact="spec",
                    workspaces_dir=workspaces_dir,
                )
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, str, str]:
                    with session_factory() as s:
                        head, md, update = kernel_init_spec(
                            s, room_doc=room.doc, workspace_id=workspace_id,
                            workspaces_dir=workspaces_dir, actor_id=actor_id,
                        )
                        if room.evicted:
                            raise RoomEvicted(str(key))
                        s.commit()
                        return update, head, md
                update, head, md = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return head, md
        except (SpecLockedError, CommitMutexTimeout, RoomEvicted):
            raise  # pre/clean — room doc untouched or tombstoned; no evict
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
```

CAREFUL: `kernel_init_spec` mutates `room.doc` BEFORE its checkpoint; a failure AFTER the mutation must evict (dirty room) — that is exactly what the bare-`except` does; `SpecLockedError`/`CommitMutexTimeout` are raised BEFORE the mutation so they are clean. Do not reorder the function so that stops being true.

- [ ] **Step 5: Rewrite `post_init_spec` with the `_route()` fork**

Convert to `async def`. Shape (mirror `post_citation`, ~lines 858-997; the legacy body moves VERBATIM into a `def _git() -> SpecOut:` closure run via `run_in_threadpool`):

```python
@router.post("/workspaces/{workspace_id}/spec", response_model=SpecOut)
async def post_init_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Idempotent Spec initialization (owner-gated). Flag-on: the skeleton seeds
    through the live room (kernel) — provenance comes from the collab checkpoint,
    not a user_edit row. Flag-off: the legacy whole-doc git commit, byte-for-byte."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory

    def _route() -> bool:
        with sf() as session:
            return collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="spec"
            )

    enabled = await run_in_threadpool(_route)

    if enabled:
        def _gate() -> None:
            with sf() as session:
                require_owner(workspace_id=workspace_id, user=user, session=session)
            if not (workspace_repo_dir(settings.workspaces_dir, workspace_id) / SPEC_FILENAME).exists():
                raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        await run_in_threadpool(_gate)
        try:
            head, md = await init_spec_via_kernel(
                session_factory=sf, workspaces_dir=settings.workspaces_dir,
                workspace_id=workspace_id, actor_id=user.id,
            )
        except SpecLockedError as e:
            raise HTTPException(status_code=409, detail="spec is locked — unlock to edit") from e
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except RoomEvicted as e:
            raise HTTPException(status_code=409, detail="the live session was reset — retry") from e
        except GitCommitError as e:
            raise HTTPException(
                status_code=500, detail="spec init failed — nothing was written"
            ) from e
        return SpecOut(
            head=head, can_edit=True, sections=_sections_out(md),
            locked=False, locked_at=None, locked_by=None, can_manage_lock=True,
            collab_enabled=True, can_write=True,
        )

    def _git() -> SpecOut:
        # ↓↓↓ the ENTIRE former post_init_spec body, unmodified (owner check,
        # mutex, lock guard, whitespace branch, provenance rows, SpecOut) ↓↓↓
        ...
    return await run_in_threadpool(_git)
```

Imports to add near `direct_cite_via_kernel`'s: `init_spec_via_kernel`, `SpecLockedError` (already imported? verify), `RoomEvicted` (from `proposals.kernel_accept`). The `_git` closure is a MOVE, not a rewrite — `git diff` of that block should show pure indentation.

- [ ] **Step 6: Run tests + parity regressions**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_init_spec_kernel.py -p no:cacheprovider -q` → 4 passed.
Then every pre-existing init-spec test: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist -p no:cacheprovider -q -k "init_spec or spec_init"` → pass UNCHANGED, plus `tests/test_api_spec.py`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/kernel.py src/call_manager/proposal_assist/citations/collab_cite.py src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_init_spec_kernel.py
git add src/call_manager/proposal_assist/collab/kernel.py src/call_manager/proposal_assist/citations/collab_cite.py src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_init_spec_kernel.py
git commit -m "$(cat <<'EOF'
feat(collab): spec skeleton seeds through the kernel when collab-enabled (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `get_annotations` — room-canonical Claims-tab read

**Files:**
- Modify: `src/call_manager/web/api/annotations.py::get_annotations` (~lines 89-97)
- Test: `tests/proposal_assist/collab/test_collab_annotations_canonical.py` (new)

**Interfaces:**
- Consumes: `canonical_artifact_markdown`, `collab_state.is_collab_enabled`.
- Produces: Claims-tab sections derive from the live room when draft collab is enabled — Task 8's insert then anchors against the SAME content the tab displayed.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_collab_annotations_canonical.py`:

```python
"""3b-5d.D4 §3.4 (Codex Medium): the Claims tab sectionizes the LIVE room, not
stale git, once draft collab is enabled."""

import pytest

from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def test_annotations_read_reflects_live_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    live = pm_json_to_ydoc(markdown_to_pm("# T\n\nintro\n\n## Body\n\nlive rewrite.\n"))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=current_user.id,
            update=live.get_update(),
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{wid}/annotations").json()
    live_shas = {sec.sha for sec in split_sections(serialize_doc_to_markdown(live))}
    disk_shas = {sec.sha for sec in split_sections(DOC)}
    got = {s_["section_sha"] for s_ in body["sections"]}
    assert got and got <= live_shas          # every returned sha comes from the ROOM
    assert not (got & (disk_shas - live_shas))  # none from the stale disk doc


def test_annotations_read_unchanged_when_disabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        wid = ws.id
    body = client_authed_owner.get(f"/api/workspaces/{wid}/annotations").json()
    disk_shas = {sec.sha for sec in split_sections(DOC) if sec.text.strip()}
    got = {s_["section_sha"] for s_ in body["sections"]}
    assert got <= disk_shas and got  # disk-derived, non-empty
```


- [ ] **Step 2: Run tests to verify the enabled case fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_annotations_canonical.py -p no:cacheprovider -q`
Expected: enabled-case FAIL (shas come from disk DOC); disabled-case PASS.

- [ ] **Step 3: Implement**

In `annotations.py`, replace line ~97 (`sections = split_sections(proposal.read_text())`) with:

```python
        # 3b-5d.D4 §3.4: with draft collab enabled the tab must sectionize the
        # LIVE room (the room-aware insert of the same tab anchors against it);
        # claim rows are sha-keyed, so a live-edited section simply shows no
        # findings until the next checkpoint's analysis lands.
        if collab_state.is_collab_enabled(session, workspace_id=ws.id, artifact="draft"):
            doc_raw = canonical_artifact_markdown(
                session, workspace_id=ws.id, artifact="draft", repo_dir=repo
            )
        else:
            doc_raw = proposal.read_text()
        sections = split_sections(doc_raw)
```

Add imports: `from call_manager.proposal_assist.collab import state as collab_state` and `from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown`. The pre-draft degrade guards above (missing file / no HEAD → 200 empty) stay untouched.

- [ ] **Step 4: Run tests + the annotations read-model regressions**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_annotations_canonical.py tests/proposal_assist/test_api_annotations_read.py tests/proposal_assist/test_api_annotations_insert.py tests/proposal_assist/test_api_annotations_dismiss.py -p no:cacheprovider -q`
Expected: all pass (pre-existing tests never enable collab).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/web/api/annotations.py tests/proposal_assist/collab/test_collab_annotations_canonical.py
git add src/call_manager/web/api/annotations.py tests/proposal_assist/collab/test_collab_annotations_canonical.py
git commit -m "$(cat <<'EOF'
feat(collab): Claims-tab read model goes room-canonical when draft collab is on (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Claims-tab insert — route through the kernel when enabled

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/insert.py` (extract `derive_annotation_cite`; `insert_from_annotation` delegates — behavior-preserving)
- Modify: `src/call_manager/proposal_assist/citations/collab_cite.py` (add `insert_annotation_via_kernel`)
- Modify: `src/call_manager/web/api/annotations.py::insert_annotation` (~lines 190-245, becomes async with the `_route()` fork)
- Test: `tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py` (new); existing `tests/proposal_assist/test_api_annotations_insert.py` + `tests/test_analysis_insert*.py` (if present, locate via `grep -rl insert_from_annotation tests/`) pass **UNCHANGED** — that is the extraction gate, same discipline as D3's reconcile refactor.

**Interfaces:**
- Consumes: `apply_citation_to_room` (kernel.py:65 — the citation primitive `direct_cite_via_kernel` already uses, with `require_anchor_in_section=True, enforce_section_sha=False`); the `direct_cite_via_kernel` wrapper template; Task 7's canonical read (the tab and this insert now see the same doc).
- Produces: `derive_annotation_cite(session, *, workspace, annotation, doc: str) -> DerivedCite | None` (`None` = this annotation already inserted, the idempotent signal; `DerivedCite` is a frozen dataclass: `section_index: int, anchor: str, reference_id, chunk_index: int, evidence_quote: str, quote_start: int, quote_end: int`) raising `InsertConflict` exactly where the current inline steps 3-6 do; async `insert_annotation_via_kernel(*, session_factory, workspaces_dir, workspace_id, actor_id, annotation_id) -> dict` returning `{"ev_id","citekey","commit_sha"}` or `{"idempotent": True}`.

- [ ] **Step 1: Extraction first (behavior-preserving, gated by existing tests)**

In `analysis/insert.py`: define the dataclass + move steps **3-6** of `insert_from_annotation` (status/actionability checks → reference+chunk resolution → quote-offset verify → target-section match from `doc` → anchor build/extend) into:

```python
@dataclass(frozen=True)
class DerivedCite:
    section_index: int
    anchor: str
    reference_id: uuid.UUID
    chunk_index: int
    evidence_quote: str
    quote_start: int
    quote_end: int


def derive_annotation_cite(
    session: Session, *, workspace: Workspace, annotation: UserClaimAnnotation, doc: str
) -> DerivedCite | None:
    """Steps 3-6 of insert_from_annotation, extracted VERBATIM with the document
    text parameterized (git path passes the disk read; the kernel path passes
    the live-room serialization). Returns None when THIS annotation is already
    inserted (the idempotent signal); raises InsertConflict exactly as before.
    Contains NO mutex/expire/turn-lock — callers own those."""
```

Body: the current lines from the `if annotation.status == "inserted": return ...` check through the anchor-uniqueness `raise` — with `return {"idempotent": True}` becoming `return None`, `doc = proposal_path.read_text()` deleted (the `doc` param replaces it; the `proposal_path.exists()` guard STAYS in the git caller), and the final line `return DerivedCite(section_index=target.index, anchor=anchor, reference_id=ref.id, chunk_index=annotation.chunk_index, evidence_quote=annotation.evidence_quote, quote_start=q_start, quote_end=q_end)`. Then `insert_from_annotation` keeps its steps 1-2 (mutex, expire, turn-lock) and the disk read + exists-guard, calls `derived = derive_annotation_cite(...)`, maps `None → {"idempotent": True}`, and continues its steps 7+ (ledger/citekey/marker/commit/annotation-status) using `derived.anchor` etc. — READ insert.py:206-317 and keep those steps byte-identical, only swapping variable sources.

Gate: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_api_annotations_insert.py $(grep -rl insert_from_annotation tests/ | tr '\n' ' ') -p no:cacheprovider -q` → all pass with ZERO test edits (`git status --porcelain -- tests/` clean). Commit the extraction alone:

```bash
uv run ruff check src/call_manager/proposal_assist/analysis/insert.py
git add src/call_manager/proposal_assist/analysis/insert.py
git commit -m "$(cat <<'EOF'
refactor(analysis): extract derive_annotation_cite from insert_from_annotation (3b-5d.D4)

Behavior-preserving; existing annotation-insert tests pass unchanged.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Write the failing kernel-path tests**

Create `tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py` (seed helper copied from `tests/proposal_assist/test_api_annotations_insert.py::_seed_supported`, plus `state.enable_collab` + `_reset_kill_switch` autouse):

```python
"""3b-5d.D4: Claims-tab insert routes through the KERNEL when draft collab is on —
the marker lands in the LIVE room + ledger + checkpoint; annotation flips to
'inserted'; second call is idempotent."""

import pytest

from call_manager.db.models import UserClaimAnnotation
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_user
from tests.proposal_assist.test_api_annotations_insert import _seed_supported


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def test_enabled_insert_lands_in_room_and_flips_annotation(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    body = r.json()
    assert body["ev_id"].startswith("ev-") and body["commit_sha"]
    with pg_session_factory() as s:
        canon = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft",
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
        )
        assert f"ev:{body['ev_id']}" in canon  # marker is IN THE ROOM projection
        assert s.get(UserClaimAnnotation, ann_id).status == "inserted"
    # idempotent repeat
    r2 = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r2.status_code == 200 and r2.json()["idempotent"] is True


def test_disabled_insert_takes_git_path(  # regression pin for the fork
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    assert "<!-- ev:ev-" in (repo / "proposal.md").read_text()  # git-first, disk mutated
```

- [ ] **Step 3: Run to verify the enabled case fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py -p no:cacheprovider -q`
Expected: enabled-case FAIL (`ev:` marker lands on DISK, not in the room projection — the canonical read shows the pre-insert seed or falls back); disabled-case PASS.

- [ ] **Step 4: Implement `insert_annotation_via_kernel` (collab_cite.py)**

```python
async def insert_annotation_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, actor_id: int,
    annotation_id: int,
) -> dict:
    """Claims-tab insert against the live room: derive the cite from the
    annotation vs the ROOM-serialized doc, then apply via the same kernel
    citation primitive as direct_cite_via_kernel; flip the annotation to
    'inserted' in the SAME transaction as the room append + checkpoint."""
    from call_manager.db.models import UserClaimAnnotation, Workspace
    from call_manager.proposal_assist.analysis.insert import (
        InsertConflict,
        _turn_in_flight,
        derive_annotation_cite,
    )
    from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown

    key = (workspace_id, "draft")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(s, workspace_id=workspace_id, artifact="draft",
                                          workspaces_dir=workspaces_dir)
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, dict]:
                    with session_factory() as s:
                        ws = s.get(Workspace, workspace_id)
                        ann = s.get(UserClaimAnnotation, annotation_id)
                        if ann is None or ann.workspace_id != workspace_id:
                            raise InsertConflict("annotation is no longer actionable")
                        if _turn_in_flight(s, workspace_id):
                            raise InsertConflict(TURN_IN_FLIGHT_MESSAGE)
                        live_md = serialize_doc_to_markdown(room.doc)
                        derived = derive_annotation_cite(
                            s, workspace=ws, annotation=ann, doc=live_md
                        )
                        if derived is None:
                            return None, {"idempotent": True}
                        res = apply_citation_to_room(
                            s, room_doc=room.doc, workspace_id=workspace_id,
                            section_index=derived.section_index,
                            base_section_sha="", anchor=derived.anchor,
                            reference_id=derived.reference_id,
                            chunk_index=derived.chunk_index,
                            evidence_quote=derived.evidence_quote,
                            quote_start=derived.quote_start, quote_end=derived.quote_end,
                            stance="supports", actor_id=actor_id,
                            workspaces_dir=workspaces_dir,
                            require_anchor_in_section=True, enforce_section_sha=False,
                        )
                        if room.evicted:
                            raise RoomEvicted(str(key))
                        ann.status = "inserted"  # mirror the git path's flip — verify
                        # against insert.py steps 8-9 whether more fields are set
                        # (e.g. inserted ev linkage) and mirror them EXACTLY.
                        s.commit()
                        return res.update, {"ev_id": res.ev_id, "citekey": res.citekey,
                                            "commit_sha": res.commit_sha}
                update, out = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return out
        except (*_CLEAN_CITE_ERRORS, InsertConflict):
            raise  # pre-mutation / tombstoned — no evict
        except Exception:
            await MANAGER.evict(key)
            raise
```

`TURN_IN_FLIGHT_MESSAGE` imports from `analysis.insert` (verify its name there; if module-private, import the module and reference it). BEFORE committing this step, READ `insert.py`'s post-insert annotation mutation block (steps 8-9, ~lines 206-317) and mirror every annotation-row field it sets — the invariant: after either path, the `UserClaimAnnotation` row is in the identical state. If the git path sets fields beyond `status` (e.g. a link to the created evidence id or timestamps), replicate them; state what you found in your report.

- [ ] **Step 5: Rewrite `insert_annotation` with the `_route()` fork**

Async conversion mirroring `post_citation`: `_route()` reads the draft flag; enabled → owner gate (`require_owner` + annotation 404 in a threadpooled `_gate`), then `insert_annotation_via_kernel(...)`, mapping `InsertConflict → 409(str(e))`, `CiteStale/CiteInvalid/CiteRefNotFound → 409(str(e))` (the annotation world reports every expected failure as a 409 conflict — parity), `CommitMutexTimeout → 429 + Retry-After`, `RoomEvicted → 409("the live session was reset — retry")`, `GitCommitError → 500("insert failed — nothing was written")`; build `InsertResultOut` from the dict (idempotent flag included). On the NON-idempotent kernel success, ALSO call `enqueue_containing_section(...)` exactly as the git branch does (same args, `commit_sha` from the result) — Tier-2 immediacy parity. Disabled → the ENTIRE former body verbatim in a threadpooled `def _git() -> InsertResultOut:` closure.

- [ ] **Step 6: Run all gates for this task**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py tests/proposal_assist/test_api_annotations_insert.py tests/proposal_assist/test_api_annotations_read.py -p no:cacheprovider -q`
Expected: all pass; `git status --porcelain -- tests/proposal_assist/test_api_annotations_insert.py` is clean.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/citations/collab_cite.py src/call_manager/web/api/annotations.py tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py
git add src/call_manager/proposal_assist/citations/collab_cite.py src/call_manager/web/api/annotations.py tests/proposal_assist/collab/test_collab_annotation_insert_kernel.py
git commit -m "$(cat <<'EOF'
feat(collab): Claims-tab insert routes through the kernel when draft collab is on (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `AddCitationOut.citekey` Optional + OpenAPI regen

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py:186` (`citekey: str` → `citekey: str | None`)
- Modify (generated): `frontend/src/api/schema.gen.ts` via `pnpm run gen:api`
- Test: type-level (tsc) + one backend assertion added to the EXISTING kernel-cite test file if a citekey-None fixture already exists there — otherwise the model change is assertion-free by design (the 500 was theoretical; the fix is the type).

**Interfaces:**
- Produces: `AddCitationOut.citekey: str | None` — closes the 3b-5a deferred "idempotent re-cite of a citekey-less ledger row → pydantic 500".

- [ ] **Step 1: Change the model**

```python
class AddCitationOut(BaseModel):
    ev_id: str
    # None on an idempotent re-cite of a citekey-less/corrupt ledger row —
    # kernel.py carries str | None end-to-end (3b-5a deferral, fixed at the flip).
    citekey: str | None
    commit_sha: str
```

- [ ] **Step 2: Regenerate the OpenAPI types + verify frontend null-tolerance**

From `frontend/`: `EMBEDDING_MODEL=bge-m3 pnpm run gen:api` then `pnpm exec tsc --noEmit`.
If tsc fails on a `citekey` consumer: fix the consumer with a null-coalesce (`citekey ?? ev_id` for display) — `grep -rn "citekey" frontend/src --include="*.tsx" --include="*.ts" | grep -v schema.gen` to find them all; report which you touched.

- [ ] **Step 3: Backend + API-spec regression**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_spec.py tests/proposal_assist/collab -p no:cacheprovider -q`
Expected: all pass (test_api_spec snapshots the schema — if it pins `citekey` as non-nullable, its fixture regenerates or the test asserts the NEW shape; if the file requires a snapshot update, that IS the intended change — update the snapshot per that file's own regen instructions, and say so in your report).

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/call_manager/web/api/workspaces.py
git add src/call_manager/web/api/workspaces.py frontend/src/api/schema.gen.ts
git commit -m "$(cat <<'EOF'
fix(collab): AddCitationOut.citekey Optional — closes 3b-5a idempotent-recite 500 (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

(If Step 2 touched frontend consumers, add those exact paths to the `git add` list.)

---

### Task 10: Prod CSP — narrow same-origin WS token

**Files:**
- Modify: `src/call_manager/web/middleware.py::build_csp` (lines 20-59)
- Modify: `src/call_manager/main.py` (~line 341, the `_csp = build_csp(...)` wiring)
- Test: `tests/test_security_headers.py` (extend)

**Interfaces:**
- Produces: `build_csp(dev: bool = False, ws_origin: str | None = None) -> str` — non-dev `connect-src 'self' <ws_origin>`; dev output byte-identical to today. `main.py` derives `ws_origin` from `APP_BASE_URL` (`https→wss`, `http→ws`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_security_headers.py`; read its existing style first and match it)

```python
def test_build_csp_prod_has_same_origin_ws_token():
    from call_manager.web.middleware import build_csp

    csp = build_csp(dev=False, ws_origin="wss://grants.example.org")
    assert "connect-src 'self' wss://grants.example.org" in csp
    assert "ws:" not in csp.replace("wss://grants.example.org", "")  # narrow, no bare scheme
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]


def test_build_csp_http_base_gets_ws_token():
    from call_manager.web.middleware import build_csp

    assert "connect-src 'self' ws://lanhost:8000" in build_csp(ws_origin="ws://lanhost:8000")


def test_build_csp_dev_output_unchanged():
    from call_manager.web.middleware import build_csp

    assert "connect-src 'self' ws: http://localhost:5173" in build_csp(dev=True)
    # dev ignores ws_origin (its broad ws: token already covers the Yjs socket)
    assert build_csp(dev=True) == build_csp(dev=True, ws_origin="wss://x")


def test_build_csp_no_origin_stays_strict():
    from call_manager.web.middleware import build_csp

    assert "connect-src 'self';" in build_csp() + ";"
```

- [ ] **Step 2: Run to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_security_headers.py -p no:cacheprovider -q`
Expected: TypeError — `build_csp` has no `ws_origin` parameter.

- [ ] **Step 3: Implement**

`middleware.py::build_csp` — signature + connect handling (docstring gains one paragraph; everything else verbatim):

```python
def build_csp(dev: bool = False, ws_origin: str | None = None) -> str:
```

```python
    script = "script-src 'self'"
    connect = "connect-src 'self'"
    if ws_origin:
        # 3b-5d.D4: narrow same-origin allowance for the Yjs WebSocket
        # (connect-src 'self' does not reliably cover ws:/wss: across engines).
        # Exactly ONE origin, derived from APP_BASE_URL — never a bare scheme.
        connect = f"connect-src 'self' {ws_origin}"
    if dev:
        script = "script-src 'self' 'unsafe-inline'"
        connect = "connect-src 'self' ws: http://localhost:5173"
```

`main.py` — replace the `_csp = build_csp(dev=settings.spa_mode == "dev")` line (~341):

```python
    # 3b-5d.D4: the Yjs WS needs an explicit same-origin token in the non-dev
    # CSP. Derived from APP_BASE_URL — which also drives check_origin, cookie
    # security and HSTS, so scheme misalignment fails loudly here at startup.
    _base = urlsplit(settings.app_base_url)
    if not _base.scheme or not _base.netloc:
        raise RuntimeError(f"APP_BASE_URL is not a valid URL: {settings.app_base_url!r}")
    _ws_origin = f"{'wss' if _base.scheme == 'https' else 'ws'}://{_base.netloc}"
    _csp = build_csp(dev=settings.spa_mode == "dev", ws_origin=_ws_origin)
```

Add `from urllib.parse import urlsplit` to main.py's stdlib imports (check it isn't already there).

- [ ] **Step 4: Run the full header suites**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_security_headers.py tests/test_spa_serving.py -p no:cacheprovider -q`
Expected: all pass. If a pre-existing test asserts the EXACT old prod `connect-src 'self'` string (docstring says tests assert strictness), it now sees the extra token — if that test's intent was "no third-party origins", update its assertion to allow exactly the same-origin ws token and SAY SO in the report; if it asserted generic substrings it passes untouched.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/web/middleware.py src/call_manager/main.py tests/test_security_headers.py
git add src/call_manager/web/middleware.py src/call_manager/main.py tests/test_security_headers.py
git commit -m "$(cat <<'EOF'
feat(web): narrow same-origin WS token in the non-dev CSP for the Yjs socket (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Tiptap formatting floor — shortcuts + input rules (frontend-only)

**Files:**
- Modify: `frontend/src/collab/schema.ts`
- Test: `frontend/src/collab/schema.formatting.test.tsx` (new; mirror the Editor-construction setup of the existing tests in `frontend/src/collab/` — find one with `new Editor(` or `useEditor` mocking and copy its scaffolding)

**Interfaces:**
- Consumes/produces: the SAME node/mark set — no new types, no attribute changes (the Python bridge, goldens, and `collab-schema.mjs` twin stay untouched; `collabSchema.equality.test.ts` must still pass, which proves the schema SHAPE didn't move).

- [ ] **Step 1: Write the failing tests**

```tsx
/** 3b-5d.D4 formatting floor: author bold/italic/strike + headings/lists. */
import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";

import { collabExtensions } from "./schema";

function makeEditor(content = "<p></p>") {
  return new Editor({ extensions: collabExtensions, content });
}

function pressMod(editor: Editor, key: string, shift = false) {
  editor.view.dom.dispatchEvent(
    new KeyboardEvent("keydown", { key, ctrlKey: true, shiftKey: shift, bubbles: true })
  );
}

describe("mark shortcuts", () => {
  it("Mod-B toggles strong", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "b");
    expect(e.getHTML()).toContain("<strong>word</strong>");
  });
  it("Mod-I toggles em", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "i");
    expect(e.getHTML()).toContain("<em>word</em>");
  });
  it("Mod-Shift-S toggles strikethrough", () => {
    const e = makeEditor("<p>word</p>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    pressMod(e, "s", true);
    expect(e.getHTML()).toContain("<s>word</s>");
  });
});

describe("block input rules", () => {
  // Input rules fire on handleTextInput — simulate typing the trigger char at
  // the end of the prefix (the established Tiptap testing technique).
  function type(e: Editor, text: string) {
    const { state } = e.view;
    const handled = e.view.someProps.handleTextInput?.(
      e.view, state.selection.from, state.selection.to, text
    );
    if (!handled) e.view.dispatch(state.tr.insertText(text));
  }

  it("'## ' becomes an h2", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("##");
    e.commands.setTextSelection(3);
    type(e, " ");
    expect(e.getHTML()).toContain("<h2>");
  });
  it("'- ' becomes a bullet list", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("-");
    e.commands.setTextSelection(2);
    type(e, " ");
    expect(e.getHTML()).toContain("<ul>");
  });
  it("'1. ' becomes an ordered list", () => {
    const e = makeEditor("<p></p>");
    e.commands.insertContent("1.");
    e.commands.setTextSelection(3);
    type(e, " ");
    expect(e.getHTML()).toContain("<ol>");
  });
});
```

Run: `cd frontend && pnpm exec vitest run src/collab/schema.formatting.test.tsx` → all FAIL (no shortcuts/rules registered). If the `pressMod`/`type` simulation doesn't fire in jsdom (environment quirk), fall back to asserting via `editor.commands.toggleMark("strong")` for marks and calling the extension's input-rule handler through `editor.view.someProps.handleTextInput` only — the REQUIRED outcome is one meaningful failing-then-passing assertion per shortcut and per rule; adapt the mechanics, not the coverage, and record what you changed.

- [ ] **Step 2: Implement in `schema.ts`**

Add `textblockTypeInputRule, wrappingInputRule` to the `@tiptap/core` import. Replace the three mark definitions and extend the three block nodes (note the switch from arrow-property to method syntax where `this` is needed — keep everything else on each node byte-identical):

```ts
const Heading = Node.create({
  name: "heading", group: "block", content: "inline*", defining: true,
  addAttributes: () => ({ level: { default: 1 } }),
  parseHTML: () => [1, 2, 3, 4, 5, 6].map((level) => ({ tag: `h${level}`, attrs: { level } })),
  renderHTML: ({ node, HTMLAttributes }) => [`h${node.attrs.level}`, mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    // "# " .. "###### " at line start → heading of that level (3b-5d.D4 floor)
    return [1, 2, 3, 4, 5, 6].map((level) =>
      textblockTypeInputRule({
        find: new RegExp(`^(#{${level}})\\s$`),
        type: this.type,
        getAttributes: { level },
      })
    );
  },
});
```

```ts
const BulletList = Node.create({
  name: "bullet_list", group: "block", content: "list_item+",
  parseHTML: () => [{ tag: "ul" }], renderHTML: ({ HTMLAttributes }) => ["ul", mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    return [wrappingInputRule({ find: /^\s*([-+*])\s$/, type: this.type })];
  },
});
const OrderedList = Node.create({
  name: "ordered_list", group: "block", content: "list_item+",
  addAttributes: () => ({ order: { default: 1 } }),
  parseHTML: () => [{ tag: "ol" }], renderHTML: ({ HTMLAttributes }) => ["ol", mergeAttributes(HTMLAttributes), 0],
  addInputRules() {
    return [
      wrappingInputRule({
        find: /^(\d+)\.\s$/,
        type: this.type,
        getAttributes: (match) => ({ order: +match[1] }),
      }),
    ];
  },
});
```

```ts
const Em = Mark.create({
  name: "em", parseHTML: () => [{ tag: "em" }, { tag: "i" }], renderHTML: () => ["em", 0],
  addKeyboardShortcuts() {
    return { "Mod-i": () => this.editor.commands.toggleMark(this.name) };
  },
});
const Strong = Mark.create({
  name: "strong", parseHTML: () => [{ tag: "strong" }, { tag: "b" }], renderHTML: () => ["strong", 0],
  addKeyboardShortcuts() {
    return { "Mod-b": () => this.editor.commands.toggleMark(this.name) };
  },
});
const Strikethrough = Mark.create({
  name: "strikethrough", parseHTML: () => [{ tag: "s" }, { tag: "strike" }, { tag: "del" }], renderHTML: () => ["s", 0],
  addKeyboardShortcuts() {
    return { "Mod-Shift-s": () => this.editor.commands.toggleMark(this.name) };
  },
});
```

- [ ] **Step 3: Run the frontend gates**

```bash
cd frontend && pnpm exec vitest run src/collab && pnpm exec tsc --noEmit
```
Expected: ALL src/collab tests pass — including `collabSchema.equality.test.ts` (shortcuts/input-rules are behavior, not schema shape; if the equality test fails, the change leaked into the schema — STOP and fix, do not touch the equality test).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/collab/schema.ts frontend/src/collab/schema.formatting.test.tsx
git commit -m "$(cat <<'EOF'
feat(collab): Tiptap formatting floor — B/I/S shortcuts + heading/list input rules (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Two-browser e2e in `SPA_MODE=prod`

**Files:**
- Modify: `scripts/collab_e2e_seed.py` (~line 57, the hard `os.environ["SPA_MODE"] = "dev"`)
- Modify: `scripts/collab_e2e_run.sh` (prod mode: build+sync the SPA, skip Vite, point the browser at :8000)
- Modify: `frontend/e2e/collab.spec.ts` (base-URL parameterization, if it hard-codes :5173)

**Interfaces:**
- Produces: `E2E_SPA_MODE=prod scripts/collab_e2e_run.sh` — the flip's proof: built bundle served by FastAPI + the Task 10 CSP (`APP_BASE_URL=http://localhost:8000` → `ws://localhost:8000` token) + scenarios (a)-(e) green. Default invocation stays dev-mode, byte-identical behavior.

This task is exploratory by nature — the runner's process management is delicate (`set -m`, PGID cleanup; see the header comments). Contract, not keystrokes:

- [ ] **Step 1: Seed parameterization** — `scripts/collab_e2e_seed.py` line ~57 becomes:

```python
# SPA_MODE: "dev" (default) uses the Vite server + dev CSP's broad ws: token.
# "prod" (E2E_SPA_MODE=prod) serves the BUILT bundle from FastAPI under the
# strict CSP with ONLY the same-origin ws token derived from APP_BASE_URL —
# the 3b-5d.D4 flip gate. prod REQUIRES a built SPA synced to
# src/call_manager/web/spa/ (the runner does this).
os.environ["SPA_MODE"] = os.environ.get("E2E_SPA_MODE", "dev")
```

Also ensure the seed exports/normalizes `APP_BASE_URL=http://localhost:8000` in prod mode (check what it sets today; the Yjs socket and check_origin must agree with the page origin :8000).

- [ ] **Step 2: Runner prod branch** — in `collab_e2e_run.sh`, when `E2E_SPA_MODE=prod`: (a) build + sync the bundle BEFORE booting the seed — `(cd frontend && pnpm build)` then sync `frontend/dist/` → `src/call_manager/web/spa/` (find the existing house sync — `grep -rn "web/spa" scripts/ frontend/package.json docs/superpowers/plans/2026-06*.md | head` — reuse it; if none exists as a script, `rm -rf src/call_manager/web/spa && cp -r frontend/dist src/call_manager/web/spa` and confirm that path is gitignored first with `git check-ignore -q src/call_manager/web/spa`; if it is NOT ignored, STOP and report); (b) skip starting Vite and the :5173 wait; (c) export the browser base URL as :8000 for the spec.

- [ ] **Step 3: Spec base-URL** — in `collab.spec.ts`, replace any hard-coded `http://localhost:5173` with `process.env.E2E_BASE_URL ?? "http://localhost:5173"`, and have the runner export `E2E_BASE_URL=http://localhost:8000` in prod mode. READ the file first — if it already navigates via a config baseURL, parameterize THAT instead.

- [ ] **Step 4: Prove both modes**

```bash
scripts/collab_e2e_run.sh                      # dev mode — regression, (a)-(e) green
E2E_SPA_MODE=prod scripts/collab_e2e_run.sh    # prod mode — THE FLIP GATE, (a)-(e) green
```
A prod-mode CSP failure shows as the Yjs socket never connecting (editor stuck "connecting") — check the browser console log the runner captures for `Refused to connect` before suspecting the backend. Timing flakes: one retry is legitimate (documented convergence-under-contention flake); report both outcomes.

- [ ] **Step 5: Commit**

```bash
git add scripts/collab_e2e_seed.py scripts/collab_e2e_run.sh frontend/e2e/collab.spec.ts
git commit -m "$(cat <<'EOF'
test(collab): two-browser e2e runs in SPA_MODE=prod under the strict CSP (3b-5d.D4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Acceptance gates

**Files:** none — verification only, per spec §6.

- [ ] **Step 1: Grouped backend sweep**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab tests/proposal_assist/test_api_annotations_read.py \
  tests/proposal_assist/test_api_annotations_insert.py tests/proposal_assist/test_api_annotations_dismiss.py \
  tests/test_editing_e2e.py tests/test_analysis_queue.py tests/test_analysis_reconcile.py \
  tests/test_analysis_pipeline.py tests/test_security_headers.py tests/test_spa_serving.py \
  tests/test_api_spec.py \
  -p no:cacheprovider -q
```
Expected: all pass (`connection refused` = infra → re-run that file isolated once).

- [ ] **Step 2: Lint** — `uv run ruff check .` → `All checks passed!`

- [ ] **Step 3: Frontend** — from `frontend/`: `pnpm exec tsc --noEmit && pnpm exec vitest run src/collab` → clean.

- [ ] **Step 4: E2E, both modes** — dev run + `E2E_SPA_MODE=prod` run, scenarios (a)-(e) green each (already proven in Task 12; re-run here as the final gate on the finished branch).

- [ ] **Step 5: Manual DoD (report, don't automate)** — paste-from-Word sanity: in the prod-mode e2e stack (or a dev session), paste a formatted snippet (bold + heading + list) from a Word-generated HTML source into the Tiptap editor and report what survives (spec backlog §5 note: untested against real Word output). This is a REPORTED observation, not a gate — the flip does not block on it.

- [ ] **Step 6: Stop for review — do NOT merge**

Report gate results and stop. Merge (local `--no-ff` into `master`) happens only after the independent whole-branch review, per house convention.

---

## Spec-coverage map (self-review)

- Spec §3.1 Component A (enable-on-read + hold-back) → Tasks 2, 3. §3.8 audit script → Task 2.
- Spec §3.2 Component B (kill-switch + §10 drain-then-truncate handover, both clobber directions, failure isolation, atomicity, scan retry, lifespan) → Tasks 1, 4.
- Spec §3.3 Component C (severance: CM6 saves 409 / Claims insert kernel-routed / spec skeleton kernel-routed; latent tools waived) → Tasks 5, 8, 6.
- Spec §3.4 Component D (get_spec + get_annotations canonical) → Tasks 3, 7.
- Spec §3.5 Component E (citekey Optional) → Task 9.
- Spec §3.6 Component F (CSP ws token, APP_BASE_URL-derived, startup fail-fast) → Task 10.
- Spec §3.7 Component G (formatting floor, no schema-shape change) → Task 11.
- Spec §6 gates incl. prod-mode e2e + Word-paste manual DoD → Tasks 12, 13.
- Spec §8 waivers → no tasks, by design (do NOT add code for waived items).
- Deviation to carry into review prompts: provenance rows on the kernel init-spec path come from the checkpoint, not `user_edit` (Task 6, spec'd §3.3).
