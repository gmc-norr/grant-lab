"""An SMTP-only deploy silently never scheduled the weekly digest.

TWO selectors read one setting and disagreed:

  create_app          picks SMTPEmailSender on `smtp_host` alone, ignoring
                      EMAIL_TRANSPORT — so ingestion alerts send fine.
  build_email_client  reads EMAIL_TRANSPORT, which DEFAULTED to "resend", and
                      raised EmailConfigError for a missing RESEND_API_KEY.

The lifespan swallows that into one WARNING and leaves `digest_email_client`
None, and `scheduler.py:468` then never registers the `digest:weekly` job at
all. An operator who sets SMTP_HOST and nothing else — neither variable is in
`.env.example`, so neither gets copied — loses the product's core deliverable,
permanently, on a fully green /healthz, with the admin "send digest now" button
returning triggered=false.

The fix is one transport decision (`resolve_email_transport`) that both
selectors consume, not two agreeing copies.
"""

import pytest

from grant_lab.email.factory import (
    EmailConfigError,
    build_email_client,
    resolve_email_transport,
)


class _Settings:
    """Field-shaped stand-in; build_email_client accepts any such object."""

    def __init__(self, **kw):
        self.email_transport = kw.get("email_transport", "")
        self.resend_api_key = kw.get("resend_api_key")
        self.smtp_host = kw.get("smtp_host")
        self.smtp_port = kw.get("smtp_port", 587)
        self.smtp_user = kw.get("smtp_user")
        self.smtp_password = kw.get("smtp_password")
        self.email_from = kw.get("email_from", "x@umu.se")


def test_smtp_host_alone_builds_a_digest_client():
    # THE regression. Before: EmailConfigError -> digest_email_client=None ->
    # the digest job is never registered.
    client = build_email_client(_Settings(smtp_host="smtp.umu.se"))
    assert type(client).__name__ == "SMTPClient"


def test_resend_key_alone_still_builds_a_resend_client():
    client = build_email_client(_Settings(resend_api_key="re_x"))
    assert type(client).__name__ == "ResendClient"


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        # Inference, when EMAIL_TRANSPORT is unset — must match create_app's
        # order (resend wins when both are present), or the two selectors
        # disagree again in a subtler way.
        ({"resend_api_key": "re_x"}, "resend"),
        ({"smtp_host": "smtp.umu.se"}, "smtp"),
        ({"resend_api_key": "re_x", "smtp_host": "smtp.umu.se"}, "resend"),
        ({}, None),
        # An explicit setting always wins over inference, including when it
        # contradicts the credentials present — that is an operator's decision
        # and it should fail loudly, not be silently overridden.
        ({"email_transport": "smtp", "resend_api_key": "re_x"}, "smtp"),
        ({"email_transport": "resend", "smtp_host": "smtp.umu.se"}, "resend"),
    ],
)
def test_the_transport_decision_is_single_sourced(kw, expected):
    assert resolve_email_transport(_Settings(**kw)) == expected


def test_an_explicit_transport_without_its_credential_still_raises():
    # Inference must not paper over a misconfiguration the operator stated.
    with pytest.raises(EmailConfigError, match="RESEND_API_KEY"):
        build_email_client(_Settings(email_transport="resend", smtp_host="smtp.umu.se"))
    with pytest.raises(EmailConfigError, match="SMTP_HOST"):
        build_email_client(_Settings(email_transport="smtp", resend_api_key="re_x"))


def test_no_credentials_at_all_still_raises():
    with pytest.raises(EmailConfigError):
        build_email_client(_Settings())


def test_both_env_vars_are_documented():
    # Neither was in .env.example, so an operator copying it never learned
    # EMAIL_TRANSPORT existed — which is how the default silently won.
    from pathlib import Path

    text = Path(".env.example").read_text()
    assert "EMAIL_TRANSPORT" in text
    assert "SMTP_HOST" in text


def test_an_smtp_only_app_actually_builds_the_digest_client(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # The factory tests above prove the FACTORY. This proves the WIRING: the
    # lifespan swallows EmailConfigError into a WARNING, so a broken factory
    # shows up here as a None client and a `digest:weekly` job that is never
    # registered — on a fully green /healthz. `tests/test_scheduler_digest.py`
    # could not catch it: it passes `digest_email_client` in directly.
    from fastapi.testclient import TestClient

    from grant_lab.main import create_app

    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "WORKSPACES_DIR": str(tmp_path),
        # The whole deploy shape under test: SMTP host, nothing else.
        "SMTP_HOST": "smtp.umu.se",
        "EMAIL_FROM": "noreply@umu.se",  # a real address; the placeholder is rejected
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_TRANSPORT", raising=False)

    app = create_app()
    # pg_session_factory pulls in the migrated schema; the lifespan runs a
    # startup sweep that needs real tables.
    app.state.session_factory = pg_session_factory
    with TestClient(app):
        client = getattr(app.state, "digest_email_client", None)
        assert client is not None, (
            "SMTP-only deploy built no digest client — scheduler.py registers "
            "digest:weekly only when this exists, so the weekly digest would "
            "never be sent and nothing would say so"
        )
        assert type(client).__name__ == "SMTPClient"


@pytest.mark.parametrize(
    ("env", "expect_sender", "expect_client"),
    [
        # An explicit transport must move BOTH selectors together. Before, the
        # alert sender ignored EMAIL_TRANSPORT entirely, so this row picked
        # Resend for alerts and SMTP for the digest — the same disagreement in
        # the opposite direction.
        (
            {"EMAIL_TRANSPORT": "smtp", "RESEND_API_KEY": "re_x", "SMTP_HOST": "smtp.umu.se"},
            "SMTPEmailSender",
            "SMTPClient",
        ),
        (
            {"EMAIL_TRANSPORT": "resend", "RESEND_API_KEY": "re_x", "SMTP_HOST": "smtp.umu.se"},
            "ResendEmailSender",
            "ResendClient",
        ),
        # Inferred, no explicit setting.
        ({"SMTP_HOST": "smtp.umu.se"}, "SMTPEmailSender", "SMTPClient"),
        ({"RESEND_API_KEY": "re_x"}, "ResendEmailSender", "ResendClient"),
    ],
)
def test_both_selectors_always_agree(env, expect_sender, expect_client, monkeypatch, pg_url):
    from grant_lab.config import Settings
    from grant_lab.main import create_app

    base = {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "EMAIL_FROM": "noreply@umu.se",
    }
    for k in ("RESEND_API_KEY", "SMTP_HOST", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)

    app = create_app()
    assert type(app.state.email_sender).__name__ == expect_sender
    # The digest client is built in the lifespan from the same settings; build
    # it directly here so this stays a pure selector-agreement test.
    client = build_email_client(Settings())
    assert type(client).__name__ == expect_client
