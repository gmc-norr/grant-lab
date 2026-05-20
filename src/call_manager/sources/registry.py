"""Source-adapter registry — single source of truth for which sources exist.

Adding a new source requires editing this file (one new SOURCES entry) and
writing the adapter file. The scheduler iterates SOURCES to register cron
jobs; the ingestion CLI iterates it for `list-sources` and `_build_adapter`.

Each adapter still constructs its own object from a Settings-shaped dependency
via the `build` callable — this keeps the registry decoupled from how
adapters are configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from call_manager.config import Settings
from call_manager.sources.eu_portal import EUPortalAdapter
from call_manager.sources.types import SourceAdapter
from call_manager.sources.vinnova import VinnovaAdapter


@dataclass(frozen=True)
class SourceConfig:
    """One entry per registered funding source."""

    slug: str
    cron_hour: int
    cron_minute: int
    cron_name: str  # APScheduler job name (human-readable)
    build: Callable[[Settings], SourceAdapter]


SOURCES: list[SourceConfig] = [
    SourceConfig(
        slug="eu_portal",
        cron_hour=3,
        cron_minute=0,
        cron_name="Ingest EU Funding & Tenders Portal",
        build=lambda s: EUPortalAdapter(base_url=s.eu_portal_base_url),
    ),
    SourceConfig(
        slug="vinnova",
        cron_hour=3,
        cron_minute=15,
        cron_name="Ingest Vinnova",
        build=lambda s: VinnovaAdapter(base_url=s.vinnova_base_url),
    ),
]


def build_adapter(slug: str, settings: Settings) -> SourceAdapter | None:
    """Construct the adapter for `slug`, or None if not registered."""
    for cfg in SOURCES:
        if cfg.slug == slug:
            return cfg.build(settings)
    return None


def known_slugs() -> list[str]:
    """Return all registered source slugs in their declared order."""
    return [cfg.slug for cfg in SOURCES]
