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
    gemma_model: str = Field(default="gemma4:e4b-q8", alias="GEMMA_MODEL")
    email_transport: str = Field(default="resend", alias="EMAIL_TRANSPORT")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
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
