from __future__ import annotations

import json

from call_manager.profiles.seed import DraftProfile, Publication

BOOTSTRAP_PROMPT_VERSION = "v2"


class BootstrapParseError(Exception):
    """LLM returned non-JSON or invalid shape. Caller falls back."""


_SYSTEM_UNIT = """\
You are a research-funding analyst for an academic hospital. Draft a profile for
an ORGANIZATIONAL UNIT (a {level} — e.g. lab, group, department, clinic, or
institution) from its mission/capabilities text, any member publications, and
pasted past grants. The profile scores incoming funding calls for relevance.

PROFILE:
  Name: {name}
  Kind: organizational_unit
  Level: {level}

Output a single JSON object (all keys optional — omit if unclear, don't fabricate):
  {{
    "description": "<1-2 paragraph summary of what this unit does>",
    "areas": ["<short tag>", ...],
    "budget_min": <int or null>, "budget_max": <int or null>,
    "trl_min": <int 1-9 or null>, "trl_max": <int 1-9 or null>,
    "must_include": ["<keyword>", ...],
    "hard_exclusions": ["<topic the unit won't work on>", ...],
    "org": {{
      "org_type": "research_organisation|university_hospital|public_body|sme|other",
      "capabilities": ["biobank","clinical_trials_unit","imaging_core","registry", ...],
      "patient_populations": ["<tag>", ...],
      "scale": "<free text, e.g. ~400 researchers>"
    }},
    "funding": {{
      "instruments": ["project_grant|fellowship|mobility|infrastructure|programme", ...]
    }}
  }}
"""

_SYSTEM_INDIVIDUAL = """\
You are a research-funding analyst for an academic hospital. Draft a profile for
an INDIVIDUAL researcher from their recent publications and pasted past grants.
The profile scores incoming funding calls for relevance, including eligibility
(career stage matters for many calls).

PROFILE:
  Name: {name}
  Kind: individual

Output a single JSON object (all keys optional — omit if unclear, don't fabricate):
  {{
    "description": "<1-2 paragraph summary of this researcher's focus>",
    "areas": ["<short tag>", ...],
    "budget_min": <int or null>, "budget_max": <int or null>,
    "trl_min": <int 1-9 or null>, "trl_max": <int 1-9 or null>,
    "must_include": ["<keyword>", ...],
    "hard_exclusions": ["<topic>", ...],
    "eligibility": {{
      "career_stage": "phd_student|postdoc|junior_pi|senior_pi|group_leader",
      "applicant_types": ["solo_pi","consortium_partner", ...]
    }},
    "funding": {{
      "instruments": ["project_grant|fellowship|mobility|infrastructure|programme", ...]
    }}
  }}
"""

_USER_TEMPLATE = """\
MISSION / CAPABILITIES:
{mission}

PUBLICATIONS ({n_pubs}):
{pubs_block}

PASTED GRANTS:
{grants}
"""


def _pubs_block(publications: list[Publication]) -> str:
    if not publications:
        return "(no publications fetched)"
    lines = []
    for p in publications[:40]:  # cap; 40 is plenty of signal for a v1 draft
        year = f" ({p.year})" if p.year else ""
        lines.append(f"- [{p.pmid}]{year} {p.title}")
    return "\n".join(lines)


def build_bootstrap_messages(
    *,
    profile_name: str,
    kind: str,
    level: str | None,
    publications: list[Publication],
    pasted_grants: str,
    mission_text: str = "",
) -> list[dict]:
    if kind == "individual":
        system = _SYSTEM_INDIVIDUAL.format(name=profile_name)
    else:
        system = _SYSTEM_UNIT.format(name=profile_name, level=level or "unit")
    user = _USER_TEMPLATE.format(
        mission=mission_text or "(none)",
        n_pubs=len(publications),
        pubs_block=_pubs_block(publications),
        grants=pasted_grants or "(none)",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_bootstrap_response(content: str) -> DraftProfile:
    """Parse Gemma's JSON response into a DraftProfile.

    Fields missing from the response get sensible defaults (None for
    numerics, [] for lists, "" for description) — the user can fill
    them in on the edit screen."""
    try:
        data = json.loads(content)
    except ValueError as e:
        raise BootstrapParseError(f"LLM returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise BootstrapParseError("LLM response is not a JSON object")

    description = str(data.get("description") or "")
    structured = {
        "areas": _str_list(data.get("areas")),
        "budget_min": _int_or_none(data.get("budget_min")),
        "budget_max": _int_or_none(data.get("budget_max")),
        "trl_min": _int_or_none(data.get("trl_min")),
        "trl_max": _int_or_none(data.get("trl_max")),
        "must_include": _str_list(data.get("must_include")),
        "hard_exclusions": _str_list(data.get("hard_exclusions")),
    }
    # Carry through the new namespaces if the model supplied them. These are
    # SUGGESTIONS shown on the edit screen, re-validated on save via
    # profiles.fields.parse_structured — so a loose shape here is acceptable.
    for ns in ("eligibility", "org", "funding", "geo"):
        if isinstance(data.get(ns), dict):
            structured[ns] = data[ns]
    return DraftProfile(
        description=description,
        structured=structured,
        publications_fetched=0,  # caller fills in
        publications_skipped=0,
    )


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v]


def _int_or_none(value) -> int | None:
    if isinstance(value, bool):
        return None  # Python quirk: bool subclasses int; reject bools
    if isinstance(value, int):
        return value
    return None
