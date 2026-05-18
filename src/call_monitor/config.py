from typing import Annotated

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(alias="DATABASE_URL")
    session_secret: str = Field(alias="SESSION_SECRET", min_length=16)
    # NoDecode: pydantic-settings would otherwise try to JSON-decode the env
    # value for complex types. We want plain comma-separated strings, parsed
    # by the validator below.
    allowed_email_domains: Annotated[list[str], NoDecode] = Field(
        alias="ALLOWED_EMAIL_DOMAINS"
    )
    initial_admin_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="INITIAL_ADMIN_EMAIL"
    )
    app_base_url: str = Field(alias="APP_BASE_URL")
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    email_from: str = Field(default="no-reply@example.com", alias="EMAIL_FROM")
    # Set to True only when running behind a proxy we control that
    # sets X-Forwarded-For correctly (e.g. Nginx/Traefik with
    # `proxy_set_header X-Forwarded-For`). When False, X-Forwarded-For
    # is ignored so an attacker can't spoof their rate-limit bucket.
    trusted_proxy: bool = Field(default=False, alias="TRUSTED_PROXY")
    eu_portal_base_url: str = Field(
        default="https://ec.europa.eu", alias="EU_PORTAL_BASE_URL"
    )
    vinnova_base_url: str = Field(
        default="https://data.vinnova.se", alias="VINNOVA_BASE_URL"
    )
    admin_alert_email: str | None = Field(default=None, alias="ADMIN_ALERT_EMAIL")
    ranking_tick_seconds: int = Field(default=60, alias="RANKING_TICK_SECONDS")
    ranking_queue_batch: int = Field(default=10, alias="RANKING_QUEUE_BATCH")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    gemma_model: str = Field(default="gemma4:e4b-it-q4_K_M", alias="GEMMA_MODEL")
    embedding_model: str = Field(default="bge-m3", alias="EMBEDDING_MODEL")
    # Cosine-similarity threshold for the embedding pre-gate. Pairs
    # below this (computed by the caller, not the gate) are zeroed out
    # before the LLM is invoked. Default 0.30 chosen from BGE-M3's typical
    # similarity distribution (see ranking-v2 spec).
    embedding_pregate_threshold: float = Field(
        default=0.30, alias="EMBEDDING_PREGATE_THRESHOLD"
    )
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
    # Remote-rank escalation: pairs with cosine >= threshold go to a
    # hosted model (Anthropic) for a deeper analysis instead of the local
    # Gemma. Leave anthropic_api_key unset (None or "") to disable —
    # rank_pair then routes every pair to the local client and the
    # threshold + model fields are ignored. The threshold defaults to
    # 0.50; tune up to skip more, down to send more pairs to Sonnet.
    anthropic_api_key: str | None = Field(
        default=None, alias="ANTHROPIC_API_KEY"
    )
    remote_rank_model: str = Field(
        default="claude-sonnet-4-6", alias="REMOTE_RANK_MODEL"
    )
    remote_rank_cosine_threshold: float = Field(
        default=0.50, alias="REMOTE_RANK_COSINE_THRESHOLD"
    )
    email_transport: str = Field(default="resend", alias="EMAIL_TRANSPORT")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    # Session cookie TTL. Drives both the itsdangerous signer's max_age
    # and the Set-Cookie max-age; keep these in lockstep or a valid
    # signed session will outlast the cookie (or vice versa). 30 days is
    # the hospital-internal default; shorten via env for high-security
    # deployments.
    session_max_age_seconds: int = Field(
        default=30 * 24 * 3600, alias="SESSION_MAX_AGE_SECONDS"
    )
    digest_dry_run: bool = Field(default=False, alias="DIGEST_DRY_RUN")
    admin_dashboard_url: str = Field(
        default="http://localhost:8000/", alias="ADMIN_DASHBOARD_URL"
    )

    @field_validator("admin_dashboard_url", mode="after")
    @classmethod
    def _ensure_trailing_slash(cls, v: str) -> str:
        return v if v.endswith("/") else v + "/"

    @field_validator("allowed_email_domains", "initial_admin_emails", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

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
