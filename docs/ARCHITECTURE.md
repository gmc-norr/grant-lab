# Architecture map

> **GENERATED — do not edit.** Produced by `scripts/gen_architecture.py` from the
> source, via `ast` (the app is never imported). A pytest test regenerates this
> file and fails on any diff, so it cannot go stale.
> Regenerate with `uv run python scripts/gen_architecture.py`.

This map exists because two wrong recommendations were made from unmeasured
inference about facts that were not written down anywhere lookup-able.

## Active-uniqueness: every partial UNIQUE index and its exact predicate

Read this before claiming two tables share, or do not share, a rule.

| table | index | columns | predicate |
|---|---|---|---|
| `attachment_jobs` | `uq_attachment_jobs_active` | attachment_id, kind | `status IN ('queued', 'running')` |
| `claim_analysis_jobs` | `uq_claim_analysis_jobs_ws_sha_queued` | workspace_id, section_sha | `status = 'queued'` |
| `coherence_jobs` | `uq_coherence_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `evaluator_jobs` | `uq_evaluator_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `profiles` | `uq_profiles_active_owner_name` | created_by, name | `is_active = true` |
| `ranking_jobs` | `uq_ranking_jobs_active_pair` | call_id, profile_id | `status IN ('pending', 'running')` |
| `references` | `uq_references_workspace_citekey` | workspace_id, citekey | `citekey IS NOT NULL` |
| `review_runs` | `uq_review_runs_active` | workspace_id | `status IN ('queued','running')` |
| `turn_jobs` | `uq_turn_jobs_running` | workspace_id | `status = 'running'` |
| `uncited_claims_jobs` | `uq_uncited_claims_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `users` | `uq_users_oidc_identity` | oidc_iss, oidc_sub | `oidc_sub IS NOT NULL` |
| `workspace_events` | `uq_workspace_events_rubric_initialized` | workspace_id | `kind = 'rubric_initialized'` |

**12 partial unique indexes across 12 tables, 9 distinct predicates.**

- `status IN ('queued','running')` — 4 table(s): coherence_jobs, evaluator_jobs, review_runs, uncited_claims_jobs
- `oidc_sub IS NOT NULL` — 1 table(s): users
- `status = 'running'` — 1 table(s): turn_jobs
- `status IN ('queued', 'running')` — 1 table(s): attachment_jobs
- `status = 'queued'` — 1 table(s): claim_analysis_jobs
- `is_active = true` — 1 table(s): profiles
- `status IN ('pending', 'running')` — 1 table(s): ranking_jobs
- `citekey IS NOT NULL` — 1 table(s): references
- `kind = 'rubric_initialized'` — 1 table(s): workspace_events

## Tables

48 mapped tables.

| table | class | cols | FKs | CHECKs | indexes |
|---|---|---|---|---|---|
| `artifact_versions` | `ArtifactVersion` | 7 | 2 | 1 | 2 |
| `attachment_jobs` | `AttachmentJob` | 9 | 2 | 2 | 2 |
| `attachments` | `Attachment` | 12 | 2 | 1 | 1 |
| `board_items` | `BoardItem` | 8 | 3 | 0 | 1 |
| `board_suppressions` | `BoardSuppression` | 4 | 3 | 0 | 0 |
| `boards` | `Board` | 19 | 3 | 6 | 1 |
| `branches` | `Branch` | 5 | 1 | 0 | 0 |
| `call_decisions` | `CallDecision` | 8 | 4 | 1 | 3 |
| `call_ranking_criteria` | `CallRankingCriterion` | 7 | 2 | 0 | 0 |
| `call_rankings` | `CallRanking` | 22 | 2 | 1 | 1 |
| `call_rubrics` | `CallRubric` | 7 | 1 | 0 | 0 |
| `call_section_extracts` | `CallSectionExtract` | 7 | 1 | 1 | 0 |
| `calls` | `Call` | 20 | 1 | 1 | 7 |
| `chat_summaries` | `ChatSummary` | 7 | 2 | 0 | 1 |
| `chat_turns` | `ChatTurn` | 9 | 1 | 1 | 2 |
| `citation_audit_verdicts` | `CitationAuditVerdict` | 7 | 1 | 1 | 2 |
| `citation_llm_usage` | `CitationLlmUsage` | 8 | 1 | 2 | 2 |
| `claim_analysis_jobs` | `ClaimAnalysisJob` | 12 | 1 | 1 | 2 |
| `claim_classifications` | `ClaimClassification` | 12 | 2 | 1 | 0 |
| `coherence_jobs` | `CoherenceJob` | 14 | 2 | 1 | 2 |
| `collab_artifact_state` | `CollabArtifactState` | 6 | 1 | 1 | 0 |
| `digest_runs` | `DigestRun` | 5 | 1 | 0 | 2 |
| `evaluator_jobs` | `EvaluatorJob` | 17 | 2 | 1 | 2 |
| `golden_set_triples` | `GoldenSetTriple` | 8 | 3 | 1 | 0 |
| `llm_spend` | `LlmSpend` | 10 | 1 | 3 | 2 |
| `phase_approvals` | `PhaseApproval` | 6 | 2 | 1 | 1 |
| `profile_bootstrap_jobs` | `ProfileBootstrapJob` | 11 | 1 | 1 | 0 |
| `profile_viewers` | `ProfileViewer` | 3 | 2 | 0 | 0 |
| `profiles` | `Profile` | 16 | 2 | 4 | 2 |
| `proposed_changes` | `ProposedChange` | 19 | 4 | 4 | 2 |
| `ranking_jobs` | `RankingJob` | 12 | 2 | 1 | 4 |
| `ranking_remote_attempts` | `RankingRemoteAttempt` | 6 | 2 | 1 | 1 |
| `reference_chunks` | `ReferenceChunk` | 8 | 2 | 0 | 2 |
| `references` | `Reference` | 28 | 2 | 6 | 2 |
| `review_run_items` | `ReviewRunItem` | 13 | 1 | 2 | 1 |
| `review_runs` | `ReviewRun` | 14 | 2 | 1 | 2 |
| `sources` | `Source` | 9 | 0 | 0 | 0 |
| `spec_lock_events` | `SpecLockEvent` | 6 | 2 | 1 | 1 |
| `subscriptions` | `Subscription` | 5 | 2 | 0 | 0 |
| `turn_jobs` | `TurnJob` | 11 | 3 | 1 | 2 |
| `uncited_claims_jobs` | `UncitedClaimsJob` | 15 | 2 | 1 | 2 |
| `user_claim_annotations` | `UserClaimAnnotation` | 19 | 3 | 2 | 1 |
| `users` | `User` | 16 | 2 | 3 | 1 |
| `workspace_events` | `WorkspaceEvent` | 6 | 2 | 0 | 2 |
| `workspace_setup_jobs` | `WorkspaceSetupJob` | 10 | 1 | 2 | 0 |
| `workspace_viewers` | `WorkspaceViewer` | 3 | 2 | 0 | 0 |
| `workspaces` | `Workspace` | 29 | 5 | 3 | 1 |
| `yjs_updates` | `YjsUpdate` | 6 | 2 | 1 | 1 |

### Status vocabularies

Every CHECK constraining a `status`-like column. Divergence here is a real defect.

| table | constraint | vocabulary |
|---|---|---|
| `attachment_jobs` | `ck_attachment_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `citation_llm_usage` | `ck_citation_llm_usage_status` | `status IN ('ok','failed','timeout')` |
| `claim_analysis_jobs` | `ck_claim_analysis_jobs_status` | `status IN ('queued','running','done','failed','superseded')` |
| `coherence_jobs` | `ck_coherence_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `evaluator_jobs` | `ck_evaluator_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `llm_spend` | `ck_llm_spend_status` | `status IN ('ok','failed','timeout')` |
| `profile_bootstrap_jobs` | `ck_profile_bootstrap_jobs_status` | `status IN ('queued','running','done','failed')` |
| `proposed_changes` | `ck_proposed_changes_status` | `status IN ('pending','accepted','rejected','superseded','stale')` |
| `references` | `ck_references_text_status` | `text_status IN ('pending','running','done','failed','skipped')` |
| `references` | `ck_references_outline_status` | `outline_status IN ('pending','running','done','failed','skipped')` |
| `references` | `ck_references_metadata_status` | `metadata_status IN ('pending','extracted','verified_external','approved','failed')` |
| `review_run_items` | `ck_review_run_items_status` | `status IN ('queued','running','done','failed','skipped','cancelled')` |
| `review_runs` | `ck_review_runs_status` | `status IN ('queued','running','done','failed','cancelled','partial')` |
| `turn_jobs` | `ck_turn_jobs_status` | `status IN ('queued','running','completed','failed','cancelled')` |
| `uncited_claims_jobs` | `ck_uncited_claims_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `user_claim_annotations` | `ck_user_claim_annotations_status` | `status IN ('open','inserted','dismissed','stale')` |
| `users` | `ck_users_status_valid` | `status IN ('pending', 'active', 'disabled')` |
| `workspace_setup_jobs` | `ck_workspace_setup_jobs_status` | `status IN ('queued','running','waiting_for_rubric_review','done','failed','cancelled')` |
| `workspaces` | `ck_workspaces_status` | `status IN ('drafting','submitted','awarded','rejected','abandoned')` |

### Ownership: what each table is keyed on

| table | foreign keys (column → target, ON DELETE) |
|---|---|
| `artifact_versions` | `workspace_id` → `workspaces.id` (CASCADE)<br>`triggering_turn_id` → `chat_turns.id` (SET NULL) |
| `attachment_jobs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`attachment_id` → `attachments.id` (CASCADE) |
| `attachments` | `workspace_id` → `workspaces.id` (CASCADE)<br>`uploaded_by` → `users.id` (RESTRICT) |
| `board_items` | `board_id` → `boards.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`approved_by` → `users.id` (RESTRICT) |
| `board_suppressions` | `board_id` → `boards.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`suppressed_by` → `users.id` (RESTRICT) |
| `boards` | `profile_id` → `profiles.id` (RESTRICT)<br>`created_by` → `users.id` (RESTRICT)<br>`published_by` → `users.id` (RESTRICT) |
| `branches` | `workspace_id` → `workspaces.id` (CASCADE) |
| `call_decisions` | `user_id` → `users.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`decided_by` → `users.id` (SET NULL) |
| `call_ranking_criteria` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `call_rankings` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `call_rubrics` | `call_id` → `calls.id` (CASCADE) |
| `call_section_extracts` | `call_id` → `calls.id` (CASCADE) |
| `calls` | `source_id` → `sources.id` (RESTRICT) |
| `chat_summaries` | `workspace_id` → `workspaces.id` (CASCADE)<br>`summarized_through` → `chat_turns.id` (CASCADE) |
| `chat_turns` | `workspace_id` → `workspaces.id` (CASCADE) |
| `citation_audit_verdicts` | `workspace_id` → `workspaces.id` (CASCADE) |
| `citation_llm_usage` | `workspace_id` → `workspaces.id` (CASCADE) |
| `claim_analysis_jobs` | `workspace_id` → `workspaces.id` (CASCADE) |
| `claim_classifications` | `workspace_id` → `workspaces.id` (CASCADE)<br>`reference_id` → `references.id` (SET NULL) |
| `coherence_jobs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `collab_artifact_state` | `workspace_id` → `workspaces.id` (CASCADE) |
| `digest_runs` | `user_id` → `users.id` (CASCADE) |
| `evaluator_jobs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `golden_set_triples` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE)<br>`created_by` → `users.id` (SET NULL) |
| `llm_spend` | `workspace_id` → `workspaces.id` (CASCADE) |
| `phase_approvals` | `workspace_id` → `workspaces.id` (CASCADE)<br>`approving_user_id` → `users.id` (RESTRICT) |
| `profile_bootstrap_jobs` | `profile_id` → `profiles.id` (CASCADE) |
| `profile_viewers` | `profile_id` → `profiles.id` (CASCADE)<br>`user_id` → `users.id` (CASCADE) |
| `profiles` | `parent_profile_id` → `profiles.id` (SET NULL)<br>`created_by` → `users.id` (RESTRICT) |
| `proposed_changes` | `workspace_id` → `workspaces.id` (CASCADE)<br>`triggering_turn_id` → `chat_turns.id` (SET NULL)<br>`created_by` → `users.id` (SET NULL)<br>`resolved_by` → `users.id` (SET NULL) |
| `ranking_jobs` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `ranking_remote_attempts` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `reference_chunks` | `reference_id` → `references.id` (CASCADE)<br>`workspace_id` → `workspaces.id` (CASCADE) |
| `references` | `workspace_id` → `workspaces.id` (CASCADE)<br>`uploaded_by` → `users.id` (RESTRICT) |
| `review_run_items` | `review_run_id` → `review_runs.id` (CASCADE) |
| `review_runs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`waived_from_review_run_id` → `review_runs.id` (SET NULL) |
| `spec_lock_events` | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |
| `subscriptions` | `user_id` → `users.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `turn_jobs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`user_turn_id` → `chat_turns.id` (SET NULL)<br>`submitted_by_user_id` → `users.id` (RESTRICT) |
| `uncited_claims_jobs` | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `user_claim_annotations` | `workspace_id` → `workspaces.id` (CASCADE)<br>`reference_id` → `references.id` (SET NULL)<br>`resolved_by_user_id` → `users.id` (SET NULL) |
| `users` | `approved_by_user_id` → `users.id` (SET NULL)<br>`default_profile_id` → `profiles.id` (SET NULL) |
| `workspace_events` | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |
| `workspace_setup_jobs` | `workspace_id` → `workspaces.id` (CASCADE) |
| `workspace_viewers` | `workspace_id` → `workspaces.id` (CASCADE)<br>`user_id` → `users.id` (CASCADE) |
| `workspaces` | `owner_id` → `users.id` (RESTRICT)<br>`call_id` → `calls.id` (SET NULL)<br>`profile_id` → `profiles.id` (SET NULL)<br>`root_branch_id` → `branches.id` (RESTRICT)<br>`spec_locked_by` → `users.id` (SET NULL) |
| `yjs_updates` | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |

## The epoch fence

Public primitives in `src/grant_lab/proposal_assist/fencing.py`.
**Read the docstring for the exact SCOPE of each rule before relying on it** —
a rule that looks absolute usually is not.

| primitive | line | first line of its contract |
|---|---|---|
| `live_at_generation` | fencing.py:73 | Primitive A. "Is this workspace LIVE and STILL at *generation*?", as a |
| `acquire_commit_mutex_at_generation` | fencing.py:116 | Primitive B. Acquire the commit mutex, then refuse a workspace that is |
| `acquire_shared_lease` | fencing.py:183 | Primitive C. Take the workspace's commit-mutex key in SHARED mode for the |

### Armed-gate allowlists (`tests/fencing/inventory.py`)

Each may only SHRINK. A growing entry is a reviewed diff.

| allowlist | entries |
|---|---|
| `CATEGORIES_BY_KIND` | 4 |
| `NON_SEMANTIC_PINS` | 3 |
| `OPTIONAL_KEYS` | 2 |
| `RETAINED_PINS` | 1 |
| `SLICE_STATUS` | 16 |
| `TOOLS_NOT_YET_CONVERTED` | 5 |
| `_SLICE_FOR_KIND` | 2 |

## Caps, ceilings and limits

23 settings in `config.py`. A cap that exists in only one of a pair of
sibling paths is the shape of a real defect.

| setting | default |
|---|---|
| `citation_deep_check_max_per_request` | `Field(default=25, alias='CITATION_DEEP_CHECK_MAX_PER_REQUEST', description='Maximum number of FRESH (uncached) semantic LLM calls made per POST /citation-audit/deep request. Already-cached verdicts are returned without counting against this ceiling. Rows beyond the cap are deferred and reported in the response so the client can re-poll to continue. Default 25 keeps a single request well within a synchronous HTTP timeout budget.')` |
| `citation_llm_daily_token_cap_input` | `Field(default=200000, alias='CITATION_LLM_DAILY_TOKEN_CAP_INPUT')` |
| `citation_llm_daily_token_cap_output` | `Field(default=50000, alias='CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT')` |
| `citation_llm_max_output_tokens` | `Field(default=1024, alias='CITATION_LLM_MAX_OUTPUT_TOKENS')` |
| `coherence_daily_token_cap` | `Field(default=200000, alias='COHERENCE_DAILY_TOKEN_CAP', description="Per-workspace daily ceiling on combined input+output tokens spent by coherence-keeper passes (Europe/Stockholm day). A pass is blocked pre-run once today's coherence_jobs token sum reaches this cap. The other coherence settings land in SP2b-2c Task 7.")` |
| `daily_token_cap_input` | `Field(default=500000, alias='DAILY_TOKEN_CAP_INPUT', description='Per-workspace default daily input-token cap (Europe/Stockholm day). Overridable per-workspace via workspaces.daily_token_cap_in_override. Enforced pre-enqueue by submit_user_turn(); B8 will surface the UX.')` |
| `daily_token_cap_output` | `Field(default=100000, alias='DAILY_TOKEN_CAP_OUTPUT', description='Per-workspace default daily output-token cap.')` |
| `drafting_max_live_subprocesses` | `Field(default=8, alias='DRAFTING_MAX_LIVE_SUBPROCESSES', gt=0, description="Hard ceiling on concurrent live drafting subprocesses (Defect 1 remediation). Each live subprocess pins ONE pooled DB connection for its lifetime (the session-level advisory lock keeps it checked out, up to WORKSPACE_IDLE_TIMEOUT_SECONDS). Keep this well below the engine pool (pool_size=10 + max_overflow=20 = 30) so drafting can never starve web/scheduler connections. In production those lock sessions come from a DEDICATED engine (DRAFTING_DB_POOL_SIZE) sized to this ceiling, so they don't draw on the request pool at all. At the cap, a new launch evicts the least-recently-used IDLE handle; if all are mid-turn, it returns a retriable 429.")` |
| `evaluator_daily_token_cap` | `Field(default=200000, alias='EVALUATOR_DAILY_TOKEN_CAP', description='Per-workspace daily ceiling on combined input+output tokens spent by evaluator passes (Europe/Stockholm day). A pass is blocked pre-run once reached.')` |
| `global_citation_llm_daily_token_cap_input` | `Field(default=2000000, alias='GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_INPUT')` |
| `global_citation_llm_daily_token_cap_output` | `Field(default=500000, alias='GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT')` |
| `global_daily_token_cap_input` | `Field(default=5000000, alias='GLOBAL_DAILY_TOKEN_CAP_INPUT', description="Org-wide ceiling on total input tokens (sum across all workspaces) per Europe/Stockholm day. Defaults to 10x the per-workspace cap so a single runaway workspace can't bill the whole budget. Enforced before per-workspace cap; a small overshoot from concurrent submits is acceptable (guardrail, not exact ledger).")` |
| `global_daily_token_cap_output` | `Field(default=1000000, alias='GLOBAL_DAILY_TOKEN_CAP_OUTPUT', description='Org-wide ceiling on total output tokens (sum across all workspaces) per Europe/Stockholm day. Defaults to 10x the per-workspace cap.')` |
| `max_request_body_bytes` | `Field(default=64 * 1024 * 1024, ge=0, alias='MAX_REQUEST_BODY_BYTES')` |
| `problem_section_char_budget` | `Field(default=6000, alias='PROBLEM_SECTION_CHAR_BUDGET', description="Per-section character budget for the Problems panel's over-length heuristic (draft sections only). A loose guardrail, not a hard funder limit; a value <= 0 disables the over-length check.")` |
| `ranking_remote_daily_cap` | `Field(default=300, alias='RANKING_REMOTE_DAILY_CAP', description='Max remote escalation attempts per Europe/Stockholm day (counted from ranking_remote_attempts). Guardrail for backfill spikes. Raised 100 -> 150 on 2026-09-09 when REMOTE_RANK_MODEL moved from claude-sonnet-4-6 ($3/$15 per MTok) to claude-sonnet-5 ($2/$10) -- the 2/3 price ratio makes 150 escalations cost what 100 did, so this is a cost-neutral raise, not a budget increase. It was chosen because the cap was being HIT: on 2026-09-09, 33 pairs scoring >= RANKING_ESCALATION_MIN_SCORE were denied escalation after the 100th attempt and shipped with a Gemma-only score and no analysis. Raised 150 -> 300 on 2026-10-05: since ranking freshness (2026-09-24) a day uses 0-27 attempts (09-25..10-04), so the cap binds only on a full re-rank, and 150 split the 09-24 re-rank (223 attempts that day) over two days.')` |
| `ranking_tick_budget_seconds` | `Field(default=240.0, alias='RANKING_TICK_BUDGET_SECONDS')` |
| `reference_max_batch_bytes` | `Field(default=200 * 1024 * 1024, ge=1, description='Client-enforced max total bytes per drag-drop batch.')` |
| `reference_max_batch_files` | `Field(default=25, ge=1, description='Client-enforced max PDFs per drag-drop batch.')` |
| `review_dollar_cap_default` | `Field(default=Decimal('5.000'), alias='REVIEW_DOLLAR_CAP_DEFAULT', description='Global per-workspace daily $ cap on mock-panel runs; overridable per workspace via Workspace.review_dollar_cap_override.')` |
| `session_max_age_seconds` | `Field(default=30 * 24 * 3600, alias='SESSION_MAX_AGE_SECONDS', description='Session cookie lifetime. 30 days by default; an organisational policy choice.')` |
| `turn_max_seconds` | `Field(default=600, alias='TURN_MAX_SECONDS', description="Hard wall-clock bound on a single turn's processing inside the drain tick. A turn that exceeds this is cancelled in-loop and finalized as failed, releasing the workspace's running-row cleanly. MUST stay comfortably below the reaper TTL (reaper.DEFAULT_REAP_TTL_SECONDS) so a slow-but-live turn is bounded by this clean path, not reaped while still streaming.")` |
| `uncited_claims_daily_token_cap` | `Field(default=200000, alias='UNCITED_CLAIMS_DAILY_TOKEN_CAP', description='Per-workspace daily ceiling on combined input+output tokens spent by uncited-claims passes (Europe/Stockholm day). A pass is blocked pre-run once reached.')` |

