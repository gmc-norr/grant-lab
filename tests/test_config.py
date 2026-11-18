import re
import subprocess
from pathlib import Path

import pytest

from grant_lab.config import _PUBLIC_ADMIN_PASSWORDS, Settings


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
    monkeypatch.setenv("TRUSTED_PROXY", "true")
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
    monkeypatch.setenv("TRUSTED_PROXY", "true")

    with pytest.raises(ValueError, match="at least 32"):
        Settings()


def test_https_rejects_dev_placeholder_secret(monkeypatch):
    """A known dev placeholder secret is refused in production even if long."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "dev-only-session-secret-not-for-prod")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.setenv("TRUSTED_PROXY", "true")

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
    monkeypatch.setenv("TRUSTED_PROXY", "true")
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


def _deployer_facing_texts() -> dict[str, str]:
    """Every tracked text file a deployer reads or copies, by path."""
    files = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    texts: dict[str, str] = {}
    for rel in files:
        if rel.startswith(("tests/", "docs/superpowers/")):
            continue
        try:
            texts[rel] = (_REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
    return texts


def test_the_scans_reach_the_files_deployers_copy():
    """Guard the guard: both scans below read through this walk. If it came
    back empty (a broken git call, an over-wide skip list) they would find
    nothing and pass."""
    texts = _deployer_facing_texts()
    assert {"README.md", ".env.example", "scripts/e2e_seed.py", "frontend/README.md"} <= set(texts)
    assert not any(rel.startswith(("tests/", "docs/superpowers/")) for rel in texts)


def _documented_session_secrets() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for rel, text in _deployer_facing_texts().items():
        for m in _SECRET_LITERAL.finditer(text):
            value = m.group(1).lstrip("-")
            if len(value) >= 8:
                found.setdefault(value, []).append(rel)
    return found


def test_the_scan_finds_the_known_examples():
    """Guard the guard: a scan that finds nothing would pass vacuously."""
    found = _documented_session_secrets()
    assert "test-secret-at-least-16-chars" in found  # scripts/gen_openapi_types.sh
    # scripts/e2e_seed.py used to be the second example; it generates its key now.


def test_every_documented_session_secret_is_refused_over_https(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")
    # Control: with a real secret this environment starts. Without it the loop
    # below would count ANY startup refusal as "the secret was refused".
    monkeypatch.setenv("SESSION_SECRET", _STRONG)
    Settings()

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
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    monkeypatch.setenv("SESSION_SECRET", "local-development-secret-change-me")

    with pytest.raises(ValueError, match="placeholder"):
        Settings()


# --- one definition of "a real deployment" ----------------------------------
#
# Review 2026-11-18 (M5 and the config cluster). Every startup guard hung off
# "APP_BASE_URL is https://". The live deployment served the built UI
# (SPA_MODE=prod) over plain http, so no guard fired and it ran on a signing
# key that is listed in _DEV_SECRET_SENTINELS. The rule is now
# `Settings.is_production`: https, OR SPA_MODE=prod (the only mode that serves
# the UI for real). Measured on the whole suite before choosing: this rule
# reddened 6 tests in one file; "any non-localhost host" reddened 644.

_STRONG = "prod-grade-secret-with-enough-entropy-xyz123"


def _settings_env(monkeypatch, **over):
    env: dict[str, str | None] = {
        "DATABASE_URL": "postgresql://x",
        "SESSION_SECRET": _STRONG,
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://localhost:8000",
        "EMAIL_FROM": "noreply@umu.se",
        "SPA_MODE": None,
        "TRUSTED_PROXY": None,
        "INITIAL_ADMIN_PASSWORD": None,
    }
    env.update(over)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_prod_mode_over_plain_http_is_production(monkeypatch):
    _settings_env(monkeypatch, SPA_MODE="prod")
    s = Settings()
    assert s.is_production is True
    assert s.cookie_secure is False  # still http: no Secure cookie, no HSTS


@pytest.mark.parametrize("spa_mode", ["dev", "disabled", None])
def test_dev_and_api_only_over_http_are_not_production(monkeypatch, spa_mode):
    _settings_env(monkeypatch, SPA_MODE=spa_mode, SESSION_SECRET="test-secret-at-least-16-chars")
    assert Settings().is_production is False


def test_https_is_production_whatever_the_spa_mode(monkeypatch):
    _settings_env(monkeypatch, APP_BASE_URL="https://calls.example.org", TRUSTED_PROXY="true")
    assert Settings().is_production is True


def test_prod_mode_over_http_refuses_a_placeholder_secret(monkeypatch):
    """The live deployment's exact shape on 2026-11-18."""
    _settings_env(
        monkeypatch, SPA_MODE="prod", SESSION_SECRET="dev-only-session-secret-not-for-prod"
    )
    with pytest.raises(ValueError, match="placeholder"):
        Settings()


@pytest.mark.parametrize(
    "dressed",
    [
        "LOCAL-DEVELOPMENT-SECRET-CHANGE-ME",
        "local-development-secret-change-me     ",
        "  local-development-secret-change-me",
        '"local-development-secret-change-me"',
        "'local-development-secret-change-me'",
        "local-development-secret-change-me\n",
        '" local-development-secret-change-me "',
        "\u200blocal-development-secret-change-me",
    ],
)
def test_a_published_key_is_refused_however_it_is_dressed_up(monkeypatch, dressed):
    """Padding or quoting a published key (a systemd Environment= line keeps
    the quotes) made it 35+ characters and no longer an exact match."""
    _settings_env(monkeypatch, SPA_MODE="prod", SESSION_SECRET=dressed)
    with pytest.raises(ValueError, match="placeholder"):
        Settings()


def test_prod_mode_over_http_refuses_a_short_secret(monkeypatch):
    _settings_env(monkeypatch, SPA_MODE="prod", SESSION_SECRET="short-but-16-chars")
    with pytest.raises(ValueError, match="at least 32"):
        Settings()


def test_every_documented_session_secret_is_refused_under_spa_mode_prod(monkeypatch):
    _settings_env(monkeypatch, SPA_MODE="prod")
    Settings()  # control: this environment is valid with a real secret

    accepted = {}
    for value, where in _documented_session_secrets().items():
        monkeypatch.setenv("SESSION_SECRET", value)
        try:
            Settings()
        except ValueError:
            continue
        accepted[value] = where
    assert accepted == {}, f"public example secrets accepted under SPA_MODE=prod: {accepted}"


def test_https_refuses_spa_mode_dev(monkeypatch):
    """SPA_MODE=dev turns on /docs and loosens the CSP for the Vite server."""
    _settings_env(
        monkeypatch, APP_BASE_URL="https://calls.example.org", TRUSTED_PROXY="true", SPA_MODE="dev"
    )
    with pytest.raises(ValueError, match="SPA_MODE=dev"):
        Settings()


# --- the admin password a deployer can read on GitHub ------------------------
#
# The README quickstart shipped INITIAL_ADMIN_PASSWORD=change-this-password
# until 2026-11-18, and the validator only checked its length. Same rule as the
# session secret: any value written down in this repository is public.

_ADMIN_PASSWORD_LITERAL = re.compile(
    r"""INITIAL_ADMIN_PASSWORD["']?[ \t]*(?:=|:-|:|,)[ \t]*["']?([^\s"'`\\${}|)]+)"""
)


def _documented_admin_passwords() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for rel, text in _deployer_facing_texts().items():
        for m in _ADMIN_PASSWORD_LITERAL.finditer(text):
            found.setdefault(m.group(1), []).append(rel)
    return found


def test_the_admin_password_scan_can_see_a_literal():
    """Guard the guard: once the README is clean the repo scan finds nothing,
    so prove the pattern on the line it exists to catch."""
    block = "INITIAL_ADMIN_EMAIL=admin@example.org\nINITIAL_ADMIN_PASSWORD=change-this-password\n"
    assert _ADMIN_PASSWORD_LITERAL.findall(block) == ["change-this-password"]
    assert _ADMIN_PASSWORD_LITERAL.findall("INITIAL_ADMIN_PASSWORD=\nAPP_BASE_URL=x\n") == []


def test_no_documented_admin_password_is_accepted(monkeypatch):
    _settings_env(monkeypatch)
    Settings()  # control: valid without an admin password

    accepted = {}
    for value, where in _documented_admin_passwords().items():
        monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", value)
        try:
            Settings()
        except ValueError:
            continue
        accepted[value] = where
    assert accepted == {}, f"public example admin passwords accepted: {accepted}"


def test_the_published_passwords_stay_on_the_list():
    """The refusal test below is parametrized over the list itself, so a value
    dropped from it would simply stop being tested. Name them here."""
    assert {"change-this-password", "correct-horse-1"} <= _PUBLIC_ADMIN_PASSWORDS


@pytest.mark.parametrize("password", sorted(_PUBLIC_ADMIN_PASSWORDS))
@pytest.mark.parametrize(
    "over",
    [
        {"APP_BASE_URL": "http://localhost:5173", "SPA_MODE": "dev"},
        {"SPA_MODE": "prod"},
        {"APP_BASE_URL": "https://calls.example.org", "TRUSTED_PROXY": "true", "SPA_MODE": "prod"},
    ],
)
def test_every_published_admin_password_is_refused_in_every_mode(monkeypatch, over, password):
    _settings_env(monkeypatch, **over)
    Settings()  # control: this environment is valid without the password
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", password)
    with pytest.raises(ValueError, match="public example"):
        Settings()


def test_an_admin_password_of_your_own_is_still_accepted(monkeypatch):
    _settings_env(monkeypatch, INITIAL_ADMIN_PASSWORD="a-password-nobody-published")
    assert Settings().initial_admin_password == "a-password-nobody-published"


# --- behind a proxy or not: an https deployment has to say ------------------
#
# With TRUSTED_PROXY unset behind a TLS proxy, every visitor arrives from the
# proxy's address, so they share ONE login and public-board rate-limit bucket
# and 30 requests lock everybody out. That used to be a log line.


def test_https_requires_an_explicit_trusted_proxy(monkeypatch):
    _settings_env(monkeypatch, APP_BASE_URL="https://calls.example.org")
    with pytest.raises(ValueError, match="TRUSTED_PROXY"):
        Settings()


@pytest.mark.parametrize(("raw", "parsed"), [("true", True), ("false", False)])
def test_https_accepts_either_explicit_answer(monkeypatch, raw, parsed):
    _settings_env(monkeypatch, APP_BASE_URL="https://calls.example.org", TRUSTED_PROXY=raw)
    assert Settings().trusted_proxy is parsed


def test_plain_http_does_not_ask_about_the_proxy(monkeypatch):
    _settings_env(monkeypatch, SPA_MODE="prod")
    assert Settings().trusted_proxy is False


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "https://calls.example.org"])
@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_trusted_proxy_is_refused_and_named(monkeypatch, base_url, raw):
    """A blank is not an answer, in any mode: it is refused where it is set,
    by name. (Reading it as "unset" was tried and dropped: a blank in the
    process environment then silently overrode a `true` in the .env file.)"""
    _settings_env(monkeypatch, APP_BASE_URL=base_url, TRUSTED_PROXY=raw)
    with pytest.raises(ValueError, match="TRUSTED_PROXY"):
        Settings()


def test_a_blank_in_the_environment_cannot_hide_a_true_in_the_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "dotenv"
    env_file.write_text("TRUSTED_PROXY=true\n")
    _settings_env(monkeypatch, SPA_MODE="prod", TRUSTED_PROXY="")
    with pytest.raises(ValueError, match="TRUSTED_PROXY"):
        Settings(_env_file=str(env_file))
    monkeypatch.delenv("TRUSTED_PROXY")
    assert Settings(_env_file=str(env_file)).trusted_proxy is True
