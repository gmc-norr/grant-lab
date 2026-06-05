from __future__ import annotations

import logging
import re
import time

import httpx

from call_manager.profiles.seed import Member, Publication

_logger = logging.getLogger(__name__)

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_RETMAX = 20  # publications per member; 20 is plenty for bootstrap signal


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
    ):
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep_between_members

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
                pmids = _esearch(client, query)
                if not pmids:
                    _logger.info(
                        "pubmed.no_results",
                        extra={"event": "pubmed_no_results", "member": _describe(member)},
                    )
                    continue
                for pub in _esummary(client, pmids):
                    pubs.append(pub)
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


def _esearch(client: httpx.Client, query: str) -> list[str]:
    try:
        resp = client.get(
            "/esearch.fcgi",
            params={
                "db": "pubmed",
                "term": query,
                "retmax": _RETMAX,
                "retmode": "json",
                "sort": "date",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise PubMedError(f"PubMed esearch failed: {e}") from e
    except ValueError as e:
        raise PubMedError(f"PubMed esearch returned non-JSON: {e}") from e
    return list(data.get("esearchresult", {}).get("idlist", []) or [])


def _esummary(client: httpx.Client, pmids: list[str]) -> list[Publication]:
    if not pmids:
        return []
    try:
        resp = client.get(
            "/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
        )
        resp.raise_for_status()
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
