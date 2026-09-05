from __future__ import annotations

import logging
import re
import time

import httpx

from grant_lab.profiles.seed import Member, Publication

_logger = logging.getLogger(__name__)

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_RETMAX = 20  # publications per member; 20 is plenty for bootstrap signal

# Status codes worth a single retry: 429 (rate-limited) and 503 (service
# briefly unavailable) are transient on the E-utilities endpoint. Anything
# else (4xx auth/usage, malformed JSON) is not retried.
_RETRY_STATUSES = frozenset({429, 503})


class PubMedError(Exception):
    """PubMed request failed — bootstrap treats this as partial-failure
    and falls back to pasted grants only."""


class PubMedClient:
    """Thin E-utilities client over esearch + esummary.

    Rate-limit conscious: sleeps `sleep_between_members` seconds between
    each member's query. NCBI permits 3 req/s unauthenticated; 350 ms
    keeps us comfortably under that.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep_between_members: float = 0.35,
        api_key: str | None = None,
        tool: str | None = None,
        email: str | None = None,
        retry_backoff: float = 0.5,
    ):
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep_between_members
        # NCBI identification + higher rate limit. All optional: when None
        # they're simply omitted from the query. `tool`/`email` are NCBI's
        # documented courtesy identifiers; `api_key` lifts the unauthenticated
        # 3 req/s cap to 10 req/s.
        self._api_key = api_key
        self._tool = tool
        self._email = email
        # Backoff before the single retry on a transient (429/503) status.
        self._retry_backoff = retry_backoff

    def _common_params(self) -> dict[str, str]:
        """Identification params shared by every E-utilities request."""
        params: dict[str, str] = {}
        if self._api_key:
            params["api_key"] = self._api_key
        if self._tool:
            params["tool"] = self._tool
        if self._email:
            params["email"] = self._email
        return params

    @classmethod
    def from_settings(cls, settings, **kwargs) -> PubMedClient:
        """Build a client reading optional NCBI identification from config.

        Reads `pubmed_api_key` / `pubmed_tool` / `pubmed_email` off the
        Settings object if present (via getattr so a config without those
        fields degrades to None rather than raising). Kept tolerant so adding
        the config fields later is a no-code-change opt-in.
        """
        return cls(
            api_key=getattr(settings, "pubmed_api_key", None),
            tool=getattr(settings, "pubmed_tool", None),
            email=getattr(settings, "pubmed_email", None),
            **kwargs,
        )

    def fetch_publications(self, members: list[Member]) -> list[Publication]:
        """Return all publications across the given members.

        Members with zero results are logged and skipped; HTTP errors
        raise PubMedError (the whole fetch fails, caller decides on
        fallback)."""
        pubs: list[Publication] = []
        with httpx.Client(
            base_url=_BASE, transport=self._transport, timeout=self._timeout
        ) as client:
            for i, member in enumerate(members):
                if i > 0:
                    time.sleep(self._sleep)
                query = _query_for(member)
                pmids = self._esearch(client, query)
                if not pmids:
                    _logger.info(
                        "pubmed.no_results",
                        extra={"event": "pubmed_no_results", "member": _describe(member)},
                    )
                    continue
                for pub in self._esummary(client, pmids):
                    pubs.append(pub)
        return pubs

    def _get_with_retry(self, client: httpx.Client, path: str, params: dict) -> httpx.Response:
        """GET with a single retry on a transient (429/503) status.

        NCBI's E-utilities occasionally rate-limit (429) or briefly 503. One
        short backoff + retry smooths over those without giving up on the
        whole bootstrap. The 3 req/s pacing in `fetch_publications` still
        applies between members; this is an extra in-request safety net.
        Raises httpx.HTTPStatusError if the retry also fails (caller converts
        to PubMedError).
        """
        merged = {**self._common_params(), **params}
        last_exc: httpx.HTTPStatusError | None = None
        for attempt in range(2):  # initial + one retry
            resp = client.get(path, params=merged)
            if resp.status_code in _RETRY_STATUSES and attempt == 0:
                _logger.info(
                    "pubmed.retry",
                    extra={
                        "event": "pubmed_retry",
                        "path": path,
                        "status": resp.status_code,
                    },
                )
                if self._retry_backoff:
                    time.sleep(self._retry_backoff)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                last_exc = e
                break
            return resp
        # Both attempts exhausted on a retryable status, or a non-retryable
        # error status on the first try.
        if last_exc is not None:
            raise last_exc
        resp.raise_for_status()  # transient status persisted past the retry
        return resp

    def _esearch(self, client: httpx.Client, query: str) -> list[str]:
        try:
            resp = self._get_with_retry(
                client,
                "/esearch.fcgi",
                {
                    "db": "pubmed",
                    "term": query,
                    "retmax": _RETMAX,
                    "retmode": "json",
                    "sort": "date",
                },
            )
            data = resp.json()
        except httpx.HTTPError as e:
            raise PubMedError(f"PubMed esearch failed: {e}") from e
        except ValueError as e:
            raise PubMedError(f"PubMed esearch returned non-JSON: {e}") from e
        return list(data.get("esearchresult", {}).get("idlist", []) or [])

    def _esummary(self, client: httpx.Client, pmids: list[str]) -> list[Publication]:
        if not pmids:
            return []
        try:
            resp = self._get_with_retry(
                client,
                "/esummary.fcgi",
                {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
            )
            data = resp.json()
        except httpx.HTTPError as e:
            raise PubMedError(f"PubMed esummary failed: {e}") from e
        except ValueError as e:
            raise PubMedError(f"PubMed esummary returned non-JSON: {e}") from e

        result = data.get("result", {})
        pubs: list[Publication] = []
        for pmid in result.get("uids", []):
            rec = result.get(pmid)
            if not isinstance(rec, dict):
                continue
            year = _parse_year(rec.get("pubdate"))
            authors = [
                a.get("name", "")
                for a in rec.get("authors", [])
                if isinstance(a, dict) and a.get("name")
            ]
            pubs.append(
                Publication(
                    pmid=str(pmid),
                    title=str(rec.get("title") or ""),
                    year=year,
                    authors=authors,
                )
            )
        return pubs


# Entrez term metacharacters / boolean operators we must neutralize so a
# member's name or affiliation can't smuggle field tags ("[au]"), boolean
# logic (AND/OR/NOT), or grouping/quoting into the constructed query.
_ENTREZ_METACHARS = re.compile(r"[\[\]()\"]")
_ENTREZ_BOOLEANS = re.compile(r"\b(AND|OR|NOT)\b")


def _sanitize_entrez_value(value: str) -> str:
    """Strip Entrez metacharacters and field tags from a user-supplied value
    so it can be safely embedded in a term as a quoted phrase.

    Removes brackets/parens/quotes (which could open a field tag like ``[au]``
    or close our own quoting), neutralizes the boolean operators AND/OR/NOT,
    and collapses whitespace. The result is wrapped in double quotes by the
    caller so it is matched as a literal phrase.
    """
    cleaned = _ENTREZ_METACHARS.sub(" ", value)
    cleaned = _ENTREZ_BOOLEANS.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _query_for(member: Member) -> str:
    if member.orcid:
        # ORCID in PubMed's AUID field. Bare ORCID is the documented
        # term syntax; some indexers also accept orcid:... — the portal
        # treats them equivalently. ORCID format is validated at Member
        # construction, so no metacharacters can appear here.
        return f"{member.orcid}[AUID]"
    # name + affiliation fallback. [au] is the author field, [ad] the
    # affiliation field. Both are fuzzy by nature. Sanitize + quote the
    # user-supplied values so they can't inject field tags or boolean
    # operators into the Entrez term (query-injection defense).
    name = _sanitize_entrez_value(member.name or "")
    affiliation = _sanitize_entrez_value(member.affiliation or "")
    return f'"{name}"[au] AND "{affiliation}"[ad]'


def _describe(member: Member) -> str:
    if member.orcid:
        return f"orcid={member.orcid}"
    return f"{member.name} @ {member.affiliation}"


def _parse_year(pubdate: str | None) -> int | None:
    """PubMed pubdate is like '2024 Jan', '2023', '2024 Dec 15'. Take
    the first 4-digit token."""
    if not pubdate:
        return None
    for tok in str(pubdate).split():
        if tok.isdigit() and len(tok) == 4:
            try:
                return int(tok)
            except ValueError:
                return None
    return None
