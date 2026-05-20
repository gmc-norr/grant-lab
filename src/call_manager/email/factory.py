from __future__ import annotations

from call_manager.email.client import EmailClient, ResendClient, SMTPClient


class EmailConfigError(Exception):
    """Settings inconsistency that blocks email transport from being built."""


def build_email_client(settings) -> EmailClient:
    """Build an EmailClient from settings. `settings` may be the real
    Settings class or any object with the same field shape (for tests)."""
    transport = (settings.email_transport or "resend").lower()
    if transport == "resend":
        if not settings.resend_api_key:
            raise EmailConfigError("RESEND_API_KEY required when EMAIL_TRANSPORT=resend")
        return ResendClient(api_key=settings.resend_api_key, from_addr=settings.email_from)
    if transport == "smtp":
        if not settings.smtp_host:
            raise EmailConfigError("SMTP_HOST required when EMAIL_TRANSPORT=smtp")
        return SMTPClient(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_addr=settings.email_from,
        )
    raise EmailConfigError(f"unknown EMAIL_TRANSPORT={transport!r}")
