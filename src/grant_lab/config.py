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
        # Every value committed to this repo is public. These two are long
        # enough to pass the 32-char floor: the first was the README
        # quickstart's example until 2026-10-31 (beta review F1 forged an admin
        # cookie with it), the second is a script default. Lower-case only —
        # the check compares session_secret.lower().
        # tests/test_config.py scans the repo and fails on any new one.
        "local-development-secret-change-me",
        "e2e-seed-secret-at-least-16-chars!",
        "changeme",
        "change-me",
        "secret",
    }
)


class Settings(BaseSettings):
    # hide_input_in_errors: a validation failure here renders in the STARTUP
    # traceback and in whatever log captured it. pydantic's default puts
    # `input_value=` in the message, and for a model-level validator that input is
    # the WHOLE settings mapping — truncated in the middle but with its tail
    # intact, which is the last-defined field that happens to be set. Review
    # 2026-10-26 A7 measured SMTP_PASSWORD landing there verbatim on a deployment
    # that had no SPA_MODE set. The message itself still names the field that is
    # wrong (every validator above raises a sentence that says so), so nothing
    # diagnostic is lost — only the echo of the input.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: str = Field(
        alias="DATABASE_URL",
        description=(
            "SQLAlchemy URL of the Postgres 16 database (psycopg driver), e.g. "
            "postgresql+psycopg://call_manager:PASSWORD@localhost:5432/call_manager. "
            "Alembic reads the same value (alembic/env.py falls back to this file)."
        ),
    )
    session_secret: str = Field(
        alias="SESSION_SECRET",
        min_length=16,
        description=(
            "Signing key for the session cookie. Generate one: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))". Left blank in '
            "the example on purpose so a copied file cannot ship a known key; with an "
            "https:// APP_BASE_URL the app refuses to start unless it is >= 32 chars and "
            "not a known dev placeholder."
        ),
    )
    # NoDecode: pydantic-settings would otherwise try to JSON-decode the env
    # value for complex types. We want plain comma-separated strings, parsed
    # by the validator below.
    allowed_email_domains: Annotated[list[str], NoDecode] = Field(
        alias="ALLOWED_EMAIL_DOMAINS",
        description=(
            "Comma-separated email domains allowed to sign in or request access. Checked "
            "at registration, every sign-in and every request: a member whose domain is "
            "removed loses access at once. Administrators are exempt, except at Google "
            "sign-in, which also creates accounts."
        ),
    )
    initial_admin_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="INITIAL_ADMIN_EMAIL",
        description=(
            "Comma-separated admin emails created at startup. Lockout recovery: when NO "
            "active admin exists, these are re-activated and re-promoted at startup — "
            "otherwise an existing user is left exactly as it is. To permanently demote "
            "one, remove it from this list first."
        ),
    )
    initial_admin_password: str | None = Field(
        default=None,
        alias="INITIAL_ADMIN_PASSWORD",
        description=(
            "Password for a configured admin that does NOT have one yet (first boot, or "
            "an admin restored from total lockout). Must be >= 12 chars; empty = unset. "
            "It never overwrites an existing password hash (admin_bootstrap.py): a "
            "restart with this set does not reset a forgotten or compromised admin "
            "password — use the in-app admin reset, which needs a second admin. Unset it "
            "once logged in so the value stops living in a file."
        ),
    )
    app_base_url: str = Field(
        alias="APP_BASE_URL",
        description=(
            "The origin browsers use, scheme included, no path. It is what the CSRF "
            "origin check compares against, so in SPA_MODE=dev it is the Vite "
            "origin (http://localhost:5173); https:// switches on the production checks."
        ),
    )
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
    # The EU Funding & Tenders search API lives on the Commission's shared
    # SEDIA host, NOT on ec.europa.eu. The old ec.europa.eu v3.0 path now
    # 404s; see the header comment in sources/eu_portal.py.
    eu_portal_base_url: str = Field(
        default="https://api.tech.ec.europa.eu", alias="EU_PORTAL_BASE_URL"
    )
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
    ranking_tick_budget_seconds: float = Field(default=240.0, alias="RANKING_TICK_BUDGET_SECONDS")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    gemma_model: str = Field(default="gemma4:e4b-it-q4_K_M", alias="GEMMA_MODEL")
    embedding_model: str = Field(
        default="bge-m3",
        alias="EMBEDDING_MODEL",
        description=(
            "Ollama embedding model (1024-dim). Must match what Ollama has pulled; the "
            "similarity thresholds are calibrated for bge-m3."
        ),
    )
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
    profile_bootstrap_tick_seconds: int = Field(
        default=30,
        alias="PROFILE_BOOTSTRAP_TICK_SECONDS",
        description="How often the in-process profile bootstrap worker drains "
        "profile_bootstrap_jobs. Lowering this shortens the gap "
        "between profile creation and LLM draft completion; "
        "raising it reduces idle DB churn when the queue is empty.",
    )
    scheduler_standby_ok: bool = Field(
        default=False,
        alias="SCHEDULER_STANDBY_OK",
        description=(
            "This app is single-instance: the in-process scheduler and the login "
            "limiters are per-process state. When another process already holds the "
            "single-instance advisory lock, startup is FATAL by default. Set true "
            "to boot an HTTP-only standby instead: it serves requests, runs no "
            "scheduler, and /healthz answers 200 with scheduler_lock=false. Without "
            "this flag a process that does not hold the lock answers /healthz 503."
        ),
    )
    remote_rank_model: str = Field(default="claude-sonnet-5-5", alias="REMOTE_RANK_MODEL")
    remote_rank_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        default="high",
        alias="REMOTE_RANK_EFFORT",
        description="Anthropic `output_config.effort` for the remote ranker. `high` "
        "is what claude-sonnet-5-5 does when none is sent, so the default changes "
        "nothing; it is pinned so a REMOTE_RANK_MODEL swap cannot silently change "
        "thinking depth (claude-opus-5-5 defaults to medium). Effort is NOT in the "
        "scoring fingerprint (the remote model is not either), so changing it re-ranks "
        "nothing already stored. Lower it only after measuring against labels.",
    )
    ranking_escalation_min_score: int = Field(
        default=45,
        alias="RANKING_ESCALATION_MIN_SCORE",
        description="Local aggregate score at/above which a pair re-runs on the "
        "remote model. NOT the digest threshold: DIGEST_BORDERLINE_LOWER is 40, "
        "so a call scoring 40-44 reaches the digest with local scores only. "
        "Lower this to cover more of the digest's borderline band, at hosted "
        "cost and against the daily escalation cap.",
    )
    ranking_logprobs_enabled: bool = Field(
        default=False,
        alias="RANKING_LOGPROBS_ENABLED",
        description=(
            "Ask the local ranker for per-token logprobs so ranking/ev.py can "
            "replace the integer topic_fit rating with a probability-weighted "
            "expected value, un-quantising aggregate_score from multiples of "
            "20. OFF by default because it is measured at ~6x the wall-clock "
            "cost (127s vs 21s per ranking on gemma4:e4b-it-q4_K_M, 8000-char "
            "call, idle 2080 Ti: the response grows from 6KB to 1.2MB) while "
            "buying under 3 points of score movement at temperature 0 — far "
            "too little to cross RANKING_ESCALATION_MIN_SCORE, which sits 5 "
            "points from the nearest reachable score. Turn ON for a model "
            "whose rating distribution is less peaked, or if the temperature "
            "is raised."
        ),
    )
    ranking_remote_daily_cap: int = Field(
        default=300,
        alias="RANKING_REMOTE_DAILY_CAP",
        description="Max remote escalation attempts per Europe/Stockholm day "
        "(counted from ranking_remote_attempts). Guardrail for backfill spikes. "
        "Raised 100 -> 150 on 2026-09-09 when REMOTE_RANK_MODEL moved from "
        "claude-sonnet-4-6 ($3/$15 per MTok) to claude-sonnet-5 ($2/$10) -- the "
        "2/3 price ratio makes 150 escalations cost what 100 did, so this is a "
        "cost-neutral raise, not a budget increase. It was chosen because the "
        "cap was being HIT: on 2026-09-09, 33 pairs scoring >= "
        "RANKING_ESCALATION_MIN_SCORE were denied escalation after the 100th "
        "attempt and shipped with a Gemma-only score and no analysis. "
        "Raised 150 -> 300 on 2026-10-05: since ranking freshness "
        "(2026-09-24) a day uses 0-27 attempts (09-25..10-04), so the cap binds "
        "only on a full re-rank, and 150 split the 09-24 re-rank (223 "
        "attempts that day) over two days.",
    )
    email_transport: str = Field(
        default="",
        alias="EMAIL_TRANSPORT",
        description=(
            "Explicit email transport: 'resend' or 'smtp'. Empty (the default) "
            "means INFER from whichever credential is set — resend first, then "
            "smtp — which is what create_app's sender selection already did. "
            "It used to default to 'resend', so an SMTP-only deploy built its "
            "alert sender fine and silently never scheduled the weekly digest. "
            "See email/factory.resolve_email_transport."
        ),
    )
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
    session_max_age_seconds: int = Field(
        default=30 * 24 * 3600,
        alias="SESSION_MAX_AGE_SECONDS",
        description="Session cookie lifetime. 30 days by default; an organisational policy choice.",
    )
    # App-level cap on request body size (bytes). Backstop behind a reverse
    # proxy's client_max_body_size. 0 disables the check.
    max_request_body_bytes: int = Field(
        default=64 * 1024 * 1024, ge=0, alias="MAX_REQUEST_BODY_BYTES"
    )
    digest_dry_run: bool = Field(
        default=False,
        alias="DIGEST_DRY_RUN",
        description="Render the weekly digest without sending it. Set true in development.",
    )
    admin_dashboard_url: str = Field(
        default="http://localhost:8000/",
        alias="ADMIN_DASHBOARD_URL",
        description=(
            "Public URL of the dashboard; EVERY link in the weekly digest email is built "
            "from it. The default points at localhost, so a production deployment must set it."
        ),
    )
    # SPA serving mode:
    #   'disabled' (default) — no browser UI is mounted; backend routes remain
    #                          available (used by tests and API-only processes).
    #   'prod'              — FastAPI serves the built SPA from web/spa/ as a
    #                          manifest-gated catch-all (see web/spa_serving.py).
    #   'dev'              — SPA is served by the Vite dev server; FastAPI mounts
    #                          nothing (the dev proxy forwards backend prefixes).
    spa_mode: Literal["dev", "prod", "disabled"] = Field(
        default="disabled",
        alias="SPA_MODE",
        description=(
            "How the React SPA is served. prod = FastAPI serves the built bundle (the only "
            "production value; a built SPA must ship); dev = Vite dev server on :5173 "
            "proxies the API; disabled = API only, no UI at all. There is no safe default."
        ),
    )
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description=(
            "Root log level (DEBUG, INFO, WARNING, ERROR). APScheduler's own logger is "
            "held at WARNING regardless — its per-job INFO lines were 32 MB/day."
        ),
    )

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
                "real sender address (e.g. noreply@umu.se) to avoid bounces."
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

    @field_validator("log_level", mode="after")
    @classmethod
    def _log_level_is_a_level(cls, v: str) -> str:
        name = str(v).strip().upper()
        if name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return name

    @field_validator("allowed_email_domains", mode="after")
    @classmethod
    def _normalize_domains(cls, v: list[str]) -> list[str]:
        # Canonicalize each allowlist entry to IDNA/punycode so the stored
        # allowlist lives in the same space the login check normalizes into
        # (auth.domain.normalize_domain) — an IDN homoglyph can't impersonate
        # an ASCII entry. Fail fast on a malformed configured domain.
        from grant_lab.auth.domain import normalize_domain

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
