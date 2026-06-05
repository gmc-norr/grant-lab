from __future__ import annotations

import re as _re
from dataclasses import dataclass, field

_ORCID_RE = _re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


@dataclass
class Member:
    """One research-group member the bootstrap should pull pubs for.

    Either `orcid` OR `(name, affiliation)` must be set; orcid takes
    precedence. Affiliation matches via PubMed's [ad] field and is fuzzy
    by nature — user reviews the draft, so some imprecision is OK.
    """

    orcid: str | None = None
    name: str | None = None
    affiliation: str | None = None

    def __post_init__(self) -> None:
        if self.orcid is not None and not _ORCID_RE.match(self.orcid):
            raise ValueError(f"invalid ORCID format: {self.orcid!r}")
        if not self.orcid and not (self.name and self.affiliation):
            raise ValueError("Member requires either `orcid` or both `name` and `affiliation`")


@dataclass
class Publication:
    pmid: str
    title: str
    year: int | None = None
    authors: list[str] = field(default_factory=list)


@dataclass
class SeedInput:
    name: str  # profile name, user-visible
    kind: str  # 'organizational_unit' | 'individual'
    level: str | None = None  # organizational unit level; None for individuals
    members: list[Member] = field(default_factory=list)
    pasted_grants: str = ""  # free text — titles + abstracts pasted in by user
    mission_text: str = ""  # org units: mission/strategy/capabilities free text


@dataclass
class DraftProfile:
    """What bootstrap returns. The web layer persists it as a Profile row."""

    description: str
    structured: dict  # matches Profile.structured shape
    publications_fetched: int
    publications_skipped: int  # members whose PubMed search returned zero
