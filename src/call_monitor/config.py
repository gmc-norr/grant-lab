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
        """Emit Secure cookie attribute only under HTTPS — derived from APP_BASE_URL."""
        return self.app_base_url.lower().startswith("https://")
