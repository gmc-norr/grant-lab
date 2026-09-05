from __future__ import annotations

from grant_lab.email.client import EmailClient, ResendClient, SMTPClient


class EmailConfigError(Exception):
    """Settings inconsistency that blocks email transport from being built."""


def resolve_email_transport(settings) -> str | None:
    """Which transport this deployment uses: "resend", "smtp", or None.

    THE single transport decision. There used to be two, and they disagreed:
    `create_app` chose a sender from whichever credential was present and
    ignored EMAIL_TRANSPORT, while `build_email_client` read EMAIL_TRANSPORT —
    which DEFAULTED to "resend". An SMTP-only deploy therefore sent its
    ingestion alerts and silently never scheduled the weekly digest, because
    the lifespan swallowed the resulting EmailConfigError and
    `scheduler.py` registers `digest:weekly` only when the client exists.

    An explicit EMAIL_TRANSPORT always wins, including when it contradicts the
    credentials present — that is an operator's stated intent, and
    `build_email_client` fails loudly on it rather than quietly picking the
    other one. Only when it is unset do we infer, in create_app's order:
    resend first, then smtp. Matching that order matters; a different one would
    put the two selectors back into disagreement in a subtler way.
    """
    explicit = (settings.email_transport or "").strip().lower()
    if explicit:
        return explicit
    if settings.resend_api_key:
        return "resend"
    if settings.smtp_host:
        return "smtp"
    return None


def build_email_client(settings) -> EmailClient:
    """Build an EmailClient from settings. `settings` may be the real
    Settings class or any object with the same field shape (for tests)."""
    transport = resolve_email_transport(settings)
    if transport is None:
        raise EmailConfigError(
            "no email transport configured — set RESEND_API_KEY or SMTP_HOST "
            "(or EMAIL_TRANSPORT explicitly)"
        )
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
