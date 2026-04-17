from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class AdapterError(Exception):
    """Adapter could not complete a fetch — orchestrator should treat as a
    full-run failure (do NOT advance last_fetched_at, increment failure count)."""


@dataclass(slots=True)
class NormalizedCall:
    """Source-agnostic shape that the ingestion orchestrator persists.

    Adapters MUST normalize timestamps to UTC and budgets to integer minor
    units of `currency` (or None if unknown). `external_id` is the source's
    own stable identifier — uniqueness is per-source, enforced by the DB.
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
