# Collab Slice 3b-5d·D0 — Two-Browser E2E De-Risk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: this plan is executed **inline** in the working session with superpowers:executing-plans (NOT a detached SDD build, NOT strict red/green unit-TDD — the deliverable is a live two-browser run + a GO/NO-GO findings doc). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the first *real* two-browser signal for the collaborative-editing stack — a GO/NO-GO verdict + a punch-list — by extending and running the never-executed e2e harness (`scripts/collab_e2e_seed.py` + `frontend/e2e/collab.spec.ts`) with two distinct users and the core scenarios, before investing in D1–D3 and the flip.

**Architecture:** Test-harness surface only. The seed boots a testcontainers Postgres, migrates, seeds **two users** (owner + a `WorkspaceViewer` member) on an **active draft** (both writers) plus a **locked spec** (member read-only) plus one `Reference`+`ReferenceChunk` (citeable source), enables collab per-artifact, inits the git repo with `proposal.md` + `proposal-spec.md`, starts uvicorn (SPA_MODE=dev), and prints signed cookies + labels. A one-command runner brings up the seed + Vite and runs the Playwright spec. The spec drives two real Chromium contexts through convergence / distinct-name presence / checkpoint+persist / reconnect / Cite / read-only.

**Tech Stack:** Python 3.12 / `uv` (seed + runner), `testcontainers` Postgres, uvicorn, Vite dev (:5173 → uvicorn :8000), `@playwright/test` 1.60 + cached Chromium (`~/.cache/ms-playwright/chromium-1223`), Tiptap v3 + custom Yjs WS provider.

## Global Constraints

- **Feature flag STAYS OFF in prod.** Add **no** `enable_collab` source caller. The seed enables collab only on the ephemeral testcontainers workspace (both artifacts). D0 changes nothing in prod.
- **No app/backend logic change, no DB migration, no schema change, no new endpoint.** The only edits are the three harness files (`scripts/collab_e2e_seed.py`, `scripts/collab_e2e_run.sh`, `frontend/e2e/collab.spec.ts`) + the findings doc.
- **Dev serving mode only** (Vite :5173 proxying uvicorn :8000, SPA_MODE=dev). The dev CSP already emits `connect-src 'self' ws: http://localhost:5173`. **Do NOT touch the prod CSP** (that is D4).
- The e2e suite stays **opt-in / skip-gated** (`test.skip` when `E2E_COLLAB_WS_ID`/cookies unset) so the default CI Playwright run stays green. **No new blocking CI gate.**
- Python lint gate is `uv run ruff check .` (NOT `ruff format`); keep lines ≤100 chars (E501). `git add` with **explicit paths only** (never `-A`/`.`). Run any pytest with `EMBEDDING_MODEL=bge-m3` — though D0 adds no pytest.
- **Read-only member** and **Cite fresh-section** are strong signals but **NOT GO gates** (§ GO/NO-GO). The GO gate is core transport only: two-distinct-user convergence, distinct-name presence, checkpoint + reload-persistence, reconnect/re-sync.

## Execution Model

- **Inline** (spec §6): the harness edits are made + committed inline; the run is performed inline; the findings doc is written + committed inline.
- Work on a short-lived local branch **`collab-slice3b5d0`** cut from `master` (`595c702`), commit the harness there, run, commit findings there. **Merge is user-gated** (as with every prior sub-slice) — the plan STOPS at the GO/NO-GO presentation; do NOT merge/push without the explicit "please merge". Merge, when approved, is `--no-ff`, **local only**.
- We are in the **main working tree** (not a worktree), so switching to the D0 branch here is ordinary git — the "never touch master from a worktree" rule is not engaged.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `scripts/collab_e2e_seed.py` | **modify** | Seed 2 users + active-draft/locked-spec + reference/chunk; print `E2E_COLLAB_WS_ID`, `E2E_COOKIE_OWNER`, `E2E_COOKIE_MEMBER`, `E2E_SESSION_COOKIE` (=owner, back-compat), `E2E_OWNER_LABEL`, `E2E_MEMBER_LABEL`. |
| `scripts/collab_e2e_run.sh` | **create** | One command: start seed (bg) → parse printed vars → start Vite (bg) → poll both ports → run Playwright → teardown on exit. |
| `frontend/e2e/collab.spec.ts` | **modify** | Distinct-user cookies + 4 scenarios (convergence+presence+checkpoint+persist / reconnect / Cite / read-only-on-locked-spec). |
| `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` | **create** | GO/NO-GO verdict + punch-list (new defects + confirmed expected deferrals), or the blocker + exact manual recipe + partial signal. |

### Grounding facts (verified against source — do not re-derive)

- `resolve_access` ([ws_auth.py:118-126](../../../src/call_manager/proposal_assist/collab/ws_auth.py#L118)): active draft → `(True, True)` for any member; **locked spec → `(True, is_owner)`** (member read-only). `WorkspaceViewer` is membership, no role field ([models.py:753](../../../src/call_manager/db/models.py#L753)).
- Per-artifact flag: `collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft"|"spec")` ([state.py:22](../../../src/call_manager/proposal_assist/collab/state.py#L22)).
- Spec lock is `Workspace.spec_locked_at` (a datetime) — set it directly ([models.py:684](../../../src/call_manager/db/models.py#L684)).
- Artifact files: draft=`proposal.md`, spec=`proposal-spec.md` ([artifacts.py:21-22](../../../src/call_manager/proposal_assist/artifacts.py#L21)). A room with an empty Yjs log **hydrates its initial doc from git HEAD markdown** ([hydrate.py](../../../src/call_manager/proposal_assist/collab/hydrate.py)) — so the seeded file contents render. `SpecPanel` shows a "Start spec from skeleton" CTA when every section is whitespace, so the seeded spec **must be non-empty**.
- Frontend: `DraftPanel`/`SpecPanel` render `<CollabEditor canWrite={data.can_write}>` when `data.collab_enabled`. `CollabEditor` sets ProseMirror `editable: props.canWrite` — read-only ⇒ `.ProseMirror[contenteditable="false"]`. The editable surface is `[data-testid="collab-editor"]`; remote carets are `.collaboration-carets__caret` with a `.collaboration-carets__label` name div; label text = `displayName` = `name || email.split("@")[0]` ([collabColor.ts:15](../../../frontend/src/collab/collabColor.ts#L15)).
- Cockpit artifact tab lives in `?tab=` (`draft`|`spec`) ([cockpitNav.ts:9](../../../frontend/src/cockpit/cockpitNav.ts#L9)); the member opens the spec via `/cockpit/<id>?tab=spec`.
- Cite flow: selection toolbar `Cite` button `aria-label="Cite"` (visible only when `canWrite`) → `CitationComposer` (`aside[aria-label="Add a citation"]`) → pick reference button (title text; enabled only when `text_status==="done"`) → select passage in a chunk `<pre>` (fires on `onMouseUp`) → **Add citation** button. The inserted node renders `[data-testid="citation-pill"]`.

---

## Task 1: Extend the seed to two users + active-draft/locked-spec + reference

**Files:**
- Modify: `scripts/collab_e2e_seed.py`

**Interfaces:**
- Produces (printed to stdout, parsed by the runner + spec):
  `E2E_COLLAB_WS_ID`, `E2E_COOKIE_OWNER`, `E2E_COOKIE_MEMBER`, `E2E_SESSION_COOKIE` (=owner), `E2E_OWNER_LABEL`, `E2E_MEMBER_LABEL`.

- [ ] **Step 1: Create the D0 branch (from the current `master` tip `595c702`)**

```bash
cd /home/parlar_ai/dev/call-monitor
git checkout -b collab-slice3b5d0
git branch --show-current   # -> collab-slice3b5d0
```

- [ ] **Step 2: Add imports at the top of `scripts/collab_e2e_seed.py`**

After the existing `import ... uuid` block (the module-level imports ending at `from pathlib import Path`), add `subprocess` and the datetime import. Change the existing import group so it reads:

```python
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
```

- [ ] **Step 3: Replace the seed + repo-init + cookie block**

Replace the current block (the `# --- seed ---` import line through the `cookie_value = app.state.session_signer.make(user_id=oid)` line — i.e. the lines from `from call_manager.proposal_assist.collab import state as collab_state` down to and including the single-owner cookie mint) with the two-user version below:

```python
        # --- seed: two users, two collab artifacts ---
        from call_manager.db.models import Reference, ReferenceChunk, WorkspaceViewer
        from call_manager.proposal_assist.citations.gitio import _git_env
        from call_manager.proposal_assist.collab import state as collab_state
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_editing_save import _init_repo

        suffix = uuid.uuid4().hex[:8]
        with Session() as s:
            owner = make_user(s, email=f"e2e-owner-{suffix}@ki.se")
            member = make_user(s, email=f"e2e-member-{suffix}@ki.se")
            ws = make_workspace(s, owner=owner, current_phase="active")
            # Membership row: a WorkspaceViewer is a *writer* on the active draft
            # and *read-only* on the locked spec (resolve_access, ws_auth.py:118-126).
            s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
            # Collab enabled on BOTH artifacts; spec locked so the member is read-only there.
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="spec")
            ws.spec_locked_at = datetime.now(UTC)
            # One citeable reference (indexed) so the select->Cite flow has a source.
            ref = Reference(
                workspace_id=ws.id,
                kind="paper",
                source_type="uploaded",
                filename="seed-ref.pdf",
                storage_path=f"seed/{suffix}/seed-ref.pdf",
                mime_type="application/pdf",
                title="Seed Reference on Precision Medicine",
                citekey=f"seed{suffix}",
                text_status="done",
                metadata_status="approved",
            )
            s.add(ref)
            s.flush()
            s.add(
                ReferenceChunk(
                    reference_id=ref.id,
                    workspace_id=ws.id,
                    chunk_index=0,
                    text=(
                        "Precision medicine improves diagnostic yield in "
                        "oncology cohorts and shortens time to targeted therapy."
                    ),
                    embedding=[0.0] * 1024,
                    embedding_model="bge-m3",
                )
            )
            s.commit()
            wid = ws.id
            owner_label = owner.email.split("@")[0]
            member_label = member.email.split("@")[0]
            owner_cookie = app.state.session_signer.make(user_id=owner.id)
            member_cookie = app.state.session_signer.make(user_id=member.id)

        # Repo: draft (proposal.md) + a NON-EMPTY spec (proposal-spec.md), both committed.
        # Empty spec sections would trigger SpecPanel's "Start spec from skeleton" CTA
        # instead of the read-only CollabEditor the read-only scenario needs.
        repo = _init_repo(
            workspaces,
            wid,
            doc="Preamble.\n\n## Aims\n\nPrecision medicine improves diagnostic outcomes for patients.\n",
        )
        (repo / "proposal-spec.md").write_text(
            "# Spec\n\n## Aim\n\nThe aim of this proposal is precision oncology.\n"
        )
        env = _git_env()
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "seed spec"], cwd=repo, check=True, env=env)
```

Note: this removes the old single-owner `wid, oid = ...` / `_init_repo(...)` / `cookie_value = ...` lines. `owner_cookie` replaces `cookie_value` in the print block (Step 4).

- [ ] **Step 4: Replace the print block (with an explicit flush)**

Replace the `print("=== collab e2e seed ready ===")` block (from that line through the final `print()` before the `try:` loop) with the version below. The **`sys.stdout.flush()`** is load-bearing (Codex High): the runner redirects the seed's stdout to a file, and Python **fully block-buffers** a non-tty stdout — the seed then blocks forever in its `while True: sleep(1)` loop, so without an explicit flush the `E2E_*` lines never leave the buffer and the runner's grep times out. (The runner also launches it with `python -u`; the flush makes the seed correct regardless of how it is invoked.)

```python
        print()
        print("=== collab e2e seed ready (2 users) ===")
        print()
        print(f"E2E_COLLAB_WS_ID={wid}")
        print(f"E2E_COOKIE_OWNER={owner_cookie}")
        print(f"E2E_COOKIE_MEMBER={member_cookie}")
        print(f"E2E_SESSION_COOKIE={owner_cookie}")  # back-compat with the pre-D0 tests
        print(f"E2E_OWNER_LABEL={owner_label}")
        print(f"E2E_MEMBER_LABEL={member_label}")
        print()
        print("Owner is a writer on draft + spec; member is a writer on the")
        print("active draft and READ-ONLY on the locked spec (?tab=spec).")
        print()
        print("Now start Vite in another terminal:  cd frontend && pnpm dev")
        print("Or use the one-command runner:        scripts/collab_e2e_run.sh")
        print()
        print("Press Ctrl-C to stop uvicorn (testcontainers Postgres also stops).")
        print()
        sys.stdout.flush()  # Codex High: flush before the blocking sleep loop so the
        #                     runner (which reads a redirected stdout file) sees the vars.
```

- [ ] **Step 5: Lint the seed**

Run: `EMBEDDING_MODEL=bge-m3 uv run ruff check scripts/collab_e2e_seed.py`
Expected: `All checks passed!` (fix any E501 by wrapping — logic unchanged).

- [ ] **Step 6: Smoke-run the seed (boots PG + migrates + seeds + uvicorn), confirm the vars print, then stop it**

Run (foreground; Ctrl-C after you see the vars):
```bash
EMBEDDING_MODEL=bge-m3 uv run python scripts/collab_e2e_seed.py
```
Expected: after the testcontainers PG pull + Alembic upgrade (~30–90 s), stdout shows all six `E2E_*` lines with non-empty values and `=== collab e2e seed ready (2 users) ===`. No traceback (a `Reference`/`ReferenceChunk` check-constraint or a `WorkspaceViewer` FK error would surface here). Press Ctrl-C.

If a check-constraint fails, re-verify the enum values against [models.py:1569-1692](../../../src/call_manager/db/models.py#L1569) (`kind∈paper…`, `source_type∈uploaded…`, `text_status∈…done…`, `metadata_status∈…approved…`) and fix, then re-run.

- [ ] **Step 7: Commit**

```bash
git add scripts/collab_e2e_seed.py
git commit -m "$(cat <<'EOF'
test(collab): 2-user e2e seed — active-draft writers + locked-spec read-only + citeable ref (3b-5d.D0)

Seed owner + WorkspaceViewer member on an active draft (both writers) plus a
locked spec (member read-only), collab enabled on both artifacts, and one
indexed Reference+ReferenceChunk for the select->Cite scenario. Print
E2E_COOKIE_OWNER/MEMBER + labels (E2E_SESSION_COOKIE kept = owner for back-compat).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: One-command runner

**Files:**
- Create: `scripts/collab_e2e_run.sh`

**Interfaces:**
- Consumes: the seed's stdout `E2E_*` lines (Task 1).
- Produces: exports the parsed vars into the environment and invokes `pnpm exec playwright test e2e/collab.spec.ts "$@"` (extra args pass through, e.g. `--headed`, `-g "reconnect"`).

- [ ] **Step 1: Create `scripts/collab_e2e_run.sh`**

```bash
#!/usr/bin/env bash
# One-command collab two-browser e2e runner (Slice 3b-5d.D0; reused by D4).
#
#   scripts/collab_e2e_run.sh [extra playwright args...]
#
# Boots scripts/collab_e2e_seed.py (testcontainers PG + uvicorn:8000, SPA_MODE=dev),
# parses the printed E2E_* vars, starts Vite dev (:5173), waits for both ports,
# then runs frontend/e2e/collab.spec.ts. Tears everything down on exit.
#
# Requirements: `uv` (the seed runs via `uv run`, no pre-activated venv needed),
# frontend deps installed (pnpm i), and a Chromium usable by Playwright. On a fresh
# machine the browser OS libs may be missing:
#   cd frontend && pnpm exec playwright install --with-deps chromium   (needs sudo)
# Chromium itself is cached at ~/.cache/ms-playwright/chromium-1223.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SEED_LOG="$(mktemp)"
VITE_LOG="$ROOT/.e2e-vite.log"
SEED_PGID=""
VITE_PGID=""

cleanup() {
  # SIGINT the seed's process GROUP so the seed's KeyboardInterrupt handler runs
  # (it sets server.should_exit and lets the `with PostgresContainer` context stop
  # the docker container). Plain SIGTERM would skip that handler and orphan the PG
  # container (Ryuk reaps it eventually, but slowly). Give it a moment, then hard-kill.
  if [ -n "$SEED_PGID" ]; then
    kill -INT -"$SEED_PGID" 2>/dev/null || true
    sleep 2
    kill -KILL -"$SEED_PGID" 2>/dev/null || true
  fi
  # Vite spawns esbuild/node children; kill the whole group (setsid made VITE_PGID
  # the group leader), not just the wrapper PID.
  [ -n "$VITE_PGID" ] && kill -TERM -"$VITE_PGID" 2>/dev/null || true
}
trap cleanup EXIT

echo "[run] starting seed (testcontainers PG + uvicorn:8000)…"
# setsid → own process group (clean group-kill in cleanup). `uv run python -u` →
# unbuffered stdout so the printed E2E_* vars reach $SEED_LOG immediately, and no
# pre-activated venv required.
setsid env EMBEDDING_MODEL=bge-m3 uv run python -u scripts/collab_e2e_seed.py \
  >"$SEED_LOG" 2>&1 &
SEED_PGID=$!

# Wait up to ~150s for the seed to print the workspace id (PG image pull + migrate).
for _ in $(seq 1 300); do
  grep -q '^E2E_COLLAB_WS_ID=' "$SEED_LOG" && break
  kill -0 "$SEED_PGID" 2>/dev/null || { echo "[run] seed exited early:"; cat "$SEED_LOG"; exit 1; }
  sleep 0.5
done
grep -q '^E2E_COLLAB_WS_ID=' "$SEED_LOG" || { echo "[run] seed never printed vars:"; tail -40 "$SEED_LOG"; exit 1; }

get() { grep "^$1=" "$SEED_LOG" | head -1 | cut -d= -f2-; }
export E2E_COLLAB_WS_ID; E2E_COLLAB_WS_ID="$(get E2E_COLLAB_WS_ID)"
export E2E_COOKIE_OWNER; E2E_COOKIE_OWNER="$(get E2E_COOKIE_OWNER)"
export E2E_COOKIE_MEMBER; E2E_COOKIE_MEMBER="$(get E2E_COOKIE_MEMBER)"
export E2E_SESSION_COOKIE; E2E_SESSION_COOKIE="$(get E2E_SESSION_COOKIE)"
export E2E_OWNER_LABEL; E2E_OWNER_LABEL="$(get E2E_OWNER_LABEL)"
export E2E_MEMBER_LABEL; E2E_MEMBER_LABEL="$(get E2E_MEMBER_LABEL)"
echo "[run] workspace=$E2E_COLLAB_WS_ID owner=$E2E_OWNER_LABEL member=$E2E_MEMBER_LABEL"

echo "[run] starting Vite dev (:5173)…"
setsid bash -c 'cd frontend && exec pnpm dev' >"$VITE_LOG" 2>&1 &
VITE_PGID=$!

echo "[run] waiting for uvicorn:8000 + vite:5173…"
# curl WITHOUT -f on purpose: a 401 from /api/me (unauthenticated) still proves the
# port is up and serving. -f would treat that 401 as failure and never go ready.
ready=0
for _ in $(seq 1 120); do
  if curl -s -o /dev/null "http://127.0.0.1:8000/api/me" \
     && curl -s -o /dev/null "http://localhost:5173/"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "[run] servers not ready after ~60s — aborting."
  echo "--- seed log (tail) ---"; tail -30 "$SEED_LOG"
  echo "--- vite log (tail) ---"; tail -30 "$VITE_LOG"
  exit 1
fi

echo "[run] running collab e2e…"
cd frontend
pnpm exec playwright test e2e/collab.spec.ts "$@"
```

- [ ] **Step 2: Make it executable + syntax-check**

```bash
chmod +x scripts/collab_e2e_run.sh
bash -n scripts/collab_e2e_run.sh   # no output = parses clean
```

- [ ] **Step 3: Commit**

```bash
git add scripts/collab_e2e_run.sh
git commit -m "$(cat <<'EOF'
test(collab): one-command two-browser e2e runner (seed + Vite + playwright) (3b-5d.D0)

Boots the seed, parses its E2E_* vars, starts Vite dev, waits for both ports,
runs frontend/e2e/collab.spec.ts, and tears everything down on exit. Reused by
D4's final re-validation.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Distinct-user cookies + core scenarios in the e2e spec

**Files:**
- Modify: `frontend/e2e/collab.spec.ts` (replace the whole file)

**Interfaces:**
- Consumes: `E2E_COLLAB_WS_ID`, `E2E_COOKIE_OWNER` (falls back to `E2E_SESSION_COOKIE`), `E2E_COOKIE_MEMBER`, `E2E_OWNER_LABEL`, `E2E_MEMBER_LABEL`.

- [ ] **Step 1: Replace `frontend/e2e/collab.spec.ts` with the distinct-user + 4-scenario version**

```typescript
import { expect, test, type Browser } from "@playwright/test";

// Collab two-browser e2e — Slice 3b-5d.D0 de-risk (extends the 3b-3/3b-4 harness).
//
// DEFERRED / OPT-IN: skip-gated so the default CI Playwright run stays GREEN.
// Requires the seed (scripts/collab_e2e_seed.py) + Vite dev + uvicorn (SPA_MODE=dev).
// The one-command path is scripts/collab_e2e_run.sh. Manual env:
//   E2E_COLLAB_WS_ID  — seeded active workspace (collab on draft + spec; spec locked)
//   E2E_COOKIE_OWNER  — signed session cookie for the owner (writer everywhere)
//   E2E_COOKIE_MEMBER — signed session cookie for the member (draft writer; spec read-only)
//   E2E_OWNER_LABEL / E2E_MEMBER_LABEL — expected caret label text (email local part)
// The Yjs WS is ws://localhost:8000/api/ws/workspaces/<id>/<artifact>, proxied by
// Vite; the dev CSP's "ws:" token allows it. Do NOT change prod CSP here (D4).
//
// Caret DOM (from @tiptap/extension-collaboration-caret 3.27.1):
//   span.collaboration-carets__caret  (widget) > div.collaboration-carets__label (name)

const WS = process.env.E2E_COLLAB_WS_ID;
const COOKIE_OWNER = process.env.E2E_COOKIE_OWNER ?? process.env.E2E_SESSION_COOKIE;
const COOKIE_MEMBER = process.env.E2E_COOKIE_MEMBER;
const OWNER_LABEL = process.env.E2E_OWNER_LABEL ?? "";
const MEMBER_LABEL = process.env.E2E_MEMBER_LABEL ?? "";

function sessionCookie(value: string, url: string) {
  return [{ name: "session", value, url }];
}

async function openAs(browser: Browser, cookie: string, baseURL: string, path: string) {
  const ctx = await browser.newContext();
  await ctx.addCookies(sessionCookie(cookie, baseURL));
  const page = await ctx.newPage();
  await page.goto(path);
  return { ctx, page };
}

test.describe("collab two-browser (distinct users)", () => {
  test.skip(
    !WS || !COOKIE_OWNER || !COOKIE_MEMBER,
    "requires the 2-user seed — set E2E_COLLAB_WS_ID + E2E_COOKIE_OWNER + E2E_COOKIE_MEMBER",
  );

  // (a) Convergence + distinct-name presence + checkpoint + reload-persistence. [GO gate]
  test("owner and member converge, see each other's distinct caret, checkpoint persists", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft); // owner
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft); // member (draft writer)

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Owner types; member converges.
    await edA.click();
    await A.page.keyboard.type("hello-collab");
    await expect(edB).toContainText("hello-collab", { timeout: 8_000 });

    // Member moves its cursor too (broadcasts awareness back to the owner).
    await edB.click();
    await B.page.keyboard.type("-from-member");
    await expect(edA).toContainText("-from-member", { timeout: 8_000 });

    // Distinct-name presence: B sees the OWNER's caret, A sees the MEMBER's caret.
    const caretOnB = edB.locator(".collaboration-carets__caret .collaboration-carets__label");
    const caretOnA = edA.locator(".collaboration-carets__caret .collaboration-carets__label");
    await expect(caretOnB.first()).toBeVisible({ timeout: 8_000 });
    await expect(caretOnA.first()).toBeVisible({ timeout: 8_000 });
    const nameOnB = (await caretOnB.first().textContent())?.trim() ?? "";
    const nameOnA = (await caretOnA.first().textContent())?.trim() ?? "";
    expect(nameOnB.length).toBeGreaterThan(0);
    expect(nameOnA.length).toBeGreaterThan(0);
    // Real two-user presence => the two labels differ (not one shared owner name).
    expect(nameOnA).not.toBe(nameOnB);
    if (OWNER_LABEL) expect(nameOnB).toContain(OWNER_LABEL);
    if (MEMBER_LABEL) expect(nameOnA).toContain(MEMBER_LABEL);

    // Checkpoint (Save a version) on the owner, then reload and confirm persistence.
    const saveBtn = A.page.getByRole("button", { name: /save a version/i });
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await saveBtn.click();
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });

    await A.page.reload();
    await expect(A.page.getByTestId("collab-editor")).toContainText("hello-collab", {
      timeout: 15_000,
    });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (b) Reconnect / re-sync after a dropped socket. [GO gate]
  test("member reconnects, re-syncs the latest doc, and its caret reappears", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    await edA.click();
    await A.page.keyboard.type("before-drop ");
    await expect(edB).toContainText("before-drop", { timeout: 8_000 });

    // Drop B's connectivity, mutate on A while B is offline, then restore B.
    await B.ctx.setOffline(true);
    await A.page.keyboard.type("after-drop");
    await B.ctx.setOffline(false);

    // B re-syncs the edit made while it was offline (provider reconnect + sync).
    await expect(edB).toContainText("after-drop", { timeout: 20_000 });
    // Presence self-heals: B's caret reappears for A within the ~15s heartbeat window.
    await edB.click();
    await B.page.keyboard.type("x");
    await expect(
      edA.locator(".collaboration-carets__caret"),
    ).toBeVisible({ timeout: 20_000 });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (c) Cite-via-room on a fresh section; the citation node appears on BOTH sides.
  //     [strong signal, NOT a GO gate] — the 409-after-co-edit variant (3b-5a C#3)
  //     is a KNOWN D1 deferral and is exercised as a documented observation in D0's
  //     run notes, not asserted here (it must not turn into a flaky hard failure).
  //
  //     STALE-STATE GUARD (Codex Medium): all scenarios share ONE server-side room,
  //     and the earlier scenarios leave UNCHECKPOINTED live Yjs edits. The Cite
  //     toolbar derives base_section_sha from the git-backed useDraft sections
  //     (TiptapSelectionToolbar.tsx:81), but the citation kernel validates it against
  //     the LIVE room section SHA (kernel.py:88-92, direct_cite_via_kernel). So a
  //     drifted git-vs-live section would 409 from ACCUMULATED state, not the intended
  //     fresh path — masking a real result. Serial execution alone does not fix this.
  //     Remedy: checkpoint (Save a version) THEN reload both, so git == live before we
  //     cite, giving this test a clean, self-consistent base regardless of test order.
  test("owner cites a claim; the pill converges to the member", async ({ browser, baseURL }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Reset to a consistent base: fold any prior scenario's uncheckpointed edits into
    // git, then reload so the toolbar's git sections match the live room (see guard above).
    const saveBtn = A.page.getByRole("button", { name: /save a version/i });
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await saveBtn.click();
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await A.page.reload();
    await B.page.reload();
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Select a unique word in the seeded section body to anchor the claim.
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    // Pick the seeded (indexed) reference.
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    // Select a supporting passage: triple-click the chunk <pre> (selects the line
    // and fires the mouseup captureSelection listens for).
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    await chunk.click({ clickCount: 3 });
    await composer.getByRole("button", { name: /add citation/i }).click();

    // The pill appears for the owner and converges to the member.
    await expect(edA.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });
    await expect(edB.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (d) Read-only member on the LOCKED SPEC. [strong signal, NOT a GO gate]
  //     A write leak (member mutates the locked spec) IS a punch-list defect.
  test("member is read-only on the locked spec but sees the owner's live edits", async ({
    browser,
    baseURL,
  }) => {
    const spec = `/cockpit/${WS}?tab=spec`;
    const owner = await openAs(browser, COOKIE_OWNER!, baseURL!, spec);
    const member = await openAs(browser, COOKIE_MEMBER!, baseURL!, spec);

    const edOwner = owner.page.getByTestId("collab-editor");
    const edMember = member.page.getByTestId("collab-editor");
    await expect(edOwner).toBeVisible({ timeout: 15_000 });
    await expect(edMember).toBeVisible({ timeout: 15_000 });

    // The member's editor surface is non-editable (editable: canWrite === false).
    await expect(edMember.locator(".ProseMirror")).toHaveAttribute("contenteditable", "false");

    // Owner edits the spec; the member sees it (ungated read path).
    await edOwner.click();
    await owner.page.keyboard.type("owner-spec-edit");
    await expect(edMember).toContainText("owner-spec-edit", { timeout: 8_000 });
    // Owner's caret is visible to the read-only member.
    await expect(
      edMember.locator(".collaboration-carets__caret"),
    ).toBeVisible({ timeout: 8_000 });

    // Write-leak check: the member cannot mutate the locked spec.
    await edMember.click();
    await member.page.keyboard.type("MEMBER-SHOULD-NOT-WRITE");
    await expect(edMember).not.toContainText("MEMBER-SHOULD-NOT-WRITE");

    await owner.ctx.close();
    await member.ctx.close();
  });
});
```

- [ ] **Step 2: Typecheck / parse the spec offline (does NOT run browsers)**

```bash
cd frontend
pnpm exec playwright test e2e/collab.spec.ts --list
```
Expected: it **lists 4 tests** under "collab two-browser (distinct users)" and exits 0 (no seed env ⇒ they are skip-gated but still enumerated; a TypeScript/parse error would fail this command). If `--list` reports them as skipped rather than listed on your Playwright version, `pnpm exec tsc --noEmit -p tsconfig.json` is an acceptable fallback parse check.

- [ ] **Step 3: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/e2e/collab.spec.ts
git commit -m "$(cat <<'EOF'
test(collab): distinct-user two-browser e2e scenarios (converge/presence/reconnect/cite/read-only) (3b-5d.D0)

Switch to owner/member cookies (real two-user presence, distinct caret names)
and add reconnect, Cite-via-room (fresh section), and read-only-on-locked-spec
scenarios. Skip-gated on the 2-user seed vars so default CI stays green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: The live run + GO/NO-GO findings doc

**Files:**
- Create: `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md`

- [ ] **Step 1: Preflight — confirm a usable Chromium (best-effort)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm exec playwright install chromium   # no-op if cached (chromium-1223)
# If a later run fails on missing OS libs, the fix (needs sudo) is:
#   pnpm exec playwright install --with-deps chromium
```
Record whether OS libs are present; do NOT run `sudo` non-interactively — if libs are missing, that becomes the documented blocker (Step 3).

- [ ] **Step 2: Run the harness**

```bash
cd /home/parlar_ai/dev/call-monitor
scripts/collab_e2e_run.sh --reporter=list
```
Capture, per test: **PASS / FAIL / BLOCKED**. If the full run is flaky under contention, re-run individual scenarios with `-g`, e.g.:
```bash
scripts/collab_e2e_run.sh -g "converge, see each other's distinct caret"
scripts/collab_e2e_run.sh -g "reconnects, re-syncs"
```
While a session is up, also **manually** observe the 3b-5a C#3 deferral (documented, not automated): with two owner-writer tabs on the draft, cite a claim, then have the other tab edit that same section, then cite again in the first tab → expect the composer's stale-snapshot **409** ("save and reselect"). Note whether it reproduced.

- [ ] **Step 3: Write the findings doc**

Create `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` with:
- **Environment**: what actually ran (browser libs present? both ports up? which scenarios executed vs blocked).
- **Per-scenario table**: (a) converge/presence/checkpoint/persist, (b) reconnect, (c) Cite fresh-section, (d) read-only-on-locked-spec → PASS / FAIL / BLOCKED + one-line evidence.
- **GO/NO-GO verdict** per the criteria below.
- **Punch-list**: each *new* defect (real failure) tagged with a target sub-slice (D1/D2/D3/D4); plus the *expected* deferrals it confirmed — 3b-5a C#3 stale-snapshot 409 → D1; relay-spoof not benignly reproducible in a browser → D2.
- **If BLOCKED** (missing OS libs / port conflicts / resource limits): record the exact blocker, the **manual run recipe** (`scripts/collab_e2e_run.sh`, or the seed + `pnpm dev` + `pnpm exec playwright test` triplet with the env vars), and whatever **partial signal** was obtained (seed booted? servers up? any single scenario green?). A blocked run is a valid D0 outcome — it de-risks by producing an exact repro, and the GO decision then defers to the first successful manual run.

**GO/NO-GO criteria (copy into the doc):**
- **GO** ⇒ proceed to D1: core transport all PASS — two-distinct-user convergence, distinct-name presence, checkpoint + reload-persistence, reconnect/re-sync — with only the *expected* deferrals outstanding (Cite-after-co-edit 409 → D1; relay-spoof → D2).
- **NO-GO** ⇒ stop and reshape before D1: any core-transport break (text doesn't sync, presence absent/wrong-identity, checkpoint loses content, reconnect fails to re-sync). The punch-list defines the fix.
- Read-only (d) and Cite fresh-section (c) are **strong signals, not GO gates**. A read-only **write leak** (member mutates the locked spec) or a fresh-section Cite failure that is *not* the known 409 is a punch-list defect worth weighing, but not a mechanical GO/NO-GO trip.

- [ ] **Step 4: Commit the findings**

```bash
git add docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md
git commit -m "$(cat <<'EOF'
docs(collab): Slice 3b-5d.D0 two-browser e2e findings + GO/NO-GO (3b-5d.D0)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: GO/NO-GO presentation (STOP — user-gated merge)

- [ ] **Step 1: Summarize for the user**
  - The GO/NO-GO verdict + the punch-list (new defects vs confirmed expected deferrals), and how far the live run got (full / partial / blocked-with-recipe).
  - The branch `collab-slice3b5d0` diff surface: `scripts/collab_e2e_seed.py`, `scripts/collab_e2e_run.sh`, `frontend/e2e/collab.spec.ts`, the findings doc — **no prod/backend/schema change**.

- [ ] **Step 2: STOP.** Do NOT merge or push. Await the explicit merge decision (as with every prior sub-slice). On "please merge": `git checkout master && git merge --no-ff collab-slice3b5d0` (local only), then record the outcome to memory. The verdict decides whether D1 starts next or the design is reshaped first.

---

## Self-Review

**Spec coverage (design §):**
- §3.1 two-user + active-draft/locked-spec + reference seed → Task 1. ✅
- §3.2 one-command runner → Task 2. ✅
- §3.3(a) convergence + distinct-name presence (+ checkpoint + persist) → Task 3 test (a). ✅
- §3.3(b) reconnect / re-sync → Task 3 test (b). ✅
- §3.3(c) Cite-via-room fresh section (+ documented 409) → Task 3 test (c) + Task 4 Step 2 manual observation. ✅ (409 deliberately documented, not asserted — avoids a flaky hard failure, per §3.3c "documented check".)
- §3.3(d) read-only member on locked spec + write-leak check → Task 3 test (d). ✅
- §4 run + findings doc → Task 4. ✅
- §5 GO/NO-GO (core-transport-only gate; read-only + Cite strong signals) → Task 4 Step 3 criteria + Task 5. ✅
- §6 inline execution → Execution Model. ✅
- §8 non-goals (no flip, no enable_collab caller, no prod CSP, no new CI gate) → Global Constraints. ✅

**Placeholder scan:** none — every step has concrete code/commands. The findings doc content is intentionally run-dependent (its *structure* is fully specified).

**Type/name consistency:** seed prints `E2E_COOKIE_OWNER/MEMBER`, `E2E_OWNER_LABEL/MEMBER_LABEL`, `E2E_SESSION_COOKIE` (=owner) → runner `get()` exports the same names → spec reads the same names (with `E2E_SESSION_COOKIE` fallback for `COOKIE_OWNER`). Selectors verified against source: `[data-testid="collab-editor"]`, `.collaboration-carets__caret`/`__label`, `button[aria-label="Cite"]`, `aside[aria-label="Add a citation"]` (`getByRole("complementary")`), `[data-testid="citation-pill"]`, `.ProseMirror[contenteditable]`, `?tab=spec`.

**Known fragilities (acknowledged, not blockers):** the Cite passage-selection (triple-click → `onMouseUp`) and the anchor-uniqueness of the double-clicked word are the least-robust interactions; per §3.3c/§5 a Cite failure that is not the known 409 is a weigh-it punch-list item, not a GO trip — if it proves flaky in the run, record it as a gap in the findings rather than forcing green.

**Codex plan-review findings folded (all three verified against source):**
- **High — seed stdout never flushes to the runner.** Python fully block-buffers a non-tty stdout; the seed then blocks in `while True: sleep(1)`, so the redirected `E2E_*` lines never reach the runner's `grep` → 150 s timeout. Fixed: `sys.stdout.flush()` after the seed's print block (Task 1 Step 4) **and** the runner launches it with `uv run python -u` (Task 2 Step 1).
- **Medium — shared-workspace stale state contaminates the fresh Cite.** Verified `direct_cite_via_kernel` validates `base_section_sha` against the **live room** section SHA ([kernel.py:88-92](../../../src/call_manager/proposal_assist/collab/kernel.py#L88)), while the toolbar derives it from git-backed `useDraft` sections — so uncheckpointed edits from scenarios (a)/(b) would 409 the "fresh" Cite from accumulated state, not the intended gap (serial mode alone would not fix it). Fixed: scenario (c) checkpoints + reloads both pages to a consistent git==live base before selecting (Task 3).
- **Medium — soft runner readiness/cleanup.** Fixed: hard readiness failure with seed+Vite log tails (no fall-through into Playwright); `setsid` process groups with a group-kill for Vite's esbuild/node children and a **SIGINT** to the seed group so its `KeyboardInterrupt` handler stops the testcontainers PG (SIGTERM would orphan it). The `curl` probes intentionally omit `-f` (a 401 from `/api/me` still proves the port is serving) — annotated in-script so it is not "fixed" back.
