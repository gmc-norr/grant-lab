from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import httpx
from lxml import html as lxml_html

from grant_lab.sources.types import (
    AdapterError,
    NeedsBody,
    NormalizedCall,
    is_safe_url,
    pinned_transport_for,
    request_with_retry,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 2026-09-08: the v3.0 endpoint is GONE
# ---------------------------------------------------------------------------
# This adapter used to POST to
#   https://ec.europa.eu/info/funding-tenders/opportunities/api/v3.0/notices/search
# That path now returns 404 (the portal home page itself still returns 200, so
# the site is up — only the API moved). The source had therefore NEVER
# succeeded: `sources.last_fetched_at` was NULL with 14 consecutive failures.
#
# The portal's own SPA now reads from the Commission's shared SEDIA search API:
#   POST https://api.tech.ec.europa.eu/search-api/prod/rest/search?apiKey=SEDIA
#
# Three things about that API are easy to get wrong, all verified live:
#
# 1. `apiKey=SEDIA` is NOT a credential. It is the database selector — the
#    portal's public JavaScript sends the same literal string. There is no
#    secret to configure and nothing to rotate.
#
# 2. The `query` and `languages` multipart parts MUST carry
#    `Content-Type: application/json`. Sent untyped, the API answers
#    HTTP 500 `{"type":"throwable","message":"An internal error occurred"}` —
#    a server error that looks like an outage but is actually a request the
#    server refused to parse. httpx expresses the typing as the 3-tuple
#    `(None, value, "application/json")`; the leading None suppresses the
#    filename parameter, matching curl's `-F 'query=...;type=application/json'`.
#
# 3. Every useful field lives under `metadata` as a SINGLE-ELEMENT LIST, and
#    the top-level `title` is None. Reading `item["title"]` — which the old
#    parser did — yields nothing for every record.
# ---------------------------------------------------------------------------

_SEARCH_PATH = "/search-api/prod/rest/search"

# The database selector, not a secret. See note 1 above.
_API_KEY = "SEDIA"

# Status codes, decoded by cross-checking `startDate` against the wall clock on
# a live sample of 100 records: every 31094502 had already started and every
# 31094501 but one had not.
_STATUS_FORTHCOMING = "31094501"
_STATUS_OPEN = "31094502"

# `type` partitions the SEDIA index. Measured live on 2026-09-08 with the
# open+forthcoming status filter applied:
#   type=1  1214 grant topics    <- what this adapter wants
#   type=2    25 EuropeAid procurement tenders (service contracts, no body)
#   type=8   168 legacy "competitive-call" rows, deadlines years in the past
#   type=0  1539 rows keyed by opaque UUID
# Only type=1 carries `descriptionByte`; the others parse to a title and a
# stale date. Fetching them would add noise to the ranker, not coverage.
_TYPE_GRANT_TOPIC = "1"

# The server silently clamps pageSize to 100 (asking for 150 returns 100), so
# state the real ceiling rather than a wish. _MAX_PAGES is the same defensive
# guard as before: 200 * 100 = 20k records, far beyond the ~1.2k live set, so a
# server that never stops paginating cannot spin us forever.
_PAGE_SIZE = 100
_MAX_PAGES = 200

# Summary length. The API gives no abstract — item-level `summary` is just the
# parent call's title — so we take the opening of the call body verbatim.
_SUMMARY_CHARS = 500

# Tags whose end should become a line break when flattening the description
# HTML. Without this, `text_content()` welds the last word of one bullet onto
# the first word of the next.
_BLOCK_XPATH = "|".join(
    f"//{tag}"
    for tag in (
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "table",
        "section",
    )
)

# Identifier prefix -> readable programme name. `metadata.frameworkProgramme`
# is an opaque numeric code (43108390, 43152860, ...) with no label anywhere in
# the payload, but the prefix of `identifier` tracks it exactly: on the live
# 100-record sample all 89 rows coded 43108390 were the 89 rows whose
# identifier began "HORIZON". Deriving from the prefix needs no code table to
# stay in sync, and an unknown programme degrades to its own prefix rather than
# to None.
_PROGRAMME_NAMES = {
    "HORIZON": "Horizon Europe",
    "DIGITAL": "Digital Europe Programme",
    "LIFE": "LIFE Programme",
    "CREA": "Creative Europe",
    "ERASMUS": "Erasmus+",
    "CEF": "Connecting Europe Facility",
    "EU4H": "EU4Health",
    "ISF": "Internal Security Fund",
    "AMIF": "Asylum, Migration and Integration Fund",
    "SMP": "Single Market Programme",
    "JUST": "Justice Programme",
    "CERV": "Citizens, Equality, Rights and Values",
    "EDF": "European Defence Fund",
    "EUBA": "EU Budget — pilot projects and preparatory actions",
    "IMREG": "Interregional Innovation Investments",
    "RFCS": "Research Fund for Coal and Steel",
    "UCPM": "Union Civil Protection Mechanism",
}


def _first(metadata: dict, key: str) -> str | None:
    """Read a SEDIA metadata field.

    Every value in `metadata` is a list, almost always of length one. Return
    the first non-empty entry as a string, or None. Tolerates a bare scalar so
    a shape change upstream degrades instead of raising.
    """
    value = metadata.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a SEDIA timestamp into tz-aware UTC.

    SEDIA emits `2027-04-06T00:00:00.000+0000` — a UTC offset with no colon.
    Python 3.12's `fromisoformat` accepts that form directly (verified against
    this interpreter, alongside `+00:00` and `Z`), so no pre-normalisation is
    needed. An earlier revision carried a regex to insert the colon; it was
    removed after a mutation showed the test suite could not tell whether it
    was there.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# EU calls close at 17:00 Brussels time on their deadline date — the standard
# closing time across Horizon Europe and the other SEDIA programmes. SEDIA's
# search API carries only the DATE, encoded as midnight UTC
# (`2027-04-06T00:00:00.000+0000`; the captured fixture has 2/2 in that form).
# Stored unchanged, every EU call closed in the feed at 02:00 Stockholm on its
# last day — the one day a researcher is guaranteed to look. On 2026-10-06, 670
# of 676 stored eu_portal deadlines sat at exactly 00:00 UTC (review H3).
# Migration 0082 moved the stored rows with this same function.
_BRUSSELS = ZoneInfo("Europe/Brussels")
_EU_CLOSING_TIME = time(17, 0)


def brussels_close_if_midnight(value: datetime) -> datetime:
    """A bare SEDIA deadline date, read as the 17:00 Brussels close that day.

    Only an EXACT midnight UTC is a bare date; any other instant states a real
    time and is returned unchanged (as UTC). The zone does the CET/CEST work, so
    the result is 16:00 UTC in winter and 15:00 UTC in summer. Same idea as
    vinnova.py's `end_of_day_if_midnight`, with the EU's closing time.
    """
    utc = value.astimezone(UTC)
    if utc.time() != time.min:
        return utc
    return datetime.combine(utc.date(), _EU_CLOSING_TIME, tzinfo=_BRUSSELS).astimezone(UTC)


def _deadlines(metadata: dict) -> list[datetime]:
    """Every parseable entry of `metadata.deadlineDate`, in payload order.

    NOT a single value. Measured across all 1214 live topics: 1117 carry one
    deadline, 75 carry two, and the rest run up to ten — two-stage calls and
    rolling calls with a deadline per cut-off round. Reading only entry [0]
    (which `_first` would do) reports the FIRST round, so a call whose early
    round has closed but whose later round is still open looks expired. Six
    live topics were in exactly that state, e.g. PERI-2026-ANTI-COUNTERFEIT
    with rounds 2026-04-21 and 2026-09-15.

    Each entry is bumped to its 17:00 Brussels close HERE, before
    `_pick_deadline` and the expiry filter see it: a round closing today must
    still be the open one this morning.
    """
    raw = metadata.get("deadlineDate")
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    out = []
    for entry in raw:
        parsed = _parse_iso(str(entry) if entry is not None else None)
        if parsed is not None:
            out.append(brussels_close_if_midnight(parsed))
    return sorted(out)


def _pick_deadline(deadlines: list[datetime], now: datetime) -> datetime | None:
    """The deadline a would-be applicant is actually working towards.

    The earliest round still open; if every round has closed, the last one, so
    the caller can tell "expired" from "no deadline stated" — a call with no
    deadline at all is NOT expired, and must not be filtered out.
    """
    if not deadlines:
        return None
    for deadline in deadlines:
        if deadline > now:
            return deadline
    return deadlines[-1]


def _html_to_text(fragment: str | None) -> str | None:
    """Flatten a SEDIA description fragment to plain text.

    NOT `trafilatura.extract`, which this module already depends on elsewhere:
    trafilatura is tuned for article pages and, measured on a real
    `descriptionByte`, silently DROPPED every `<ul>` bullet — 4115 characters
    out of 5058, and the bullets are where the actual expected outcomes are.
    lxml keeps everything; all it needs is block-level line breaks.
    """
    if not fragment or not fragment.strip():
        return None
    try:
        doc = lxml_html.fromstring(fragment)
        for bad in doc.xpath("//script|//style"):
            parent = bad.getparent()
            if parent is not None:
                parent.remove(bad)
        for element in doc.xpath(_BLOCK_XPATH):
            element.tail = "\n" + (element.tail or "")
        raw = doc.text_content()
    except Exception as e:  # noqa: BLE001 — lxml raises many types on bad markup
        _logger.warning(
            "eu_portal.description_parse_failed",
            extra={"event": "description_parse_failed", "error": str(e)},
        )
        return None
    lines = [line.strip() for line in raw.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def _summarize(text: str | None, *, limit: int = _SUMMARY_CHARS) -> str | None:
    """Take the opening of the call body as the summary.

    The API supplies no abstract: item-level `summary` and `content` both hold
    the parent call's title (e.g. "MSCA COFUND 2027"), which is not a
    description of anything. Rather than synthesise one, quote the start of the
    body verbatim and mark the cut. Cutting on a word boundary keeps the last
    word whole.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    head = flat[:limit]
    boundary = head.rfind(" ")
    if boundary > limit // 2:
        head = head[:boundary]
    return head.rstrip(" ,;:") + "…"


def _budget_bounds(raw: str | None, identifier: str) -> tuple[int | None, int | None]:
    """Extract per-grant funding bounds from `metadata.budgetOverview`.

    The field is a JSON *string* holding a nested map of actions. Two numbers
    are available per action and they are NOT interchangeable:

      * `budgetYearMap`     — the whole call's envelope (e.g. 51 251 165 EUR)
      * min/maxContribution — what a single project may request

    We take the per-grant pair, because `Call.budget_min/budget_max` are
    matched against the SAME fields on a research profile, where they mean "the
    size of project this group wants to run". Storing a 51M call envelope there
    would compare a programme against a project. Measured on 100 live records,
    scoped as below: every topic matched an action of its own, and 85 of them
    carried non-zero contribution bounds.

    Zero means "not stated" in this payload, not "zero euros".

    The map is keyed by the PARENT CALL's topics, so a topic's own record
    carries every sibling topic's budget too — reading the whole map gave
    HORIZON-CL5-2027-03-D5-05 and -D5-06 the identical span 2M-13.5M, which is
    the parent call's range, not either topic's. Each action's `action` string
    begins with the topic identifier it belongs to ("HORIZON-... - HORIZON-TMA-
    MSCA-Cofund-D ..."), and on a live 100-record sample every single topic
    matched at least one action that way. So select by identifier and fall back
    to nothing rather than to the parent's span: a wrong number here is worse
    than a missing one, because the profile match would silently use it.
    """
    if not raw:
        return (None, None)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return (None, None)
    if not isinstance(parsed, dict):
        return (None, None)
    mins: list[int] = []
    maxes: list[int] = []
    action_map = parsed.get("budgetTopicActionMap")
    if not isinstance(action_map, dict):
        return (None, None)
    for actions in action_map.values():
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            # "<topic identifier> - <type of action> <label>"
            owner = str(action.get("action") or "").split(" - ")[0].strip()
            if owner != identifier:
                continue
            for key, sink in (("minContribution", mins), ("maxContribution", maxes)):
                value = action.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                as_int = int(value)
                if as_int > 0:
                    sink.append(as_int)
    return (min(mins) if mins else None, max(maxes) if maxes else None)


def _keywords(metadata: dict) -> list[str]:
    """Real subject terms from `metadata.keywords`.

    The list leads with the topic's own identifier and its parent call's
    identifier — "HORIZON-MSCA-2027-COFUND-01-01", "HORIZON-MSCA-2027-COFUND-01"
    — before any actual subject term. Those are already stored as
    `external_id`; repeating them as keywords would put the identifier into the
    embedding text twice and tell the ranker nothing.
    """
    raw = metadata.get("keywords")
    if not isinstance(raw, list):
        return []
    identifiers = {
        value
        for value in (_first(metadata, "identifier"), _first(metadata, "callIdentifier"))
        if value
    }
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        term = entry.strip()
        if not term or term in identifiers or term in out:
            continue
        out.append(term)
    return out


def _programme(metadata: dict) -> str | None:
    identifier = _first(metadata, "identifier")
    if not identifier:
        return None
    prefix = identifier.split("-", 1)[0].upper()
    return _PROGRAMME_NAMES.get(prefix, prefix or None)


class EUPortalAdapter:
    """SourceAdapter for the EU Funding & Tenders Portal, via the SEDIA search API.

    The search response carries the full call body in `metadata.descriptionByte`,
    so this adapter does NO page scraping. The previous version fetched every
    topic-detail HTML page one at a time and needed a per-tick scrape cap and a
    wall-clock budget to stay tractable; all of that is gone, along with the
    `extraction_transport` seam it existed for. `needs_body` is still accepted
    because the SourceAdapter protocol passes it, and is ignored: there is no
    second request to skip.
    """

    slug = "eu_portal"

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        now: datetime | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        # Injectable so the expiry tests state a fixed instant instead of
        # depending on the wall clock and rotting next year.
        self._now = now

    def _query_parts(self) -> dict[str, tuple[None, str, str]]:
        """Build the typed multipart body. See note 2 at the top of this file —
        untyped parts make the API answer 500."""
        query = {
            "bool": {
                "must": [
                    {"terms": {"type": [_TYPE_GRANT_TOPIC]}},
                    {"terms": {"status": [_STATUS_FORTHCOMING, _STATUS_OPEN]}},
                ]
            }
        }
        return {
            "query": (None, json.dumps(query), "application/json"),
            "languages": (None, json.dumps(["en"]), "application/json"),
        }

    def fetch_new_calls(
        self, since: datetime, *, needs_body: NeedsBody | None = None
    ) -> Iterator[NormalizedCall]:
        # `since` is informational: this API has no "modified since" filter, and
        # the orchestrator's upsert is idempotent, so every run fetches the full
        # open+forthcoming set. `needs_body` is unused — see the class docstring.
        seen = 0
        now = self._now or datetime.now(UTC)
        try:
            api_transport = self._transport
            if api_transport is None:
                api_transport = pinned_transport_for(self._base_url)
                # pinned_transport_for pins the API base URL to prevent DNS
                # rebinding. Tests always inject self._transport, so this branch
                # is production-only.
                if api_transport is None:
                    # None is the REFUSAL signal (unsafe scheme, a blocked IP in
                    # any DNS answer, or DNS failure) — and `httpx.Client(
                    # transport=None)` builds the DEFAULT transport, so passing
                    # it through would connect UNPINNED on exactly the inputs the
                    # check rejected. Fail closed.
                    raise AdapterError(
                        f"EU Portal fetch refused: {self._base_url} is unsafe or "
                        "could not be resolved to a pinnable public address"
                    )
            with httpx.Client(
                base_url=self._base_url,
                transport=api_transport,
                timeout=self._timeout,
            ) as client:
                for page_number in range(1, _MAX_PAGES + 1):
                    resp = request_with_retry(
                        client,
                        "POST",
                        _SEARCH_PATH,
                        params={
                            "apiKey": _API_KEY,
                            "text": "***",
                            "pageSize": str(_PAGE_SIZE),
                            "pageNumber": str(page_number),
                        },
                        files=self._query_parts(),
                    )
                    resp.raise_for_status()
                    payload = resp.json()

                    results = payload.get("results", []) if isinstance(payload, dict) else []
                    if not results:
                        break
                    for item in results:
                        normalized = self._normalize(item, now=now)
                        if normalized is not None:
                            yield normalized
                    seen += len(results)

                    total = payload.get("totalResults") if isinstance(payload, dict) else None
                    if isinstance(total, int) and seen >= total:
                        break
                    if len(results) < _PAGE_SIZE:
                        # A short page means there is no next page.
                        break
        except httpx.HTTPError as e:
            raise AdapterError(f"EU Portal fetch failed: {e}") from e
        except ValueError as e:  # JSON decode
            raise AdapterError(f"EU Portal returned non-JSON body: {e}") from e

    def _normalize(self, item: dict, *, now: datetime) -> NormalizedCall | None:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            _logger.info(
                "eu_portal.skip_incomplete",
                extra={
                    "event": "skip_incomplete",
                    "fields_present": sorted(item.keys()) if isinstance(item, dict) else [],
                },
            )
            return None

        external_id = _first(metadata, "identifier")
        title = _first(metadata, "title")
        # `metadata.url` and the item-level `url` agree in the live payload;
        # prefer the metadata one and fall back, so one going missing is not
        # fatal.
        url = _first(metadata, "url") or (item.get("url") if isinstance(item, dict) else None)
        if not (external_id and title and url):
            _logger.info(
                "eu_portal.skip_incomplete",
                extra={"event": "skip_incomplete", "fields_present": sorted(metadata.keys())},
            )
            return None
        # Defense in depth against stored XSS — see is_safe_url docstring.
        if not is_safe_url(str(url)):
            _logger.warning(
                "eu_portal.skip_unsafe_url",
                extra={"event": "skip_unsafe_url", "external_id": str(external_id)},
            )
            return None

        # Drop a call whose every round has already closed. Measured on the
        # live feed: 590 of 1214 topics the API reports as open-or-forthcoming
        # have no round left — the portal's own status field says otherwise for
        # 352 of them, so the deadline is the only trustworthy signal.
        #
        # This costs no LLM money either way: `rank_pair` short-circuits a
        # FAILing timeline gate before any model call. What it saves is a local
        # embedding per call, a ranking_jobs row per active profile, and 590
        # rows of dashboard noise. A profile `must_include` keyword match
        # bypasses only the topic-similarity gate, not this one — it still
        # cannot reach the LLM once a call is dropped here as expired.
        #
        # A call with NO deadline stated is not expired and is kept.
        deadline = _pick_deadline(_deadlines(metadata), now)
        if deadline is not None and deadline <= now:
            _logger.info(
                "eu_portal.skip_expired",
                extra={
                    "event": "skip_expired",
                    "external_id": str(external_id),
                    "deadline": deadline.isoformat(),
                },
            )
            return None

        # `descriptionByte` is the call body. `topicConditions` and `supportInfo`
        # are deliberately NOT appended: both are legal/administrative
        # boilerplate that repeats near-verbatim across calls (2 distinct
        # openings across 5 sampled records), so including them would push the
        # same thousands of characters into every call's embedding and make the
        # corpus LESS separable, not more.
        full_text = _html_to_text(_first(metadata, "descriptionByte"))
        budget_min, budget_max = _budget_bounds(
            _first(metadata, "budgetOverview"), str(external_id)
        )

        return NormalizedCall(
            external_id=str(external_id),
            url=str(url),
            title=str(title),
            summary=_summarize(full_text),
            full_text=full_text,
            deadline=deadline,
            budget_min=budget_min,
            budget_max=budget_max,
            # The payload states no currency; every SEDIA budget figure is euro.
            currency="EUR",
            programme=_programme(metadata),
            keywords=_keywords(metadata),
            published_at=_parse_iso(_first(metadata, "startDate")),
            raw=item,
        )
