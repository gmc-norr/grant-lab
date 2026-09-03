"""Seed the dev database with one realistic profile + a varied set of calls.

Idempotent: re-running won't duplicate rows. Skips calls already present
(unique (source_id, external_id)) and skips the profile if its name
already exists active. Existing rankings are left alone.

Usage:
    set -a && source .env && set +a
    uv run python scripts/seed_dev_data.py

What it produces (so you can verify in the UI):
- 1 profile: "Digital Pathology & Imaging AI" (group, you as owner).
- 8 calls across vinnova + eu_portal, varied to exercise each gate:
  * 3 should rank well (clear topic + budget + TRL + deadline overlap)
  * 1 must-include keyword match that bypasses a tight deadline
  * 1 timeline-FAIL (deadline <14 days out, no must-include match)
  * 1 budget-FAIL (1B SEK call vs 5M SEK profile cap)
  * 1 TRL-FAIL (TRL 8-9 industrial readiness, profile maxes at 7)
  * 1 topic-dissimilar (industrial materials, off-topic)
- A RankingJob enqueued for every (call × profile) pair.
- Embeddings are NOT computed here — the daily refresh tick or the
  embeddings CLI populates them. Until then the embedding gate is
  UNKNOWN and the LLM runs as the safe fallback, which is what you want
  for verifying the GUI surfaces results promptly.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

# Make `src/` importable when running this file directly (no install needed).
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, os.pardir, "src"))

from call_manager.config import Settings  # noqa: E402
from call_manager.db.models import (  # noqa: E402
    Call,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
)
from call_manager.db.session import make_engine, make_session_factory  # noqa: E402
from call_manager.ranking.queue import enqueue_for_pair  # noqa: E402

_NOW = datetime.now(UTC)


_PROFILE = {
    "name": "Digital Pathology & Imaging AI",
    "kind": ProfileKind.ORGANIZATIONAL_UNIT,
    "level": "group",
    "description": (
        "Clinical research group at the hospital developing and validating "
        "AI/ML for digital pathology, radiology, and federated learning across "
        "Nordic medical centers. Focus: cancer diagnostics from whole-slide "
        "images and multi-site model training that preserves data sovereignty. "
        "Active collaborations with Umeå University, Region Västerbotten, and regional pathology "
        "labs. Funded grants include translational AI for tumor classification."
    ),
    "structured": {
        "areas": [
            "digital pathology",
            "medical imaging AI",
            "federated learning",
            "cancer diagnostics",
        ],
        "budget_min": 500_000,
        "budget_max": 5_000_000,
        "trl_min": 3,
        "trl_max": 7,
        "must_include": ["pathology", "cancer", "AI"],
        "hard_exclusions": ["animal studies", "veterinary"],
    },
    "budget_currency": "SEK",
}

_CALLS = [
    # ----- vinnova, should rank well -----
    {
        "source": "vinnova",
        "external_id": "SEED-V-001",
        "url": "https://www.vinnova.se/seed/dev-only-001",
        "title": "AI for early cancer detection from digital pathology slides",
        "summary": (
            "Vinnova invites proposals for projects developing and validating "
            "AI/ML methods for tumor identification and grading from "
            "whole-slide pathology images. Multi-site Swedish consortia "
            "preferred. Funding intended for TRL 3-6 work."
        ),
        "full_text": (
            "Sweden's pathology workload is growing faster than its workforce. "
            "Vinnova seeks projects that bring validated AI decision support "
            "into clinical pathology workflows. Scope: model development, "
            "prospective validation, regulatory pathway prep. Budget per "
            "project up to 4 MSEK across 24 months. Co-funding from clinical "
            "partners required."
        ),
        "deadline": _NOW + timedelta(days=60),
        "published_at": _NOW - timedelta(days=3),
        "budget_min": 1_000_000,
        "budget_max": 4_000_000,
        "currency": "SEK",
        "programme": "Vinnova — Tillämpad AI i vården",
        "keywords": ["AI", "pathology", "cancer", "digital"],
        "raw": {"trl_min": 3, "trl_max": 6},
    },
    {
        "source": "eu_portal",
        "external_id": "SEED-E-001",
        "url": "https://ec.europa.eu/seed/dev-only-001",
        "title": "HORIZON: Federated AI for cross-border cancer diagnostics",
        "summary": (
            "Horizon Europe call supporting federated learning pilots across "
            "European cancer centers to train diagnostic AI without moving "
            "patient data. Pathology and radiology consortia in scope."
        ),
        "full_text": (
            "The European Health Data Space goal of usable, privacy-preserving "
            "AI requires federated training infrastructure that hospitals can "
            "actually operate. This call funds reference implementations, "
            "evaluation against centrally-trained baselines, and clinical "
            "deployment studies. TRL 4-7. Budget 2-4 MEUR per project, but "
            "applicants from Sweden may request SEK-denominated payment via "
            "their national funding body — quoted ranges below are in SEK."
        ),
        "deadline": _NOW + timedelta(days=90),
        "published_at": _NOW - timedelta(days=7),
        "budget_min": 2_000_000,
        "budget_max": 5_000_000,
        "currency": "SEK",
        "programme": "Horizon Europe — Health Cluster",
        "keywords": ["AI", "federated learning", "cancer", "imaging"],
        "raw": {"trl_min": 4, "trl_max": 7},
    },
    {
        "source": "vinnova",
        "external_id": "SEED-V-002",
        "url": "https://www.vinnova.se/seed/dev-only-002",
        "title": "Klinisk validering av AI för digital patologi",
        "summary": (
            "Vinnova söker projekt som genomför klinisk validering av "
            "AI-modeller för tumördiagnostik i digital patologi. Multi-center "
            "studier prioriteras."
        ),
        "full_text": (
            "Hospital-led prospective validation of pathology AI is the "
            "bottleneck between academic models and clinical deployment. This "
            "call funds 12-18 month validation studies with TRL 5-7 entry "
            "criteria. Single-site applications discouraged unless paired "
            "with an existing multi-site cohort."
        ),
        "deadline": _NOW + timedelta(days=45),
        "published_at": _NOW - timedelta(days=2),
        "budget_min": 800_000,
        "budget_max": 3_000_000,
        "currency": "SEK",
        "programme": "Vinnova — Klinisk innovation",
        "keywords": ["AI", "pathology", "validation", "cancer"],
        "raw": {"trl_min": 5, "trl_max": 7},
    },
    # ----- must-include keyword bypass on a tight deadline -----
    {
        "source": "eu_portal",
        "external_id": "SEED-E-002",
        "url": "https://ec.europa.eu/seed/dev-only-002",
        "title": "Rapid response: AI tools for pathology training datasets",
        "summary": (
            "Short-fuse call: small grants for annotated pathology datasets "
            "supporting AI model development. Deadline in 10 days."
        ),
        "full_text": (
            "Recognising that good clinical AI starts with good labels, this "
            "rapid-response action funds dataset creation and curation. "
            "Pathology and radiology focus. Deadline is tight; must-include "
            "matching on 'pathology' bypasses the timeline gate so this "
            "still surfaces in your digest."
        ),
        "deadline": _NOW + timedelta(days=10),
        "published_at": _NOW - timedelta(days=1),
        "budget_min": 200_000,
        "budget_max": 1_500_000,
        "currency": "SEK",
        "programme": "Horizon — Health Cluster rapid response",
        "keywords": ["pathology", "AI", "datasets"],
        "raw": {"trl_min": 2, "trl_max": 5},
    },
    # ----- timeline FAIL (no must_include match) -----
    {
        "source": "eu_portal",
        "external_id": "SEED-E-003",
        "url": "https://ec.europa.eu/seed/dev-only-003",
        "title": "Quantum sensing for environmental monitoring",
        "summary": (
            "Horizon Europe quantum technologies call. Deadline in 8 days. "
            "Out of scope for medical AI groups but illustrates the timeline "
            "gate."
        ),
        "deadline": _NOW + timedelta(days=8),
        "published_at": _NOW - timedelta(days=2),
        "budget_min": 1_000_000,
        "budget_max": 3_000_000,
        "currency": "EUR",
        "programme": "Horizon — Digital Cluster",
        "keywords": ["quantum", "sensing"],
        "raw": {"trl_min": 4, "trl_max": 6},
    },
    # ----- budget FAIL (way above profile cap) -----
    {
        "source": "vinnova",
        "external_id": "SEED-V-003",
        "url": "https://www.vinnova.se/seed/dev-only-003",
        "title": "Strategisk forskningsinfrastruktur för AI-modeller",
        "summary": (
            "Storskaligt infrastrukturanslag — 500 MSEK till nationella "
            "AI-resurser. Långt över en forskargrupps budgetspann."
        ),
        "deadline": _NOW + timedelta(days=120),
        "published_at": _NOW - timedelta(days=5),
        "budget_min": 100_000_000,
        "budget_max": 500_000_000,
        "currency": "SEK",
        "programme": "Vinnova — Strategisk infrastruktur",
        "keywords": ["AI", "infrastructure"],
        "raw": {"trl_min": 6, "trl_max": 9},
    },
    # ----- TRL FAIL (profile maxes at 7, call wants 8-9) -----
    {
        "source": "vinnova",
        "external_id": "SEED-V-004",
        "url": "https://www.vinnova.se/seed/dev-only-004",
        "title": "Marknadsintroduktion av AI-diagnostik i primärvården",
        "summary": (
            "Finansiering för marknadsförberedelse och CE-märkning av "
            "befintliga AI-diagnostikprodukter. Förväntar sig TRL 8-9 vid "
            "ansökan."
        ),
        "deadline": _NOW + timedelta(days=70),
        "published_at": _NOW - timedelta(days=4),
        "budget_min": 1_000_000,
        "budget_max": 5_000_000,
        "currency": "SEK",
        "programme": "Vinnova — Marknadsintroduktion",
        "keywords": ["AI", "diagnostics", "market"],
        "raw": {"trl_min": 8, "trl_max": 9},
    },
    # ----- topic-dissimilar (industrial materials, expect FAIL on embedding) -----
    {
        "source": "eu_portal",
        "external_id": "SEED-E-004",
        "url": "https://ec.europa.eu/seed/dev-only-004",
        "title": "Recyclable composites for the construction industry",
        "summary": (
            "Horizon Europe call for circular-economy construction materials. "
            "No medical or AI scope."
        ),
        "deadline": _NOW + timedelta(days=80),
        "published_at": _NOW - timedelta(days=6),
        "budget_min": 1_000_000,
        "budget_max": 4_000_000,
        "currency": "EUR",
        "programme": "Horizon — Industrial Cluster",
        "keywords": ["materials", "construction"],
        "raw": {"trl_min": 5, "trl_max": 7},
    },
]


def main() -> int:
    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    inserted_calls = 0
    enqueued_jobs = 0

    with session_factory() as s:
        # Owner = the first admin user (you, since INITIAL_ADMIN_EMAIL was
        # bootstrapped). We don't require a specific email here so this script
        # works for whatever admin you have configured.
        owner = s.execute(
            select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1)
        ).scalar_one_or_none()
        if owner is None:
            print(
                "no admin user found — start the app once first so "
                "INITIAL_ADMIN_EMAIL bootstraps an admin row.",
                file=sys.stderr,
            )
            return 1
        owner_id = owner.id
        print(f"owner: {owner.email} (id={owner_id})")

        profile = s.execute(
            select(Profile).where(Profile.name == _PROFILE["name"], Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if profile is None:
            profile = Profile(
                name=_PROFILE["name"],
                kind=_PROFILE["kind"],
                level=_PROFILE["level"],
                description=_PROFILE["description"],
                structured=_PROFILE["structured"],
                budget_currency=_PROFILE["budget_currency"],
                created_by=owner_id,
            )
            s.add(profile)
            s.commit()
            s.refresh(profile)
            print(f"profile created: {profile.name} (id={profile.id})")
        else:
            print(f"profile already exists: {profile.name} (id={profile.id})")
        profile_id = profile.id

        # Subscribe the owner so the weekly digest would include this profile
        # (we leave digest_dry_run=1 in .env so nothing actually sends).
        sub_exists = s.execute(
            select(Subscription).where(
                Subscription.user_id == owner_id,
                Subscription.profile_id == profile_id,
            )
        ).scalar_one_or_none()
        if sub_exists is None:
            s.add(Subscription(user_id=owner_id, profile_id=profile_id))
            s.commit()
            print("subscription created (owner ↔ profile)")

        # Map source slug → id once.
        source_ids = {row.slug: row.id for row in s.execute(select(Source)).scalars().all()}

        for c in _CALLS:
            src_id = source_ids.get(c["source"])
            if src_id is None:
                print(f"  skip {c['external_id']}: unknown source {c['source']!r}")
                continue
            exists = s.execute(
                select(Call.id).where(
                    Call.source_id == src_id, Call.external_id == c["external_id"]
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            call = Call(
                source_id=src_id,
                external_id=c["external_id"],
                url=c["url"],
                title=c["title"],
                summary=c.get("summary"),
                full_text=c.get("full_text"),
                deadline=c.get("deadline"),
                budget_min=c.get("budget_min"),
                budget_max=c.get("budget_max"),
                currency=c.get("currency"),
                programme=c.get("programme"),
                keywords=c.get("keywords") or [],
                published_at=c.get("published_at"),
                raw=c.get("raw") or {},
            )
            s.add(call)
            s.flush()
            enqueue_for_pair(s, call_id=call.id, profile_id=profile_id)
            inserted_calls += 1
            enqueued_jobs += 1
            print(f"  + {c['source']}/{c['external_id']}  {c['title'][:60]!r}")
        s.commit()

    print()
    print(f"done. inserted {inserted_calls} calls, enqueued {enqueued_jobs} ranking jobs.")
    print(
        "the rank-drain tick runs every 60s; refresh the dashboard in a minute "
        "to see scores fill in. embeddings will populate at the 03:30 daily "
        "tick — until then the embedding gate is UNKNOWN and the LLM runs as "
        "the safe fallback."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
