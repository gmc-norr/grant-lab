from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from call_monitor.digest.assemble import DigestPayload

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _days_until(deadline: datetime | None) -> int | None:
    if deadline is None:
        return None
    delta = deadline - datetime.now(UTC)
    return max(0, delta.days)


def _env(autoescape_html: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        # Use unconditional True/False rather than select_autoescape():
        # select_autoescape(["html"]) checks the template filename extension —
        # "digest.html.jinja" ends in ".jinja", not ".html", so autoescape
        # would NOT trigger, leaving user content unescaped.
        autoescape=bool(autoescape_html),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["days_until"] = _days_until
    return env


def render_html(payload: DigestPayload, *, dashboard_url: str) -> str:
    env = _env(autoescape_html=True)
    return env.get_template("digest.html.jinja").render(
        payload=payload, dashboard_url=dashboard_url
    )


def render_text(payload: DigestPayload, *, dashboard_url: str) -> str:
    env = _env(autoescape_html=False)
    return env.get_template("digest.txt.jinja").render(
        payload=payload, dashboard_url=dashboard_url
    )


def build_subject(payload: DigestPayload) -> str:
    profile_names = ", ".join(sec.profile_name for sec in payload.sections)
    plural = "s" if payload.total_calls != 1 else ""
    return f"{payload.total_calls} new funding call{plural} for {profile_names}"
