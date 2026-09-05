from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from grant_lab.digest.assemble import DigestPayload
from grant_lab.web.filters import display_date, display_dt

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _days_until(deadline: datetime | None, *, now: datetime) -> int | None:
    """Whole days from `now` to `deadline`, floored at 0.

    `now` is threaded in explicitly (not `datetime.now(UTC)`) so the "in N
    days" copy is consistent with the deadline filtering done at assemble time
    and is deterministic / testable. Both args are UTC-aware.
    """
    if deadline is None:
        return None
    delta = deadline - now
    return max(0, delta.days)


def _env(autoescape_html: bool, *, now: datetime) -> Environment:
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
    # Bind `now` into the template global so templates keep calling
    # `days_until(deadline)` with one arg.
    env.globals["days_until"] = lambda deadline: _days_until(deadline, now=now)
    env.filters["display_date"] = display_date
    env.filters["display_dt"] = display_dt
    return env


def render_html(payload: DigestPayload, *, dashboard_url: str, now: datetime | None = None) -> str:
    env = _env(autoescape_html=True, now=now or datetime.now(UTC))
    return env.get_template("digest.html.jinja").render(
        payload=payload, dashboard_url=dashboard_url
    )


def render_text(payload: DigestPayload, *, dashboard_url: str, now: datetime | None = None) -> str:
    env = _env(autoescape_html=False, now=now or datetime.now(UTC))
    return env.get_template("digest.txt.jinja").render(payload=payload, dashboard_url=dashboard_url)


def build_subject(payload: DigestPayload) -> str:
    profile_names = ", ".join(sec.profile_name for sec in payload.sections)
    plural = "s" if payload.total_calls != 1 else ""
    subject = f"{payload.total_calls} new funding call{plural} for {profile_names}"
    # Strip CR/LF so a profile name can't inject extra email headers if the
    # transport ever stops folding the Subject for us (current SMTP path is
    # saved by EmailMessage header-folding; Resend uses JSON). Cheap insurance.
    return subject.replace("\r", " ").replace("\n", " ")
