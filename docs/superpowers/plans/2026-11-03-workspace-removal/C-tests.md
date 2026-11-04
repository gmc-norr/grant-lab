# Test-change map for the workspace and edition removal

**Base:** `388494c8`. There is no diff under `tests/` between `dfb6dbae` and `388494c8` (READ, `git diff --stat`).

**Rules I kept to:**
- I edited, created and committed nothing.
- I ran no tests. The only pytest run was `--collect-only` with `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`. Its counts are given below as MEASURED.
- I ran one ruff check, with `--no-cache --isolated`. `git status` is unchanged afterwards (the user's two pre-existing doc changes are still the only ones).

**Labels:** READ means I read it in the file. INFERRED means I derived it and it must be measured after the src change.

---

## 0. Facts that change the plan

1. **The Task 1 grep matches 57 files, not 54.** (MEASURED) The proposal_assist-only grep gives 54. The three extra files are `test_api_admin_retention_list.py`, `test_workspace_restore_service.py` and `workspace_delete_helper.py`.

2. **The grep misses many files that depend on the workspace.** (READ) They import `tests.proposal_assist.*` helpers, read workspace settings or `phase_label`, or pin edition or CI artefacts:
   - `test_api_files.py`, `test_api_rubric.py`, `test_api_settings.py`
   - `test_profile_access_workspaces.py`, `test_migration_0083_llm_spend_setup_kind.py`
   - `test_jinja_severance.py`, `test_jinja_workspace_surface_absent.py`, `test_admin_users_jinja_surface_absent.py`
   - `admin_retention_helpers.py`
   - `test_web_filters.py` (tests `phase_label`), `test_web_filters_model_label.py`
   - `test_config.py`, `test_config_extract_timeout.py`, `test_env_example.py`
   - `test_api_me.py`, `test_board_preview_headers.py`
   - `tests/conftest.py`
   - all 30 files in `tests/fencing/`
   - `architecture/test_collab_e2e_run_cleanup.py`, `architecture/test_profile_access_single_source.py`

3. **`tests/conftest.py` will break every test unless it is edited.** (READ, lines 344-361) The autouse fixture `_close_helper_entered_lifespans` does `from tests.proposal_assist._refs_helpers import close_entered_clients` at the teardown of every test. Once `tests/proposal_assist/` is deleted, every test errors. Delete the fixture.

4. **The test suite currently runs the FULL edition.** (READ) `app_edition` defaults to `"full"` (`config.py:218`), `build_api_router` defaults to `edition="full"`, and `make_app` sets no edition. After the removal, every `create_app()` in tests serves the match surface. That is why the route counts below move.

5. **The edition grep gives 12 files, not 14.** (MEASURED) `git grep -liE "edition" -- 'tests/*.py' ':!tests/proposal_assist'`, and the same with `tests` as the pathspec, both give 12.

6. **Skips and xfails.** (READ)
   - The only skip marker outside `tests/proposal_assist` is `tests/test_secret_file_exclusion.py:46`. It needs a Docker daemon.
   - The 6 xfails are all `strict=True` and all in `tests/proposal_assist/collab/test_bridge_review_2026_10_26.py`: 4 `pytest.param` marks at lines 190/195/200/205, plus decorators at 217 and 1132.
   - `pyproject.toml` defines no markers, no `xfail_strict` and no `--strict-markers`.
   - After the removal, on a machine with Docker, the INFERRED result is 0 skipped and 0 xfailed.

7. **The migrations read `WORKSPACES_DIR` straight from `os.environ`, not from `Settings`.** (READ)
   - `0037:44`, `0066:40` and `0068:65` all do `os.environ.get("WORKSPACES_DIR", "var/workspaces")`.
   - So the conftest isolation fixture must stay, even though the `Settings.workspaces_dir` field goes.
   - 13 kept migration tests downgrade below 0068 and upgrade back, so they re-run the frozen copies: `test_alembic_v2_round_trip`, `0034`, `0035`, `0042`–`0045`, `0050`, `0052`, `0062`, `0064`, `0065`.

8. **Libraries that kept tests stop using** (INFERRED, from grep). Hand these to the dependency owner:
   - `reportlab` is used only by `test_api_references.py` and `test_citation_cloud_egress_gate.py`, plus `tests/proposal_assist`. Both are deleted.
   - `pytest-httpx` (`httpx_mock`) is used only in `tests/proposal_assist`.
   - No kept test is `async def`. `pytest-asyncio` and `asyncio_mode = "auto"` become inert but harmless; I recommend keeping them.
   - No kept test imports any library on the removal list. `PIL` appears only in `test_api_files.py`, which is deleted.

9. **Dependencies on the src and frontend agents that decide test edits:**
   - `web/api/events.py:10` imports `SSE_HEADERS` from `web/api/turns.py`. Src must move it or delete `events.py`. If `events.py` is deleted, see Task 3 → `test_auth_allowlist`.
   - `scripts/gen_architecture.py` reads `src/grant_lab/proposal_assist/fencing.py` (line 24) and `tests/fencing/inventory.py` (line 25). It finds models with a non-recursive `MODELS_PKG.glob("*.py")` (line 75). See Task 3 → `test_architecture_map_is_current`.
   - The CSP `ws_origin` token exists only for the Yjs socket (`main.py:538`, `middleware.py`). If src keeps it, no test changes. If src removes it, see Task 4.
   - `frontend/routes.json` holds `/cockpit` and the `/workspaces` backend prefix. When the frontend drops them, regenerate with `uv run python scripts/gen_routes.py`. `tests/test_gen_routes.py --check` fails until you do; its code needs no edit.

---

## TASK 1 — tests outside `tests/proposal_assist/` that use workspace code

Test counts are MEASURED by collection.

### 1a. The 57 files the grep matches

| # | File | Tests | Action | Reason or detail |
|---|---|---|---|---|
| 1–30 | `tests/fencing/**` (whole directory, including the 19 files the grep does not match) | 255 | **DELETE dir** | See note A below |
| 31 | `architecture/test_branch_entity_invariants.py` | 5 | DELETE | Pins `proposal_assist.branches` and `branch_entity_backfill`. Tell the backfill owner: invariant 1 (every ref has a row after backfill) could move into their frozen-copy 0068 test. |
| 32 | `architecture/test_edition_surface.py` | 13 | REWRITE → `test_route_surface.py` | Task 3 |
| 33 | `architecture/test_match_edition_process_cut.py` | 7 | DELETE | Task 3 |
| 34 | `architecture/test_no_import_cycles.py` | 7 | EDIT | Task 3 |
| 35 | `test_analysis_insert.py` | 9 | DELETE | Workspace claim analysis |
| 36 | `test_analysis_invalidation.py` | 6 | DELETE | Same |
| 37 | `test_analysis_pipeline.py` | 16 | DELETE | Same |
| 38 | `test_analysis_queue.py` | 19 | DELETE | Same |
| 39 | `test_analysis_reconcile.py` | 3 | DELETE | Same |
| 40 | `test_api_admin_drafting.py` | 7 | DELETE | Tests `admin.drafting_router`, which goes |
| 41 | `test_api_admin_retention_inspect.py` | 28 | DELETE | `admin_retention` router goes |
| 42 | `test_api_admin_retention_list.py` | 8 | DELETE | Same |
| 43 | `test_api_admin_retention_restore.py` | 10 | DELETE | Same |
| 44 | `test_api_citation_audit.py` | 9 | DELETE | `citation_audit` router goes |
| 45 | `test_api_citation_audit_defect2.py` | 2 | DELETE | Same |
| 46 | `test_api_export.py` | 10 | DELETE | `export` router goes |
| 47 | `test_api_references.py` | 23 | DELETE | `references` router goes. Its SSRF tests are for reference fetch only. |
| 48 | `test_api_references_defect1.py` | 4 | DELETE | Same |
| 49 | `test_api_session_revocation.py` | 8 | DELETE ⚠ live rule | Covers the 5 `signal_user_revocation` wirings, which go. The `session_epoch` bump on each route is pinned elsewhere: see "Live rules" at the end. |
| 50 | `test_api_spec.py` | 15 | DELETE | `workspaces` router |
| 51 | `test_api_spec_lock.py` | 11 | DELETE | Same |
| 52 | `test_api_turns.py` | 18 | DELETE | `turns` router. Its two revocation tests are for the turn SSE stream, which goes. |
| 53 | `test_api_workspaces.py` | 64 | DELETE | `workspaces` router |
| 54 | `test_auth_domain_policy.py` | 7 | TRIM ⚠ live rule | See 1c |
| 55 | `test_citation_cloud_egress_gate.py` | 10 | DELETE | The citation LLM and its settings go |
| 56 | `test_citations_bare_markers.py` | 4 | DELETE | `proposal_assist.citations` |
| 57 | `test_editing_commit_mutex.py` | 8 | DELETE | Workspace commit mutex and git |
| 58 | `test_editing_e2e.py` | 1 | DELETE | Workspace |
| 59 | `test_editing_enqueue_race.py` | 1 | DELETE | Workspace mutex race |
| 60 | `test_editing_save.py` | 12 | DELETE | Workspace |
| 61 | `test_editing_sectionizer.py` | 9 | DELETE | `proposal_assist.editing` |
| 62 | `test_sectionizer_fences.py` | 4 | DELETE | Same |
| 63 | `test_embeddings_truncation_defect.py` | 7 | TRIM | See 1c |
| 64 | `test_lifecycle_generation.py` | 5 | DELETE | `grant_lab.workspaces` |
| 65 | `test_main.py` | 5 | TRIM | See 1c |
| 66 | `test_migration_0037_backfill.py` | — | **Other owner** | Noted only. It also imports `tests.proposal_assist.conftest.make_user` and `make_workspace`. |
| 67 | `test_pool_exhaustion_event_loop.py` | 3 | TRIM ⚠ live rule | See 1c |
| 68 | `test_profiles_bootstrap_worker.py` | 7 | EDIT | See 1d |
| 69 | `test_scheduler.py` | 15 | TRIM | See 1c |
| 70 | `test_scheduler_digest.py` | 3 | EDIT | See 1d |
| 71 | `test_scheduler_embedding_cursor.py` | 7 | EDIT | See 1d |
| 72 | `test_scheduler_single_instance.py` | 11 | EDIT ⚠ live rule (single instance) | See 1d |
| 73 | `test_workspace_restore_acceptance.py` | 13 | DELETE | Workspace restore |
| 74 | `test_workspace_restore_service.py` | 11 | DELETE | `grant_lab.workspaces` |
| 75 | `test_workspaces_service.py` | 9 | DELETE | Same |
| 76 | `tests/workspace_delete_helper.py` | 0 | DELETE | Helper used only by deleted files |

**Note A — `tests/fencing/` (READ).** Every gate in it fences the workspace:
- git sinks in `proposal_assist` and `web/api/workspaces.py`
- the commit mutex and FenceStamp
- the tool import boundary (`proposal_assist.ports`)
- the `llm_spend` ledger writer (`proposal_assist/spend.py`)

Several gates read pins that live in deleted files. For example, `test_f3e1a2_slice` pins `web/api/workspaces.py::post_init_spec._git`, and `test_fence_coverage` matches the inventory against the tree.

Two parts outlive the removal, both vacuously:
- `sink_guard`'s generic clauses (no `os.system`, no `shell=True`, no git library). Live src has zero subprocess calls today (READ, grep finds comments only).
- `spend_guard`'s one live pin, `ranking/remote_attempts.py::record_attempt`. The ranking cap is pinned by `test_ranking_remote_attempts.py`, `test_ranking_worker.py` and `test_config_escalation_settings.py`.

So: DELETE. Optionally, port the three `sink_guard` clauses into a small new test if you want them kept.

**Note B.** `scripts/gen_architecture.py` must stop reading `tests/fencing/inventory.py` and `proposal_assist/fencing.py` in the same commit as the deletion.

### 1b. Files the grep misses

| File | Tests | Action | Reason or detail |
|---|---|---|---|
| `tests/conftest.py` | — | EDIT | Task 4 |
| `tests/admin_retention_helpers.py` | 0 | DELETE | Used only by deleted retention tests |
| `test_api_files.py` | 18 | DELETE | `files` router; imports `PIL` and `tests.proposal_assist._refs_helpers` |
| `test_api_rubric.py` | 16 | DELETE | `rubric` router |
| `test_api_settings.py` | 19 | DELETE | Workspace `settings` router |
| `test_profile_access_workspaces.py` | 8 | DELETE | The profile-read rule on `POST /api/workspaces`. The route goes, and so does its pin in `architecture/test_profile_access_single_source.py` (Task 3). |
| `test_jinja_severance.py` | 2 | DELETE | Guards imports of `proposal_assist/web/*`, which is all deleted |
| `test_jinja_workspace_surface_absent.py` | 1 | DELETE | Covered by `REMOVED_PREFIXES` in the new `test_route_surface.py`, which includes `/workspaces` |
| `test_admin_users_jinja_surface_absent.py` | 1 | EDIT | Delete the line `"/api/admin/drafting",` from the "/api routes present" list (line 38). Keep `"/admin/drafting"` in the absent list. |
| `test_migration_0083_llm_spend_setup_kind.py` | 1 | EDIT | See 1d |
| `test_web_filters.py` | 2 | DELETE | Tests only `phase_label`, which the spec removes |
| `test_web_filters_model_label.py` | 10 | TRIM | Delete `test_the_configured_drafting_model_has_a_label` (lines 41-48). `PROPOSAL_ASSIST_LLM_MODEL` goes; the `Settings` import stays, the remote-rank test uses it. |
| `test_config_extract_timeout.py` | 2 | DELETE | `reference_extract_timeout_seconds` is a workspace setting |
| `test_config.py`, `test_env_example.py`, `test_api_me.py` | | | Task 4 |
| `test_board_preview_headers.py` | | | Task 2 |
| `test_spa_serving.py` | 10 | No functional change | Line 213 `/workspaces/totally-unknown` → 404 still holds; a path not owned by the SPA is a 404. Optionally reword the comment at line 212. The CSP assertion at 109-117 is conditional, see Task 4. |
| Workspace-table migration tests: `0035`, `0040`, `0041`, `0052`, `0062`, `0064`, `0065`, `0067`, `0069` | — | KEEP unchanged | They use `from grant_lab.db.models import Workspace, LlmSpend, CollabArtifactState, …`. They need `db/models/__init__` to keep re-exporting the moved models. |
| `test_schema_drift.py` | — | KEEP | G3. `from grant_lab.db.models import Base` |

### 1c. TRIM details

**`test_auth_domain_policy.py`** ⚠ live rule (domain allowlist). 7 → 6 tests.
- Delete lines 141-163: the comment `# --- the collab socket (full edition) ---` and `test_the_collab_socket_applies_the_same_rule`.
- Imports are unchanged. `pytest` is still used by `parametrize`, and `SessionSigner` by `_session`.
- What remains covers password login, existing sessions, the admin exemption and demotion over HTTP. All of that is live.

**`test_embeddings_truncation_defect.py`.** 7 → 3 tests.
- Keep lines 1-88: `_client_returning` and the three `OllamaEmbedClient.embed()` tests. They are live, `grant_lab.embeddings.client`.
- Delete from line 90 (`# OllamaEmbedderAdapter wraps …`) to end of file. That removes `test_ollama_embedder_adapter_embed_returns_embed_result`, `test_fake_embedder_returns_embed_result`, `_TruncatingEmbedder`, `test_ingest_stores_truncated_prefix_when_embed_shrinks` and `test_ingest_no_truncation_preserves_full_chunk_text`.
- Rewrite the docstring to: `"""OllamaEmbedClient.embed() returns EmbedResult; after a shrink-and-retry, embedded_chars is the truncated length."""`

Import block before:
```python
from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from grant_lab.db.models import ReferenceChunk
from grant_lab.embeddings.client import EmbedResult, OllamaEmbedClient
from grant_lab.proposal_assist.references.embed import FakeEmbedder, OllamaEmbedderAdapter
from grant_lab.proposal_assist.references.ingest import ingest_bytes
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace
```
After:
```python
from __future__ import annotations

import json

import httpx

from grant_lab.embeddings.client import EmbedResult, OllamaEmbedClient
```

**`test_main.py`.** 5 → 4 tests.
- Delete `test_create_app_exposes_citation_service` (lines 66-82). No import changes.
- What remains is email-sender selection, which is live.

**`test_pool_exhaustion_event_loop.py`** ⚠ live rule (`make_engine` `pool_timeout=5`). 3 → 1 test.
- Keep only `test_make_engine_fails_the_request_after_five_seconds_not_thirty` (lines 235-270).
- Delete `_TINY_POOL_TIMEOUT`, `_run_beside_a_ticker`, `_Grabber`, `_seed_running_job`, `_open_stream` and both `async` tests. They drive `web.api.turns._json_turn_stream`.
- Replace the docstring with: `"""Review 2026-10-26 A4: make_engine sets pool_timeout=5, so a request that cannot get a pooled connection fails in seconds instead of parking for SQLAlchemy's 30 s default."""`
- Optionally `git mv` the file to `tests/test_make_engine_pool_timeout.py`.

Import block before:
```python
import asyncio
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

from grant_lab.db.session import make_engine, make_session_factory
```
After:
```python
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

from grant_lab.db.session import make_engine
```

**`test_scheduler.py`.** 15 → 8 tests.
- **Delete:**
  - `test_build_scheduler_includes_turn_reaper_job` (227-285)
  - `_ANALYSIS_JOB_IDS`, `_build_with_analysis_deps` and the 4 `…analysis_ticks…` tests (374-442)
  - `test_build_scheduler_wires_collab_checkpoint_job` (486-532)
  - `test_build_scheduler_omits_collab_checkpoint_when_disabled` (535-563)
- **In `_settings_env`:** delete `monkeypatch.setenv("DRAFTING_ADAPTER", "fake")`.
- **In each of the 7 kept tests that build a scheduler** (`…one_job_per_active_adapter`, `…includes_rate_limit_sweep…`, `…omits_rate_limit_sweep…`, `…includes_rank_drain_job`, `test_auto_publish_tick…`, `test_daily_cron_jobs…`, `test_digest_tick_also_sends_board_alerts`), delete exactly:
  - the line `from grant_lab.proposal_assist.drafting.factory import build_workspace_controller`
  - the block `workspace_controller = build_workspace_controller(...)`
  - the kwarg `workspace_controller=workspace_controller,`
  - in `test_digest_tick_also_sends_board_alerts` only, also `monkeypatch.setenv("DRAFTING_ADAPTER", "fake")`
- **In `test_auto_publish_tick…`'s docstring:** change "Same idiom as `test_build_scheduler_includes_turn_reaper_job` above" to "Calling the closure with the work patched out is the only way to prove the wiring".
- These edits work whether or not src has already dropped the parameter.

Import block before:
```python
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
```
After (`timedelta` and `IntervalTrigger` were used only by the turn_reaper test):
```python
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
```

Example of the edited form:
```python
def test_build_scheduler_returns_one_job_per_active_adapter(_settings_env):
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""
    settings = Settings()

    def session_factory():  # not invoked because we don't start
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    ...unchanged...
```

Optional, and only after src lands: replace the `issubset` check with an exact set. INFERRED, measure it first. `_NullAlerter` has no `.sender`, so `ingest:stale_check` is absent; there are no limiters and no email client.
```python
assert actual_ids == {"ingest:eu_portal", "ingest:vinnova", "rank:drain", "golden_eval:weekly",
                      "embeddings:refresh-missing", "ranking:reconcile", "boards:auto-publish",
                      "profile_bootstrap_tick"}
```

### 1d. EDIT details

**`test_profiles_bootstrap_worker.py::test_scheduler_registers_bootstrap_tick`** (146-188). Delete:
- `from grant_lab.proposal_assist.drafting.factory import build_workspace_controller`
- the env entry `"DRAFTING_ADAPTER": "fake",`
- `wc = build_workspace_controller(settings=settings, session_factory=session_factory)`
- the kwarg `workspace_controller=wc,`

**`test_scheduler_digest.py`.** In `_settings_env`, delete the `DRAFTING_ADAPTER` line. The new `_build`:
```python
def _build(session_factory, *, with_digest):
    settings = Settings()
    kwargs = dict(
        session_factory=session_factory,
        settings=settings,
        alerter=MagicMock(),
        ollama_url="http://ollama",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
        kwargs["digest_settings"] = MagicMock(
            digest_dry_run=False, admin_dashboard_url="http://app/"
        )
    return build_scheduler(**kwargs)
```

**`test_scheduler_embedding_cursor.py`.**
- Delete `DRAFTING_ADAPTER` at line 60.
- In the fixtures `embeddings_tick` (~128-142), `embeddings_tick_calls_fail` (~257-271) and `embeddings_tick_profiles_fail` (~497-511), delete:
  - the import line
  - `controller = build_workspace_controller(settings=settings, session_factory=pg_session_factory)`
  - `workspace_controller=controller,`
- Fix the docstring reference "tests/test_scheduler.py:423-436 uses for the collab checkpoint job" to read "the pattern tests/test_scheduler.py uses for boards:auto-publish".

**`test_scheduler_single_instance.py`** ⚠ live rule (single instance). Delete `DRAFTING_ADAPTER` at line 44. The new `_build`:
```python
def _build(settings):
    def session_factory():
        return None

    return build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
```
All lock, watchdog and lifespan tests are unchanged. Executor sizing is unchanged as long as src keeps `SCHEDULER_EXECUTOR_MAX_WORKERS >= 30`.

**`test_migration_0083_llm_spend_setup_kind.py`.** Imports before:
```python
from alembic import command
from tests.proposal_assist.conftest import make_workspace
```
After:
```python
from alembic import command
from grant_lab.db.models import User, Workspace
```
In the test, replace:
```python
    with pg_session_factory() as s:
        ws_id = make_workspace(s).id
        s.commit()
```
with:
```python
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@umu.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="Test workspace", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        ws_id = ws.id
        s.commit()
```
If the backfill owner adds a shared row helper for the 0037, 0066 and 0068 tests, this file can use it instead.

---

## TASK 2 — tests about the edition split (12 files, MEASURED)

| File | What it pins about the edition | Action |
|---|---|---|
| `architecture/test_edition_surface.py` | Match drops the full-only routers; full baseline of 120 paths; match refuses `DRAFTING_ENABLED` | REWRITE (Task 3) |
| `architecture/test_match_edition_process_cut.py` | A match boot loads 0 proposal_assist modules and no pycrdt/sdk/prosemirror; full loads more than 100 | DELETE (Task 3) |
| `architecture/test_spa_edition_match.py` | `edition.txt` must agree with `APP_EDITION`, or boot is refused | DELETE (Task 3) |
| `architecture/test_package_spa.py` | `package_spa.sh` reads the edition from the bundle gate; the Dockerfile `ARG VITE_APP_EDITION` | TRIM (Task 3) |
| `architecture/test_match_gates_are_wired.py` | CI runs `build:match && check:bundle` and `VITE_APP_EDITION=match … virtual-router.test.ts` | TRIM (Task 3) |
| `architecture/test_auth_allowlist.py` | Parametrized over `edition` in `["full","match"]`; sets `DRAFTING_ENABLED` | EDIT (Task 3) |
| `architecture/test_boards_permission_single_source.py` | Reads `full_edition_routes.txt`; comments cite `test_edition_surface.py` | EDIT (Task 3) |
| `architecture/test_decision_routes_require_a_profile.py` | Docstring cites `full_edition_routes.txt` | EDIT, docstring only (Task 3) |
| `architecture/test_no_import_cycles.py` | Docstring history only | EDIT for the threshold (Task 3); the docstring can stay |
| `test_auth_domain_policy.py` | The collab-socket test is labelled "(full edition)" | TRIM (1c) |
| `test_board_preview_headers.py` ⚠ live rule (clickjacking headers) | Line 25: `spa_serving.mount_spa(app, spa_mode="prod", edition="match")` | EDIT: `spa_serving.mount_spa(app, spa_mode="prod")`. Must land with src dropping the `edition` parameter. |
| `test_api_calls_feed_behavior.py` | Docstring line 898: "the full edition's unranked archive" | No code change. Optionally reword to "the no-profile archive". |

---

## TASK 3 — architecture tests

There are 17 `test_*.py` files plus `full_edition_routes.txt`. Three are unaffected: `test_criterion_vocabulary`, `test_ranking_job_terminality` and `test_investigations_index` (READ, 0 hits). `test_investigations_index` pins only that `notes/*.md` and `reviews/*.md` are linked from `INVESTIGATIONS.md`, with no counts. Piece 1 deletes no note or review, so it needs no change.

### `test_edition_surface.py` → REWRITE as `test_route_surface.py`. Live rule: route-surface drift.

Steps:
1. `git mv tests/architecture/test_edition_surface.py tests/architecture/test_route_surface.py`
2. `git mv tests/architecture/full_edition_routes.txt tests/architecture/app_routes.txt`
3. Regenerate the baseline after the src removal:
```
DATABASE_URL=postgresql+psycopg://u:p@localhost:5999/nope SESSION_SECRET=test-secret-at-least-16-chars \
ALLOWED_EMAIL_DOMAINS=umu.se APP_BASE_URL=http://testserver SPA_MODE=disabled uv run python -c \
"from grant_lab.config import Settings; Settings.model_config['env_file'] = None; \
from grant_lab.main import create_app; print('\n'.join(sorted({r.path for r in create_app().routes})))" \
> tests/architecture/app_routes.txt
```
   INFERRED: 51 paths. That is today's 120 minus the 69 paths under the removed prefixes; I computed it from the current file. Measure it and put the measured number in `_BASELINE_SIZE`.
4. Update the references (READ):
   - `test_boards_permission_single_source.py:78` → `_BASELINE = Path(__file__).with_name("app_routes.txt")`. Its comments at 74, 91, 289 and 308-309 change `test_edition_surface.py` to `test_route_surface.py` and `full_edition_routes.txt` to `app_routes.txt`; drop the `:135-141` and `:269` line references.
   - `test_decision_routes_require_a_profile.py:27`, docstring → `tests/architecture/app_routes.txt`.
   - Comments in `src/grant_lab/main.py:969`, `web/api/__init__.py:67`, `web/api/admin.py:35` and `frontend/e2e/match.spec.ts:19` cite the old name. They are owned by the src and frontend agents.
5. In `_ROUTER_TUPLE`, put the name src gives the module-level router tuple in `web/api/__init__.py`. Use `"_ALWAYS"` if src keeps that name.

New file content:
```python
"""The app's route surface: what it serves, and that the removed workspace half is absent.

There is one build. The proposal workspace and the match/full edition split left
master on 2026-11-03 (docs/superpowers/specs/2026-11-03-workspace-removal-design.md);
the workspace is frozen on the `workspace` branch. Nothing here may serve any of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from starlette.routing import WebSocketRoute

# Prefixes the removed workspace owned. No route may be registered under any of them.
# A blocklist only catches an ANTICIPATED return; the baseline equality below is the
# self-maintaining half (any unintended route fails it).
REMOVED_PREFIXES = (
    "/workspaces",
    "/api/workspaces",
    "/api/ws/",
    "/api/turns",
    "/api/annotations",
    "/api/rubric",
    "/api/references",
    "/api/citation-audit",
    "/api/export",
    "/api/files",
    "/api/admin/drafting",
    "/api/admin/workspaces",
)


@pytest.fixture
def _base_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5999/nope",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "SPA_MODE": "disabled",
    }.items():
        monkeypatch.setenv(k, v)


def _paths(app) -> set[str]:
    """HTTP route paths only."""
    return {r.path for r in app.routes if hasattr(r, "methods")}


def _ws_paths(app) -> set[str]:
    """WebSocket routes. FastAPI's APIWebSocketRoute subclasses Starlette's
    WebSocketRoute and carries no `.methods`, so a methods filter drops it."""
    return {r.path for r in app.routes if isinstance(r, WebSocketRoute)}


@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_no_removed_workspace_route_is_registered(make_app, monkeypatch, _base_env, spa_mode):
    monkeypatch.setenv("SPA_MODE", spa_mode)
    app = make_app()
    leaked = sorted(p for p in _paths(app) | _ws_paths(app) if p.startswith(REMOVED_PREFIXES))
    assert leaked == [], f"a removed workspace route is registered: {leaked}"


@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_no_websocket_route_is_registered(make_app, monkeypatch, _base_env, spa_mode):
    """Live editing was the only websocket. A new one is a product decision."""
    monkeypatch.setenv("SPA_MODE", spa_mode)
    assert _ws_paths(make_app()) == set()


def test_the_websocket_helper_can_see_a_websocket_route():
    """Guards the guard: without this, the test above passes on a helper that
    sees nothing."""
    app = FastAPI()

    @app.websocket("/probe")
    async def _probe(ws: WebSocket):  # pragma: no cover - never connected
        await ws.close()

    assert _ws_paths(app) == {"/probe"}


def test_the_app_serves_the_golden_set_api(make_app, _base_env):
    assert "/api/admin/golden-set" in _paths(make_app())


def test_the_app_serves_the_release_1_surface(make_app, _base_env):
    paths = _paths(make_app())
    for required in ("/api/calls", "/api/profiles", "/api/me", "/api/admin/users",
                     "/api/boards", "/api/public/boards/{slug}", "/healthz"):
        assert required in paths, f"the app dropped {required}"


def test_leftover_edition_settings_change_nothing(make_app, monkeypatch, _base_env):
    """The live .env still says APP_EDITION=match and DRAFTING_ENABLED=false, and an
    older one may say full/true. Settings ignores unknown keys (extra="ignore"):
    neither may change the surface or stop the app booting."""
    from grant_lab.config import Settings

    assert "app_edition" not in Settings.model_fields
    assert "drafting_enabled" not in Settings.model_fields
    baseline = {r.path for r in make_app().routes}
    monkeypatch.setenv("APP_EDITION", "full")
    monkeypatch.setenv("DRAFTING_ENABLED", "true")
    assert {r.path for r in make_app().routes} == baseline


# The app's complete route set (unique PATHS, not operations; HTTP, websocket and
# mounts). Regenerate ONLY when a route change is intended, and bump
# _BASELINE_SIZE BY HAND in the same commit, so a regeneration alone cannot wave
# through a route that arrived without intent:
#
#   DATABASE_URL=postgresql+psycopg://u:p@localhost:5999/nope \
#   SESSION_SECRET=test-secret-at-least-16-chars ALLOWED_EMAIL_DOMAINS=umu.se \
#   APP_BASE_URL=http://testserver SPA_MODE=disabled uv run python -c \
#     "from grant_lab.config import Settings; Settings.model_config['env_file'] = None; \
#      from grant_lab.main import create_app; \
#      print('\n'.join(sorted({r.path for r in create_app().routes})))" \
#     > tests/architecture/app_routes.txt
#
# 2026-11-03, workspace removal: 120 -> <measured>. The old full_edition_routes.txt
# held the FULL edition; every path that left is under REMOVED_PREFIXES.
_BASELINE = Path(__file__).with_name("app_routes.txt")
_BASELINE_SIZE = 51  # INFERRED — replace with the measured count


def test_the_route_set_matches_the_committed_baseline(make_app, _base_env):
    live = sorted({r.path for r in make_app().routes})
    expected = _BASELINE.read_text().split()
    added = sorted(set(live) - set(expected))
    removed = sorted(set(expected) - set(live))
    assert (added, removed) == ([], []), (
        f"the route surface moved: +{added} -{removed}\n"
        f"If intended, regenerate {_BASELINE.name} and bump _BASELINE_SIZE in the same commit."
    )


def test_the_baseline_is_not_empty():
    assert len(_BASELINE.read_text().split()) == _BASELINE_SIZE


# The routers web/api/__init__.py mounts, pinned by NAME: adding one is a product
# decision, made in two places so a reviewer sees it.
_ROUTER_TUPLE = "_ROUTERS"  # the module-level tuple name in web/api/__init__.py
_ROUTERS = [
    "auth.router",
    "me.router",
    "admin.router",
    "admin_users.router",
    "calls.router",
    "profiles.router",
    "boards.router",
    "public_boards.router",
    "admin_golden_set.router",
]


def _tuple_elements(name: str) -> list[str]:
    src = (Path(__file__).resolve().parents[2] / "src/grant_lab/web/api/__init__.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), f"{name} is not a tuple literal"
            return [ast.unparse(e) for e in node.value.elts]
    raise AssertionError(f"no module-level tuple {name} in web/api/__init__.py")


def test_the_mounted_routers_are_exactly_the_reviewed_set():
    assert _tuple_elements(_ROUTER_TUPLE) == _ROUTERS
```
This takes the file from 13 collected tests to 11. Two of the new tests run in two SPA modes each.

Deleted with the old file:
- `test_match_edition_mounts_no_collab_websocket`, `test_full_edition_still_mounts_the_collab_websocket`
- `test_full_edition_is_unchanged`, `test_match_edition_refuses_drafting_enabled`, `test_match_edition_accepts_drafting_disabled`
- `test_match_edition_drops_only_excluded_routes`, `test_the_full_only_routers_are_exactly_the_reviewed_set`

### `test_match_edition_process_cut.py` → DELETE (7 tests)

The package and its libraries cease to exist, so "a match boot loads 0 proposal_assist modules" is true by construction. The libraries are uninstalled (G5). In CI, `uv sync --frozen` would make any kept import of them fail the whole suite at collection. The full-edition comparison tests cannot run at all. The `drafting_idle_sweep` job goes with the scheduler code.

### `test_spa_edition_match.py` → DELETE (10 tests)

It pins a bundle-vs-backend edition agreement, and there is now one build. `spa_serving.packaged_edition` and the `edition` parameter of `mount_spa` go in src. "Prod must ship a built SPA" stays pinned by `test_spa_serving.py::test_prod_mode_missing_build_fails_fast` (READ).

### `test_package_spa.py` → TRIM. 17 → 10 tests. Live rules: validate before delete; the served tree has one writer.

- **Delete:**
  - `test_a_full_bundle_asked_for_as_match_is_refused`
  - `test_the_edition_comes_from_the_bundle_not_the_variable` (2 parametrized cases)
  - `test_a_full_bundle_packages_as_full`
  - `test_the_bundle_gate_rejects_a_manifest_containing_an_excluded_module`
  - `test_the_bundle_gate_accepts_a_clean_manifest`
  - `test_the_container_build_does_not_hard_code_the_edition`
  - the constants `CLEAN` and `DIRTY`, the helper `_gate`, and the imports `json` and `pytest`
- **Today's repo-root check uses `scripts/check_match_bundle.mjs`, which is deleted** (READ, `package_spa.sh:51`). The rewritten script needs a new marker; `ROOT_MARKER` below must equal it. The script must keep the messages `no build at` and `is not the repo root`.

New top of file, replacing lines 1-118:
```python
"""scripts/package_spa.sh must validate BEFORE it deletes, and must stay the only
writer of the served tree. The script derives ROOT from BASH_SOURCE, so a fixture
tree is enough: copy the script in, fake a dist, and run it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "do-not-delete-me.txt"
# The file package_spa.sh's repo-root identity check requires (`[ -f "$ROOT/<this>" ]`).
# MUST match the script; it was scripts/check_match_bundle.mjs until 2026-11-03.
ROOT_MARKER = "pyproject.toml"


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "package_spa.sh", tmp_path / "scripts" / "package_spa.sh")
    (tmp_path / "scripts" / "package_spa.sh").chmod(0o755)
    (tmp_path / ROOT_MARKER).write_text("")
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    spa = tmp_path / "src" / "grant_lab" / "web" / "spa"
    spa.mkdir(parents=True)
    (spa / SENTINEL).write_text("the previously served bundle")
    return tmp_path


def _served(root: Path) -> Path:
    return root / "src" / "grant_lab" / "web" / "spa"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "package_spa.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root)},
        check=False,
    )


def test_a_missing_build_is_refused_and_the_served_tree_survives(tmp_path):
    """Move `rm -rf` above the checks and this goes red."""
    root = _tree(tmp_path)
    (root / "frontend" / "dist" / "index.html").unlink()
    spa = _served(root)
    before = sorted(p.name for p in spa.iterdir())
    out = _run(root)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "no build at" in out.stderr
    assert spa.is_dir(), "the served directory was deleted on the way to failing"
    assert sorted(p.name for p in spa.iterdir()) == before
    assert (spa / SENTINEL).read_text() == "the previously served bundle"


def test_a_built_bundle_packages_and_replaces_the_previous_one(tmp_path):
    root = _tree(tmp_path)
    out = _run(root)
    assert out.returncode == 0, out.stdout + out.stderr
    spa = _served(root)
    assert (spa / "index.html").exists()
    assert not (spa / SENTINEL).exists(), "the previous bundle must not survive"
    assert not (spa / "edition.txt").exists(), "one build: no edition marker"


def test_it_refuses_a_tree_that_is_not_the_repo_root(tmp_path):
    root = _tree(tmp_path)
    (root / ROOT_MARKER).unlink()
    out = _run(root)
    assert out.returncode == 1
    assert "is not the repo root" in out.stderr
    assert (_served(root) / SENTINEL).exists(), "refused, but deleted the served tree first"
```

Writer section (lines 156 onward):
- Keep `_SERVED`, `_WRITE`, `_ASSIGN`, `_scanned_files`, `_writer_lines` and all the detector tests.
- Rewrite the comment block above `_SERVED`: drop the `edition.txt` and `collab_e2e_run.sh` history. It is enough to say "every served tree must come through the script's checks".
- In `test_the_scan_set_is_not_empty`: `assert {"package_spa.sh", "ci.yml", "Dockerfile"} <= names, names`. Add the new e2e runner script's filename here if one replaces `collab_e2e_run.sh`.
- `test_no_other_writer_of_the_served_tree`: change the message to "…without going through scripts/package_spa.sh, so its build checks do not govern what they leave behind".
- `test_the_detector_sees_the_real_writer` still needs at least 3 flagged lines including `rm -rf` and `cp -R`. That holds if the script keeps `rm -rf "$SPA"`, `mkdir -p "$SPA"` and `cp -R "$DIST"/. "$SPA"/`.

### `test_match_gates_are_wired.py` → TRIM, plus new pins. 18 → 16 tests, plus the new ones.

- **References** (READ): it does not mention `collab.spec.ts`, `dashboard.spec.ts`, `check_match_bundle.mjs` or the `e2e` job.
  - It does pin `pnpm run build:match && pnpm run check:bundle` (line 128) and `VITE_APP_EDITION=match pnpm run test src/virtual-router.test.ts` (line 136).
  - It pins the CI job name `match-e2e` (lines 141 and 471), which stays.
  - It also pins `bash scripts/package_spa.sh` and the `e2e:match` script containing `E2E_MODE=match` and `e2e/match.spec.ts`, which stay.
- **Delete:** `test_gate_1_the_bundle_check_runs_in_ci` (125-128) and `test_the_match_direction_of_the_router_alias_runs_in_ci` (131-136). Rewrite the module docstring lines 3-12 to drop "gate 1" and the router alias, and name `e2e/match.spec.ts`, `e2e/smoke.spec.ts` and `test_package_spa.py`.
- **`REQUIRED_SCENARIOS`** (36 titles) and **`expects == 185`** (line 281): unchanged only if `match.spec.ts` is unchanged. Line 287 `assert "/cockpit" in src` is the live code of the scenario "no rendered link anywhere in release 1 points into the excluded half". If the frontend drops or renames that scenario, or the excluded-route one, update `REQUIRED_SCENARIOS`, re-measure 185 with this file's regex, and drop or adjust the `/cockpit` assert, all in the same diff.
- **`test_the_run_parser_finds_commands`** needs more than 20 lines. MEASURED 42 enforcing run lines now, about 40 afterwards (INFERRED). No change.
- **Add, in the same diff as `frontend/e2e/smoke.spec.ts` and the CI `e2e` job rewrite.** These are G7's wiring pins. `E2E_SPECS` is a step `env:` key, so `_run_lines()` cannot see it, which is why the test reads `env` directly. Fill the four titles from the spec file.
```python
SMOKE_SPEC = REPO / "frontend" / "e2e" / "smoke.spec.ts"
# The only browser checks that hit a real FastAPI + Postgres (match.spec.ts mocks /api
# in-page). Keep in step with smoke.spec.ts, by exact title.
SMOKE_SCENARIOS = [
    "<<title: / loads with no critical a11y violations>>",
    "<<title: an unknown path gets FastAPI's JSON 404, not index.html>>",
    "<<title: logout through the UI lands on /login>>",
    "<<title: opening a call detail page stays in the SPA>>",
]


def test_the_real_backend_smoke_runs_in_ci():
    job = yaml.safe_load(CI.read_text())["jobs"]["e2e"]
    assert _enforces(job), "the e2e job carries an if: or continue-on-error"
    runs_smoke = [
        s for s in job.get("steps", [])
        if s.get("run") and _enforces(s)
        and "e2e/smoke.spec.ts" in str(s.get("env", {})) + str(s["run"])
    ]
    assert len(runs_smoke) == 1, job.get("steps")
    assert not any("collab.spec.ts" in str(s) for s in job.get("steps", []))


def test_the_smoke_spec_declares_every_required_scenario():
    src = "\n".join(
        ln for ln in SMOKE_SPEC.read_text().splitlines() if not ln.strip().startswith("//")
    )
    titles = re.findall(r'^test\("((?:[^"\\]|\\.)*)"', src, re.M)
    assert sorted(titles) == sorted(SMOKE_SCENARIOS), titles


def test_every_async_matcher_in_the_smoke_spec_is_awaited():
    src = "\n".join(
        ln for ln in SMOKE_SPEC.read_text().splitlines() if not ln.strip().startswith("//")
    )
    assert unawaited_async_matchers(src) == []
```
  The spec must use the `test("…")` double-quoted form, as `dashboard.spec.ts` does today.

### `test_auth_allowlist.py` → EDIT. 10 → 5 tests, or 6 with the optional addition. ⚠ Live rule: authentication.

- `_env`: delete the `"DRAFTING_ENABLED": "false",` entry and its 2-line comment.
- `test_no_unlisted_route_is_public`:
  - delete `@pytest.mark.parametrize("edition", ["full", "match"])`
  - drop the `edition` parameter
  - delete `monkeypatch.setenv("APP_EDITION", edition)`
  - change the message to `f"SPA_MODE={spa_mode}: …"`
- `test_the_allowlist_has_no_dead_entries`: delete `monkeypatch.setenv("APP_EDITION", "full")`.
- `UNJUDGEABLE_ROUTES`: delete `"/api/ws/workspaces/{workspace_id}/{artifact}"` and its comment (lines 134-146). Change "exactly these six" (line 126) to "exactly these five".
- `test_the_walk_judges_every_route_it_does_not_pin`: same edition removal as `test_no_unlisted_route_is_public`.
- Delete `test_the_exempted_websocket_is_covered_somewhere_else` (187-201) and the now-unused `from pathlib import Path`.
- Recommended addition. It is what would have caught the stale websocket pin.
```python
def test_the_unjudgeable_pins_have_no_dead_entries(make_app, monkeypatch, _env):
    """An exemption that outlives its route stops being reviewed. dev mounts the docs."""
    monkeypatch.setenv("SPA_MODE", "dev")
    dead = sorted(UNJUDGEABLE_ROUTES - _unjudgeable_routes(make_app()))
    assert dead == [], f"exemptions with no matching route: {dead}"
```
- Conditional: if src deletes `web/api/events.py`, delete `("GET", "/events/ping")` from `PUBLIC_ROUTES`, or the dead-entry test fails. Also delete `tests/test_events_ping.py` (4 tests).

### `test_boards_permission_single_source.py` → EDIT

Rename the baseline only (see `test_route_surface` above). The rule (every `{board_id}` route calls `_board_for_edit`) is live and unchanged. The `/api/boards` paths are in the new baseline (READ: 8 board paths kept).

### `test_branch_entity_invariants.py` → DELETE (see Task 1, row 31)

### `test_decision_routes_require_a_profile.py` → EDIT, docstring line 27 only

It reads `frontend/src/api/schema.gen.ts`. The regenerated schema keeps the five decision paths and seven operations, so the logic is unchanged.

### `test_no_import_cycles.py` → EDIT. Live rule: no module-level import cycles.

- Line 213 `assert len(files) > 200` would fail.
  - MEASURED: 311 `.py` files under `src/grant_lab` now, 180 of them in `proposal_assist`.
  - INFERRED after removal: about 120. That is 119 files outside `proposal_assist` minus `workspaces.py` and the ten API modules, plus the new `db/models/workspace/` package files.
- Change it to:
```python
    assert len(files) > 100, f"only {len(files)} modules scanned — is SRC wrong? {SRC}"
```
- Optionally add a docstring note: "311 modules at dfb6dbae; ~120 after the 2026-11-03 workspace removal."

### `test_profile_access_single_source.py` → EDIT. 49 → 47 tests. ⚠ Live rule: profile privacy.

- Delete `"web/api/workspaces.py::create_workspace_api",` and its 5-line comment (82-87) from `PREDICATE_PINS`.
- Delete the same entry from `EXPECTED_PREDICATE_PINS` (line 176).
- Change the docstring of `test_pin_set_has_not_shrunk` from "THE COUNTS ARE 13 PREDICATE PINS AND 9 DELEGATION PINS" to "12 PREDICATE PINS AND 9 DELEGATION PINS (create_workspace_api left with the workspace, 2026-11-03)".
- The module docstring's "twelfth surface" history can stay.

### `test_workspaces_dir_isolation.py` → REWRITE (2 → 2). Live rule: tests must not write the live data dir.

The live directory still holds 11 workspaces, and the migrations read `WORKSPACES_DIR` from `os.environ` (fact 7). Both current tests read `Settings.workspaces_dir`, which goes. New file:
```python
"""No test may write into the operator's live workspaces directory.

Settings no longer has a workspaces_dir (the workspace left master on 2026-11-03),
but the directory and its repos are still on disk, and migrations 0037, 0066 and 0068
read WORKSPACES_DIR straight from os.environ, defaulting to "var/workspaces" — the
directory the live deployment holds. Migration tests that downgrade and re-upgrade run
those backfills inside a test. The autouse `_workspaces_dir_is_never_the_real_one`
fixture in tests/conftest.py redirects WORKSPACES_DIR; this pins it.
"""

from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_DEFAULT = (REPO / "var" / "workspaces").resolve()
_READERS = (
    "0037_backfill_reference_original_sha256.py",
    "0066_workspace_current_branch.py",
    "0068_branch_entity.py",
)


def test_the_env_never_resolves_to_the_real_workspaces_dir():
    value = os.environ.get("WORKSPACES_DIR")
    assert value, "the autouse fixture in tests/conftest.py did not set WORKSPACES_DIR"
    assert pathlib.Path(value).resolve() != PRODUCTION_DEFAULT


def test_the_migrations_still_default_to_the_thing_being_guarded_against():
    """Control: if a reader stops using WORKSPACES_DIR or changes its default, the
    test above guards nothing — fail loudly so this file is updated."""
    for name in _READERS:
        src = (REPO / "alembic" / "versions" / name).read_text()
        assert 'os.environ.get("WORKSPACES_DIR", "var/workspaces")' in src, name
```
Coordinate with the backfill owner. If their frozen copies change how they read the directory, change the literal. If no migration reads it any more, delete this file and the conftest fixture.

### `test_collab_e2e_run_cleanup.py` → DELETE or RE-POINT (1 test)

`scripts/collab_e2e_run.sh` goes. If the CI `e2e` job gets a successor runner that keeps the port preflight and the cleanup trap, `git mv` this test to `test_e2e_run_cleanup.py` and set `SCRIPT = REPO / "scripts" / "<new runner>.sh"`. The trap-before-preflight bug would recur there. Otherwise, delete it.

### `test_architecture_map_is_current.py` → depends on `gen_architecture.py` (15 tests)

- The generator must drop its fence section: `FENCING` and `INVENTORY`, lines 24-25 and 281-302.
- `parse_model_files` globs `db/models/*.py` non-recursively (READ, line 75). Moved models would vanish from the map.
- **Option A (recommended):** make the generator include `db/models/workspace/*.py`. The 31 tables still exist in the database. All 15 tests stay unchanged.
- **Option B:** the map covers live tables only. Then:
  - Change the parametrize of `test_the_active_uniqueness_section_carries_the_real_predicate` to `[("ranking_jobs", "status IN ('pending', 'running')"), ("profiles", "is_active = true"), ("users", "oidc_sub IS NOT NULL")]`. These are READ from `ARCHITECTURE.md` lines 21, 22 and 27.
  - Change `test_state_columns_are_listed_with_the_status_vocabularies` to `["ck_call_rankings_escalation_state", "ck_call_decisions_state", "ck_profile_bootstrap_jobs_status"]` (READ, lines 104, 105 and 111).
  - Delete `test_the_status_filter_does_not_match_inside_a_longer_word`. It would pass vacuously because its constraint is a workspace table; `test_is_status_like` keeps that same case on the pure function.
- Either way, regenerate with `uv run python scripts/gen_architecture.py`.

`INVESTIGATIONS.md` is pinned only by `test_investigations_index.py`, which needs no change (above).

---

## TASK 4 — shared test infrastructure

### `tests/conftest.py` (the only conftest outside `tests/proposal_assist`; there are 3 inside)

- **Delete** the whole fixture `_close_helper_entered_lifespans` (lines 344-361). This is mandatory, see fact 3.
- **`make_app`** (READ, 139-199) sets no edition and no `DRAFTING_ENABLED`; it just calls `create_app()`. No code change. Optionally drop the docstring's mention of `tests/test_api_workspaces.py`'s `_app` and `test_phase0_retirement.py`.
- **Keep** `_test_workspaces_root` and `_workspaces_dir_is_never_the_real_one`. Replace the docstring of the latter:
```python
    """Point WORKSPACES_DIR at a temp root for every test.

    Settings no longer reads it (the workspace left master on 2026-11-03), but
    migrations 0037, 0066 and 0068 read it straight from os.environ and default to
    "var/workspaces" — the live deployment's directory, which still holds real
    workspace repos. Migration tests that downgrade and re-upgrade run those
    backfills inside a test.

    Pinned by tests/architecture/test_workspaces_dir_isolation.py.
    """
```
- `_hermetic_git_env`: keep it. Kept tests still run `git ls-files` and `git check-ignore`, and it is harmless. Its docstring mentions workspace tests; trimming that is optional.
- `pg_session_factory`: no change. The truncate covers `Base.metadata.sorted_tables`, so the moved models must stay registered on `Base` through `db.models`.
- There is no `APP_EDITION` or `DRAFTING_ENABLED` anywhere in conftest (READ).

### `pyproject.toml`

- `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, no markers, no xfail options. `filterwarnings` (ResourceWarning and Unraisable) and `timeout = 120` stay. Only the comment at line 328 mentions the collab suite; optionally reword it.
- `[tool.ruff.lint.per-file-ignores]` `"tests/**" = ["ASYNC221"]` (lines 338-343): delete the whole block. MEASURED: `ruff check --no-cache --isolated --select ASYNC221` over every kept test file reports "All checks passed!"; its stated reason was git shell-outs inside async workspace tests. Re-run `uv run ruff check .` after deleting it.
- No `per-file-ignores` entry names `proposal_assist` or a workspace test path (READ).
- Dev dependencies `reportlab` and `pytest-httpx` become unused (fact 8). That is the dependency owner's call.

### `tests/test_env_example.py` (5 tests, count unchanged)

- `test_a_verbatim_copy_is_a_serving_production_configuration` reads `s.drafting_enabled`, `s.proposal_assist_llm_model`, `s.citation_llm_provider` and `s.citation_llm_allow_cloud`. All four go, so the test raises `AttributeError`. Replace lines 71-76 with the following, which keeps the "model id the code names" guard on the live hosted model:
```python
    assert not s.anthropic_api_key  # blank in the example: hosted features stay off
    assert s.remote_rank_model == Settings.model_fields["remote_rank_model"].default, (
        "the example must not pin a model id the code no longer names"
    )
```
- `test_the_example_matches_the_settings_fields` and `test_every_setting_has_a_line` pass once `.env.example` is regenerated with `uv run python scripts/gen_env_example.py`.
- Dependency: `scripts/gen_env_example.py`'s `DAY_ONE` (lines 61-73) names `DRAFTING_ENABLED`, `DRAFTING_ADAPTER`, `PROPOSAL_ASSIST_LLM_MODEL`, `DRAFTING_MAX_LIVE_SUBPROCESSES`, `DRAFTING_DB_POOL_SIZE`, `TURN_MAX_SECONDS`, `WORKSPACES_DIR`, `COLLAB_KILL_SWITCH`, `RETENTION_INSPECTION_ENABLED`, `CITATION_LLM_PROVIDER`, `CITATION_LLM_ALLOW_CLOUD`, `REVIEW_DOLLAR_CAP_DEFAULT` and `REVIEW_RUNS_PER_DAY_DEFAULT`. `render()` raises `SystemExit` on any `DAY_ONE` key that is no longer a setting. The script owner must remove the deleted ones.
- `test_digest_smtp_only_deploy.py::test_both_env_vars_are_documented` (`EMAIL_TRANSPORT`, `SMTP_HOST`) is unaffected.

### `tests/test_config.py`. TRIM, 19 → 16 tests. ⚠ Live rule: public example secrets are refused.

- **Delete:**
  - `test_citation_llm_provider_defaults_to_local` (142-149)
  - `test_reference_ingest_settings_defaults` (152-160)
  - `test_the_dead_reference_batch_settings_are_gone` (203-217); its positive control is `reference_ingest_concurrency`, a workspace setting
- Imports are unchanged; `Settings`, `pytest`, `re`, `subprocess` and `Path` are still used.
- `test_the_scan_finds_the_known_examples` (line 280) asserts that `"e2e-seed-secret-at-least-16-chars!"` appears in the repo. Today only `scripts/collab_e2e_seed.py` holds it. If the e2e seed script is renamed, update the trailing comment. If the literal disappears, swap the assert for a literal the new seed script contains. `config.py:23` also lists it as a sentinel. Line 279 `"test-secret-at-least-16-chars"` comes from `scripts/gen_openapi_types.sh`, which must keep it.

### Other conditional edits, depending on src and frontend decisions

- **`tests/test_api_me.py`**: only if src drops the workspace-only `retention_inspection` capability (`me.py:62-64`, which reads `retention_inspection_enabled`).
  - Line 49 → `assert body["capabilities"] == {"admin": False}`
  - Line 66 → `assert r.json()["capabilities"] == {"admin": True}`
  - Drop the retention comments at 47-48 and 63-65.
- **CSP `ws_origin`**: only if src removes it.
  - `test_spa_serving.py:109-117` → `assert connect_src.strip() == "'self'"`
  - In `test_security_headers.py`, delete `test_build_csp_prod_has_same_origin_ws_token`, `test_build_csp_http_base_gets_ws_token`, `test_ws_origin_injection_rejected_at_startup` and `test_ws_origin_whitespace_netloc_rejected_at_startup`, and drop the `ws_origin` comparison line in `test_build_csp_dev_output_unchanged`.
  - My recommendation is to leave the CSP alone in piece 1.
- **`tests/test_secret_file_exclusion.py:84`**: if the frontend deletes `frontend/.env.example` (its only key is `VITE_APP_EDITION`), change the parametrize to `[".env.example"]`. ⚠ Live rule: secrets kept out of the Docker context.
- **Cosmetic only:** `extra="ignore"` and pydantic-settings never read an unknown variable, so these lines do nothing. Optionally delete:
  - `DRAFTING_ENABLED` in `test_ranking_remote_effort.py:22`
  - `DRAFTING_ADAPTER` in `test_ranking_freshness_wiring.py:136` and `test_scheduler_ranking_logprobs.py:31`
  - `WORKSPACES_DIR` in `test_digest_smtp_only_deploy.py:116`

### Hard-coded counts that change (READ, plus MEASURED or INFERRED where stated)

| File::test | Current | After |
|---|---|---|
| `architecture/test_edition_surface.py::test_the_baseline_is_not_empty` (→ `_BASELINE_SIZE`) | 120 paths | about 51 (INFERRED; measure) |
| `architecture/test_no_import_cycles.py::test_the_analysis_actually_saw_the_codebase` | `> 200` (311 measured) | `> 100` (about 120 INFERRED) |
| `architecture/test_profile_access_single_source.py::test_pin_set_has_not_shrunk` | 13 predicate and 9 delegation pins | 12 and 9 |
| `architecture/test_auth_allowlist.py` comment on `UNJUDGEABLE_ROUTES` | "exactly these six" | five |
| `architecture/test_match_gates_are_wired.py::test_the_match_spec_actually_asserts_things` | `== 185` | unchanged unless `match.spec.ts` changes |
| `architecture/test_match_gates_are_wired.py` `REQUIRED_SCENARIOS` | 36 titles | unchanged unless `match.spec.ts` changes |
| `architecture/test_match_gates_are_wired.py::test_the_run_parser_finds_commands` | `> 20` (42 measured) | about 40, no change |
| `architecture/test_package_spa.py::test_the_detector_sees_the_real_writer` | `>= 3` | holds if the script keeps rm, mkdir and cp |
| `tests/fencing/test_f3e1a_slice.py` (9 git rows); `test_f3e1a2_slice.py` (9 holders, allowlist of 6) | — | deleted |
| `architecture/test_match_edition_process_cut.py` (`> 100`, `126 -> 44`) | — | deleted |
| `test_jinja_severance.py` (`len(_KEPT_API) >= 10`) | — | deleted |

No kept test pins a count of OpenAPI paths, settings, scheduler jobs, migrations or tables. The scheduler tests use subset or `get_job` checks.

---

## TOTALS

Test counts are MEASURED by collection; deltas are INFERRED until run.

- **Deleted files: 75.**
  - `tests/fencing/` (30 files, 255 tests)
  - 4 architecture tests: `branch_entity_invariants`, `match_edition_process_cut`, `spa_edition_match`, `collab_e2e_run_cleanup`. That is 23 tests. `collab_e2e_run_cleanup` is a re-point instead if a successor runner keeps the trap.
  - 39 top-level test files (425 tests)
  - 2 helpers (`workspace_delete_helper.py`, `admin_retention_helpers.py`)
  - Total: **703 tests**.
- **Rewritten: 2**, plus the baseline data file.
  - `test_edition_surface.py` → `test_route_surface.py`: 13 → 11 tests (−2)
  - `test_workspaces_dir_isolation.py`: 2 → 2
  - `full_edition_routes.txt` → `app_routes.txt`
- **Trimmed: 11 files, −35 tests.** `test_auth_domain_policy` −1, `test_embeddings_truncation_defect` −4, `test_main` −1, `test_pool_exhaustion_event_loop` −2, `test_scheduler` −7, `test_config` −3, `test_web_filters_model_label` −1, `test_auth_allowlist` −5, `test_match_gates_are_wired` −2, `test_package_spa` −7, `test_profile_access_single_source` −2. Option B for the map test would add −1.
- **Edited, count unchanged: 12.**
  - `tests/conftest.py`, `pyproject.toml`
  - `test_admin_users_jinja_surface_absent`, `test_board_preview_headers`, `test_migration_0083`, `test_profiles_bootstrap_worker`
  - `test_scheduler_digest`, `test_scheduler_embedding_cursor`, `test_scheduler_single_instance`, `test_env_example`
  - `test_no_import_cycles`, `test_boards_permission_single_source`, `test_decision_routes_require_a_profile`
- **Conditional edits:** `test_api_me`, `test_spa_serving` and `test_security_headers` (CSP), `test_secret_file_exclusion`, `test_events_ping` with the `PUBLIC_ROUTES` entry, and the `test_config` seed-secret comment.
- **Additions:**
  - in `test_auth_allowlist`, the unjudgeable dead-entry check: +1 (recommended)
  - in `test_match_gates_are_wired`, the 3 smoke wiring pins: +3 (G7)
- **Net outside `tests/proposal_assist`: about −740** (703 + 35 + 2), before additions. This excludes the three backfill tests, which belong to the other owner.
- **Skips and xfails:** 0 skipped expected on a machine with Docker. 0 xfailed: all 6 are in `tests/proposal_assist/collab/test_bridge_review_2026_10_26.py`.

## Live rules whose tests are touched, and how protection is kept

| Rule | Touched test | How it stays protected |
|---|---|---|
| Sign-out and revocation (`session_epoch`) | `test_api_session_revocation.py`, DELETED; it pinned only the `signal_user_revocation` calls | Every route's epoch bump is pinned elsewhere (READ). Logout: `test_api_auth.py::test_logout_bumps_epoch_and_clears_cookie` and `::test_logout_without_cookie_is_noop_200`. Password change: `test_api_me_password.py::test_password_change_remints_cookie_and_bumps_epoch`. Admin routes in `test_api_admin_users.py`: `test_disable_bumps_epoch`, `test_reset_password_bumps_epoch`, `test_reset_password_has_no_status_gate`, `test_approve_local_requires_password_then_bumps_epoch`, `test_approve_oauth_without_password_does_not_bump_epoch`, and no bump in `test_change_role_to_admin_and_member` and `test_enable_only_disabled`. Races: `test_auth_credential_write_races.py`. All are untouched. |
| Authentication allowlist | `architecture/test_auth_allowlist.py`, EDIT | The same walk over the one app in both `disabled` and `dev` modes. The dead-entry check stays, and an unjudgeable dead-entry check is added. |
| CSRF (Origin/Referer) | none | `test_auth_csrf.py` untouched (READ) |
| Email-domain allowlist | `test_auth_domain_policy.py`, TRIM | All HTTP login and session cases kept; only the websocket case goes |
| Profile privacy (single read predicate) | `architecture/test_profile_access_single_source.py`, EDIT | 12 predicate and 9 delegation pins, plus the negative half; only the deleted route's pin is removed |
| Clickjacking frame headers | `test_board_preview_headers.py`, EDIT | Only the `edition=` keyword argument is dropped; all six cases kept |
| Scheduler single instance and lock watchdog | `test_scheduler_single_instance.py`, EDIT | Only `_build` loses the workspace controller; the lock, probe, watchdog and lifespan tests are unchanged |
| Rate-limit sweep covers the board limiter | `test_scheduler.py`, TRIM | `test_main_pys_rate_limit_sweep_covers_the_board_limiter` and both sweep tests kept |
| DB pool fail-fast (`pool_timeout=5`) | `test_pool_exhaustion_event_loop.py`, TRIM | The `make_engine` test kept verbatim |
| Route surface (no route without intent; removed surface absent) | `test_edition_surface.py` → `test_route_surface.py` | Baseline equality, a hand-bumped size, `REMOVED_PREFIXES`, no websocket, router names pinned, and a leftover `APP_EDITION`/`DRAFTING_ENABLED` has no effect |
| Served SPA: validate before delete; one writer | `test_package_spa.py`, TRIM | A missing build and a missing root marker are both refused with the tree intact; the writer scan and detector controls are kept |
| CI wiring of the browser gates | `test_match_gates_are_wired.py`, TRIM and ADD | `match-e2e` pins kept; new pins for `smoke.spec.ts` in the `e2e` job (G7) |
| Tests must not write the live data dir | `test_workspaces_dir_isolation.py`, REWRITE, plus the conftest fixture kept | Now pins `os.environ["WORKSPACES_DIR"]` and the migrations' default |
| Public example secrets refused over https | `test_config.py` | Kept; only the seed-script comment or literal may need updating |
| No module-level import cycles | `test_no_import_cycles.py`, EDIT | Only the anti-vacuity floor lowers, to `> 100` |

### Critical files for implementation
- `/home/parlar_ai/dev/grant-lab/tests/conftest.py`
- `/home/parlar_ai/dev/grant-lab/tests/architecture/test_edition_surface.py` (→ `test_route_surface.py`) and `/home/parlar_ai/dev/grant-lab/tests/architecture/full_edition_routes.txt`
- `/home/parlar_ai/dev/grant-lab/tests/architecture/test_auth_allowlist.py`
- `/home/parlar_ai/dev/grant-lab/tests/test_scheduler.py`
- `/home/parlar_ai/dev/grant-lab/tests/architecture/test_package_spa.py`
- `/home/parlar_ai/dev/grant-lab/tests/architecture/test_match_gates_are_wired.py`
