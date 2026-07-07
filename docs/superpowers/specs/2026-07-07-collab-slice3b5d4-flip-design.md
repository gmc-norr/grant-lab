# Collab Slice 3b-5d·D4 — THE FLIP (Design)

**Date:** 2026-07-07
**Slice:** 3b-5d·D4 (fifth and FINAL sub-slice of the flip family; follows D0 e2e de-risk, D1a toolbar robustness, D1b stale-snapshot 409, D2 awareness relay-spoof filter, D3 post-checkpoint Tier-2 coverage).
**Status:** Design approved; ready for implementation plan.
**After D4:** collab is ON in prod; the 3b-5d family is complete. Post-flip roadmap (citations-as-node, structure ops, word-backlog Tiers 1–3, CM6 code deletion) is explicitly out of scope.

---

## 1. Goal & decisions (locked with the user)

Turn real-time collab editing ON in production — the terminal deliverable of the entire collab foundation (Slices 0→D3, all merged, flag OFF throughout). Five user decisions bind this design:

1. **Rollout = enable-on-touch, single mechanism (Approach A).** No `create_workspace` hook, no backfill script, no toggle UI. The enable seam lives in the artifact read models; new AND existing workspaces converge lazily. (The old roadmap line "auto-enable at create_workspace" is superseded — a new workspace's first cockpit load is a read, so the read hook covers it, and it sidesteps the untested "flag before repo exists" ordering entirely.)
2. **CM6 = sever now, delete later.** D4 adds 409 guards on the two `save_section` routes when the artifact is collab-enabled; the CM6 UI remains the never-rendered flag-off branch. Deletion is a later teardown slice (Jinja-teardown pattern: sever → delete).
3. **Tables = audit script + hold-back.** A shared pipe-table detector gates auto-enable (affected artifacts stay CM6) and powers a one-shot audit script listing workspaces to clean up manually. Hold-back is removable once the audit list is empty.
4. **Formatting floor = shortcuts + input rules.** Mod-B/Mod-I/Mod-Shift-S plus `## `/`- `/`1. ` input rules, strictly within the existing schema node/mark set.
5. **Rollback = kill-switch env var.** `COLLAB_KILL_SWITCH` makes `is_collab_enabled` return False everywhere; engaging it implies a restart (env change), which is itself the socket drain. No `disable_collab` is built (§8 sticky-forward).

## 2. Existing landscape (verified by exploration, 2026-07-07)

- **Flag** — per-`(workspace_id, artifact)` (`CollabArtifactState`, `db/models.py:1930-1947`); `is_collab_enabled` / sticky `enable_collab` in `collab/state.py:17-28`. **Zero prod callers of `enable_collab`** (tests + 2 dev scripts only) — the flip surface does not exist yet.
- **What the flag already gates** (all verified): WS connect (`web/collab_ws.py:73-85`, close 1008), accept routing (`web/proposals.py:157-206` → `accept_via_kernel` vs `_accept_git_first`), select-to-cite (`web/api/workspaces.py:858-997` → `direct_cite_via_kernel` vs git-first), canonical read projection (`collab/canonical.py` via `get_draft`/staleness/turns/agent tools), explicit checkpoint endpoint, spec lock/unlock freeze.
- **Frontend switch** — a server field, not a client flag: `collab_enabled: bool` on `DraftOut`/`SpecOut` (`workspaces.py:120,135`, populated :272-291/:326-337); `DraftPanel.tsx:65-88` and `SpecPanel.tsx:101-123` render `CollabEditor` (Tiptap) vs `ArtifactSectionEditor` (CM6) on it — mutually exclusive, no client override.
- **Ungated git-first writers** (the severance list — each would silently diverge room-vs-git when the flag is on):
  1. `editing/save.py::save_section` (:144-321, zero collab references) via `post_save_section` (`workspaces.py:774-855`, draft) and `post_save_spec_section` (:342-417, spec) — neither route checks the flag.
  2. `analysis/insert.py::insert_from_annotation` (:87-317) via `web/api/annotations.py::insert_annotation` (:190-245) — Claims-tab "insert citation"; reads `proposal.md` off disk, commits via `write_and_commit_files`, no flag awareness at all.
  3. `web/api/workspaces.py::post_init_spec` (:420-513) — writes `SPEC_SKELETON` unconditionally (:448) when the on-disk spec is whitespace-only; reads the flag only afterwards for the response (:480). The "Start spec from skeleton" CTA renders whenever all sections are empty, BEFORE the `collab_enabled` branch (`SpecPanel.tsx:44-58`) — reachable on day one post-flip.
  4. Latent only: `tools/citations.py` direct-commit mutators (`insert/remove/update_citation_impl`, `_rename_citekey_locked`) are NOT wired in `tools/registry.py` (:605-690 wires only `propose_citation_impl` + `list_citations_impl`) — dead code, waived (§8).
- **`get_spec` is still git-canonical** (`workspaces.py:318` reads the raw file) while `get_draft` branches to `canonical_artifact_markdown` when enabled (:275-280) — post-flip the Spec Outline SHAs would lag live edits until a checkpoint.
- **`AddCitationOut.citekey: str`** (`workspaces.py:184-187`) vs kernel `citekey: str | None` (`kernel.py:55,131`) — idempotent re-cite of a citekey-less ledger row → pydantic failure → 500 (3b-5a §deferred, "fix at the flip").
- **CSP** — `build_csp` (`web/middleware.py:20-59`): prod AND disabled share `connect-src 'self'` (no WS token, line 37); only dev adds `ws: http://localhost:5173` (line 40). Wired in `main.py:325,341-347` + mirrored on the body-cap early-reject path (:355-361). The e2e seed forces `SPA_MODE=dev` explicitly because of this gap (`scripts/collab_e2e_seed.py:53-57`); `frontend/e2e/collab.spec.ts:12-13` says "Do NOT change prod CSP here (D4)".
- **Frontend WS URL** is always same-origin, scheme-derived (`useCollabDoc.ts:5-8`: `wss:` under https, `ws:` under http). `check_origin` (`ws_auth.py:65-90`) compares the browser's document Origin against `APP_BASE_URL` via the CSRF `_hostport` normalizer — scheme is part of the key, so `APP_BASE_URL` must match real serving scheme (it already independently drives `cookie_secure` and HSTS; config-alignment, not code).
- **Enabling late is structurally safe**: `load_or_hydrate` seeds the room from git HEAD exactly once when the update store is empty (`hydrate.py:26-32`; repo-less/no-commit cases no-op cleanly, `checkpoint.py:82-89`), and canonical reads fall back to raw HEAD while the log is empty — so a long-lived active workspace can be enabled at any read with no migration.
- **Closed prerequisites** (D4 must NOT redo): C1/C2/C3 hardening (3a `aeeb2b9`), server hardening + write-path GO (3b-1 `e4d5442`), Tiptap frontend (3b-3 `16e4bca`), presence (3b-4 `b6e60e3`), citation parity (3b-5a `9832210`), presence follow-ups (3b-5b `205f561`), tail-bounding (3b-5c `b62bdbe`), e2e harness (D0 `d024ea9`), settle-latch + disclosure-ack C#2 (D1a `d71d0e9`), stale-snapshot C#3 (D1b `421ab9c`), relay-spoof filter (D2 `249ed6e`), Tier-1/2 re-fire (D3 `629fc75` — Tier-2 done; Tier-1 needs no code by design).

## 3. Architecture — eight components

### 3.1 Component A — enable-on-read hook (new `collab/flip.py`)

```python
def has_pipe_tables(md: str) -> bool: ...
    # conservative: a line containing >=2 unescaped pipes outside fenced code blocks,
    # in the shape of a table row/delimiter; false positives acceptable (they only
    # delay enablement), false negatives are the failure to avoid.

def maybe_auto_enable(session: Session, *, workspace_id: uuid.UUID, artifact: str, head_md: str) -> bool:
    """Sticky lazy flip: enable collab for this artifact iff the kill-switch is off,
    it is not already enabled, and head_md has no pipe tables (hold-back).
    Returns the effective enabled state. Tolerates the concurrent-first-read race
    (two simultaneous GETs both inserting CollabArtifactState -> the loser treats
    the conflict as enabled). Committed with the request's session."""
```

Called from exactly two places — the draft and spec read models in `web/api/workspaces.py`, where `collab_enabled` is already computed and HEAD markdown is already read. When it enables, the SAME response returns `collab_enabled: true` → Tiptap mounts on that load; the room hydrates from HEAD at first WS connect. Per-artifact; setup-phase workspaces enable too (WS access is already owner-only in setup via `resolve_access`). Soft-deleted workspaces never reach the hook (read models 404 first).

### 3.2 Component B — kill-switch (`COLLAB_KILL_SWITCH`)

New settings flag (env `COLLAB_KILL_SWITCH`, default off), honored **inside `is_collab_enabled`** — the single choke point every gate already consults (WS connect, accept/cite routing, canonical reads, checkpoint endpoint, spec-lock freeze, the new guards of §3.3, and `maybe_auto_enable`). Must not require threading settings through call sites (module-level accessor; exact mechanism = plan detail). Engaging it is an env change → restart: in-memory rooms die (persisted Yjs log stays canonical — no data loss), new connects refuse 1008, every writer takes its git-first branch, reads fall back to git HEAD. The 3b-5c tail-scan selects candidates by uncheckpointed tail size, NOT by flag — **the plan must verify `find_checkpoint_candidates` is flag-independent and add a regression test that the scan drains tails under kill-switch** — so within one scan interval (60s) git is current and the git-first world is coherent. Un-set + restart to resume; rooms replay from the preserved log. `enabled_at` rows are never cleared.

### 3.3 Component C — writer severance

- **CM6 saves**: `post_save_section` + `post_save_spec_section` gain a guard — when the artifact is (effectively) collab-enabled → **409** with a body telling the client to reload (a stale CM6 tab reloads into Tiptap). `save_section` itself is untouched (it still serves the flag-off/kill-switch world).
- **Claims-tab insert**: `insert_annotation` gains the `post_citation` `_route()` shape — flag-on routes through a room-aware path built on the existing kernel citation machinery (`apply_citation_to_room` / the `direct_cite_via_kernel` composition); flag-off calls `insert_from_annotation` byte-for-byte unchanged. **Fallback (second choice, only if the plan phase shows the annotation offset logic resists room routing): 409 + disabled Claims-tab button with hint.** Feature-preserving routing is the goal.
- **Spec skeleton**: `post_init_spec` flag-on routes the skeleton through the kernel as a room write + checkpoint (an open empty room does NOT re-hydrate from a git-side write — the store already holds the empty seed — so a git write here can diverge); flag-off unchanged.

### 3.4 Component D — `get_spec` room-canonical

Mirror `get_draft`'s branch: when enabled, sections derive from `canonical_artifact_markdown` (Spec Outline SHAs track the live room). Flag-off read unchanged.

### 3.5 Component E — `AddCitationOut.citekey` Optional

`citekey: str | None` (`workspaces.py:184-187`) + OpenAPI regen (`EMBEDDING_MODEL=bge-m3` gotcha) + verify the frontend consumer tolerates null.

### 3.6 Component F — narrow same-origin CSP WS allowance

`build_csp` non-dev derives one explicit token from `APP_BASE_URL`: `https://…` → `wss://<hostport>`, `http://…` → `ws://<hostport>`, appended to `connect-src 'self'`. Everything else stays strict; dev mode unchanged; the body-cap mirror (`main.py:355-361`) picks it up automatically (same built string). Explicit token, not CSP3 `'self'`-covers-ws (engine-dependent). Tests assert the exact prod string for both schemes. Runbook note: `APP_BASE_URL` scheme must match real serving — it already drives `check_origin`, `cookie_secure`, HSTS.

### 3.7 Component G — formatting floor (frontend-only)

`frontend/src/collab/schema.ts`: `addKeyboardShortcuts` (Mod-B → bold, Mod-I → italic, Mod-Shift-S → strike) + `addInputRules` (`## `→heading of matching level for the levels the schema supports, `- `→bullet list, `1. `→ordered list). **Constraint: no new nodes/marks, no schema shape change** → Python bridge, goldens, and `collab-schema.mjs` twin untouched. Vitest coverage for each shortcut/rule.

### 3.8 Component H — table audit script

`scripts/collab_table_audit.py`: iterate live workspaces' repos, run `has_pipe_tables` on HEAD `proposal.md` + `proposal-spec.md`, print `(workspace_id, title, artifact)` rows. One-shot, read-only, imports the same detector as the hold-back (one definition).

## 4. Rollout narrative & rollback runbook

```
deploy D4
  └─ nothing changes until a workspace artifact is READ
       get_draft/get_spec → maybe_auto_enable (kill-switch off, no tables) → collab_enabled: true
         └─ Tiptap mounts → first WS connect hydrates room from git HEAD → room canonical
       held-back artifacts (tables) → stay CM6 until cleaned + re-read; audit script lists them
rollback (incident)
  └─ set COLLAB_KILL_SWITCH=1 + restart (env implies restart; restart = socket drain)
       → is_collab_enabled False everywhere: CM6 renders, saves work git-first, WS refuses
       → tail-scan drains any uncheckpointed log tail to git within ~60s (flag-independent)
       → fix forward; unset + restart; rooms replay from preserved log
```

## 5. Error handling & threading

- `maybe_auto_enable` runs on the request's sync session in the read models (Starlette threadpool) — pure DB DML + a string scan; no git, no room `MANAGER`. The concurrent-first-read insert race resolves via conflict-tolerant insert (treat conflict as enabled).
- The 409 save-guards and routing branches read the flag OUTSIDE any lock (cheap, same as `accept()`'s `_route()`); the kernel paths they route to carry their own established locking (room lock → FOR UPDATE → commit mutex).
- Kill-switch reads must be race-free trivially (a bool env read); no live-socket teardown code — the restart IS the teardown, and checkpoint-on-evict/last-leave/tail-scan bound git lag.
- CSP derivation happens once at app startup (build_csp already does); a malformed `APP_BASE_URL` fails at startup, not per-request.

## 6. Testing

- **flip.py unit**: enables once (sticky); returns-enabled on repeat; hold-back on table docs (incl. fenced-code false-positive guard); kill-switch suppresses; concurrent-first-read race (two sessions) tolerated.
- **Read-model integration**: first `get_draft`/`get_spec` flips `collab_enabled` false→true in ONE request; setup-phase workspace enables; repo-less/mid-Phase-0 workspace read → no 500, no premature row (whatever the read model does today pre-repo stays); held-back artifact stays false and CM6 path remains usable (save route NOT 409'd).
- **Severance**: CM6 save routes → 409 when enabled, byte-for-byte behavior when disabled (parity tests unchanged); annotation-insert routes room-aware when enabled (room content + ledger + checkpoint commit) and git-first unchanged when disabled; `post_init_spec` seeds via room when enabled (open-empty-room case gets the skeleton live) and git-first when disabled.
- **get_spec canonical**: enabled spec read reflects un-checkpointed room content (mirror the existing get_draft tests).
- **Kill-switch**: with env set — WS connect 1008 even for an enabled artifact; save routes revert to git-first (no 409); auto-enable suppressed; tail-scan still checkpoints (the §3.2 verification test).
- **CSP**: exact prod string with `wss://` (https base) and `ws://` (http base) tokens; dev string unchanged; disabled-mode… gets the same non-dev string (assert).
- **Frontend**: vitest for shortcuts + input rules; tsc; OpenAPI regen drift check (citekey nullable).
- **Acceptance gates**: grouped backend sweep incl. `tests/test_api_spec.py` (`EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, never whole-suite) · `ruff check .` · `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (+ any touched suites) · **two-browser e2e TWICE**: existing dev-mode run AND a new `SPA_MODE=prod` run against the built bundle with the new CSP (seed/runner parameterized — this is the flip's proof) · **manual DoD**: paste-from-Word sanity in Tiptap (backlog §5 note: untested against real Word HTML).

## 7. Global constraints (verbatim)

- **This slice ENDS flag-off parity as the merge bar** — post-merge, flag-ON is the prod path. Flag-off branches remain (kill-switch world) and their parity tests must still pass unchanged.
- **No migration, no schema change** (reuses `CollabArtifactState`; kill-switch is env-only). Backend + one frontend task.
- No new locks; no lock-ordering changes (routing branches read the flag outside locks; kernel paths unchanged).
- Explicit-path `git add` only; merges are **local `--no-ff` only**, not pushed without an explicit ask; do NOT invoke `/code-review` / `/ultrareview`; Codex findings are authoritative-but-verify-against-code.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## 8. Explicit waivers & satisfied invariants (cited so they are not rediscovered)

- **Sticky-forward invariant** (foundation spec): satisfied structurally — no `disable_collab` exists or is built; the kill-switch is a global pause that respects checkpoint-first via restart + flag-independent tail drain; `enabled_at` is never cleared.
- Waived, documented, accepted: identity-from-binding self-spoof (D2 §8, post-flip optional); D2's four residuals (reconnect-clock invisibility, same-user two-socket overlap, binding-reset race, half-open ghost caret ≤30s); quiet-checkpoint version gap (3b-5c); theoretical checkpoint-vs-reconcile deadlock (D3 §5, self-healing; mitigation documented); Yjs log squash + full poison-broadcast (Slice-1 debt); C#4 heading off-by-one + C#6 no-disabled-hint + Approach-3 toolbar git-uniqueness gate (fail-safe polish); debounced-idle checkpoint trigger (superseded by 3b-5c's periodic scan); unwired direct-commit citation tools (dead code — any future re-registration must add propose/accept indirection).
- Out of scope (post-flip roadmap): word-backlog Tiers 1–3, GFM tables, citations-as-node, structure ops/stable section IDs, CM6 code deletion (teardown slice), per-workspace disable UI.

## 9. Key references

- `src/call_manager/proposal_assist/collab/state.py:17-28` — flag primitives; `db/models.py:1930-1947` — per-artifact rows.
- `src/call_manager/web/api/workspaces.py` — read models (:272-291, :318-338), save routes (:342-417, :774-855), `post_init_spec` (:420-513), `post_citation` `_route()` (:858-997), `AddCitationOut` (:184-187).
- `src/call_manager/web/proposals.py:141-206` — the `_route()` pattern to mirror.
- `src/call_manager/web/api/annotations.py:190-245` + `analysis/insert.py:87-317` — Claims-tab insert to route.
- `src/call_manager/web/middleware.py:20-59` + `main.py:325-361` — CSP; `ws_auth.py:65-90` — origin check.
- `frontend/src/cockpit/DraftPanel.tsx:65-88`, `SpecPanel.tsx:44-123` — editor ternary + skeleton CTA; `frontend/src/collab/schema.ts` — formatting floor site; `useCollabDoc.ts:5-8` — WS URL.
- `scripts/collab_e2e_run.sh` + `scripts/collab_e2e_seed.py:53-57,100-110` — e2e harness to parameterize for prod mode.
- `collab/hydrate.py:26-32`, `collab/checkpoint.py:82-89` — why late/early enabling is safe.
