"""SSRF-safe HTTP fetch for references.

The Claude adapter launches with tools=[] + strict_mcp_config=True,
so the agent has NO built-in web access. All external fetches (DOI
resolution, PubMed XML, arXiv PDF, generic URLs) go through this
module, which:

1. Rejects non-http(s) schemes (no file://, gopher://, data:, etc.).
   Both http and https are allowed unconditionally; the private-IP
   block below is the actual SSRF defense.
2. Resolves DNS for validation, checks every returned IP against the
   private/internal range deny-list in `grant_lab.net_safety` (`not
   ip.is_global` plus the explicit RFC1918/loopback/link-local/multicast/
   reserved/unspecified checks, so CGNAT 100.64.0.0/10 is refused too).
   Rejects if any address is private.
3. VALIDATES the resolved IP against the deny-list AND PINS the
   connection to that literal IP via `net_safety.PinnedNetworkBackend`.
   httpx no
   longer re-resolves the hostname at connect time, so a determined
   attacker rebinding DNS between validation and fetch cannot flip to a
   private IP — we connect to the exact IP we validated. The original
   Host header and TLS SNI/cert validation are preserved.

   Residual: the pin map keys on the hostname string, so this defeats
   classic time-of-check/time-of-use rebinding. It does NOT cover a
   server that legitimately resolves to multiple IPs where one rotates
   private mid-session; we pin the FIRST validated answer per hop, which
   is the standard SSRF-pinning posture.
4. Re-validates every redirect target up to a configurable max-redirect
   count.
5. Caps response size at max_bytes; reads in chunks.

Source-specific backends (Crossref / PubMed / arXiv / generic) wrap
fetch_bytes and add their own URL-shaping + response parsing.

Per spec rev4 Codex High #2 (with rev5 honesty pass on pinning).
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.parse
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from grant_lab.net_safety import (
    SSRF_REFUSAL_MESSAGE,
    BlockedAddress,
    ResolutionFailed,
    pinned_transport,
    resolve_public_ip,
)

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Generic fetch failure (network, timeout, oversize, parse)."""


class SSRFError(FetchError):
    """Refused for SSRF safety (private IP, bad scheme, etc.).

    `str(e)` is the LOG detail — it names the hostname and the address it
    resolved to, which is what an internal-network map is made of.
    `member_message` is the only thing a handler may put on the wire.

    The answer hangs off the exception rather than being looked up because
    looking it up is what both handlers got wrong: Task 20 put the generic
    sentence on the web route and the agent tool went on returning the detail
    to the model for another commit (review 2026-10-26 M-sec-1). A handler that
    has the error now has the answer. The string itself is defined once, in
    `grant_lab.net_safety`.
    """

    member_message = SSRF_REFUSAL_MESSAGE


class UnresolvableHostError(SSRFError):
    """DNS returned nothing usable for the host.

    A SUBCLASS of `SSRFError`, which is the whole point. It is not an SSRF
    violation — nothing unsafe was reached — but it has to produce the
    byte-identical answer, because the DIFFERENCE was the oracle (review
    2026-10-26, M-sec-1's second half): a refusal answered 400 "That address
    isn't allowed." while an unresolvable name answered 502 "DNS resolution
    failed for 'pacs.hospital.internal': …", so a 400 uniquely meant "this
    name resolves to an address inside your network" — the hostname half of
    the same question, with the name echoed back on top.

    Subclassing rather than adding a second `except` arm at each call site
    means a handler cannot reopen the differential by forgetting one: every
    `except SSRFError` already covers it. A genuine upstream failure on a host
    that DID resolve publicly stays a plain `FetchError` and keeps its detail
    and its 502 — otherwise every broken server would look like a policy
    refusal, which is undebuggable for the member.
    """


# http permitted alongside https; the private-IP block is the real SSRF defense.
_ALLOWED_SCHEMES = frozenset({"https", "http"})
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 60.0
# Review 2026-09-02 §2.5(b)(c): bounds INSIDE one hop. `timeout_s` is a single
# deadline for the whole fetch; httpx only knows a PER-READ timeout, so a body
# that drips one byte every (timeout_s - ε) seconds never tripped it and held an
# ingest slot for as long as it liked (measured: 12 bytes over 4.5 s under
# timeout_s=1.0, no error). The read loop now re-checks the deadline between
# pieces, and the per-read timeout is capped so one silent read cannot eat the
# whole budget before that check runs.
_READ_TIMEOUT_S = 10.0
# An error status used to `response.read()` its whole body for a 200-char
# snippet — 200 MB served under max_bytes=1 MiB was read in full. One small
# piece is all the message needs.
_ERROR_SNIPPET_BYTES = 4096
_MAX_REDIRECTS = 5


def _resolve_and_validate(host: str, port: int) -> str:
    """`net_safety.resolve_public_ip` in this module's error domain. Both
    outcomes are member-facing refusals — an `SSRFError` for an address we
    refuse, an `UnresolvableHostError` (its subclass) for a name DNS cannot
    answer, because telling those two apart from outside is the oracle
    (M-sec-1). The address checks and the single-lookup rule live in
    `grant_lab.net_safety` — one copy for this module and the ingestion
    adapters both (review 2026-10-26 A7).

    `from e`, not `from None`: the response body stays generic, but the
    server-side traceback keeps the check that fired and, for DNS, the
    resolver's own `socket.gaierror` — EAI_NONAME and EAI_AGAIN are different
    operational problems, and debuggability is the entire argument for keeping
    the detail anywhere (review 2026-10-26 Minor 4).

    Patched by tests that need a fetch to reach a stubbed transport without a
    resolver, so it stays a module-level name here.
    """
    try:
        return resolve_public_ip(host, port)
    except BlockedAddress as e:
        raise SSRFError(str(e)) from e
    except ResolutionFailed as e:
        raise UnresolvableHostError(str(e)) from e


def _pinned_transport(pin_map: dict[str, str]) -> httpx.HTTPTransport:
    """An HTTPTransport that pins the hostnames in ``pin_map`` to their
    validated IPs at socket-connect time, refusing anything else through this
    module's own resolver."""
    return pinned_transport(pin_map, resolve=_resolve_and_validate)


def _validate_url(url: str) -> tuple[str, str, int, str]:
    """Returns (scheme, host, port, path-and-query). Raises SSRFError
    on any safety violation.

    Both http and https are accepted — the private-IP check in
    _resolve_and_validate is the security-critical defense. Gating
    http:// here would produce less actionable errors ("scheme" vs
    "private") for the common private-IP-over-http case."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"refused: scheme {scheme!r} not allowed")
    if not parsed.hostname:
        raise SSRFError(f"refused: no hostname in {url!r}")
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, parsed.hostname, port, path


def fetch_bytes(
    url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    user_agent: str = "call-manager-references/1.0",
) -> bytes:
    """Fetch the bytes at `url` (see _fetch_url_bytes); returns only the body.

    Kept as the stable entry point for callers that don't need the
    server-declared Content-Type (arXiv/PubMed/DOI shape their own MIME).
    """
    body, _content_type = _fetch_url_bytes(
        url, max_bytes=max_bytes, timeout_s=timeout_s, user_agent=user_agent
    )
    return body


def _fetch_url_bytes(
    url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    user_agent: str = "call-manager-references/1.0",
) -> tuple[bytes, str]:
    """Fetch the bytes at `url`, following up to _MAX_REDIRECTS hops,
    enforcing SSRF defenses at every hop, capping at max_bytes.

    Returns (raw bytes, response Content-Type) on success. Raises:
    - SSRFError on any safety violation (scheme, private IP, redirect
      to private IP), and its UnresolvableHostError subclass when DNS
      cannot answer the name — one indistinguishable refusal for both
      (review 2026-10-26 M-sec-1).
    - FetchError on HTTP error (4xx/5xx), oversize, timeout, or parse
      failure — i.e. only for a host that DID resolve publicly, so this
      arm can keep its detail.
    """
    current_url = url
    seen_urls: set[str] = set()

    # Compute a SINGLE deadline for the entire redirect chain — not per hop.
    # This prevents a stalling server on 5 hops from consuming 5×timeout_s.
    deadline_time = time.monotonic() + timeout_s

    # Cumulative byte accumulator across ALL redirect hops. Resetting it per
    # hop would allow 5 hops × (max_bytes-1) bytes = 5× the intended cap.
    total = 0

    for _redirect_num in range(_MAX_REDIRECTS + 1):
        if current_url in seen_urls:
            raise FetchError(f"redirect loop at {current_url!r}")
        seen_urls.add(current_url)

        # Enforce the wall-clock budget BEFORE attempting each hop.
        remaining_s = deadline_time - time.monotonic()
        if remaining_s <= 0:
            raise FetchError(f"fetch budget exhausted for {url!r}")

        scheme, host, port, _ = _validate_url(current_url)
        # Resolve + validate ONCE, then pin the connection to that literal
        # IP. _pinned_transport routes the TCP connect to the validated IP
        # instead of letting httpx re-resolve the hostname at connect time,
        # closing the DNS-rebinding window. The original Host header and TLS
        # SNI/cert validation are preserved. We re-resolve and re-pin on
        # EVERY redirect hop (loop continues below).
        pinned_ip = _resolve_and_validate(host, port)
        transport = _pinned_transport({host: pinned_ip})

        try:
            # follow_redirects=False so we re-validate each hop ourselves.
            # Stream so we can enforce the size cap incrementally and abort
            # as soon as max_bytes is exceeded — a non-streaming
            # client.get() would materialise the entire (potentially
            # multi-GB) body into memory BEFORE the len() check.
            with (
                httpx.Client(
                    follow_redirects=False,
                    timeout=min(remaining_s, _READ_TIMEOUT_S),
                    headers={"User-Agent": user_agent},
                    transport=transport,
                ) as client,
                client.stream("GET", current_url) as response,
            ):
                # Redirect?
                if response.status_code in (301, 302, 303, 307, 308):
                    loc = response.headers.get("location")
                    if not loc:
                        raise FetchError(f"{response.status_code} response missing Location header")
                    # Resolve relative redirects against the current URL.
                    from urllib.parse import urljoin

                    current_url = urljoin(current_url, loc)
                    continue

                if response.status_code >= 400:
                    # Echo a SNIPPET of the error body — one bounded piece, never
                    # response.read(): the streaming cap below covers only the
                    # 2xx branch, and an attacker-controlled 5xx body is as big as
                    # they like.
                    snippet = next(response.iter_bytes(_ERROR_SNIPPET_BYTES), b"")
                    text = snippet[:_ERROR_SNIPPET_BYTES].decode("utf-8", errors="replace")
                    raise FetchError(
                        f"HTTP {response.status_code} for {current_url!r}: {text[:200]!r}"
                    )

                # Accumulate chunks, aborting the moment the running
                # total exceeds max_bytes. `total` is cumulative across
                # all redirect hops — a bomb can't reset the budget by
                # redirecting to a fresh hop.
                chunks: list[bytes] = []
                # No chunk_size: httpx would otherwise BUFFER pieces until it has
                # that many bytes, and the deadline check below would run only
                # after a whole 64 KiB of drip had arrived (verified against
                # httpx 0.28's ByteChunker). Bare iter_bytes() yields each
                # decoded piece as the transport delivers it.
                for chunk in response.iter_bytes():
                    # The deadline is a wall-clock budget for the WHOLE fetch;
                    # checking it only between hops let a slow drip run forever.
                    if time.monotonic() > deadline_time:
                        raise FetchError(f"fetch budget exhausted for {url!r} (slow body)")
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(
                            f"response from {current_url!r} exceeds max_bytes={max_bytes}"
                        )
                    chunks.append(chunk)
                content_type = response.headers.get("content-type", "")
                return b"".join(chunks), content_type
        except httpx.RequestError as e:
            raise FetchError(f"request failed for {current_url!r}: {e}") from e

    raise FetchError(f"too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}")


_FILENAME_TO_MIME = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".rtf": "application/rtf",
    ".csv": "text/csv",
    # Legacy .xls (BIFF) is unsupported downstream (openpyxl reads OOXML only).
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ipynb": "application/x-ipynb+json",
}


def _infer_mime_from_filename(filename: str) -> str:
    for suffix, mime in _FILENAME_TO_MIME.items():
        if filename.lower().endswith(suffix):
            return mime
    return "application/pdf"  # most common fallback for academic content


_ALLOWED_SNIFF_MIMES = frozenset(_FILENAME_TO_MIME.values())

#: Types nothing downstream can extract, and that a URL fetch hits constantly:
#: an ordinary web page. Refused by NAME instead of falling through to
#: _infer_mime_from_filename, whose default is application/pdf — which is how a
#: web page used to reach the PDF extractor and die there with
#: "No /Root object!" after spending a fetch slot (review 2026-10-26 M-PA-5).
_HTML_SNIFF_MIMES = frozenset({"text/html", "application/xhtml+xml"})

#: What the member is told. A page is not a reference, and the useful next step
#: is a real document, so say that rather than reporting a parser failure.
HTML_UNSUPPORTED_MESSAGE = "HTML pages aren't supported — upload a PDF or give a direct PDF link"


def _sniff_mime(body: bytes, content_type: str, filename: str) -> str:
    """Determine a reference's MIME by trusting the BYTES and the server's
    declared Content-Type over the URL string.

    Priority:
      1. Unambiguous magic numbers (most trustworthy — the URL suffix is
         attacker-controlled and the old code blindly defaulted unknown
         content to application/pdf).
      2. The response Content-Type, if it is a type we can actually extract.
      3. The URL filename suffix (legacy fallback).

    Raises FetchError for an HTML page, BEFORE any extractor sees the bytes.
    """
    head = body[:8]
    if head[:5] == b"%PDF-":
        return "application/pdf"
    if head[:5] == b"{\\rtf":
        return "application/rtf"
    base_ct = content_type.split(";")[0].strip().lower()
    # After the magic-number checks on purpose: a real PDF served with a sloppy
    # text/html header is still a PDF, and the bytes outrank the header.
    if base_ct in _HTML_SNIFF_MIMES:
        raise FetchError(HTML_UNSUPPORTED_MESSAGE)
    if base_ct in _ALLOWED_SNIFF_MIMES:
        return base_ct
    return _infer_mime_from_filename(filename)


def fetch_crossref_doi(doi: str) -> tuple[bytes, str, str, dict]:
    """Resolve a DOI via Crossref API, find the publisher PDF link,
    fetch it via fetch_bytes."""
    # Percent-encode the DOI so characters significant in a URL path/query
    # (e.g. '#', '?', '/') can't break out of the works/ path or inject
    # query parameters into the Crossref request.
    api_url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    meta_bytes = fetch_bytes(api_url, max_bytes=1 * 1024 * 1024)  # 1 MB cap on metadata
    try:
        api_data = json.loads(meta_bytes)
    except json.JSONDecodeError as e:
        raise FetchError(f"Crossref returned non-JSON for DOI {doi!r}: {e}") from e

    message = api_data.get("message", {})
    links = message.get("link", []) or []
    pdf_links = [
        link.get("URL") for link in links if "pdf" in (link.get("content-type") or "").lower()
    ]
    if not pdf_links:
        raise FetchError(
            f"Crossref returned no PDF link for DOI {doi!r} — "
            f"paper may be paywalled. Upload the PDF manually."
        )
    pdf_url = pdf_links[0]
    pdf_bytes = fetch_bytes(pdf_url)

    title = " ".join(message.get("title", []))[:200] or doi
    filename = f"doi-{doi.replace('/', '_')}.pdf"
    meta: dict = {
        "doi": doi,
        "title": title,
        "source": "crossref",
    }
    # Include the full Crossref message dict so csl_from_source_meta can
    # consume author/issued/container-title without a second API call.
    # Defensive: api_data.get("message") may be None/absent if the JSON
    # shape is unexpected — omit the key rather than storing None.
    raw_message = api_data.get("message")
    if raw_message is not None:
        meta["message"] = raw_message
    return (
        pdf_bytes,
        filename,
        "application/pdf",
        meta,
    )


def fetch_pubmed_pmid(pmid: str) -> tuple[bytes, str, str, dict]:
    """Convert PMID → PMC ID via elink; fetch PMC full-text XML via
    efetch. Raises FetchError if the article isn't in PMC (not open-
    access)."""
    elink_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id={pmid}"
    )
    elink_bytes = fetch_bytes(elink_url, max_bytes=512 * 1024)

    try:
        # defusedxml refuses entity-expansion / external-entity / DTD
        # attacks (billion laughs) that the stdlib parser is vulnerable to.
        root = DET.fromstring(elink_bytes)
    except DefusedXmlException as e:
        raise FetchError(f"elink returned unsafe XML for PMID {pmid!r}: {e}") from e
    except DET.ParseError as e:
        raise FetchError(f"elink returned non-XML for PMID {pmid!r}: {e}") from e
    pmc_ids = [el.text for el in root.iter("Id") if el.text]
    if not pmc_ids:
        raise FetchError(
            f"PMID {pmid!r} has no PMC mapping (likely closed-access). Upload the PDF manually."
        )
    pmc_id = pmc_ids[0]

    efetch_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pmc&id={urllib.parse.quote(pmc_id, safe='')}&rettype=xml"
    )
    xml_bytes = fetch_bytes(efetch_url, max_bytes=10 * 1024 * 1024)

    meta: dict = {
        "pmid": pmid,
        "pmc_id": pmc_id,
        "source": "pubmed",
    }
    # Parse JATS XML for bibliographic fields. Non-fatal: if the XML is
    # malformed or lacks expected elements we just return the IDs.
    try:
        article_root = DET.fromstring(xml_bytes)
        # article-title: PMC efetch JATS is namespace-free; the lookup
        # below works correctly for that format.  If a document were served
        # with a namespaced JATS root (uncommon in PMC efetch), the
        # namespace-free find would return None and title/authors would
        # simply be absent from meta — non-fatal, falls back to IDs.
        title_el = article_root.find(".//article-title")
        if title_el is not None:
            title_text = "".join(title_el.itertext()).strip()
            if title_text:
                meta["title"] = title_text

        # Contributors: .//contrib[@contrib-type='author']/name
        authors: list[dict[str, str]] = []
        for contrib in article_root.findall(".//contrib[@contrib-type='author']"):
            name_el = contrib.find("name")
            if name_el is None:
                continue
            surname_el = name_el.find("surname")
            given_el = name_el.find("given-names")
            if surname_el is not None and surname_el.text:
                entry: dict[str, str] = {"family": surname_el.text.strip()}
                if given_el is not None and given_el.text:
                    entry["given"] = given_el.text.strip()
                authors.append(entry)
        if authors:
            meta["authors"] = authors
    except (DefusedXmlException, DET.ParseError) as exc:
        logger.warning("PubMed JATS XML parse failed for PMID %r / %r: %s", pmid, pmc_id, exc)

    return (
        xml_bytes,
        f"pmid-{pmid}-{pmc_id}.xml",
        "application/xml",
        meta,
    )


_ATOM_NS = "http://www.w3.org/2005/Atom"


def _fetch_arxiv_atom_meta(arxiv_id: str) -> dict:
    """Fetch and parse the arXiv Atom API entry for ``arxiv_id``.

    Returns a (possibly empty) dict with keys: title, authors (list of
    str), year (int), url (str).  Never raises — logs and returns {} on
    any fetch or parse failure so that the PDF fetch can proceed.
    """
    atom_url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id, safe='')}"
    try:
        atom_bytes = fetch_bytes(atom_url, max_bytes=256 * 1024)
    except FetchError as exc:
        logger.warning("arXiv Atom fetch failed for %r: %s", arxiv_id, exc)
        return {}

    try:
        root = DET.fromstring(atom_bytes)
    except (DefusedXmlException, DET.ParseError) as exc:
        logger.warning("arXiv Atom XML parse failed for %r: %s", arxiv_id, exc)
        return {}

    entry = root.find(f"{{{_ATOM_NS}}}entry")
    if entry is None:
        logger.warning("arXiv Atom response for %r has no <entry>", arxiv_id)
        return {}

    meta: dict = {}

    title_el = entry.find(f"{{{_ATOM_NS}}}title")
    if title_el is not None and title_el.text:
        meta["title"] = title_el.text.strip()

    authors = [
        name_el.text.strip()
        for author_el in entry.findall(f"{{{_ATOM_NS}}}author")
        for name_el in [author_el.find(f"{{{_ATOM_NS}}}name")]
        if name_el is not None and name_el.text
    ]
    if authors:
        meta["authors"] = authors

    published_el = entry.find(f"{{{_ATOM_NS}}}published")
    if published_el is not None and published_el.text and len(published_el.text) >= 4:
        with contextlib.suppress(ValueError):
            meta["year"] = int(published_el.text[:4])

    id_el = entry.find(f"{{{_ATOM_NS}}}id")
    if id_el is not None and id_el.text:
        meta["url"] = id_el.text.strip()

    return meta


def fetch_arxiv(arxiv_id: str) -> tuple[bytes, str, str, dict]:
    """arXiv ID → arxiv.org/pdf/<id>.pdf (open access by default).

    Also fetches the Atom API metadata entry and merges title, authors,
    year, url into the returned meta dict.  Atom fetch/parse failure is
    non-fatal: the PDF is returned with just arxiv_id + source.
    """
    atom_meta = _fetch_arxiv_atom_meta(arxiv_id)
    pdf_url = f"https://arxiv.org/pdf/{urllib.parse.quote(arxiv_id, safe='')}.pdf"
    pdf_bytes = fetch_bytes(pdf_url)
    meta: dict = {
        "arxiv_id": arxiv_id,
        "source": "arxiv",
    }
    meta.update(atom_meta)
    return (
        pdf_bytes,
        f"arxiv-{arxiv_id}.pdf",
        "application/pdf",
        meta,
    )


def fetch_generic_url(url: str) -> tuple[bytes, str, str, dict]:
    """Generic URL fetch via SSRF-safe _fetch_url_bytes. The MIME is sniffed
    from the response bytes + server Content-Type (NOT blindly from the URL
    suffix), so a crafted URL can't dictate how the downstream parser treats
    the content. Filename is derived from the URL path."""
    body, content_type = _fetch_url_bytes(url)
    parsed = urlparse(url)
    filename = parsed.path.split("/")[-1] or "fetched-reference"
    mime = _sniff_mime(body, content_type, filename)
    return (
        body,
        filename,
        mime,
        {
            "url": url,
            "source": "web",
        },
    )
