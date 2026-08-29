# Call-Monitor Architecture Review — Prioritized Defect Review

> **Superseded:** The priorities and current-state claims in this document were
> adversarially rechecked against the 2026-08-29 working tree. Use
> [2026-08-29-second-pass-architecture-review.md](2026-08-29-second-pass-architecture-review.md)
> as the review of record. This document remains as the original review artifact.

**Date:** 2026-08-29  
**Scope:** Full-project architecture and quality review  
**Review method:** subsystem-level architecture analysis, code-path verification, and defect triage across ingestion, ranking, web/API, proposal-assist, collaborative editing, lifecycle, data layer, and frontend.  
**Status:** Formal review saved to the project review archive.

---

## 1. Executive summary

This is a healthy and ambitious architecture with clear product intent, a disciplined domain model, and a very strong signal of production-minded engineering. The codebase is organized around explicit operational concerns: ingestion, ranking, proposal assist, collaboration, lifecycle, and web/API presentation. The ranking core in particular is a strong example of architecture as a decision: deterministic gates first, LLM rubric second, and fixed aggregation logic last.

The project is not in a fragile or “demo-only” state. The current verified baseline is strong:

- Verified command: `cd /home/parlar_ai/dev/call-monitor && uv run pytest -n 4 --dist loadfile -q`
- Result: `3964 passed, 1 skipped, 326 warnings in 214.05s`

The architecture is therefore in a solid state overall, but it carries meaningful debt concentrated in a few recurring patterns:

1. duplication across families of similar subsystems
2. too much data and lifecycle logic concentrated in hot tables / large modules
3. large composition roots and hand-maintained orchestration ceremony
4. resource cleanup warnings that are not failing tests today but are likely to become production reliability defects over time

The dominant debt is not “bad boundaries everywhere”; it is “DRY drift plus operational cleanup debt,” which is easier to fix once recognized and prioritized.

---

## 2. Architecture review verdict

### Overall assessment

**Grade: B+ / strong but not yet fully hardened for long-term production operations**

### Strengths

- Clear product domain and consistent architecture boundaries
- Strong configuration validation and environment guardrails
- Single-instance scheduler enforcement and operational lifecycle discipline
- Strong ranking/rubric separation and model-agnostic design
- Mature test architecture with real regression coverage
- Thoughtful start-up and shutdown invariants

### Core risks

- Resource warnings indicate likely unclosed DB/socket/event-loop cleanup paths
- Four-fold duplication of similar orchestration logic across job family implementations
- ToolContext-style god objects and large orchestration closures increase maintenance risk
- Frontend migration state adds integration drift risk and hidden contract mismatch risk
- Operational complexity is high due to many moving parts (DB, scheduler, LLMs, source APIs, git-based workspaces)

---

## 3. Verified project health

### Fresh evidence

The project was validated by running the full relevant suite in the repo root.

**Command run:**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run pytest -n 4 --dist loadfile -q
```

**Result:**

- `3964 passed`
- `1 skipped`
- `326 warnings`
- runtime: `214.05s`

### Interpretation

This means the codebase is highly functional at the repo level. The green suite strongly validates the system’s current behavior. However, the warnings are not harmless noise: they include `ResourceWarning` output around unclosed DB connections, sockets, and event loops. That should be treated as a real reliability issue even though it did not fail the test suite.

---

## 4. Priority order of findings

### P0 — Must address first

1. Fix unclosed DB/socket/event-loop resource warnings
   - This is the most urgent non-failing but dangerous issue.
   - It threatens long-lived runtime stability and can surface as FD exhaustion or subtle process degradation.

2. Finish the ports/F3 conversion and retire the duplicated write systems
   - The architecture review identifies the tool/ports layer as the risk concentration.
   - `ToolContext` is a confirmed god object and the F3d conversion is the planned mitigation.

3. Extract the shared evict-or-not orchestration ceremony
   - The same defect class exists in four collab orchestration paths and already produced a real bug pattern.

4. Parameterize the job-family core and eliminate the four-fold duplicated runner logic
   - Job queue + pass-runner drift has already been demonstrated.

### P1 — High-priority architecture debt

5. Consolidate large orchestration functions that mix too many concerns
   - `build_scheduler`, `drain_queue`, `submit_user_turn`, and others fit this category.

6. Reduce the number of “comment-enforced invariants” that are not machine-enforced
   - Lock order and route-through-kernel invariants are good examples.

7. Shrink large monoliths and hot tables
   - `db/models.py` and the large workspace/job tables remain risk points.

8. Resolve the frontend migration boundaries and contract drift
   - The hybrid React + HTMX architecture is manageable but requires stronger discipline and clearer ownership.

### P2 — Medium-priority design cleanup

9. Remove duplicated logic and dead-letter patterns across modules
10. Replace stringly/fragile external contracts with typed, machine-readable data flows
11. Improve naming and contract clarity of protocol-like interfaces
12. Move private helper access behind stronger boundaries

### P3 — Lower-priority cleanup items

13. Consolidate duplicated comment-based invariants into real guards
14. Reduce copy-paste ceremony in small helper modules
15. Downstream cleanup around naming consistency and duplication of types/config

---

## 5. Formal architecture review: findings by subsystem

## 5.1 Ingestion & sources

**Overall assessment:** Healthy but with a few medium-priority abstraction leaks.

### Strengths

- Registry + Protocol + DTO structure is a good boundary.
- Source adapters are additive, not invasive.
- Source fetch logic is testable and mostly isolated.
- Good separation between ingestion pipeline and source-specific logic.

### Defects

- `sources/types.py` is a grab-bag containing retry, SSRF, and transport logic instead of staying a true type module.
- IP pinning reaches into `httpx/httpcore` private internals on the production fetch path; this is fragile and not robust to library changes.
- Stream-fetch skeleton is duplicated between extraction helpers.
- `SourceAdapter` promises diverge from actual orchestrator contract enforcement.
- Adding a source requires coordinated edits across registry, adapter, config, and migration paths; this is not as additive as advertised.
- EU portal normalization performs blocking network I/O inside normalization logic.
- Transport-security policy is inconsistent across some adapters.
- The same local-import workaround appears multiple times in ingestion alert logic.
- CLI code trusts its own data structure too little and uses defensive `getattr` patterns that suggest weak internal contracts.

**Priority:** P2

---

## 5.2 Ranking, embeddings & LLM

**Overall assessment:** Very strong domain architecture. This is one of the project’s best-designed subsystems.

### Strengths

- Vendor dependence is confined to narrow adapter points.
- Local/remote models are intentionally abstracted.
- Score aggregation is deterministic and auditable.
- Prompt-in/JSON-out semantics are sound and model-agnostic.
- Escalation and failure behavior are controlled rather than uncontrolled.

### Defects

- Chat-client contract exists only as comments; no shared Protocol is enforced.
- `LLMError` lives inside the Ollama adapter instead of in the shared abstraction boundary.
- Dead-letter suppression logic is duplicated across raw-SQL sites.
- `drain_queue` mixes too many responsibilities.
- Ranking imports private helper code from proposal-assist internals.
- `AnthropicClient` does not fully satisfy the local-path signature reality of the drop-in abstraction story.
- `RankResult` construction is repeated at multiple call sites.
- Validation logic imports private helpers from the main ranking flow.
- Report paths/thresholds are resolved from environment state in duplicated locations.

**Priority:** P2

---

## 5.3 Web / API layer

**Overall assessment:** Good boundary and good core structure, but with some inconsistent API assumptions.

### Strengths

- Centralized error envelope and clean exception mapping strategy
- DTO-only API responses
- Consolidation helpers like `workspace_write` and manifest-based route control
- Strong app-state and API-layer maintenance patterns

### Defects

- The “workspaces.py is a god module” claim was refuted; the module is mostly a thin router delegating to services.
- The “exception-to-HTTP mapping is shotgun surgery” claim was refuted; app-level handlers exist and some divergences are deliberate.
- Some proposal-assist routers bypass typed-response conventions and reintroduce untyped dict responses.
- The machine-readable `code` in the error envelope is unreachable from the dominant raising pattern.
- Workspace status allowlists and title caps are duplicated across multiple sources.
- Existence disclosure policies are inconsistent between shared guards and collab guards.
- Cross-module private helper imports leak internal details through the web layer.
- `app.state` is used in a service-locator way from many handlers, creating inconsistent session semantics.
- Lifespan reads controller internals in a way that increases coupling.
- Middleware documentation contradicts implementation choices.
- Several near-clone route triggering patterns exist with mirrored gate ordering.

**Priority:** P1/P2

---

## 5.4 Proposal-assist core

**Overall assessment:** This subsystem contains the highest conceptual complexity and some of the highest risk.

### Strengths

- The system has explicit invariants for turns, finalization, retirement, and preserve-on-delete logic.
- Drafting agent adapters are centralized and isolated.
- The retirement coordinator pattern is strong and useful.
- There is a real central owner for admission and cancellation behaviors.

### Confirmed high-severity defects

1. `ToolContext` is a 28-field god object serving multiple roles.
2. `_collect_tools` is a large closure factory that makes adding a tool a multi-file edit.
3. Human accept paths import private internals and fake ToolContext composition.
4. DraftingAgent protocol is partially specified and partly guessed via runtime behavior.
5. Citation submission logic duplicates verification pipeline code.
6. `submit_user_turn` mixes many responsibilities: mutex policy, IDOR validation, image probing, cap accounting, persistence.

**Priority:** P0/P1

---

## 5.5 Collaborative editing

**Overall assessment:** Good core design, but the orchestration at the edges is where the risk sits.

### Strengths

- The room/manager architecture is dependency-inverted and strong.
- Concurrency invariants are documented and defended.
- The collab write kernel is a useful choke point.

### Confirmed high-severity defect

- Evict-or-not decisions are encoded in four copied ceremonies with hand-maintained exception tuples.
- This is exactly the kind of defect class that has already produced a real production bug.

### Other defects

- Write kernel imports private helpers from peer packages.
- Section-edit rules are duplicated across kernel paths.
- The “all markdown mutations route through the kernel” rule is convention-based, not structural.
- `checkpoint_artifact` accumulates caller-mode booleans and subsystem side effects.
- `apply_citation_to_room` is overloaded via many mode flags.
- ProposedChange resolution is copy-pasted in multiple areas.
- Room/RoomManager have some encapsulation awkwardness.

**Priority:** P0

---

## 5.6 Ports architecture & fence work (F3)

**Overall assessment:** One of the most interesting and theoretically strong parts of the repo, but it is not yet fully operationalized in production.

### Strengths

- The ports layer is structurally disciplined.
- The fence rules are explicit and strong.
- Gate enforcement is built to fail closed.
- The design clearly prefers correctness over permissiveness.

### Confirmed high-severity defect

- `ToolContext` is the central structural risk and part of the same architecture debt.

### Other defects

- The ports layer has zero production consumers yet.
- Three semantic effects are implemented twice and kept in sync by comments and duplicated strings.
- Live accounting logic contradicts the rules codified in the port layer.
- Port error taxonomy does not fully capture the `CommitMutexTimeout` pattern.
- The queries port covers only a limited part of the real read surface.
- The locator-name rule depends on token-based granularity and is partly bypassed by skipped composition paths.

**Priority:** P0/P1

---

## 5.7 Background jobs & lifecycle

**Overall assessment:** Good locking substrate and lifecycle discipline, but a serious duplication problem remains.

### Strengths

- Single-instance lock is handled with explicit, robust logic.
- Shutdown / retirement logic is well thought through.
- Hook-based retirement fan-out is strong.
- Timeouts and lifecycle invariants are documented with reasoning.

### Confirmed high-severity defect

- Four-fold clone of the job queue + pass runner system has already drifted.
- Drift includes delayed spend accounting and pre-launch delete recheck divergence.

### Other defects

- `build_scheduler` is a large composition function.
- Scheduler configuration is supplied through both unpacked kwargs and `Settings` values.
- Delete-time job cancellation is effectively hard-coded per family.
- `assemble_for_user` mixes too many responsibilities.
- The lock-order invariants live in comments more than mechanical gates.
- Reaper contract shapes are not consistent across all implementations.
- `run_digest` has a large inlined state machine.

**Priority:** P0/P1

---

## 5.8 Data layer

**Overall assessment:** Strong transactional instincts, but the model layer is too large and too hot.

### Strengths

- Good use of DB-level invariants and unique indexes.
- Clear transaction and locking policy intent.
- Good use of migration discipline and explicit model constraints.

### Defects

- `db/models.py` is a very large monolith spanning multiple bounded contexts.
- Job-table column blocks are copy-pasted across several tables and drifted.
- The workspace table is growing into a hot row with too many features.
- There are dual session acquisition idioms across the stack.
- Profile validation caps are duplicated in conflicting ways.
- Terminal-status vocabulary and enum strategy are inconsistent across job tables.
- `expire_on_commit=False` session invariants are enforced via comments rather than structure.
- Domain packages raise HTTP exceptions, leaking web concerns into model logic.
- Some service functions are under-typed.
- `Call.structured` duck-types interfaces across modules.

**Priority:** P1/P2

---

## 5.9 React SPA

**Overall assessment:** Good frontend architecture, but with drift and missing tooling discipline.

### Strengths

- Feature-sliced architecture is understandable.
- Generated OpenAPI types are a strong boundary.
- Query-store and route-state separation is sensible.
- Strict-mode-safe lifecycle handling is a positive sign.

### Defects

- Shell chrome hard-wires cockpit knowledge instead of using route-handling extension points.
- Stringly-typed query keys are duplicated.
- Some 403 behavior is detected by message regex instead of a machine-readable error code.
- `editorStore` is a global singleton living in a feature folder.
- API layer imports feature types instead of using a stable shared boundary.
- `CockpitSidebar` mixes too many responsibilities.
- `LibraryPanel` mixes multiple distinct domains into one component.
- Local Turn types are duplicated instead of derived from generated schema.
- Score-band threshold logic is duplicated in different frontend routes.
- No linter is included in the build gate.
- `citations` boolean on `ArtifactSectionEditor` is effectively a “draft mode” flag disguised as a feature property.

**Priority:** P2

---

## 6. Defect summary table

| Priority | Severity | Defect | Area |
|---|---|---|---|
| P0 | High | Resource warnings / unclosed resources | Runtime stability |
| P0 | High | F3 ports conversion incomplete | Architecture |
| P0 | High | Evict ceremony duplicated in four places | Collaborative editing |
| P0 | High | Job-family queue runner duplicated and drifted | Lifecycle |
| P1 | High | ToolContext god object | Proposal-assist |
| P1 | Medium | Large orchestration functions mix concerns | Scheduler + workflow |
| P1 | Medium | Comment-enforced invariants not turned into gates | Lifecycle / lock discipline |
| P1 | Medium | `db/models.py` growth and hot-row bloat | Data layer |
| P1 | Medium | Hybrid frontend migration and implicit contract drift | SPA / web |
| P2 | Medium | Duplicated dead-letter and helper logic | Ingestion + ranking |
| P2 | Medium | Inconsistent protocol boundaries and naming | API + service contracts |
| P2 | Low | Localized duplication and weaker code hygiene | Many subsystems |

---

## 7. Recommended action plan

### Phase 1 — Reliability and correctness hardening

1. Close the resource warnings and unclosed connection/socket lifecycle issues.
2. Complete the ports conversion and remove the duplicate write systems.
3. Extract the shared eviction orchestration helper.
4. Refactor the job-family logic into a common parameterized core.

### Phase 2 — Architecture simplification

5. Reduce `ToolContext` and large closure factories.
6. Break up `db/models.py` and reduce hot-row scope.
7. Consolidate duplicated API and service contracts.
8. Create a real frontend contract + lint gate.

### Phase 3 — Long-term maintenance

9. Convert more comment-enforced invariants into real machine-enforced gates.
10. Continue reducing DRY drift across job families and orchestration paths.
11. Revisit the hybrid frontend migration plan and set a clear completion target.

---

## 8. Final conclusion

This project is operationally real, architecturally thoughtful, and substantively stronger than a typical one-off internal prototype. It is not suffering from a broken design; it is suffering from the standard risk pattern of a system that has grown across multiple subsystems faster than the cleanup and consolidation work has caught up.

The most important conclusion is not that the architecture is weak. It is that the architecture is good enough to support the next level of discipline. The right move is not a rewrite; it is targeted reduction of duplication, enforcement of invariants via code, completion of the ports-based conversion, and cleanup of runtime resource handling.

If those steps are addressed in priority order, the project’s strong foundations will convert into a durable, lower-risk production platform.

---

## 9. Review archive metadata

- Review type: architecture review
- Repository: call-monitor
- Saved under: `docs/superpowers/reviews/`
- Verified baseline: `3964 passed, 1 skipped, 326 warnings`
- Primary risk concentration: ports/F3, ToolContext, orchestrator duplication, resource cleanup, lifecycle drift
