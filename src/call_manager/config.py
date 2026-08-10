from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Known development / placeholder session secrets that must never be accepted in
# a production (https) deployment — exact-match so a legitimate random token
# can't trip a false positive. The `min_length=16` Field constraint is the floor
# everywhere; this list + the 32-char prod minimum is the extra bar for
# internet-exposed deploys.
_DEV_SECRET_SENTINELS = frozenset(
    {
        "dev-only-session-secret-not-for-prod",
        "test-secret-at-least-16-chars",
        "changeme",
        "change-me",
        "secret",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    session_secret: str = Field(alias="SESSION_SECRET", min_length=16)
    # NoDecode: pydantic-settings would otherwise try to JSON-decode the env
    # value for complex types. We want plain comma-separated strings, parsed
    # by the validator below.
    allowed_email_domains: Annotated[list[str], NoDecode] = Field(alias="ALLOWED_EMAIL_DOMAINS")
    initial_admin_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="INITIAL_ADMIN_EMAIL"
    )
    initial_admin_password: str | None = Field(default=None, alias="INITIAL_ADMIN_PASSWORD")
    app_base_url: str = Field(alias="APP_BASE_URL")
    google_oidc_enabled: bool = Field(default=False, alias="GOOGLE_OIDC_ENABLED")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_oidc_discovery_url: str = Field(
        default="https://accounts.google.com/.well-known/openid-configuration",
        alias="GOOGLE_OIDC_DISCOVERY_URL",
    )
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    email_from: str = Field(default="no-reply@example.com", alias="EMAIL_FROM")
    # Set to True only when running behind a proxy we control that
    # sets X-Forwarded-For correctly (e.g. Nginx/Traefik with
    # `proxy_set_header X-Forwarded-For`). When False, X-Forwarded-For
    # is ignored so an attacker can't spoof their rate-limit bucket.
    trusted_proxy: bool = Field(default=False, alias="TRUSTED_PROXY")
    # Number of trusted reverse-proxy hops between the client and the app. Each
    # trusted hop appends one entry to X-Forwarded-For, so the real client IP is
    # the Nth-from-the-right entry. With the default 1 (single hospital-edge
    # proxy) the rightmost entry is used. Set to 2+ when TLS terminates behind a
    # second trusted layer; otherwise every external client would collapse into
    # one rate-limit bucket (the inner proxy's fixed IP). Only consulted when
    # trusted_proxy is True. Must be >= 1.
    trusted_proxy_hops: int = Field(default=1, ge=1, alias="TRUSTED_PROXY_HOPS")
    eu_portal_base_url: str = Field(default="https://ec.europa.eu", alias="EU_PORTAL_BASE_URL")
    vinnova_base_url: str = Field(default="https://data.vinnova.se", alias="VINNOVA_BASE_URL")
    admin_alert_email: str | None = Field(default=None, alias="ADMIN_ALERT_EMAIL")
    stale_source_alert_days: int = Field(
        default=4,
        alias="STALE_SOURCE_ALERT_DAYS",
        description=(
            "A source whose last_fetched_at is older than this many days "
            "triggers a stale-fetch alert regardless of the consecutive-failure "
            "streak state. Acts as a restart-independent backstop: catches "
            "sources that silently stop being fetched (e.g. scheduler crash "
            "within the 3-day streak window). Default 4 gives one full day of "
            "slack beyond the 3-day streak threshold."
        ),
    )
    ranking_tick_seconds: int = Field(default=60, alias="RANKING_TICK_SECONDS")
    ranking_queue_batch: int = Field(default=10, alias="RANKING_QUEUE_BATCH")
    # Soft per-tick wall-clock budget for the serial ranking drain (B11 Defect
    # 6). A single job can take ~240s (local Gemma + a Sonnet escalation); an
    # escalation-heavy batch could monopolize the scheduler slot for minutes.
    # Once exceeded, the drain stops claiming and leaves the remainder PENDING
    # for the next tick. Default 240s ≈ one worst-case job, generous enough that
    # a healthy batch never trips it while still bounding the pathological case.
    ranking_tick_budget_seconds: float = Field(
        default=240.0, alias="RANKING_TICK_BUDGET_SECONDS"
    )
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    gemma_model: str = Field(default="gemma4:e4b-it-q4_K_M", alias="GEMMA_MODEL")
    embedding_model: str = Field(default="bge-m3", alias="EMBEDDING_MODEL")
    # Expected embedding dimension. MUST match the model in EMBEDDING_MODEL and
    # the Vector(N) column width in db/models.py. bge-m3 and the dev fallback
    # mxbai-embed-large are both 1024; a swap to e.g. nomic-embed-text (768)
    # requires changing this AND a migration. Surfaced as config (not hardcoded
    # in the client) so the dimension and the model name can't silently drift.
    embedding_dim: int = Field(default=1024, ge=1, alias="EMBEDDING_DIM")
    # Cosine-similarity threshold for the embedding pre-gate. Pairs
    # below this (computed by the caller, not the gate) are zeroed out
    # before the LLM is invoked. Default 0.30 chosen from BGE-M3's typical
    # similarity distribution (see ranking-v2 spec).
    embedding_pregate_threshold: float = Field(default=0.30, alias="EMBEDDING_PREGATE_THRESHOLD")
    # Two-band embedding gate (Codex #1):
    #   sim < EMBEDDING_HARD_FAIL_THRESHOLD  -> short-circuit FAIL, no LLM
    #   hard_fail <= sim < pregate_threshold -> UNKNOWN, LLM adjudicates
    #   sim >= pregate_threshold             -> PASS
    # Defaults to 0.20 so any pair whose BGE-M3 cosine is in [0.20, 0.30)
    # reaches the LLM instead of being silently zeroed. Tighten (raise
    # toward the pregate) once the golden set has enough examples to
    # calibrate the cutoff against observed false negatives.
    embedding_hard_fail_threshold: float = Field(
        default=0.20, alias="EMBEDDING_HARD_FAIL_THRESHOLD"
    )
    # Remote-rank escalation: pairs whose local aggregate score reaches
    # ranking_escalation_min_score are re-ranked by the hosted model
    # (Anthropic). Leave anthropic_api_key unset (None or "") to disable —
    # rank_pair then routes every pair to the local client and the
    # escalation settings are ignored.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    drafting_enabled: bool = Field(
        default=True,
        alias="DRAFTING_ENABLED",
        description=(
            "Kill switch for the proposal-assist drafting subsystem. "
            "When False, POST /workspaces/<id>/turns returns 503 and "
            "the drain tick is a no-op (queued turns wait for re-enable). "
            "Toggle requires app restart; UI-driven toggling is deferred."
        ),
    )
    extractor_version: str = Field(
        default="sections-v1",
        alias="EXTRACTOR_VERSION",
        description="Bump when the section extractor prompt or output schema changes; "
        "stored on call_section_extracts.extractor_version and used by Phase 0 "
        "to decide whether to re-extract.",
    )
    rubric_extractor_version: str = Field(
        default="rubric-v1",
        alias="RUBRIC_EXTRACTOR_VERSION",
        description="Bump when the rubric extractor prompt or output schema changes.",
    )
    proposal_assist_llm_model: str = Field(
        default="claude-opus-5",
        alias="PROPOSAL_ASSIST_LLM_MODEL",
        description="Anthropic model id used for proposal-assist extractors, "
        "score_against_rubric, fit-check, persona scoring, and consolidation.",
    )
    phase0_tick_seconds: int = Field(
        default=30,
        alias="PHASE0_TICK_SECONDS",
        description="How often the in-process Phase 0 worker drains "
        "workspace_setup_jobs. Lowering this shortens the gap "
        "between workspace creation and rubric-review prompt; "
        "raising it reduces idle DB churn when the queue is empty.",
    )
    profile_bootstrap_tick_seconds: int = Field(
        default=30,
        alias="PROFILE_BOOTSTRAP_TICK_SECONDS",
        description="How often the in-process profile bootstrap worker drains "
        "profile_bootstrap_jobs. Lowering this shortens the gap "
        "between profile creation and LLM draft completion; "
        "raising it reduces idle DB churn when the queue is empty.",
    )
    workspaces_dir: str = Field(
        default="var/workspaces",
        alias="WORKSPACES_DIR",
        description="Filesystem root for per-workspace git repos. Each "
        "workspace gets var/workspaces/<uuid>/repo/. Must be "
        "writable, backed up, excluded from the docker image.",
    )
    drafting_adapter: str = Field(
        default="claude",
        alias="DRAFTING_ADAPTER",
        description=(
            "Which DraftingAgent implementation to wire up. "
            "'claude' (default) → ClaudeAgentSDKAdapter; "
            "'fake' → FakeAgentAdapter (for integration tests + local "
            "demos without an API key)."
        ),
    )
    drafting_max_live_subprocesses: int = Field(
        default=8,
        alias="DRAFTING_MAX_LIVE_SUBPROCESSES",
        gt=0,
        description=(
            "Hard ceiling on concurrent live drafting subprocesses (Defect 1 "
            "remediation). Each live subprocess pins ONE pooled DB connection "
            "for its lifetime (the session-level advisory lock keeps it checked "
            "out, up to WORKSPACE_IDLE_TIMEOUT_SECONDS). Keep this well below the "
            "engine pool (pool_size=10 + max_overflow=20 = 30) so drafting can "
            "never starve web/scheduler connections. In production those lock "
            "sessions come from a DEDICATED engine (DRAFTING_DB_POOL_SIZE) sized "
            "to this ceiling, so they don't draw on the request pool at all. At "
            "the cap, a new launch evicts the least-recently-used IDLE handle; if "
            "all are mid-turn, it returns a retriable 429."
        ),
    )
    drafting_db_pool_size: int = Field(
        default=8,
        alias="DRAFTING_DB_POOL_SIZE",
        gt=0,
        description=(
            "pool_size for the DEDICATED engine that backs drafting advisory-lock "
            "sessions, isolating long-held drafting connections from the request/"
            "scheduler engine. Size it to DRAFTING_MAX_LIVE_SUBPROCESSES (+1 head "
            "room for the brief launch overlap during LRU eviction). max_overflow "
            "is 1 so the pool can't silently exceed the ceiling."
        ),
    )
    workspace_idle_timeout_seconds: int = Field(
        default=600,
        alias="WORKSPACE_IDLE_TIMEOUT_SECONDS",
        description=(
            "Seconds of inactivity after which a drafting subprocess is "
            "torn down. The next user turn relaunches it (with resume-"
            "from-history). Lower = less idle memory; higher = fewer "
            "cold starts on bursty usage."
        ),
    )
    workspace_idle_sweep_seconds: int = Field(
        default=60,
        alias="WORKSPACE_IDLE_SWEEP_SECONDS",
        description=(
            "Cadence at which the scheduler asks the subprocess "
            "registry to sweep handles idle for "
            "WORKSPACE_IDLE_TIMEOUT_SECONDS or longer. Faster sweep = "
            "tighter teardown latency but more polling."
        ),
    )
    collab_checkpoint_scan_seconds: int = Field(
        default=60,
        alias="COLLAB_CHECKPOINT_SCAN_SECONDS",
        description=(
            "Interval for the collab tail-bounding scan (Slice 3b-5c). 0 disables the "
            "periodic job entirely."
        ),
    )
    collab_checkpoint_min_tail: int = Field(
        default=200,
        alias="COLLAB_CHECKPOINT_MIN_TAIL",
        description="Min uncheckpointed Yjs-log rows before a collab room is auto-checkpointed.",
    )
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
    daily_token_cap_input: int = Field(
        default=500_000,
        alias="DAILY_TOKEN_CAP_INPUT",
        description=(
            "Per-workspace default daily input-token cap (Europe/Stockholm "
            "day). Overridable per-workspace via "
            "workspaces.daily_token_cap_in_override. Enforced pre-enqueue "
            "by submit_user_turn(); B8 will surface the UX."
        ),
    )
    daily_token_cap_output: int = Field(
        default=100_000,
        alias="DAILY_TOKEN_CAP_OUTPUT",
        description="Per-workspace default daily output-token cap.",
    )
    global_daily_token_cap_input: int = Field(
        default=5_000_000,
        alias="GLOBAL_DAILY_TOKEN_CAP_INPUT",
        description=(
            "Org-wide ceiling on total input tokens (sum across all "
            "workspaces) per Europe/Stockholm day. Defaults to 10x the "
            "per-workspace cap so a single runaway workspace can't bill "
            "the whole budget. Enforced before per-workspace cap; a small "
            "overshoot from concurrent submits is acceptable (guardrail, "
            "not exact ledger)."
        ),
    )
    global_daily_token_cap_output: int = Field(
        default=1_000_000,
        alias="GLOBAL_DAILY_TOKEN_CAP_OUTPUT",
        description=(
            "Org-wide ceiling on total output tokens (sum across all "
            "workspaces) per Europe/Stockholm day. Defaults to 10x the "
            "per-workspace cap."
        ),
    )
    coherence_daily_token_cap: int = Field(
        default=200_000,
        alias="COHERENCE_DAILY_TOKEN_CAP",
        description=(
            "Per-workspace daily ceiling on combined input+output tokens "
            "spent by coherence-keeper passes (Europe/Stockholm day). A pass "
            "is blocked pre-run once today's coherence_jobs token sum reaches "
            "this cap. The other coherence settings land in SP2b-2c Task 7."
        ),
    )
    coherence_pass_timeout_seconds: int = Field(
        default=180,
        alias="COHERENCE_PASS_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the keeper SDK turn. On "
        "timeout the one-shot adapter is terminated and the job is failed.",
    )
    coherence_tick_seconds: int = Field(
        default=20,
        alias="COHERENCE_TICK_SECONDS",
        description="How often the coherence drain tick claims+runs one queued "
        "coherence pass. Manual-only, so this just bounds latency from click to run.",
    )
    coherence_job_timeout_seconds: int = Field(
        default=300,
        alias="COHERENCE_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL (age cutoff): a running coherence job older than this is "
        "failed (crash/restart recovery), unwedging the workspace's active-unique slot.",
    )
    coherence_reaper_tick_seconds: int = Field(
        default=60,
        alias="COHERENCE_REAPER_TICK_SECONDS",
        description="How often the reaper sweep RUNS (decoupled from the TTL age cutoff "
        "above). Keeping this below coherence_job_timeout_seconds caps worst-case unwedge "
        "after a crash at ~TTL + this interval, rather than ~2x TTL when the two are equal.",
    )
    coherence_terminate_timeout_seconds: int = Field(
        default=30,
        alias="COHERENCE_TERMINATE_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the one-shot keeper adapter's "
        "terminate() cleanup. A hung SDK disconnect must not leak the pass coroutine; "
        "kept well under the scheduler's coherence_pass_timeout_seconds+60 future grace.",
    )
    evaluator_tick_seconds: int = Field(
        default=20,
        alias="EVALUATOR_TICK_SECONDS",
        description="How often the evaluator drain tick claims+runs one queued evaluator "
        "pass. Manual-only, so this just bounds latency from click to run.",
    )
    evaluator_pass_timeout_seconds: int = Field(
        default=180,
        alias="EVALUATOR_PASS_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the evaluator SDK turn (launch+drive). "
        "On timeout the one-shot adapter is terminated and the job is failed.",
    )
    evaluator_job_timeout_seconds: int = Field(
        default=300,
        alias="EVALUATOR_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL (age cutoff): a running evaluator job older than this is failed "
        "(crash/restart recovery), unwedging the workspace's active-unique slot.",
    )
    evaluator_reaper_tick_seconds: int = Field(
        default=60,
        alias="EVALUATOR_REAPER_TICK_SECONDS",
        description="How often the evaluator reaper sweep RUNS (decoupled from the TTL age "
        "cutoff above), capping worst-case unwedge at ~TTL + this interval.",
    )
    evaluator_terminate_timeout_seconds: int = Field(
        default=30,
        alias="EVALUATOR_TERMINATE_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the one-shot evaluator adapter's "
        "terminate() cleanup. A hung SDK disconnect must not leak the pass coroutine.",
    )
    evaluator_daily_token_cap: int = Field(
        default=200_000,
        alias="EVALUATOR_DAILY_TOKEN_CAP",
        description="Per-workspace daily ceiling on combined input+output tokens spent by "
        "evaluator passes (Europe/Stockholm day). A pass is blocked pre-run once reached.",
    )
    # SP2b-3d: on-demand uncited-claims (LLM) check (a single background pass).
    uncited_claims_tick_seconds: int = Field(
        default=20,
        alias="UNCITED_CLAIMS_TICK_SECONDS",
        description="How often the uncited-claims drain tick claims+runs one queued pass. "
        "Manual-only, so this just bounds latency from click to run.",
    )
    uncited_claims_pass_timeout_seconds: int = Field(
        default=180,
        alias="UNCITED_CLAIMS_PASS_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the uncited-claims SDK turn "
        "(launch+drive). On timeout the one-shot adapter is terminated and the job is failed.",
    )
    uncited_claims_job_timeout_seconds: int = Field(
        default=300,
        alias="UNCITED_CLAIMS_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL: a running uncited-claims job older than this is failed "
        "(crash/restart recovery), unwedging the workspace's active-unique slot.",
    )
    uncited_claims_reaper_tick_seconds: int = Field(
        default=60,
        alias="UNCITED_CLAIMS_REAPER_TICK_SECONDS",
        description="How often the uncited-claims reaper sweep RUNS (decoupled from the TTL).",
    )
    uncited_claims_terminate_timeout_seconds: int = Field(
        default=30,
        alias="UNCITED_CLAIMS_TERMINATE_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the one-shot adapter's terminate() "
        "cleanup. A hung SDK disconnect must not leak the pass coroutine.",
    )
    uncited_claims_daily_token_cap: int = Field(
        default=200_000,
        alias="UNCITED_CLAIMS_DAILY_TOKEN_CAP",
        description="Per-workspace daily ceiling on combined input+output tokens spent by "
        "uncited-claims passes (Europe/Stockholm day). A pass is blocked pre-run once reached.",
    )
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
        ge=1,
        alias="REVIEW_PERSONA_CONCURRENCY",
        description="Max simultaneous wave-1 sub-calls (fit-check + 4 personas) within one run. "
        "Must be >= 1: asyncio.Semaphore(0) would deadlock the fan-out until the reaper TTL.",
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
        description="Input price per 1M tokens for proposal_assist_llm_model (cache-read is "
        "bundled into tokens_in by the adapter and priced at this rate, which OVERSTATES it — "
        "a cache hit really costs 0.1x input). $5.00 covers Opus 4.5 through Opus 5, which all "
        "share this rate; re-check against the pricing page when changing the model.",
    )
    review_price_output_per_mtok: Decimal = Field(
        default=Decimal("25.00"),
        alias="REVIEW_PRICE_OUTPUT_PER_MTOK",
        description="Output price per 1M tokens for proposal_assist_llm_model. $25.00 covers "
        "Opus 4.5 through Opus 5, which all share this rate.",
    )
    problem_section_char_budget: int = Field(
        default=6000,
        alias="PROBLEM_SECTION_CHAR_BUDGET",
        description="Per-section character budget for the Problems panel's over-length "
        "heuristic (draft sections only). A loose guardrail, not a hard funder limit; "
        "a value <= 0 disables the over-length check.",
    )
    citation_deep_check_max_per_request: int = Field(
        default=25,
        alias="CITATION_DEEP_CHECK_MAX_PER_REQUEST",
        description=(
            "Maximum number of FRESH (uncached) semantic LLM calls made per "
            "POST /citation-audit/deep request. Already-cached verdicts are "
            "returned without counting against this ceiling. Rows beyond the cap "
            "are deferred and reported in the response so the client can re-poll "
            "to continue. Default 25 keeps a single request well within a "
            "synchronous HTTP timeout budget."
        ),
    )
    citation_llm_provider: str = Field(default="ollama", alias="CITATION_LLM_PROVIDER")
    citation_llm_model: str = Field(
        default="",
        alias="CITATION_LLM_MODEL",
        description="Empty string = use the provider's default model "
        "(GEMMA_MODEL for ollama, REMOTE_RANK_MODEL for anthropic).",
    )
    citation_llm_timeout_seconds: float = Field(default=60.0, alias="CITATION_LLM_TIMEOUT_SECONDS")
    citation_llm_max_output_tokens: int = Field(
        default=1024, alias="CITATION_LLM_MAX_OUTPUT_TOKENS"
    )
    citation_llm_daily_token_cap_input: int = Field(
        default=200_000, alias="CITATION_LLM_DAILY_TOKEN_CAP_INPUT"
    )
    citation_llm_daily_token_cap_output: int = Field(
        default=50_000, alias="CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT"
    )
    global_citation_llm_daily_token_cap_input: int = Field(
        default=2_000_000, alias="GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_INPUT"
    )
    global_citation_llm_daily_token_cap_output: int = Field(
        default=500_000, alias="GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT"
    )
    reference_extract_timeout_seconds: float = Field(
        default=30.0,
        alias="REFERENCE_EXTRACT_TIMEOUT_SECONDS",
        description="Hard wall-clock bound on untrusted file text-extraction. "
        "Extraction runs in a separate process that is terminated on timeout; "
        "the reference is then marked text_status='failed'. Guards the single "
        "app process against a pathological upload pinning the CPU.",
    )
    reference_ingest_concurrency: int = Field(
        default=3,
        ge=1,
        le=32,
        description="Max concurrent reference ingests across upload/fetch/tool. "
        "Acquired before buffering the body; over-capacity uploads get 429.",
    )
    reference_max_batch_files: int = Field(
        default=25, ge=1, description="Client-enforced max PDFs per drag-drop batch."
    )
    reference_max_batch_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1,
        description="Client-enforced max total bytes per drag-drop batch.",
    )
    compaction_turn_threshold: int = Field(
        default=100,
        alias="COMPACTION_TURN_THRESHOLD",
        description=(
            "Number of un-summarized chat_turns since the latest "
            "chat_summaries row that triggers compaction."
        ),
    )
    compaction_token_threshold: int = Field(
        default=200_000,
        alias="COMPACTION_TOKEN_THRESHOLD",
        description=(
            "Sum of input-tokens across un-summarized chat_turns that "
            "triggers compaction (whichever threshold trips first)."
        ),
    )
    compaction_tick_seconds: int = Field(
        default=300,
        alias="COMPACTION_TICK_SECONDS",
        description="How often the compaction tick scans for workspaces over threshold.",
    )
    analysis_tick_seconds: int = Field(
        default=20,
        alias="ANALYSIS_TICK_SECONDS",
        description="Claim-analysis queue drain interval (seconds).",
    )
    analysis_reconcile_tick_seconds: int = Field(
        default=900,
        alias="ANALYSIS_RECONCILE_TICK_SECONDS",
        description="Claim-analysis coverage reconcile interval (seconds).",
    )
    turn_drain_tick_seconds: int = Field(
        default=5,
        alias="TURN_DRAIN_TICK_SECONDS",
        description=(
            "How often the turn-drain tick picks one queued turn per "
            "workspace and dispatches it. Lower = snappier perceived "
            "latency; higher = less polling overhead. B6's SSE bridge "
            "may add its own zero-latency wakeup so this is a floor, "
            "not the only mechanism."
        ),
    )
    turn_max_seconds: int = Field(
        default=600,
        alias="TURN_MAX_SECONDS",
        description=(
            "Hard wall-clock bound on a single turn's processing inside the "
            "drain tick. A turn that exceeds this is cancelled in-loop and "
            "finalized as failed, releasing the workspace's running-row "
            "cleanly. MUST stay comfortably below the reaper TTL "
            "(reaper.DEFAULT_REAP_TTL_SECONDS) so a slow-but-live turn is "
            "bounded by this clean path, not reaped while still streaming."
        ),
    )
    remote_rank_model: str = Field(default="claude-sonnet-4-6", alias="REMOTE_RANK_MODEL")
    ranking_escalation_min_score: int = Field(
        default=45,
        alias="RANKING_ESCALATION_MIN_SCORE",
        description="Local aggregate score at/above which a pair re-runs on the "
        "remote model. Mirrors DIGEST_BORDERLINE_LOWER so every digest-worthy "
        "call gets the Sonnet second opinion + analysis.",
    )
    ranking_remote_daily_cap: int = Field(
        default=100,
        alias="RANKING_REMOTE_DAILY_CAP",
        description="Max remote escalation attempts per Europe/Stockholm day "
        "(counted from ranking_remote_attempts). Guardrail for backfill spikes.",
    )
    email_transport: str = Field(default="resend", alias="EMAIL_TRANSPORT")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    # Session cookie TTL. Drives BOTH the itsdangerous signer's max_age
    # (server-side signature expiry) and the Set-Cookie Max-Age attribute
    # (client-side cookie expiry). Keep these in lockstep: if the signer's
    # max_age is longer, a cookie that the browser has already discarded
    # could still be re-presented and accepted; if shorter, a valid cookie
    # will be rejected before it expires in the browser. 30 days is the
    # hospital-internal default (staff log in from shared workstations and
    # expect single-sign-on-style persistence). High-security roles or
    # public-facing deployments should shorten via SESSION_MAX_AGE_SECONDS.
    # Note: logout bumps session_epoch, which invalidates ALL outstanding
    # cookies for a user regardless of TTL — so the 30-day window is the
    # idle-session bound, not a hard upper limit on session lifetime.
    session_max_age_seconds: int = Field(default=30 * 24 * 3600, alias="SESSION_MAX_AGE_SECONDS")
    # App-level cap on request body size (bytes). Backstop behind a reverse
    # proxy's client_max_body_size. Must exceed the largest legitimate upload:
    # reference uploads are capped at 50 MiB (references web route), so the
    # default leaves headroom for multipart overhead. 0 disables the check.
    max_request_body_bytes: int = Field(
        default=64 * 1024 * 1024, ge=0, alias="MAX_REQUEST_BODY_BYTES"
    )
    digest_dry_run: bool = Field(default=False, alias="DIGEST_DRY_RUN")
    admin_dashboard_url: str = Field(default="http://localhost:8000/", alias="ADMIN_DASHBOARD_URL")
    # SPA serving mode (React-SPA foundation, Plan 2):
    #   'disabled' (default) — no SPA mounted; the server-rendered HTML app is
    #                          the whole surface (current production behavior).
    #   'prod'              — FastAPI serves the built SPA from web/spa/ as a
    #                          manifest-gated catch-all (see web/spa_serving.py).
    #   'dev'              — SPA is served by the Vite dev server; FastAPI mounts
    #                          nothing (the dev proxy forwards backend prefixes).
    spa_mode: Literal["dev", "prod", "disabled"] = Field(default="disabled", alias="SPA_MODE")

    @field_validator("admin_dashboard_url", mode="after")
    @classmethod
    def _ensure_trailing_slash(cls, v: str) -> str:
        return v if v.endswith("/") else v + "/"

    @model_validator(mode="after")
    def _require_strong_secret_in_prod(self):
        """Refuse to start with a weak/placeholder SESSION_SECRET in production.

        Gated on `APP_BASE_URL` being https:// (the same signal that drives
        `cookie_secure` and the email-sender fallback), so local dev/tests over
        plain HTTP are unaffected. A short or known-placeholder signing key
        would let an attacker forge session cookies → full account/admin
        takeover, so we fail loud instead of silently accepting it.
        """
        if self.app_base_url.lower().startswith("https://"):
            if len(self.session_secret) < 32:
                raise ValueError(
                    "SESSION_SECRET must be at least 32 characters when "
                    "APP_BASE_URL is https:// (production). Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            if self.session_secret.lower() in _DEV_SECRET_SENTINELS:
                raise ValueError(
                    "SESSION_SECRET is a known development placeholder; set a "
                    "real random secret in production (APP_BASE_URL is https://)."
                )
        return self

    @model_validator(mode="after")
    def _validate_email_from(self):
        """Refuse the placeholder EMAIL_FROM when a real send transport is set.

        `no-reply@example.com` is the default for local dev/CI. It only matters
        once a real transport (Resend API key or SMTP host) is configured — then
        every send bounces from an IANA-controlled domain. A production deploy
        with NO transport falls back to NoOpEmailSender (see create_app) and is a
        valid, supported configuration, so we gate on transport-configured rather
        than on `APP_BASE_URL` alone — otherwise prod-without-email can't start.
        """
        has_transport = bool(self.resend_api_key or self.smtp_host)
        if has_transport and "example.com" in self.email_from.lower():
            raise ValueError(
                f"EMAIL_FROM is set to the placeholder '{self.email_from}' but a "
                "send transport (RESEND_API_KEY or SMTP_HOST) is configured. Set a "
                "real sender address (e.g. noreply@ki.se) to avoid bounces."
            )
        return self

    @model_validator(mode="after")
    def _validate_embedding_thresholds(self):
        """The two-band embedding gate requires hard_fail <= pregate, both in
        [0, 1]. An inverted pair (hard_fail > pregate) silently FAILs every pair
        whose cosine lands in [pregate, hard_fail) — calls that should PASS — so
        refuse to start rather than mis-rank the whole corpus on a typo'd env."""
        for name, val in (
            ("EMBEDDING_PREGATE_THRESHOLD", self.embedding_pregate_threshold),
            ("EMBEDDING_HARD_FAIL_THRESHOLD", self.embedding_hard_fail_threshold),
        ):
            if not 0.0 <= val <= 1.0:
                raise ValueError(f"{name} must be in [0.0, 1.0] (got {val!r})")
        if self.embedding_hard_fail_threshold > self.embedding_pregate_threshold:
            raise ValueError(
                "EMBEDDING_HARD_FAIL_THRESHOLD "
                f"({self.embedding_hard_fail_threshold}) must be <= "
                f"EMBEDDING_PREGATE_THRESHOLD ({self.embedding_pregate_threshold}); "
                "an inverted gate silently FAILs pairs whose cosine is in "
                "[pregate, hard_fail) that should PASS."
            )
        return self

    @model_validator(mode="after")
    def _require_google_credentials_when_enabled(self):
        if self.google_oidc_enabled and (
            not self.google_client_id or not self.google_client_secret
        ):
            raise ValueError(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required when "
                "GOOGLE_OIDC_ENABLED is true."
            )
        return self

    @field_validator("app_base_url", mode="after")
    @classmethod
    def _require_url_scheme(cls, v: str) -> str:
        # cookie_secure keys off this URL's scheme; a scheme-less value would
        # silently disable Secure cookies (and flip the email-sender fallback
        # in main.py). Fail fast on a malformed APP_BASE_URL instead.
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("APP_BASE_URL must start with http:// or https://")
        return v

    @field_validator("allowed_email_domains", "initial_admin_emails", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

    @field_validator("initial_admin_password", mode="after")
    @classmethod
    def _validate_initial_admin_password(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None  # None / whitespace-only -> unset (no trivial credential)
        if len(v) < 12:
            raise ValueError(
                "INITIAL_ADMIN_PASSWORD must be at least 12 characters (or empty/unset)."
            )
        return v  # unchanged — surrounding whitespace may be part of the password

    @field_validator("allowed_email_domains", mode="after")
    @classmethod
    def _normalize_domains(cls, v: list[str]) -> list[str]:
        # Canonicalize each allowlist entry to IDNA/punycode so the stored
        # allowlist lives in the same space the login check normalizes into
        # (auth.domain.normalize_domain) — an IDN homoglyph can't impersonate
        # an ASCII entry. Fail fast on a malformed configured domain.
        from call_manager.auth.domain import normalize_domain

        out: list[str] = []
        for d in v:
            try:
                out.append(normalize_domain(d))
            except ValueError as e:
                raise ValueError(f"ALLOWED_EMAIL_DOMAINS contains an invalid domain: {d!r}") from e
        return out

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cookie_secure(self) -> bool:
        """Whether to set Secure on session cookies.

        Derived from `APP_BASE_URL` because that's the URL the **browser**
        sees — which is what the Secure attribute keys off. In a typical
        deploy a TLS-terminating proxy (Nginx/Traefik/Apache) sits in
        front and forwards HTTP to the app, so `request.url.scheme` would
        say `http` and is the wrong signal. Don't change this to look at
        the incoming request — `APP_BASE_URL` is the right answer.

        Same value also gates the email-sender fallback in `main.py`:
        without it, a misconfigured prod (https without `RESEND_API_KEY`)
        would silently log magic links to stderr instead of refusing to
        start.
        """
        return self.app_base_url.lower().startswith("https://")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_redirect_uri(self) -> str:
        return self.app_base_url.rstrip("/") + "/auth/google/callback"
