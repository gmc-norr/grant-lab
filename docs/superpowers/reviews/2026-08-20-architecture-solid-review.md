# Call-Monitor Architecture Review — SOLID & clean code

**Date:** 2026-08-20
**Method:** 14-agent review. Nine independent subsystem readers (SOLID + clean-code lens, file:line evidence required), then adversarial verification of every high-severity claim (verifier instructed to *refute* by reading the cited code and its callers; dedup per file). 82 findings total, 6 high, 5 verified.
**Legend:** CONFIRMED = survived a refutation attempt. REFUTED = the code contradicted the claim's harm story or severity. Unmarked findings were not independently verified.

---

## Verdict

**This is a healthy architecture.** Multiple reviewers primed to find violations called their subsystems "one of the cleanest I have reviewed." The load-bearing boundaries are real: source adapters behind a Protocol + DTO, LLM vendors confined to two adapter files, the drafting SDK confined to one, the collab write kernel as a single choke point, DTO-only API responses, and invariants pushed into the database and into structural gates (`workspace_write`, the route-ownership manifest, the import gate). The CLAUDE.md "model-agnostic, prompt-in/JSON-out" claim is verifiably true in the code.

The dominant debt is **not** coupling or god-objects — it is **copy-paste ceremony that has already drifted**. Of five high-severity claims, the two "god module" claims about the web layer were *refuted* on verification; the three that survived are all the same species: a correct mechanism duplicated four times and maintained by hand. Second theme: several critical invariants are enforced by comments and discipline in a codebase that demonstrably knows how to build mechanical gates.

## Scorecard

| Subsystem | Grade | One-line verdict |
|---|---|---|
| Ingestion & sources | A− | Registry + Protocol + DTO make new sources genuinely additive; debt is contained copy-paste in extraction helpers. |
| Ranking, embeddings & LLM | A− | Model-agnosticism verifiably true; vendor SDKs in exactly two adapter files; scoring core is pure functions. |
| React SPA | A− | Feature-sliced, small components, enforced generated-types API boundary; debt is implicit contracts tsc can't see. |
| Web / API layer | B+ | Strong bones (error envelope, DTO-only, consolidation helpers); both of its high-severity claims were refuted. |
| Collaborative editing | B+ | Rigorous dependency-inverted core; the confirmed problem sits at the kernel's edges, not its heart. |
| Proposal-assist core | B | Hard problems each have a single named owner; debt concentrates in the tool layer (ToolContext, registry). |
| Ports & fence (F3) | B | Best-engineered boundary in the repo — but zero production consumers until F3d lands. |
| Data layer | B | Invariants live in the database; migrations disciplined; models.py is a 2,037-line monolith with drifting copy-paste. |
| Background jobs & lifecycle | B− | Exemplary locking substrate under a four-fold clone of the job-family machinery with demonstrated drift. |

## The three CONFIRMED high-severity problems

### 1. ToolContext is a 28-field god object — `src/call_manager/proposal_assist/tools/registry.py:47`
28 mutable fields serving six actor roles (per-workspace wiring, per-turn provenance, fencing identity, read-view snapshots, …) inside a 1,021-line composition root whose 657-line `_collect_tools` makes every tool addition a multi-file edit. The misattribution race it invites is code-acknowledged: the `Invocation` snapshot was built specifically to patch it, and no production tool consumes it yet. Independently flagged by two readers. **The F3a–F3d fence program is the existing payment plan; the missing piece is the F3d conversion.**

### 2. Evict-or-not decision copied four times in collab orchestration — `src/call_manager/proposal_assist/citations/collab_cite.py:44`
`accept_via_kernel` (proposals/kernel_accept.py:56–124), `direct_cite_via_kernel` (collab_cite.py:49–96), `init_spec_via_kernel` (99–146), `insert_annotation_via_kernel` (149–203) each repeat the same ~45-line ceremony — `MANAGER.operate` → room lock → threadpool body → evicted recheck → commit → broadcast-outside-lock — with hand-maintained exception tuples deciding eviction. **Decisively: this exact defect class has already shipped one real bug.** The kernel's own mutation-state signal goes unread in production. Strongest candidate for extraction into one orchestration helper.

### 3. Four-fold clone of job-queue + pass-runner machinery, already drifted — `src/call_manager/proposal_assist/uncited_claims/pass_runner.py:180`
coherence/, evaluator/, uncited_claims/ each carry a structurally identical `jobs.py` + `pass_runner.py` (enqueue-with-SAVEPOINT, claim FOR UPDATE SKIP LOCKED with `of=` + same-statement generation capture, atomic UPDATE…RETURNING cancel, Stockholm-day cap, TTL reaper); review/ is a bespoke fan-out variant. Measured difflib ratios 0.48–0.61. **Drift demonstrated:** two of four families drop late token spend from their daily caps (fires when a running job is terminalized mid-turn — realistic trigger is soft-delete cancel; matters for same-day restore + usage accounting, cannot cause unbounded burn); one lacks the pre-launch delete recheck the others gained. Hardening already applied 3–4× by hand. A parameterized family core converts the 4× fix cost to 1×.

## Two REFUTED high-severity claims (don't spend time here)

- **"workspaces.py is a god-module"** (`web/api/workspaces.py:461`) — Large (1,537 lines) but mostly a *thin* REST router: maintained write routes delegate ceremony to services (`save_section`, `direct_cite_via_kernel`, `init_spec_via_kernel`, `soft_delete_workspace_db`); routes hold only authz ordering, prechecks, and exception→HTTP mapping. The thickest inline block (post_init_spec's `_git()` branch) is an explicitly comment-marked retiring flag-off-only path.
- **"Exception-to-HTTP mapping is copy-pasted shotgun surgery"** (`web/api/proposals.py:105`) — Counts accurate (21 `except CommitMutexTimeout` arms, 37 Retry-After sites) but both "silent divergences" are deliberate and commented (IndexError→409 at workspaces.py:1073 is documented parity with flag-off CiteStale→409; the rollback asymmetry is structural — kernel chains have no route session in scope). Cross-cutting exceptions get one app-level handler (`WorkspaceRetired` is the in-repo counterexample to the shotgun-edit scenario).

## Cross-cutting themes

1. **The debt species is DRY-drift, not bad boundaries.** Job families ×4, evict ceremonies ×4, job-table column blocks ×7 in models.py (identical 8-line comment; status vocabulary drifted), SSRF/stream-fetch skeleton ×2, dead-letter SQL ×3, stringly TanStack query keys across the SPA.
2. **Comment-enforced invariants in a repo that knows how to build gates.** Lock order (mutex<jobs<workspaces<calls) lives in ~25 comments; "all markdown mutations route through the kernel" is per-site convention; session-per-unit-of-work under `expire_on_commit=False` is a comment; httpx IP-pinning rides on two underscore-private attributes no test exercises. The import gate / AST gate / route manifest trick applies to each.
3. **The ports layer is excellent and unused — F3d is the risk concentration.** `build_ports` has zero callers, all nine gated tool modules are exempt, three semantic effects exist as hand-synchronized duplicates whose live copies violate the rules the ports codify. Until F3d, two systems of record for the most correctness-critical writes.
4. **Two session idioms + a service-locator habit in the web layer.** `app.state` reached into from ~95 handler sites alongside a proper DI idiom; cockpit vs rest-of-API acquire sessions differently.

## All findings by subsystem

### Ingestion & sources — 6 strengths, 9 findings, 0 high
One of the healthiest subsystems: data-driven registry + structural Protocol + single upsert chokepoint with defense-in-depth URL/bounds guards, three-tier error taxonomy, every network seam test-injectable.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| Med | SRP | sources/types.py is a grab-bag (retry, SSRF, transport) misnamed as a types module | `sources/types.py:21` |
| Med | Leaky abstraction | IP pinning reaches into httpx/httpcore private internals (`_pool`, `_network_backend`) on the production fetch path; only production exercises it | `sources/types.py:233` |
| Med | DRY | Stream-fetch skeleton duplicated between the two extraction helpers | `sources/extraction.py:198` |
| Med | LSP | SourceAdapter's written promises (streaming, needs_body signature) diverge from what the orchestrator enforces (`list()` + `inspect.signature`) | `ingestion/orchestrator.py:25` |
| Med | OCP | Adding a source really takes 4 coordinated edits (registry, adapter, config, seed migration); docstring claims 2; failure mode = runtime LookupError | `sources/registry.py:3` |
| Low | SRP | EU-portal normalization performs blocking network I/O inside `_normalize` | `sources/eu_portal.py:257` |
| Low | Consistency | Transport-security policy inconsistent across adapters (https enforcement) | `sources/vinnova.py:121` |
| Low | Coupling | Four copies of the same "local import avoids circular" workaround | `ingestion/alerts.py:80` |
| Low | Cleanliness | CLI distrusts its own dataclass via getattr defensiveness | `ingestion/cli.py:144` |

### Ranking, embeddings & LLM — 7 strengths, 9 findings, 0 high
Vendor SDKs confined to two adapter modules (httpx in ollama_client.py, anthropic SDK in anthropic_client.py); AnthropicClient adapts to the OpenAI envelope so downstream parsing is shared; scoring core is pure functions, zero SQLAlchemy/HTTP imports; escalation has a hard "remote failures never propagate" contract; spend ledger in its own transaction.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| Med | DIP | Chat-client contract exists only as comments at every consumer — no shared Protocol | `ranking/rank_pair.py:69` |
| Med | Cohesion | Shared LLMError lives inside the Ollama adapter module | `embeddings/client.py:16` |
| Med | DRY | Dead-letter suppression rule duplicated across three raw-SQL sites | `ranking/backfill.py:18` |
| Med | SRP | drain_queue is a 270-line function mixing five responsibilities | `ranking/worker.py:31` |
| Low | Coupling | ranking imports a private helper from proposal_assist internals | `ranking/remote_attempts.py:34` |
| Low | LSP | AnthropicClient's "drop-in replacement" claim fails on the local-path signature | `ranking/anthropic_client.py:1` |
| Low | Complexity | RankResult 17-field construction repeated at four sites | `ranking/rank_pair.py:262` |
| Low | Encapsulation | validate_ev imports private helpers from rank_pair | `ranking/validate_ev.py:26` |
| Low | DRY | Report paths/thresholds resolved from env at import time, in duplicate | `ranking/eval.py:31` |

### Web / API layer — 8 strengths, 11 findings, 2 high (both REFUTED)
Single-source-of-truth error envelope down to raw-ASGI middleware, centralized exception handlers, DTO-only responses, standout consolidations (workspace_write, gate_cloud_llm, route manifest). Debt is Jinja-migration + collab-flag leftovers.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| REFUTED | SRP | workspaces.py "god-module" — verified as a mostly-thin router delegating to services | `web/api/workspaces.py:461` |
| REFUTED | DRY | Exception-mapping "shotgun surgery" — divergences deliberate; app-level handlers exist | `web/api/proposals.py:105` |
| Med | OCP | Two-to-three mounting conventions split the /api surface; four older proposal-assist routers return untyped dicts, bypassing the typed-response conventions the TS codegen gate depends on | `proposal_assist/web/proposals.py:38` |
| Med | Consistency | Error envelope's machine-readable `code` is unreachable from the dominant raising pattern | `web/api/errors.py:71` |
| Med | DRY | Workspace-status allowlist and title cap defined twice, mirroring a third source of truth | `proposal_assist/web/workspace_meta.py:16` |
| Med | Consistency | Contradictory existence-disclosure policies: shared guards vs collab-path guards | `proposal_assist/web/_perms.py:25` |
| Med | Leaky abstraction | Cross-module imports of underscore-private helpers, incl. web importing agent-tools internals | `web/api/references.py:35` |
| Med | DIP | Service-locator style: app.state reached into from ~95 handler sites; two session idioms coexist | `web/api/workspaces.py:268` |
| Low | Leaky abstraction | Lifespan reads controller privates; dashboard service raises FastAPI HTTPException | `main.py:316` |
| Low | Docs | Middleware docstring contradicts the CSRF middleware's implementation choice | `web/middleware.py:4` |
| Low | DRY | Four near-clone run-trigger endpoints with hand-mirrored gate ordering | `proposal_assist/web/coherence.py:44` |

### Proposal-assist core — 8 strengths, 9 findings, 1 high (CONFIRMED)
Turn claim/finalize idempotency, subprocess lifetime vs advisory locks, retirement fan-out, propose-only enforcement each have a single named owner with documented invariants. DIP seams that matter are clean (DraftingAgent adapter confines the SDK to one file; fake adapter; hook-based retirement coordinator; single `record_terminal_outcome` writer shared by five callers).

| Sev | Principle | Finding | Location |
|---|---|---|---|
| **High (CONFIRMED)** | God object | ToolContext: 28 mutable fields serving six actor roles (see confirmed problems above) | `tools/registry.py:47` |
| Med | OCP | `_collect_tools` is a 657-line closure factory; adding a tool is shotgun surgery across 3–4 files | `tools/registry.py:364` |
| Med | Leaky abstraction | Human accept path imports private agent-tool internals and fakes a ToolContext | `proposals/accepting.py:195` |
| Med | LSP | DraftingAgent Protocol names about half the real adapter contract; rest via getattr / direct calls on `object` | `drafting/contract.py:146` |
| Med | DRY | propose_citation_impl hand-duplicates the verification pipeline of compute_citation_insert | `tools/citations.py:455` |
| Med | SRP | submit_user_turn: ~200 lines mixing mutex policy, IDOR validation, image probing, cap accounting, persistence | `turns/submit.py:179` |
| Low | Coupling | Controller reaches into the registry's private `_adapter` | `drafting/controller.py:190` |
| Low | DRY | Per-turn deny gate applied inconsistently (double-checked in two closures, single elsewhere) | `tools/registry.py:413` |
| Low | ISP | set_turn_provenance is workspace-keyed while the rest of the contract is handle-keyed → shared mutable sink dict | `drafting/contract.py:189` |

### Collaborative editing — 7 strengths, 8 findings, 1 high (CONFIRMED)
Room/manager core genuinely dependency-inverted; concurrency invariants stated and defended at acquisition sites; flag/severance/kill-switch seam let collab layer on through one choke point.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| **High (CONFIRMED)** | Leaky abstraction | Evict-or-not encoded in four copied ceremonies with hand-maintained exception tuples; kernel's own evict signal production-dead (see confirmed problems above) | `citations/collab_cite.py:44` |
| Med | Coupling | Write kernel imports private helpers from three peer packages, incl. domain logic stranded in agent-tools | `collab/kernel.py:57` |
| Med | DRY | Section-edit rules duplicated between kernel_accept_prose and save_section; collab error taxonomy owned by the retiring git-first module | `collab/kernel.py:242` |
| Med | Leaky abstraction | "All markdown mutations route through the kernel" enforced by per-site convention, not structurally | `collab/state.py:57` |
| Med | OCP | checkpoint_artifact accretes caller-mode booleans and a hard-coded cross-subsystem side effect | `collab/checkpoint.py:77` |
| Low | ISP | apply_citation_to_room wears two behavioral hats via mode flags on a 17-parameter signature | `collab/kernel.py:98` |
| Low | DRY | ProposedChange resolution ceremony copy-pasted seven times inside kernel.py | `collab/kernel.py:224` |
| Low | Cohesion | Minor encapsulation wobbles in Room/RoomManager | `collab/room.py:589` |

### Ports architecture & fence work (F3) — 6 strengths, 7 findings, 1 high (shared: ToolContext)
Each Effects method owns lease acquisition, a single fenced statement, transaction scope, and a race-safe refusal taxonomy — enforced mechanically at four layers (fresh-interpreter transitive-import proof, fail-closed DTO walker, three-direction exemption ratchet). Honest counterweight: none of it runs in production yet; the gate currently enforces a no-drift ratchet, not the boundary. Risk concentrates in the F3d conversion window (Queries port must grow ~10x; CommitMutexTimeout gap must close; ToolContext must be dismantled).

| Sev | Principle | Finding | Location |
|---|---|---|---|
| **High (CONFIRMED)** | God object | ToolContext (same as core — independently flagged by this reader too) | `tools/registry.py:46` |
| Med | DIP | Entire ports layer has zero production consumers (`build_ports` zero callers; `pass_invocation` never set; all nine gated tool modules exempt) | `port_impl/__init__.py:24` |
| Med | DRY | Three semantic effects implemented twice, kept in sync by comments and duplicated string literals | `port_impl/effects.py:128` |
| Med | DRY | Live accounting code contradicts the rule its own port codifies (workspace-bound accounting, unfenced ORM writes) | `tools/references.py:467` |
| Med | ISP | Port error taxonomy incomplete: the "busy, retry" exception (CommitMutexTimeout) cannot be named by a gate-compliant tool | `ports/effects.py:87` |
| Low | ISP | Queries port covers a sliver of the real read surface, with heavyweight per-method ceremony | `ports/queries.py:42` |
| Low | Leaky abstraction | Locator-name rule is token-based, launderable through the permanently-skipped composition root | `tests/fencing/import_gate.py:84` |

### Background jobs & lifecycle — 9 strengths, 8 findings, 1 high (CONFIRMED)
The lifecycle/retirement/locking core is exemplary: single monotonic authority replacing scattered tombstones; hook-registry coordinator failing boot on a missing hook; typed contention-vs-deletion errors; every timeout constant carries its derivation plus a pinning test; ticks are thin closures over pure never-committing helpers; single-instance constraint handled with an advisory lock that degrades safely. Debt is horizontal.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| **High (CONFIRMED)** | DRY | Four-fold clone of job-queue + pass-runner machinery with demonstrated drift (see confirmed problems above) | `uncited_claims/pass_runner.py:180` |
| Med | Complexity | build_scheduler is a 936-line composition god-function; drain/reaper tick inlined 6+ times | `scheduler.py:189` |
| Med | Consistency | Dual configuration paths into the scheduler (unpacked kwargs and Settings both supply values) | `scheduler.py:200` |
| Med | OCP | Delete-time job cancellation is a hard-coded per-family fan-out, parallel to the extensible hook registry the same delete uses in-process | `workspaces.py:178` |
| Med | Complexity | assemble_for_user: ~305 lines mixing candidate queries, dedup policy, presentation capping | `digest/assemble.py:73` |
| Low | Consistency | Normative lock order lives in ~25 comments with only site-local enforcement | `proposal_assist/fencing.py:135` |
| Low | Cohesion | Reaper contract shape inconsistent across the five reaper functions | `turns/reaper.py:43` |
| Low | Complexity | run_digest inlines the whole per-user send state machine at four nesting levels | `digest/send.py:84` |

### Data layer — 8 strengths, 10 findings, 0 high
Transaction-script over anemic ORM (no repository layer outside the new ports/), but disciplined where it matters: paired-null CHECKs, partial unique indexes encoding one-active-job-per-workspace, lowercase-email CHECK; all 64 linear migrations have real (sometimes deliberately-lossy-and-documented) downgrades; sessions passed explicitly; workspace_write is a model consolidation.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| Med | SRP | models.py is a 2,037-line monolith spanning four-plus bounded contexts | `db/models.py:36` |
| Med | DRY | Job-table column block copy-pasted seven times, incl. an identical 8-line comment; drifted | `db/models.py:913` |
| Med | God object | Workspace table accretes per-feature columns into a 27-column hot row (which the commit-mutex/fence machinery exists partly to police) | `db/models.py:620` |
| Med | DIP | Two competing session-acquisition idioms: DI dependency vs app.state service locator | `web/api/profiles.py:248` |
| Med | DRY | Profile validation caps duplicated between fields.py and forms.py with conflicting over-limit policy | `profiles/forms.py:11` |
| Low | Naming | Terminal-status vocabulary and enum strategy inconsistent across the eight job tables | `db/models.py:946` |
| Low | Convention | Session-per-unit-of-work invariant under expire_on_commit=False enforced only by a comment | `db/session.py:52` |
| Low | Leaky abstraction | forms.py raises HTTPException from inside the profiles domain package | `profiles/forms.py:38` |
| Low | Typing | Untyped session/profile parameters in profiles service functions | `profiles/create.py:56` |
| Low | LSP | Call.structured property duck-types the model into another module's interface | `db/models.py:221` |

### React SPA — 8 strengths, 11 findings, 0 high
Largest production file 362 lines; generated OpenAPI types with a single unwrap() error chokepoint; deliberate three-tier state layering (TanStack Query / URL params / minimal editor store); framework-free injectable CollabProvider under a StrictMode-safe lifecycle hook. Debt: implicit contracts tsc can't see, no linter in the gate.

| Sev | Principle | Finding | Location |
|---|---|---|---|
| Med | Coupling | Shell chrome hard-wires cockpit knowledge instead of using the existing route-handle extension point | `frontend/src/shell/StatusBar.tsx:3` |
| Med | DRY | Stringly-typed TanStack query keys duplicated with no key factory | `frontend/src/cockpit/chat/CockpitChat.tsx:52` |
| Med | Fragility | Disclosure 403 detected by regex on the human-readable error message, not a machine code | `frontend/src/collab/TiptapSelectionToolbar.tsx:85` |
| Low | Cohesion | editorStore is a cross-cutting global singleton filed under the cockpit feature directory | `frontend/src/cockpit/editorStore.ts:10` |
| Low | DIP | API layer imports a type from a feature directory | `frontend/src/api/turns.ts:3` |
| Low | SRP | CockpitSidebar mixes five navigation groups with an embedded rubric-editing workflow | `frontend/src/cockpit/CockpitSidebar.tsx:57` |
| Low | SRP | LibraryPanel bundles references CRUD, DOI fetch, file management, and an upload queue | `frontend/src/cockpit/LibraryPanel.tsx:67` |
| Low | DRY | Local Turn type duplicated in two chat files instead of deriving from the generated schema | `frontend/src/cockpit/chat/CockpitChat.tsx:12` |
| Low | DRY | Two divergent hard-coded score-band threshold schemes for the same aggregate score | `frontend/src/routes/CallDetailBody.tsx:31` |
| Low | Tooling | No linter in the build gate; vestigial eslint-disable comments reference rules that never run | `frontend/package.json:11` |
| Low | OCP | The `citations` boolean on ArtifactSectionEditor is a flag argument that really means "draft mode" | `frontend/src/editor/ArtifactSectionEditor.tsx:38` |

## Suggested order of work

1. **Finish F3d (convert the tools to the ports).** Already the plan of record; retires ToolContext; until it lands the codebase carries two systems of record for its most correctness-critical writes. Close the CommitMutexTimeout gap in the port error taxonomy while there.
2. **Extract the evict ceremony.** One orchestration helper replacing the four ~45-line copies; this defect class already produced one shipped bug.
3. **Parameterize the job-family core.** One queue+runner skeleton for coherence/evaluator/uncited-claims/review; backport the two drifted fixes (daily-cap spend, pre-launch delete recheck) as part of the extraction.
4. **Cheap-now consolidations, any order:** job-table mixin in db/models.py; SPA query-key factory + machine error code (replacing the 403 message-regex); move LLMError out of the Ollama adapter; name the chat-client Protocol; unify the two session idioms as routes get touched.
5. **Turn comment-invariants into gates where cheap:** the lock order and the route-through-the-kernel rule are both candidates for the same AST/import-gate treatment the repo already uses elsewhere.

---

*Published artifact version: https://claude.ai/code/artifact/fb299dde-fb36-4f01-8daf-db26e8114fb2*
