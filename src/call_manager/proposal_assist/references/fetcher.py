"""SSRF-safe HTTP fetch for references.

The Claude adapter launches with tools=[] + strict_mcp_config=True,
so the agent has NO built-in web access. All external fetches (DOI
resolution, PubMed XML, arXiv PDF, generic URLs) go through this
module, which:

1. Rejects non-http(s) schemes (no file://, gopher://, data:, etc.).
   Both http and https are allowed unconditionally; the private-IP
   block below is the actual SSRF defense.
2. Resolves DNS for validation, checks every returned IP against the
   private/internal range deny-list. Rejects if any address is private.
3. VALIDATES the resolved IP against the deny-list AND PINS the
   connection to that literal IP via _PinnedResolverTransport. httpx no
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
import ipaddress
import json
import logging
import socket
import urllib.parse
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Generic fetch failure (network, timeout, oversize, parse)."""


class SSRFError(FetchError):
    """Refused for SSRF safety (private IP, bad scheme, etc.)."""


# http permitted alongside https; the private-IP block is the real SSRF defense.
_ALLOWED_SCHEMES = frozenset({"https", "http"})
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 60.0
_MAX_REDIRECTS = 5
_CHUNK_BYTES = 64 * 1024


def _is_private_ip(ip_str: str) -> bool:
    """True if the IP falls in a private/internal range we refuse to
    talk to: RFC1918, loopback, link-local (incl 169.254/16 AWS
    metadata), ULA, CGNAT, IPv6 loopback + link-local + ULA."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → reject defensively
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_validate(host: str, port: int) -> str:
    """Resolve host to IPs, reject if any is private. Return the first
    validated IP for connection-pinning.

    If the host is already a literal IP, validate it directly without a
    DNS lookup (so we can refuse private literal-IP URLs in environments
    without working DNS, e.g., the test suite)."""
    # Literal IP shortcut: ipaddress.ip_address accepts plain IPv4/IPv6
    # but not bracketed IPv6 — urlparse already strips brackets from
    # hostname, so we receive the bare form.
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if _is_private_ip(host):
            raise SSRFError(f"refused to connect to {host!r} → private IP {host!r}")
        return host

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise FetchError(f"no DNS answers for {host!r}")
    addrs = [info[4][0] for info in infos]
    for addr in addrs:
        if _is_private_ip(addr):
            raise SSRFError(f"refused to connect to {host!r} → private IP {addr!r}")
    return addrs[0]


class _PinnedNetworkBackend:
    """Wraps httpcore's network backend so TCP connects go to a
    pre-validated IP instead of a freshly-resolved hostname.

    DNS-rebinding SSRF defense: the caller resolves the host ONCE to a
    public IP (via _resolve_and_validate), then this backend forces the
    socket connect to that literal IP. TLS SNI and certificate validation
    are unaffected — httpcore derives those from the connection ORIGIN
    (the request URL's hostname), not from the ``host`` argument to
    ``connect_tcp`` — so the Host header and cert hostname stay correct
    while the socket targets the pinned IP. No URL rewriting, so request
    mocking and Host headers behave normally.
    """

    def __init__(self, inner, pin_map: dict[str, str]):
        self._inner = inner
        self._pin_map = pin_map

    def connect_tcp(self, host: str, port: int, *args, **kwargs):
        pinned = self._pin_map.get(host)
        if pinned is None:
            # Defense in depth: a connect for a host we didn't pre-pin must NOT
            # fall through to connecting the raw (re-resolvable) hostname — that
            # would reopen the DNS-rebinding window the pin map exists to close.
            # Validate-and-pin it on the fly so every connect targets a
            # confirmed-public literal IP (raises SSRFError on a private one).
            pinned = _resolve_and_validate(host, port)
        return self._inner.connect_tcp(pinned, port, *args, **kwargs)

    def __getattr__(self, name):
        # Delegate connect_unix_socket / anything else to the real backend.
        return getattr(self._inner, name)


def _pinned_transport(pin_map: dict[str, str]) -> httpx.HTTPTransport:
    """Build an HTTPTransport whose network backend pins hostnames in
    ``pin_map`` to their validated IPs at socket-connect time."""
    transport = httpx.HTTPTransport()
    pool = transport._pool
    pool._network_backend = _PinnedNetworkBackend(pool._network_backend, pin_map)
    return transport


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
      to private IP).
    - FetchError on HTTP error (4xx/5xx), oversize, timeout, DNS
      failure, or parse failure.
    """
    current_url = url
    seen_urls: set[str] = set()

    for _redirect_num in range(_MAX_REDIRECTS + 1):
        if current_url in seen_urls:
            raise FetchError(f"redirect loop at {current_url!r}")
        seen_urls.add(current_url)

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
                    timeout=timeout_s,
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
                    # read() the (error) body so we can echo a snippet.
                    response.read()
                    raise FetchError(
                        f"HTTP {response.status_code} for {current_url!r}: {response.text[:200]!r}"
                    )

                # Accumulate chunks, aborting the moment the running
                # total exceeds max_bytes.
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=_CHUNK_BYTES):
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


def _sniff_mime(body: bytes, content_type: str, filename: str) -> str:
    """Determine a reference's MIME by trusting the BYTES and the server's
    declared Content-Type over the URL string.

    Priority:
      1. Unambiguous magic numbers (most trustworthy — the URL suffix is
         attacker-controlled and the old code blindly defaulted unknown
         content to application/pdf).
      2. The response Content-Type, if it is a type we can actually extract.
      3. The URL filename suffix (legacy fallback).
    """
    head = body[:8]
    if head[:5] == b"%PDF-":
        return "application/pdf"
    if head[:5] == b"{\\rtf":
        return "application/rtf"
    base_ct = content_type.split(";")[0].strip().lower()
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
            "source": "generic",
        },
    )
