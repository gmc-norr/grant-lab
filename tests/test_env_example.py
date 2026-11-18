"""`.env.example` is generated from Settings and must stay that way.

Review 2026-09-02 §2.6e: the hand-written example promised an admin-password
reset the code refuses to do, a verbatim copy yielded SPA_MODE=disabled (no UI),
every digest link pointing at localhost, drafting on with no key, and a model id
the code no longer names; 57 Settings aliases had no line. The only test touching
the file asserted two keys were present. Nothing loaded it.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from grant_lab.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = REPO / ".env.example"
SCRIPT = REPO / "scripts/gen_env_example.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_env_example", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_example_matches_the_settings_fields():
    """Regenerate and diff. Failing here means config.py moved and the example did not."""
    assert EXAMPLE.exists(), f"{EXAMPLE} is missing — run: uv run python {SCRIPT.name}"
    assert EXAMPLE.read_text() == _generator().render(), (
        ".env.example is stale. Regenerate it in the same commit that changed config.py:\n"
        "    uv run python scripts/gen_env_example.py"
    )


def test_every_setting_has_a_line():
    text = EXAMPLE.read_text()
    missing = [
        f.alias
        for f in Settings.model_fields.values()
        if f.alias and f"\n{f.alias}=" not in text and f"\n# {f.alias}=" not in text
    ]
    assert missing == [], f"settings with no line in .env.example: {missing}"


@pytest.fixture
def _clean_env(monkeypatch):
    # The process may carry real values (a developer shell, CI); the file must
    # be judged on its own.
    for f in Settings.model_fields.values():
        if f.alias:
            monkeypatch.delenv(f.alias, raising=False)


def test_a_verbatim_copy_is_a_serving_production_configuration(_clean_env):
    """Load the example through the app's own loader, supplying only the three
    values it deliberately leaves blank, and check the day-one outcomes the
    review measured wrong."""
    s = Settings(
        _env_file=str(EXAMPLE),
        SESSION_SECRET="an-example-only-secret-that-is-long-enough-xxxxxxxx",
        INITIAL_ADMIN_EMAIL="admin@umu.se",
        TRUSTED_PROXY="true",
    )
    assert s.spa_mode == "prod", "the example must serve a UI"
    assert "localhost" not in s.admin_dashboard_url, "every digest link is built from this"
    assert not s.anthropic_api_key  # blank in the example: hosted features stay off
    assert s.remote_rank_model == Settings.model_fields["remote_rank_model"].default, (
        "the example must not pin a model id the code no longer names"
    )
    assert s.log_level == "INFO"
    assert s.app_base_url.startswith("https://")


def test_a_verbatim_copy_will_not_start_until_the_proxy_question_is_answered(_clean_env):
    """The example is an https deployment. Shipping TRUSTED_PROXY=false in it
    answered the question for the operator, wrongly for anyone behind a TLS
    proxy: every visitor then shares one rate-limit bucket (review 2026-11-18)."""
    with pytest.raises(ValueError, match="TRUSTED_PROXY must be true or false"):
        Settings(
            _env_file=str(EXAMPLE),
            SESSION_SECRET="an-example-only-secret-that-is-long-enough-xxxxxxxx",
            INITIAL_ADMIN_EMAIL="admin@umu.se",
        )


def test_the_admin_password_prose_matches_the_code():
    """admin_bootstrap never overwrites an existing password hash; the old
    example said the opposite ("(re)set on each startup")."""
    text = EXAMPLE.read_text()
    block = text[text.index("# INITIAL_ADMIN_PASSWORD") :].split("\n\n", 1)[0]
    assert "never overwrites" in block
    assert "on each startup" not in block


def test_the_generator_never_imports_the_running_app():
    src = SCRIPT.read_text()
    assert "grant_lab.main" not in src
