"""Single source of truth for profile structured-field vocabularies, caps, and
parsing. Framework-agnostic: raises FieldValidationError (with a status hint)
which the route layer maps to HTTPException. Shared by the create/edit routes,
the bootstrap response parser, and the ranking prompt."""

from __future__ import annotations

# Allow-lists (enum-style fields). frozenset so they can't be mutated at runtime.
APPLICANT_TYPES = frozenset(
    {"solo_pi", "consortium_coordinator", "consortium_partner", "institution"}
)
CAREER_STAGES = frozenset({"phd_student", "postdoc", "junior_pi", "senior_pi", "group_leader"})
ORG_TYPES = frozenset(
    {"research_organisation", "university_hospital", "public_body", "sme", "other"}
)
FUNDING_INSTRUMENTS = frozenset(
    {"project_grant", "fellowship", "mobility", "infrastructure", "programme"}
)
LEVELS = frozenset({"lab", "group", "department", "clinic", "institution", "other"})

# Caps (see spec "Validation caps").
MAX_LIST_ENTRIES = 100
MAX_TAG_LEN = 80
MAX_SHORT_TEXT = 200
YEARS_SINCE_PHD_MIN = 0
YEARS_SINCE_PHD_MAX = 80


class FieldValidationError(Exception):
    """Validation failure with an HTTP status hint (400 bad value, 413 too long)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _tags(raw, *, field: str) -> list[str]:
    """Split a textarea (one entry per line) into a count/length-checked list.

    Rejects (does not silently truncate) over-count or over-length input — the
    spec requires a 400, not silent data loss."""
    if raw is None:
        return []
    items = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    if len(items) > MAX_LIST_ENTRIES:
        raise FieldValidationError(
            f"{field}: too many items (max {MAX_LIST_ENTRIES})", status_code=400
        )
    for item in items:
        if len(item) > MAX_TAG_LEN:
            raise FieldValidationError(
                f"{field}: item exceeds {MAX_TAG_LEN} chars", status_code=400
            )
    return items


def _enum_list(raw, *, allowed: frozenset[str], field: str) -> list[str]:
    out = _tags(raw, field=field)
    for v in out:
        if v not in allowed:
            raise FieldValidationError(f"{field}: unknown value {v!r}", status_code=400)
    return out


def _enum_scalar(raw, *, allowed: frozenset[str], field: str) -> str | None:
    v = (raw or "").strip()
    if not v:
        return None
    if v not in allowed:
        raise FieldValidationError(f"{field}: unknown value {v!r}", status_code=400)
    return v


def _short_text(raw, *, field: str) -> str | None:
    v = (raw or "").strip()
    if not v:
        return None
    if len(v) > MAX_SHORT_TEXT:
        raise FieldValidationError(f"{field}: exceeds {MAX_SHORT_TEXT} chars", status_code=413)
    return v


def _bounded_int(raw, *, low: int, high: int, field: str) -> int | None:
    v = (raw or "").strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        return None  # optional numeric box: "n/a" → None (matches existing UX)
    if n < low or n > high:
        raise FieldValidationError(f"{field}: out of range [{low},{high}]", status_code=400)
    return n


def parse_structured(*, kind: str, raw: dict) -> dict:
    """Parse + validate the new + existing structured inputs for a profile.

    `raw` is a flat dict of string form values (textarea/inputs). Returns the
    namespaced `structured` dict. Fields not relevant to `kind` are dropped.
    Existing rubric fields (areas/budget/trl/must_include/hard_exclusions) are
    parsed by the caller and merged in — this function owns only the new
    namespaces plus `areas` (shared). Raises FieldValidationError on bad input.
    """
    out: dict = {"areas": _tags(raw.get("areas"), field="areas")}

    eligibility = {
        "applicant_types": _enum_list(
            raw.get("applicant_types"), allowed=APPLICANT_TYPES, field="applicant_types"
        ),
        "role": _short_text(raw.get("role"), field="role"),
        "years_since_phd": _bounded_int(
            raw.get("years_since_phd"),
            low=YEARS_SINCE_PHD_MIN,
            high=YEARS_SINCE_PHD_MAX,
            field="years_since_phd",
        ),
    }
    if kind == "individual":
        eligibility["career_stage"] = _enum_scalar(
            raw.get("career_stage"), allowed=CAREER_STAGES, field="career_stage"
        )
    out["eligibility"] = eligibility

    if kind == "organizational_unit":
        out["org"] = {
            "org_type": _enum_scalar(raw.get("org_type"), allowed=ORG_TYPES, field="org_type"),
            "capabilities": _tags(raw.get("capabilities"), field="capabilities"),
            "patient_populations": _tags(
                raw.get("patient_populations"), field="patient_populations"
            ),
            "scale": _short_text(raw.get("scale"), field="scale"),
        }

    out["funding"] = {
        "instruments": _enum_list(
            raw.get("funding_instruments"), allowed=FUNDING_INSTRUMENTS, field="funding_instruments"
        ),
        "funder_exclusions": _tags(raw.get("funder_exclusions"), field="funder_exclusions"),
    }
    out["geo"] = {
        "country": _short_text(raw.get("country"), field="country"),
        "host_institution": _short_text(raw.get("host_institution"), field="host_institution"),
    }
    return out


def sanitize_structured(*, kind: str, structured: dict) -> dict:
    """Coerce an LLM-suggested (nested) structured dict to a valid shape.

    Unlike parse_structured (strict — raises on bad USER form input), this is
    LENIENT: it silently drops unknown enum values, over-length items, and
    over-count lists, because the input is machine-generated bootstrap output,
    not a user form. Used on create so LLM suggestions are never persisted
    unvalidated. Returns only the namespaces (caller keeps description/areas/
    rubric from the draft)."""
    structured = structured or {}

    def _clean_tags(v):
        if not isinstance(v, list):
            return []
        out = [str(x).strip() for x in v if str(x).strip()][:MAX_LIST_ENTRIES]
        return [x for x in out if len(x) <= MAX_TAG_LEN]

    def _clean_enum_list(v, allowed):
        return [x for x in _clean_tags(v) if x in allowed]

    def _clean_enum(v, allowed):
        v = str(v).strip() if v is not None else ""
        return v if v in allowed else None

    def _clean_text(v):
        v = str(v).strip() if v is not None else ""
        return v[:MAX_SHORT_TEXT] or None

    def _clean_years(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return v if YEARS_SINCE_PHD_MIN <= v <= YEARS_SINCE_PHD_MAX else None

    elig_in = structured.get("eligibility") or {}
    org_in = structured.get("org") or {}
    fund_in = structured.get("funding") or {}
    geo_in = structured.get("geo") or {}

    eligibility = {
        "applicant_types": _clean_enum_list(elig_in.get("applicant_types"), APPLICANT_TYPES),
        "role": _clean_text(elig_in.get("role")),
        "years_since_phd": _clean_years(elig_in.get("years_since_phd")),
    }
    if kind == "individual":
        eligibility["career_stage"] = _clean_enum(elig_in.get("career_stage"), CAREER_STAGES)

    out: dict = {"eligibility": eligibility}
    if kind == "organizational_unit":
        out["org"] = {
            "org_type": _clean_enum(org_in.get("org_type"), ORG_TYPES),
            "capabilities": _clean_tags(org_in.get("capabilities")),
            "patient_populations": _clean_tags(org_in.get("patient_populations")),
            "scale": _clean_text(org_in.get("scale")),
        }
    out["funding"] = {
        "instruments": _clean_enum_list(fund_in.get("instruments"), FUNDING_INSTRUMENTS),
        "funder_exclusions": _clean_tags(fund_in.get("funder_exclusions")),
    }
    out["geo"] = {
        "country": _clean_text(geo_in.get("country")),
        "host_institution": _clean_text(geo_in.get("host_institution")),
    }
    return out
