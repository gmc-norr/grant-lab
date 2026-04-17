from __future__ import annotations

import json

from call_monitor.profiles.seed import DraftProfile, Publication

BOOTSTRAP_PROMPT_VERSION = "v1"


class BootstrapParseError(Exception):
    """LLM returned non-JSON or invalid shape. Caller falls back."""


_SYSTEM_TEMPLATE = """\
You are a research-funding analyst for an academic hospital. Draft a
profile for a research group based on their recent publications and any
past grants pasted below. The profile will be used to score incoming
funding calls for relevance.

PROFILE:
  Name: {name}
  Kind: {kind}

Output a single JSON object with these keys (all optional — omit if
unclear from evidence, don't fabricate):
  {{
    "description": "<1-2 paragraph summary of the group's research focus>",
    "areas":             ["<short tag>", ...],
    "budget_min":        <integer, native currency unit, or null>,
    "budget_max":        <integer, native currency unit, or null>,
    "trl_min":           <int 1-9 or null>,
    "trl_max":           <int 1-9 or null>,
    "must_include":      ["<force-include keyword>", ...],
    "hard_exclusions":   ["<topic the group will not work on>", ...]
  }}
"""

_USER_TEMPLATE = """\
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
    publications: list[Publication],
    pasted_grants: str,
) -> list[dict]:
    system = _SYSTEM_TEMPLATE.format(name=profile_name, kind=kind)
    user = _USER_TEMPLATE.format(
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
