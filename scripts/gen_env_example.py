"""Generate .env.example from the Settings fields — the way gen_architecture.py
generates the architecture map.

Review 2026-09-02 §2.6e: the hand-written example contradicted the code (it
promised an admin-password reset the code refuses to do), a verbatim copy
produced a non-serving deployment (SPA_MODE=disabled, every digest link pointing
at localhost, drafting on with no key, a model id the code no longer names), and
57 Settings aliases had no line at all. Generating it from `Settings` makes all of
that impossible to drift: every field is listed, the default shown is the code's
default, and the description is the one config.py carries.

    uv run python scripts/gen_env_example.py

`tests/test_env_example.py` fails when the committed file is stale.
"""

from __future__ import annotations

import pathlib
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic_core import PydanticUndefined  # noqa: E402

from call_manager.config import Settings  # noqa: E402

# Keys a fresh deployment must decide on day one. Emitted UNCOMMENTED with the
# value given here, or the code default when the value is None. Everything else
# is emitted commented out, showing the code default.
DAY_ONE: dict[str, str | None] = {
    "DATABASE_URL": "postgresql+psycopg://call_manager:CHANGE_ME@localhost:5432/call_manager",
    "SESSION_SECRET": "",
    "ALLOWED_EMAIL_DOMAINS": "regionvasterbotten.se,umu.se",
    "INITIAL_ADMIN_EMAIL": "",
    "APP_BASE_URL": "https://calls.example.org",
    "ADMIN_DASHBOARD_URL": "https://calls.example.org/",
    "SPA_MODE": "prod",
    "LOG_LEVEL": None,
    "SESSION_MAX_AGE_SECONDS": None,
    "MAX_REQUEST_BODY_BYTES": None,
    "TRUSTED_PROXY": None,
    "TRUSTED_PROXY_HOPS": None,
    "EMAIL_TRANSPORT": None,
    "RESEND_API_KEY": "",
    "SMTP_HOST": "",
    "SMTP_PORT": None,
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "EMAIL_FROM": None,
    "ADMIN_ALERT_EMAIL": "",
    "DIGEST_DRY_RUN": None,
    "STALE_SOURCE_ALERT_DAYS": None,
    "OLLAMA_URL": None,
    "GEMMA_MODEL": None,
    "EMBEDDING_MODEL": None,
    "ANTHROPIC_API_KEY": "",
    "REMOTE_RANK_MODEL": None,
    "DRAFTING_ENABLED": "false",
    "DRAFTING_ADAPTER": None,
    "PROPOSAL_ASSIST_LLM_MODEL": None,
    "DRAFTING_MAX_LIVE_SUBPROCESSES": None,
    "DRAFTING_DB_POOL_SIZE": None,
    "TURN_MAX_SECONDS": None,
    "WORKSPACES_DIR": None,
    "COLLAB_KILL_SWITCH": None,
    "RETENTION_INSPECTION_ENABLED": None,
    "CITATION_LLM_PROVIDER": None,
    "CITATION_LLM_ALLOW_CLOUD": None,
    "REVIEW_DOLLAR_CAP_DEFAULT": None,
    "REVIEW_RUNS_PER_DAY_DEFAULT": None,
}

HEADER = """\
# .env.example — GENERATED from src/call_manager/config.py by
# scripts/gen_env_example.py. Do not hand-edit: regenerate with
#     uv run python scripts/gen_env_example.py
# and tests/test_env_example.py fails while this file is stale.
#
# Copy to .env. Lines that are NOT commented out are the day-one decisions
# (required values, and defaults a real deployment must confirm); commented
# lines show the code default and can be left alone. Every setting the app
# reads is listed, in the order config.py declares it.
"""


def _render_default(field) -> str:
    if field.default_factory is not None:
        value = field.default_factory()
    elif field.default is PydanticUndefined:
        return ""
    else:
        value = field.default
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def render() -> str:
    lines = [HEADER]
    fields = Settings.model_fields
    aliases = {f.alias for f in fields.values() if f.alias}
    unknown = set(DAY_ONE) - aliases
    if unknown:
        raise SystemExit(f"DAY_ONE names settings that do not exist: {sorted(unknown)}")
    for name, field in fields.items():
        alias = field.alias or name.upper()
        required = field.is_required()
        default = _render_default(field)
        head = f"# {alias} (REQUIRED)" if required else f"# {alias} (default: {default or 'empty'})"
        lines.append("")
        lines.append(head)
        for para in (field.description or "").split("\n"):
            for wrapped in textwrap.wrap(para, width=76) or [""]:
                lines.append(f"# {wrapped}".rstrip())
        if alias in DAY_ONE or required:
            value = DAY_ONE.get(alias)
            lines.append(f"{alias}={default if value is None else value}")
        else:
            lines.append(f"# {alias}={default}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    out = ROOT / ".env.example"
    out.write_text(render())
    print(f"wrote {out.relative_to(ROOT)} ({len(Settings.model_fields)} settings)")
