# Architecture map

> **GENERATED — do not edit.** Produced by `scripts/gen_architecture.py` from the
> source, via `ast` (the app is never imported). A pytest test regenerates this
> file and fails on any diff, so it cannot go stale.
> Regenerate with `uv run python scripts/gen_architecture.py`.

This map exists because two wrong recommendations were made from unmeasured
inference about facts that were not written down anywhere lookup-able.

A table marked **(parked)** belongs to the proposal workspace, which left master
on 2026-11-03. It and its rows are still in the database and its definition is in
`src/grant_lab/db/models/workspace/`, but no code on master reads or writes it.

## Active-uniqueness: every partial UNIQUE index and its exact predicate

Read this before claiming two tables share, or do not share, a rule.

| table | index | columns | predicate |
|---|---|---|---|
| `attachment_jobs` (parked) | `uq_attachment_jobs_active` | attachment_id, kind | `status IN ('queued', 'running')` |
| `claim_analysis_jobs` (parked) | `uq_claim_analysis_jobs_ws_sha_queued` | workspace_id, section_sha | `status = 'queued'` |
| `coherence_jobs` (parked) | `uq_coherence_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `evaluator_jobs` (parked) | `uq_evaluator_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `profiles` | `uq_profiles_active_owner_name` | created_by, name | `is_active = true` |
| `ranking_jobs` | `uq_ranking_jobs_active_pair` | call_id, profile_id | `status IN ('pending', 'running')` |
| `references` (parked) | `uq_references_workspace_citekey` | workspace_id, citekey | `citekey IS NOT NULL` |
| `review_runs` (parked) | `uq_review_runs_active` | workspace_id | `status IN ('queued','running')` |
| `turn_jobs` (parked) | `uq_turn_jobs_running` | workspace_id | `status = 'running'` |
| `uncited_claims_jobs` (parked) | `uq_uncited_claims_jobs_active` | workspace_id | `status IN ('queued','running')` |
| `users` | `uq_users_oidc_identity` | oidc_iss, oidc_sub | `oidc_sub IS NOT NULL` |
| `workspace_events` (parked) | `uq_workspace_events_rubric_initialized` | workspace_id | `kind = 'rubric_initialized'` |

**12 partial unique indexes across 12 tables, 9 distinct predicates.**

- `status IN ('queued','running')` — 4 table(s): coherence_jobs, evaluator_jobs, review_runs, uncited_claims_jobs
- `oidc_sub IS NOT NULL` — 1 table(s): users
- `is_active = true` — 1 table(s): profiles
- `status IN ('pending', 'running')` — 1 table(s): ranking_jobs
- `status = 'running'` — 1 table(s): turn_jobs
- `status IN ('queued', 'running')` — 1 table(s): attachment_jobs
- `status = 'queued'` — 1 table(s): claim_analysis_jobs
- `citekey IS NOT NULL` — 1 table(s): references
- `kind = 'rubric_initialized'` — 1 table(s): workspace_events

## Tables

48 mapped tables, 31 of them parked.

| table | class | cols | FKs | CHECKs | indexes |
|---|---|---|---|---|---|
| `artifact_versions` (parked) | `ArtifactVersion` | 7 | 2 | 1 | 2 |
| `attachment_jobs` (parked) | `AttachmentJob` | 9 | 2 | 2 | 2 |
| `attachments` (parked) | `Attachment` | 12 | 2 | 1 | 1 |
| `board_items` | `BoardItem` | 8 | 3 | 0 | 1 |
| `board_suppressions` | `BoardSuppression` | 4 | 3 | 0 | 0 |
| `boards` | `Board` | 19 | 3 | 7 | 1 |
| `branches` (parked) | `Branch` | 5 | 1 | 0 | 0 |
| `call_decisions` | `CallDecision` | 8 | 4 | 1 | 3 |
| `call_ranking_criteria` | `CallRankingCriterion` | 7 | 2 | 0 | 0 |
| `call_rankings` | `CallRanking` | 22 | 2 | 1 | 1 |
| `call_rubrics` (parked) | `CallRubric` | 7 | 1 | 0 | 0 |
| `call_section_extracts` (parked) | `CallSectionExtract` | 7 | 1 | 1 | 0 |
| `calls` | `Call` | 20 | 1 | 1 | 7 |
| `chat_summaries` (parked) | `ChatSummary` | 7 | 2 | 0 | 1 |
| `chat_turns` (parked) | `ChatTurn` | 9 | 1 | 1 | 2 |
| `citation_audit_verdicts` (parked) | `CitationAuditVerdict` | 7 | 1 | 1 | 2 |
| `citation_llm_usage` (parked) | `CitationLlmUsage` | 8 | 1 | 2 | 2 |
| `claim_analysis_jobs` (parked) | `ClaimAnalysisJob` | 12 | 1 | 1 | 2 |
| `claim_classifications` (parked) | `ClaimClassification` | 12 | 2 | 1 | 0 |
| `coherence_jobs` (parked) | `CoherenceJob` | 14 | 2 | 1 | 2 |
| `collab_artifact_state` (parked) | `CollabArtifactState` | 6 | 1 | 1 | 0 |
| `digest_runs` | `DigestRun` | 5 | 1 | 0 | 2 |
| `evaluator_jobs` (parked) | `EvaluatorJob` | 17 | 2 | 1 | 2 |
| `golden_set_triples` | `GoldenSetTriple` | 8 | 3 | 1 | 0 |
| `llm_spend` (parked) | `LlmSpend` | 10 | 1 | 3 | 2 |
| `phase_approvals` (parked) | `PhaseApproval` | 6 | 2 | 1 | 1 |
| `profile_bootstrap_jobs` | `ProfileBootstrapJob` | 11 | 1 | 1 | 0 |
| `profile_viewers` | `ProfileViewer` | 3 | 2 | 0 | 0 |
| `profiles` | `Profile` | 16 | 2 | 4 | 2 |
| `proposed_changes` (parked) | `ProposedChange` | 19 | 4 | 4 | 2 |
| `ranking_jobs` | `RankingJob` | 12 | 2 | 1 | 4 |
| `ranking_remote_attempts` | `RankingRemoteAttempt` | 9 | 2 | 1 | 1 |
| `reference_chunks` (parked) | `ReferenceChunk` | 8 | 2 | 0 | 2 |
| `references` (parked) | `Reference` | 28 | 2 | 6 | 2 |
| `review_run_items` (parked) | `ReviewRunItem` | 13 | 1 | 2 | 1 |
| `review_runs` (parked) | `ReviewRun` | 14 | 2 | 1 | 2 |
| `sources` | `Source` | 9 | 0 | 0 | 0 |
| `spec_lock_events` (parked) | `SpecLockEvent` | 6 | 2 | 1 | 1 |
| `subscriptions` | `Subscription` | 5 | 2 | 0 | 0 |
| `turn_jobs` (parked) | `TurnJob` | 11 | 3 | 1 | 2 |
| `uncited_claims_jobs` (parked) | `UncitedClaimsJob` | 15 | 2 | 1 | 2 |
| `user_claim_annotations` (parked) | `UserClaimAnnotation` | 19 | 3 | 2 | 1 |
| `users` | `User` | 16 | 2 | 3 | 1 |
| `workspace_events` (parked) | `WorkspaceEvent` | 6 | 2 | 0 | 2 |
| `workspace_setup_jobs` (parked) | `WorkspaceSetupJob` | 10 | 1 | 2 | 0 |
| `workspace_viewers` (parked) | `WorkspaceViewer` | 3 | 2 | 0 | 0 |
| `workspaces` (parked) | `Workspace` | 29 | 5 | 3 | 1 |
| `yjs_updates` (parked) | `YjsUpdate` | 6 | 2 | 1 | 1 |

### Status vocabularies

Every CHECK constraining a `status`- or `state`-like column. Divergence here is a real defect.

| table | constraint | vocabulary |
|---|---|---|
| `attachment_jobs` (parked) | `ck_attachment_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `call_decisions` | `ck_call_decisions_state` | `state IN ('saved','hidden')` |
| `call_rankings` | `ck_call_rankings_escalation_state` | `escalation_state IN ('pending','gave_up')` |
| `citation_llm_usage` (parked) | `ck_citation_llm_usage_status` | `status IN ('ok','failed','timeout')` |
| `claim_analysis_jobs` (parked) | `ck_claim_analysis_jobs_status` | `status IN ('queued','running','done','failed','superseded')` |
| `coherence_jobs` (parked) | `ck_coherence_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `evaluator_jobs` (parked) | `ck_evaluator_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `llm_spend` (parked) | `ck_llm_spend_status` | `status IN ('ok','failed','timeout')` |
| `profile_bootstrap_jobs` | `ck_profile_bootstrap_jobs_status` | `status IN ('queued','running','done','failed')` |
| `proposed_changes` (parked) | `ck_proposed_changes_status` | `status IN ('pending','accepted','rejected','superseded','stale')` |
| `references` (parked) | `ck_references_text_status` | `text_status IN ('pending','running','done','failed','skipped')` |
| `references` (parked) | `ck_references_outline_status` | `outline_status IN ('pending','running','done','failed','skipped')` |
| `references` (parked) | `ck_references_metadata_status` | `metadata_status IN ('pending','extracted','verified_external','approved','failed')` |
| `review_run_items` (parked) | `ck_review_run_items_status` | `status IN ('queued','running','done','failed','skipped','cancelled')` |
| `review_runs` (parked) | `ck_review_runs_status` | `status IN ('queued','running','done','failed','cancelled','partial')` |
| `turn_jobs` (parked) | `ck_turn_jobs_status` | `status IN ('queued','running','completed','failed','cancelled')` |
| `uncited_claims_jobs` (parked) | `ck_uncited_claims_jobs_status` | `status IN ('queued','running','completed','failed')` |
| `user_claim_annotations` (parked) | `ck_user_claim_annotations_status` | `status IN ('open','inserted','dismissed','stale')` |
| `users` | `ck_users_status_valid` | `status IN ('pending', 'active', 'disabled')` |
| `workspace_setup_jobs` (parked) | `ck_workspace_setup_jobs_status` | `status IN ('queued','running','waiting_for_rubric_review','done','failed','cancelled')` |
| `workspaces` (parked) | `ck_workspaces_status` | `status IN ('drafting','submitted','awarded','rejected','abandoned')` |

### Ownership: what each table is keyed on

| table | foreign keys (column → target, ON DELETE) |
|---|---|
| `artifact_versions` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`triggering_turn_id` → `chat_turns.id` (SET NULL) |
| `attachment_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`attachment_id` → `attachments.id` (CASCADE) |
| `attachments` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`uploaded_by` → `users.id` (RESTRICT) |
| `board_items` | `board_id` → `boards.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`approved_by` → `users.id` (RESTRICT) |
| `board_suppressions` | `board_id` → `boards.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`suppressed_by` → `users.id` (RESTRICT) |
| `boards` | `profile_id` → `profiles.id` (RESTRICT)<br>`created_by` → `users.id` (RESTRICT)<br>`published_by` → `users.id` (RESTRICT) |
| `branches` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `call_decisions` | `user_id` → `users.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE)<br>`call_id` → `calls.id` (CASCADE)<br>`decided_by` → `users.id` (SET NULL) |
| `call_ranking_criteria` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `call_rankings` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `call_rubrics` (parked) | `call_id` → `calls.id` (CASCADE) |
| `call_section_extracts` (parked) | `call_id` → `calls.id` (CASCADE) |
| `calls` | `source_id` → `sources.id` (RESTRICT) |
| `chat_summaries` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`summarized_through` → `chat_turns.id` (CASCADE) |
| `chat_turns` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `citation_audit_verdicts` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `citation_llm_usage` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `claim_analysis_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `claim_classifications` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`reference_id` → `references.id` (SET NULL) |
| `coherence_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `collab_artifact_state` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `digest_runs` | `user_id` → `users.id` (CASCADE) |
| `evaluator_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `golden_set_triples` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE)<br>`created_by` → `users.id` (SET NULL) |
| `llm_spend` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `phase_approvals` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`approving_user_id` → `users.id` (RESTRICT) |
| `profile_bootstrap_jobs` | `profile_id` → `profiles.id` (CASCADE) |
| `profile_viewers` | `profile_id` → `profiles.id` (CASCADE)<br>`user_id` → `users.id` (CASCADE) |
| `profiles` | `parent_profile_id` → `profiles.id` (SET NULL)<br>`created_by` → `users.id` (RESTRICT) |
| `proposed_changes` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`triggering_turn_id` → `chat_turns.id` (SET NULL)<br>`created_by` → `users.id` (SET NULL)<br>`resolved_by` → `users.id` (SET NULL) |
| `ranking_jobs` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `ranking_remote_attempts` | `call_id` → `calls.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `reference_chunks` (parked) | `reference_id` → `references.id` (CASCADE)<br>`workspace_id` → `workspaces.id` (CASCADE) |
| `references` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`uploaded_by` → `users.id` (RESTRICT) |
| `review_run_items` (parked) | `review_run_id` → `review_runs.id` (CASCADE) |
| `review_runs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`waived_from_review_run_id` → `review_runs.id` (SET NULL) |
| `spec_lock_events` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |
| `subscriptions` | `user_id` → `users.id` (CASCADE)<br>`profile_id` → `profiles.id` (CASCADE) |
| `turn_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`user_turn_id` → `chat_turns.id` (SET NULL)<br>`submitted_by_user_id` → `users.id` (RESTRICT) |
| `uncited_claims_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`requested_by` → `users.id` (SET NULL) |
| `user_claim_annotations` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`reference_id` → `references.id` (SET NULL)<br>`resolved_by_user_id` → `users.id` (SET NULL) |
| `users` | `approved_by_user_id` → `users.id` (SET NULL)<br>`default_profile_id` → `profiles.id` (SET NULL) |
| `workspace_events` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |
| `workspace_setup_jobs` (parked) | `workspace_id` → `workspaces.id` (CASCADE) |
| `workspace_viewers` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`user_id` → `users.id` (CASCADE) |
| `workspaces` (parked) | `owner_id` → `users.id` (RESTRICT)<br>`call_id` → `calls.id` (SET NULL)<br>`profile_id` → `profiles.id` (SET NULL)<br>`root_branch_id` → `branches.id` (RESTRICT)<br>`spec_locked_by` → `users.id` (SET NULL) |
| `yjs_updates` (parked) | `workspace_id` → `workspaces.id` (CASCADE)<br>`actor_id` → `users.id` (SET NULL) |

## Caps, ceilings and limits

4 settings in `config.py`. A cap that exists in only one of a pair of
sibling paths is the shape of a real defect.

| setting | default |
|---|---|
| `max_request_body_bytes` | `Field(default=64 * 1024 * 1024, ge=0, alias='MAX_REQUEST_BODY_BYTES')` |
| `ranking_remote_daily_cap` | `Field(default=300, alias='RANKING_REMOTE_DAILY_CAP', description='Max remote escalation attempts per Europe/Stockholm day (counted from ranking_remote_attempts). Guardrail for backfill spikes. Raised 100 -> 150 on 2026-09-09 when REMOTE_RANK_MODEL moved from claude-sonnet-4-6 ($3/$15 per MTok) to claude-sonnet-5 ($2/$10) -- the 2/3 price ratio makes 150 escalations cost what 100 did, so this is a cost-neutral raise, not a budget increase. It was chosen because the cap was being HIT: on 2026-09-09, 33 pairs scoring >= RANKING_ESCALATION_MIN_SCORE were denied escalation after the 100th attempt and shipped with a Gemma-only score and no analysis. Raised 150 -> 300 on 2026-10-05: since ranking freshness (2026-09-24) a day uses 0-27 attempts (09-25..10-04), so the cap binds only on a full re-rank, and 150 split the 09-24 re-rank (223 attempts that day) over two days.')` |
| `ranking_tick_budget_seconds` | `Field(default=240.0, alias='RANKING_TICK_BUDGET_SECONDS')` |
| `session_max_age_seconds` | `Field(default=30 * 24 * 3600, alias='SESSION_MAX_AGE_SECONDS', description='Session cookie lifetime. 30 days by default; an organisational policy choice.')` |

