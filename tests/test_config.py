import re
import subprocess
from pathlib import Path

import pytest

from grant_lab.config import Settings


def test_settings_parse_domain_list_from_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se, regionvasterbotten.se,REGION.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    s = Settings()
    assert s.allowed_email_domains == ["umu.se", "regionvasterbotten.se", "region.se"]


def test_cookie_secure_follows_app_base_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    # Strong secret so the https branch passes the prod-secret validator.
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")

    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    assert Settings().cookie_secure is False

    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.umu.se")
    # Real EMAIL_FROM required in prod (https) — example.com default is rejected.
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")
    assert Settings().cookie_secure is True


def test_settings_parse_initial_admins_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "a@umu.se, B@Umu.se")

    s = Settings()
    assert s.initial_admin_emails == ["a@umu.se", "b@umu.se"]


def test_https_rejects_short_session_secret(monkeypatch):
    """In production (https) a <32-char SESSION_SECRET is refused."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "short-but-16-chars")  # 18 chars, < 32
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")

    with pytest.raises(ValueError, match="at least 32"):
        Settings()


def test_https_rejects_dev_placeholder_secret(monkeypatch):
    """A known dev placeholder secret is refused in production even if long."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "dev-only-session-secret-not-for-prod")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")

    with pytest.raises(ValueError, match="placeholder"):
        Settings()


def test_http_allows_weak_secret_for_dev(monkeypatch):
    """Plain-HTTP dev is unaffected by the prod strong-secret rule."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    assert Settings().session_secret == "test-secret-at-least-16-chars"


def test_allowed_domains_normalized_to_punycode(monkeypatch):
    """A Unicode allowlist entry is stored in its punycode form so the login
    check (which also normalizes) compares like-for-like."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se, REGIONVASTERBOTTEN.SE")

    s = Settings()
    assert s.allowed_email_domains == ["umu.se", "regionvasterbotten.se"]


def test_settings_require_session_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    with pytest.raises(ValueError):
        Settings()


def test_admin_dashboard_url_gets_trailing_slash_appended(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ADMIN_DASHBOARD_URL", "https://app.example.com")

    s = Settings()
    assert s.admin_dashboard_url == "https://app.example.com/"


def test_admin_dashboard_url_already_trailing_slash_is_idempotent(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ADMIN_DASHBOARD_URL", "https://app.example.com/")

    s = Settings()
    assert s.admin_dashboard_url == "https://app.example.com/"


def test_create_app_allows_console_sender_in_dev_without_resend_key(monkeypatch, pg_url):
    """Converse of the refuse-to-start test: plain HTTP APP_BASE_URL is
    dev, so ConsoleEmailSender is the intended fallback and create_app
    must NOT raise."""
    from grant_lab.auth.email import ConsoleEmailSender
    from grant_lab.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    app = create_app()
    try:
        assert isinstance(app.state.email_sender, ConsoleEmailSender)
    finally:
        app.state.engine.dispose()


def test_a_settings_validation_error_never_prints_a_secret(monkeypatch):
    """A boot-time config error must not put a credential in the traceback.

    Review 2026-10-26 A7: pydantic renders `input_value=` for a model-level
    validator failure. That input is the WHOLE settings dict; pydantic truncates
    it but keeps the tail, which is the last-defined field that is set — so a
    deployment with `SMTP_PASSWORD` set and no `SPA_MODE` put the SMTP password
    into the startup traceback and into any log that captured it.

    Both halves are asserted, because "the error does not contain the secret" is
    vacuously true of an error that was never raised or is empty: the error MUST
    be raised and MUST still name the field that is wrong.
    """
    # Obviously fake, and deliberately SHORT: pydantic truncates the middle of the
    # rendered dict and keeps its tail, so a long value is only partly visible and
    # "the secret is not in the string" would pass for the wrong reason. A short
    # one lands whole in the tail, which is the shape the A7 probe measured.
    fake_secret = "not-a-real-pw"  # never a real value
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "short-but-16-chars")  # 18 chars, < 32 under https
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")
    monkeypatch.setenv("SMTP_PASSWORD", fake_secret)

    with pytest.raises(ValueError) as excinfo:
        Settings()
    rendered = str(excinfo.value)
    assert "SESSION_SECRET" in rendered, (
        f"the error must still say what is wrong, or this test proves nothing: {rendered!r}"
    )
    assert "at least 32" in rendered, rendered
    assert fake_secret not in rendered, (
        "a Settings validation error must not render the raw config dict"
    )
    assert "input_value" not in rendered, (
        f"pydantic must not echo the input at all; got {rendered!r}"
    )


def test_the_escalation_threshold_no_longer_claims_to_mirror_the_digest_floor():
    """The description said it "Mirrors DIGEST_BORDERLINE_LOWER so every
    digest-worthy call gets the Sonnet second opinion". It does not:
    `DIGEST_BORDERLINE_LOWER` is 40 and the default threshold is 45, so calls
    scoring 40-44 reach the digest with local scores only (review 2026-10-26).
    """
    from grant_lab.config import Settings
    from grant_lab.digest.assemble import DIGEST_BORDERLINE_LOWER

    assert DIGEST_BORDERLINE_LOWER == 40
    description = Settings.model_fields["ranking_escalation_min_score"].description or ""
    assert "Mirrors DIGEST_BORDERLINE_LOWER" not in description
    assert str(DIGEST_BORDERLINE_LOWER) in description, (
        "the description must carry the real relationship to the digest floor, "
        "not a claim of equality"
    )


# --- every committed example secret is refused in production ----------------
#
# The README quickstart used to ship SESSION_SECRET=local-development-secret-
# change-me: 34 characters, so it passed the length floor, and absent from
# _DEV_SECRET_SENTINELS, so a deploy that kept it ran on a PUBLIC signing key
# and anyone could mint an admin cookie (beta review 2026-10-31 F1, reproduced
# against a real app). Length cannot tell a published key from a random one,
# so the rule is: any value written down in this repository is public.
#
# This scans what deployers read and copy (READMEs, .env.example, scripts, CI,
# compose, frontend config). tests/ is excluded — fixtures, not instructions —
# and so is docs/superpowers/, whose plans and reviews are dated records.

_REPO = Path(__file__).resolve().parents[1]
_SECRET_LITERAL = re.compile(
    r"""SESSION_SECRET["']?[ \t]*(?:=|:-|:|,)[ \t]*["']?([^\s"'`\\${}|)]+)"""
)


def _documented_session_secrets() -> dict[str, list[str]]:
    files = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    found: dict[str, list[str]] = {}
    for rel in files:
        if rel.startswith(("tests/", "docs/superpowers/")):
            continue
        try:
            text = (_REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for m in _SECRET_LITERAL.finditer(text):
            value = m.group(1).lstrip("-")
            if len(value) >= 8:
                found.setdefault(value, []).append(rel)
    return found


def test_the_scan_finds_the_known_examples():
    """Guard the guard: a scan that finds nothing would pass vacuously."""
    found = _documented_session_secrets()
    assert "test-secret-at-least-16-chars" in found  # scripts/gen_openapi_types.sh
    assert "e2e-seed-secret-at-least-16-chars!" in found  # scripts/collab_e2e_seed.py


def test_every_documented_session_secret_is_refused_over_https(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")

    accepted = {}
    for value, where in _documented_session_secrets().items():
        monkeypatch.setenv("SESSION_SECRET", value)
        try:
            Settings()
        except ValueError:
            continue
        accepted[value] = where
    assert accepted == {}, f"public example secrets accepted over https: {accepted}"


def test_the_readme_example_secret_is_refused_over_https(monkeypatch):
    """The exact value the review exploited, pinned on its own so the fix
    cannot pass by the scan quietly no longer reaching it."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.setenv("SESSION_SECRET", "local-development-secret-change-me")

    with pytest.raises(ValueError, match="placeholder"):
        Settings()
