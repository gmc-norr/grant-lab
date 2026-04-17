from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class AdapterError(Exception):
    """Adapter could not complete a fetch — orchestrator should treat as a
    full-run failure (do NOT advance last_fetched_at, increment failure count)."""


def is_safe_url(url: str) -> bool:
    """Return True iff the URL uses an http(s) scheme.

    Adapters use this to drop records whose URL would render as a
    `javascript:` (or `data:`, etc.) link in the dashboard — those would
    execute on click as stored XSS. Doing the check at ingest time is
    defense-in-depth: the dashboard template is also Jinja-autoescaped,
    but autoescape doesn't strip dangerous URL schemes from `href=...`.
    """
    lower = url.lower()
    return lower.startswith(("http://", "https://"))


@dataclass(slots=True)
class NormalizedCall:
    """Source-agnostic shape that the ingestion orchestrator persists.

    Adapters MUST normalize timestamps to UTC. Budget values pass through
    in the source's native unit (typically major: euros, kronor) — the
    dashboard renders them with thousand-separators, no normalization.
    `external_id` is the source's own stable identifier — uniqueness is
    per-source, enforced by the DB.
    """

    external_id: str
    url: str
    title: str
    summary: str | None = None
    full_text: str | None = None
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    programme: str | None = None
    keywords: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """Each source is one adapter. Implementations live in `sources/<slug>.py`.

    `fetch_new_calls(since)` must yield NormalizedCalls — generator, not list,
    so the orchestrator can stream-upsert without holding everything in memory.
    `since` is the DB's `sources.last_fetched_at`; pass `None`-or-very-old date
    for first-ever fetch.
    """

    slug: str

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]: ...
